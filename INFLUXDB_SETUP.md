# InfluxDB Setup Guide

This guide explains how to set up InfluxDB to store your health data for the Health Command Center dashboard.

---

## Option 1: Use Existing InfluxDB (Recommended)

If you already have InfluxDB running, configure these settings:

```bash
# Environment variables
export INFLUXDB_URL=http://localhost:8086
export INFLUXDB_TOKEN=your-admin-token
export INFLUXDB_ORG=your-org
export INFLUXDB_BUCKET=health
```

### Create Bucket Manually

1. Open InfluxDB UI (usually http://localhost:8086)
2. Go to **Load Data** → **Buckets**
3. Create new bucket named `health`
4. Create a token with read/write access to `health` bucket

---

## Option 2: Docker Setup (New Installation)

```bash
# Run InfluxDB container
docker run -d \
  --name influxdb \
  -p 8086:8086 \
  -v influxdb-data:/var/lib/influxdb2 \
  -v influxdb-config:/etc/influxdb2 \
  influxdb:latest

# Wait for startup, then create initial setup
docker exec influxdb influxd setup \
  --org tapio \
  --bucket health \
  --username admin \
  --password your-password \
  --token your-admin-token \
  --force
```

---

## Option 3: InfluxDB Cloud (Hosted)

1. Sign up at https://cloud.influxdata.com
2. Create a new organization
3. Create a bucket named `health`
4. Generate a token with read/write access
5. Use the URL, token, and org name in your config

---

## Required Data Schema

### Measurement: `daily_health`
| Field | Type | Description |
|-------|------|-------------|
| date | string | Date (YYYY-MM-DD) |
| sleep_duration_hours | float | Total sleep in hours |
| hrv_avg | float | Average HRV (ms) |
| resting_hr | float | Resting heart rate (bpm) |
| steps | integer | Total steps |
| recovery_score | integer | 0-100 recovery score |
| training_load | float | Acute:chronic workload ratio |

### Measurement: `workouts`
| Field | Type | Description |
|-------|------|-------------|
| date | string | Date (YYYY-MM-DD) |
| type | string | Workout type (Running, Cycling, etc.) |
| duration_minutes | float | Duration in minutes |
| avg_hr | float | Average heart rate |
| max_hr | float | Maximum heart rate |
| calories | integer | Calories burned |
| intensity | float | 1-10 intensity rating |
| feeling | string | great/good/okay/bad |

---

## Quick Test

After setup, test your connection:

```python
from influxdb_client import InfluxDBClient

client = InfluxDBClient(
    url="http://localhost:8086",
    token="your-token",
    org="your-org"
)

# Check connection
buckets = client.buckets_api().find_buckets()
print([b.name for b in buckets.buckets])
```

---

## Environment Variables Summary

```bash
# Required
INFLUXDB_URL=http://localhost:8086
INFLUXDB_TOKEN=your-token
INFLUXDB_ORG=tapio
INFLUXDB_BUCKET=health

# Optional (defaults shown)
INFLUXDB_URL defaults to http://localhost:8086
INFLUXDB_BUCKET defaults to "health"
```

---

## Troubleshooting

**Connection refused:**
- Check if InfluxDB is running: `docker ps`
- Check port: `curl http://localhost:8086/health`

**Authentication failed:**
- Verify token has correct permissions
- Token needs read/write on the `health` bucket

**Bucket not found:**
- Create bucket manually in InfluxDB UI
- Or use the setup commands above
