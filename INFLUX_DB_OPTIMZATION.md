# InfluxDB Performance Audit and Optimization Plan

Date: 2026-03-01

## Executive Summary

The app's slow load time is not primarily a "too much data" problem. The `health` bucket is modest in size, but the workout query path is structurally expensive.

Main findings:

- `daily_health` and `manual_values` queries are generally fast.
- Workout queries are the dominant bottleneck.
- The single biggest issue is that workout points are written without an explicit event timestamp, so `_time` is the sync/write time, not the workout time.
- Because of that, queries like `range(start: -42d)` do not mean "last 42 workout days"; they mean "rows written in the last 42 days", which can include years of historical workouts if they were synced recently.
- `workouts` and `workout_cache` both store the same workout events, which doubles some scans.
- Several workout queries use large `_field == ... or ...` chains that are slower than expected.
- `manual_values` has tombstone-style deletes and at least one malformed date (`0026-02-19`), and weight writes use inconsistent timestamps.

Bottom line:

- The main optimization target is the workout schema and workout query path.
- `daily_health` is not the reason the dashboard feels slow.

## Scope and Method

This audit covered:

- All InfluxDB query call sites in `app.py`, `sync_strava.py`, and helper scripts.
- Read-only live queries against `http://192.168.0.220:8086`, org `auroran`, bucket `health`.
- A recent CSV export in `backups/influxdb-health-20260228-184742`.

I used:

- Code inspection for query shape and route behavior.
- Live timing of representative Flux queries with the Influx CLI.
- CSV export analysis for row counts, date coverage, duplication, and field distribution.

## Bucket Overview

Bucket metadata:

- Bucket: `health`
- Retention: infinite
- Shard group duration: 168h

Measurements present:

- `daily_health`
- `manual_values`
- `workout_cache`
- `workouts`

## Data Profile

The recent CSV export gives a reliable size estimate:

| Measurement | Export rows | Distinct dates | Notes |
| --- | ---: | ---: | --- |
| `daily_health` | 14,676 | 3,656 | 2016-02-26 to 2026-02-28 |
| `manual_values` | 169 | 24 | 1 malformed date tag: `0026-02-19` |
| `workouts` | 37,355 | 2,138 | 3,484 workout events, 14 workout types |
| `workout_cache` | 33,871 | 2,138 | same 3,484 workout events, 14 workout types |
| Total | 86,071 | n/a | raw CSV rows only, not counting comments/headers |

Recent export directory size:

- `backups/influxdb-health-20260228-184742`: `16M`

Important interpretation:

- This is not a large InfluxDB dataset.
- Dashboard slowness is coming from query design and data modeling, not raw volume.

## Schema Observations

### `daily_health`

Observed fields from export:

- `steps`
- `avg_hr`
- `resting_hr`
- `sleep_duration_hours`
- `weight`
- `active_calories`
- `basal_calories`
- `total_calories`

Tag usage:

- `date`

This measurement is structurally reasonable because writes set `_time` to the actual day:

- `apple_health_sync.py:153-158`
- `import_apple_calories.py:103-108`

### `manual_values`

Observed fields:

- `hrv`
- `resting_hr`
- `sleep`
- `atl`
- `weight`
- `steps`
- `ctl`
- `tsb`
- `calories`

Tags:

- `date`
- `deleted` (only on tombstone rows)

Notable issues:

- 66 of 169 rows are delete tombstones, so about 39% of this measurement is cleanup metadata.
- There is at least one malformed date tag: `0026-02-19`.
- Weight writes use inconsistent `_time` handling:
  - generic manual values POST sets explicit target date/time: `app.py:1290-1293`
  - manual DELETE also sets explicit target date/time: `app.py:1317-1322`
  - weight POST does not set `.time(...)`: `app.py:1445-1449`

That inconsistency makes date-window queries less predictable.

### `workouts`

Observed fields per workout:

- `strava_id`
- `date`
- `start_time`
- `duration`
- `distance`
- `elevation_gain`
- `avg_hr`
- `max_hr`
- `suffer_score`
- `calories`
- `name`

Tags:

- `date`
- `type`

Issues:

- `date` is stored both as a tag and as a field.
- The measurement duplicates `workout_cache`.
- The writer does not set event `_time`; Influx uses write time.

Writer:

- `sync_strava.py:157-173`

### `workout_cache`

Observed fields per workout:

- same as `workouts`, minus the redundant `date` field

Tags:

- `date`
- `type`

This is cleaner than `workouts`, but it still has the same critical timestamp issue: no explicit `.time(...)`.

