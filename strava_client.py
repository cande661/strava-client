import json
import time
import requests
from datetime import datetime
from typing import Dict, List, Optional


class RateLimitError(Exception):
    """Raised on HTTP 429. daily=True means the daily quota is exhausted
    (retrying after the next 15-minute window won't help)."""

    def __init__(self, message: str, daily: bool = False):
        super().__init__(message)
        self.daily = daily


class StravaClient:
    """Client for interacting with the Strava API v3."""

    BASE_URL = "https://www.strava.com/api/v3"
    TOKEN_URL = "https://www.strava.com/oauth/token"

    def __init__(self, config_path: str = "config.json"):
        """Initialize the Strava client with config file path."""
        self.config_path = config_path
        self.config = self._load_config()
        # Read-quota usage from the most recent response headers:
        # {'short_usage', 'short_limit', 'daily_usage', 'daily_limit'}
        self.rate_limit: Optional[Dict[str, int]] = None
        self._ensure_valid_token()

    def _load_config(self) -> Dict:
        """Load configuration from JSON file."""
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Config file not found at {self.config_path}. "
                "Please create it using config.json.example as a template."
            )
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in config file: {e}")

    def _save_config(self):
        """Save configuration back to JSON file."""
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2)

    def _ensure_valid_token(self):
        """Check if access token is valid and refresh if needed."""
        current_time = int(time.time())
        expires_at_value = self.config.get('expires_at', 0)

        # Handle both Unix timestamp (int) and ISO 8601 date string formats
        if isinstance(expires_at_value, str):
            try:
                # Try to parse as ISO 8601 date string
                expires_at = int(datetime.fromisoformat(expires_at_value.replace('Z', '+00:00')).timestamp())
            except (ValueError, AttributeError):
                # If parsing fails, assume expired and refresh
                expires_at = 0
        else:
            expires_at = int(expires_at_value)

        if current_time >= expires_at:
            print("Access token expired, refreshing...")
            self._refresh_token()

    def _refresh_token(self):
        """Refresh the access token using the refresh token."""
        payload = {
            'client_id': self.config['client_id'],
            'client_secret': self.config['client_secret'],
            'refresh_token': self.config['refresh_token'],
            'grant_type': 'refresh_token'
        }

        response = requests.post(self.TOKEN_URL, data=payload)

        if response.status_code != 200:
            print(f"Token refresh failed with status {response.status_code}")
            print(f"Response: {response.text}")
            response.raise_for_status()

        token_data = response.json()

        self.config['access_token'] = token_data['access_token']
        self.config['refresh_token'] = token_data['refresh_token']
        self.config['expires_at'] = token_data['expires_at']

        self._save_config()
        print("Access token refreshed successfully")

    def _get_headers(self) -> Dict[str, str]:
        """Get authorization headers for API requests."""
        return {
            'Authorization': f"Bearer {self.config['access_token']}"
        }

    def _update_rate_limit(self, response):
        """Capture read-quota usage from response headers when present."""
        limit = (response.headers.get('X-ReadRateLimit-Limit')
                 or response.headers.get('X-RateLimit-Limit'))
        usage = (response.headers.get('X-ReadRateLimit-Usage')
                 or response.headers.get('X-RateLimit-Usage'))
        if limit and usage:
            try:
                short_limit, daily_limit = (int(x) for x in limit.split(','))
                short_usage, daily_usage = (int(x) for x in usage.split(','))
            except ValueError:
                return
            self.rate_limit = {
                'short_usage': short_usage, 'short_limit': short_limit,
                'daily_usage': daily_usage, 'daily_limit': daily_limit,
            }

    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """Make a GET request to the Strava API."""
        url = f"{self.BASE_URL}{endpoint}"
        response = requests.get(url, headers=self._get_headers(), params=params)
        self._update_rate_limit(response)

        if response.status_code == 429:
            rl = self.rate_limit or {}
            daily = rl.get('daily_usage', 0) >= rl.get('daily_limit', 1)
            raise RateLimitError(
                f"Rate limited on {endpoint} (usage: {rl})", daily=daily)

        # If we get a 401, try refreshing the token once and retry
        if response.status_code == 401:
            print("Received 401 Unauthorized, refreshing token...")
            self._refresh_token()
            response = requests.get(url, headers=self._get_headers(), params=params)

            if response.status_code == 401:
                print(f"Still getting 401 after refresh. Response: {response.text}")
                print("This might be a scope issue. Make sure your OAuth token has 'activity:read_all' scope.")

        response.raise_for_status()
        return response.json()

    def get_athlete_zones(self) -> Dict:
        """Get athlete's heart rate and power zones."""
        return self._make_request("/athlete/zones")

    def get_latest_activity(self) -> Dict:
        """Get the most recent activity."""
        activities = self._make_request("/athlete/activities", params={'per_page': 1})
        if not activities:
            raise ValueError("No activities found")
        return activities[0]

    def get_activity(self, activity_id: int) -> Dict:
        """Get a specific activity by ID."""
        return self._make_request(f"/activities/{activity_id}")

    def get_activity_streams(self, activity_id: int, stream_types: List[str]) -> Dict:
        """
        Get activity streams (e.g., time, heartrate, watts).

        Args:
            activity_id: The ID of the activity
            stream_types: List of stream types to fetch (e.g., ['time', 'heartrate', 'watts'])
        """
        stream_keys = ','.join(stream_types)
        endpoint = f"/activities/{activity_id}/streams"
        params = {
            'keys': stream_keys,
            'key_by_type': 'true'
        }
        return self._make_request(endpoint, params=params)

    def get_activity_laps(self, activity_id: int) -> List[Dict]:
        """Get lap data for an activity."""
        return self._make_request(f"/activities/{activity_id}/laps")

    def list_activities(self, page: int = 1, per_page: int = 200,
                        after: Optional[int] = None,
                        before: Optional[int] = None) -> List[Dict]:
        """List athlete activities (summary representation).

        Args:
            page: 1-based page number
            per_page: up to 200
            after: only activities started after this epoch timestamp
            before: only activities started before this epoch timestamp
        """
        params = {'page': page, 'per_page': per_page}
        if after is not None:
            params['after'] = int(after)
        if before is not None:
            params['before'] = int(before)
        return self._make_request("/athlete/activities", params=params)

    def get_athlete(self) -> Dict:
        """Get the authenticated athlete, including bikes and shoes."""
        return self._make_request("/athlete")

    def get_gear(self, gear_id: str) -> Dict:
        """Get detailed gear info by ID."""
        return self._make_request(f"/gear/{gear_id}")
