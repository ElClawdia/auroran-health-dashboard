"""
Configuration for Health Dashboard
Set these environment variables or edit below
"""

import os

# InfluxDB Configuration
# Try multiple URLs for Docker networking
# Priority: localhost (host), sirius (host), docker bridge
INFLUXDB_URL = os.getenv('INFLUXDB_URL', 'http://host.docker.internal:8086')
INFLUXDB_TOKEN = os.getenv('INFLUXDB_TOKEN', 'REMOVED==')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG', 'auroran')
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET', 'health')

# Suunto API Configuration
SUUNTO_CLIENT_ID = os.getenv('SUUNTO_CLIENT_ID', '')
SUUNTO_CLIENT_SECRET = os.getenv('SUUNTO_CLIENT_SECRET', '')

# Flask Configuration
FLASK_HOST = os.getenv('FLASK_HOST', '0.0.0.0')
FLASK_PORT = int(os.getenv('PORT', 5000))
FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

# Demo Mode (if no InfluxDB configured)
DEMO_MODE = not INFLUXDB_TOKEN
