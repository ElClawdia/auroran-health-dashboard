#!/usr/bin/env python3
"""
Health Dashboard - Flask Web Server
Auroran Health Command Center 🦞

Run: python app.py
Access: http://localhost:5000
"""

import os
import sys
import secrets
import logging
import threading
import json
from zoneinfo import ZoneInfo
from datetime import time as dt_time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from time import perf_counter
from typing import Dict, List
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, Response, send_from_directory
from werkzeug.utils import secure_filename
from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS
import pandas as pd

# Configure logging with timestamps
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Create logs directory
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_PROFILE_EXTS = {"png", "jpg", "jpeg", "webp"}
RECENT_WORKOUTS_CACHE_FILE = log_dir / "recent_workouts_cache.json"
PMC_DAILY_LOADS_CACHE_FILE = log_dir / "pmc_daily_loads_cache.json"
RECENT_WORKOUTS_CACHE_TTL_SECONDS = 300
PMC_DAILY_LOADS_CACHE_TTL_SECONDS = 900
ENABLE_INFLUX_WORKOUT_REFRESH = os.getenv("ENABLE_INFLUX_WORKOUT_REFRESH", "1") == "1"
_workout_read_measurement_env = os.getenv("WORKOUT_READ_MEASUREMENT", "workout_cache").strip()
WORKOUT_READ_MEASUREMENT = (
    "workout_cache"
    if _workout_read_measurement_env == "workout_cache_rebuilt"
    else _workout_read_measurement_env
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(log_dir / "health-dashboard.log", maxBytes=10*1024*1024, backupCount=5)
    ]
)
logger = logging.getLogger(__name__)

# Import our modules
from suunto_client import SuuntoClient
from strava_client import StravaClient, MockStravaClient
from planner import ExercisePlanner
from ai_analyzer import AIAnalyzer
from training_load import calculate_training_load, calculate_ctl_atl_tsb, calculate_pmc_series, get_status_description, reload_params
from auth import login_required, authenticate, get_current_user, update_user, get_user, hash_password
from formula_learning import load_params, run_learning_cycle
from email_service import generate_reset_token, consume_reset_token, send_password_reset_email

app = Flask(__name__)

# Configuration
from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET
from config import SUUNTO_CLIENT_ID, SUUNTO_CLIENT_SECRET
from config import STRAVA_ACCESS_TOKEN, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
from config import FLASK_HOST, FLASK_PORT, FLASK_DEBUG, FLASK_SECRET_KEY

# Use persistent secret key - generate and save if not configured
def get_or_create_secret_key():
    """Get secret key from config or generate and persist one"""
    if FLASK_SECRET_KEY:
        return FLASK_SECRET_KEY
    
    # Try to load from file
    secret_key_file = Path(__file__).parent / ".flask_secret_key"
    if secret_key_file.exists():
        return secret_key_file.read_text().strip()
    
    # Generate new key and save it
    new_key = secrets.token_hex(32)
    secret_key_file.write_text(new_key)
    logger.info("Generated new Flask secret key")
    return new_key

app.secret_key = get_or_create_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5MB upload limit

# Initialize InfluxDB client with fallback
influx_client = None
write_api = None
query_api = None

if INFLUXDB_TOKEN:
    try:
        influx_client = InfluxDBClient(
            url=INFLUXDB_URL,
            token=INFLUXDB_TOKEN,
            org=INFLUXDB_ORG,
            timeout=60_000,  # 60s for large workout history queries
        )
        # Quick health check
        health = influx_client.health()
        if health.status == "pass":
            write_api = influx_client.write_api(write_options=SYNCHRONOUS)
            query_api = influx_client.query_api()
            logger.info(f"Connected to InfluxDB at {INFLUXDB_URL}")
        else:
            logger.warning("InfluxDB health check failed, using demo mode")
            influx_client = None
    except Exception as e:
        logger.error(f"Could not connect to InfluxDB: {e}")
        logger.warning("Running in demo mode with mock data")
        influx_client = None

# Initialize modules
suunto = SuuntoClient(SUUNTO_CLIENT_ID, SUUNTO_CLIENT_SECRET)
strava = StravaClient(
    access_token=STRAVA_ACCESS_TOKEN,
    client_id=STRAVA_CLIENT_ID,
    client_secret=STRAVA_CLIENT_SECRET,
    refresh_token=STRAVA_REFRESH_TOKEN
) if (STRAVA_ACCESS_TOKEN or STRAVA_REFRESH_TOKEN or (Path(__file__).parent / "renew-strava-tokens" / "strava_tokens.json").exists()) else MockStravaClient()
planner = ExercisePlanner()
analyzer = AIAnalyzer()


_workout_index_preloaded = False

# Simple in-memory cache for workouts, PMC, weight, and dashboard
_workout_cache = {"data": None, "expires": None}
_recent_workouts_cache = {"data": None, "loaded_at": None, "loading": False}
_recent_workouts_lock = threading.Lock()
_pmc_cache = {"data": None, "expires": None}
_pmc_daily_loads_cache = {"data": None, "loaded_at": None, "source": None, "loading": False}
_pmc_daily_loads_lock = threading.Lock()
_weight_cache: dict[str, tuple[dict, datetime]] = {}  # (date -> (response, expires))
_dashboard_cache: dict[str, tuple[dict, datetime]] = {}  # (date -> (response, expires))
CACHE_TTL_SECONDS = 30
DASHBOARD_CACHE_TTL_SECONDS = 120
PMC_CACHE_TTL_SECONDS = 300
INFLUX_FAST_TIMEOUT_MS = 5000
WORKOUT_INDEX_TTL_SECONDS = 600  # 10 minutes
WORKOUT_INDEX_RANGE_DAYS = 42
_workout_index: dict[str, object] = {
    "data": None,        # list of workouts
    "loading": False,
    "loaded_at": None,
    "loading_started_at": None,
}
_workout_index_lock = threading.Lock()


def _log_perf(label: str, started_at: float, **fields):
    parts = [f"{k}={v}" for k, v in fields.items() if v is not None]
    suffix = f" {' '.join(parts)}" if parts else ""
    logger.info(f"{label} took {perf_counter() - started_at:.3f}s{suffix}")


OURA_PRIMARY_FIELDS = {
    "sleep_duration_hours",
    "deep_sleep_hours",
    "rem_sleep_hours",
    "light_sleep_hours",
    "awake_hours",
    "hrv_avg",
    "resting_hr",
    "avg_sleep_hr",
    "sleep_efficiency",
    "sleep_score",
    "recovery_score",
    "steps",
    "active_calories",
    "total_calories",
}


def _daily_health_source_rank(field: str, source: object) -> int:
    src = "" if pd.isna(source) else str(source).lower()
    if field == "weight":
        if src == "fitbit":
            return 0
        if src == "oura":
            return 3
        return 1
    if field in OURA_PRIMARY_FIELDS:
        if src == "oura":
            return 0
        if src == "fitbit":
            return 2
        return 1
    return 1


def _reduce_daily_health_by_source(df: pd.DataFrame, numeric_cols: list[str]) -> pd.DataFrame:
    """Return one row per date, preferring the intended provider for each metric."""
    if not numeric_cols:
        return df[["date"]].drop_duplicates()

    rows = []
    time_col = "_time" if "_time" in df.columns else None
    source_col = "source" if "source" in df.columns else None
    for date_value, group in df.groupby("date", sort=True):
        row = {"date": date_value}
        for field in numeric_cols:
            candidates = group[["date", field] + ([source_col] if source_col else []) + ([time_col] if time_col else [])].copy()
            candidates = candidates.dropna(subset=[field])
            if field in OURA_PRIMARY_FIELDS and source_col:
                candidates = candidates[candidates[source_col].fillna("").astype(str).str.lower() == "oura"]
            if candidates.empty:
                continue
            candidates["_source_rank"] = candidates[source_col].apply(lambda s: _daily_health_source_rank(field, s)) if source_col else 1
            if time_col:
                candidates = candidates.sort_values(["_source_rank", time_col], ascending=[True, False])
            else:
                candidates = candidates.sort_values(["_source_rank"])
            row[field] = candidates.iloc[0][field]
        rows.append(row)
    return pd.DataFrame(rows) if rows else pd.DataFrame({"date": []})


def _load_recent_workouts_cache_from_disk():
    with _recent_workouts_lock:
        if _recent_workouts_cache.get("data") is not None:
            return
        if RECENT_WORKOUTS_CACHE_FILE.exists():
            try:
                payload = json.loads(RECENT_WORKOUTS_CACHE_FILE.read_text())
                _recent_workouts_cache["data"] = payload.get("data", [])
                ts = payload.get("loaded_at")
                _recent_workouts_cache["loaded_at"] = datetime.fromisoformat(ts) if ts else None
            except Exception as e:
                logger.warning(f"Failed to load recent workouts cache: {e}")


def _save_recent_workouts_cache_to_disk(data: list[dict]):
    try:
        payload = {
            "loaded_at": datetime.now().isoformat(),
            "data": data,
        }
        RECENT_WORKOUTS_CACHE_FILE.write_text(json.dumps(payload))
    except Exception as e:
        logger.warning(f"Failed to save recent workouts cache: {e}")


def _load_pmc_daily_loads_cache_from_disk():
    with _pmc_daily_loads_lock:
        if _pmc_daily_loads_cache.get("data") is not None:
            return
        if PMC_DAILY_LOADS_CACHE_FILE.exists():
            try:
                payload = json.loads(PMC_DAILY_LOADS_CACHE_FILE.read_text())
                _pmc_daily_loads_cache["data"] = payload.get("data", [])
                _pmc_daily_loads_cache["source"] = payload.get("source")
                ts = payload.get("loaded_at")
                _pmc_daily_loads_cache["loaded_at"] = datetime.fromisoformat(ts) if ts else None
            except Exception as e:
                logger.warning(f"Failed to load PMC daily-load cache: {e}")


def _save_pmc_daily_loads_cache_to_disk(data: list[dict], source: str):
    try:
        payload = {
            "loaded_at": datetime.now().isoformat(),
            "source": source,
            "data": data,
        }
        PMC_DAILY_LOADS_CACHE_FILE.write_text(json.dumps(payload))
    except Exception as e:
        logger.warning(f"Failed to save PMC daily-load cache: {e}")


def _filter_daily_loads_window(daily_loads: list[dict], query_days: int, end_date: str | None = None) -> list[dict]:
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else datetime.now().date()
    except ValueError:
        end_dt = datetime.now().date()
    start_dt = end_dt - timedelta(days=query_days - 1)
    out = []
    for row in daily_loads:
        ds = row.get("date")
        if not ds or ds < start_dt.isoformat() or ds > end_dt.isoformat():
            continue
        try:
            out.append({"date": ds, "load": float(row.get("load", 0.0))})
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x["date"])


def _get_pmc_daily_loads_from_cache(query_days: int, end_date: str | None = None) -> tuple[list[dict], bool, str | None]:
    _load_pmc_daily_loads_cache_from_disk()
    with _pmc_daily_loads_lock:
        cached = list(_pmc_daily_loads_cache.get("data") or [])
        loaded_at = _pmc_daily_loads_cache.get("loaded_at")
        source = _pmc_daily_loads_cache.get("source")
    if not cached:
        return [], True, source
    filtered = _filter_daily_loads_window(cached, query_days, end_date)
    stale = not loaded_at or (datetime.now() - loaded_at).total_seconds() > PMC_DAILY_LOADS_CACHE_TTL_SECONDS
    return filtered, stale, source


def _store_pmc_daily_loads_cache(daily_loads: list[dict], source: str):
    daily_loads = sorted(
        [{"date": row["date"], "load": float(row.get("load", 0.0))} for row in daily_loads if row.get("date")],
        key=lambda x: x["date"],
    )
    with _pmc_daily_loads_lock:
        _pmc_daily_loads_cache["data"] = daily_loads
        _pmc_daily_loads_cache["loaded_at"] = datetime.now()
        _pmc_daily_loads_cache["source"] = source
    _save_pmc_daily_loads_cache_to_disk(daily_loads, source)


def _refresh_pmc_daily_loads_async(query_days: int = 365, end_date: str | None = None):
    if not getattr(strava, "is_configured", False):
        return
    with _pmc_daily_loads_lock:
        if _pmc_daily_loads_cache.get("loading"):
            return
        _pmc_daily_loads_cache["loading"] = True

    def _worker():
        try:
            loads = _fetch_daily_loads_from_strava(query_days, end_date)
            if loads:
                _store_pmc_daily_loads_cache(loads, "strava")
        except Exception as e:
            logger.warning(f"PMC daily-load cache refresh failed: {e}")
        finally:
            with _pmc_daily_loads_lock:
                _pmc_daily_loads_cache["loading"] = False

    threading.Thread(target=_worker, daemon=True).start()


