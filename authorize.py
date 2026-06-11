#!/usr/bin/env python3
"""
OAuth Authorization Helper for Strava

This script helps you authorize your application and get OAuth tokens
with the correct scopes (read, activity:read_all, profile:read_all).
"""

import json
import requests
import webbrowser
from urllib.parse import urlencode


def load_config():
    """Load existing config to get client ID and secret."""
    try:
        with open('config.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print("config.json not found. Let's create one.")
        return {}


def get_client_credentials(config):
    """Get or prompt for client ID and secret."""
    client_id = config.get('client_id')
    client_secret = config.get('client_secret')

    if not client_id:
        print("\nYou can find your Client ID and Client Secret at:")
        print("https://www.strava.com/settings/api")
        print()
        client_id = input("Enter your Client ID: ").strip()

    if not client_secret:
        client_secret = input("Enter your Client Secret: ").strip()

    return client_id, client_secret


def build_authorization_url(client_id, redirect_uri):
    """Build the OAuth authorization URL with correct scopes."""
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'approval_prompt': 'force',  # Force re-authorization to update scopes
        'scope': 'read,activity:read_all,profile:read_all'
    }
    return f"https://www.strava.com/oauth/authorize?{urlencode(params)}"


def exchange_code_for_tokens(client_id, client_secret, code):
    """Exchange authorization code for access and refresh tokens."""
    payload = {
        'client_id': client_id,
        'client_secret': client_secret,
        'code': code,
        'grant_type': 'authorization_code'
    }

    response = requests.post('https://www.strava.com/oauth/token', data=payload)

    if response.status_code != 200:
        print(f"\nError getting tokens: {response.status_code}")
        print(response.text)
        return None

    return response.json()


def save_config(client_id, client_secret, token_data):
    """Save configuration with new tokens."""
    config = {
        'client_id': client_id,
        'client_secret': client_secret,
        'access_token': token_data['access_token'],
        'refresh_token': token_data['refresh_token'],
        'expires_at': token_data['expires_at']
    }

    with open('config.json', 'w') as f:
        json.dump(config, f, indent=2)

    print("\nConfiguration saved to config.json")


def main():
    """Main authorization flow."""
    print("=" * 60)
    print("Strava OAuth Authorization Helper")
    print("=" * 60)

    # Load existing config
    config = load_config()

    # Get client credentials
    client_id, client_secret = get_client_credentials(config)

    # Default redirect URI (this is what Strava uses for localhost apps)
    redirect_uri = "http://localhost"

    print(f"\nUsing redirect URI: {redirect_uri}")
    print("\nMake sure this redirect URI is set in your Strava API application settings:")
    print("https://www.strava.com/settings/api")
    print()

    # Build authorization URL
    auth_url = build_authorization_url(client_id, redirect_uri)

    print("Step 1: Opening authorization URL in your browser...")
    print(f"\nIf the browser doesn't open automatically, visit this URL:")
    print(f"{auth_url}\n")

    # Try to open browser
    try:
        webbrowser.open(auth_url)
    except:
        pass

    print("\nStep 2: After authorizing, you'll be redirected to a URL like:")
    print("http://localhost/?state=&code=XXXXXXXXXXXXX&scope=read,activity:read_all,profile:read_all")
    print()

    # Get authorization code from user
    redirect_url = input("Paste the entire redirect URL here: ").strip()

    # Extract code from URL
    if 'code=' in redirect_url:
        code = redirect_url.split('code=')[1].split('&')[0]
    else:
        print("\nError: Could not find authorization code in URL")
        return

    print(f"\nStep 3: Exchanging authorization code for tokens...")

    # Exchange code for tokens
    token_data = exchange_code_for_tokens(client_id, client_secret, code)

    if not token_data:
        print("\nFailed to get tokens. Please try again.")
        return

    # Verify scopes
    athlete = token_data.get('athlete', {})
    print(f"\nAuthorized for athlete: {athlete.get('firstname')} {athlete.get('lastname')}")

    # Save configuration
    save_config(client_id, client_secret, token_data)

    print("\n" + "=" * 60)
    print("Authorization complete!")
    print("=" * 60)
    print("\nYou can now run the analyzer:")
    print("  python main.py")


if __name__ == "__main__":
    main()
