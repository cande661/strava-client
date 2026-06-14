#!/usr/bin/env python3
"""
Strava Activity Analyzer

Fetches the latest activity from Strava and provides detailed analysis including:
- Time spent in power and heart rate zones (with subzones)
- Workout type detection from lap data
"""

import sys
import json
import os
from strava_client import StravaClient
from zone_analyzer import ZoneAnalyzer
from workout_detector import WorkoutDetector
from stravaclient.metrics import hr_max_for_activity


def print_header(text: str, char: str = "="):
    """Print a formatted header."""
    print(f"\n{char * 60}")
    print(f"{text:^60}")
    print(f"{char * 60}\n")


def print_activity_summary(activity: dict, trimp: int = None, normalized_power: int = None):
    """Print basic activity information."""
    print_header("Activity Summary")

    print(f"Name:     {activity.get('name', 'N/A')}")
    print(f"Type:     {activity.get('type', 'N/A')}")
    print(f"Date:     {activity.get('start_date_local', 'N/A')}")

    activity_id = activity.get('id')
    if activity_id:
        print(f"URL:      https://www.strava.com/activities/{activity_id}")

    distance_miles = activity.get('distance', 0) / 1609.34  # Convert meters to miles
    print(f"Distance: {distance_miles:.2f} mi")

    moving_time = activity.get('moving_time', 0)
    elapsed_time = activity.get('elapsed_time', 0)

    # Format moving time
    hours = int(moving_time // 3600)
    minutes = int((moving_time % 3600) // 60)
    seconds = int(moving_time % 60)
    time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    # If elapsed time differs from moving time, show both
    if elapsed_time != moving_time and elapsed_time > 0:
        elapsed_hours = int(elapsed_time // 3600)
        elapsed_minutes = int((elapsed_time % 3600) // 60)
        elapsed_seconds = int(elapsed_time % 60)
        elapsed_str = f"{elapsed_hours:02d}:{elapsed_minutes:02d}:{elapsed_seconds:02d}"
        print(f"Time:     {time_str} ({elapsed_str} elapsed)")
    else:
        print(f"Time:     {time_str}")

    avg_watts = activity.get('average_watts')
    if avg_watts:
        has_power_meter = activity.get('device_watts', False)
        power_label = "Avg Power:" if has_power_meter else "Est Power:"
        print(f"{power_label} {avg_watts:.0f} W")

    if normalized_power is not None:
        print(f"NP:       {normalized_power} W")

    avg_hr = activity.get('average_heartrate')
    if avg_hr:
        print(f"Avg HR:    {avg_hr:.0f} bpm")

    if trimp is not None:
        print(f"TRIMP:     {trimp}")


def print_zone_analysis(zone_data: dict, title: str, zone_analyzer: 'ZoneAnalyzer', is_power: bool = True):
    """Print time spent in each zone and subzone."""
    import math
    print_header(title, "-")

    if not zone_data:
        print("No data available")
        return

    # Get the appropriate zones
    zones_list = zone_analyzer.power_zones if is_power else zone_analyzer.hr_zones
    zone_names = zone_analyzer.POWER_ZONE_NAMES if is_power else zone_analyzer.HR_ZONE_NAMES
    unit = "W" if is_power else "bpm"

    # Group by main zone
    zones = {}
    for full_name, time_seconds in zone_data.items():
        parts = full_name.split(" - ")
        if len(parts) == 2:
            zone_name, subzone_name = parts
            if zone_name not in zones:
                zones[zone_name] = {}
            zones[zone_name][subzone_name] = time_seconds

    # Calculate total time
    total_time = sum(zone_data.values())

    # First pass: build all line entries as (blank_before, prefix, pct)
    # so we can find the max prefix width and align all bars.
    # Iterate in canonical zone order so ranges always match names correctly,
    # regardless of which zones were first encountered in the stream.
    entries = []

    for zone_name in zone_names:
        if zone_name not in zones:
            continue
        subzones = zones[zone_name]
        zone_idx = zone_names.index(zone_name)

        zone_total = sum(subzones.values())
        zone_pct = (zone_total / total_time * 100) if total_time > 0 else 0

        if zone_idx < len(zones_list):
            zone_min, zone_max = zones_list[zone_idx]
            if zone_max == float('inf'):
                zone_range = f"{zone_min}+ {unit}"
            else:
                zone_range = f"{zone_min}-{int(zone_max)} {unit}"
        else:
            zone_range = ""

        zone_prefix = f"{zone_name} ({zone_range}): {format_time(zone_total)} ({zone_pct:.1f}%)"
        entries.append((True, zone_prefix, zone_pct))

        if zone_idx < len(zones_list):
            zone_min, zone_max = zones_list[zone_idx]
            subzone_ranges = zone_analyzer._split_zone_into_subzones(zone_min, zone_max)

            for subzone_idx, subzone_name in enumerate(["Low", "Medium", "High"]):
                if subzone_name in subzones:
                    sub_time = subzones[subzone_name]
                    sub_pct = (sub_time / total_time * 100) if total_time > 0 else 0

                    if subzone_idx < len(subzone_ranges):
                        sub_min, sub_max = subzone_ranges[subzone_idx]
                        if sub_max == float('inf'):
                            sub_range = f"{int(sub_min)}+ {unit}"
                        else:
                            if subzone_idx == 0:
                                display_min = int(sub_min)
                                display_max = int(math.floor(sub_max))
                            elif subzone_idx == len(subzone_ranges) - 1:
                                display_min = int(math.ceil(sub_min))
                                display_max = int(sub_max)
                            else:
                                display_min = int(math.ceil(sub_min))
                                display_max = int(math.floor(sub_max))
                            sub_range = f"{display_min}-{display_max} {unit}"
                    else:
                        sub_range = ""

                    sub_prefix = f"  {subzone_name:8s} ({sub_range:>12s}): {format_time(sub_time):>8s} ({sub_pct:5.1f}%)"
                    entries.append((False, sub_prefix, sub_pct))

    # Second pass: print with bars left-aligned
    max_width = max(len(prefix) for _, prefix, _ in entries)

    for blank_before, prefix, pct in entries:
        if blank_before:
            print()
        print(f"{prefix:<{max_width}} {make_bar(pct)}")


def print_workout_analysis(workout_type: str, lap_summary: list):
    """Print workout type and lap breakdown."""
    print_header("Workout Analysis", "-")

    print(f"Workout Type: {workout_type}\n")

    if lap_summary:
        print("Lap Breakdown:")
        print(f"{'Lap':<6} {'Duration':<12} {'Distance':<10} {'Avg Power':<12} {'2min HR':<10} {'Avg HR':<10} {'Max HR':<10} {'Cadence':<10} {'Zone':<15}")
        print("-" * 105)

        for lap in lap_summary:
            lap_num = lap['lap_number']
            duration = lap['duration_formatted']
            distance = f"{lap['distance_mi']:.2f} mi"
            power = f"{lap['avg_power']:.0f} W" if lap['avg_power'] > 0 else "N/A"
            hr_2min = f"{lap['hr_at_2min']:.0f} bpm" if lap.get('hr_at_2min') else "-"
            avg_hr = f"{lap['avg_hr']:.0f} bpm" if lap['avg_hr'] > 0 else "N/A"
            max_hr = f"{lap['max_hr']:.0f} bpm" if lap.get('max_hr', 0) > 0 else "N/A"
            cadence = f"{lap['avg_cadence']:.0f} rpm" if lap.get('avg_cadence', 0) > 0 else "N/A"
            zone = lap['zone']

            print(f"{lap_num:<6} {duration:<12} {distance:<10} {power:<12} {hr_2min:<10} {avg_hr:<10} {max_hr:<10} {cadence:<10} {zone:<15}")


def format_time(seconds: float) -> str:
    """Format seconds into MM:SS format."""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"


def make_bar(pct: float) -> str:
    """
    Generate an 8-character-wide ASCII bar using Unicode block characters.
    8 full characters = 100%, subdivided into eighths for 64 possible values.
    """
    partial = ['', '▏', '▎', '▍', '▌', '▋', '▊', '▉']
    total_eighths = round(pct / 100 * 64)
    full_blocks = total_eighths // 8
    remainder = total_eighths % 8
    bar = '█' * full_blocks
    if remainder:
        bar += partial[remainder]
    return bar


def load_cached_data(cache_file='debug_activity.json'):
    """Load cached activity data from file."""
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            return json.load(f)
    return None


def save_cached_data(data, cache_file='debug_activity.json'):
    """Save activity data to cache file."""
    with open(cache_file, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"\n[DEBUG] Activity data saved to {cache_file}")


def print_help():
    """Print help message with usage information."""
    help_text = """
Strava Activity Analyzer - Analyze your cycling activities with detailed zone breakdowns

USAGE:
    python main.py [OPTIONS]

OPTIONS:
    --activity=ID       Analyze specific activity by ID (e.g., --activity=17464283022)
    --id=ID            Same as --activity (e.g., --id=17464283022)

    --debug            Use cached activity data (no API calls)
    --cached           Same as --debug
    --save             Fetch from API and save to cache for future use
    --refresh          Force refresh even in debug mode

    --help, -h         Show this help message

EXAMPLES:
    # Analyze latest activity
    python main.py

    # Analyze specific activity
    python main.py --activity=17464283022

    # Save activity for offline analysis
    python main.py --save

    # Use cached data for fast iteration
    python main.py --debug

Find activity IDs in Strava URLs: https://www.strava.com/activities/[ID]
"""
    print(help_text)


def main():
    """Main entry point for the Strava analyzer."""
    try:
        # Check for help flag
        if '--help' in sys.argv or '-h' in sys.argv:
            print_help()
            return

        # Check for debug mode (use --debug or --cached flag)
        use_cache = '--debug' in sys.argv or '--cached' in sys.argv
        force_refresh = '--refresh' in sys.argv

        # Check for specific activity ID
        activity_id_arg = None
        for arg in sys.argv[1:]:
            if arg.startswith('--activity=') or arg.startswith('--id='):
                activity_id_arg = arg.split('=')[1]
                break

        cache_file = 'debug_activity.json'
        cached_data = None

        if use_cache and not force_refresh:
            cached_data = load_cached_data(cache_file)
            if cached_data:
                print("[DEBUG MODE] Using cached activity data")
                print(f"[DEBUG] Loaded from {cache_file}\n")

        if cached_data:
            # Use cached data
            zones_data = cached_data['zones']
            activity = cached_data['activity']
            streams = cached_data['streams']
            laps = cached_data['laps']
        else:
            # Fetch from API
            print("Connecting to Strava API...")
            client = StravaClient()

            # Fetch athlete zones
            print("Fetching athlete zones...")
            zones_data = client.get_athlete_zones()

            # Fetch activity (either specific ID or latest)
            if activity_id_arg:
                print(f"Fetching activity {activity_id_arg}...")
                activity = client.get_activity(int(activity_id_arg))
                activity_id = activity['id']
            else:
                print("Fetching latest activity...")
                activity = client.get_latest_activity()
                activity_id = activity['id']

            # Fetch activity streams
            print("Fetching activity streams...")
            streams = client.get_activity_streams(
                activity_id,
                ['time', 'heartrate', 'watts']
            )

            # Fetch and analyze laps
            print("Fetching lap data...")
            laps = client.get_activity_laps(activity_id)

            # Save to cache for future debug runs
            if use_cache or '--save' in sys.argv:
                save_cached_data({
                    'zones': zones_data,
                    'activity': activity,
                    'streams': streams,
                    'laps': laps
                }, cache_file)

        # Initialize zone analyzer
        zone_analyzer = ZoneAnalyzer(zones_data)

        # Analyze zones
        time_stream = streams.get('time', {}).get('data', [])
        hr_stream = streams.get('heartrate', {}).get('data', [])
        power_stream = streams.get('watts', {}).get('data', [])

        # Calculate summary metrics before printing. Use the age-based HRmax
        # (from the stored birthdate) when available so this matches the TRIMP
        # in the local database; otherwise fall back to the zone-5 estimate.
        hr_max = hr_max_for_activity(activity)
        trimp = zone_analyzer.calculate_trimp(
            hr_stream, time_stream, hr_max=hr_max) if hr_stream else None
        has_power_meter = activity.get('device_watts', False)
        normalized_power = zone_analyzer.calculate_normalized_power(power_stream) if has_power_meter and power_stream else None

        # Print basic activity info
        print_activity_summary(activity, trimp=trimp, normalized_power=normalized_power)

        if power_stream:
            power_zone_data = zone_analyzer.analyze_power_zones(time_stream, power_stream)
            print_zone_analysis(power_zone_data, "Power Zone Analysis", zone_analyzer, is_power=True)
        else:
            print("\nNo power data available for this activity")

        if hr_stream:
            hr_zone_data = zone_analyzer.analyze_hr_zones(time_stream, hr_stream)
            print_zone_analysis(hr_zone_data, "Heart Rate Zone Analysis", zone_analyzer, is_power=False)
        else:
            print("\nNo heart rate data available for this activity")

        # Analyze laps
        if laps:
            # Only do workout detection if we have power data
            # Check if activity has power meter data (not just estimated power)
            has_power_meter = activity.get('device_watts', False)

            if has_power_meter and power_stream:
                workout_detector = WorkoutDetector(zone_analyzer.power_zones)
                workout_type = workout_detector.detect_workout_type(laps)
                lap_summary = workout_detector.get_lap_summary(laps, hr_stream, time_stream)
                print_workout_analysis(workout_type, lap_summary)
            else:
                # For activities without power meter, show simplified lap summary
                print_header("Workout Analysis", "-")
                if not has_power_meter and activity.get('average_watts'):
                    print("Note: This activity has estimated power (no power meter)\n")

                print("Lap Breakdown:")
                print(f"{'Lap':<6} {'Duration':<12} {'Distance':<10} {'Avg HR':<10} {'Max HR':<10} {'Cadence':<10}")
                print("-" * 68)

                for i, lap in enumerate(laps, 1):
                    elapsed_time = lap.get('elapsed_time', 0)
                    distance = lap.get('distance', 0) / 1609.34  # Convert to miles
                    avg_hr = lap.get('average_heartrate', 0)
                    max_hr = lap.get('max_heartrate', 0)
                    avg_cadence = lap.get('average_cadence', 0)

                    # Format duration
                    minutes = int(elapsed_time // 60)
                    secs = int(elapsed_time % 60)
                    duration_str = f"{minutes}:{secs:02d}"

                    hr_avg_str = f"{avg_hr:.0f} bpm" if avg_hr > 0 else "N/A"
                    hr_max_str = f"{max_hr:.0f} bpm" if max_hr > 0 else "N/A"
                    cadence_str = f"{avg_cadence:.0f} rpm" if avg_cadence > 0 else "N/A"

                    print(f"{i:<6} {duration_str:<12} {distance:.2f} mi{'':<2} {hr_avg_str:<10} {hr_max_str:<10} {cadence_str:<10}")
        else:
            print("\nNo lap data available for this activity")

        print_header("Analysis Complete")

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("\nPlease create a config.json file with your Strava credentials.", file=sys.stderr)
        print("See config.json.example for the required format.", file=sys.stderr)
        sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