Writer:

- `sync_strava.py:175-190`

## Live Query Timings

These were timed against the live `health` bucket.

### Fast queries

| Query shape | Code path | Time |
| --- | --- | ---: |
| `daily_health` pivot for 8-day window | `app.py:1605-1611` | `0.12s` |
| `daily_health` pivot for 37-day window | `app.py:1655-1661` | `0.13s` |
| `manual_values` 30-day history | `app.py:1687-1694` | `0.06s` |
| `workouts.suffer_score` for 365 days | `app.py:1579-1585` | `0.62s` |
| weight fallback lookup in `daily_health` | `app.py:2040-2047` plus fallback pattern | `0.29s` |

Interpretation:

- `daily_health` is fine.
- `manual_values` is fine for now.
- PMC reads are acceptable at the current scale.

### Slow queries

| Query shape | Code path | Time | Result size |
| --- | --- | ---: | ---: |
| `workout_cache`, 14-day recent fetch with big `_field` OR filter | `app.py:217-223` | `6.39s` | ~349 output lines |
| dashboard workout fetch, `workout_cache`, 42-day scan, no `_field` filter | `app.py:883-890` | `13.18s` | ~847 output lines |
| 42-day limited workout fetch, `workout_cache`, large `_field` OR filter | `app.py:965-971` | `41.34s` | ~847 output lines |
| workout index load, both `workout_cache` and `workouts`, large `_field` OR filter | `app.py:1018-1024` | `39.20s` | ~1,689 output lines |

Interpretation:

- The workout path is the bottleneck.
- The `_field == ... or ...` chain is not helping; in the measured cases it was worse than querying the whole measurement.
- Loading both `workout_cache` and `workouts` roughly doubles the rows returned for the same underlying workout set.

## Why Workout Queries Are Slow

## 1. `_time` is wrong for workouts

This is the most important finding.

Workout writes do not set `.time(...)`, so `_time` becomes the sync/write timestamp, not the workout timestamp:

- `sync_strava.py:157-190`

Effects:

- `range(start: -42d)` means "written in the last 42 days", not "workouts from the last 42 days".
- A recent full historical sync places old workouts into recent shards.
- Every query that assumes `_time` tracks workout date is working against the storage layout.

This explains why queries limited to "42 days" can still be slow even though the logical result set is small.

## 2. Duplicate workout storage

The same logical workout set exists in both:

- `workouts`
- `workout_cache`

The export shows:

- `workouts`: 3,484 workouts
- `workout_cache`: 3,484 workouts

The index loader queries both measurements:

- `app.py:1018-1024`

That doubles scan cost before de-duplication in Python.

## 3. Large `_field` OR chains are expensive

These queries build filters like:

- `_field == "duration" or _field == "duration_minutes" or ...`

Observed result:

- 42-day `workout_cache` scan without the `_field` filter: `13.18s`
- 42-day `workout_cache` scan with the large `_field` OR filter: `41.34s`

That is a major signal that the field filter is making the query planner do more work than the unfiltered scan for this dataset.

Affected code:

- `app.py:206-223`
- `app.py:945-971`
- `app.py:1011-1024`
- `sync_strava.py:200-215`

## 4. Heavy workout queries are still on the user request path

The UI is already phased:

- quick data
- charts
- workouts last

Frontend:

- `templates/index.html:1117`
- `templates/index.html:1152`
- `templates/index.html:1165`

But the backend still has expensive request-path behavior:

- `/api/workouts` can trigger a background 42-day workout index load: `app.py:1116-1156`
- if that load stalls for 15 seconds, it falls back to a direct scan: `app.py:1136-1146`
- `_dash_fetch_workouts()` still uses the direct full workout fetch path: `app.py:1778-1789`

That means the slow path is still part of normal dashboard behavior.

## 5. Python does extra work after the scan

This is secondary, but still real:

- streaming raw rows
- reconstructing workouts by `_time`
- sorting in Python
- de-duplicating in Python

Affected code:

- `app.py:890-911`
- `app.py:972-989`
- `app.py:1026-1048`

This is not the root cause, but it adds latency on top of already expensive reads.

## 6. `daily_health` is not the bottleneck

The `pivot()`-based `daily_health` reads were all fast in testing.

That means:

- do not spend time prematurely redesigning `daily_health`
- focus on workout storage and workout query patterns first

## Data Quality and Consistency Issues

### Inconsistent timestamps in `manual_values`

- Generic manual metric writes use explicit target time: `app.py:1290-1293`
- Delete tombstones use explicit target time: `app.py:1317-1322`
- Weight POST does not: `app.py:1445-1449`