def _refresh_recent_workouts_cache_async(before_date: str | None):
    if not ENABLE_INFLUX_WORKOUT_REFRESH:
        return
    with _recent_workouts_lock:
        if _recent_workouts_cache.get("loading"):
            return
        _recent_workouts_cache["loading"] = True

    def _worker():
        try:
            target = before_date or datetime.now().strftime("%Y-%m-%d")
            records = _fetch_workouts_recent_fast(target, 200)
            if not records or len(records) < 10:
                records = _fetch_workouts_limited(target, 200)
            with _recent_workouts_lock:
                _recent_workouts_cache["data"] = records
                _recent_workouts_cache["loaded_at"] = datetime.now()
        except Exception as e:
            logger.warning(f"Recent workouts cache refresh failed: {e}")
        finally:
            _save_recent_workouts_cache_to_disk(_recent_workouts_cache.get("data") or [])
            with _recent_workouts_lock:
                _recent_workouts_cache["loading"] = False

    threading.Thread(target=_worker, daemon=True).start()


def _fetch_workouts_recent_fast(before_date: str | None, limit: int) -> list[dict]:
    """Fast-path fetch from workout_cache using limited _time sort."""
    if not query_api:
        return []
    try:
        target_dt = datetime.strptime(before_date, "%Y-%m-%d") if before_date else datetime.now()
    except ValueError:
        target_dt = datetime.now()
    cutoff = (target_dt - timedelta(days=14)).strftime('%Y-%m-%d')
    date_filter = f'|> filter(fn: (r) => r.date >= "{cutoff}")'
    if before_date:
        date_filter = f'|> filter(fn: (r) => r.date >= "{cutoff}" and r.date <= "{before_date}")'

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {(target_dt - timedelta(days=14)).strftime("%Y-%m-%dT00:00:00Z")}, stop: {(target_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
      |> filter(fn: (r) => r._measurement == "{WORKOUT_READ_MEASUREMENT}")
      {date_filter}
    '''
    workouts = {}
    try:
        for record in query_api.query_stream(query):
            key = str(record.get_time())
            entry = workouts.setdefault(
                key,
                {"date": record.values.get("date", ""), "type": record.values.get("type", "")},
            )
            entry[record.get_field()] = record.get_value()
    except Exception:
        return []

    if not workouts:
        return []

    records = list(workouts.values())
    records = sorted(records, key=lambda x: (x.get("date", ""), x.get("start_time", "")), reverse=True)
    deduped = _dedupe_workouts(records)
    return deduped[:limit]


def _get_recent_workouts_from_cache(before_date: str | None, limit: int):
    _load_recent_workouts_cache_from_disk()
    with _recent_workouts_lock:
        data = _recent_workouts_cache.get("data") or []
        loaded_at = _recent_workouts_cache.get("loaded_at")
        loading = _recent_workouts_cache.get("loading")

    if not data:
        return None, False

    target = before_date or datetime.now().strftime("%Y-%m-%d")
    filtered = [w for w in data if w.get("date", "") <= target]
    filtered = sorted(filtered, key=lambda x: (x.get("date", ""), x.get("start_time", "")), reverse=True)
    stale = True
    if loaded_at and (datetime.now() - loaded_at).total_seconds() < RECENT_WORKOUTS_CACHE_TTL_SECONDS:
        stale = False
    max_date = max((w.get("date", "") for w in data), default="")
    # If cache doesn't cover the requested date range, keep refreshing
    if (not filtered) and max_date and target <= max_date:
        stale = True
    if len(filtered) < limit:
        stale = True
    return filtered[:limit], (stale or loading)


def _get_pmc_params_for_user(user: dict | None) -> dict:
    """Return PMC parameters per selected style.

    - strava: prioritize curve-shape match (canonical taus + lagged form display)
    - suunto/native: keep existing house styles for now
    """
    style = (user or {}).get("pmc_style", "native")
    if style == "suunto":
        return {
            "ctl_days": 70,
            "atl_days": 10,
            "load_scale_factor": 1.5,
            "tsb_lag_days": 0,
            "seed_mode": "zeros",
        }
    if style == "strava":
        return {
            "ctl_days": 42,
            "atl_days": 7,
            # Empirical calibration against current Strava anchor levels
            "load_scale_factor": 1.27,
            "tsb_lag_days": 0,
            "seed_mode": "rolling_avg",
        }
    return {
        "ctl_days": 55,
        "atl_days": 10,
        "load_scale_factor": 1.38,
        "tsb_lag_days": 0,
        "seed_mode": "zeros",
    }

# Dashboard lookback windows (keep small for speed)
WORKOUT_LOOKBACK_DAYS = 42
HEALTH_LOOKBACK_DAYS = 42
PMC_MIN_LOOKBACK_DAYS = 120
WEIGHT_LOOKBACK_DAYS = 42  # Never load more than 42 days at a time

# Generate mock data for demo mode
def get_mock_health_today():
    """Return realistic mock data for demo"""
    import random
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "sleep_hours": round(7.0 + random.random() * 1.5, 1),
        "hrv": random.randint(38, 48),
        "resting_hr": random.randint(54, 62),
        "steps": random.randint(5000, 12000),
        "recovery_score": random.randint(70, 95),
        "training_load": round(random.uniform(0.8, 1.4), 2),
        "trend": {
            "sleep": "+12m" if random.random() > 0.5 else "-5m",
            "hrv": "+5ms ▲" if random.random() > 0.5 else "-3ms ▼",
            "resting_hr": "-2bpm ▼" if random.random() > 0.5 else "+1bpm ▲"
        }
    }

def get_mock_history(days=30):
    """Return realistic mock history"""
    import random
    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days-1, -1, -1)]
    
    hrv = [35 + i + random.randint(-3, 5) for i in range(days)]
    resting_hr = [65 - i//3 + random.randint(-2, 2) for i in range(days)]
    sleep = [7 + (i % 5) * 0.2 + random.uniform(-0.3, 0.3) for i in range(days)]
    recovery = [60 + i + random.randint(-5, 10) for i in range(days)]
    
    return {
        "dates": dates,
        "hrv": [max(20, min(60, h)) for h in hrv],
        "resting_hr": [max(50, min(70, r)) for r in resting_hr],
        "sleep": [round(max(5, min(9, s)), 1) for s in sleep],
        "recovery": [max(30, min(100, r)) for r in recovery]
    }

def get_mock_workouts():
    """Return realistic mock workouts"""
    import random
    workouts = [
        {"date": "2026-02-15", "type": "Running", "duration": 35, "avg_hr": 145, "max_hr": 168, "feeling": "great"},
        {"date": "2026-02-14", "type": "Strength", "duration": 45, "avg_hr": 110, "max_hr": 135, "feeling": "good"},
        {"date": "2026-02-13", "type": "Rest", "duration": 0, "avg_hr": 62, "max_hr": 78, "feeling": "great"},
        {"date": "2026-02-12", "type": "Cycling", "duration": 60, "avg_hr": 128, "max_hr": 155, "feeling": "good"},
        {"date": "2026-02-11", "type": "HIIT", "duration": 25, "avg_hr": 155, "max_hr": 175, "feeling": "okay"},
        {"date": "2026-02-10", "type": "Running", "duration": 40, "avg_hr": 142, "max_hr": 165, "feeling": "great"},
    ]
    return workouts


@app.route('/')
@login_required
def index():
    """Main dashboard page"""
    user = get_current_user()
    return render_template('index.html', user=user)


@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route('/favicon.svg')
def favicon_svg():
    """Inline SVG favicon (lobster)."""
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
        "<text y='0.9em' font-size='90'>🦞</text>"
        "</svg>"
    )
    return Response(svg, mimetype='image/svg+xml')


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    """Login page"""
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        username = data.get('username', '')
        password = data.get('password', '')
        
        user = authenticate(username, password)
        if user:
            session['user'] = username
            session.permanent = True
            app.permanent_session_lifetime = timedelta(days=30)
            if request.is_json:
                return jsonify({"success": True, "user": user["full_name"]})
            return redirect(url_for('index'))
        
        if request.is_json:
            return jsonify({"error": "Invalid username or password"}), 401
        return render_template('login.html', error="Invalid username or password")
    
    return render_template('login.html')


@app.route('/logout')
def logout():
    """Logout and clear session"""
    session.pop('user', None)
    return redirect(url_for('login_page'))


@app.route('/api/profile-photo', methods=['POST'])
@login_required
def upload_profile_photo():
    """Upload profile photo for the current user."""
    try:
        user = get_current_user()
        if not user:
            return jsonify({"error": "User not found"}), 404

        if 'file' not in request.files:
            return jsonify({"error": "No file provided"}), 400
        file = request.files['file']
        if not file or file.filename == '':
            return jsonify({"error": "No file provided"}), 400

        filename = secure_filename(file.filename)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext not in ALLOWED_PROFILE_EXTS:
            return jsonify({"error": "Unsupported file type"}), 400

        safe_name = f"profile_{user['username']}.{ext}"
        save_path = UPLOAD_DIR / safe_name
        file.save(save_path)

        rel_path = f"/uploads/{safe_name}"
        update_user(user["username"], {"profile_image": rel_path})
        return jsonify({"success": True, "profile_image": rel_path})
    except Exception as e:
        logger.error(f"Profile photo upload error: {e}")
        return jsonify({"error": str(e)}), 500


@app.errorhandler(413)
def request_entity_too_large(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "File too large (max 5MB)"}), 413
    return "File too large", 413


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Forgot password - send reset link to email"""
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        email = data.get('email', '').strip().lower()
        
        if not email:
            return jsonify({"error": "Email is required"}), 400
        
        # Find user by email
        from auth import load_users
        users = load_users()
        username = None
        user = None
        for uname, udata in users.items():
            if udata.get('email', '').lower() == email:
                username = uname
                user = udata
                break
        
        if not user:
            # Don't reveal if email exists or not for security
            return jsonify({
                "success": True,
                "message": "If an account with that email exists, a password reset link has been sent."
            })
        
        # Generate reset token and send email
        token = generate_reset_token(username)
        reset_link = url_for('set_new_password', token=token, _external=True)
        
        email_sent = send_password_reset_email(
            to_email=user['email'],
            username=user['full_name'],
            reset_link=reset_link
        )
        
        if email_sent:
            return jsonify({
                "success": True,
                "message": "If an account with that email exists, a password reset link has been sent."
            })
        else:
            return jsonify({
                "success": True,
                "message": "If an account with that email exists, a password reset link has been sent.",
                "verification_link": reset_link
            })
    
    return render_template('forgot_password.html')


@app.route('/register')
def register_page():
    """Registration page (currently disabled)"""
    return render_template('register.html')


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account_page():
    """Account settings page"""
    user = get_current_user()
    
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        updates = {}
        
        if data.get('full_name'):
            updates['full_name'] = data['full_name']
        if data.get('email'):
            updates['email'] = data['email']
        if data.get('dob'):
            updates['dob'] = data['dob']
        if data.get('height_cm'):
            try:
                updates['height_cm'] = float(data['height_cm'])
            except (TypeError, ValueError):
                pass
        if data.get('initial_weight_kg'):
            try:
                updates['initial_weight_kg'] = float(data['initial_weight_kg'])
            except (TypeError, ValueError):
                pass
        if data.get('timezone'):
            updates['timezone'] = data['timezone']
        if data.get('pmc_style'):
            updates['pmc_style'] = data['pmc_style']

        # Planner constraints
        if isinstance(data.get('allowed_sports'), list):
            supported = {"cycling", "run", "swim", "gym", "xc_skiing", "kayaking"}
            updates['allowed_sports'] = [s for s in data['allowed_sports'] if s in supported]
        if data.get('max_workout_days') is not None and str(data.get('max_workout_days')).strip() != "":
            try:
                updates['max_workout_days'] = max(3, min(7, int(data.get('max_workout_days'))))
            except (TypeError, ValueError):
                pass
        
        if updates:
            update_user(session['user'], updates)
            user = get_current_user()  # Refresh user data
            if request.is_json:
                return jsonify({"success": True})
        
        if request.is_json:
            return jsonify({"success": True})
        return render_template('account.html', user=user, success="Account updated successfully")
    
    return render_template('account.html', user=user)


@app.route('/account/change-password', methods=['POST'])
@login_required
def request_password_change():
    """Request password change - sends verification email with link to set new password"""
    user = get_current_user()
    
    # Generate reset token (no password yet - user will set it after clicking link)
    token = generate_reset_token(session['user'])
    
    # Build verification link
    reset_link = url_for('set_new_password', token=token, _external=True)
    
    # Send email
    email_sent = send_password_reset_email(
        to_email=user['email'],
        username=user['full_name'],
        reset_link=reset_link
    )
    
    if email_sent:
        return jsonify({
            "success": True,
            "message": f"Password reset link sent to {user['email']}. Please check your inbox."
        })
    else:
        return jsonify({
            "success": True,
            "message": "Email service not configured. For testing, use this link:",
            "verification_link": reset_link
        })


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def set_new_password(token):
    """Show form to set new password after clicking email link"""
    from email_service import verify_reset_token
    
    # Verify token is valid (don't consume yet)
    token_data = verify_reset_token(token)
    
    if not token_data:
        return render_template('password_verified.html', 
                             success=False, 
                             message="Invalid or expired password reset link.")
    
    if request.method == 'GET':
        # Show the password reset form
        return render_template('set_password.html', token=token)
    
    # POST - process new password
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    if not new_password or not confirm_password:
        return render_template('set_password.html', token=token, 
                             error="Please fill in both password fields.")
    
    if new_password != confirm_password:
        return render_template('set_password.html', token=token,
                             error="Passwords do not match.")
    
    if len(new_password) < 8:
        return render_template('set_password.html', token=token,
                             error="Password must be at least 8 characters.")
    
    # Now consume the token
    from email_service import consume_reset_token
    token_data = consume_reset_token(token)
    
    if not token_data:
        return render_template('password_verified.html', 
                             success=False, 
                             message="Invalid or expired password reset link.")
    
    # Hash and save the new password
    from auth import load_users, save_users
    password_hash, salt = hash_password(new_password)
    users = load_users()
    username = token_data['username']
    
    if username in users:
        users[username]['password_hash'] = password_hash
        users[username]['salt'] = salt
        save_users(users)
        
        return render_template('password_verified.html',
                             success=True,
                             message="Your password has been changed successfully. You can now log in with your new password.")
    
    return render_template('password_verified.html',
                         success=False,
                         message="User not found.")


@app.route('/api/user')
@login_required
def api_user():
    """Get current user info"""
    user = get_current_user()
    if user:
        return jsonify({
            "username": user["username"],
            "full_name": user["full_name"],
            "email": user["email"],
            "dob": user.get("dob"),
            "height_cm": user.get("height_cm"),
            "initial_weight_kg": user.get("initial_weight_kg"),
            "timezone": user.get("timezone"),
            "profile_image": user.get("profile_image"),
            "pmc_style": user.get("pmc_style", "native"),
            "allowed_sports": user.get("allowed_sports", ["cycling", "run", "swim", "gym", "xc_skiing", "kayaking"]),
            "max_workout_days": user.get("max_workout_days", 6)
        })
    return jsonify({"error": "Not logged in"}), 401


@app.route('/api/cache/clear', methods=['POST'])
@login_required
def clear_cache():
    """Clear all in-memory caches to force fresh data fetch"""
    global _workout_cache, _pmc_cache, _weight_cache, _dashboard_cache, _workout_index
    _workout_cache = {"data": None, "expires": None}
    _pmc_cache = {"data": None, "expires": None}
    _weight_cache.clear()
    _dashboard_cache.clear()
    with _workout_index_lock:
        _workout_index = {"data": None, "loading": False, "loaded_at": None, "loading_started_at": None}
    logger.info("Cache cleared by user")
    return jsonify({"success": True, "message": "Cache cleared"})


@app.route('/api/health/today')
@login_required
def health_today():
    """Get health metrics for a specific date (default: today)"""
    target_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    logger.debug(f"Fetching health metrics for {target_date}")
    if not query_api:
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=7)
        stop_dt = target_dt + timedelta(days=1)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r._field == "sleep_duration_hours" or r._field == "deep_sleep_hours" or r._field == "rem_sleep_hours" or r._field == "light_sleep_hours" or r._field == "awake_hours" or r._field == "hrv_avg" or r._field == "resting_hr" or r._field == "avg_sleep_hr" or r._field == "sleep_efficiency" or r._field == "sleep_score" or r._field == "steps" or r._field == "recovery_score" or r._field == "training_load" or r._field == "weight")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)
        if isinstance(result, list):
            if len(result) == 0:
                return jsonify({"error": "No data from InfluxDB"}), 404
            result = pd.concat(result, ignore_index=True)

        if result.empty:
            return jsonify({"error": "No data from InfluxDB"}), 404

        # Build one row per date
        if "date" not in result.columns:
            return jsonify({"error": "No date field in daily_health data"}), 404

        df = result.copy()
        numeric_cols = [
            c for c in ["sleep_duration_hours", "deep_sleep_hours", "rem_sleep_hours", "light_sleep_hours", "awake_hours", "hrv_avg", "resting_hr", "avg_sleep_hr", "sleep_efficiency", "sleep_score", "steps", "recovery_score", "training_load", "weight"]
            if c in df.columns
        ]
        for c in numeric_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        df = _reduce_daily_health_by_source(df, numeric_cols)

        df = df.sort_values("date")
        
        target_row = df[df['date'] == target_date]
        row = target_row.iloc[0] if not target_row.empty else pd.Series(dtype=object)

        def clean_number(val):
            return None if pd.isna(val) else float(val)

        def get_value(col):
            if col not in row.index:
                return None
            return clean_number(row[col])

        steps_val = get_value("steps")
        if steps_val is None:
            previous_date = (target_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            previous_row = df[df["date"] == previous_date] if "date" in df.columns else pd.DataFrame()
            if not previous_row.empty and "steps" in previous_row.columns:
                previous_steps = previous_row.iloc[0].get("steps")
                if not pd.isna(previous_steps):
                    steps_val = float(previous_steps)
                    steps_source_date = previous_date
                else:
                    steps_source_date = None
            else:
                steps_source_date = None
        else:
            steps_source_date = target_date
        steps_clean = None if steps_val is None else int(float(steps_val))
        weight_val = get_value("weight")

        out = {
            "date": target_date,
            "source_date": row.get("date") if not row.empty else None,
            "sleep_hours": get_value("sleep_duration_hours"),
            "deep_sleep_hours": get_value("deep_sleep_hours"),
            "rem_sleep_hours": get_value("rem_sleep_hours"),
            "light_sleep_hours": get_value("light_sleep_hours"),
            "awake_hours": get_value("awake_hours"),
            "hrv": get_value("hrv_avg"),
            "resting_hr": get_value("resting_hr"),
            "avg_sleep_hr": get_value("avg_sleep_hr"),
            "sleep_efficiency": get_value("sleep_efficiency"),
            "sleep_score": get_value("sleep_score"),
            "steps": steps_clean,
            "steps_source_date": steps_source_date,
            "recovery_score": get_value("recovery_score"),
            "training_load": get_value("training_load")
        }
        if weight_val is not None:
            out["weight"] = weight_val
        return jsonify(out)
    except Exception as e:
        logger.error(f"Error fetching health for {target_date}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/health/history')
@login_required
def health_history():
    """Get historical health data"""
    days = request.args.get('days', 30, type=int)
    end_date = request.args.get('end_date', datetime.now().strftime("%Y-%m-%d"))
    logger.debug(f"Fetching health history: {days} days ending {end_date}")
    
    if not query_api:
        # Return mock data for demo
        logger.info("Using mock data for history (no InfluxDB)")
        return jsonify({"error": "No data from InfluxDB"}), 404
    
    try:
        # Calculate start date based on end_date and days
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=days + 7)  # Small buffer for data availability
        
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {(end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r._field == "hrv_avg" or r._field == "resting_hr" or r._field == "avg_sleep_hr" or r._field == "sleep_duration_hours" or r._field == "deep_sleep_hours" or r._field == "rem_sleep_hours" or r._field == "light_sleep_hours" or r._field == "awake_hours" or r._field == "sleep_efficiency" or r._field == "sleep_score" or r._field == "recovery_score" or r._field == "steps" or r._field == "weight" or r._field == "active_calories" or r._field == "total_calories")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)

        # Generate date range for the requested period
        dates_list = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days-1, -1, -1)]
        
        # Check if we have daily_health data
        has_daily_health = False
        if isinstance(result, list):
            if len(result) > 0:
                result = pd.concat(result, ignore_index=True)
                has_daily_health = not result.empty
        elif not result.empty:
            has_daily_health = True
        
        if has_daily_health and "date" in result.columns:
            # Process actual data from daily_health
            df = result.copy()
            numeric_cols = [
                c for c in ["hrv_avg", "resting_hr", "avg_sleep_hr", "sleep_duration_hours", "deep_sleep_hours", "rem_sleep_hours", "light_sleep_hours", "awake_hours", "sleep_efficiency", "sleep_score", "recovery_score", "steps", "weight", "active_calories", "total_calories"]
                if c in df.columns
            ]
            if numeric_cols:
                for c in numeric_cols:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df = _reduce_daily_health_by_source(df, numeric_cols)
            df = pd.DataFrame({"date": dates_list}).merge(df, on="date", how="left")
        else:
            # No daily_health data - create empty dataframe with just dates
            df = pd.DataFrame({"date": dates_list})

        def clean_series(series, digits=2):
            return [None if pd.isna(v) else round(float(v), digits) for v in series.tolist()]

        # Also fetch manual values history (weight, hrv, sleep, etc.)
        manual_data = {field: {} for field in ['weight', 'hrv', 'sleep', 'resting_hr', 'steps', 'calories']}
        try:
            manual_query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {(end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r._field == "weight" or r._field == "hrv" or r._field == "sleep" or r._field == "resting_hr" or r._field == "steps" or r._field == "calories")
              |> filter(fn: (r) => r.deleted != "true")
            '''
            manual_result = query_api.query(manual_query)
            for table in manual_result:
                for record in table.records:
                    date = record.values.get('date', '')
                    field = record.get_field()
                    if date and field in manual_data:
                        manual_data[field][date] = float(record.get_value())
        except Exception as e:
            logger.warning(f"Could not fetch manual values history: {e}")

        # Helper to merge automated and manual data, preferring manual values
        def merge_with_manual(auto_series, manual_dict, dates_list):
            result = []
            for i, date in enumerate(dates_list):
                manual_val = manual_dict.get(date)
                if manual_val is not None:
                    result.append(manual_val)
                elif i < len(auto_series):
                    result.append(auto_series[i])
                else:
                    result.append(None)
            return result

        hrv_auto = clean_series(df["hrv_avg"], 2) if "hrv_avg" in df else [None] * len(dates_list)
        rhr_auto = clean_series(df["resting_hr"], 2) if "resting_hr" in df else [None] * len(dates_list)
        sleep_auto = clean_series(df["sleep_duration_hours"], 2) if "sleep_duration_hours" in df else [None] * len(dates_list)
        deep_sleep_auto = clean_series(df["deep_sleep_hours"], 2) if "deep_sleep_hours" in df else [None] * len(dates_list)
        rem_sleep_auto = clean_series(df["rem_sleep_hours"], 2) if "rem_sleep_hours" in df else [None] * len(dates_list)
        light_sleep_auto = clean_series(df["light_sleep_hours"], 2) if "light_sleep_hours" in df else [None] * len(dates_list)
        awake_auto = clean_series(df["awake_hours"], 2) if "awake_hours" in df else [None] * len(dates_list)
        avg_sleep_hr_auto = clean_series(df["avg_sleep_hr"], 2) if "avg_sleep_hr" in df else [None] * len(dates_list)
        sleep_efficiency_auto = clean_series(df["sleep_efficiency"], 2) if "sleep_efficiency" in df else [None] * len(dates_list)
        sleep_score_auto = clean_series(df["sleep_score"], 2) if "sleep_score" in df else [None] * len(dates_list)
        steps_auto = clean_series(df["steps"], 0) if "steps" in df else [None] * len(dates_list)
        weight_auto = clean_series(df["weight"], 2) if "weight" in df else [None] * len(dates_list)
        active_cal_auto = clean_series(df["active_calories"], 0) if "active_calories" in df else [None] * len(dates_list)
        total_cal_auto = clean_series(df["total_calories"], 0) if "total_calories" in df else [None] * len(dates_list)
        calories_auto = [a if a is not None else total_cal_auto[i] for i, a in enumerate(active_cal_auto)]

        return jsonify({
            "dates": dates_list,
            "hrv": merge_with_manual(hrv_auto, manual_data['hrv'], dates_list),
            "resting_hr": merge_with_manual(rhr_auto, manual_data['resting_hr'], dates_list),
            "sleep": merge_with_manual(sleep_auto, manual_data['sleep'], dates_list),
            "deep_sleep": deep_sleep_auto,
            "rem_sleep": rem_sleep_auto,
            "light_sleep": light_sleep_auto,
            "awake": awake_auto,
            "avg_sleep_hr": avg_sleep_hr_auto,
            "sleep_efficiency": sleep_efficiency_auto,
            "sleep_score": sleep_score_auto,
            "recovery": clean_series(df["recovery_score"], 1) if "recovery_score" in df else [],
            "steps": merge_with_manual(steps_auto, manual_data['steps'], dates_list),
            "weight": merge_with_manual(weight_auto, manual_data['weight'], dates_list),
            "calories": merge_with_manual(calories_auto, manual_data['calories'], dates_list)
        })
    except Exception as e:
        logger.error(f"Error fetching health history: {e}")
        return jsonify({"error": str(e)}), 500


def _fetch_workouts_from_influx(before_date: str | None = None):
    """Fetch workouts from InfluxDB. Filter by date tag (not _time - points use write time)."""
    from collections import defaultdict

    now = datetime.now()
    days_back = WORKOUT_LOOKBACK_DAYS
    cutoff = (now - timedelta(days=days_back)).strftime('%Y-%m-%d')
    # Cap range for speed: only load last 42 days
    if before_date:
        try:
            target = datetime.strptime(before_date, "%Y-%m-%d").date()
            days_ago = (now.date() - target).days
            range_days = days_back
        except ValueError:
            range_days = days_back
    else:
        range_days = days_back
    date_filter = f'|> filter(fn: (r) => r.date >= "{cutoff}")'
    if before_date:
        date_filter = f'|> filter(fn: (r) => r.date >= "{cutoff}" and r.date <= "{before_date}")'

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{range_days}d)
      |> filter(fn: (r) => r._measurement == "{WORKOUT_READ_MEASUREMENT}")
      {date_filter}
    '''

    tables = query_api.query_stream(query)

    # Manual pivot in Python using _time as unique key
    workouts = defaultdict(dict)
    for record in tables:
        key = str(record.get_time())
        field = record.get_field()
        value = record.get_value()
        workouts[key][field] = value
        workouts[key]['date'] = record.values.get('date', '')
        workouts[key]['type'] = record.values.get('type', '')
    
    # Sort by date and start_time descending
    result = sorted(
        workouts.values(), 
        key=lambda x: (x.get('date', ''), x.get('start_time', '')), 
        reverse=True
    )
    return _dedupe_workouts(result)


def _workout_dedupe_key(workout: dict) -> str:
    """Return a stable key to de-duplicate workouts from multiple sources."""
    strava_id = workout.get("strava_id")
    if strava_id:
        sid = str(strava_id).strip()
        if sid.endswith(".0"):
            sid = sid[:-2]
        return f"strava:{sid}"
    return "|".join([
        str(workout.get("date", "")).strip(),
        str(workout.get("start_time", "")).strip(),
        str(workout.get("name", "")).strip(),
        str(workout.get("type", "")).strip(),
    ])


def _dedupe_workouts(records: list[dict]) -> list[dict]:
    """De-duplicate workouts while keeping original order."""
    seen = set()
    out = []
    for w in records:
        key = _workout_dedupe_key(w)
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out


def _fetch_workouts_limited(before_date: str | None, limit: int) -> list[dict]:
    """Fetch only the most recent workouts (limited) from both workout measurements."""
    if not INFLUXDB_TOKEN:
        return []
    if before_date:
        try:
            target_date = datetime.strptime(before_date, "%Y-%m-%d").date()
        except ValueError:
            target_date = datetime.now().date()
        cutoff_date = target_date - timedelta(days=42)
        date_filter = f'|> filter(fn: (r) => r.date >= "{cutoff_date.isoformat()}" and r.date <= "{before_date}")'
        range_days = min(max((datetime.now().date() - cutoff_date).days, 42), 4000)
    else:
        target_date = datetime.now().date()
        cutoff_date = target_date - timedelta(days=42)
        date_filter = f'|> filter(fn: (r) => r.date >= "{cutoff_date.isoformat()}")'
        range_days = 42

    workouts = {}
    needed_fields = (
        'r._field == "avg_hr" or r._field == "calories" or r._field == "distance" or '
        'r._field == "duration" or r._field == "duration_minutes" or r._field == "elevation_gain" or '
        'r._field == "max_hr" or r._field == "name" or r._field == "start_time" or '
        'r._field == "strava_id" or r._field == "suffer_score" or r._field == "relative_effort"'
    )
    influx = InfluxDBClient(
        url=INFLUXDB_URL,
        token=INFLUXDB_TOKEN,
        org=INFLUXDB_ORG,
        timeout=INFLUX_FAST_TIMEOUT_MS,
    )
    try:
        local_query_api = influx.query_api()
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {cutoff_date.strftime("%Y-%m-%dT00:00:00Z")}, stop: {(target_date + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "workouts" or r._measurement == "{WORKOUT_READ_MEASUREMENT}")
          |> filter(fn: (r) => {needed_fields})
          {date_filter}
        '''
        for record in local_query_api.query_stream(query):
            measurement = record.values.get("_measurement", "")
            key = f"{measurement}:{record.get_time()}"
            entry = workouts.setdefault(
                key,
                {
                    "date": record.values.get("date", ""),
                    "type": record.values.get("type", ""),
                },
            )
            entry[record.get_field()] = record.get_value()
    finally:
        influx.close()

    if not workouts:
        return []

    records = list(workouts.values())
    records = sorted(records, key=lambda x: (x.get("date", ""), x.get("start_time", "")), reverse=True)
    return _dedupe_workouts(records)[:limit]


def _get_daily_activity_snapshot(date: str) -> dict:
    """Fetch the latest daily activity fields for one date."""
    if not query_api:
        return {}

    try:
        target_dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {}

    start_dt = target_dt - timedelta(days=1)
    stop_dt = target_dt + timedelta(days=1)
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT23:59:59Z")})
      |> filter(fn: (r) => r._measurement == "daily_health")
      |> filter(fn: (r) => r.date == "{date}")
      |> filter(fn: (r) => r._field == "active_calories" or r._field == "total_calories" or r._field == "steps" or r._field == "weight")
    '''
    out = {}
    latest_by_field: dict[str, tuple[datetime, float]] = {}
    for rec in query_api.query_stream(query):
        field = rec.get_field()
        val = rec.get_value()
        if val is None:
            continue
        try:
            fval = float(val)
        except (TypeError, ValueError):
            continue
        ts = rec.get_time()
        current = latest_by_field.get(field)
        if current is None or ts > current[0]:
            latest_by_field[field] = (ts, fval)
    for field, (_ts, value) in latest_by_field.items():
        out[field] = value
    return out


def _estimate_step_calories(steps: float | None, weight_kg: float | None) -> float:
    """Estimate calories from walking steps when Apple active calories are unavailable."""
    if steps is None or weight_kg is None:
        return 0.0
    try:
        steps_val = float(steps)
        weight_val = float(weight_kg)
    except (TypeError, ValueError):
        return 0.0
    if steps_val <= 0 or weight_val <= 0:
        return 0.0
    # Rough walking estimate: ~0.04 kcal/step for a 70 kg person, scaled by body weight.
    return steps_val * 0.04 * (weight_val / 70.0)


def _load_workout_index() -> None:
    """Background load of all workouts into memory for fast filtering."""
    if not query_api:
        with _workout_index_lock:
            _workout_index["loading"] = False
        return

    from collections import defaultdict

    cutoff = (datetime.now() - timedelta(days=WORKOUT_INDEX_RANGE_DAYS)).strftime('%Y-%m-%d')
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{WORKOUT_INDEX_RANGE_DAYS}d)
      |> filter(fn: (r) => r._measurement == "{WORKOUT_READ_MEASUREMENT}")
      |> filter(fn: (r) => r.date >= "{cutoff}")
    '''

    workouts = defaultdict(dict)
    try:
        tables = query_api.query_stream(query)
        for record in tables:
            key = str(record.get_time())
            field = record.get_field()
            value = record.get_value()
            workouts[key][field] = value
            workouts[key]["date"] = record.values.get("date", "")
            workouts[key]["type"] = record.values.get("type", "")
    except Exception as e:
        logger.error(f"Error loading workout index: {e}")
        with _workout_index_lock:
            _workout_index["loading"] = False
        return

    # Sort by date and start_time descending once
    data = sorted(
        workouts.values(),
        key=lambda x: (x.get("date", ""), x.get("start_time", "")),
        reverse=True,
    )
    data = _dedupe_workouts(data)
    with _workout_index_lock:
        _workout_index["data"] = data
        _workout_index["loaded_at"] = datetime.now()
        _workout_index["loading"] = False
        _workout_index["loading_started_at"] = None


def _ensure_workout_index_loaded():
    """Return cached workout index or trigger background load."""
    now = datetime.now()
    with _workout_index_lock:
        data = _workout_index.get("data")
        loaded_at = _workout_index.get("loaded_at")
        loading = _workout_index.get("loading", False)
        loading_started_at = _workout_index.get("loading_started_at")

        if data and loaded_at and (now - loaded_at).total_seconds() < WORKOUT_INDEX_TTL_SECONDS:
            return data

        # If data exists but is stale, return it and refresh in background
        if not loading:
            _workout_index["loading"] = True
            _workout_index["loading_started_at"] = now
            threading.Thread(target=_load_workout_index, daemon=True).start()

        return data

@app.route('/api/workouts', methods=['GET', 'POST'])
@login_required
def workouts():
    """Get or log workouts"""
    if request.method == 'GET':
        filter_date = request.args.get('date')
        before_date = request.args.get('before_date')
        limit = request.args.get('limit', type=int)
        
        if not query_api:
            logger.info("Using mock workouts (no InfluxDB)")
            return jsonify({"error": "No workouts from InfluxDB"}), 404
        
        try:
            if before_date and limit and limit <= 10:
                cached, stale = _get_recent_workouts_from_cache(before_date, limit)
                if cached:
                    if stale:
                        _refresh_recent_workouts_cache_async(before_date)
                    resp = jsonify(cached)
                    if stale:
                        resp.headers["X-Workouts-Stale"] = "true"
                    return resp

            # Keep filtered reads off the slow index build path.
            if before_date or filter_date:
                query_limit = limit if limit and limit > 0 else 50
                target_date = before_date or filter_date
                records = _fetch_workouts_limited(target_date, query_limit)
                if filter_date:
                    records = [w for w in records if w.get('date') == filter_date]
                elif before_date:
                    records = [w for w in records if w.get('date', '') <= before_date]
                if limit and limit > 0:
                    records = records[:limit]
                return jsonify(records)

            # Check cache first (only if no filters)
            now = datetime.now()
            if not filter_date and not before_date and _workout_cache["data"] and _workout_cache["expires"] and now < _workout_cache["expires"]:
                records = _workout_cache["data"]
            else:
                logger.info(f"Fetching workouts from InfluxDB (date: {filter_date}, before: {before_date})")
                records = _fetch_workouts_from_influx(before_date=before_date)
            
            if not records:
                return jsonify({"error": "No workouts from InfluxDB"}), 404
            
            # Update cache (only if no filters)
            if not filter_date and not before_date:
                _workout_cache["data"] = records
                _workout_cache["expires"] = now + timedelta(seconds=CACHE_TTL_SECONDS)
            
            # Filter: before_date = workouts on or before that date (descending), limit
            if before_date:
                records = [w for w in records if w.get('date', '') <= before_date]
                if limit and limit > 0:
                    records = records[:limit]
            elif filter_date:
                records = [w for w in records if w.get('date') == filter_date]
            
            return jsonify(records)
        except Exception as e:
            logger.error(f"Error fetching workouts: {e}")
            return jsonify({"error": str(e)}), 500
    
    # POST - Log new workout
    if not write_api:
        logger.warning("Cannot log workout: InfluxDB not configured")
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    data = request.json
    logger.info(f"Logging workout: {data.get('type')} - {data.get('date')}")
    try:
        point = Point(WORKOUT_READ_MEASUREMENT)\
            .tag("type", data.get("type", "Unknown"))\
            .tag("date", data.get("date", datetime.now().strftime("%Y-%m-%d")))\
            .field("duration_minutes", float(data.get("duration", 0)))\
            .field("avg_hr", float(data.get("avg_hr", 0)))\
            .field("max_hr", float(data.get("max_hr", 0)))\
            .field("calories", int(data.get("calories", 0)))\
            .field("intensity", float(data.get("intensity", 5)))\
            .field("feeling", data.get("feeling", "okay"))
        
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        logger.info(f"Workout logged successfully: {data.get('type')}")
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error logging workout: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/manual-values', methods=['GET', 'POST', 'DELETE'])
@login_required
def manual_values():
    """Get, set, or delete manual override values"""
    if request.method == 'GET':
        date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if not query_api:
            return jsonify({})
        
        try:
            # Query latest manual values per metric (respect deleted markers)
            target_dt = datetime.strptime(date, "%Y-%m-%d")
            start_dt = target_dt - timedelta(days=7)
            stop_dt = target_dt + timedelta(days=1)
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r.date == "{date}")
              |> group(columns: ["_field"])
              |> sort(columns: ["_time"], desc: true)
              |> limit(n: 1)
            '''
            result = query_api.query(query)
            
            metrics = ['sleep', 'hrv', 'resting_hr', 'steps', 'weight', 'calories', 'ctl', 'atl', 'tsb']
            values = {m: None for m in metrics}
            
            for table in result:
                for record in table.records:
                    metric = record.get_field()
                    if metric not in values:
                        continue
                    is_deleted = str(record.values.get('deleted', '')).lower() == 'true'
                    if is_deleted:
                        values[metric] = None
                    else:
                        val = record.get_value()
                        values[metric] = None if val is None else float(val)
            
            logger.info(f"Manual values for {date}: {values}")
            return jsonify(values)
        except Exception as e:
            logger.error(f"Error fetching manual values: {e}")
            return jsonify({})
    
    elif request.method == 'POST':
        if not write_api:
            return jsonify({"error": "InfluxDB not configured"}), 500
        
        data = request.json
        metric = data.get('metric')
        value = data.get('value')
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if not metric or value is None:
            return jsonify({"error": "Missing metric or value"}), 400
        
        try:
            # Parse the target date and set timestamp to noon of that day
            target_dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=12, minute=0, second=0)
            point = Point("manual_values")\
                .tag("date", date)\
                .field(metric, float(value))\
                .time(target_dt)
            
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            logger.info(f"Manual value saved: {metric}={value} for {date}")
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"Error saving manual value: {e}")
            return jsonify({"error": str(e)}), 500
    
    elif request.method == 'DELETE':
        if not write_api:
            return jsonify({"error": "InfluxDB not configured"}), 500
        
        data = request.json
        metric = data.get('metric')
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if not metric:
            return jsonify({"error": "Missing metric"}), 400
        
        try:
            # Write a null/sentinel value to indicate deletion
            # InfluxDB doesn't support true deletion easily, so we use a marker
            # Parse the target date and set timestamp to noon of that day
            target_dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=12, minute=0, second=0)
            point = Point("manual_values")\
                .tag("date", date)\
                .tag("deleted", "true")\
                .field(metric, 0.0)\
                .time(target_dt)
            
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            if metric == "weight":
                _weight_cache.pop(date, None)
            logger.info(f"Manual value cleared: {metric} for {date}")
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"Error clearing manual value: {e}")
            return jsonify({"error": str(e)}), 500


@app.route('/api/recommendations/today')
@login_required
def recommendations_today():
    """Get today's exercise recommendations with user sport/day constraints."""
    health_data = get_mock_health_today()
    user = get_current_user() or {}
    allowed_sports = user.get("allowed_sports", ["cycling", "run", "swim", "gym", "xc_skiing", "kayaking"])
    max_days = user.get("max_workout_days", 6)

    rec = planner.get_recommendation(
        health_data,
        allowed_sports=allowed_sports,
        max_workout_days=max_days,
    )
    return jsonify(rec)


@app.route('/api/ai/recommendations')
@login_required
def ai_recommendations():
    """AI-powered adaptive recommendations using real health data."""
    user = get_current_user() or {}
    allowed_sports = user.get("allowed_sports", ["cycling", "run", "swim", "gym", "xc_skiing", "kayaking"])
    max_days = user.get("max_workout_days", 6)
    
    # Fetch real health data from InfluxDB
    health_data = _fetch_latest_health_metrics()
    
    # Fetch recent workouts from Strava
    recent_workouts = _fetch_recent_strava_workouts(days=14)
    
    # Use AI analyzer
    rec = analyzer.get_daily_recommendation(
        health_data=health_data,
        allowed_sports=allowed_sports,
        max_workout_days=max_days
    )
    
    # Also get weekly plan
    weekly_plan = analyzer.generate_adaptive_weekly_plan(
        health_data=health_data,
        allowed_sports=allowed_sports,
        max_workout_days=max_days
    )
    
    rec["weekly_plan"] = weekly_plan
    return jsonify(rec)


def _fetch_latest_health_metrics() -> Dict:
    """Fetch latest health metrics from InfluxDB."""
    try:
        if not INFLUX_CLIENT:
            return {"hrv": None, "sleep_hours": None, "resting_hr": None}
        
        query_api = INFLUX_CLIENT.query_api()
        
        # Query latest HRV
        hrv_query = 'from(bucket:"health") |> range(start:-7d) |> filter(fn:(r)=>r._measurement=="daily_health") |> filter(fn:(r)=>r._field=="hrv") |> last()'
        hrv_result = query_api.query(org=INFLUX_ORG, query=hrv_query)
        hrv = None
        for table in hrv_result:
            for record in table.records:
                hrv = record.get_value()
                break
        
        # Query latest sleep
        sleep_query = 'from(bucket:"health") |> range(start:-7d) |> filter(fn:(r)=>r._measurement=="daily_health") |> filter(fn:(r)=>r._field=="sleep_hours") |> last()'
        sleep_result = query_api.query(org=INFLUX_ORG, query=sleep_query)
        sleep_hours = None
        for table in sleep_result:
            for record in table.records:
                sleep_hours = record.get_value()
                break
        
        # Query latest resting HR
        resting_hr_query = 'from(bucket:"health") |> range(start:-7d) |> filter(fn:(r)=>r._measurement=="daily_health") |> filter(fn:(r)=>r._field=="resting_heart_rate") |> last()'
        resting_result = query_api.query(org=INFLUX_ORG, query=resting_hr_query)
        resting_hr = None
        for table in resting_result:
            for record in table.records:
                resting_hr = record.get_value()
                break
        
        return {
            "hrv": hrv,
            "sleep_hours": sleep_hours,
            "resting_hr": resting_hr,
            "source": "influxdb"
        }
    except Exception as e:
        print(f"Error fetching health metrics: {e}")
        return {"hrv": None, "sleep_hours": None, "resting_hr": None, "error": str(e)}


def _fetch_recent_strava_workouts(days: int = 14) -> List[Dict]:
    """Fetch recent workouts from Strava."""
    try:
        if not STRAVA_ACCESS_TOKEN:
            return []
        
        import requests
        headers = {"Authorization": f"Bearer {STRAVA_ACCESS_TOKEN}"}
        resp = requests.get(
            f"https://www.strava.com/api/v3/athlete/activities?per_page={days}",
            headers=headers
        )
        
        if resp.status_code != 200:
            return []
        
        activities = resp.json()
        workouts = []
        for a in activities:
            date = a.get("start_date_local", "")[:10]
            workouts.append({
                "date": date,
                "type": a.get("type"),
                "duration": a.get("moving_time", 0) // 60,
                "intensity": min(10, a.get("suffer_score", 30) / 10 + 3) if a.get("suffer_score") else 5,
                "distance": a.get("distance", 0) / 1000,
                "suffer_score": a.get("suffer_score")
            })
        
        return workouts
    except Exception as e:
        print(f"Error fetching Strava workouts: {e}")
        return []


@app.route('/api/calories')
@login_required
def calories():
    """Get calories burned for today (default) or specified date.
    
    Sources (in priority order):
    1. BMR + workouts (requires user profile)
    2. daily_health.active_calories (Apple Health fallback)
    3. daily_health.total_calories (fallback)
    """
    date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    
    if not query_api:
        return jsonify({"calories": 0, "date": date})
    
    try:
        bmr, meta = _get_bmr_calories_for_user(date)
        weight_for_workouts = meta.get("weight_kg") if bmr is not None else None
        workout_total = _get_workout_calories(date, weight_for_workouts)
        activity = _get_daily_activity_snapshot(date)
        active_val = activity.get("active_calories")
        total_val = activity.get("total_calories")
        steps_val = activity.get("steps")
        step_total = _estimate_step_calories(steps_val, weight_for_workouts)
        if bmr is not None:
            if total_val is not None and total_val > 0:
                total = total_val
                source = "apple_health_total"
            else:
                activity_total = max(float(active_val or 0.0), float(workout_total or 0.0) + float(step_total or 0.0))
                total = bmr + activity_total if activity_total > 0 else bmr
                if active_val is not None and active_val > 0:
                    source = "bmr+apple_active"
                elif workout_total > 0 or step_total > 0:
                    source = "bmr+activity"
                else:
                    source = "bmr"
            return jsonify({
                "calories": int(total),
                "date": date,
                "source": source,
                "bmr": round(bmr, 1),
                "workout_calories": int(workout_total),
                "step_calories": int(step_total),
                "steps": int(steps_val) if steps_val is not None else None,
                "active_calories": int(active_val) if active_val is not None else None,
            })

        if active_val is not None:
            return jsonify({"calories": int(active_val), "date": date, "source": "apple_health_active"})
        if total_val is not None:
            return jsonify({"calories": int(total_val), "date": date, "source": "apple_health_total"})
        
        return jsonify({"calories": 0, "date": date, "source": "none", "missing_profile": meta})
    except Exception as e:
        logger.error(f"Error fetching calories: {e}")
        return jsonify({"calories": 0, "date": date, "error": str(e)})


@app.route('/api/calories/history')
@login_required
def calories_history():
    """Get computed calories history anchored to the selected end date."""
    days = request.args.get('days', 30, type=int)
    end_date = request.args.get('end_date', datetime.now().strftime('%Y-%m-%d'))
    try:
        user = get_current_user()
        return jsonify(_get_calories_history(days, end_date, user=user))
    except Exception as e:
        logger.error(f"Calories history error: {e}")
        return jsonify({"error": str(e), "dates": [], "calories": []}), 500


@app.route('/api/weight', methods=['GET', 'POST'])
@login_required
def weight():
    """Get or set weight"""
    if request.method == 'GET':
        date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if not query_api:
            return jsonify({"weight": None})
        
        try:
            resp = _get_weight_for_date(date)
            return jsonify(resp)
        except Exception as e:
            logger.error(f"Error fetching weight: {e}")
            return jsonify({"weight": None, "error": str(e)})
    
    elif request.method == 'POST':
        if not write_api:
            return jsonify({"error": "InfluxDB not configured"}), 500
        
        data = request.json
        weight_val = data.get('weight')
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if weight_val is None:
            return jsonify({"error": "Missing weight value"}), 400
        
        try:
            target_dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=12, minute=0, second=0)
            point = Point("manual_values")\
                .tag("date", date)\
                .field("weight", float(weight_val))\
                .time(target_dt)
            
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            _weight_cache.pop(date, None)  # Invalidate cache
            logger.info(f"Weight saved: {weight_val} kg for {date}")
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"Error saving weight: {e}")
            return jsonify({"error": str(e)}), 500


@app.route('/api/formula/params')
@login_required
def formula_params():
    """Get current formula parameters"""
    params = load_params()
    return jsonify(params)


@app.route('/api/formula/learn', methods=['POST'])
@login_required
def formula_learn():
    """
    Trigger a learning cycle to optimize formula parameters
    based on manually entered CTL/ATL values.
    """
    if not query_api:
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    try:
        # Fetch daily loads from InfluxDB
        daily_loads = _fetch_daily_loads_from_influx(query_days=365)
        
        if not daily_loads:
            return jsonify({"error": "No training load data available"}), 400
        
        # Run learning cycle
        new_params = run_learning_cycle(query_api, INFLUXDB_BUCKET, daily_loads)
        
        # Reload parameters in training_load module
        reload_params()
        
        # Clear PMC cache to use new parameters
        _pmc_cache["data"] = None
        _pmc_cache["expires"] = None
        
        logger.info(f"Formula learning completed: {new_params}")
        return jsonify({
            "success": True,
            "params": new_params,
            "message": "Parameters optimized based on manual reference values"
        })
    except Exception as e:
        logger.error(f"Error in formula learning: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/suunto/sync')
def suunto_sync():
    """Sync data from Suunto API"""
    logger.info("Starting Suunto sync")
    if not suunto.is_configured:
        logger.warning("Suunto not configured")
        return jsonify({"error": "Suunto not configured"}), 500
    
    try:
        data = suunto.get_daily_summaries(days=7)
        logger.info(f"Suunto returned {len(data) if data else 0} days of data")
        
        if write_api and data:
            for day in data:
                point = Point("daily_health")\
                    .tag("date", day.get("date"))\
                    .field("sleep_duration_hours", day.get("sleep_hours", 0))\
                    .field("hrv_avg", day.get("hrv", 0))\
                    .field("resting_hr", day.get("resting_hr", 0))\
                    .field("steps", day.get("steps", 0))
                
                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            logger.info(f"Synced {len(data)} days to InfluxDB")
        
        return jsonify({"synced": len(data) if data else 0, "data": data})
    except Exception as e:
        logger.error(f"Error syncing Suunto: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/strava/sync')
def strava_sync():
    """Sync workouts from Strava API"""
    logger.info("Starting Strava sync")
    days = request.args.get('days', 30, type=int)
    
    if not strava.is_configured:
        # Return mock data
        logger.info("Strava not configured, using demo mode")
        mock_workouts = MockStravaClient().get_activities(days)
        return jsonify({"synced": len(mock_workouts), "data": mock_workouts, "mode": "demo"})
    
    if not write_api:
        logger.warning("InfluxDB not configured for Strava sync")
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    try:
        activities = strava.get_activities(days)
        
        if activities:
            from influxdb_client import Point
            for activity in activities:
                point = Point("workouts")\
                    .tag("type", activity.get("type", "Unknown"))\
                    .tag("date", activity.get("date", ""))\
                    .field("duration_minutes", float(activity.get("duration", 0)))\
                    .field("avg_hr", float(activity.get("avg_hr", 0)) if activity.get("avg_hr") else 0.0)\
                    .field("max_hr", float(activity.get("max_hr", 0)) if activity.get("max_hr") else 0.0)\
                    .field("calories", activity.get("calories", 0))\
                    .field("feeling", activity.get("feeling", "good"))
                
                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        
        logger.info(f"Synced {len(activities) if activities else 0} activities to InfluxDB")
        return jsonify({"synced": len(activities) if activities else 0, "data": activities})
    except Exception as e:
        logger.error(f"Error syncing Strava: {e}")
        return jsonify({"error": str(e)}), 500


def _fetch_daily_loads_from_influx(query_days=120, end_date: str | None = None):
    """Fetch daily training loads from raw workout_cache rows.

    The grouped Flux query over suffer_score is unstable on this InfluxDB
    instance, but the raw workout stream is stable. We aggregate daily loads
    in Python after de-duplicating workouts.
    """
    if not INFLUXDB_TOKEN:
        return []
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()
    except ValueError:
        end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=query_days - 1)

    workouts_by_time: dict[str, dict] = {}
    needed_fields = (
        'r._field == "suffer_score" or r._field == "relative_effort" or '
        'r._field == "strava_id" or r._field == "start_time" or r._field == "name"'
    )
    influx = InfluxDBClient(
        url=INFLUXDB_URL,
        token=INFLUXDB_TOKEN,
        org=INFLUXDB_ORG,
        timeout=INFLUX_FAST_TIMEOUT_MS,
    )
    try:
        local_query_api = influx.query_api()
        measurements = ["workouts", WORKOUT_READ_MEASUREMENT]
        measurement_filter = " or ".join([f'r._measurement == "{m}"' for m in measurements])
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {(end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => {measurement_filter})
          |> filter(fn: (r) => {needed_fields})
          |> filter(fn: (r) => r.date >= "{start_dt.strftime("%Y-%m-%d")}" and r.date <= "{end_dt.strftime("%Y-%m-%d")}")
        '''
        for record in local_query_api.query_stream(query):
            measurement = record.values.get("_measurement", "")
            key = f"{measurement}:{record.get_time()}"
            row = workouts_by_time.setdefault(
                key,
                {"date": record.values.get("date", ""), "type": record.values.get("type", "")},
            )
            row[record.get_field()] = record.get_value()
    finally:
        influx.close()

    recent_workouts = _dedupe_workouts(
        sorted(
            workouts_by_time.values(),
            key=lambda x: (x.get("date", ""), x.get("start_time", "")),
            reverse=True,
        )
    )

    # Overlay the local recent-workouts cache, which may contain newer workouts
    # than the current stable Influx workout stream.
    _load_recent_workouts_cache_from_disk()
    with _recent_workouts_lock:
        cached_workouts = list(_recent_workouts_cache.get("data") or [])
    if cached_workouts:
        recent_workouts = _dedupe_workouts(
            sorted(
                recent_workouts + cached_workouts,
                key=lambda x: (x.get("date", ""), x.get("start_time", "")),
                reverse=True,
            )
        )

    by_date: dict[str, float] = {}
    for workout in recent_workouts:
        date = workout.get("date", "")
        if not date:
            continue
        load = workout.get("suffer_score")
        if load is None:
            load = workout.get("relative_effort")
        try:
            load_val = float(load or 0.0)
        except (TypeError, ValueError):
            load_val = 0.0
        by_date[date] = by_date.get(date, 0.0) + load_val
    return [{"date": d, "load": by_date[d]} for d in sorted(by_date)]


