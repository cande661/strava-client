"""Sync engine: replicate Strava activities into the local database.

Three passes, each independently resumable:
  1. Activity list — pages /athlete/activities into summary rows. Incremental
     runs use after= with a 14-day overlap to pick up recent edits.
  2. Enrichment — per activity, fetch detail + streams + laps (3 requests).
     Throttled against Strava's read rate limit: sleeps through 15-minute
     windows, stops cleanly when the daily quota is gone.
  3. Metrics — compute TRIMP/NP/TSS/zone times for anything with streams.

Interrupting at any point is safe; the next run picks up where it left off.
"""

import time
from datetime import datetime, timezone
from typing import List, Optional

import requests

from strava_client import StravaClient, RateLimitError
from .db import Database
from .metrics import (compute_activity_metrics, estimate_ftp,
                      hr_max_for_age, age_on)

STREAM_KEYS = ['time', 'distance', 'latlng', 'altitude', 'velocity_smooth',
               'heartrate', 'cadence', 'watts', 'temp', 'moving', 'grade_smooth']

# Re-list this far back of the newest known activity to catch edits.
LIST_OVERLAP_SECONDS = 14 * 86400

# Stop issuing requests when within this margin of a quota.
RATE_LIMIT_MARGIN = 3


class DailyLimitReached(Exception):
    """Daily read quota exhausted; sync should stop and resume tomorrow."""


def _parse_start_ts(start_date: str) -> float:
    return datetime.fromisoformat(
        start_date.replace('Z', '+00:00')).timestamp()