Recommendation:

- make all manual writes use explicit target time, preferably noon UTC on the target date

### Malformed date tag

Found:

- `manual_values.date = 0026-02-19`

Recommendation:

- fix the bad row
- add server-side date validation before writing

### Redundant field in `workouts`

`workouts` stores:

- `date` as a tag
- `date` again as a field

Recommendation:

- stop writing the redundant field

## Cardinality Assessment

This does not look like a high-cardinality problem.

Observed tag spread:

- `daily_health.date`: 3,656 distinct values
- `workouts.date`: 2,138 distinct values
- `workout_cache.date`: 2,138 distinct values
- workout `type`: 14 distinct values

Conclusion:

- cardinality is moderate and manageable
- the issue is time modeling, duplicated storage, and query design

## Route-by-Route Impact

### `/api/dashboard/quick`

Components:

- health today
- recommendation
- calories
- weight

Influx cost:

- low

Main note:

- this route is not the reason the initial page feels slow

### `/api/dashboard/charts`

Components:

- 10-day health history
- PMC

Influx cost:

- low to moderate

Main note:

- acceptable at current scale

### `/api/workouts?before_date=...&limit=10`

Influx cost:

- high

Main note:

- this is the main user-facing performance problem

### Full dashboard aggregate route `/api/dashboard`

Backend fans out to 7 tasks:

- `app.py:2187-2195`

If this route is used, the total request cost will be dominated by workouts even when the other queries are fast.

## Optimization Plan

## Phase 0: Immediate fixes without schema migration

Goal: reduce user-visible latency quickly.

### 1. Stop loading the 42-day workout index on request

Current behavior:

- request can trigger `_load_workout_index()`
- index build can take ~39s

Action:

- remove request-triggered index builds for dashboard traffic
- rely on `logs/recent_workouts_cache.json` for homepage/recent-workout views
- build or refresh the cache only after sync jobs or on a separate background schedule

Expected impact:

- large reduction in user-visible page latency

### 2. Use only `workout_cache` for reads

Action:

- stop querying both `workout_cache` and `workouts` in read paths
- keep `workouts` only for migration/compatibility until the cutover is complete

Affected code:

- `app.py:882`
- `app.py:991`
- `app.py:1021`
- `app.py:1866`

Expected impact:

- cut many workout scans nearly in half

### 3. Remove large `_field` OR filters from workout scans

Measured effect:

- with OR filter: ~41s
- without OR filter: ~13s

Action:

- for current schema, query `workout_cache` without the large `_field` OR chain
- reconstruct only the fields needed in Python

Expected impact:

- major reduction in workout query latency even before schema migration

### 4. Increase workout cache TTLs

Current:

- many caches use `30s`

Action:

- raise recent workout cache and workout index TTLs to something realistic, such as 5 to 15 minutes
- invalidate on sync completion instead of waiting for time expiry

Expected impact:

- fewer repeated expensive reads

### 5. Fix `manual_values` weight writes

Action:

- set explicit `.time(target_dt)` in `POST /api/weight`
- align it with the generic manual values path

Expected impact:

- more consistent date-window query behavior

## Phase 1: Fix the workout time model

Goal: make Influx ranges reflect workout time instead of sync time.

### 1. Write workout `_time` as the actual activity start timestamp

Action:

- in `sync_strava.py`, construct a timestamp from workout `date` + `start_time`
- use `.time(activity_start_ts)` on both `workouts` and `workout_cache`
- if exact time is missing, use noon UTC on the workout date

This is the highest-value change in the whole system.

Expected impact:

- `range(start: -42d)` becomes semantically correct
- recent workout scans become naturally small
- old historical workouts stop polluting recent time windows

### 2. Backfill existing workout points

Action:

- rewrite or re-import `workout_cache` and `workouts` with correct `_time`
- ideally migrate from source data or rebuild from Strava export/API

Recommendation:

- rebuild `workout_cache` first
- switch app reads to the rebuilt measurement
- then decide whether `workouts` is still needed

## Phase 2: Simplify the workout storage model

Goal: remove duplication and reduce read complexity.

### 1. Make `workout_cache` the single read measurement

Action:

- choose one canonical measurement for app reads
- prefer `workout_cache`
- keep `workouts` only if required for compatibility or auditing

### 2. Remove redundant `date` field from `workouts`

Action:

- if `workouts` remains, stop writing `.field("date", date)`

Expected impact:

- less storage waste
- slightly less scan overhead

