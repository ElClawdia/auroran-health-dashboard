#!/usr/bin/env python3
"""
Strava Token Renewer – All secrets from secrets.json
- Loads config from secrets.json
- Refreshes access_token if needed
- Saves updated tokens (including new refresh_token)
- Prints only the fresh access_token to stdout
"""

import json
import time
import os
import sys
import requests
from pathlib import Path

# ────────────────────────────────────────────────
#                CONFIG FILE
# ────────────────────────────────────────────────
SECRETS_FILE = Path("secrets.json")

def load_secrets() -> dict:
    if not SECRETS_FILE.is_file():
        print(f"Error: {SECRETS_FILE} not found", file=sys.stderr)
        print("Create it with this structure:", file=sys.stderr)
        print('''{
  "client_id": "17398",
  "client_secret": "your-real-client-secret-here",
  "token_file": "strava_tokens.json",
  "refresh_threshold_seconds": 600
}''', file=sys.stderr)
        sys.exit(1)

    try:
        with SECRETS_FILE.open("r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {SECRETS_FILE}: {e}", file=sys.stderr)
        sys.exit(1)

    required = {"client_id", "client_secret", "token_file"}
    missing = required - set(data.keys())
    if missing:
        print(f"Error: Missing keys in {SECRETS_FILE}: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    return data


# Load config once
secrets = load_secrets()

CLIENT_ID       = os.getenv("STRAVA_CLIENT_ID", secrets["client_id"])
CLIENT_SECRET   = os.getenv("STRAVA_CLIENT_SECRET", secrets["client_secret"])
TOKEN_FILE      = Path(secrets["token_file"])
REFRESH_THRESHOLD = secrets.get("refresh_threshold_seconds", 600)


def load_tokens() -> dict:
    if not TOKEN_FILE.is_file():
        print(f"Error: Token file not found: {TOKEN_FILE}", file=sys.stderr)
        print("Create it first with your initial tokens:", file=sys.stderr)
        print('''{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1771330760
}''', file=sys.stderr)
        sys.exit(1)

    with TOKEN_FILE.open("r") as f:
        return json.load(f)


def save_tokens(tokens: dict):
    with TOKEN_FILE.open("w") as f:
        json.dump(tokens, f, indent=2)
    # Restrict permissions to current user only
    os.chmod(TOKEN_FILE, 0o600)


def get_fresh_access_token() -> str:
    tokens = load_tokens()

    now = time.time()
    expires_at = tokens.get("expires_at", 0)

    if expires_at < now + REFRESH_THRESHOLD:
        print(f"Token expires at {time.ctime(expires_at)} → refreshing...", file=sys.stderr)

        try:
            response = requests.post(
                "https://www.strava.com/oauth/token",
                data={
                    "client_id":     CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type":    "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                },
                timeout=15,
            )

            response.raise_for_status()
            new_tokens = response.json()

            # Update and save (important: new refresh_token!)
            tokens.update({
                "access_token":  new_tokens["access_token"],
                "refresh_token": new_tokens["refresh_token"],
                "expires_at":    new_tokens["expires_at"],
            })

            save_tokens(tokens)
            print("Token refreshed and saved", file=sys.stderr)

        except requests.exceptions.RequestException as e:
            print(f"Refresh failed: {e}", file=sys.stderr)
            if hasattr(e.response, 'text'):
                print(e.response.text, file=sys.stderr)
            sys.exit(1)

    else:
        remaining = int(expires_at - now)
        print(f"Token still valid ({remaining//60} min left)", file=sys.stderr)

    return tokens["access_token"]


if __name__ == "__main__":
    try:
        token = get_fresh_access_token()
        print(token)  # Only the token → stdout
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)
