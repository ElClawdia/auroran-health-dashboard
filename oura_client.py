#!/usr/bin/env python3
"""
Oura API v2 client.

Uses OAuth2 authorization-code flow and stores access/refresh tokens in a
local JSON file. Do not commit token files or client secrets.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests


API_BASE_URL = "https://api.ouraring.com/v2/usercollection"
AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"


class OuraClient:
    """Small OAuth2-aware Oura API v2 client."""

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

    @property
    def is_configured(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    def authorization_url(
        self,
        redirect_uri: str,
        scopes: str = "daily personal",
        state: str | None = None,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
        }
        if state:
            params["state"] = state
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def load_tokens_from_file(self) -> bool:
        if not self.token_file or not self.token_file.exists():
            return False
        try:
            with self.token_file.open() as f:
                data = json.load(f)
            self.access_token = data.get("access_token", "")
            self.refresh_token = data.get("refresh_token", "")
            return self.is_configured
        except Exception:
            return False

    def save_tokens(self, token_payload: dict[str, Any]) -> None:
        if not self.token_file:
            return
        expires_in = int(token_payload.get("expires_in", 86400))
        data = {
            "access_token": token_payload["access_token"],
            "refresh_token": token_payload.get("refresh_token", self.refresh_token),
            "expires_at": int(time.time()) + expires_in,
            "token_type": token_payload.get("token_type", "bearer"),
            "scope": token_payload.get("scope", ""),
        }
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        with self.token_file.open("w") as f:
            json.dump(data, f, indent=2)
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]

    def token_expiring(self, buffer_seconds: int = 300) -> bool:
        if not self.token_file or not self.token_file.exists():
            return not bool(self.access_token)
        try:
            with self.token_file.open() as f:
                data = json.load(f)
            return int(data.get("expires_at", 0)) < int(time.time()) + buffer_seconds
        except Exception:
            return True

    def exchange_code(self, code: str, redirect_uri: str) -> bool:
        if not all([self.client_id, self.client_secret, code, redirect_uri]):
            return False
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri,
            },
            timeout=20,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Oura token exchange failed ({response.status_code}): {response.text}")
        self.save_tokens(response.json())
        return True

    def refresh_access_token(self) -> bool:
        if not all([self.client_id, self.client_secret, self.refresh_token]):
            return False
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=20,
        )
        if response.status_code != 200:
            return False
        self.save_tokens(response.json())
        return True

    def ensure_access_token(self) -> bool:
        if not self.access_token:
            self.load_tokens_from_file()
        if self.token_expiring():
            return self.refresh_access_token()
        return bool(self.access_token)

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self.ensure_access_token():
            return None
        response = requests.get(
            f"{API_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
            params=params,
            timeout=30,
        )
        if response.status_code == 401 and self.refresh_access_token():
            response = requests.get(
                f"{API_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
                params=params,
                timeout=30,
            )
        if response.status_code != 200:
            raise RuntimeError(f"Oura API request failed ({response.status_code}) {path}: {response.text}")
        return response.json()

    def _list_documents(self, path: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
        while True:
            payload = self._request(path, params=params)
            if not payload:
                break
            documents.extend(payload.get("data", []))
            next_token = payload.get("next_token")
            if not next_token:
                break
            params = {"next_token": next_token}
        return documents

    def get_daily_activity(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._list_documents("/daily_activity", start_date, end_date)

    def get_daily_sleep(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._list_documents("/daily_sleep", start_date, end_date)

    def get_sleep(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._list_documents("/sleep", start_date, end_date)

    def get_daily_readiness(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._list_documents("/daily_readiness", start_date, end_date)

    def get_personal_info(self) -> dict[str, Any] | None:
        return self._request("/personal_info")
