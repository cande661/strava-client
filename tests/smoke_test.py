"""Smoke test for db + metrics + trends using synthetic data (no API calls).

Run from the project root: python tests/smoke_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stravaclient.db import Database
from stravaclient.metrics import (compute_activity_metrics, estimate_ftp,
                                  power_zones_from_ftp, hr_max_for_age,
                                  age_on, scale_hr_zones)
from stravaclient.trends import compute_trends

ZONES = {
    'heart_rate': {'zones': [
        {'min': 0, 'max': 120}, {'min': 120, 'max': 145}, {'min': 145, 'max': 160},
        {'min': 160, 'max': 172}, {'min': 172, 'max': -1}]},
    'power': {'zones': [
        {'min': 0, 'max': 137}, {'min': 137, 'max': 187}, {'min': 187, 'max': 225},
        {'min': 225, 'max': 262}, {'min': 262, 'max': 300},
        {'min': 300, 'max': 375}, {'min': 375, 'max': -1}]},
}


def make_summary(i, start_local, distance_m, moving_s, commute=False):
    return {
        'id': 1000 + i,
        'athlete': {'id': 42},
        'name': f'Test ride {i}',
        'sport_type': 'Ride',
        'type': 'Ride',
        'start_date': start_local + 'Z',
        'start_date_local': start_local + 'Z',
        'distance': distance_m,
        'moving_time': moving_s,
        'elapsed_time': moving_s + 60,
        'total_elevation_gain': 100.0,
        'average_watts': 180.0,
        'device_watts': True,
        'average_heartrate': 140.0,
        'commute': commute,
        'manual': False,
        'gear_id': 'b123',
    }


def main():
    db = Database(':memory:')

    # Zones recording: first insert, dedup, then a changed version
    assert db.record_zones(ZONES, estimate_ftp(ZONES)) is True
    assert db.record_zones(ZONES, estimate_ftp(ZONES)) is False
    ftp = estimate_ftp(ZONES)
    assert 240 <= ftp <= 260, f"FTP estimate {ftp} outside expected range"

    # Three rides across two ISO weeks (Mon Jun 1 and Mon Jun 8, 2026)
    rides = [
        (1, '2026-06-01T08:00:00', 16093.4, 3600, True),   # 10 mi commute
        (2, '2026-06-03T08:00:00', 32186.8, 7200, False),  # 20 mi
        (3, '2026-06-08T08:00:00', 16093.4, 3600, False),  # 10 mi, next week
    ]
    for i, start, dist, mov, commute in rides:
        db.upsert_activity_summary(make_summary(i, start, dist, mov, commute))

    # Upsert is idempotent and preserves enrichment timestamps
    db.save_activity_detail(1001, {'id': 1001, 'device_name': 'Wahoo ELEMNT BOLT'})
    db.upsert_activity_summary(make_summary(1, '2026-06-01T08:00:00', 16093.4, 3600, True))
    row = db.get_activity(1001)
    assert row['detail_fetched_at'] is not None, "upsert clobbered detail_fetched_at"
    assert row['device_name'] == 'Wahoo ELEMNT BOLT'

    # Streams: 1 hour at steady 200 W, HR 150, with an auto-pause gap at t=1800
    n = 3600
    time_stream = [t if t < 1800 else t + 300 for t in range(n)]
    streams = {
        'time': {'data': time_stream},
        'watts': {'data': [200] * n},
        'heartrate': {'data': [150] * n},
    }
    db.save_streams(1001, streams)
    loaded = db.get_streams(1001)
    assert loaded['watts'] == [200] * n, "stream round-trip failed"

    laps = [
        {'average_watts': 130, 'elapsed_time': 600, 'distance': 3000},
        {'average_watts': 240, 'elapsed_time': 900, 'distance': 5000},
        {'average_watts': 120, 'elapsed_time': 300, 'distance': 1500},
        {'average_watts': 240, 'elapsed_time': 900, 'distance': 5000},
        {'average_watts': 110, 'elapsed_time': 600, 'distance': 2500},
    ]
    db.save_laps(1001, laps)

    zones_row = db.zones_for_date('2026-06-01')
    assert zones_row is not None
    metrics = compute_activity_metrics(db.get_activity(1001),
                                       db.get_streams(1001),
                                       db.get_laps(1001), zones_row)

    # Steady 200 W -> NP == 200; IF = 200/ftp; TSS = 3600*200*IF/(ftp*3600)*100
    assert metrics['normalized_power'] == 200, metrics
    expected_if = 200 / ftp
    assert abs(metrics['intensity_factor'] - expected_if) < 0.01, metrics
    expected_tss = 3600 * 200 * expected_if / (ftp * 3600) * 100
    assert abs(metrics['tss'] - expected_tss) < 2, metrics
    assert metrics['trimp'] and metrics['trimp'] > 0, metrics
    assert metrics['power_zone_times'], metrics
    assert metrics['workout_description'], metrics
    db.save_derived_metrics(1001, metrics)

    # Trends: weekly miles = 30 (week of Jun 1) and 10 (week of Jun 8)
    weekly = compute_trends(db, metric='miles', by='week')
    assert len(weekly) == 2, weekly
    assert abs(weekly[0]['value'] - 30.0) < 0.01, weekly
    assert weekly[0]['count'] == 2, weekly
    assert abs(weekly[1]['value'] - 10.0) < 0.01, weekly

    # Commute filtering
    no_commutes = compute_trends(db, metric='miles', by='week', commutes=False)
    assert abs(no_commutes[0]['value'] - 20.0) < 0.01, no_commutes

    # Monthly TSS only counts the activity with computed metrics
    monthly_tss = compute_trends(db, metric='tss', by='month')
    assert len(monthly_tss) == 1, monthly_tss
    assert abs(monthly_tss[0]['value'] - metrics['tss']) < 0.01, monthly_tss

    # Zones from FTP round-trip through the estimator, matching Strava's
    # observed boundary convention (min = prev max + 1, open-ended top)
    for ftp_in in (200, 250, 276, 320):
        built = {'heart_rate': ZONES['heart_rate'],
                 'power': power_zones_from_ftp(ftp_in)}
        ftp_out = estimate_ftp(built)
        assert abs(ftp_out - ftp_in) <= 1, (ftp_in, ftp_out)
    built_276 = power_zones_from_ftp(276)['zones']
    assert [z['max'] for z in built_276] == [152, 207, 248, 290, 331, 414, -1]
    assert [z['min'] for z in built_276][:3] == [0, 153, 208]

    # Age-based max HR: formula, age math, and HR zone scaling
    assert abs(hr_max_for_age(40) - 181.4) < 0.01
    assert abs(age_on('1986-06-15', '2026-06-15') - 40.0) < 0.01
    scaled = scale_hr_zones(ZONES['heart_rate'], 0.95)
    assert scaled['zones'][0]['min'] == 0          # zone 1 anchored at 0
    assert scaled['zones'][-1]['max'] == -1        # top zone open-ended
    assert scaled['zones'][1]['max'] == round(145 * 0.95)
    for i in range(1, 5):                          # contiguity preserved
        assert scaled['zones'][i]['min'] == scaled['zones'][i - 1]['max']

    # Explicit (lower) hr_max increases TRIMP vs the zone-5 estimate
    activity = db.get_activity(1001)
    m_default = compute_activity_metrics(activity, db.get_streams(1001),
                                         db.get_laps(1001), zones_row)
    m_formula = compute_activity_metrics(activity, db.get_streams(1001),
                                         db.get_laps(1001), zones_row,
                                         hr_max=181.0)
    assert m_formula['trimp'] > m_default['trimp'], (m_formula, m_default)
    # Power-side metrics are unaffected by hr_max
    assert m_formula['tss'] == m_default['tss']

    # Seeded historical zones are selected by activity date
    old_zones = {'heart_rate': ZONES['heart_rate'],
                 'power': power_zones_from_ftp(220)}
    db.seed_zones('2020-01-01', old_zones, estimate_ftp(old_zones))
    assert db.zones_for_date('2021-06-15')['ftp_estimate'] == estimate_ftp(old_zones)
    # Activities after the current (observed) version still get the new zones
    assert db.zones_for_date('2027-01-01')['ftp_estimate'] == ftp
    # Activities before all known versions fall back to the earliest
    assert db.zones_for_date('2016-01-01')['ftp_estimate'] == estimate_ftp(old_zones)

    # Enrichment bookkeeping: 1001 is done, 1002/1003 still pending
    pending = db.activities_needing_enrichment()
    pending_ids = {r['id'] for r in pending}
    assert pending_ids == {1002, 1003}, pending_ids

    print("All smoke tests passed.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
