"""Trend aggregation over the local activity database."""

from collections import OrderedDict
from datetime import date, timedelta
from typing import Dict, List, Optional

from .db import Database

METERS_PER_MILE = 1609.34
FEET_PER_METER = 3.28084

# metric name -> (row extractor, display unit)
METRICS = {
    'miles': (lambda r: (r['distance_m'] or 0) / METERS_PER_MILE, 'mi'),
    'hours': (lambda r: (r['moving_time_s'] or 0) / 3600.0, 'h'),
    'elevation': (lambda r: (r['total_elevation_gain_m'] or 0) * FEET_PER_METER, 'ft'),
    'tss': (lambda r: r['tss'] or 0, 'TSS'),
    'trimp': (lambda r: r['trimp'] or 0, 'TRIMP'),
    'kj': (lambda r: r['kilojoules'] or 0, 'kJ'),
    'rides': (lambda r: 1, 'activities'),
}


def _bucket_key(start_date_local: str, by: str) -> str:
    d = date.fromisoformat(start_date_local[:10])
    if by == 'month':
        return f"{d.year}-{d.month:02d}"
    if by == 'year':
        return str(d.year)
    # week: label by ISO week, e.g. "2026-W23 (Jun 01)"
    iso = d.isocalendar()
    monday = d - timedelta(days=d.weekday())
    return f"{iso[0]}-W{iso[1]:02d} ({monday.strftime('%b %d')})"


def compute_trends(db: Database, metric: str = 'miles', by: str = 'week',
                   since: Optional[str] = None, sport: Optional[str] = None,
                   commutes: Optional[bool] = None) -> List[Dict]:
    """Aggregate a metric per week/month/year.

    Args:
        metric: one of METRICS keys
        by: 'week', 'month', or 'year'
        since: ISO date lower bound on local start date
        sport: filter by sport_type/type (e.g. 'Ride')
        commutes: True = only commutes, False = exclude commutes, None = all

    Returns:
        [{'bucket', 'value', 'count'}] in chronological order. Buckets with
        no activities are omitted.
    """
    if metric not in METRICS:
        raise ValueError(f"Unknown metric '{metric}'. Choose from: {', '.join(METRICS)}")
    extractor, _ = METRICS[metric]

    buckets: 'OrderedDict[str, Dict]' = OrderedDict()
    for row in db.trend_rows(since=since, sport=sport):
        if commutes is True and not row['commute']:
            continue
        if commutes is False and row['commute']:
            continue
        if not row['start_date_local']:
            continue
        key = _bucket_key(row['start_date_local'], by)
        bucket = buckets.setdefault(key, {'bucket': key, 'value': 0.0, 'count': 0})
        bucket['value'] += extractor(row)
        bucket['count'] += 1

    return list(buckets.values())


def format_trends(results: List[Dict], metric: str, bar_width: int = 30) -> str:
    """Render trend buckets as an aligned table with bars scaled to the max."""
    if not results:
        return "No activities found for the given filters."

    _, unit = METRICS[metric]
    max_value = max(r['value'] for r in results) or 1
    label_width = max(len(r['bucket']) for r in results)

    lines = []
    for r in results:
        bar_len = round(r['value'] / max_value * bar_width)
        bar = '█' * bar_len if bar_len else ('▏' if r['value'] > 0 else '')
        value_str = f"{r['value']:8.1f} {unit}"
        lines.append(f"{r['bucket']:<{label_width}}  {value_str:>14} "
                     f"({r['count']:3d} act) {bar}")
    return '\n'.join(lines)
