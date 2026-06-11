# Quick Setup Guide

## Step 1: Install Dependencies

```bash
cd /Users/collin/Projects/strava-client
pip install requests
```

## Step 2: Create Your Config File

Create a file named `config.json` in this directory with your Strava OAuth credentials:

```json
{
  "client_id": "YOUR_CLIENT_ID",
  "client_secret": "YOUR_CLIENT_SECRET",
  "access_token": "YOUR_ACCESS_TOKEN",
  "refresh_token": "YOUR_REFRESH_TOKEN",
  "expires_at": 0
}
```

### Where to Find Your Credentials

1. **Client ID and Client Secret**:
   - Go to https://www.strava.com/settings/api
   - You should see your application with Client ID and Client Secret

2. **Access Token and Refresh Token**:
   - If you already have these from a previous OAuth flow, use them
   - The `expires_at` field will be automatically updated by the script
   - Set `expires_at` to `0` initially to force a token refresh on first run

## Step 3: Run the Script

```bash
python main.py
```

The script will:
1. Check if your access token is expired
2. Automatically refresh it if needed (and update config.json)
3. Fetch your latest activity
4. Analyze and display the results

## Troubleshooting

**Error: "Config file not found"**
- Make sure you created `config.json` (not `config.json.example`)
- Check that you're in the correct directory

**Error: "401 Unauthorized"**
- Your tokens may be invalid
- Make sure you copied the correct tokens from Strava
- Try setting `expires_at` to `0` to force a refresh

**Error: "No activities found"**
- Make sure you have at least one activity on Strava
- Check that your OAuth tokens have the correct permissions (at least `activity:read_all`)

**No power or heart rate data**
- The script will skip zone analysis if the data isn't available
- Make sure your activity was recorded with a power meter and/or heart rate monitor
