"""SQLite storage layer for replicated Strava data.

One row per activity with commonly-queried fields extracted into real columns
and the full API responses kept as JSON. Per-second streams are stored as
zlib-compressed JSON blobs, one row per (activity, stream type).
"""

import json
import os
import sqlite3
import zlib
from datetime import datetime, timezone
from typing import Dict, List, Optional

DEFAULT_DB_PATH = "data/strava.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    athlete_id INTEGER,
    name TEXT,
    sport_type TEXT,
    type TEXT,
    start_date TEXT,
    start_date_local TEXT,
    timezone TEXT,
    distance_m REAL,
    moving_time_s INTEGER,
    elapsed_time_s INTEGER,
    total_elevation_gain_m REAL,
    average_speed_mps REAL,
    average_watts REAL,
    weighted_average_watts REAL,
    max_watts REAL,
    device_watts INTEGER,
    kilojoules REAL,
    average_heartrate REAL,
    max_heartrate REAL,
    average_cadence REAL,
    commute INTEGER,
    trainer INTEGER,
    manual INTEGER,
    workout_type INTEGER,
    gear_id TEXT,
    device_name TEXT,
    summary_json TEXT NOT NULL,
    detail_json TEXT,
    detail_fetched_at TEXT,
    streams_fetched_at TEXT,
    laps_fetched_at TEXT,
    first_seen_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_date);
CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities(sport_type);

CREATE TABLE IF NOT EXISTS streams (
    activity_id INTEGER NOT NULL,
    stream_type TEXT NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (activity_id, stream_type)
);

CREATE TABLE IF NOT EXISTS laps (
    activity_id INTEGER NOT NULL,
    lap_index INTEGER NOT NULL,
    lap_json TEXT NOT NULL,
    PRIMARY KEY (activity_id, lap_index)
);