def _fetch_daily_loads_from_strava(query_days=120, end_date: str | None = None):
    """Fallback PMC loader using Strava activities when Influx is unstable."""
    if not getattr(strava, "is_configured", False):
        return []
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else datetime.now().date()
    except ValueError:
        end_dt = datetime.now().date()
    today = datetime.now().date()
    days_back = max(0, (today - end_dt).days)
    fetch_days = max(query_days + days_back + 7, query_days)
    activities = strava.get_activities(fetch_days)
    if not activities:
        return []
    start_dt = end_dt - timedelta(days=query_days - 1)
    by_date: dict[str, float] = {}
    for activity in activities:
        ds = activity.get("date")
        if not ds or ds < start_dt.isoformat() or ds > end_dt.isoformat():
            continue
        load = activity.get("suffer_score")
        if load is None:
            load = activity.get("relative_effort")
        try:
            load_val = float(load or 0.0)
        except (TypeError, ValueError):
            load_val = 0.0
        by_date[ds] = by_date.get(ds, 0.0) + load_val
    return [{"date": d, "load": by_date[d]} for d in sorted(by_date)]


def _fetch_daily_loads_from_recent_cache(query_days=120, end_date: str | None = None):
    """Fast local fallback using the on-disk recent workouts cache."""
    _load_recent_workouts_cache_from_disk()
    with _recent_workouts_lock:
        workouts = list(_recent_workouts_cache.get("data") or [])
    if not workouts:
        return []
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else datetime.now().date()
    except ValueError:
        end_dt = datetime.now().date()
    start_dt = end_dt - timedelta(days=query_days - 1)
    workouts = _dedupe_workouts(
        sorted(workouts, key=lambda x: (x.get("date", ""), x.get("start_time", "")), reverse=True)
    )
    by_date: dict[str, float] = {}
    for workout in workouts:
        ds = workout.get("date")
        if not ds or ds < start_dt.isoformat() or ds > end_dt.isoformat():
            continue
        load = workout.get("suffer_score")
        if load is None:
            load = workout.get("relative_effort")
        try:
            load_val = float(load or 0.0)
        except (TypeError, ValueError):
            load_val = 0.0
        by_date[ds] = by_date.get(ds, 0.0) + load_val
    return [{"date": d, "load": by_date[d]} for d in sorted(by_date)]


