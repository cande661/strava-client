"""CLI for the local Strava replica.

Run from the project root (config.json and data/strava.db are resolved
relative to the working directory):

    python -m stravaclient sync [--full] [--limit N] [--no-enrich]
    python -m stravaclient reenrich ID [ID ...]
    python -m stravaclient status
    python -m stravaclient trends --metric miles --by week --since 2026-01-01
    python -m stravaclient recompute
"""

import argparse
import sys

from .db import Database, DEFAULT_DB_PATH
from .trends import compute_trends, format_trends, METRICS


def cmd_sync(args):
    from strava_client import StravaClient
    from .sync import SyncEngine

    db = Database(args.db)
    client = StravaClient(args.config)
    engine = SyncEngine(db, client)
    completed = engine.run(full=args.full, limit=args.limit,
                           skip_enrich=args.no_enrich)
    return 0 if completed else 2


def cmd_reenrich(args):
    from strava_client import StravaClient
    from .sync import SyncEngine

    db = Database(args.db)
    client = StravaClient(args.config)
    engine = SyncEngine(db, client)
    n = engine.reenrich(args.activity_ids)
    print(f"Re-enriched {n} activities ({engine.requests_made} API requests).")
    return 0


def cmd_status(args):
    db = Database(args.db)
    s = db.status()
    print(f"Database: {args.db}")
    print(f"Activities:       {s['activities']}")
    print(f"  with detail:    {s['with_detail']}")
    print(f"  with streams:   {s['with_streams']}")
    print(f"  with laps:      {s['with_laps']}")
    print(f"  with metrics:   {s['with_metrics']}")
    print(f"Gear:             {s['gear']}")
    print(f"Zones versions:   {s['zones_versions']}")
    if s['oldest']:
        print(f"Date range:       {s['oldest'][:10]} to {s['newest'][:10]}")
    print(f"Last list sync:   {s['last_list_sync'] or 'never'}")
    pending = s['activities'] - s['with_streams']
    if pending > 0:
        print(f"\n{pending} activities still need enrichment "
              f"(~{pending * 3} API requests). Run: python -m stravaclient sync")
    return 0


def cmd_trends(args):
    db = Database(args.db)
    commutes = None
    if args.commutes_only:
        commutes = True
    elif args.no_commutes:
        commutes = False
    results = compute_trends(db, metric=args.metric, by=args.by,
                             since=args.since, sport=args.sport,
                             commutes=commutes)
    if args.last and len(results) > args.last:
        results = results[-args.last:]
    title = f"{args.metric} per {args.by}"
    if args.sport:
        title += f" ({args.sport})"
    print(title)
    print("-" * len(title))
    print(format_trends(results, args.metric))
    return 0


