"""Derived training metrics computed at sync time.

Reuses ZoneAnalyzer and WorkoutDetector from the original analyzer so the
numbers in the database match what main.py prints for a single activity.
"""

import json
import sqlite3
from typing import Dict, Optional

from zone_analyzer import ZoneAnalyzer
from workout_detector import WorkoutDetector


def estimate_ftp(zones: Dict) -> Optional[int]:
    """Estimate FTP from a /athlete/zones response via WorkoutDetector's
    zone-4 midpoint logic."""
    analyzer = ZoneAnalyzer(zones)
    if not analyzer.power_zones:
        return None
    return WorkoutDetector(analyzer.power_zones).ftp


def compute_activity_metrics(activity: sqlite3.Row, streams: Dict,
                             laps: list, zones_row: sqlite3.Row) -> Dict:
    """Compute derived metrics for one activity.

    Args:
        activity: row from the activities table
        streams: {stream_type: [values]} as stored in the streams table
        laps: list of lap dicts
        zones_row: athlete_zones row in effect on the activity date

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
        metrics['trimp'] = analyzer.calculate_trimp(hr_stream, time_stream)
        metrics['hr_zone_times'] = analyzer.analyze_hr_zones(time_stream, hr_stream)

    if has_power_meter and power_stream and time_stream:
        metrics['power_zone_times'] = analyzer.analyze_power_zones(
            time_stream, power_stream)

        np = analyzer.calculate_normalized_power(power_stream)
        ftp = WorkoutDetector(analyzer.power_zones).ftp if analyzer.power_zones else None
        if np and ftp:
            intensity = np / ftp
            duration_s = activity['moving_time_s'] or len(power_stream)
            metrics['normalized_power'] = np
            metrics['intensity_factor'] = round(intensity, 3)
            metrics['tss'] = round(
                duration_s * np * intensity / (ftp * 3600) * 100, 1)
            metrics['ftp_used'] = ftp

        if laps and len(laps) >= 2:
            detector = WorkoutDetector(analyzer.power_zones)
            if detector.ftp:
                metrics['workout_description'] = detector.detect_workout_type(laps)

    return metrics
