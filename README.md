# Strava Activity Analyzer

A Python tool that fetches your latest Strava cycling activity and provides detailed analysis including:

- Time spent in power zones and heart rate zones (with low/medium/high subzones)
- Workout type detection based on lap structure
- Detailed lap-by-lap breakdown

## Features

- **Automatic OAuth token refresh**: Keeps your access tokens up to date
- **Detailed zone analysis**: Splits each of the 7 power zones and 5 HR zones into low/medium/high ranges
- **Smart workout detection**: Identifies structured workouts like "3 x 15 minutes of Sweet Spot with 5 minute recovery"
- **Clean console output**: Easy-to-read formatted analysis

## Setup

### 1. Install Dependencies

```bash
cd /Users/collin/Projects/strava-client
pip install -r requirements.txt
```

### 2. Create Configuration File

Copy the example config and add your Strava credentials:

```bash
cp config.json.example config.json
```

Edit `config.json` with your Strava OAuth credentials:

```json
{
  "client_id": "YOUR_CLIENT_ID",
  "client_secret": "YOUR_CLIENT_SECRET",
  "access_token": "YOUR_ACCESS_TOKEN",
  "refresh_token": "YOUR_REFRESH_TOKEN",
  "expires_at": 0
}
```

You can get these credentials from your Strava API application settings at https://www.strava.com/settings/api

**Important:** When authorizing your application, make sure to request these OAuth scopes:
- `activity:read_all` - Required to read your activities
- `profile:read_all` - Required to read your athlete zones (power/HR zones)

### 3. Run the Analyzer

**Analyze your latest activity:**
```bash
python main.py
```

**Analyze a specific activity by ID:**
```bash
python main.py --activity=17464283022
# or
python main.py --id=17464283022
```

You can find the activity ID in the Strava URL (e.g., `https://www.strava.com/activities/17464283022`)

### Debug Mode (Development)

For faster iteration during development, you can cache activity data locally:

```bash
# Save activity data to debug_activity.json
python main.py --save

# Use cached data (skips API calls)
python main.py --debug
# or
python main.py --cached

# Force refresh cached data
python main.py --refresh
```

This is useful when testing analysis logic changes without hitting the Strava API repeatedly.

## Output Example

The script will display:

1. **Activity Summary**: Name, type, date, distance, time, avg power/HR
2. **Power Zone Analysis**: Time spent in each power zone (Active Recovery through Neuromuscular) broken down by low/medium/high subzones
3. **Heart Rate Zone Analysis**: Time spent in each HR zone (1-5) broken down by subzones
4. **Workout Analysis**: Detected workout type (e.g., "15 minute warm up, 3 x 15 minutes of Sweet Spot with 5 minute recovery between, and 10 minute cool down")
5. **Lap Breakdown**: Table showing duration, distance, avg power, avg HR, and zone for each lap

## Local Database & Trends

All activities can be replicated into a local SQLite database (`data/strava.db`)
for offline analysis and long-term trends.

```bash
# Sync: activity list + per-activity detail/streams/laps + derived metrics
# (TRIMP, NP, IF, TSS, zone times). Resumable — interrupt or hit the API
# quota and the next run continues where it left off.
python -m stravaclient sync

# Limit how many activities get enriched this run
python -m stravaclient sync --limit 50

# Only refresh the activity list (cheap, ~15 requests for a full history)
python -m stravaclient sync --no-enrich

# Replication progress and database contents
python -m stravaclient status

# Trends
python -m stravaclient trends --metric miles --by week --last 12
python -m stravaclient trends --metric tss --by month --since 2026-01-01
python -m stravaclient trends --metric trimp --by month --sport Ride --no-commutes
```

Trend metrics: `miles`, `hours`, `elevation`, `tss`, `trimp`, `kj`, `rides`
(buckets: `week`, `month`, `year`). Distance/time/elevation work from summary
data alone; TSS and TRIMP need streams, which are fetched during enrichment.

**Backfill and rate limits:** enrichment costs 3 API requests per activity.
Strava allows ~100 read requests per 15 minutes and ~1,000 per day, so a full
multi-year backfill takes several days of `sync` runs. The sync engine sleeps
through 15-minute windows automatically and stops cleanly with a resume
message when the daily quota is reached.

**Zone history:** Strava only exposes your *current* zones, so the database
versions them over time (`athlete_zones` table) and computes each activity's
metrics with the zones in effect on its date. Seed past FTP changes so
historical TSS uses the right FTP:

```bash
python -m stravaclient zones                              # list versions
python -m stravaclient zones --set-ftp 250 --from 2022-03-15
python -m stravaclient zones --delete 3                   # undo a mistake
python -m stravaclient recompute                          # re-derive metrics
```

Each `--set-ftp` builds Strava-style power zones (55/75/90/105/120/150% of
FTP) effective from that date until the next version; HR zones are copied
from the nearest existing version. Activities older than the earliest version
fall back to it.

## Power Zones

The analyzer uses the standard 7-zone power model:

1. Active Recovery (< 55% FTP)
2. Endurance (55-75% FTP)
3. Tempo (75-90% FTP)
4. Threshold (90-105% FTP)
5. VO2max (105-120% FTP)
6. Anaerobic (120-150% FTP)
7. Neuromuscular (> 150% FTP)

Each zone is further divided into Low, Medium, and High ranges.

## Requirements

- Python 3.6+
- Valid Strava OAuth tokens
- Activities with power meter and/or heart rate data

## Files

- `main.py`: Entry point script
- `strava_client.py`: Strava API client with authentication
- `zone_analyzer.py`: Zone calculation and time analysis
- `workout_detector.py`: Workout pattern detection from laps
- `config.json`: Your Strava credentials (not tracked in git)
- `requirements.txt`: Python dependencies

## Notes

- The script only analyzes your most recent activity
- Both power and heart rate data are optional (the script will skip analysis for missing data types)
- Token refresh is automatic - your `config.json` will be updated with new tokens as needed
- FTP is estimated from your Zone 4 (Threshold) lower bound from Strava zones
