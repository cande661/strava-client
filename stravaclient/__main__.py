"""CLI for the local Strava replica.

Run from the project root (config.json and data/strava.db are resolved
relative to the working directory):

    python -m stravaclient sync [--full] [--limit N] [--no-enrich]
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


def cmd_zones(args):
    import json
    from .metrics import power_zones_from_ftp, estimate_ftp

    db = Database(args.db)

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
        db.seed_zones(args.effective_from, zones, estimate_ftp(zones))
        print(f"Seeded zones effective {args.effective_from} with FTP "
              f"{args.set_ftp} W (HR zones copied from version {template['id']})")
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

    p = sub.add_parser('status', help='show database status')
    p.set_defaults(func=cmd_status)

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
    p.set_defaults(func=cmd_zones)

    p = sub.add_parser('recompute', help='recompute derived metrics for all activities')
    p.set_defaults(func=cmd_recompute)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
