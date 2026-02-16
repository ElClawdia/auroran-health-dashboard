# Health Command Center - Tapio's Personal Health Dashboard

## Vision
A comprehensive health monitoring and exercise planning system that:
1. Collects data from Suunto API (workouts, sleep, HR, HRV)
2. Stores in InfluxDB (time-series optimized)
3. Provides a beautiful web UI for visualization
4. Generates AI-powered exercise recommendations based on trends

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        OpenClaw Container                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │  Suunto API  │  │  InfluxDB    │  │   Health Dashboard  │ │
│  │    Driver    │──▶│   (Time      │──▶│      (Web UI)       │ │
│  │  (Python)    │  │   Series)     │  │   (Flask + Chart)   │ │
│  └──────────────┘  └──────────────┘  └──────────────────────┘ │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Exercise Planner (AI Recommendations)        │  │
│  │   - Recovery score based on HRV + Sleep + RHR          │  │
│  │   - Training load analysis                             │  │
│  │   - Adaptive periodization                              │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

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
| workout_id | tag | Unique ID |
| type | tag | Running, Cycling, Gym, etc. |
| date | tag | Date |
| duration_minutes | float | Duration |
| avg_hr | float | Average heart rate |
| max_hr | float | Max heart rate |
| calories | integer | Estimated calories |
| intensity | float | 1-10 scale |
| feeling | string | How felt (great, good, okay, bad) |

### Measurement: `notes`
| Field | Type | Description |
|-------|------|-------------|
| date | tag | Date |
| alcohol | boolean | Had alcohol |
| stress_level | integer | 1-10 |
| notes | string | Free text |

---

## Suunto API Integration

### OAuth Flow
1. Register at apizone.suunto.com as developer
2. Create app to get Client ID + Secret
3. OAuth URL: `https://apizone.suunto.com/oauth/authorize`
4. Token endpoint: `https://apizone.suunto.com/oauth/token`

### Key Endpoints
- `/dailies` - Daily summaries (steps, HR, sleep)
- `/exercises` - Workout data
- `/sleeps` - Sleep analysis (HRV during sleep)
- `/recovery` - Daily recovery recommendations

### Data Fields to Collect
- `daySummary`: steps, distance, calories, HR
- `sleepSummary`: duration, deep sleep, REM, HRV
- `exerciseSamples`: HR, speed, power (if available)

---

## Web UI Components

### Dashboard (Single Page)
```
┌─────────────────────────────────────────────────────────────┐
│  🦞 AURORAN HEALTH COMMAND CENTER                    [⚙️]  │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐           │
│  │   💤 Sleep  │ │  ❤️ HRV    │ │  🫀 RHR    │           │
│  │   7h 27m   │ │    42ms    │ │   58 bpm   │           │
│  │    +12m    │ │   +5ms ▲   │ │   -2bpm ▼  │           │
│  └─────────────┘ └─────────────┘ └─────────────┘           │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │           30-Day HRV Trend Chart                     │   │
│  │  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                 │   │
│  │       ╭───────╮                                        │   │
│  │  ╭────╯       ╰────╮                                   │   │
│  │  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                 │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌──────────────────┐  ┌────────────────────────────────┐  │
│  │  🎯 Today        │  │  📅 This Week                 │  │
│  │  RECOVERY: 85%  │  │  Trainability: HIGH            │  │
│  │  ─────────────  │  │  ────────────────────────────  │  │
│  │  🏃 Run: 45min  │  │  Mon: ✓ Easy run               │  │
│  │     Zone 2-3    │  │  Tue: ✓ Strength                │  │
│  │  🚫 Rest: NO    │  │  Wed: ✓ Intervals              │  │
│  └──────────────────┘  └────────────────────────────────┘  │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Recent Workouts                                     │   │
│  │  • Yesterday: 5K Run - 28min - 145bpm avg - Great  │   │
│  │  • Feb 14:  Strength - 45min - Feeling: Good        │   │
│  │  • Feb 13:  Rest Day - Recovery                      │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Color Scheme
- Background: `#0d1117` (dark)
- Card BG: `#161b22`
- Primary: `#58a6ff` (blue)
- Success: `#3fb950` (green)
- Warning: `#d29922` (amber)
- Danger: `#f85149` (red)
- Text: `#c9d1d9`

---

## Exercise Planning Algorithm

### Recovery Score (0-100)
```
recovery = (
  hrv_percentile * 0.35 +
  sleep_quality * 0.30 +
  resting_hr_score * 0.20 +
  days_since_intense * 0.15
) * 100
```

### Training Recommendation Logic
| Recovery | Recommendation |
|----------|---------------|
| 85-100% | High intensity OK, push hard |
| 70-84% | Normal training, moderate load |
| 50-69% | Easy training, Zone 2 only |
| <50% | Rest or light mobility |

### Periodization (Weekly)
- **Recovery Week**: 2 hard days, 5 easy
- **Base Week**: 3 hard, 4 easy  
- **Build Week**: 4 hard, 3 easy
- **Peak Week**: 5 hard, 2 easy
- **Deload Week**: 2 hard, 5 easy

---

## API Endpoints (Flask Server)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health/today` | Today's metrics |
| GET | `/api/health/history?days=30` | Historical data |
| GET | `/api/workouts` | Workout log |
| POST | `/api/workouts` | Log a workout |
| GET | `/api/recommendations/today` | Today's exercise plan |
| GET | `/api/trends` | Weekly/monthly trends |

---

## Files to Create

```
/home/node/.openclaw/
├── workspace/
│   ├── health-dashboard/
│   │   ├── app.py                 # Flask web server
│   │   ├── config.py               # Configuration
│   │   ├── suunto_client.py       # Suunto API driver
│   │   ├── models.py               # InfluxDB schemas
│   │   ├── planner.py              # Exercise recommendations
│   │   ├── static/
│   │   │   ├── style.css           # Dark theme
│   │   │   ├── script.js           # Charts & UI logic
│   │   │   └── chart.js            # Chart.js config
│   │   ├── templates/
│   │   │   └── index.html         # Main dashboard
│   │   ├── requirements.txt        # Python deps
│   │   └── README.md               # Setup instructions
│   └── MEMORY.md                   # Update with new capabilities
```

---

## Python Dependencies

```
influxdb-client
flask
pandas
numpy
python-dateutil
requests
```

---

## Setup Steps

1. **Get Suunto API Credentials**
   - Visit apizone.suunto.com
   - Create developer account
   - Generate OAuth client

2. **Run the Dashboard**
   ```bash
   cd /home/node/.openclaw/workspace/health-dashboard
   pip install -r requirements.py
   export SUUNTO_CLIENT_ID=xxx
   export SUUNTO_CLIENT_SECRET=xxx
   export INFLUXDB_URL=http://localhost:8086
   export INFLUXDB_TOKEN=xxx
   python app.py
   ```

3. **Access**
   - Dashboard: `http://localhost:5000`
   - API: `http://localhost:5000/api/*`

---

## Future Enhancements

- [ ] Apple Health integration via Thryve
- [ ] Strava sync as backup
- [ ] Push notifications for daily recommendations
- [ ] Voice commands via TTS
- [ ] Meal logging
- [ ] Weight/body composition tracking
- [ ] Weather-aware outdoor exercise planning
- [ ] Race predictor (marathon times)
- [ ] Sleep debt calculator
- [ ] Training zone calculator (from lactate or HR)

---

*Designed by Clawdia 🦞 for Tapio — Let's optimize!*
