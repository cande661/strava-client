"""Sync engine: replicate Strava activities into the local database.

Passes, each independently resumable:
  1. Activity list — pages /athlete/activities into summary rows. Incremental
     runs use after= with a 14-day overlap to pick up recent edits.
  2. Enrichment — per activity, fetch detail + streams + laps (3 requests),
     then immediately compute its metrics (TRIMP/NP/TSS/zone times) and commit
     both together. Throttled against Strava's read rate limit: sleeps through
     15-minute windows, stops cleanly when the daily quota is gone.

Because metrics are computed inline as each activity is enriched, interrupting
at any point (Ctrl-C, shutdown, rate-limit wait) leaves every finished activity
fully processed — never streams without metrics. A final compute_metrics() pass
acts as a safety net that backfills anything stranded by older runs.
"""

import json
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

# Wake this many seconds *after* a window boundary. Strava's 15-min windows are
# aligned to the UTC quarter-hour, but clock skew and reset-phase jitter mean a
# tiny cushion can land the probe back in the old window; 30s absorbs that.
WINDOW_CUSHION_SECONDS = 30

# If a probe still 429s right after a window boundary (woke a touch early),
# retry after this delay, doubling up to the next boundary, instead of sleeping
# a whole extra 15-minute window.
SHORT_BACKOFF_SECONDS = 30

# sync_state key under which the latest rate-limit observation is persisted, so
# throttling survives across separate sync runs.
RATE_LIMIT_STATE_KEY = 'rate_limit'


class DailyLimitReached(Exception):
    """Daily read quota exhausted; sync should stop and resume tomorrow."""


def _parse_start_ts(start_date: str) -> float:
    return datetime.fromisoformat(
        start_date.replace('Z', '+00:00')).timestamp()


