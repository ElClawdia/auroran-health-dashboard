#!/usr/bin/env python3
import requests
import time
import json
import os

# ────────────────────────────────────────
#  CONFIG – fill these once
CLIENT_ID      = "STRAVA_CLIENT_ID"
CLIENT_SECRET  = os.getenv("STRAVA_CLIENT_SECRET")          # never commit this!
TOKEN_FILE     = "strava_tokens.json"                       # where we store tokens
# ────────────────────────────────────────

def load_tokens():
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError("Run manual authorization first to create tokens file")
    with open(TOKEN_FILE, "r") as f:
        return json.load(f)

def save_tokens(tokens):
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)

def refresh_if_needed():
    tokens = load_tokens()

    # Refresh if expired (or will expire soon, e.g. < 10 min left)
    if tokens.get("expires_at", 0) < time.time() + 600:
        print("Access token expired or near expiry → refreshing...")
        r = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            }
        )
        r.raise_for_status()
        new_tokens = r.json()

        # Update our stored data (always use the newest refresh_token!)
        tokens.update({
            "access_token": new_tokens["access_token"],
            "refresh_token": new_tokens["refresh_token"],  # IMPORTANT: save this new one
            "expires_at": new_tokens["expires_at"],
        })
        save_tokens(tokens)
        print("Refreshed successfully")

    return tokens["access_token"]

# ────────────────────────────────────────
# Example: use it
access_token = refresh_if_needed()

headers = {"Authorization": f"Bearer {access_token}"}
r = requests.get("https://www.strava.com/api/v3/athlete/activities?per_page=3", headers=headers)
print(r.json())
# ────────────────────────────────────────
