"""
Configuration for Health Dashboard
Set these environment variables or edit below
"""

import os

# InfluxDB Configuration
# Try multiple URLs for Docker networking
# Now accessible at influxdb:8086 (docker network)
INFLUXDB_URL = os.getenv('INFLUXDB_URL', 'http://influxdb:8086')
INFLUXDB_TOKEN = os.getenv('INFLUXDB_TOKEN', 'REMOVED==')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG', 'auroran')
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET', 'health')

# Suunto API Configuration
SUUNTO_CLIENT_ID = os.getenv('SUUNTO_CLIENT_ID', '')
SUUNTO_CLIENT_SECRET = os.getenv('SUUNTO_CLIENT_SECRET', '')

# Strava API Configuration
STRAVA_ACCESS_TOKEN = os.getenv('STRAVA_ACCESS_TOKEN', 'REPLACE_STRAVA_TOKEN')

# Garmin (no personal API - sync via Strava recommended)
GARMIN_USERNAME = os.getenv('GARMIN_USERNAME', '')
GARMIN_PASSWORD = os.getenv('GARMIN_PASSWORD', '')

# Flask Configuration
FLASK_HOST = os.getenv('FLASK_HOST', '0.0.0.0')
FLASK_PORT = int(os.getenv('PORT', 5000))
FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

# Demo Mode (if no InfluxDB configured)
DEMO_MODE = not INFLUXDB_TOKEN