def _fmt_hms(seconds):
    if not seconds:
        return '-'
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def cmd_list(args):
    from datetime import date, timedelta

    db = Database(args.db)
    since = args.since
    if not since and args.days:
        since = (date.today() - timedelta(days=args.days)).isoformat()
    commutes = True if args.commutes_only else (False if args.no_commutes else None)

    rows = db.list_activities_rows(since=since, sport=args.sport,
                                   commutes=commutes, limit=args.limit)
    if not rows:
        print("No activities found for the given filters.")
        return 0

    print(f"{'Date':<11} {'Name':<34} {'Sport':<12} {'Miles':>6} {'Time':>8} "
          f"{'Power':>7} {'HR':>4} {'TSS':>6} {'TRIMP':>6} Flags")
    print('-' * 109)
    total_m = total_s = total_tss = total_trimp = 0.0
    for r in rows:
        name = (r['name'] or '')[:34]
        miles = (r['distance_m'] or 0) / 1609.34
        # NP when computed; otherwise average watts, ~ marks estimated power
        if r['normalized_power']:
            power = f"{r['normalized_power']}np"
        elif r['average_watts']:
            power = f"{r['average_watts']:.0f}{'' if r['device_watts'] else '~'}"
        else:
            power = '-'
        hr = f"{r['average_heartrate']:.0f}" if r['average_heartrate'] else '-'
        tss = f"{r['tss']:.0f}" if r['tss'] else '-'
        trimp = f"{r['trimp']:.0f}" if r['trimp'] else '-'
        flags = ''.join([
            'C' if r['commute'] else '',
            'T' if r['trainer'] else '',
            'W' if r['workout_type'] in (3, 12) else '',
            'R' if r['workout_type'] in (1, 11) else '',
        ])
        sport_name = (r['sport_type'] or r['type'] or '')[:12]
        print(f"{(r['start_date_local'] or '')[:10]:<11} {name:<34} "
              f"{sport_name:<12} {miles:>6.1f} "
              f"{_fmt_hms(r['moving_time_s']):>8} {power:>7} {hr:>4} "
              f"{tss:>6} {trimp:>6} {flags}")
        total_m += miles
        total_s += r['moving_time_s'] or 0
        total_tss += r['tss'] or 0
        total_trimp += r['trimp'] or 0

    print('-' * 109)
    print(f"{len(rows)} activities, {total_m:.1f} mi, {_fmt_hms(total_s)} moving"
          + (f", {total_tss:.0f} TSS" if total_tss else "")
          + (f", {total_trimp:.0f} TRIMP" if total_trimp else ""))
    return 0


def cmd_zones(args):
    import json
    from datetime import date
    from .metrics import (power_zones_from_ftp, estimate_ftp,
                          hr_max_for_age, age_on, scale_hr_zones)

    db = Database(args.db)

    if args.set_birthdate:
        db.set_state('birthdate', args.set_birthdate)
        age = age_on(args.set_birthdate, date.today().isoformat())
        print(f"Birthdate set. Current age {age:.1f}, estimated max HR "
              f"{hr_max_for_age(age):.0f} bpm (205.8 - 0.61 * age).")
        print("TRIMP now uses this estimate; seeded zones scale HR boundaries "
              "by age. Run 'recompute' to update existing metrics.")
        return 0

    if args.delete:
        if db.delete_zones(args.delete):
            print(f"Deleted zones version {args.delete}")
        else:
            print(f"No zones version with id {args.delete}")
            return 1
        return 0

    if args.set_ftp:
        if not args.effective_from:
            print("--from YYYY-MM-DD is required with --set-ftp")
            return 1
        # Reuse HR zones from whatever version is in effect on that date
        # (HR zones change rarely; FTP is what moves).
        template = db.zones_for_date(args.effective_from)
        if not template:
            print("No zones in the database yet — run a sync first so there "
                  "is a current version to copy heart rate zones from.")
            return 1
        zones = json.loads(template['zones_json'])
        zones['power'] = power_zones_from_ftp(args.set_ftp)

        birthdate = db.get_state('birthdate')
        if birthdate and zones.get('heart_rate'):
            ratio = (hr_max_for_age(age_on(birthdate, args.effective_from))
                     / hr_max_for_age(age_on(birthdate, template['effective_from'])))
            zones['heart_rate'] = scale_hr_zones(zones['heart_rate'], ratio)
            hr_note = (f"HR zones scaled from version {template['id']} "
                       f"by age (x{ratio:.4f})")
        else:
            hr_note = f"HR zones copied from version {template['id']}"

        db.seed_zones(args.effective_from, zones, estimate_ftp(zones))
        print(f"Seeded zones effective {args.effective_from} with FTP "
              f"{args.set_ftp} W ({hr_note})")
        print("Run 'python -m stravaclient recompute' to re-derive TSS/zone "
              "times with the new history.")
        return 0

    rows = db.list_zones()
    if not rows:
        print("No zones recorded yet — run a sync first.")
        return 0
    print(f"{'id':>4}  {'effective from':<15} {'FTP':>5}  source")
    for row in rows:
        source = 'observed' if row['observed_at'][:10] == row['effective_from'] \
            else 'seeded'
        print(f"{row['id']:>4}  {row['effective_from']:<15} "
              f"{row['ftp_estimate'] or '?':>5}  {source}")
    return 0


