# Influx Cleanup Runbook

Current production values from this app:

- `INFLUXDB_URL=http://sirius:8086`
- `INFLUXDB_ORG=auroran`
- `INFLUXDB_BUCKET=health`

Current measurements seen in the bucket:

- `daily_health`
- `manual_values`
- `workout_cache`
- `workout_cache_rebuilt`
- `workouts`

The dashboard now reads from `workout_cache`. The two best cleanup targets are:

1. `workout_cache_rebuilt`
2. `workouts`

Delete `workout_cache_rebuilt` first. Keep `workout_cache` because the live app uses it.

## 1. Export a safety backup first

Set the token in your shell:

```bash
export INFLUXDB_URL="http://sirius:8086"
export INFLUXDB_ORG="auroran"
export INFLUXDB_BUCKET="health"
export INFLUXDB_TOKEN='PASTE_TOKEN_HERE'
```

Optional backup exports before deletion:

```bash
mkdir -p /tmp/influx-backup
curl -sS --request POST "$INFLUXDB_URL/api/v2/query?org=$INFLUXDB_ORG" \
  --header "Authorization: Token $INFLUXDB_TOKEN" \
  --header 'Accept: application/csv' \
  --header 'Content-type: application/vnd.flux' \
  --data 'from(bucket: "health") |> range(start: 0) |> filter(fn: (r) => r._measurement == "workout_cache_rebuilt")' \
  > /tmp/influx-backup/workout_cache_rebuilt.csv

curl -sS --request POST "$INFLUXDB_URL/api/v2/query?org=$INFLUXDB_ORG" \
  --header "Authorization: Token $INFLUXDB_TOKEN" \
  --header 'Accept: application/csv' \
  --header 'Content-type: application/vnd.flux' \
  --data 'from(bucket: "health") |> range(start: 0) |> filter(fn: (r) => r._measurement == "workouts")' \
  > /tmp/influx-backup/workouts.csv
```

## 2. Delete `workout_cache_rebuilt`

This is the safest first deletion.

```bash
curl -sS --request POST "$INFLUXDB_URL/api/v2/delete?org=$INFLUXDB_ORG&bucket=$INFLUXDB_BUCKET" \
  --header "Authorization: Token $INFLUXDB_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "start": "1970-01-01T00:00:00Z",
    "stop": "2100-01-01T00:00:00Z",
    "predicate": "_measurement=\"workout_cache_rebuilt\""
  }'
```

## 3. Delete legacy `workouts`

The current app no longer needs this for dashboard reads.

```bash
curl -sS --request POST "$INFLUXDB_URL/api/v2/delete?org=$INFLUXDB_ORG&bucket=$INFLUXDB_BUCKET" \
  --header "Authorization: Token $INFLUXDB_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "start": "1970-01-01T00:00:00Z",
    "stop": "2100-01-01T00:00:00Z",
    "predicate": "_measurement=\"workouts\""
  }'
```

## 4. Optional: put a retention policy on old raw workout data

If you want the bucket to stop growing forever, create a separate archive bucket or set retention on `health`.

Example: set `health` retention to 365 days with Python client:

```bash
python3 - <<'PY'
from influxdb_client import InfluxDBClient
from influxdb_client.domain.bucket_retention_rules import BucketRetentionRules

url = "http://sirius:8086"
token = "PASTE_TOKEN_HERE"
org = "auroran"
bucket_name = "health"

client = InfluxDBClient(url=url, token=token, org=org)
buckets = client.buckets_api()
bucket = buckets.find_bucket_by_name(bucket_name)
bucket.retention_rules = [BucketRetentionRules(type="expire", every_seconds=365 * 24 * 3600)]
buckets.update_bucket(bucket)
print("Updated retention to 365 days for bucket:", bucket_name)
client.close()
PY
```

Do not do this unless you are sure you want all measurements in `health` to expire, including `daily_health` and `manual_values`.

## 5. Safer long-term structure

Recommended structure:

- `health_hot`: 365d retention for `daily_health`, `manual_values`, `workout_cache`
- `health_archive`: infinite retention only if you truly need raw history

## 6. Verify after deletion

Check the remaining measurements:

```bash
curl -sS --request POST "$INFLUXDB_URL/api/v2/query?org=$INFLUXDB_ORG" \
  --header "Authorization: Token $INFLUXDB_TOKEN" \
  --header 'Accept: application/csv' \
  --header 'Content-type: application/vnd.flux' \
  --data 'import "influxdata/influxdb/schema"
schema.measurements(bucket: "health")'
```

Check disk usage later on the Influx host:

```bash
du -sh /var/lib/influxdb2
du -sh /var/lib/influxdb2/engine
```

Note: Influx may not reclaim on-disk space immediately after delete. Compaction needs to run before the disk size drops.
