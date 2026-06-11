from typing import List, Dict, Tuple, Optional
from collections import Counter


class WorkoutDetector:
    """Detects workout patterns from lap data."""

    # Power zone thresholds (as percentage of FTP)
    ZONE_THRESHOLDS = {
        'Active Recovery': (0, 0.55),
        'Endurance': (0.55, 0.75),
        'Tempo': (0.75, 0.88),
        'Sweet Spot': (0.88, 0.94),  # Low end of threshold zone, ~92% FTP
        'Threshold': (0.94, 1.05),
        'VO2max': (1.05, 1.20),
        'Anaerobic': (1.20, 1.50),
        'Neuromuscular': (1.50, 10.0)
    }

    def __init__(self, power_zones: List[Tuple[int, int]]):
        """
        Initialize workout detector.

        Args:
            power_zones: List of (min, max) tuples for power zones
        """
        self.power_zones = power_zones
        # Estimate FTP from Zone 4 (Threshold) range
        # Zone 4 is typically 90-105% of FTP, so we use the midpoint
        if len(power_zones) >= 4:
            zone4_min = power_zones[3][0]
            zone4_max = power_zones[3][1]
            if zone4_max == float('inf'):
                # If no upper bound, use lower bound / 0.90
                self.ftp = int(zone4_min / 0.90)
            else:
                # Use midpoint of zone 4 and assume it's ~97.5% of FTP
                zone4_mid = (zone4_min + zone4_max) / 2
                self.ftp = int(zone4_mid / 0.975)
        else:
            self.ftp = None

    def _classify_power_level(self, avg_power: float) -> str:
        """Classify average power into training zone."""
        if not self.ftp or avg_power == 0:
            return "Recovery"

        power_ratio = avg_power / self.ftp

        if power_ratio < 0.55:
            return "Recovery"
        elif power_ratio < 0.75:
            return "Endurance"
        elif power_ratio < 0.88:
            return "Tempo"
        elif power_ratio < 0.94:
            return "Sweet Spot"
        elif power_ratio < 1.05:
            return "Threshold"
        elif power_ratio < 1.20:
            return "VO2max"
        elif power_ratio < 1.50:
            return "Anaerobic"
        else:
            return "Sprint"

    def _format_duration(self, seconds: float) -> str:
        """Format duration in a readable way (e.g., '15 minutes', '90 seconds')."""
        # Round to nearest 10 seconds if within 2 seconds of a 10-second multiple
        remainder = seconds % 10
        if remainder <= 2 or remainder >= 8:
            seconds = round(seconds / 10) * 10

        minutes = seconds / 60

        if minutes >= 1:
            # Check if it's close to a whole number (within 0.15 minutes, which is 9 seconds)
            if abs(minutes - round(minutes)) <= 0.15:
                whole_minutes = int(round(minutes))
                return f"{whole_minutes} min{'s' if whole_minutes != 1 else ''}"
            else:
                return f"{minutes:.1f} mins"
        else:
            return f"{int(seconds)} seconds"

    def _filter_short_final_lap(self, laps: List[Dict], threshold_seconds: int = 30) -> List[Dict]:
        """
        Filter out very short final lap (common in Zwift when activity auto-ends).

        Args:
            laps: List of lap dictionaries
            threshold_seconds: Minimum duration for final lap to be included

        Returns:
            Filtered list of laps
        """
        if not laps or len(laps) <= 1:
            return laps

        last_lap = laps[-1]
        last_lap_duration = last_lap.get('elapsed_time', 0)

        # If last lap is very short, exclude it
        if last_lap_duration < threshold_seconds:
            return laps[:-1]

        return laps

    def _group_intervals(self, laps: List[Dict]) -> List[Dict]:
        """
        Group consecutive laps into intervals (work) and recovery periods.

        Returns list of intervals with their type, count, duration, and power.
        """
        intervals = []
        current_group = None

        for lap in laps:
            avg_power = lap.get('average_watts', 0)
            elapsed_time = lap.get('elapsed_time', 0)
            zone = self._classify_power_level(avg_power)

            lap_info = {
                'zone': zone,
                'power': avg_power,
                'duration': elapsed_time
            }

            if current_group is None:
                current_group = {
                    'zone': zone,
                    'laps': [lap_info],
                    'total_duration': elapsed_time,
                    'avg_power': avg_power
                }
            elif zone == current_group['zone']:
                # Same zone, add to current group
                current_group['laps'].append(lap_info)
                current_group['total_duration'] += elapsed_time
                # Recalculate average power
                total_power = sum(l['power'] for l in current_group['laps'])
                current_group['avg_power'] = total_power / len(current_group['laps'])
            else:
                # Different zone, save current group and start new one
                intervals.append(current_group)
                current_group = {
                    'zone': zone,
                    'laps': [lap_info],
                    'total_duration': elapsed_time,
                    'avg_power': avg_power
                }

        # Add the last group
        if current_group:
            intervals.append(current_group)

        return intervals

    def _identify_openers(self, laps: List[Dict]) -> Tuple[int, List[Dict]]:
        """
        Identify opener intervals (short, high-intensity efforts near the beginning).

        Openers are typically short (20-40s) high-intensity efforts that appear
        at the start of the workout, often with recovery periods between them.

        Returns:
            Tuple of (number_of_laps_to_skip, opener_work_laps)
        """
        openers = []
        # Look at first 10 laps for potential openers
        search_window = min(10, len(laps))
        last_opener_index = -1

        for i in range(search_window):
            lap = laps[i]
            duration = lap.get('elapsed_time', 0)
            avg_power = lap.get('average_watts', 0)

            # Openers are typically 20-40 seconds at high intensity
            if 20 <= duration <= 40 and avg_power > self.ftp * 0.88:
                openers.append(lap)
                last_opener_index = i
            elif len(openers) >= 2:
                # Stop looking after we've found at least 2 openers and hit a significantly different pattern
                # Check if this lap is much longer (not recovery between openers)
                if duration > 90:  # More than 1.5 minutes suggests end of opener section
                    break

        # Only consider them openers if we found at least 2
        if len(openers) >= 2:
            # Return the index after the last opener (to skip all openers and their recoveries)
            return last_opener_index + 1, openers
        return 0, []

    def detect_workout_type(self, laps: List[Dict]) -> str:
        """
        Detect the type of workout from lap data.

        Args:
            laps: List of lap dictionaries from Strava API

        Returns:
            String description of the workout
        """
        if not laps or not self.ftp:
            return "Unable to determine workout type"

        # Filter out very short final lap (common Zwift artifact)
        filtered_laps = self._filter_short_final_lap(laps)

        # Check if first lap is a warmup (Recovery/Endurance zone, >5 minutes)
        warmup_lap = None
        analysis_laps = filtered_laps
        if filtered_laps:
            first_lap = filtered_laps[0]
            first_power = first_lap.get('average_watts', 0)
            first_duration = first_lap.get('elapsed_time', 0)
            first_zone = self._classify_power_level(first_power)

            # If first lap is recovery/endurance and longer than 5 minutes, treat as warmup
            if first_zone in ['Recovery', 'Endurance'] and first_duration > 300:
                warmup_lap = first_lap
                analysis_laps = filtered_laps[1:]

        # Identify openers
        skip_laps, openers = self._identify_openers(analysis_laps)

        # Skip the opener section (including recovery laps between openers)
        remaining_laps = analysis_laps[skip_laps:]

        intervals = self._group_intervals(remaining_laps)

        if not intervals:
            return "Unable to determine workout type"

        if len(intervals) < 2:
            # Single continuous effort
            zone = intervals[0]['zone']
            duration = self._format_duration(intervals[0]['total_duration'])
            return f"{duration} {zone} ride"

        # Identify workout structure
        warmup = None
        cooldown = None
        main_work_intervals = []

        # Use the warmup lap we identified earlier
        if warmup_lap:
            warmup = {
                'zone': self._classify_power_level(warmup_lap.get('average_watts', 0)),
                'total_duration': warmup_lap.get('elapsed_time', 0)
            }

        # Last interval is likely cooldown if it's recovery/endurance
        if intervals and intervals[-1]['zone'] in ['Recovery', 'Endurance']:
            cooldown = intervals[-1]
            intervals = intervals[:-1]

        # Group similar work intervals together
        # Separate work from recovery
        work_intervals = [i for i in intervals if i['zone'] not in ['Recovery', 'Endurance']]
        recovery_intervals = [i for i in intervals if i['zone'] in ['Recovery', 'Endurance']]

        # Group work intervals by similar duration (within 20%)
        if work_intervals:
            work_groups = {}
            for w in work_intervals:
                duration = w['total_duration']
                zone = w['zone']

                # Find matching group
                matched = False
                for key in work_groups:
                    key_duration, key_zone = key
                    if abs(duration - key_duration) / max(duration, key_duration) < 0.2 and zone == key_zone:
                        work_groups[key].append(w)
                        matched = True
                        break

                if not matched:
                    work_groups[(duration, zone)] = [w]

            # Sort by count (descending) to get main set first
            main_work_intervals = sorted(work_groups.items(), key=lambda x: len(x[1]), reverse=True)

        # Build workout description
        description_parts = []

        # Add warmup first
        if warmup:
            warmup_str = f"{self._format_duration(warmup['total_duration'])} warm up"
            description_parts.append(warmup_str)

        # Add openers if present
        if openers:
            opener_avg_power = sum(l.get('average_watts', 0) for l in openers) / len(openers)
            opener_zone = self._classify_power_level(opener_avg_power)
            opener_duration = sum(l.get('elapsed_time', 0) for l in openers) / len(openers)
            openers_desc = f"{len(openers)} x {self._format_duration(opener_duration)} openers ({opener_zone})"
            description_parts.append(openers_desc)

        # Add main work intervals
        if main_work_intervals:
            for (duration, zone), interval_list in main_work_intervals:
                count = len(interval_list)
                formatted_duration = self._format_duration(duration)

                # Calculate average recovery time between these intervals
                if recovery_intervals and count > 1:
                    # Find recovery intervals that match the pattern
                    avg_recovery = sum(r['total_duration'] for r in recovery_intervals) / len(recovery_intervals)
                    # Format recovery time with rounding applied
                    recovery_str = f" with {self._format_duration(avg_recovery)} recovery"
                else:
                    recovery_str = ""

                work_desc = f"{count} x {formatted_duration} {zone}{recovery_str}"
                description_parts.append(work_desc)

        # Add cooldown
        if cooldown:
            cooldown_str = f"{self._format_duration(cooldown['total_duration'])} cool down"
            description_parts.append(cooldown_str)

        if description_parts:
            # Join with commas and "and" for better readability
            if len(description_parts) == 1:
                return description_parts[0]
            elif len(description_parts) == 2:
                return f"{description_parts[0]} and {description_parts[1]}"
            else:
                # Join all but last with commas, then add "and" before the last
                return f"{', '.join(description_parts[:-1])}, and {description_parts[-1]}"

        return "Mixed workout"

    def get_lap_summary(self, laps: List[Dict], hr_stream: List[int] = None, time_stream: List[int] = None) -> List[Dict]:
        """
        Get a detailed summary of each lap.

        Args:
            laps: List of lap dictionaries from Strava API
            hr_stream: Optional heart rate stream data for calculating 2-min HR
            time_stream: Optional time stream data for calculating 2-min HR

        Returns:
            List of lap summaries with zone classification
        """
        # Filter out very short final lap
        filtered_laps = self._filter_short_final_lap(laps)

        summary = []

        for i, lap in enumerate(filtered_laps, 1):
            avg_power = lap.get('average_watts', 0)
            elapsed_time = lap.get('elapsed_time', 0)
            avg_hr = lap.get('average_heartrate', 0)
            max_hr = lap.get('max_heartrate', 0)
            avg_cadence = lap.get('average_cadence', 0)
            distance = lap.get('distance', 0)

            zone = self._classify_power_level(avg_power)

            hr_at_2min = None
            if hr_stream and time_stream:
                hr_at_2min = self._get_hr_at_2min(lap, hr_stream, time_stream)

            summary.append({
                'lap_number': i,
                'duration': elapsed_time,
                'duration_formatted': self._format_duration(elapsed_time),
                'distance_mi': distance / 1609.34,  # Convert meters to miles
                'avg_power': avg_power,
                'hr_at_2min': hr_at_2min,
                'avg_hr': avg_hr,
                'max_hr': max_hr,
                'avg_cadence': avg_cadence,
                'zone': zone
            })

        return summary

    def _get_hr_at_2min(self, lap: Dict, hr_stream: List[int], time_stream: List[int]) -> Optional[int]:
        """
        Get heart rate at 2 minutes of moving time into a lap.

        Walks the time stream and accumulates only small inter-sample deltas so
        that auto-pause gaps don't push the 2-minute mark past a real pause.

        Args:
            lap: Lap dictionary with start_index and end_index
            hr_stream: Heart rate stream data
            time_stream: Time stream data

        Returns:
            Heart rate at 2 minutes, or None if lap is too short or data unavailable
        """
        start_index = lap.get('start_index')
        end_index = lap.get('end_index')

        if start_index is None or end_index is None:
            return None

        if start_index >= len(time_stream) or end_index >= len(time_stream):
            return None

        PAUSE_GAP_SECONDS = 10
        moving_elapsed = 0.0

        last_idx = min(end_index + 1, len(time_stream))
        for idx in range(start_index + 1, last_idx):
            delta = time_stream[idx] - time_stream[idx - 1]
            if 0 < delta <= PAUSE_GAP_SECONDS:
                moving_elapsed += delta
            if moving_elapsed >= 120 and idx < len(hr_stream):
                return hr_stream[idx]

        return None