CREATE TABLE IF NOT EXISTS gear (
    id TEXT PRIMARY KEY,
    name TEXT,
    gear_json TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS athlete_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from TEXT NOT NULL,
    zones_json TEXT NOT NULL,
    ftp_estimate INTEGER,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_metrics (
    activity_id INTEGER PRIMARY KEY,
    normalized_power INTEGER,
    intensity_factor REAL,
    tss REAL,
    trimp REAL,
    ftp_used INTEGER,
    zones_id INTEGER,
    power_zone_times TEXT,
    hr_zone_times TEXT,
    workout_description TEXT,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS bike_assignments (
    activity_id INTEGER PRIMARY KEY,
    bike TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL,
    assigned_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Strava workout_type codes. 3/12 = structured workout (run/ride), 1/11 = race
# (run/ride). These drive the W/R flags in listings and the workout-commute
# filter.
WORKOUT_TYPE_CODES = (3, 12)
RACE_TYPE_CODES = (1, 11)

# Summary fields extracted into queryable columns. Maps column -> JSON key.
_SUMMARY_COLUMNS = {
    'name': 'name',
    'sport_type': 'sport_type',
    'type': 'type',
    'start_date': 'start_date',
    'start_date_local': 'start_date_local',
    'timezone': 'timezone',
    'distance_m': 'distance',
    'moving_time_s': 'moving_time',
    'elapsed_time_s': 'elapsed_time',
    'total_elevation_gain_m': 'total_elevation_gain',
    'average_speed_mps': 'average_speed',
    'average_watts': 'average_watts',
    'weighted_average_watts': 'weighted_average_watts',
    'max_watts': 'max_watts',
    'device_watts': 'device_watts',
    'kilojoules': 'kilojoules',
    'average_heartrate': 'average_heartrate',
    'max_heartrate': 'max_heartrate',
    'average_cadence': 'average_cadence',
    'commute': 'commute',
    'trainer': 'trainer',
    'manual': 'manual',
    'workout_type': 'workout_type',
    'gear_id': 'gear_id',
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Database:
    """Access layer around the local SQLite replica."""

    def __init__(self, path: str = DEFAULT_DB_PATH):
        if path != ':memory:':
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- sync state ---------------------------------------------------------

    def get_state(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row['value'] if row else None

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        self.conn.commit()

    # -- activities ---------------------------------------------------------

    def upsert_activity_summary(self, summary: Dict):
        """Insert or update an activity from a SummaryActivity response.

        Detail/streams/laps fetch timestamps are preserved on update so an
        edited summary doesn't force re-enrichment.
        """
        cols = {col: summary.get(key) for col, key in _SUMMARY_COLUMNS.items()}
        cols['id'] = summary['id']
        cols['athlete_id'] = (summary.get('athlete') or {}).get('id')
        cols['summary_json'] = json.dumps(summary)
        cols['first_seen_at'] = _now_iso()
        cols['updated_at'] = _now_iso()

        col_names = ', '.join(cols)
        placeholders = ', '.join('?' for _ in cols)
        updates = ', '.join(
            f"{c} = excluded.{c}" for c in cols if c not in ('id', 'first_seen_at'))
        self.conn.execute(
            f"INSERT INTO activities ({col_names}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            list(cols.values()))
        self.conn.commit()

    def save_activity_detail(self, activity_id: int, detail: Dict):
        self.conn.execute(
            "UPDATE activities SET detail_json = ?, device_name = ?, "
            "detail_fetched_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(detail), detail.get('device_name'),
             _now_iso(), _now_iso(), activity_id))
        self.conn.commit()

    def save_streams(self, activity_id: int, streams: Dict):
        """Store streams (key_by_type response) as compressed JSON blobs."""
        for stream_type, stream in (streams or {}).items():
            blob = zlib.compress(json.dumps(stream.get('data', [])).encode())
            self.conn.execute(
                "INSERT OR REPLACE INTO streams (activity_id, stream_type, data) "
                "VALUES (?, ?, ?)", (activity_id, stream_type, blob))
        self.conn.execute(
            "UPDATE activities SET streams_fetched_at = ? WHERE id = ?",
            (_now_iso(), activity_id))
        self.conn.commit()

    def get_streams(self, activity_id: int) -> Dict[str, List]:
        rows = self.conn.execute(
            "SELECT stream_type, data FROM streams WHERE activity_id = ?",
            (activity_id,)).fetchall()
        return {row['stream_type']: json.loads(zlib.decompress(row['data']))
                for row in rows}

    def save_laps(self, activity_id: int, laps: List[Dict]):
        self.conn.execute(
            "DELETE FROM laps WHERE activity_id = ?", (activity_id,))
        for i, lap in enumerate(laps or []):
            self.conn.execute(
                "INSERT INTO laps (activity_id, lap_index, lap_json) VALUES (?, ?, ?)",
                (activity_id, i, json.dumps(lap)))
        self.conn.execute(
            "UPDATE activities SET laps_fetched_at = ? WHERE id = ?",
            (_now_iso(), activity_id))
        self.conn.commit()

    def get_laps(self, activity_id: int) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT lap_json FROM laps WHERE activity_id = ? ORDER BY lap_index",
            (activity_id,)).fetchall()
        return [json.loads(row['lap_json']) for row in rows]

    def get_activity(self, activity_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()

    def activities_needing_enrichment(self) -> List[sqlite3.Row]:
        """Activities missing detail, streams, or laps — newest first."""
        return self.conn.execute(
            "SELECT * FROM activities "
            "WHERE detail_fetched_at IS NULL OR streams_fetched_at IS NULL "
            "OR laps_fetched_at IS NULL ORDER BY start_date DESC").fetchall()

    def activities_needing_metrics(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT a.* FROM activities a "
            "LEFT JOIN derived_metrics d ON d.activity_id = a.id "
            "WHERE a.streams_fetched_at IS NOT NULL AND d.activity_id IS NULL "
            "ORDER BY a.start_date DESC").fetchall()

    def activities_with_streams(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM activities WHERE streams_fetched_at IS NOT NULL "
            "ORDER BY start_date DESC").fetchall()

    def mark_for_reenrichment(self, activity_id: int) -> bool:
        """Drop an activity's stored detail/streams/laps/metrics and clear its
        fetch timestamps so the next enrich re-fetches it from Strava.

        Use after editing an activity on Strava (e.g. cropping GPS points),
        which the summary upsert otherwise ignores. Returns False if the
        activity isn't in the database.
        """
        if self.get_activity(activity_id) is None:
            return False
        self.conn.execute("DELETE FROM streams WHERE activity_id = ?", (activity_id,))
        self.conn.execute("DELETE FROM laps WHERE activity_id = ?", (activity_id,))
        self.conn.execute(
            "DELETE FROM derived_metrics WHERE activity_id = ?", (activity_id,))
        self.conn.execute(
            "UPDATE activities SET detail_fetched_at = NULL, "
            "streams_fetched_at = NULL, laps_fetched_at = NULL, updated_at = ? "
            "WHERE id = ?", (_now_iso(), activity_id))
        self.conn.commit()
        return True

    # -- gear ---------------------------------------------------------------

    def upsert_gear(self, gear: Dict):
        self.conn.execute(
            "INSERT INTO gear (id, name, gear_json, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
            "gear_json = excluded.gear_json, updated_at = excluded.updated_at",
            (gear['id'], gear.get('name'), json.dumps(gear), _now_iso()))
        self.conn.commit()

    # -- athlete zones ------------------------------------------------------

    def record_zones(self, zones: Dict, ftp_estimate: Optional[int]) -> bool:
        """Record current athlete zones if they differ from the latest row.

        Returns True if a new zones row was inserted. effective_from is the
        observation date — Strava doesn't expose zone history, so past changes
        can be seeded manually with seed_zones().
        """
        normalized = json.dumps(zones, sort_keys=True)
        latest = self.conn.execute(
            "SELECT zones_json FROM athlete_zones "
            "ORDER BY effective_from DESC, id DESC LIMIT 1").fetchone()
        if latest and json.dumps(json.loads(latest['zones_json']),
                                 sort_keys=True) == normalized:
            return False
        now = _now_iso()
        self.conn.execute(
            "INSERT INTO athlete_zones (effective_from, zones_json, ftp_estimate, observed_at) "
            "VALUES (?, ?, ?, ?)", (now[:10], normalized, ftp_estimate, now))
        self.conn.commit()
        return True

    def seed_zones(self, effective_from: str, zones: Dict,
                   ftp_estimate: Optional[int]):
        """Manually insert a historical zones row (effective_from = YYYY-MM-DD)."""
        self.conn.execute(
            "INSERT INTO athlete_zones (effective_from, zones_json, ftp_estimate, observed_at) "
            "VALUES (?, ?, ?, ?)",
            (effective_from, json.dumps(zones, sort_keys=True), ftp_estimate, _now_iso()))
        self.conn.commit()

    def list_zones(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM athlete_zones ORDER BY effective_from, id").fetchall()

    def delete_zones(self, zones_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM athlete_zones WHERE id = ?", (zones_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def zones_for_date(self, date_iso: str) -> Optional[sqlite3.Row]:
        """Zones in effect on a given date: latest row at or before it,
        falling back to the earliest known row for older activities."""
        row = self.conn.execute(
            "SELECT * FROM athlete_zones WHERE effective_from <= ? "
            "ORDER BY effective_from DESC, id DESC LIMIT 1",
            (date_iso[:10],)).fetchone()
        if row:
            return row
        return self.conn.execute(
            "SELECT * FROM athlete_zones "
            "ORDER BY effective_from ASC, id ASC LIMIT 1").fetchone()

    # -- derived metrics ----------------------------------------------------

    def save_derived_metrics(self, activity_id: int, metrics: Dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO derived_metrics "
            "(activity_id, normalized_power, intensity_factor, tss, trimp, "
            " ftp_used, zones_id, power_zone_times, hr_zone_times, "
            " workout_description, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (activity_id,
             metrics.get('normalized_power'),
             metrics.get('intensity_factor'),
             metrics.get('tss'),
             metrics.get('trimp'),
             metrics.get('ftp_used'),
             metrics.get('zones_id'),
             json.dumps(metrics['power_zone_times']) if metrics.get('power_zone_times') else None,
             json.dumps(metrics['hr_zone_times']) if metrics.get('hr_zone_times') else None,
             metrics.get('workout_description'),
             _now_iso()))
        self.conn.commit()

    # -- queries ------------------------------------------------------------

    def trend_rows(self, since: Optional[str] = None,
                   sport: Optional[str] = None) -> List[sqlite3.Row]:
        """Activity rows joined with derived metrics for trend aggregation."""
        query = ("SELECT a.id, a.start_date_local, a.sport_type, a.distance_m, "
                 "a.moving_time_s, a.total_elevation_gain_m, a.kilojoules, "
                 "a.commute, d.tss, d.trimp FROM activities a "
                 "LEFT JOIN derived_metrics d ON d.activity_id = a.id WHERE 1=1")
        params = []
        if since:
            query += " AND a.start_date_local >= ?"
            params.append(since)
        if sport:
            query += " AND (a.sport_type = ? OR a.type = ?)"
            params.extend([sport, sport])
        query += " ORDER BY a.start_date_local"
        return self.conn.execute(query, params).fetchall()

    def list_activities_rows(self, since: Optional[str] = None,
                             sport: Optional[str] = None,
                             commutes: Optional[bool] = None,
                             exclude_plain_commutes: bool = False,
                             limit: Optional[int] = None) -> List[sqlite3.Row]:
        """Activity rows (newest first) joined with derived metrics, for
        table-style listings.

        exclude_plain_commutes hides commutes that aren't tagged as workouts
        (keeping workout-tagged commutes), unlike commutes=False which hides
        every commute.
        """
        query = ("SELECT a.*, d.tss, d.trimp, d.normalized_power, "
                 "d.workout_description FROM activities a "
                 "LEFT JOIN derived_metrics d ON d.activity_id = a.id WHERE 1=1")
        params: List = []
        if since:
            query += " AND a.start_date_local >= ?"
            params.append(since)
        if sport:
            query += " AND (a.sport_type = ? OR a.type = ?)"
            params.extend([sport, sport])
        if commutes is True:
            query += " AND a.commute = 1"
        elif commutes is False:
            query += " AND (a.commute IS NULL OR a.commute = 0)"
        if exclude_plain_commutes:
            placeholders = ', '.join('?' for _ in WORKOUT_TYPE_CODES)
            query += (f" AND (a.commute IS NULL OR a.commute = 0 "
                      f"OR a.workout_type IN ({placeholders}))")
            params.extend(WORKOUT_TYPE_CODES)
        query += " ORDER BY a.start_date_local DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(query, params).fetchall()

    def status(self) -> Dict:
        counts = {}
        q = self.conn.execute
        counts['activities'] = q("SELECT COUNT(*) c FROM activities").fetchone()['c']
        counts['with_detail'] = q(
            "SELECT COUNT(*) c FROM activities WHERE detail_fetched_at IS NOT NULL").fetchone()['c']
        counts['with_streams'] = q(
            "SELECT COUNT(*) c FROM activities WHERE streams_fetched_at IS NOT NULL").fetchone()['c']
        counts['with_laps'] = q(
            "SELECT COUNT(*) c FROM activities WHERE laps_fetched_at IS NOT NULL").fetchone()['c']
        counts['with_metrics'] = q("SELECT COUNT(*) c FROM derived_metrics").fetchone()['c']
        counts['gear'] = q("SELECT COUNT(*) c FROM gear").fetchone()['c']
        counts['zones_versions'] = q("SELECT COUNT(*) c FROM athlete_zones").fetchone()['c']
        dates = q("SELECT MIN(start_date_local) lo, MAX(start_date_local) hi "
                  "FROM activities").fetchone()
        counts['oldest'] = dates['lo']
        counts['newest'] = dates['hi']
        counts['last_list_sync'] = self.get_state('list_synced_at')
        return counts
