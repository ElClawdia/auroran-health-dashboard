# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Commands

```bash
# Run the dashboard (Flask server on port 5000)
python3 app.py

# Sync Strava workouts to InfluxDB
python3 sync_strava.py                    # Incremental (last 30 days)
python3 sync_strava.py --force            # Full sync (~3 years)
python3 sync_strava.py --newer-than 20240101  # Since specific date

# Test email service
python3 test_smtp.py

# Test PMC calculations
python3 training_load.py

# Apple Health XML export to InfluxDB
python3 apple_health_sync.py <export.xml>
```

## Architecture Overview

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│  Data Sources │────▶│   InfluxDB   │────▶│  Flask Dashboard │
│ (Strava, etc) │     │ (Time Series)│     │   (Web UI/API)   │
└──────────────┘     └──────────────┘     └──────────────────┘
```

**Key principle**: Dashboard never reads from APIs directly. All data flows through InfluxDB.

### Data Flow
- `sync_strava.py` → fetches from Strava API → writes to InfluxDB `workouts` measurement
- `sync_suunto.py` → fetches from Suunto → writes to InfluxDB `daily_health` measurement
- `app.py` → queries InfluxDB → serves dashboard UI

### InfluxDB Measurements
- `daily_health`: sleep_duration_hours, hrv_avg, resting_hr, steps, recovery_score
- `workouts` / `workout_cache`: type, date, duration, avg_hr, max_hr, calories, suffer_score
- `manual_values`: user overrides (sleep, hrv, weight, ctl, atl, tsb, calories)

### PMC (Performance Management Chart)
- CTL (Fitness): 42-day EWMA of daily training load
- ATL (Fatigue): 7-day EWMA of daily training load
- TSB (Form): CTL - ATL
- Parameters can be auto-tuned via `/api/formula/learn` endpoint

### Authentication
- Users stored in `users.json` with bcrypt-hashed passwords
- Session-based auth via Flask sessions
- Password reset via email tokens (stored in `email_tokens.json`)

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Flask app, all API endpoints, InfluxDB queries |
| `config.py` | Configuration (secrets.json, env vars) |
| `training_load.py` | PMC calculations (CTL/ATL/TSB) |
| `sync_strava.py` | Strava → InfluxDB sync script |
| `strava_client.py` | Strava API client |
| `auth.py` | User authentication |
| `email_service.py` | Password reset emails |
| `planner.py` | AI exercise recommendations |
| `formula_learning.py` | Auto-tune PMC parameters |

## Configuration

Create `secrets.json` with API tokens:
```json
{
  "influxdb_token": "...",
  "strava_client_id": "...",
  "strava_client_secret": "...",
  "strava_refresh_token": "..."
}
```

SMTP config in `smtp_config.json` (see `smtp_config.json.example`).

## Development Notes

- **Always commit and push** after making changes (user tests on remote server)
- **Workout list must load <1s**; fetch only 10 workouts for the UI
- Demo mode: runs without InfluxDB (mock data)
- In-memory cache: 30-second TTL for workouts and PMC data
- API requires authentication (except `/login`, `/forgot-password`, `/register`)
- Date navigation: dashboard defaults to today's date
