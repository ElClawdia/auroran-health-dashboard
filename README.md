# Auroran Health Dashboard 🦞

**Personal Health Command Center** - Tracks fitness, recovery, and performance with multi-source integration.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

---

## Status

**✅ Working:**
- **Strava:** 100+ workouts synced (no duplicates)
- **PMC:** CTL/ATL/TSB calculated from suffer_score
- **Dashboard:** http://localhost:5000 (Flask + Chart.js)
- **InfluxDB:** Time-series storage (bucket: `health`)

**PMC Calculus:**
- **Daily Training Load:** Sum of `suffer_score` from workouts
- **CTL (Fitness):** EMA(42 days) of daily load
- **ATL (Fatigue):** EMA(7 days) of daily load
- **TSB (Form):** CTL - ATL

---

## Features

- 📊 **Real-time Health Metrics** - HRV, sleep, resting HR, steps tracking
- 📈 **PMC Charts** - CTL/ATL/TSB 30-day visualization
- 🧠 **AI Exercise Planner** - Recovery-based workout recommendations
- 📈 **Beautiful Dashboards** - Dark-theme UI with Chart.js visualizations
- 🔗 **Strava Integration** - Direct sync with Strava activities
- 🏃 **Training Periodization** - Recovery/Base/Build/Peak weekly cycles
- 🎯 **Race Predictor** - Marathon/Half-marathon time predictions

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    OpenClaw Container                            │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │   Strava    │  │  InfluxDB    │  │   Health Dashboard  │ │
│  │    API      │─▶│   (Time      │─▶│      (Web UI)       │ │
│  │  (Python)   │  │   Series)     │  │   (Flask + Chart)   │ │
│  └──────────────┘  └──────────────┘  └──────────────────────┘ │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Exercise Planner (AI Recommendations)         │  │
│  │   - Recovery score based on HRV + Sleep + RHR           │  │
│  │   - Training load analysis                               │  │
│  │   - Weekly periodization                                │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**Key Principle:** Dashboard **never** reads from APIs directly. All data flows through InfluxDB.

```
Strava → sync_strava.py → InfluxDB → Dashboard
Suunto → (pending API) → InfluxDB → Dashboard
```

---

## Vision

A comprehensive health monitoring and exercise planning system that:
1. Collects data from multiple sources (Strava, Suunto, Garmin, etc.)
2. Stores in InfluxDB (time-series optimized)
3. Provides a beautiful web UI for visualization
4. Generates AI-powered exercise recommendations based on trends

---

## Database Schema (InfluxDB)

### Measurement: `daily_health`
| Field | Type | Description |
|-------|------|-------------|
| date | tag | Date (YYYY-MM-DD) |
| sleep_duration_hours | float | Total sleep |
| hrv_avg | float | Average HRV (ms) |
| hrv_std | float | HRV standard deviation |
| resting_hr | float | Average resting heart rate |
| steps | integer | Total steps |
| recovery_score | integer | 0-100 calculated score |
| training_load | float | Acute:chronic workload ratio |

### Measurement: `workouts`
| Field | Type | Description |
|-------|------|-------------|
| strava_id | tag | Unique Strava ID |
| type | tag | Running, Cycling, Ride, etc. |
| date | tag | Date |
| start_time | tag | Start time (HH:MM) |
| duration_minutes | float | Duration |
| avg_hr | float | Average heart rate |
| max_hr | float | Max heart rate |
| calories | integer | Estimated calories |
| distance | float | Distance (meters) |
| elevation_gain | float | Elevation (meters) |
| suffer_score | float | Strava effort score |
| name | string | Activity name |

---

## Setup

### Prerequisites
- Python 3.11+
- InfluxDB 2.x running
- (Optional) Strava API access

### Configuration

**1. Create `secrets.json`** (API credentials):
```json
{
  "influxdb_token": "your-token-here",
  "strava_client_id": "your-id",
  "strava_client_secret": "your-secret",
  "strava_refresh_token": "your-refresh-token"
}
```

**2. Create `smtp_config.json`** (for password reset emails):
```json
{
  "smtp_host": "smtp.auroranrunner.com",
  "smtp_port": 587,
  "smtp_user": "health@auroranrunner.com",
  "smtp_password": "YOUR_SMTP_PASSWORD",
  "smtp_from_email": "health@auroranrunner.com"
}
```
See `smtp_config.json.example` for template.

### Running

1. **Start InfluxDB:** `http://influxdb:8086` (org: `auroran`, bucket: `health`)
2. **Configure tokens:** `renew-strava-tokens/strava_tokens.json`
3. **Run dashboard:** `python3 app.py`
4. **Sync Strava:** `python3 sync_strava.py`

### Cron (Optional)
```bash
# Sync Strava hourly
0 * * * * python3 /path/to/sync_strava.py
```

---

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard UI |
| `GET /api/health/today` | Today's metrics |
| `GET /api/health/history?days=30` | Historical data |
| `GET /api/workouts` | Workout list |
| `GET /api/pmc?days=30` | PMC (CTL/ATL/TSB) |
| `GET /api/recommendations/today` | AI workout recommendation |
| `POST /api/workouts` | Log new workout |

---

## Logs
```
logs/health-dashboard.log
logs/strava_sync.log
```

---

## Apple Health Integration

Export your Apple Health data from iPhone and sync to InfluxDB:

1. **Export:** Use "Export Health Data" on iPhone (Health app → Profile → Export All Health Data)
2. **Convert:** Use `apple_health_sync.py` to parse the XML export
3. **Sync:** Data flows to InfluxDB → Dashboard

The dashboard filters to only show recent data (last 90 days) with actual HRV values for performance.

- **Apple Health:** Export from iPhone, sync to InfluxDB
- **Suunto:** HRV, sleep, resting HR (API pending)
- **Garmin:** Connect API → activities, health metrics
- **Polar:** Flow API → training load
- **Oura:** Sleep, readiness, recovery score

---

## License

MIT License 🦞

**Branch:** `dev/strava`