def _seconds_to_next_window() -> float:
    """Seconds until the next 15-minute rate-limit window (quarter hour UTC)."""
    now = time.time()
    return (int(now // 900) + 1) * 900 - now + 5


class SyncEngine:
    def __init__(self, db: Database, client: StravaClient):
        self.db = db
        self.client = client
        self.requests_made = 0

    # -- rate-limit-aware request wrapper ------------------------------------

    def _wait_for_budget(self):
        rl = self.client.rate_limit
        if not rl:
            return
        if rl['daily_usage'] >= rl['daily_limit'] - RATE_LIMIT_MARGIN:
            raise DailyLimitReached()
        if rl['short_usage'] >= rl['short_limit'] - RATE_LIMIT_MARGIN:
            wait = _seconds_to_next_window()
            print(f"  Rate limit window full ({rl['short_usage']}/{rl['short_limit']}), "
                  f"sleeping {wait / 60:.1f} min...")
            time.sleep(wait)

    def _call(self, fn, *args, **kwargs):
        """Make an API call, sleeping through 15-minute limits and raising
        DailyLimitReached when the daily quota is gone."""
        while True:
            self._wait_for_budget()
            try:
                result = fn(*args, **kwargs)
                self.requests_made += 1
                return result
            except RateLimitError as e:
                if e.daily:
                    raise DailyLimitReached() from e
                wait = _seconds_to_next_window()
                print(f"  Hit 15-min rate limit, sleeping {wait / 60:.1f} min...")
                time.sleep(wait)

    # -- pass 1: athlete context ---------------------------------------------

    def sync_athlete_context(self):
        """Refresh gear names and record zone changes (2 requests)."""
        athlete = self._call(self.client.get_athlete)
        for gear in (athlete.get('bikes') or []) + (athlete.get('shoes') or []):
            self.db.upsert_gear(gear)

        zones = self._call(self.client.get_athlete_zones)
        if self.db.record_zones(zones, estimate_ftp(zones)):
            print("  Recorded new athlete zones version "
                  f"(FTP estimate: {estimate_ftp(zones)} W)")

    # -- pass 2: activity list -----------------------------------------------

    def sync_activity_list(self, full: bool = False) -> int:
        """Page through /athlete/activities and upsert summary rows.

        Returns the number of summaries upserted.
        """
        after = None
        if not full:
            newest = self.db.get_state('newest_start_ts')
            if newest:
                after = int(float(newest)) - LIST_OVERLAP_SECONDS

        count = 0
        page = 1
        newest_ts = float(self.db.get_state('newest_start_ts') or 0)
        while True:
            batch = self._call(self.client.list_activities,
                               page=page, per_page=200, after=after)
            if not batch:
                break
            for summary in batch:
                self.db.upsert_activity_summary(summary)
                if summary.get('start_date'):
                    newest_ts = max(newest_ts, _parse_start_ts(summary['start_date']))
            count += len(batch)
            print(f"  Page {page}: {len(batch)} activities ({count} total)")
            page += 1

        if newest_ts:
            self.db.set_state('newest_start_ts', newest_ts)
        self.db.set_state('list_synced_at',
                          datetime.now(timezone.utc).isoformat(timespec='seconds'))
        return count

    # -- pass 3: enrichment ----------------------------------------------------

    def enrich(self, limit: Optional[int] = None) -> int:
        """Fetch detail/streams/laps for activities missing them, newest first.

        Returns the number of activities fully enriched this run.
        """
        pending = self.db.activities_needing_enrichment()
        if not pending:
            return 0
        total = len(pending)
        if limit:
            pending = pending[:limit]
        print(f"  {total} activities need enrichment"
              + (f", processing {len(pending)} this run" if limit and total > len(pending) else ""))

        done = 0
        for row in pending:
            self._enrich_row(row)
            done += 1
            label = f"{(row['start_date_local'] or '')[:10]} {row['name'] or row['id']}"
            print(f"  [{done}/{len(pending)}] {label}")

        return done

    def _enrich_row(self, row):
        """Fetch whichever of detail/streams/laps the row is still missing."""
        activity_id = row['id']

        if row['detail_fetched_at'] is None:
            detail = self._call(self.client.get_activity, activity_id)
            self.db.save_activity_detail(activity_id, detail)

        if row['streams_fetched_at'] is None:
            if row['manual']:
                # Manual entries have no streams; mark as fetched-empty.
                self.db.save_streams(activity_id, {})
            else:
                try:
                    streams = self._call(self.client.get_activity_streams,
                                         activity_id, STREAM_KEYS)
                    self.db.save_streams(activity_id, streams)
                except requests.exceptions.HTTPError as e:
                    if e.response is not None and e.response.status_code == 404:
                        self.db.save_streams(activity_id, {})
                    else:
                        raise

        if row['laps_fetched_at'] is None:
            if row['manual']:
                self.db.save_laps(activity_id, [])
            else:
                laps = self._call(self.client.get_activity_laps, activity_id)
                self.db.save_laps(activity_id, laps)

    def reenrich(self, activity_ids: List[int]) -> int:
        """Force re-fetch of specific activities (e.g. after editing them on
        Strava), regardless of newest-first ordering. Clears each one's stored
        data, re-fetches it, then recomputes metrics. Returns the count done."""
        done = 0
        try:
            for activity_id in activity_ids:
                if not self.db.mark_for_reenrichment(activity_id):
                    print(f"  {activity_id}: not in the database, skipping")
                    continue
                row = self.db.get_activity(activity_id)
                self._enrich_row(row)
                done += 1
                label = f"{(row['start_date_local'] or '')[:10]} {row['name'] or activity_id}"
                print(f"  [{done}] re-enriched {label}")
        except DailyLimitReached:
            print("\nDaily Strava API quota reached. Progress is saved — run the "
                  "command again tomorrow (or after midnight UTC) to continue.")
        self._finish_metrics()
        return done

    # -- pass 4: derived metrics -----------------------------------------------

    def compute_metrics(self, recompute: bool = False) -> int:
        rows = (self.db.activities_with_streams() if recompute
                else self.db.activities_needing_metrics())
        birthdate = self.db.get_state('birthdate')
        computed = 0
        for row in rows:
            activity_date = row['start_date_local'] or row['start_date'] or ''
            zones_row = self.db.zones_for_date(activity_date)
            if not zones_row:
                print("  No athlete zones recorded yet; run a sync first.")
                break
            hr_max = None
            if birthdate and activity_date:
                hr_max = hr_max_for_age(age_on(birthdate, activity_date))
            streams = self.db.get_streams(row['id'])
            laps = self.db.get_laps(row['id'])
            metrics = compute_activity_metrics(row, streams, laps, zones_row,
                                               hr_max=hr_max)
            self.db.save_derived_metrics(row['id'], metrics)
            computed += 1
        return computed

    # -- orchestration -----------------------------------------------------------

    def run(self, full: bool = False, limit: Optional[int] = None,
            skip_enrich: bool = False) -> bool:
        """Run a full sync cycle. Returns True if it completed without
        exhausting the daily quota."""
        try:
            print("Syncing athlete profile, gear, and zones...")
            self.sync_athlete_context()

            print(f"Syncing activity list ({'full backfill' if full else 'incremental'})...")
            n = self.sync_activity_list(full=full)
            print(f"  {n} activity summaries synced")

            if not skip_enrich:
                print("Enriching activities (detail + streams + laps)...")
                n = self.enrich(limit=limit)
                print(f"  {n} activities enriched")
        except DailyLimitReached:
            print("\nDaily Strava API quota reached. Progress is saved — "
                  "run sync again tomorrow (or after midnight UTC) to continue.")
            self._finish_metrics()
            return False

        self._finish_metrics()
        print(f"Sync complete ({self.requests_made} API requests).")
        return True

    def _finish_metrics(self):
        print("Computing derived metrics...")
        n = self.compute_metrics()
        print(f"  {n} activities computed")