def _resolve_pmc_daily_loads(query_days: int, end_date_str: str) -> tuple[list[dict], str]:
    cached_loads, stale, cache_source = _get_pmc_daily_loads_from_cache(query_days, end_date_str)
    if cached_loads:
        if stale:
            _refresh_pmc_daily_loads_async(max(365, query_days), end_date_str)
        return cached_loads, cache_source or "pmc_cache"

    if getattr(strava, "is_configured", False):
        try:
            daily_loads = _fetch_daily_loads_from_strava(max(365, query_days), end_date_str)
            if daily_loads:
                _store_pmc_daily_loads_cache(daily_loads, "strava")
                return _filter_daily_loads_window(daily_loads, query_days, end_date_str), "strava"
        except Exception as e:
            logger.error(f"PMC Strava fallback error: {e}")

    try:
        daily_loads = _fetch_daily_loads_from_recent_cache(query_days, end_date_str)
        if daily_loads:
            return daily_loads, "recent_workouts_cache"
    except Exception as e:
        logger.error(f"PMC local cache error: {e}")

    try:
        daily_loads = _fetch_daily_loads_from_influx(query_days, end_date_str)
        if daily_loads:
            return daily_loads, "influx"
    except Exception as e:
        logger.error(f"PMC Influx error: {e}")

    return [], "none"


