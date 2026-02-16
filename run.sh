# Health Dashboard Quick Start

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export INFLUXDB_URL=http://localhost:8086
export INFLUXDB_TOKEN=your-influxdb-token
export INFLUXDB_ORG=tapio
export INFLUXDB_BUCKET=health

# Optional: Suunto API (get from apizone.suunto.com)
export SUUNTO_CLIENT_ID=your-client-id
export SUUNTO_CLIENT_SECRET=your-client-secret

# Run the dashboard
python app.py
```

## Access

- Dashboard: http://localhost:5000
- API endpoints:
  - GET /api/health/today
  - GET /api/health/history?days=30
  - GET /api/workouts
  - GET /api/recommendations/today
  - GET /api/suunto/sync

## Demo Mode

Without InfluxDB configured, it runs in demo mode with mock data to show the UI.

## Architecture

```
suunto_client.py → Suunto API → InfluxDB ← planner.py (AI recommendations)
                                        ↓
                                      Flask → Web UI (Chart.js)
```
