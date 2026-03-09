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
from collections import defaultdict
from zoneinfo import ZoneInfo
from datetime import time as dt_time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
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
RECENT_WORKOUTS_CACHE_TTL_SECONDS = 300
ENABLE_INFLUX_WORKOUT_REFRESH = os.getenv("ENABLE_INFLUX_WORKOUT_REFRESH", "1") == "1"
WORKOUT_READ_MEASUREMENT = os.getenv("WORKOUT_READ_MEASUREMENT", "workout_cache")
WORKOUT_READ_MEASUREMENTS = tuple(
    dict.fromkeys(m for m in (WORKOUT_READ_MEASUREMENT, "workouts") if m)
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
) if STRAVA_ACCESS_TOKEN else MockStravaClient()
planner = ExercisePlanner()


_workout_index_preloaded = False

# Simple in-memory cache for workouts, PMC, weight, and dashboard
_workout_cache = {"data": None, "expires": None}
_recent_workouts_cache = {"data": None, "loaded_at": None, "loading": False, "measurements": None}
_recent_workouts_lock = threading.Lock()
_pmc_cache = {"data": None, "expires": None}
_weight_cache: dict[str, tuple[dict, datetime]] = {}  # (date -> (response, expires))
_dashboard_cache: dict[str, tuple[dict, datetime]] = {}  # (date -> (response, expires))
CACHE_TTL_SECONDS = 30  # 30 seconds - quick refresh after syncing
WORKOUT_INDEX_TTL_SECONDS = 600  # 10 minutes
WORKOUT_INDEX_RANGE_DAYS = 42
_workout_index: dict[str, object] = {
    "data": None,        # list of workouts
    "loading": False,
    "loaded_at": None,
    "loading_started_at": None,
}
_workout_index_lock = threading.Lock()


def _invalidate_workout_caches() -> None:
    global _workout_cache
    _workout_cache = {"data": None, "expires": None}
    with _recent_workouts_lock:
        _recent_workouts_cache["data"] = None
        _recent_workouts_cache["loaded_at"] = None
        _recent_workouts_cache["loading"] = False
        _recent_workouts_cache["measurements"] = None
    with _workout_index_lock:
        _workout_index["data"] = None
        _workout_index["loaded_at"] = None


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
                _recent_workouts_cache["measurements"] = payload.get("measurements")
            except Exception as e:
                logger.warning(f"Failed to load recent workouts cache: {e}")


def _save_recent_workouts_cache_to_disk(data: list[dict]):
    try:
        payload = {
            "loaded_at": datetime.now().isoformat(),
            "measurements": list(WORKOUT_READ_MEASUREMENTS),
            "data": data,
        }
        RECENT_WORKOUTS_CACHE_FILE.write_text(json.dumps(payload))
    except Exception as e:
        logger.warning(f"Failed to save recent workouts cache: {e}")


def _workout_measurement_filter() -> str:
    clauses = [f'r._measurement == "{measurement}"' for measurement in WORKOUT_READ_MEASUREMENTS]
    if not clauses:
        return '|> filter(fn: (r) => false)'
    return f'|> filter(fn: (r) => {" or ".join(clauses)})'


