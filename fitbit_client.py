#!/usr/bin/env python3
"""
Fitbit API Client
OAuth 2.0 + REST API for weight, steps, sleep, heart rate.
"""

import base64
import json
import time
from pathlib import Path

import requests

BASE_URL = "https://api.fitbit.com"
TOKEN_URL = f"{BASE_URL}/oauth2/token"


class FitbitClient:
    """Client for Fitbit Web API with token refresh."""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        access_token: str = "",
        refresh_token: str = "",
        token_file: Path | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_file = token_file
        self._user_id = None

    @property
    def is_configured(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    def load_tokens_from_file(self) -> bool:
        """Load tokens from token_file (fitbit_tokens.json)."""
        if not self.token_file or not self.token_file.exists():
            return False
        try:
            with open(self.token_file) as f:
                data = json.load(f)
            self.access_token = data.get("access_token", "")
            self.refresh_token = data.get("refresh_token", "")
            return bool(self.access_token or self.refresh_token)
        except Exception:
            return False

    def save_tokens(self, access_token: str, refresh_token: str, expires_in: int):
        """Save tokens to file. Fitbit issues new refresh_token on each refresh."""
        if not self.token_file:
            return
        expires_at = int(time.time()) + expires_in
        data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.token_file, "w") as f:
            json.dump(data, f, indent=2)

    def refresh_access_token(self) -> bool:
        """Refresh access token. Fitbit returns NEW refresh_token - must save."""
        if not all([self.client_id, self.client_secret, self.refresh_token]):
            return False
        auth = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        try:
            r = requests.post(
                TOKEN_URL,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "expires_in": 28800,
                },
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                self.access_token = data["access_token"]
                self.refresh_token = data["refresh_token"]
                self.save_tokens(
                    data["access_token"],
                    data["refresh_token"],
                    data.get("expires_in", 28800),
                )
                return True
        except Exception:
            pass
        return False

    def _request(self, path: str, params: dict | None = None) -> dict | None:
        """Make authenticated GET request. Auto-refresh on 401."""
        if not self.access_token:
            if not self.refresh_access_token():
                return None
        url = f"{BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Accept-Language": "en_DE",  # metric (kg); en_GB returns stones
        }
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401 and self.refresh_access_token():
                headers["Authorization"] = f"Bearer {self.access_token}"
                r = requests.get(url, headers=headers, params=params, timeout=30)
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return None

    def get_weight(self, end_date: str, period: str = "30d") -> list[dict]:
        """Get weight logs. period: 1d, 7d, 30d."""
        data = self._request(
            f"/1/user/-/body/log/weight/date/{end_date}/{period}.json"
        )
        if data and "weight" in data:
            return data["weight"]
        return []

    def get_weight_range(self, start_date: str, end_date: str) -> list[dict]:
        """Get weight logs for date range. Max 31 days per request."""
        data = self._request(
            f"/1/user/-/body/log/weight/date/{start_date}/{end_date}.json"
        )
        if data and "weight" in data:
            return data["weight"]
        return []

    def get_steps(self, date: str) -> int | None:
        """Get total steps for date. Returns int or None."""
        result = self.get_steps_range(date, date)
        return result.get(date) if result else None

    def get_steps_range(self, start_date: str, end_date: str) -> dict[str, int]:
        """Get steps for date range. Max 1095 days per request. Returns {date: steps}."""
        data = self._request(
            f"/1/user/-/activities/steps/date/{start_date}/{end_date}.json"
        )
        out: dict[str, int] = {}
        if data and "activities-steps" in data:
            for entry in data["activities-steps"]:
                dt = entry.get("dateTime")
                if dt and start_date <= dt <= end_date:
                    try:
                        out[dt] = int(entry.get("value", 0))
                    except (ValueError, TypeError):
                        pass
        return out

    def get_sleep(self, date: str) -> dict | None:
        """Get sleep summary for date. Returns sleep duration in minutes or None."""
        result = self.get_sleep_range(date, date)
        return result.get(date) if result else None

    def get_sleep_range(self, start_date: str, end_date: str) -> dict[str, float]:
        """Get sleep minutes by date. Max 100 days per request. Returns {date: hours}."""
        data = self._request(
            f"/1.2/user/-/sleep/date/{start_date}/{end_date}.json"
        )
        out: dict[str, float] = {}
        if not data or "sleep" not in data:
            return out
        for entry in data["sleep"]:
            if not entry.get("isMainSleep"):
                continue
            ds = entry.get("dateOfSleep", "")
            if not ds.startswith("20"):  # yyyy-mm-dd
                continue
            dt = ds[:10]
            if start_date <= dt <= end_date:
                mins = int(entry.get("minutesAsleep", 0))
                if mins:
                    out[dt] = out.get(dt, 0) + mins / 60.0
        return out

    def get_resting_hr(self, date: str) -> float | None:
        """Get resting heart rate for date."""
        result = self.get_resting_hr_range(date, date)
        return result.get(date) if result else None

    def get_resting_hr_range(self, start_date: str, end_date: str) -> dict[str, float]:
        """Get resting HR by date. Max 1 year per request. Returns {date: resting_hr}."""
        data = self._request(
            f"/1/user/-/activities/heart/date/{start_date}/{end_date}.json"
        )
        out: dict[str, float] = {}
        if not data:
            return out
        for entry in data.get("activities-heart", []):
            dt = entry.get("dateTime")
            if dt and start_date <= dt <= end_date:
                resting = entry.get("value", {}).get("restingHeartRate")
                if resting is not None:
                    out[dt] = float(resting)
        return out