def cmd_recompute(args):
    from .sync import SyncEngine

    db = Database(args.db)
    engine = SyncEngine(db, client=None)
    n = engine.compute_metrics(recompute=True)
    print(f"Recomputed metrics for {n} activities")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m stravaclient',
        description='Local Strava replica: sync, status, and trends')
    parser.add_argument('--db', default=DEFAULT_DB_PATH,
                        help=f'database path (default: {DEFAULT_DB_PATH})')
    parser.add_argument('--config', default='config.json',
                        help='Strava credentials file (default: config.json)')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('sync', help='sync activities from Strava')
    p.add_argument('--full', action='store_true',
                   help='re-list all activities, not just recent ones')
    p.add_argument('--limit', type=int, default=None,
                   help='max activities to enrich this run')
    p.add_argument('--no-enrich', action='store_true',
                   help='only sync the activity list, skip detail/streams/laps')
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser('reenrich',
                       help='force re-fetch of specific activities by id '
                            '(use after editing them on Strava)')
    p.add_argument('activity_ids', nargs='+', type=int, metavar='ID',
                   help='Strava activity id(s) to clear and re-fetch')
    p.set_defaults(func=cmd_reenrich)

    p = sub.add_parser('status', help='show database status')
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('list', help='list activities as a table')
    p.add_argument('--days', type=int, default=30, metavar='N',
                   help='show the last N days (default: 30)')
    p.add_argument('--since', default=None, metavar='YYYY-MM-DD',
                   help='explicit start date (overrides --days)')
    p.add_argument('--sport', default=None,
                   help="filter by sport type (e.g. 'Ride')")
    p.add_argument('--limit', type=int, default=None, metavar='N',
                   help='max rows to show')
    p.add_argument('--commutes-only', action='store_true',
                   help='only commute-tagged activities')
    p.add_argument('--no-commutes', action='store_true',
                   help='exclude commute-tagged activities')
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('trends', help='aggregate metrics over time')
    p.add_argument('--metric', default='miles', choices=sorted(METRICS),
                   help='metric to aggregate (default: miles)')
    p.add_argument('--by', default='week', choices=['week', 'month', 'year'],
                   help='bucket size (default: week)')
    p.add_argument('--since', default=None, metavar='YYYY-MM-DD',
                   help='only include activities on/after this date')
    p.add_argument('--sport', default=None,
                   help="filter by sport type (e.g. 'Ride', 'VirtualRide')")
    p.add_argument('--last', type=int, default=None, metavar='N',
                   help='show only the last N buckets')
    p.add_argument('--commutes-only', action='store_true',
                   help='only commute-tagged activities')
    p.add_argument('--no-commutes', action='store_true',
                   help='exclude commute-tagged activities')
    p.set_defaults(func=cmd_trends)

    p = sub.add_parser('zones', help='list or seed athlete zone history')
    p.add_argument('--set-ftp', type=int, default=None, metavar='WATTS',
                   help='seed a historical FTP (builds Strava-style power zones)')
    p.add_argument('--from', dest='effective_from', default=None,
                   metavar='YYYY-MM-DD',
                   help='date the FTP took effect (required with --set-ftp)')
    p.add_argument('--delete', type=int, default=None, metavar='ID',
                   help='delete a zones version by id')
    p.add_argument('--set-birthdate', default=None, metavar='YYYY-MM-DD',
                   help='store birthdate for age-based max HR '
                        '(205.8 - 0.61 * age); used by TRIMP and zone seeding')
    p.set_defaults(func=cmd_zones)

    p = sub.add_parser('recompute', help='recompute derived metrics for all activities')
    p.set_defaults(func=cmd_recompute)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