def _build_pmc_payload(days: int, end_date_str: str, user: dict | None = None) -> dict:
    started_at = perf_counter()
    try:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except ValueError:
        end_date = datetime.now().date()
        end_date_str = end_date.isoformat()

    query_days = max(days + 42, PMC_MIN_LOOKBACK_DAYS)
    daily_loads, source = _resolve_pmc_daily_loads(query_days, end_date_str)
    if not daily_loads:
        _log_perf("PMC", started_at, source=source, rows=0, end_date=end_date_str)
        return {"error": "No training load data available", "source": source}

    loads_map = {d["date"]: float(d.get("load", 0.0)) for d in daily_loads}
    start_date = end_date - timedelta(days=query_days - 1)
    full_series = []
    cur = start_date
    while cur <= end_date:
        full_series.append({"date": cur.isoformat(), "load": loads_map.get(cur.isoformat(), 0.0)})
        cur += timedelta(days=1)

    params = _get_pmc_params_for_user(user)
    pmc_series = calculate_pmc_series(
        full_series,
        ctl_days=params["ctl_days"],
        atl_days=params["atl_days"],
        load_scale_factor=params["load_scale_factor"],
        tsb_lag_days=params.get("tsb_lag_days", 0),
        seed_mode=params.get("seed_mode", "zeros"),
    )
    pmc_recent = pmc_series[-days:]
    latest = pmc_recent[-1] if pmc_recent else {"ctl": 0, "atl": 0, "tsb": 0}
    payload = {
        "ctl": latest["ctl"],
        "atl": latest["atl"],
        "tsb": latest["tsb"],
        "status": get_status_description(latest["tsb"]),
        "description": get_status_description(latest["tsb"]),
        "pmc_params": params,
        "days_tracked": len(full_series),
        "source": source,
        "chart": {
            "dates": [d["date"] for d in pmc_recent],
            "ctl": [d["ctl"] for d in pmc_recent],
            "atl": [d["atl"] for d in pmc_recent],
            "tsb": [d["tsb"] for d in pmc_recent],
        }
    }
    _log_perf("PMC", started_at, source=source, rows=len(daily_loads), end_date=end_date_str)
    return payload

