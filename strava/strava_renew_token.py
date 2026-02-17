#!/usr/bin/env python3
"""
Strava Token Renewer
- Loads tokens from strava_tokens.json
- Loads client credentials from config.py
- Refreshes access_token if expired or near expiry
- Always saves the new refresh_token (Strava rotates it!)
- Prints only the fresh access_token
"""

import sys
import time
import requests
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET

# Path to tokens file (relative to project root)
TOKEN_FILE = Path(__file__).parent / "strava_tokens.json"

# How many seconds before expiry to refresh (e.g. 10 minutes)
REFRESH_THRESHOLD = 600


def load_tokens() -> dict:
    if not TOKEN_FILE.is_file():
        print(f"Error: {TOKEN_FILE} not found")
        exit(1)
    
    with TOKEN_FILE.open("r") as f:
        return json.load(f)


def save_tokens(tokens: dict):
    with TOKEN_FILE.open("w") as f:
        json.dump(tokens, f, indent=2)


def get_fresh_access_token() -> str:
    tokens = load_tokens()
    
    now = time.time()
    expires_at = tokens.get("expires_at", 0)
    
    if expires_at < now + REFRESH_THRESHOLD:
        print(f"Token expires at {time.ctime(expires_at)} → refreshing...", file=sys.stderr)
        
        response = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id":     STRAVA_CLIENT_ID,
                "client_secret": STRAVA_CLIENT_SECRET,
                "grant_type":    "refresh_token",
                "refresh_token": tokens["refresh_token"],
            },
            timeout=15,
        )
        
        if response.status_code != 200:
            print("Refresh failed!", file=sys.stderr)
            print(response.text, file=sys.stderr)
            exit(1)
        
        new_tokens = response.json()
        
        # Update stored tokens (VERY IMPORTANT: save new refresh_token!)
        tokens.update({
            "access_token":  new_tokens["access_token"],
            "refresh_token": new_tokens["refresh_token"],
            "expires_at":    new_tokens["expires_at"],
        })
        
        save_tokens(tokens)
        print("Token refreshed and saved", file=sys.stderr)
    
    else:
        remaining = int(expires_at - now)
        print(f"Token still valid ({remaining//60} min left)", file=sys.stderr)
    
    return tokens["access_token"]


if __name__ == "__main__":
    try:
        import json
        token = get_fresh_access_token()
        print(token)               # ← only the token is printed to stdout
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        exit(1)
