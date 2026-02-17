# Auroran Health Dashboard 🦞

**Personal Health Command Center** - Tracks fitness, recovery, and performance with multi-source integration.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

---

## Current Status

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
│  │   - Recovery score based on HRV + Sleep + RHR          │  │
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

## Features

- 📊 **Real-time Health Metrics** - HRV, sleep, resting HR, steps tracking
- 📈 **PMC (Performance Management Cycle)** - CTL/ATL/TSB with 30-day charts
- 🧠 **AI Exercise Planner** - Recovery-based workout recommendations
- 🎯 **Race Predictor** - Marathon/Half-marathon time predictions
- 🏃 **Training Periodization** - Recovery/Base/Build/Peak weekly cycles

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

### Measurement: `notes`
| Field | Type | Description |
|-------|------|-------------|
| date | tag | Date |
| alcohol | boolean | Had alcohol |
| stress_level | integer | 1-10 |
| notes | string | Free text |

---

## Setup

### Prerequisites
- Python 3.11+
- InfluxDB 2.x running
- (Optional) Strava API access

### Configuration
Create `secrets.json`:
```json
{
  "influxdb_token": "your-token-here",
  "strava_client_id": "your-id",
  "strava_client_secret": "your-secret",
  "strava_refresh_token": "your-refresh-token"
}
```

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

## Vision - Multi-Source Integration

1. **Strava:** ✅ Working - workouts, suffer_score
2. **Suunto:** Pending API approval - HRV, sleep, resting HR
3. **Garmin:** Future - Connect API
4. **Polar:** Future - Flow API
5. **Oura:** Future - Sleep, readiness

---

## License

MIT License 🦞

**Branch:** `dev/strava`
