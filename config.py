"""
Configuration for Health Dashboard
Set these environment variables or edit secrets.json

SMTP configuration is loaded from smtp_config.json (see smtp_config.json.example)
"""

import os
import json
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load secrets from json file if exists
SECRETS_FILE = Path(__file__).parent / "secrets.json"
secrets = {}
if SECRETS_FILE.exists():
    with open(SECRETS_FILE) as f:
        secrets = json.load(f)

# Load SMTP config from separate file
SMTP_CONFIG_FILE = Path(__file__).parent / "smtp_config.json"
smtp_config = {}
if SMTP_CONFIG_FILE.exists():
    with open(SMTP_CONFIG_FILE) as f:
        smtp_config = json.load(f)
    logger.info("SMTP configuration loaded from smtp_config.json")

def get_secret(key, default=''):
    """Get secret from env var or secrets file"""
    # Support conventional uppercase env vars (e.g. INFLUXDB_TOKEN) while keeping
    # existing secrets.json keys (e.g. influxdb_token).
    return os.getenv(key) or os.getenv(key.upper()) or secrets.get(key, default)

def get_smtp_config(key, default=''):
    """Get SMTP config from env var or smtp_config.json"""
    return os.getenv(key.upper()) or smtp_config.get(key, default)

# InfluxDB Configuration
INFLUXDB_URL = os.getenv('INFLUXDB_URL', get_secret('influxdb_url', 'http://influxdb:8086'))
logger.info(f"Using InfluxDB URL: {INFLUXDB_URL}")
INFLUXDB_TOKEN = get_secret('influxdb_token', '')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG', 'auroran')
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET', 'health')

# Suunto API Configuration
SUUNTO_CLIENT_ID = get_secret('suunto_client_id', '')
SUUNTO_CLIENT_SECRET = get_secret('suunto_client_secret', '')

# Strava API Configuration
STRAVA_ACCESS_TOKEN = get_secret('strava_access_token', '')
STRAVA_CLIENT_ID = get_secret('strava_client_id', '')
STRAVA_CLIENT_SECRET = get_secret('strava_client_secret', '')
STRAVA_REFRESH_TOKEN = get_secret('strava_refresh_token', '')
logger.info(f"Strava token configured: {bool(STRAVA_ACCESS_TOKEN)}")
logger.info(f"Strava refresh configured: {bool(STRAVA_REFRESH_TOKEN)}")

# Garmin (no personal API - sync via Strava recommended)
GARMIN_USERNAME = get_secret('garmin_username', '')
GARMIN_PASSWORD = get_secret('garmin_password', '')

# Flask Configuration
FLASK_HOST = os.getenv('FLASK_HOST', '0.0.0.0')
FLASK_PORT = int(os.getenv('PORT', get_secret('port', 5000)))
FLASK_DEBUG = os.getenv('FLASK_DEBUG', str(get_secret('flask_debug', False))).lower() == 'true'
FLASK_SECRET_KEY = get_secret('flask_secret_key', '')

# SMTP Configuration for email (loaded from smtp_config.json)
SMTP_HOST = get_smtp_config('smtp_host', 'smtp.auroranrunner.com')
SMTP_PORT = int(get_smtp_config('smtp_port', 587))
SMTP_USER = get_smtp_config('smtp_user', 'health@auroranrunner.com')
SMTP_PASSWORD = get_smtp_config('smtp_password', '')
SMTP_FROM_EMAIL = get_smtp_config('smtp_from_email', 'health@auroranrunner.com')
SMTP_FROM_NAME = get_smtp_config('smtp_from_name', 'Auroran Health Dashboard')

# Demo Mode (if no InfluxDB configured)
DEMO_MODE = not INFLUXDB_TOKEN