def _dash_fetch_health_today(target_date: str) -> dict:
    """Fetch health metrics for a date. Returns dict for dashboard. Thread-safe."""
    if not query_api:
        return {"error": "InfluxDB not configured"}
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=7)
        stop_dt = target_dt + timedelta(days=1)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r._field == "sleep_duration_hours" or r._field == "deep_sleep_hours" or r._field == "rem_sleep_hours" or r._field == "light_sleep_hours" or r._field == "awake_hours" or r._field == "hrv_avg" or r._field == "resting_hr" or r._field == "avg_sleep_hr" or r._field == "sleep_efficiency" or r._field == "sleep_score" or r._field == "steps" or r._field == "recovery_score" or r._field == "training_load" or r._field == "weight")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)
        if isinstance(result, list):
            if len(result) == 0:
                return {"error": "No data from InfluxDB"}
            result = pd.concat(result, ignore_index=True)
        if result.empty or "date" not in result.columns:
            return {"error": "No data from InfluxDB"}
        df = result.copy()
        numeric_cols = [c for c in ["sleep_duration_hours", "deep_sleep_hours", "rem_sleep_hours", "light_sleep_hours", "awake_hours", "hrv_avg", "resting_hr", "avg_sleep_hr", "sleep_efficiency", "sleep_score", "steps", "recovery_score", "training_load", "weight"] if c in df.columns]
        for c in numeric_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = _reduce_daily_health_by_source(df, numeric_cols)
        target_row = df[df['date'] == target_date]
        row = target_row.iloc[0] if not target_row.empty else pd.Series(dtype=object)
        def clean(v):
            return None if pd.isna(v) else float(v)
        def get(col):
            return clean(row[col]) if col in row.index else None
        steps_val = get("steps")
        if steps_val is None:
            previous_date = (target_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            previous_row = df[df["date"] == previous_date] if "date" in df.columns else pd.DataFrame()
            if not previous_row.empty and "steps" in previous_row.columns:
                previous_steps = previous_row.iloc[0].get("steps")
                if not pd.isna(previous_steps):
                    steps_val = float(previous_steps)
                    steps_source_date = previous_date
                else:
                    steps_source_date = None
            else:
                steps_source_date = None
        else:
            steps_source_date = target_date
        return {
            "date": target_date,
            "source_date": row.get("date") if not row.empty else None,
            "sleep_hours": get("sleep_duration_hours"),
            "deep_sleep_hours": get("deep_sleep_hours"),
            "rem_sleep_hours": get("rem_sleep_hours"),
            "light_sleep_hours": get("light_sleep_hours"),
            "awake_hours": get("awake_hours"),
            "hrv": get("hrv_avg"),
            "resting_hr": get("resting_hr"),
            "avg_sleep_hr": get("avg_sleep_hr"),
            "sleep_efficiency": get("sleep_efficiency"),
            "sleep_score": get("sleep_score"),
            "steps": None if steps_val is None else int(float(steps_val)),
            "steps_source_date": steps_source_date,
            "recovery_score": get("recovery_score"),
            "training_load": get("training_load")
        }
    except Exception as e:
        logger.error(f"Dashboard health_today error: {e}")
        return {"error": str(e)}


def _dash_fetch_health_history(days: int, end_date: str) -> dict:
    """Fetch health history. Returns dict for dashboard. Thread-safe."""
    if not query_api:
        return {"error": "No data from InfluxDB"}
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=days + 7)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {(end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r._field == "hrv_avg" or r._field == "resting_hr" or r._field == "avg_sleep_hr" or r._field == "sleep_duration_hours" or r._field == "deep_sleep_hours" or r._field == "rem_sleep_hours" or r._field == "light_sleep_hours" or r._field == "awake_hours" or r._field == "sleep_efficiency" or r._field == "sleep_score" or r._field == "recovery_score" or r._field == "steps" or r._field == "weight" or r._field == "active_calories" or r._field == "total_calories")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)
        dates_list = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days-1, -1, -1)]
        has_daily_health = False
        if isinstance(result, list):
            if len(result) > 0:
                result = pd.concat(result, ignore_index=True)
                has_daily_health = not result.empty
        elif not result.empty:
            has_daily_health = True
        if has_daily_health and "date" in result.columns:
            df = result.copy()
            numeric_cols = [c for c in ["hrv_avg", "resting_hr", "avg_sleep_hr", "sleep_duration_hours", "deep_sleep_hours", "rem_sleep_hours", "light_sleep_hours", "awake_hours", "sleep_efficiency", "sleep_score", "recovery_score", "steps", "weight", "active_calories", "total_calories"] if c in df.columns]
            if numeric_cols:
                for c in numeric_cols:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df = _reduce_daily_health_by_source(df, numeric_cols)
            df = pd.DataFrame({"date": dates_list}).merge(df, on="date", how="left")
        else:
            df = pd.DataFrame({"date": dates_list})
        def clean_series(s, d=2):
            return [None if pd.isna(v) else round(float(v), d) for v in s.tolist()]
        manual_data = {f: {} for f in ['weight', 'hrv', 'sleep', 'resting_hr', 'steps', 'calories']}
        try:
            manual_query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {(end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r._field == "weight" or r._field == "hrv" or r._field == "sleep" or r._field == "resting_hr" or r._field == "steps" or r._field == "calories")
              |> filter(fn: (r) => r.deleted != "true")
            '''
            for table in query_api.query(manual_query):
                for rec in table.records:
                    d = rec.values.get('date', '')
                    f = rec.get_field()
                    if d and f in manual_data:
                        manual_data[f][d] = float(rec.get_value())
        except Exception:
            pass
        def merge(auto, manual_dict, dates):
            return [manual_dict.get(d) if manual_dict.get(d) is not None else (auto[i] if i < len(auto) else None) for i, d in enumerate(dates)]
        hrv_a = clean_series(df["hrv_avg"], 2) if "hrv_avg" in df else [None] * len(dates_list)
        rhr_a = clean_series(df["resting_hr"], 2) if "resting_hr" in df else [None] * len(dates_list)
        sleep_a = clean_series(df["sleep_duration_hours"], 2) if "sleep_duration_hours" in df else [None] * len(dates_list)
        deep_sleep_a = clean_series(df["deep_sleep_hours"], 2) if "deep_sleep_hours" in df else [None] * len(dates_list)
        rem_sleep_a = clean_series(df["rem_sleep_hours"], 2) if "rem_sleep_hours" in df else [None] * len(dates_list)
        light_sleep_a = clean_series(df["light_sleep_hours"], 2) if "light_sleep_hours" in df else [None] * len(dates_list)
        awake_a = clean_series(df["awake_hours"], 2) if "awake_hours" in df else [None] * len(dates_list)
        avg_sleep_hr_a = clean_series(df["avg_sleep_hr"], 2) if "avg_sleep_hr" in df else [None] * len(dates_list)
        sleep_efficiency_a = clean_series(df["sleep_efficiency"], 2) if "sleep_efficiency" in df else [None] * len(dates_list)
        sleep_score_a = clean_series(df["sleep_score"], 2) if "sleep_score" in df else [None] * len(dates_list)
        steps_a = clean_series(df["steps"], 0) if "steps" in df else [None] * len(dates_list)
        weight_a = clean_series(df["weight"], 2) if "weight" in df else [None] * len(dates_list)
        active_cal_a = clean_series(df["active_calories"], 0) if "active_calories" in df else [None] * len(dates_list)
        total_cal_a = clean_series(df["total_calories"], 0) if "total_calories" in df else [None] * len(dates_list)
        calories_a = [a if a is not None else total_cal_a[i] for i, a in enumerate(active_cal_a)]
        return {
            "dates": dates_list,
            "hrv": merge(hrv_a, manual_data['hrv'], dates_list),
            "resting_hr": merge(rhr_a, manual_data['resting_hr'], dates_list),
            "sleep": merge(sleep_a, manual_data['sleep'], dates_list),
            "deep_sleep": deep_sleep_a,
            "rem_sleep": rem_sleep_a,
            "light_sleep": light_sleep_a,
            "awake": awake_a,
            "avg_sleep_hr": avg_sleep_hr_a,
            "sleep_efficiency": sleep_efficiency_a,
            "sleep_score": sleep_score_a,
            "recovery": clean_series(df["recovery_score"], 1) if "recovery_score" in df else [],
            "steps": merge(steps_a, manual_data['steps'], dates_list),
            "weight": merge(weight_a, manual_data['weight'], dates_list),
            "calories": merge(calories_a, manual_data['calories'], dates_list)
        }
    except Exception as e:
        logger.error(f"Dashboard health_history error: {e}")
        return {"dates": [], "hrv": [], "resting_hr": [], "sleep": [], "recovery": [], "steps": [], "weight": []}


def _dash_fetch_recommendations(date: str, user: dict | None = None) -> dict:
    """Fetch recommendations. Thread-safe."""
    try:
        health = get_mock_health_today()
        health["date"] = date
        user = user or {}
        allowed_sports = user.get("allowed_sports", ["cycling", "run", "swim", "gym", "xc_skiing", "kayaking"])
        max_days = user.get("max_workout_days", 6)
        return planner.get_recommendation(health, allowed_sports=allowed_sports, max_workout_days=max_days)
    except Exception as e:
        logger.error(f"Dashboard recommendations error: {e}")
        return {"error": str(e)}


def _week_start_for_date(date_str: str) -> str:
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    return (target - timedelta(days=target.weekday())).isoformat()


def _field_record_value(value):
    if hasattr(value, "item"):
        return value.item()
    return value


def _query_coach_records(measurement: str, tag_filters: dict[str, str], range_days: int = 30) -> list[dict]:
    if not query_api:
        return []
    filters = "\n".join(
        f'          |> filter(fn: (r) => r.{tag} == "{value}")'
        for tag, value in tag_filters.items()
    )
    query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -{range_days}d)
          |> filter(fn: (r) => r._measurement == "{measurement}")
{filters}
        '''
    rows: dict[tuple, dict] = {}
    for table in query_api.query(query):
        for rec in table.records:
            values = rec.values
            key = (
                values.get("date", ""),
                values.get("week_start", ""),
                values.get("day", ""),
                values.get("status", ""),
                values.get("source", ""),
            )
            row = rows.setdefault(
                key,
                {
                    "date": key[0],
                    "week_start": key[1],
                    "day": key[2],
                    "status": key[3],
                    "source": key[4],
                },
            )
            row[rec.get_field()] = _field_record_value(rec.get_value())
    return list(rows.values())


@app.route('/api/coach/weekly-plan')
@login_required
def coach_weekly_plan():
    """Read AI coach weekly_plan records for the active week."""
    date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    try:
        week_start = request.args.get('week_start') or _week_start_for_date(date)
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    try:
        records = _query_coach_records("weekly_plan", {"week_start": week_start})
        day_order = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
        records.sort(key=lambda row: (row.get("date") or "", day_order.get(row.get("day"), 99)))
        return jsonify({"week_start": week_start, "items": records})
    except Exception as e:
        logger.error(f"Coach weekly plan error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/coach/daily-feedback')
@login_required
def coach_daily_feedback():
    """Read AI coach daily_feedback record for the selected date."""
    date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    try:
        datetime.strptime(date, "%Y-%m-%d")
        week_start = _week_start_for_date(date)
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    try:
        records = _query_coach_records("daily_feedback", {"date": date})
        status_order = {"adjusted": 0, "completed": 1, "checked_in": 2, "planned": 3, "missed": 4}
        records.sort(key=lambda row: status_order.get(row.get("status"), 9))
        feedback = records[0] if records else None
        return jsonify({"date": date, "week_start": week_start, "feedback": feedback})
    except Exception as e:
        logger.error(f"Coach daily feedback error: {e}")
        return jsonify({"error": str(e)}), 500


def _dash_fetch_pmc(days: int, end_date_str: str, user: dict | None = None) -> dict:
    """Fetch PMC data. Thread-safe."""
    try:
        return _build_pmc_payload(days, end_date_str, user)
    except Exception as e:
        logger.error(f"Dashboard PMC error: {e}")
        return {"error": str(e)}


def _dash_fetch_workouts(before_date: str, limit: int = 10) -> list | dict:
    """Fetch the 10 most recent workouts on or before the view date. Thread-safe."""
    if not query_api:
        return []
    try:
        return _fetch_workouts_limited(before_date, limit)
    except Exception as e:
        logger.error(f"Dashboard workouts error: {e}")
        return []


def _get_calories_history(days: int, end_date: str, user: dict | None = None) -> dict:
    """Build a calories series anchored to the selected end date."""
    if user is None:
        user = get_current_user()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    dates = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    history = _dash_fetch_health_history(days, end_date)
    if isinstance(history, dict) and history.get("error"):
        history = {"dates": dates, "steps": [None] * days, "weight": [None] * days}

    weights_by_date = {}
    steps_by_date = {}
    for i, ds in enumerate(history.get("dates", dates)):
        weights = history.get("weight", [])
        steps = history.get("steps", [])
        if i < len(weights):
            weights_by_date[ds] = weights[i]
        if i < len(steps):
            steps_by_date[ds] = steps[i]

    workouts = _fetch_workouts_limited(end_date, 500)
    workouts_by_date: dict[str, list[dict]] = {}
    for workout in workouts:
        ds = workout.get("date")
        if ds in set(dates):
            workouts_by_date.setdefault(ds, []).append(workout)

    series = []
    for ds in dates:
        weight_val = weights_by_date.get(ds)
        if weight_val is None and user and user.get("initial_weight_kg") is not None:
            weight_val = float(user["initial_weight_kg"])

        bmr = None
        if weight_val is not None:
            bmr, _ = _get_bmr_calories_for_user(ds, user=user, weight_info={"weight": weight_val, "date": ds})

        workout_total = 0.0
        if weight_val is not None:
            for workout in workouts_by_date.get(ds, []):
                dur = workout.get("duration")
                if dur is None:
                    dur = workout.get("duration_minutes")
                if dur is None:
                    continue
                workout_total += _estimate_workout_calories_from_duration(float(weight_val), float(dur), workout.get("type"))

        step_total = _estimate_step_calories(steps_by_date.get(ds), weight_val)
        total = None
        if bmr is not None:
            total = int(bmr + workout_total + step_total)
        elif workout_total or step_total:
            total = int(workout_total + step_total)
        series.append(total)

    return {"dates": dates, "calories": series}


def _calculate_age(dob_str: str, target_date: datetime) -> int | None:
    """Calculate age in years on a specific date."""
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
    except Exception:
        return None
    years = target_date.year - dob.year
    if (target_date.month, target_date.day) < (dob.month, dob.day):
        years -= 1
    return years if years >= 0 else None


def _get_user_timezone(user: dict) -> ZoneInfo:
    tz_name = user.get("timezone") if user else None
    try:
        return ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except Exception:
        return ZoneInfo("UTC")


def _day_fraction(date_str: str, tz: ZoneInfo) -> float:
    """Return fraction of day elapsed for date in the given timezone."""
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return 1.0
    now = datetime.now(tz)
    if target_date < now.date():
        return 1.0
    if target_date > now.date():
        return 0.0
    midnight = datetime.combine(target_date, dt_time(0, 0, 0), tzinfo=tz)
    elapsed = (now - midnight).total_seconds()
    return max(0.0, min(1.0, elapsed / 86400.0))


def _calculate_bmr(weight_kg: float, height_cm: float, age_years: int) -> float:
    """Harris-Benedict (original) BMR formula."""
    return 66.5 + (13.75 * weight_kg) + (5.003 * height_cm) - (6.75 * age_years)


def _estimate_workout_calories_from_duration(weight_kg: float, duration_min: float, workout_type: str | None) -> float:
    """Estimate workout calories using METs."""
    if weight_kg <= 0 or duration_min <= 0:
        return 0.0
    wt = (workout_type or "").lower()
    met = 6.0
    if "run" in wt:
        met = 9.8
    elif "cycle" in wt or "ride" in wt or "bike" in wt:
        met = 6.8
    elif "walk" in wt or "hike" in wt:
        met = 5.3
    elif "swim" in wt:
        met = 6.0
    elif "ski" in wt:
        met = 7.0
    elif "strength" in wt:
        met = 6.0
    hours = duration_min / 60.0
    return met * weight_kg * hours


def _fetch_exact_date_workouts(date: str, range_days: int = 14) -> list[dict]:
    """Fetch de-duplicated workouts for one date from both workout measurements."""
    if not INFLUXDB_TOKEN:
        return []
    try:
        target_dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return []

    influx = InfluxDBClient(
        url=INFLUXDB_URL,
        token=INFLUXDB_TOKEN,
        org=INFLUXDB_ORG,
        timeout=INFLUX_FAST_TIMEOUT_MS,
    )
    rows: dict[str, dict] = {}
    needed_fields = (
        'r._field == "calories" or r._field == "duration" or r._field == "duration_minutes" or '
        'r._field == "name" or r._field == "start_time" or r._field == "strava_id"'
    )
    try:
        local_query_api = influx.query_api()
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {(target_dt - timedelta(days=range_days)).strftime("%Y-%m-%dT00:00:00Z")}, stop: {(target_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "workouts" or r._measurement == "{WORKOUT_READ_MEASUREMENT}")
          |> filter(fn: (r) => {needed_fields})
          |> filter(fn: (r) => r.date == "{date}")
        '''
        for record in local_query_api.query_stream(query):
            measurement = record.values.get("_measurement", "")
            key = f"{measurement}:{record.get_time()}"
            row = rows.setdefault(
                key,
                {"date": record.values.get("date", ""), "type": record.values.get("type", "")},
            )
            row[record.get_field()] = record.get_value()
    finally:
        influx.close()

    records = sorted(rows.values(), key=lambda x: (x.get("date", ""), x.get("start_time", "")), reverse=True)
    return _dedupe_workouts(records)


def _get_workout_calories(date: str, weight_kg: float | None = None) -> float:
    """Sum workout calories for a date. If missing, estimate from duration."""
    if not INFLUXDB_TOKEN:
        return 0.0
    rows = _fetch_exact_date_workouts(date)
    if not rows:
        return 0.0

    total = 0.0
    for row in rows:
        cal = row.get("calories")
        if cal is not None and not pd.isna(cal):
            total += float(cal)
    if total > 0:
        return total

    if weight_kg is None:
        return 0.0

    # Estimate from duration + type when calories missing
    est_total = 0.0
    for row in rows:
        dur = row.get("duration")
        if dur is None or pd.isna(dur):
            dur = row.get("duration_minutes")
        if dur is not None and not pd.isna(dur):
            est_total += _estimate_workout_calories_from_duration(weight_kg, float(dur), row.get("type"))
    return est_total


def _get_bmr_calories_for_user(date: str, user: dict | None = None, weight_info: dict | None = None) -> tuple[float | None, dict]:
    """Return (bmr, meta) using user profile + weight for date."""
    if user is None:
        user = get_current_user()
    if not user:
        return None, {"reason": "no_user"}

    dob = user.get("dob")
    height_cm = user.get("height_cm")
    tz = _get_user_timezone(user)
    if not dob or not height_cm:
        return None, {"reason": "missing_profile"}

    if weight_info is None:
        weight_info = _get_weight_for_date(date, user=user)
    weight_kg = weight_info.get("weight")
    if weight_kg is None:
        weight_kg = user.get("initial_weight_kg")
    if weight_kg is None:
        return None, {"reason": "missing_weight"}

    target_dt = datetime.strptime(date, "%Y-%m-%d")
    age_years = _calculate_age(dob, target_dt)
    if age_years is None:
        return None, {"reason": "invalid_dob"}

    bmr_full = _calculate_bmr(float(weight_kg), float(height_cm), age_years)
    fraction = _day_fraction(date, tz)
    bmr = bmr_full * fraction
    return bmr, {
        "age": age_years,
        "height_cm": float(height_cm),
        "weight_kg": float(weight_kg),
        "weight_date": weight_info.get("date"),
        "timezone": str(tz),
        "day_fraction": round(fraction, 4)
    }


def _dash_fetch_calories(date: str, user: dict | None = None, weight_info: dict | None = None) -> dict:
    """Fetch calories. Thread-safe."""
    if not query_api:
        return {"calories": 0, "date": date}
    try:
        bmr, meta = _get_bmr_calories_for_user(date, user=user, weight_info=weight_info)
        weight_for_workouts = meta.get("weight_kg") if bmr is not None else None
        workout_total = _get_workout_calories(date, weight_for_workouts)
        activity = _get_daily_activity_snapshot(date)
        active_val = activity.get("active_calories")
        total_val = activity.get("total_calories")
        steps_val = activity.get("steps")
        step_total = _estimate_step_calories(steps_val, weight_for_workouts)
        if bmr is not None:
            if total_val is not None and total_val > 0:
                total = total_val
                source = "apple_health_total"
            else:
                activity_total = max(float(active_val or 0.0), float(workout_total or 0.0) + float(step_total or 0.0))
                total = bmr + activity_total if activity_total > 0 else bmr
                if active_val is not None and active_val > 0:
                    source = "bmr+apple_active"
                elif workout_total > 0 or step_total > 0:
                    source = "bmr+activity"
                else:
                    source = "bmr"
            return {
                "calories": int(total),
                "date": date,
                "source": source,
                "bmr": round(bmr, 1),
                "workout_calories": int(workout_total),
                "step_calories": int(step_total),
                "steps": int(steps_val) if steps_val is not None else None,
                "active_calories": int(active_val) if active_val is not None else None,
            }

        # Fallback to Apple Health if profile missing
        if active_val is not None:
            return {"calories": int(active_val), "date": date, "source": "apple_health_active", "missing_profile": meta}
        if total_val is not None:
            return {"calories": int(total_val), "date": date, "source": "apple_health_total", "missing_profile": meta}
        return {"calories": 0, "date": date, "source": "none", "missing_profile": meta}
    except Exception as e:
        logger.error(f"Dashboard calories error: {e}")
        return {"calories": 0, "date": date}


def _get_weight_for_date(date: str, user: dict | None = None) -> dict:
    """Fetch weight for a date with manual override and caching."""
    if user is None:
        user = get_current_user()

    profile_weight = None
    if user:
        try:
            profile_weight = float(user.get("initial_weight_kg")) if user.get("initial_weight_kg") is not None else None
        except Exception:
            profile_weight = None

    if not query_api:
        resp = {"weight": profile_weight, "date": date}
        if profile_weight is not None:
            resp["source"] = "profile"
        return resp

    now = datetime.now()
    if date in _weight_cache:
        cached, expires = _weight_cache[date]
        if now < expires:
            return cached
        del _weight_cache[date]

    try:
        target_dt = datetime.strptime(date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=42)
        stop_dt = target_dt + timedelta(days=1)
        manual_query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "manual_values")
          |> filter(fn: (r) => r.date == "{date}")
          |> filter(fn: (r) => r._field == "weight")
          |> group(columns: ["_field"])
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: 1)
        '''
        for table in query_api.query(manual_query):
            for rec in table.records:
                if rec.values.get("deleted") == "true":
                    resp = {"weight": None, "date": date}
                    _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                    return resp
                val = rec.get_value()
                if val is not None:
                    resp = {"weight": float(val), "source": "manual", "date": date}
                    _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                    return resp

        influx = InfluxDBClient(
            url=INFLUXDB_URL,
            token=INFLUXDB_TOKEN,
            org=INFLUXDB_ORG,
            timeout=INFLUX_FAST_TIMEOUT_MS,
        )
        try:
            local_query_api = influx.query_api()
            auto_query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
              |> filter(fn: (r) => r._measurement == "daily_health")
              |> filter(fn: (r) => r._field == "weight")
            '''
            candidates = []
            for rec in local_query_api.query_stream(auto_query):
                rec_date = rec.values.get("date", "")
                if not rec_date or rec_date > date:
                    continue
                val = rec.get_value()
                if val is None:
                    continue
                candidates.append((rec_date, rec.get_time(), float(val)))
            if candidates:
                rec_date, _ts, val = max(candidates, key=lambda item: (item[0], item[1]))
                resp = {"weight": val, "source": "auto", "date": rec_date}
                _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                return resp
        finally:
            influx.close()

        resp = {"weight": profile_weight, "date": date}
        if profile_weight is not None:
            resp["source"] = "profile"
        _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
        return resp
    except Exception as e:
        logger.error(f"Weight fetch error: {e}")
        resp = {"weight": profile_weight, "date": date}
        if profile_weight is not None:
            resp["source"] = "profile"
        return resp


def _dash_fetch_weight(date: str, user: dict | None = None) -> dict:
    """Fetch weight for dashboard. Thread-safe."""
    return _get_weight_for_date(date, user=user)


@app.route('/api/dashboard/quick')
@login_required
def api_dashboard_quick():
    """Phase 1: fast data - health, recommendation, calories, weight. Renders first."""
    started_at = perf_counter()
    date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    user = get_current_user()
    now = datetime.now()
    cache_key = f"quick:{date}"
    if cache_key in _dashboard_cache:
        cached, expires = _dashboard_cache[cache_key]
        if now < expires:
            return jsonify(cached)
        del _dashboard_cache[cache_key]
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(_dash_fetch_health_today, date): "health",
            ex.submit(_dash_fetch_recommendations, date, user): "recommendation",
            ex.submit(_dash_fetch_weight, date, user): "weight",
        }
        out = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                logger.error(f"Dashboard quick {key} error: {e}")
                out[key] = {"error": str(e)}
    out["calories"] = _dash_fetch_calories(date, user, weight_info=out.get("weight"))
    _dashboard_cache[cache_key] = (out, now + timedelta(seconds=DASHBOARD_CACHE_TTL_SECONDS))
    _log_perf("dashboard.quick", started_at, date=date)
    return jsonify(out)


@app.route('/api/dashboard/charts')
@login_required
def api_dashboard_charts():
    """Phase 2: charts - health history, PMC. Loads after quick."""
    started_at = perf_counter()
    date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    days = request.args.get('days', 10, type=int)
    now = datetime.now()
    cache_key = f"charts:{date}:{days}"
    if cache_key in _dashboard_cache:
        cached, expires = _dashboard_cache[cache_key]
        if now < expires:
            return jsonify(cached)
        del _dashboard_cache[cache_key]
    user = get_current_user()
    out = {}
    try:
        out["history"] = _dash_fetch_health_history(days, date)
    except Exception as e:
        logger.error(f"Dashboard charts history error: {e}")
        out["history"] = {"error": str(e)}
    try:
        out["pmc"] = _dash_fetch_pmc(days, date, user)
    except Exception as e:
        logger.error(f"Dashboard charts pmc error: {e}")
        out["pmc"] = {"error": str(e)}
    _dashboard_cache[cache_key] = (out, now + timedelta(seconds=DASHBOARD_CACHE_TTL_SECONDS))
    _log_perf("dashboard.charts", started_at, date=date, pmc_source=out.get("pmc", {}).get("source"))
    return jsonify(out)


@app.route('/api/dashboard')
@login_required
def api_dashboard():
    """Combined endpoint: all dashboard data in one response. Queries run in parallel."""
    started_at = perf_counter()
    date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    days = request.args.get('days', 10, type=int)  # 10-day window for fast loads
    user = get_current_user()
    now = datetime.now()
    cache_key = f"{date}:{days}"
    if cache_key in _dashboard_cache:
        cached, expires = _dashboard_cache[cache_key]
        if now < expires:
            return jsonify(cached)
        del _dashboard_cache[cache_key]
    out = {}
    user = get_current_user()
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            ex.submit(_dash_fetch_health_today, date): "health",
            ex.submit(_dash_fetch_health_history, days, date): "history",
            ex.submit(_dash_fetch_recommendations, date, user): "recommendation",
            ex.submit(_dash_fetch_pmc, days, date, user): "pmc",
            ex.submit(_dash_fetch_workouts, date, 10): "workouts",
            ex.submit(_dash_fetch_weight, date, user): "weight",
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                logger.error(f"Dashboard {key} error: {e}")
                out[key] = {"error": str(e)} if key != "workouts" else []
    out["calories"] = _dash_fetch_calories(date, user, weight_info=out.get("weight"))
    _dashboard_cache[cache_key] = (out, now + timedelta(seconds=DASHBOARD_CACHE_TTL_SECONDS))
    _log_perf("dashboard.full", started_at, date=date, pmc_source=out.get("pmc", {}).get("source"))
    return jsonify(out)


@app.route('/api/pmc')
@login_required
def pmc():
    """
    Get Performance Management Chart data (CTL, ATL, TSB)
    This calculates fitness, strain, and form from training load
    """
    days = request.args.get('days', 90, type=int)
    end_date_str = request.args.get('end_date', datetime.now().strftime("%Y-%m-%d"))
    try:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except ValueError:
        end_date = datetime.now().date()
        end_date_str = end_date.isoformat()
    
    is_today = end_date == datetime.now().date()
    
    # Check cache first (only use cache if querying for today)
    now = datetime.now()
    if (
        is_today
        and _pmc_cache["data"]
        and _pmc_cache["expires"]
        and now < _pmc_cache["expires"]
        and len(_pmc_cache["data"].get("pmc_series", [])) >= days
    ):
        logger.info("Returning cached PMC data")
        cached = _pmc_cache["data"]
        # Return cached data but slice to requested days
        pmc_recent = cached["pmc_series"][-days:]
        latest = pmc_recent[-1] if pmc_recent else {"ctl": 0, "atl": 0, "tsb": 0}
        user = get_current_user()
        params = _get_pmc_params_for_user(user)
        return jsonify({
            "ctl": latest["ctl"],
            "atl": latest["atl"],
            "tsb": latest["tsb"],
            "status": get_status_description(latest["tsb"]),
            "description": get_status_description(latest["tsb"]),
            "pmc_params": params,
            "source": cached.get("source", "pmc_memory_cache"),
            "chart": {
                "dates": [d["date"] for d in pmc_recent],
                "ctl": [d["ctl"] for d in pmc_recent],
                "atl": [d["atl"] for d in pmc_recent],
                "tsb": [d["tsb"] for d in pmc_recent],
            }
        })
    user = get_current_user()
    payload = _build_pmc_payload(days, end_date_str, user)
    if payload.get("error"):
        return jsonify(payload), 404
    
    # Update cache only if querying for today
    if is_today:
        _pmc_cache["data"] = {
            "pmc_series": [
                {"date": d, "ctl": c, "atl": a, "tsb": t}
                for d, c, a, t in zip(
                    payload["chart"]["dates"],
                    payload["chart"]["ctl"],
                    payload["chart"]["atl"],
                    payload["chart"]["tsb"],
                )
            ],
            "source": payload.get("source", "unknown"),
        }
        _pmc_cache["expires"] = now + timedelta(seconds=PMC_CACHE_TTL_SECONDS)
    return jsonify(payload)


@app.route('/api/trends')
def trends():
    """Get weekly/monthly trend analysis"""
    days = request.args.get('days', 7, type=int)
    
    # Get history data
    history = health_history().get_json()
    
    if "error" in history:
        return jsonify({"error": "No data"})
    
    # Calculate trends
    hrv_trend = "↑" if history["hrv"][-1] > history["hrv"][0] else "↓"
    hr_trend = "↓" if history["resting_hr"][-1] < history["resting_hr"][0] else "↑"
    
    # Weekly averages
    weekly_hrv = sum(history["hrv"][-7:]) / 7 if len(history["hrv"]) >= 7 else 0
    weekly_sleep = sum(history["sleep"][-7:]) / 7 if len(history["sleep"]) >= 7 else 0
    
    return jsonify({
        "hrv_trend": hrv_trend,
        "hr_trend": hr_trend,
        "weekly_avg_hrv": round(weekly_hrv, 1),
        "weekly_avg_sleep": round(weekly_sleep, 1),
        "training_status": "BUILD" if weekly_hrv > 35 else "RECOVERY"
    })


if __name__ == '__main__':
    today_str = datetime.now().strftime("%Y-%m-%d")
    cached_loads, stale, _source = _get_pmc_daily_loads_from_cache(PMC_MIN_LOOKBACK_DAYS, today_str)
    if getattr(strava, "is_configured", False) and (not cached_loads or stale):
        _refresh_pmc_daily_loads_async(max(365, PMC_MIN_LOOKBACK_DAYS), today_str)
    logger.info(f"Starting Health Dashboard on port {FLASK_PORT}")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)
