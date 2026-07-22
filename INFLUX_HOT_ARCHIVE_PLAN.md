# Influx Hot/Archive Plan

Goal:

- keep the live dashboard fast
- stop the main bucket from growing forever
- keep optional long-term history in a separate archive bucket

## Recommended buckets

- `health_hot`
  - retention: 365 days
  - contains:
    - `daily_health`
    - `manual_values`
    - `workout_cache`
- `health_archive`
  - retention: infinite or a very long period
  - contains:
    - optional historical exports if you want to preserve raw history

## Why this split

- the dashboard only needs recent operational data
- raw historical workout data is what usually dominates storage
- retention on one mixed bucket is risky because it can expire manual and health data too

## Migration outline

1. Create `health_hot` with 365d retention.
2. Create `health_archive` with no retention or a long retention.
3. Change the app to read/write `health_hot`.
4. Optionally export old raw measurements into `health_archive`.
5. Remove the old mixed bucket only after the app has run cleanly on `health_hot`.

## Example create commands

Using Python client:

```bash
python3 - <<'PY'
from influxdb_client import InfluxDBClient
from influxdb_client.domain.bucket import Bucket
from influxdb_client.domain.bucket_retention_rules import BucketRetentionRules

url = "http://sirius:8086"
token = "PASTE_TOKEN_HERE"
org = "auroran"

client = InfluxDBClient(url=url, token=token, org=org)
buckets = client.buckets_api()
orgs = client.organizations_api()
org_obj = orgs.find_organizations(org=org)[0]

def ensure_bucket(name, retention_seconds):
    existing = buckets.find_bucket_by_name(name)
    if existing:
        print("exists", name)
        return
    rules = []
    if retention_seconds:
        rules = [BucketRetentionRules(type="expire", every_seconds=retention_seconds)]
    bucket = Bucket(name=name, org_id=org_obj.id, retention_rules=rules)
    buckets.create_bucket(bucket=bucket)
    print("created", name)

ensure_bucket("health_hot", 365 * 24 * 3600)
ensure_bucket("health_archive", 0)
client.close()
PY
```

## App change

When you are ready to cut over:

- set `INFLUXDB_BUCKET=health_hot`
- restart the dashboard

## Copy options

The safest practical option is export/import by measurement rather than large in-place Flux tasks.

Examples:

- export `daily_health` from `health` and import into `health_hot`
- export `manual_values` from `health` and import into `health_hot`
- export `workout_cache` from `health` and import only recent history into `health_hot`
- export full old raw history into `health_archive` only if needed

## Suggested live strategy

- keep current `health` bucket for now
- delete duplicate measurements first
- monitor actual size change after compaction
- only do full bucket split if growth remains a problem
