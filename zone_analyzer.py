import math
from typing import Dict, List, Optional, Tuple
from collections import defaultdict


class ZoneAnalyzer:
    """Analyzes time spent in power and heart rate zones with subzone granularity."""

    POWER_ZONE_NAMES = [
        "Active Recovery",
        "Endurance",
        "Tempo",
        "Threshold",
        "VO2max",
        "Anaerobic",
        "Neuromuscular"
    ]

    HR_ZONE_NAMES = [
        "Zone 1 (Recovery)",
        "Zone 2 (Endurance)",
        "Zone 3 (Tempo)",
        "Zone 4 (Threshold)",
        "Zone 5 (Anaerobic)"
    ]

    SUBZONE_NAMES = ["Low", "Medium", "High"]

    def __init__(self, zones_data: Dict):
        """
        Initialize the zone analyzer with athlete zones data.

        Args:
            zones_data: Response from Strava /athlete/zones endpoint
        """
        self.power_zones = self._parse_power_zones(zones_data)
        self.hr_zones = self._parse_hr_zones(zones_data)

    def _parse_power_zones(self, zones_data: Dict) -> List[Tuple[int, int]]:
        """Parse power zones into list of (min, max) tuples."""
        power_zones = []

        power_data = zones_data.get('power', {})
        for zone in power_data.get('zones', []):
            min_val = zone.get('min', 0)
            max_val = zone.get('max', -1)
            # Convert -1 (Strava's indicator for no max) to infinity
            if max_val == -1:
                max_val = float('inf')
            power_zones.append((min_val, max_val))

        return power_zones

    def _parse_hr_zones(self, zones_data: Dict) -> List[Tuple[int, int]]:
        """Parse heart rate zones into list of (min, max) tuples."""
        hr_zones = []

        hr_data = zones_data.get('heart_rate', {})
        for zone_idx, zone in enumerate(hr_data.get('zones', [])):
            min_val = zone.get('min', 0)
            max_val = zone.get('max', -1)

            # For Zone 1, set a realistic minimum HR floor (50 bpm)
            if zone_idx == 0 and min_val < 50:
                min_val = 50

            # Convert -1 (Strava's indicator for no max) to infinity
            if max_val == -1:
                max_val = float('inf')
            hr_zones.append((min_val, max_val))

        return hr_zones

    def _split_zone_into_subzones(self, min_val: int, max_val: float) -> List[Tuple[float, float]]:
        """
        Split a zone into three subzones (low, medium, high).

        Args:
            min_val: Minimum value of the zone
            max_val: Maximum value of the zone

        Returns:
            List of (min, max) tuples for each subzone
        """
        if max_val == float('inf'):
            # For open-ended zones, create reasonable subzones
            range_size = min_val * 0.5  # Arbitrary choice for open zones
            return [
                (min_val, min_val + range_size / 3),
                (min_val + range_size / 3, min_val + 2 * range_size / 3),
                (min_val + 2 * range_size / 3, float('inf'))
            ]

        range_size = max_val - min_val
        third = range_size / 3

        return [
            (min_val, min_val + third),
            (min_val + third, min_val + 2 * third),
            (min_val + 2 * third, max_val)
        ]

    def analyze_power_zones(self, time_stream: List[int], power_stream: List[int]) -> Dict:
        """
        Analyze time spent in each power zone and subzone.

        Args:
            time_stream: List of time values in seconds
            power_stream: List of power values in watts

        Returns:
            Dictionary with zone/subzone time analysis
        """
        if not time_stream or not power_stream:
            return {}

        time_in_subzones = defaultdict(float)

        for i in range(len(power_stream)):
            if i == 0:
                duration = time_stream[i]
            else:
                duration = time_stream[i] - time_stream[i - 1]

            power = power_stream[i]

            # Find which zone this power value belongs to
            # Note: Strava zones use inclusive upper bounds (e.g., 153-207 includes both 153 and 207)
            for zone_idx, (zone_min, zone_max) in enumerate(self.power_zones):
                if zone_max == float('inf'):
                    in_zone = power >= zone_min
                else:
                    in_zone = zone_min <= power <= zone_max

                if in_zone:
                    # Split into subzones and find which subzone
                    subzones = self._split_zone_into_subzones(zone_min, zone_max)
                    for subzone_idx, (sub_min, sub_max) in enumerate(subzones):
                        if sub_max == float('inf'):
                            in_subzone = power >= sub_min
                        else:
                            in_subzone = sub_min <= power <= sub_max

                        if in_subzone:
                            zone_name = self.POWER_ZONE_NAMES[zone_idx]
                            subzone_name = self.SUBZONE_NAMES[subzone_idx]
                            key = f"{zone_name} - {subzone_name}"
                            time_in_subzones[key] += duration
                            break
                    break

        return dict(time_in_subzones)

    def analyze_hr_zones(self, time_stream: List[int], hr_stream: List[int]) -> Dict:
        """
        Analyze time spent in each heart rate zone and subzone.

        Args:
            time_stream: List of time values in seconds
            hr_stream: List of heart rate values in bpm

        Returns:
            Dictionary with zone/subzone time analysis
        """
        if not time_stream or not hr_stream:
            return {}

        time_in_subzones = defaultdict(float)

        for i in range(len(hr_stream)):
            if i == 0:
                duration = time_stream[i]
            else:
                duration = time_stream[i] - time_stream[i - 1]

            hr = hr_stream[i]

            # Find which zone this HR value belongs to
            # Note: Strava zones use inclusive upper bounds (e.g., 107-141 includes both 107 and 141)
            for zone_idx, (zone_min, zone_max) in enumerate(self.hr_zones):
                if zone_max == float('inf'):
                    in_zone = hr >= zone_min
                else:
                    in_zone = zone_min <= hr <= zone_max

                if in_zone:
                    # Split into subzones and find which subzone
                    subzones = self._split_zone_into_subzones(zone_min, zone_max)
                    for subzone_idx, (sub_min, sub_max) in enumerate(subzones):
                        if sub_max == float('inf'):
                            in_subzone = hr >= sub_min
                        else:
                            in_subzone = sub_min <= hr <= sub_max

                        if in_subzone:
                            zone_name = self.HR_ZONE_NAMES[zone_idx]
                            subzone_name = self.SUBZONE_NAMES[subzone_idx]
                            key = f"{zone_name} - {subzone_name}"
                            time_in_subzones[key] += duration
                            break
                    break

        return dict(time_in_subzones)

    def calculate_normalized_power(self, power_stream: List[int]) -> Optional[int]:
        """
        Calculate Normalized Power (NP) using the Coggan algorithm.

        Steps:
          1. Compute a 30-second rolling average of power (1 sample/second assumed)
          2. Raise each rolling average to the 4th power
          3. Take the mean of those values
          4. Take the 4th root of that mean
        """
        if not power_stream or len(power_stream) < 30:
            return None

        window = 30
        window_sum = sum(power_stream[:window])
        rolling_avgs = [window_sum / window]

        for i in range(window, len(power_stream)):
            window_sum += power_stream[i] - power_stream[i - window]
            rolling_avgs.append(window_sum / window)

        mean_fourth = sum(v ** 4 for v in rolling_avgs) / len(rolling_avgs)
        return round(mean_fourth ** 0.25)

    def calculate_trimp(self, hr_stream: List[int], time_stream: List[int],
                        hr_rest: int = 60, sex: str = 'male',
                        hr_max: Optional[float] = None) -> Optional[float]:
        """
        Calculate Training Impulse (TRIMP) using Bannister's formula.

        TRIMP = Σ [ Δt(min) × HRr × e^(b × HRr) ]

        Where:
          HRr = (HR_exercise - HR_rest) / (HR_max - HR_rest)
          b   = 1.92 for males, 1.67 for females

        When hr_max is not given, it is estimated as Zone 5 lower bound
        / 0.90, since Zone 5 typically begins at approximately 90% of
        maximum heart rate.
        """
        if not hr_stream or not time_stream:
            return None

        if hr_max is None:
            # Estimate HR_max from zones: Zone 5 lower bound / 0.90
            if len(self.hr_zones) >= 5:
                zone5_lower = self.hr_zones[4][0]
                hr_max = round(zone5_lower / 0.90)
            else:
                hr_max = max(hr_stream)

        b = 1.92 if sex == 'male' else 1.67
        trimp = 0.0

        for i in range(len(hr_stream)):
            hr = hr_stream[i]
            if hr <= hr_rest:
                continue

            # Duration of this sample in minutes
            if i < len(time_stream) - 1:
                dt_minutes = (time_stream[i + 1] - time_stream[i]) / 60.0
            else:
                dt_minutes = 1.0 / 60.0

            hr_ratio = (hr - hr_rest) / (hr_max - hr_rest)
            hr_ratio = max(0.0, min(1.0, hr_ratio))  # clamp to [0, 1]

            trimp += dt_minutes * hr_ratio * math.exp(b * hr_ratio)

        return round(trimp)

    def format_time(self, seconds: float) -> str:
        """Format seconds into HH:MM:SS or MM:SS format."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"
