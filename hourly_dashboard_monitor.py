#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import requests
from flask.sessions import SecureCookieSessionInterface

from app import app
from auth import load_users


BASE_URL = os.getenv("DASHBOARD_MONITOR_BASE_URL", "http://127.0.0.1:8512")
LOG_FILE = Path(__file__).parent / "logs" / "dashboard-monitor.log"
LOCK_FILE = Path("/tmp/auroran-dashboard-monitor.lock")
APP_DIR = Path(__file__).parent
PYTHON_BIN = "/home/tav/.pyenv/versions/3.12.3/bin/python3"
TMUX_SESSION = "health-dashboard"

PMCSLOW_SECONDS = float(os.getenv("PMC_SLOW_SECONDS", "1.5"))
CHARTSLOW_SECONDS = float(os.getenv("CHARTS_SLOW_SECONDS", "2.5"))
QUICKSLOW_SECONDS = float(os.getenv("QUICK_SLOW_SECONDS", "8.0"))


def log_event(payload: dict):
    LOG_FILE.parent.mkdir(exist_ok=True)
    payload["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


@contextmanager
def monitor_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        log_event({"level": "info", "event": "skip", "reason": "lock_exists"})
        sys.exit(0)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def build_session_cookie() -> dict[str, str]:
    username = next(iter(load_users().keys()))
    serializer = SecureCookieSessionInterface().get_signing_serializer(app)
    cookie_value = serializer.dumps({"user": username})
    cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    return {cookie_name: cookie_value}


def fetch(session: requests.Session, path: str, timeout: float = 20.0) -> dict:
    started = time.perf_counter()
    response = session.get(f"{BASE_URL}{path}", timeout=timeout)
    elapsed = round(time.perf_counter() - started, 3)
    payload: dict = {
        "path": path,
        "status": response.status_code,
        "seconds": elapsed,
    }
    try:
        payload["json"] = response.json()
    except Exception:
        payload["text"] = response.text[:500]
    return payload


def restart_service():
    subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
    time.sleep(1)
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            TMUX_SESSION,
            f"cd {APP_DIR} && {PYTHON_BIN} ./app.py",
        ],
        check=False,
    )
    time.sleep(3)


def run_checks() -> dict:
    session = requests.Session()
    session.cookies.update(build_session_cookie())
    today = datetime.now().strftime("%Y-%m-%d")

    results = []
    paths = [
        f"/api/pmc?days=42&end_date={today}",
        f"/api/dashboard/charts?date={today}&days=10",
        f"/api/dashboard/quick?date={today}",
    ]
    for path in paths:
        try:
            results.append(fetch(session, path))
        except Exception as e:
            results.append({"path": path, "status": 0, "seconds": None, "error": repr(e)})

    summary = {
        "level": "info",
        "event": "dashboard_check",
        "results": results,
    }

    slow = []
    for item in results:
        secs = item.get("seconds")
        path = item["path"]
        if secs is None:
            continue
        if path.startswith("/api/pmc") and secs > PMCSLOW_SECONDS:
            slow.append({"path": path, "seconds": secs, "threshold": PMCSLOW_SECONDS})
        if path.startswith("/api/dashboard/charts") and secs > CHARTSLOW_SECONDS:
            slow.append({"path": path, "seconds": secs, "threshold": CHARTSLOW_SECONDS})
        if path.startswith("/api/dashboard/quick") and secs > QUICKSLOW_SECONDS:
            slow.append({"path": path, "seconds": secs, "threshold": QUICKSLOW_SECONDS})
    if slow:
        summary["level"] = "warning"
        summary["slow"] = slow

    return summary


def main():
    with monitor_lock():
        summary = run_checks()
        failures = [r for r in summary["results"] if r.get("status") != 200]
        if len(failures) == len(summary["results"]):
            log_event({
                "level": "error",
                "event": "dashboard_check_failed",
                "action": "restart",
                "results": summary["results"],
            })
            restart_service()
            retry_summary = run_checks()
            retry_summary["event"] = "dashboard_check_after_restart"
            log_event(retry_summary)
            return
        log_event(summary)


if __name__ == "__main__":
    main()
