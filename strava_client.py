import json
import time
import requests
from datetime import datetime
from typing import Dict, List, Optional


class StravaClient:
    """Client for interacting with the Strava API v3."""

    BASE_URL = "https://www.strava.com/api/v3"
    TOKEN_URL = "https://www.strava.com/oauth/token"

    def __init__(self, config_path: str = "config.json"):
        """Initialize the Strava client with config file path."""
        self.config_path = config_path
        self.config = self._load_config()
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

    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """Make a GET request to the Strava API."""
        url = f"{self.BASE_URL}{endpoint}"
        response = requests.get(url, headers=self._get_headers(), params=params)

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
