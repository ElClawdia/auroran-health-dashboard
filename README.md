# Auroran Health Dashboard 🦞

**Personal Health Command Center** - Tracks fitness, recovery, and performance with multi-source integration.

## Status

**✅ Working:**
- **Strava:** 100 workouts synced (no duplicates)
- **PMC:** CTL/ATL/TSB calculated from suffer_score
- **Dashboard:** http://localhost:5000 (Flask + Chart.js)
- **InfluxDB:** Time-series storage (bucket: `health`)

**PMC Calculus:**
- **Daily Training Load:** Sum of `suffer_score` from workouts
- **CTL (Fitness):** EMA(42 days) of daily load
- **ATL (Fatigue):** EMA(7 days) of daily load
- **TSB (Form):** CTL - ATL

**Architecture:**
```
Strava → sync_strava.py → InfluxDB → Dashboard
Suunto → sync_suunto.py → InfluxDB → Dashboard
```
Dashboard **never** reads from APIs directly.

## Setup

1. **InfluxDB:** `http://influxdb:8086` (org: `auroran`, bucket: `health`)
2. **Strava tokens:** `renew-strava-tokens/strava_tokens.json`
3. **Run:** `python3 app.py`
4. **Sync:** `python3 sync_strava.py`

## Logs
```
logs/health-dashboard.log
logs/strava_sync.log
```

## Future
- **Suunto:** HRV, sleep, resting HR (API pending)
- **Garmin:** Activities, health metrics
- **Polar:** Flow API → training load
- **Oura:** Sleep, readiness

**Branch:** `dev/strava` → `main`

MIT License 🦞