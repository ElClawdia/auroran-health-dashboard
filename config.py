"""
Configuration for Health Dashboard
Set these environment variables or edit below
"""

import os

# InfluxDB Configuration
INFLUXDB_URL = os.getenv('INFLUXDB_URL', 'http://localhost:8086')
INFLUXDB_TOKEN = os.getenv('INFLUXDB_TOKEN', 'your-token-here')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG', 'tapio')
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