def _fetch_workout_records(
    start_dt: datetime,
    stop_dt: datetime,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Read workouts from all configured workout measurements and de-duplicate them."""
    if not query_api:
        return []

    now = datetime.now().date()
    earliest_date = start_dt.date()
    if start_date:
        try:
            earliest_date = min(earliest_date, datetime.strptime(start_date, "%Y-%m-%d").date())
        except ValueError:
            pass
    range_days = min(max((now - earliest_date).days + 7, 42), 4000)

    date_filters = []
    if start_date:
        date_filters.append(f'r.date >= "{start_date}"')
    if end_date:
        date_filters.append(f'r.date <= "{end_date}"')
    date_filter = ""
    if date_filters:
        date_filter = f'|> filter(fn: (r) => {" and ".join(date_filters)})'

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{range_days}d)
      {_workout_measurement_filter()}
      {date_filter}
    '''

    workouts = {}
    for record in query_api.query_stream(query):
        key = f'{record.values.get("_measurement", "")}|{record.get_time()}'
        entry = workouts.setdefault(
            key,
            {
                "date": record.values.get("date", ""),
                "type": record.values.get("type", ""),
            },
        )
        entry[record.get_field()] = record.get_value()

    if not workouts:
        return []

    records = list(workouts.values())
    records = sorted(records, key=lambda x: (x.get("date", ""), x.get("start_time", "")), reverse=True)
    return _dedupe_workouts(records)


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
                _recent_workouts_cache["measurements"] = list(WORKOUT_READ_MEASUREMENTS)
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
    cutoff = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
    start_dt = datetime.now() - timedelta(days=14)
    stop_dt = datetime.now() + timedelta(days=1)
    try:
        records = _fetch_workout_records(start_dt, stop_dt, start_date=cutoff, end_date=before_date)
    except Exception:
        return []
    return records[:limit]


def _get_recent_workouts_from_cache(before_date: str | None, limit: int):
    _load_recent_workouts_cache_from_disk()
    with _recent_workouts_lock:
        data = _recent_workouts_cache.get("data") or []
        loaded_at = _recent_workouts_cache.get("loaded_at")
        loading = _recent_workouts_cache.get("loading")
        measurements = _recent_workouts_cache.get("measurements")

    if not data:
        return None, False

    target = before_date or datetime.now().strftime("%Y-%m-%d")
    filtered = [w for w in data if w.get("date", "") <= target]
    filtered = sorted(filtered, key=lambda x: (x.get("date", ""), x.get("start_time", "")), reverse=True)
    stale = True
    if loaded_at and (datetime.now() - loaded_at).total_seconds() < RECENT_WORKOUTS_CACHE_TTL_SECONDS:
        stale = False
    if measurements != list(WORKOUT_READ_MEASUREMENTS):
        stale = True
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
PMC_MIN_LOOKBACK_DAYS = 365
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
    global _pmc_cache, _weight_cache, _dashboard_cache, _workout_index
    _invalidate_workout_caches()
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
            c for c in ["sleep_duration_hours", "hrv_avg", "resting_hr", "steps", "recovery_score", "training_load", "weight"]
            if c in df.columns
        ]
        for c in numeric_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        if numeric_cols:
            df = df.groupby("date", as_index=False)[numeric_cols].mean()
        else:
            df = df[["date"]].drop_duplicates()

        df = df.sort_values("date")
        
        # Try to get data for the target date, fall back to latest
        target_row = df[df['date'] == target_date]
        if not target_row.empty:
            row = target_row.iloc[0]
        else:
            row = df.iloc[-1]

        def clean_number(val):
            return None if pd.isna(val) else float(val)

        def get_value(col):
            if col not in row.index:
                return None
            return clean_number(row[col])

        steps_val = get_value("steps")
        steps_clean = None if steps_val is None else int(float(steps_val))
        weight_val = get_value("weight")

        out = {
            "date": row.get("date", target_date),
            "sleep_hours": get_value("sleep_duration_hours"),
            "hrv": get_value("hrv_avg"),
            "resting_hr": get_value("resting_hr"),
            "steps": steps_clean,
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
        return jsonify(_build_health_history_response(days, end_date, user=get_current_user(), include_calories=True))
    except Exception as e:
        logger.error(f"Error fetching health history: {e}")
        return jsonify({"error": str(e)}), 500


def _fetch_workouts_from_influx(before_date: str | None = None):
    """Fetch workouts from InfluxDB. Filter by date tag (not _time - points use write time)."""
    now = datetime.now()
    days_back = WORKOUT_LOOKBACK_DAYS
    cutoff = (now - timedelta(days=days_back)).strftime('%Y-%m-%d')
    if before_date:
        try:
            datetime.strptime(before_date, "%Y-%m-%d")
        except ValueError:
            before_date = None
    start_dt = now - timedelta(days=days_back)
    stop_dt = now + timedelta(days=1)
    return _fetch_workout_records(start_dt, stop_dt, start_date=cutoff, end_date=before_date)


def _workout_dedupe_key(workout: dict) -> str:
    """Return a stable key to de-duplicate workouts from multiple sources."""
    strava_id = workout.get("strava_id")
    if strava_id:
        return f"strava:{strava_id}"
    return "|".join([
        str(workout.get("date", "")),
        str(workout.get("start_time", "")),
        str(workout.get("name", "")),
        str(workout.get("type", "")),
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
    """Fetch only the most recent workouts (limited) using stream query."""
    if not query_api:
        return []
    if before_date:
        try:
            target_date = datetime.strptime(before_date, "%Y-%m-%d").date()
        except ValueError:
            target_date = datetime.now().date()
        cutoff_date = target_date - timedelta(days=42)
    else:
        cutoff_date = datetime.now().date() - timedelta(days=42)

    start_dt = datetime.combine(cutoff_date, dt_time(0, 0, 0))
    stop_dt = datetime.now() + timedelta(days=1)
    records = _fetch_workout_records(
        start_dt,
        stop_dt,
        start_date=cutoff_date.isoformat(),
        end_date=before_date,
    )
    return records[:limit]


def _load_workout_index() -> None:
    """Background load of all workouts into memory for fast filtering."""
    if not query_api:
        with _workout_index_lock:
            _workout_index["loading"] = False
        return

    cutoff = (datetime.now() - timedelta(days=WORKOUT_INDEX_RANGE_DAYS)).strftime('%Y-%m-%d')
    try:
        data = _fetch_workout_records(
            datetime.now() - timedelta(days=WORKOUT_INDEX_RANGE_DAYS),
            datetime.now() + timedelta(days=1),
            start_date=cutoff,
        )
    except Exception as e:
        logger.error(f"Error loading workout index: {e}")
        with _workout_index_lock:
            _workout_index["loading"] = False
        return

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
            # If requesting an old date, bypass recent cache and query directly
            if before_date:
                try:
                    target_date = datetime.strptime(before_date, "%Y-%m-%d").date()
                    if (datetime.now().date() - target_date).days > WORKOUT_LOOKBACK_DAYS:
                        records = _fetch_workouts_limited(before_date, limit or 10)
                        return jsonify(records)
                except ValueError:
                    pass

            # Fast path for dashboard: serve from recent cache, refresh in background
            if before_date and limit and limit <= 10:
                cached, stale = _get_recent_workouts_from_cache(before_date, limit)
                if cached is not None:
                    if stale:
                        _refresh_recent_workouts_cache_async(before_date)
                    resp = jsonify(cached)
                    if stale:
                        resp.headers["X-Workouts-Stale"] = "true"
                    return resp
                _refresh_recent_workouts_cache_async(before_date)
                resp = jsonify([])
                if ENABLE_INFLUX_WORKOUT_REFRESH:
                    resp.headers["X-Workouts-Stale"] = "true"
                    resp.headers["Retry-After"] = "5"
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
        workout_date = data.get("date", datetime.now().strftime("%Y-%m-%d"))
        workout_time = data.get("start_time") or data.get("time") or ""
        target_dt = datetime.now()
        if workout_date and workout_time:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    target_dt = datetime.strptime(f"{workout_date} {workout_time}", fmt)
                    break
                except ValueError:
                    continue
        point = Point("workouts")\
            .tag("type", data.get("type", "Unknown"))\
            .tag("date", workout_date)\
            .field("name", data.get("name", data.get("type", "Unknown")))\
            .field("start_time", workout_time)\
            .field("duration_minutes", float(data.get("duration", 0)))\
            .field("duration", float(data.get("duration", 0)))\
            .field("avg_hr", float(data.get("avg_hr", 0)))\
            .field("max_hr", float(data.get("max_hr", 0)))\
            .field("calories", int(data.get("calories", 0)))\
            .field("intensity", float(data.get("intensity", 5)))\
            .field("feeling", data.get("feeling", "okay"))\
            .time(target_dt)
        cache_point = Point("workout_cache")\
            .tag("type", data.get("type", "Unknown"))\
            .tag("date", workout_date)\
            .field("name", data.get("name", data.get("type", "Unknown")))\
            .field("start_time", workout_time)\
            .field("duration_minutes", float(data.get("duration", 0)))\
            .field("duration", float(data.get("duration", 0)))\
            .field("avg_hr", float(data.get("avg_hr", 0)))\
            .field("max_hr", float(data.get("max_hr", 0)))\
            .field("calories", int(data.get("calories", 0)))\
            .field("intensity", float(data.get("intensity", 5)))\
            .field("feeling", data.get("feeling", "okay"))\
            .time(target_dt)

        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=[point, cache_point])
        _invalidate_workout_caches()
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


@app.route('/api/calories')
@login_required
def calories():
    """Get calories burned for today (default) or specified date.
    
    Sources (in priority order):
    1. Manual override
    2. BMR + workouts + step estimate
    3. Apple Health active/total calories
    4. Workout/step activity-only fallback
    """
    date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    
    if not query_api:
        return jsonify({"calories": 0, "date": date})
    
    try:
        return jsonify(_fetch_calorie_breakdown_for_date(date, user=get_current_user()))
    except Exception as e:
        logger.error(f"Error fetching calories: {e}")
        return jsonify({"calories": 0, "date": date, "error": str(e)})


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
            def to_float(value, default=0.0):
                try:
                    return float(value) if value is not None else default
                except (TypeError, ValueError):
                    return default

            for activity in activities:
                workout_date = activity.get("date", "")
                activity_ts = datetime.strptime(workout_date, "%Y-%m-%d").replace(hour=12, minute=0, second=0) if workout_date else datetime.now()
                if activity.get("time"):
                    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                        try:
                            activity_ts = datetime.strptime(f"{workout_date} {activity.get('time')}", fmt)
                            break
                        except ValueError:
                            continue

                point = Point("workouts")\
                    .tag("type", activity.get("type", "Unknown"))\
                    .tag("date", workout_date)\
                    .field("strava_id", str(activity.get("id", "")))\
                    .field("name", activity.get("name", ""))\
                    .field("start_time", activity.get("time", ""))\
                    .field("duration_minutes", to_float(activity.get("duration")))\
                    .field("duration", to_float(activity.get("duration")))\
                    .field("distance", to_float(activity.get("distance")))\
                    .field("elevation_gain", to_float(activity.get("elevation_gain")))\
                    .field("avg_hr", to_float(activity.get("avg_hr")))\
                    .field("max_hr", to_float(activity.get("max_hr")))\
                    .field("suffer_score", to_float(activity.get("suffer_score")))\
                    .field("calories", to_float(activity.get("calories")))\
                    .field("feeling", activity.get("feeling", "good"))\
                    .time(activity_ts)
                cache_point = Point("workout_cache")\
                    .tag("type", activity.get("type", "Unknown"))\
                    .tag("date", workout_date)\
                    .field("strava_id", str(activity.get("id", "")))\
                    .field("name", activity.get("name", ""))\
                    .field("start_time", activity.get("time", ""))\
                    .field("duration_minutes", to_float(activity.get("duration")))\
                    .field("duration", to_float(activity.get("duration")))\
                    .field("distance", to_float(activity.get("distance")))\
                    .field("elevation_gain", to_float(activity.get("elevation_gain")))\
                    .field("avg_hr", to_float(activity.get("avg_hr")))\
                    .field("max_hr", to_float(activity.get("max_hr")))\
                    .field("suffer_score", to_float(activity.get("suffer_score")))\
                    .field("calories", to_float(activity.get("calories")))\
                    .field("feeling", activity.get("feeling", "good"))\
                    .time(activity_ts)

                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=[point, cache_point])
            _invalidate_workout_caches()
        
        logger.info(f"Synced {len(activities) if activities else 0} activities to InfluxDB")
        return jsonify({"synced": len(activities) if activities else 0, "data": activities})
    except Exception as e:
        logger.error(f"Error syncing Strava: {e}")
        return jsonify({"error": str(e)}), 500


def _fetch_daily_loads_from_influx(query_days=120):
    """Fetch daily training loads from InfluxDB.

    Reads the canonical workout cache and sums daily suffer_score
    (Strava Relative Effort).
    """
    from collections import defaultdict

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{query_days}d)
      |> filter(fn: (r) => r._measurement == "{WORKOUT_READ_MEASUREMENT}")
      |> filter(fn: (r) => r._field == "suffer_score")
    '''

    tables = query_api.query_stream(query)
    by_date = defaultdict(float)
    for record in tables:
        date = record.values.get('date', '')
        load = record.get_value() or 0
        if date:
            by_date[date] += float(load)

    return [{"date": d, "load": l} for d, l in sorted(by_date.items())]

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
        numeric_cols = [c for c in ["sleep_duration_hours", "hrv_avg", "resting_hr", "steps", "recovery_score", "training_load", "weight"] if c in df.columns]
        for c in numeric_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        if numeric_cols:
            df = df.groupby("date", as_index=False)[numeric_cols].mean()
        else:
            df = df[["date"]].drop_duplicates()
        df = df.sort_values("date")
        target_row = df[df['date'] == target_date]
        row = target_row.iloc[0] if not target_row.empty else df.iloc[-1]
        def clean(v):
            return None if pd.isna(v) else float(v)
        def get(col):
            return clean(row[col]) if col in row.index else None
        steps_val = get("steps")
        return {
            "date": row.get("date", target_date),
            "sleep_hours": get("sleep_duration_hours"),
            "hrv": get("hrv_avg"),
            "resting_hr": get("resting_hr"),
            "steps": None if steps_val is None else int(float(steps_val)),
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
        return _build_health_history_response(days, end_date, include_calories=False)
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


def _dash_fetch_pmc(days: int, end_date_str: str, user: dict | None = None) -> dict:
    """Fetch PMC data. Thread-safe."""
    if not query_api:
        return {"error": "No training load data from InfluxDB"}
    try:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        query_days = max(days + 42, PMC_MIN_LOOKBACK_DAYS)
        daily_loads = _fetch_daily_loads_from_influx(query_days)
        if not daily_loads:
            return {"error": "No training load data from InfluxDB"}
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
        return {
            "ctl": latest["ctl"], "atl": latest["atl"], "tsb": latest["tsb"],
            "status": get_status_description(latest["tsb"]),
            "description": get_status_description(latest["tsb"]),
            "pmc_params": params,
            "days_tracked": len(full_series),
            "chart": {
                "dates": [d["date"] for d in pmc_recent],
                "ctl": [d["ctl"] for d in pmc_recent],
                "atl": [d["atl"] for d in pmc_recent],
                "tsb": [d["tsb"] for d in pmc_recent],
            }
        }
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


def _coerce_float(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_query_data_frame(result) -> pd.DataFrame:
    if isinstance(result, list):
        frames = [frame for frame in result if isinstance(frame, pd.DataFrame) and not frame.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)
    if isinstance(result, pd.DataFrame):
        return result
    return pd.DataFrame()


def _fetch_daily_health_frame(start_dt: datetime, stop_dt: datetime) -> pd.DataFrame:
    if not query_api:
        return pd.DataFrame()
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
      |> filter(fn: (r) => r._measurement == "daily_health")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    result = _normalize_query_data_frame(query_api.query_data_frame(query))
    if result.empty or "date" not in result.columns:
        return pd.DataFrame()

    df = result.copy()
    numeric_cols = [
        c for c in [
            "sleep_duration_hours",
            "hrv_avg",
            "resting_hr",
            "steps",
            "recovery_score",
            "training_load",
            "weight",
            "active_calories",
            "total_calories",
        ]
        if c in df.columns
    ]
    for column in numeric_cols:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    if numeric_cols:
        df = df.groupby("date", as_index=False)[numeric_cols].mean()
    else:
        df = df[["date"]].drop_duplicates()
    return df.sort_values("date")


def _fetch_manual_history(start_dt: datetime, stop_dt: datetime, fields: list[str]) -> dict[str, dict[str, float]]:
    manual_data = {field: {} for field in fields}
    if not query_api or not fields:
        return manual_data

    field_filter = " or ".join(f'r._field == "{field}"' for field in fields)
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
      |> filter(fn: (r) => r._measurement == "manual_values")
      |> filter(fn: (r) => {field_filter})
    '''

    latest: dict[tuple[str, str], tuple[datetime, float | None]] = {}
    for table in query_api.query(query):
        for record in table.records:
            date = record.values.get("date", "")
            field = record.get_field()
            if not date or field not in manual_data:
                continue
            ts = record.get_time() or datetime.min
            is_deleted = str(record.values.get("deleted", "")).lower() == "true"
            value = None if is_deleted else _coerce_float(record.get_value())
            key = (date, field)
            if key not in latest or ts > latest[key][0]:
                latest[key] = (ts, value)

    for (date, field), (_, value) in latest.items():
        if value is not None:
            manual_data[field][date] = value
    return manual_data


def _round_optional(value, digits: int = 2):
    value = _coerce_float(value)
    if value is None:
        return None
    rounded = round(value, digits)
    if digits == 0:
        return int(rounded)
    return rounded


def _merge_daily_series(
    dates_list: list[str],
    auto_map: dict[str, float] | None,
    manual_map: dict[str, float] | None = None,
    digits: int = 2,
) -> list[float | int | None]:
    auto_map = auto_map or {}
    manual_map = manual_map or {}
    out = []
    for date in dates_list:
        if date in manual_map:
            out.append(_round_optional(manual_map.get(date), digits))
        else:
            out.append(_round_optional(auto_map.get(date), digits))
    return out


def _fetch_workout_rows_by_date(
    start_dt: datetime,
    stop_dt: datetime,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in _fetch_workout_records(start_dt, stop_dt, start_date=start_date, end_date=end_date):
        date = record.get("date")
        if date:
            grouped[date].append(record)
    return grouped


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


def _estimate_step_calories(steps: float | None, weight_kg: float | None, height_cm: float | None = None) -> float:
    """Estimate walking calories from daily step count."""
    if steps is None or weight_kg is None or weight_kg <= 0:
        return 0.0
    if steps <= 0:
        return 0.0
    height_m = (height_cm / 100.0) if height_cm and height_cm > 0 else None
    step_length_m = (height_m * 0.415) if height_m else 0.75
    step_length_m = min(max(step_length_m, 0.45), 0.90)
    distance_km = float(steps) * step_length_m / 1000.0
    return 0.53 * weight_kg * distance_km


def _calculate_workout_calories_for_rows(rows: list[dict], weight_kg: float | None = None) -> float:
    total = 0.0
    for row in rows:
        calories = _coerce_float(row.get("calories"))
        if calories is not None and calories > 0:
            total += calories
            continue
        duration = _coerce_float(row.get("duration"))
        if duration is None:
            duration = _coerce_float(row.get("duration_minutes"))
        if weight_kg is not None and duration is not None and duration > 0:
            total += _estimate_workout_calories_from_duration(weight_kg, duration, row.get("type"))
    return total


def _get_calorie_breakdown(
    date: str,
    user: dict | None = None,
    daily_row: dict | None = None,
    manual_data: dict[str, dict[str, float]] | None = None,
    workout_rows: list[dict] | None = None,
) -> dict:
    user = user or get_current_user() or {}
    manual_data = manual_data or {}
    daily_row = daily_row or {}
    workout_rows = workout_rows or []

    manual_calories = manual_data.get("calories", {}).get(date)
    if manual_calories is not None:
        return {"calories": int(round(manual_calories)), "date": date, "source": "manual"}

    weight_kg = manual_data.get("weight", {}).get(date)
    if weight_kg is None:
        weight_kg = _coerce_float(daily_row.get("weight"))
    if weight_kg is None:
        weight_info = _get_weight_for_date(date)
        weight_kg = _coerce_float(weight_info.get("weight"))
    if weight_kg is None:
        weight_kg = _coerce_float(user.get("initial_weight_kg"))

    height_cm = _coerce_float(user.get("height_cm"))
    steps = manual_data.get("steps", {}).get(date)
    if steps is None:
        steps = _coerce_float(daily_row.get("steps"))

    workout_total = _calculate_workout_calories_for_rows(workout_rows, weight_kg)
    step_total = _estimate_step_calories(steps, weight_kg, height_cm)

    dob = user.get("dob")
    if dob and height_cm is not None and weight_kg is not None:
        tz = _get_user_timezone(user)
        target_dt = datetime.strptime(date, "%Y-%m-%d")
        age_years = _calculate_age(dob, target_dt)
        if age_years is not None:
            day_fraction = _day_fraction(date, tz)
            bmr = _calculate_bmr(weight_kg, height_cm, age_years) * day_fraction
            total = bmr + workout_total + step_total
            source_parts = ["bmr"]
            if workout_total > 0:
                source_parts.append("workouts")
            if step_total > 0:
                source_parts.append("steps")
            return {
                "calories": int(round(total)),
                "date": date,
                "source": "+".join(source_parts),
                "bmr": round(bmr, 1),
                "workout_calories": int(round(workout_total)),
                "step_calories": int(round(step_total)),
            }

    active_val = _coerce_float(daily_row.get("active_calories"))
    total_val = _coerce_float(daily_row.get("total_calories"))
    if active_val is not None:
        return {"calories": int(round(active_val)), "date": date, "source": "apple_health_active"}
    if total_val is not None:
        return {"calories": int(round(total_val)), "date": date, "source": "apple_health_total"}

    activity_total = workout_total + step_total
    if activity_total > 0:
        source_parts = []
        if workout_total > 0:
            source_parts.append("workouts")
        if step_total > 0:
            source_parts.append("steps")
        return {
            "calories": int(round(activity_total)),
            "date": date,
            "source": "+".join(source_parts) or "activity",
            "workout_calories": int(round(workout_total)),
            "step_calories": int(round(step_total)),
        }

    return {"calories": 0, "date": date, "source": "none"}


def _build_health_history_response(
    days: int,
    end_date: str,
    user: dict | None = None,
    include_calories: bool = True,
) -> dict:
    days = max(1, min(int(days or 30), 365))
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=days - 1)
    query_start_dt = start_dt - timedelta(days=WEIGHT_LOOKBACK_DAYS)
    stop_dt = end_dt + timedelta(days=1)
    dates_list = [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    daily_df = _fetch_daily_health_frame(query_start_dt, stop_dt)
    if not daily_df.empty:
        daily_df = daily_df[daily_df["date"].isin(dates_list)]
        daily_df = pd.DataFrame({"date": dates_list}).merge(daily_df, on="date", how="left")
    else:
        daily_df = pd.DataFrame({"date": dates_list})

    manual_data = _fetch_manual_history(
        query_start_dt,
        stop_dt,
        ["weight", "hrv", "sleep", "resting_hr", "steps", "calories"],
    )

    auto_maps = {
        field: dict(zip(daily_df["date"], daily_df[field])) if field in daily_df else {}
        for field in [
            "hrv_avg",
            "resting_hr",
            "sleep_duration_hours",
            "recovery_score",
            "steps",
            "weight",
        ]
    }

    payload = {
        "dates": dates_list,
        "hrv": _merge_daily_series(dates_list, auto_maps.get("hrv_avg"), manual_data.get("hrv"), 2),
        "resting_hr": _merge_daily_series(dates_list, auto_maps.get("resting_hr"), manual_data.get("resting_hr"), 2),
        "sleep": _merge_daily_series(dates_list, auto_maps.get("sleep_duration_hours"), manual_data.get("sleep"), 2),
        "recovery": _merge_daily_series(dates_list, auto_maps.get("recovery_score"), digits=1),
        "steps": _merge_daily_series(dates_list, auto_maps.get("steps"), manual_data.get("steps"), 0),
        "weight": _merge_daily_series(dates_list, auto_maps.get("weight"), manual_data.get("weight"), 2),
    }

    if include_calories:
        workouts_by_date = _fetch_workout_rows_by_date(query_start_dt, stop_dt, dates_list[0], dates_list[-1])
        daily_rows = {row["date"]: row for row in daily_df.to_dict(orient="records")}
        payload["calories"] = [
            _get_calorie_breakdown(
                date,
                user=user,
                daily_row=daily_rows.get(date),
                manual_data=manual_data,
                workout_rows=workouts_by_date.get(date, []),
            ).get("calories")
            for date in dates_list
        ]

    return payload


def _fetch_calorie_breakdown_for_date(date: str, user: dict | None = None) -> dict:
    target_dt = datetime.strptime(date, "%Y-%m-%d")
    query_start_dt = target_dt - timedelta(days=WEIGHT_LOOKBACK_DAYS)
    stop_dt = target_dt + timedelta(days=1)

    daily_df = _fetch_daily_health_frame(query_start_dt, stop_dt)
    daily_row = {}
    if not daily_df.empty:
        match = daily_df[daily_df["date"] == date]
        if not match.empty:
            daily_row = match.iloc[-1].to_dict()

    manual_data = _fetch_manual_history(query_start_dt, stop_dt, ["weight", "steps", "calories"])
    workout_rows = _fetch_workout_rows_by_date(target_dt, target_dt + timedelta(days=1), date, date).get(date, [])
    return _get_calorie_breakdown(date, user=user, daily_row=daily_row, manual_data=manual_data, workout_rows=workout_rows)


def _get_workout_calories(date: str, weight_kg: float | None = None) -> float:
    """Sum workout calories for a date. If missing, estimate from duration."""
    if not query_api:
        return 0.0
    target_dt = datetime.strptime(date, "%Y-%m-%d")
    rows = _fetch_workout_records(target_dt, target_dt + timedelta(days=1), start_date=date, end_date=date)
    return _calculate_workout_calories_for_rows(rows, weight_kg)


def _get_bmr_calories_for_user(date: str, user: dict | None = None) -> tuple[float | None, dict]:
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

    weight_info = _get_weight_for_date(date)
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


def _dash_fetch_calories(date: str, user: dict | None = None) -> dict:
    """Fetch calories. Thread-safe."""
    if not query_api:
        return {"calories": 0, "date": date}
    try:
        return _fetch_calorie_breakdown_for_date(date, user=user)
    except Exception as e:
        logger.error(f"Dashboard calories error: {e}")
        return {"calories": 0, "date": date}


def _get_weight_for_date(date: str) -> dict:
    """Fetch weight for a date with manual override and caching."""
    if not query_api:
        return {"weight": None, "date": date}

    now = datetime.now()
    if date in _weight_cache:
        cached, expires = _weight_cache[date]
        if now < expires:
            return cached
        del _weight_cache[date]

    try:
        target_dt = datetime.strptime(date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=7)
        weight_start_dt = target_dt - timedelta(days=WEIGHT_LOOKBACK_DAYS)
        stop_dt = target_dt + timedelta(days=1)

        # 1) Manual for this date
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "manual_values")
          |> filter(fn: (r) => r._field == "weight")
          |> filter(fn: (r) => r.date == "{date}")
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: 1)
          |> filter(fn: (r) => r.deleted != "true")
        '''
        for table in query_api.query(query):
            for rec in table.records:
                v = rec.get_value()
                if v is not None:
                    resp = {"weight": float(v), "source": "manual", "date": date, "source_date": date}
                    _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                    return resp

        # 2) daily_health for this date
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r._field == "weight")
          |> filter(fn: (r) => r.date == "{date}")
          |> last()
        '''
        for table in query_api.query(query):
            for rec in table.records:
                v = rec.get_value()
                if v is not None:
                    resp = {"weight": float(v), "source": "auto", "date": date, "source_date": date}
                    _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                    return resp

        # 3) Most recent daily_health on/before date (42-day window)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {weight_start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r._field == "weight")
          |> filter(fn: (r) => r.date <= "{date}")
          |> group()
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: 1)
        '''
        for table in query_api.query(query):
            for rec in table.records:
                v = rec.get_value()
                if v is not None:
                    resp = {
                        "weight": float(v),
                        "source": "auto",
                        "date": date,
                        "source_date": rec.values.get("date", date),
                    }
                    _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                    return resp

        # 4) Most recent manual on/before date (42-day window)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {weight_start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "manual_values")
          |> filter(fn: (r) => r._field == "weight")
          |> filter(fn: (r) => r.date <= "{date}")
          |> group()
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: 1)
          |> filter(fn: (r) => r.deleted != "true")
        '''
        for table in query_api.query(query):
            for rec in table.records:
                v = rec.get_value()
                if v is not None:
                    resp = {
                        "weight": float(v),
                        "source": "manual",
                        "date": date,
                        "source_date": rec.values.get("date", date),
                    }
                    _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                    return resp

        resp = {"weight": None, "date": date, "source_date": None}
        _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
        return resp
    except Exception as e:
        logger.error(f"Weight fetch error: {e}")
        return {"weight": None, "date": date, "source_date": None}


def _dash_fetch_weight(date: str) -> dict:
    """Fetch weight for dashboard. Thread-safe."""
    return _get_weight_for_date(date)


@app.route('/api/dashboard/quick')
@login_required
def api_dashboard_quick():
    """Phase 1: fast data - health, recommendation, calories, weight. Renders first."""
    date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    user = get_current_user()
    now = datetime.now()
    cache_key = f"quick:{date}"
    if cache_key in _dashboard_cache:
        cached, expires = _dashboard_cache[cache_key]
        if now < expires:
            return jsonify(cached)
        del _dashboard_cache[cache_key]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            ex.submit(_dash_fetch_health_today, date): "health",
            ex.submit(_dash_fetch_recommendations, date, user): "recommendation",
            ex.submit(_dash_fetch_calories, date, user): "calories",
            ex.submit(_dash_fetch_weight, date): "weight",
        }
        out = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                logger.error(f"Dashboard quick {key} error: {e}")
                out[key] = {"error": str(e)}
    _dashboard_cache[cache_key] = (out, now + timedelta(seconds=CACHE_TTL_SECONDS))
    return jsonify(out)


@app.route('/api/dashboard/charts')
@login_required
def api_dashboard_charts():
    """Phase 2: charts - health history, PMC. Loads after quick."""
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
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(_dash_fetch_health_history, days, date): "history",
            ex.submit(_dash_fetch_pmc, days, date, user): "pmc",
        }
        out = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                logger.error(f"Dashboard charts {key} error: {e}")
                out[key] = {"error": str(e)}
    _dashboard_cache[cache_key] = (out, now + timedelta(seconds=CACHE_TTL_SECONDS))
    return jsonify(out)


@app.route('/api/dashboard')
@login_required
def api_dashboard():
    """Combined endpoint: all dashboard data in one response. Queries run in parallel."""
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
    with ThreadPoolExecutor(max_workers=7) as ex:
        futures = {
            ex.submit(_dash_fetch_health_today, date): "health",
            ex.submit(_dash_fetch_health_history, days, date): "history",
            ex.submit(_dash_fetch_recommendations, date, user): "recommendation",
            ex.submit(_dash_fetch_pmc, days, date, user): "pmc",
            ex.submit(_dash_fetch_workouts, date, 10): "workouts",
            ex.submit(_dash_fetch_calories, date, user): "calories",
            ex.submit(_dash_fetch_weight, date): "weight",
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                logger.error(f"Dashboard {key} error: {e}")
                out[key] = {"error": str(e)} if key != "workouts" else []
    _dashboard_cache[cache_key] = (out, now + timedelta(seconds=CACHE_TTL_SECONDS))
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
    query_days = max(days + 42, PMC_MIN_LOOKBACK_DAYS)  # Smaller window for speed
    
    # Parse end_date
    try:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except ValueError:
        end_date = datetime.now().date()
    
    is_today = end_date == datetime.now().date()
    
    # Check cache first (only use cache if querying for today)
    now = datetime.now()
    if is_today and _pmc_cache["data"] and _pmc_cache["expires"] and now < _pmc_cache["expires"]:
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
            "pmc_params": params,
            "chart": {
                "dates": [d["date"] for d in pmc_recent],
                "ctl": [d["ctl"] for d in pmc_recent],
                "atl": [d["atl"] for d in pmc_recent],
                "tsb": [d["tsb"] for d in pmc_recent],
            }
        })
    
    logger.info("Fetching PMC data from InfluxDB")
    daily_loads = []
    
    if query_api:
        try:
            daily_loads = _fetch_daily_loads_from_influx(query_days)
            logger.info(f"Loaded {len(daily_loads)} days of training load from InfluxDB")
        except Exception as e:
            logger.error(f"Error fetching PMC data from InfluxDB: {e}")
    
    # No mock data - return error if nothing from InfluxDB
    if not daily_loads:
        return jsonify({"error": "No training load data from InfluxDB"}), 404
    
    # Build continuous daily load series (fill missing days with zero load),
    # then compute CTL/ATL/TSB series for charting.
    loads_map = {d["date"]: float(d.get("load", 0.0)) for d in daily_loads}
    start_date = end_date - timedelta(days=query_days - 1)

    full_series = []
    cur = start_date
    while cur <= end_date:
        ds = cur.isoformat()
        full_series.append({"date": ds, "load": loads_map.get(ds, 0.0)})
        cur += timedelta(days=1)

    user = get_current_user()
    params = _get_pmc_params_for_user(user)
    pmc_series = calculate_pmc_series(
        full_series,
        ctl_days=params["ctl_days"],
        atl_days=params["atl_days"],
        load_scale_factor=params["load_scale_factor"],
        tsb_lag_days=params.get("tsb_lag_days", 0),
        seed_mode=params.get("seed_mode", "zeros"),
    )
    
    # Update cache only if querying for today
    if is_today:
        _pmc_cache["data"] = {"pmc_series": pmc_series}
        _pmc_cache["expires"] = now + timedelta(seconds=CACHE_TTL_SECONDS)

    pmc_recent = pmc_series[-days:]
    latest = pmc_recent[-1] if pmc_recent else {"ctl": 0, "atl": 0, "tsb": 0}
    status = get_status_description(latest["tsb"])
    
    return jsonify({
        "ctl": latest["ctl"],
        "atl": latest["atl"],
        "tsb": latest["tsb"],
        "status": status,
        "description": status,
        "pmc_params": params,
        "days_tracked": len(full_series),
        "chart": {
            "dates": [d["date"] for d in pmc_recent],
            "ctl": [d["ctl"] for d in pmc_recent],
            "atl": [d["atl"] for d in pmc_recent],
            "tsb": [d["tsb"] for d in pmc_recent],
        }
    })


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
    logger.info(f"Starting Health Dashboard on port {FLASK_PORT}")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)
