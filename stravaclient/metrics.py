"""Derived training metrics computed at sync time.

Reuses ZoneAnalyzer and WorkoutDetector from the original analyzer so the
numbers in the database match what main.py prints for a single activity.
"""

import json
import os
import sqlite3
from datetime import date
from typing import Dict, Optional

from zone_analyzer import ZoneAnalyzer
from workout_detector import WorkoutDetector

from .db import DEFAULT_DB_PATH


def hr_max_for_age(age_years: float) -> float:
    """Athlete's preferred max-HR estimate: 205.8 - 0.61 * age."""
    return 205.8 - 0.61 * age_years


def age_on(birthdate: str, on_date: str) -> float:
    """Age in years (fractional) on a given ISO date."""
    born = date.fromisoformat(birthdate[:10])
    when = date.fromisoformat(on_date[:10])
    return (when - born).days / 365.25


def stored_birthdate(db_path: str = DEFAULT_DB_PATH) -> Optional[str]:
    """Read the athlete birthdate from sync_state without creating the DB.

    Returns None if the database, table, or value is absent so callers can
    fall back to the zone-based HRmax estimate.
    """
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = 'birthdate'").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def hr_max_for_activity(activity: Dict,
                        db_path: str = DEFAULT_DB_PATH) -> Optional[float]:
    """Age-based HRmax for an activity dict's date, or None when no birthdate
    is stored (callers then fall back to the zone-based estimate)."""
    birthdate = stored_birthdate(db_path)
    date_str = activity.get('start_date_local') or activity.get('start_date')
    if birthdate and date_str:
        return hr_max_for_age(age_on(birthdate, date_str))
    return None


def scale_hr_zones(hr_block: Dict, ratio: float) -> Dict:
    """Scale HR zone boundaries by a max-HR ratio, preserving the zone
    scheme. Zone 1 stays anchored at 0, the top zone stays open-ended, and
    contiguity (each min == previous max) is rebuilt after rounding."""
    zones = []
    for z in hr_block.get('zones', []):
        zones.append({
            'min': 0 if z['min'] == 0 else round(z['min'] * ratio),
            'max': -1 if z['max'] == -1 else round(z['max'] * ratio),
        })
    for i in range(1, len(zones)):
        zones[i]['min'] = zones[i - 1]['max']
    return {**hr_block, 'zones': zones}


# Coggan zone upper bounds as a fraction of FTP, matching how Strava
# derives power zones from FTP (verified against live /athlete/zones data).
_POWER_ZONE_CEILINGS = (0.55, 0.75, 0.90, 1.05, 1.20, 1.50)


def power_zones_from_ftp(ftp: int) -> Dict:
    """Build a Strava-style power zones block from an FTP value.

    Produces the same structure as /athlete/zones: seven zones, each min
    one watt above the previous max, last zone open-ended (max = -1).
    estimate_ftp() round-trips this back to the input FTP.
    """
    maxes = [round(ftp * pct) for pct in _POWER_ZONE_CEILINGS]
    zones = []
    prev_max = None
    for zone_max in maxes:
        zones.append({'min': 0 if prev_max is None else prev_max + 1,
                      'max': zone_max})
        prev_max = zone_max
    zones.append({'min': prev_max + 1, 'max': -1})
    return {'zones': zones}


def estimate_ftp(zones: Dict) -> Optional[int]:
    """Estimate FTP from a /athlete/zones response via WorkoutDetector's
    zone-4 midpoint logic."""
    analyzer = ZoneAnalyzer(zones)
    if not analyzer.power_zones:
        return None
    return WorkoutDetector(analyzer.power_zones).ftp


def compute_power_metrics(analyzer: ZoneAnalyzer, power_stream: list,
                          moving_time_s: Optional[int]) -> Dict:
    """Normalized power, intensity factor, and TSS for a power stream.

    Shared by the database sync and main.py so the formula lives in one place.
    FTP is taken from the analyzer's power zones (WorkoutDetector's zone-4
    estimate). Returns a dict with normalized_power/intensity_factor/tss/
    ftp_used, each None when not computable.

        TSS = duration_s * NP * IF / (FTP * 3600) * 100,  IF = NP / FTP
    """
    result = {'normalized_power': None, 'intensity_factor': None,
              'tss': None, 'ftp_used': None}
    if not (analyzer.power_zones and power_stream):
        return result
    np = analyzer.calculate_normalized_power(power_stream)
    ftp = WorkoutDetector(analyzer.power_zones).ftp
    if np and ftp:
        intensity = np / ftp
        duration_s = moving_time_s or len(power_stream)
        result['normalized_power'] = np
        result['intensity_factor'] = round(intensity, 3)
        result['tss'] = round(duration_s * np * intensity / (ftp * 3600) * 100, 1)
        result['ftp_used'] = ftp
    return result


def compute_activity_metrics(activity: sqlite3.Row, streams: Dict,
                             laps: list, zones_row: sqlite3.Row,
                             hr_max: Optional[float] = None) -> Dict:
    """Compute derived metrics for one activity.

    Args:
        activity: row from the activities table
        streams: {stream_type: [values]} as stored in the streams table
        laps: list of lap dicts
        zones_row: athlete_zones row in effect on the activity date
        hr_max: explicit max HR for TRIMP (e.g. age-based); when None,
            falls back to ZoneAnalyzer's zone-5 estimate

    Returns:
        Dict matching Database.save_derived_metrics(); values are None where
        the underlying data (power meter, HR strap) is missing.
    """
    zones = json.loads(zones_row['zones_json'])
    analyzer = ZoneAnalyzer(zones)

    time_stream = streams.get('time', [])
    hr_stream = streams.get('heartrate', [])
    power_stream = streams.get('watts', [])
    # Estimated-power activities store watts as floats with None gaps; only
    # real power meter data is used for NP/TSS and workout detection.
    has_power_meter = bool(activity['device_watts'])
    if power_stream and any(w is None for w in power_stream):
        power_stream = [w if w is not None else 0 for w in power_stream]

    metrics = {
        'normalized_power': None,
        'intensity_factor': None,
        'tss': None,
        'trimp': None,
        'ftp_used': None,
        'zones_id': zones_row['id'],
        'power_zone_times': None,
        'hr_zone_times': None,
        'workout_description': None,
    }

    if hr_stream and time_stream:
        metrics['trimp'] = analyzer.calculate_trimp(hr_stream, time_stream,
                                                    hr_max=hr_max)
        metrics['hr_zone_times'] = analyzer.analyze_hr_zones(time_stream, hr_stream)

    if has_power_meter and power_stream and time_stream:
        metrics['power_zone_times'] = analyzer.analyze_power_zones(
            time_stream, power_stream)

        power_metrics = compute_power_metrics(
            analyzer, power_stream, activity['moving_time_s'])
        metrics.update(power_metrics)

        if laps and len(laps) >= 2:
            detector = WorkoutDetector(analyzer.power_zones)
            if detector.ftp:
                metrics['workout_description'] = detector.detect_workout_type(laps)

    return metrics