### 3. Consider storing a compact daily summary measurement

For PMC and dashboard summaries, add a derived measurement such as:

- `daily_training_load`

Fields:

- `load`
- `workout_count`
- `calories`

Tags:

- `date`

Expected impact:

- `/api/pmc` can read a few hundred daily rows instead of scanning raw workout points

## Phase 3: Optional derived views for dashboard reads

These are useful if you want the dashboard to stay fast as the dataset grows.

### 1. Materialize `recent_workouts`

Store only the most recent N workout rows in a compact measurement or JSON cache.

Use case:

- homepage workout list

### 2. Materialize `daily_health_latest`

If `daily_health` continues to accumulate multiple writes per day from multiple importers, keep a derived one-row-per-day view.

Use case:

- health cards
- chart reads

This is optional because current `daily_health` performance is already good.

## Recommended Implementation Order

1. Stop querying both workout measurements.
2. Remove large `_field` OR filters from workout reads.
3. Stop request-path workout index builds.
4. Fix `POST /api/weight` to set explicit `_time`.
5. Change workout writers to use actual workout timestamps.
6. Backfill or rebuild `workout_cache`.
7. Switch all workout reads to only the rebuilt `workout_cache`.
8. Optionally add `daily_training_load` materialization.

## Safe Rollout Added To Repo

The repo now includes a safer migration path instead of mutating the live workout measurement in place.

Added utilities:

- `rebuild_workout_cache.py`
- `audit_manual_values.py`

Suggested rollout:

1. Run a dry run:
   - `python3 rebuild_workout_cache.py --url http://192.168.0.220:8086`
2. Write rebuilt historical points into a new measurement:
   - `python3 rebuild_workout_cache.py --url http://192.168.0.220:8086 --execute`
3. Point the app at the rebuilt measurement:
   - `WORKOUT_READ_MEASUREMENT=workout_cache_rebuilt`
4. Verify dashboard behavior and query latency.
5. Only after verification, decide whether to retire or delete the old `workout_cache`.

Manual data audit:

1. Run:
   - `python3 audit_manual_values.py --url http://192.168.0.220:8086`
2. Inspect malformed dates and tombstone-heavy keys before making any cleanup changes.

## Expected Outcome After Fixes

If you do only the immediate read-path fixes:

- workout list should get materially faster
- page-load stalls should reduce substantially

If you also fix workout `_time` and rebuild the workout data:

- most "recent workouts" reads should become naturally cheap
- the 42-day workout scans should stop behaving like recent full-history scans
- the app should no longer need so much defensive caching logic around workout queries

## Concrete Code Hotspots

Most important files and regions:

- `sync_strava.py:157-190`
- `app.py:201-242`
- `app.py:882-911`
- `app.py:940-999`
- `app.py:1002-1053`
- `app.py:1778-1789`
- `app.py:1858-1909`
- `app.py:2020-2054`
- `app.py:1445-1449`

## Final Assessment

At the current scale, InfluxDB itself is not overloaded.

The performance problem is mostly self-inflicted:

- incorrect workout timestamps
- duplicate workout storage
- expensive read patterns
- slow index building on request

Fix those first. If you do, the app should become much faster without needing a database migration to another system or aggressive retention/downsampling changes.

## Validation On `dev/clawdia-optim`

I fetched the latest remote refs and switched to `dev/clawdia-optim` on 2026-03-01 to verify whether the conclusions still hold there.

Result:

- The core `daily_health`, `manual_values`, and workout query patterns are still present.
- The most important recommendations in this report are still valid on `dev/clawdia-optim`.
- The workout timestamp problem is still present because `sync_strava.py` still writes `workouts` and `workout_cache` without explicit `.time(...)`.
- The duplicate-measurement problem is still present because both `workouts` and `workout_cache` are still written and still queried.

Branch-specific note:

- `dev/clawdia-optim` changed `_fetch_daily_loads_from_influx()` so PMC now reads both `workouts` and `workout_cache` and de-duplicates in Python instead of reading only `workouts`.
- That change makes the duplicate-storage recommendation even more important on this branch.

Files reviewed on `dev/clawdia-optim`:

- `app.py`
- `sync_strava.py`
- `apple_health_sync.py`
- `import_apple_calories.py`
- `templates/index.html`

Net conclusion:

- No recommendation in this report became invalid on `dev/clawdia-optim`.
- The highest-priority fixes are unchanged:
  1. write workout `_time` correctly
  2. stop reading both workout measurements
  3. remove the expensive workout-field OR scans
  4. keep the slow workout index out of the request path