def _seconds_to_next_window() -> float:
    """Seconds until the next 15-minute rate-limit window (quarter hour UTC),
    plus a cushion so the next request lands safely inside the new window."""
    now = time.time()
    return (int(now // 900) + 1) * 900 - now + WINDOW_CUSHION_SECONDS


def _age_rate_limit(rl: Optional[dict]) -> Optional[dict]:
    """Zero out usage counts whose window/day has elapsed since they were
    observed, so a stale observation from a prior run doesn't over- or
    under-throttle. Mutates and returns the dict."""
    if not rl or 'observed_at' not in rl:
        return rl
    now = time.time()
    obs = rl['observed_at']
    if int(now // 900) != int(obs // 900):
        rl['short_usage'] = 0          # the 15-min window has rolled over
    if (datetime.fromtimestamp(now, timezone.utc).date()
            != datetime.fromtimestamp(obs, timezone.utc).date()):
        rl['daily_usage'] = 0          # the UTC day has rolled over
    return rl


class SyncEngine:
    def __init__(self, db: Database, client: StravaClient):
        self.db = db
        self.client = client
        self.requests_made = 0
        self._load_rate_limit()

    # -- rate-limit-aware request wrapper ------------------------------------

    def _load_rate_limit(self):
        """Seed the client's rate-limit view from the last run so the first
        request is throttled instead of going out blind."""
        if self.client is None or self.client.rate_limit is not None:
            return
        saved = self.db.get_state(RATE_LIMIT_STATE_KEY)
        if not saved:
            return
        try:
            rl = json.loads(saved)
        except (ValueError, TypeError):
            return
        self.client.rate_limit = _age_rate_limit(rl)

    def _persist_rate_limit(self):
        """Save the latest rate-limit observation so the next run sees it."""
        if self.client is not None and self.client.rate_limit:
            self.db.set_state(RATE_LIMIT_STATE_KEY,
                              json.dumps(self.client.rate_limit))

    def _wait_for_budget(self):
        rl = _age_rate_limit(self.client.rate_limit)
        if not rl:
            return
        if rl['daily_usage'] >= rl['daily_limit'] - RATE_LIMIT_MARGIN:
            raise DailyLimitReached()
        if rl['short_usage'] >= rl['short_limit'] - RATE_LIMIT_MARGIN:
            wait = _seconds_to_next_window()
            print(f"  Rate limit window full ({rl['short_usage']}/{rl['short_limit']}), "
                  f"sleeping {wait / 60:.1f} min...")
            time.sleep(wait)
            # The window has rolled over: optimistically clear the short count
            # so the next request probes the new window instead of re-sleeping a
            # whole one. The probe's response headers correct this; if we woke a
            # touch early, the 429 path below backs off briefly.
            rl['short_usage'] = 0

    def _call(self, fn, *args, **kwargs):
        """Make an API call, sleeping through 15-minute limits and raising
        DailyLimitReached when the daily quota is gone."""
        backoff = SHORT_BACKOFF_SECONDS
        while True:
            self._wait_for_budget()
            try:
                result = fn(*args, **kwargs)
                self.requests_made += 1
                self._persist_rate_limit()
                return result
            except RateLimitError as e:
                self._persist_rate_limit()
                if e.daily:
                    raise DailyLimitReached() from e
                # Either we probed a few seconds early after a boundary, or the
                # window genuinely filled. Back off briefly and retry rather
                # than burning a full window; grow the delay up to the next
                # boundary so a truly-full window still resolves promptly.
                print(f"  15-min limit still in effect, retrying in {backoff:.0f}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, _seconds_to_next_window())
                # Clear the maxed short count so the retry probes the window
                # again instead of _wait_for_budget re-sleeping a full one; the
                # probe's response headers restore the true usage.
                if self.client.rate_limit:
                    self.client.rate_limit['short_usage'] = 0

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
            # Compute metrics immediately, before moving to the next activity
            # or waiting on the rate limiter. Streams and metrics are committed
            # together so an interrupted sync never strands data.
            self.compute_metrics_for(self.db.get_activity(row['id']))
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
                self.compute_metrics_for(self.db.get_activity(activity_id))
                done += 1
                label = f"{(row['start_date_local'] or '')[:10]} {row['name'] or activity_id}"
                print(f"  [{done}] re-enriched {label}")
        except DailyLimitReached:
            print("\nDaily Strava API quota reached. Progress is saved — run the "
                  "command again tomorrow (or after midnight UTC) to continue.")
        self._finish_metrics()
        return done

    # -- pass 4: derived metrics -----------------------------------------------

    def _birthdate(self):
        """Cached birthdate lookup (None if unset); read once per run."""
        if not hasattr(self, '_birthdate_cache'):
            self._birthdate_cache = self.db.get_state('birthdate')
        return self._birthdate_cache

    def compute_metrics_for(self, row) -> bool:
        """Derive and store metrics for one activity from its local streams.

        Returns False (without saving) when no athlete zones cover the
        activity's date — the caller decides whether to warn or stop.
        """
        activity_date = row['start_date_local'] or row['start_date'] or ''
        zones_row = self.db.zones_for_date(activity_date)
        if not zones_row:
            return False
        hr_max = None
        birthdate = self._birthdate()
        if birthdate and activity_date:
            hr_max = hr_max_for_age(age_on(birthdate, activity_date))
        streams = self.db.get_streams(row['id'])
        laps = self.db.get_laps(row['id'])
        metrics = compute_activity_metrics(row, streams, laps, zones_row,
                                           hr_max=hr_max)
        self.db.save_derived_metrics(row['id'], metrics)
        return True

    def compute_metrics(self, recompute: bool = False) -> int:
        rows = (self.db.activities_with_streams() if recompute
                else self.db.activities_needing_metrics())
        computed = 0
        for row in rows:
            if not self.compute_metrics_for(row):
                print("  No athlete zones recorded yet; run a sync first.")
                break
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
        # Metrics are normally computed inline during enrichment; this is a
        # safety net that backfills anything stranded by an older interrupted
        # run (streams but no metrics). Stays quiet when there's nothing to do.
        n = self.compute_metrics()
        if n:
            print(f"Backfilled metrics for {n} previously un-computed activities")
