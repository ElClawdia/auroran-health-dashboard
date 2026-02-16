"""
Configuration for Health Dashboard
Set these environment variables or edit secrets.json
"""

import os
import json
from pathlib import Path

# Load secrets from json file if exists
SECRETS_FILE = Path(__file__).parent / "secrets.json"
secrets = {}
if SECRETS_FILE.exists():
    with open(SECRETS_FILE) as f:
        secrets = json.load(f)

def get_secret(key, default=''):
    """Get secret from env var or secrets file"""
    return os.getenv(key, secrets.get(key, default))

# InfluxDB Configuration
INFLUXDB_URL = os.getenv('INFLUXDB_URL', 'http://influxdb:8086')
INFLUXDB_TOKEN = get_secret('influxdb_token', '')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG', 'auroran')
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET', 'health')

# Suunto API Configuration
SUUNTO_CLIENT_ID = get_secret('suunto_client_id', '')
SUUNTO_CLIENT_SECRET = get_secret('suunto_client_secret', '')

# Strava API Configuration
STRAVA_ACCESS_TOKEN = get_secret('strava_access_token', '')

# Garmin (no personal API - sync via Strava recommended)
GARMIN_USERNAME = get_secret('garmin_username', '')
GARMIN_PASSWORD = get_secret('garmin_password', '')

# Flask Configuration
FLASK_HOST = os.getenv('FLASK_HOST', '0.0.0.0')
FLASK_PORT = int(os.getenv('PORT', 5000))
FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

# Demo Mode (if no InfluxDB configured)
DEMO_MODE = not INFLUXDB_TOKEN
