# Dashboard Date Contract

This file defines the expected behavior of the dashboard date logic.

## Core rule

The selected dashboard date is the anchor for all dashboard data.

If the dashboard is showing `YYYY-MM-DD`, then every card, chart, and list must be computed for that date window. The UI must not silently switch to a later or earlier date just because that other date has more data.

## Required behavior

### 1. Quick cards

For `/api/dashboard/quick?date=DATE`:

- `health` must represent `DATE` only.
- `weight` must be the weight as of `DATE`.
- `calories` must be calculated for `DATE`.
- `recommendation` must be calculated for `DATE`.

If a metric is missing on `DATE`, return `null` for that metric.
Do not substitute a newer day.

### 2. Weight

Weight is an as-of metric.

Rules:

- Manual value on `DATE` wins.
- Otherwise use the latest automated weight on or before `DATE`.
- The API response `date` must still be the requested dashboard date.
- If the actual source measurement came from an earlier day, expose it as `source_date`.

This keeps the dashboard anchored to the selected date while still allowing as-of lookup semantics.

### 3. Calories

Calories must be for the selected date only.

Rules:

- Use BMR for `DATE`.
- Add workout calories for workouts on `DATE`.
- Add step/activity calories from `DATE` when available.
- Do not include workouts from another date.

### 4. Recent workouts

For `/api/workouts?before_date=DATE&limit=N`:

- Return the most recent workouts on or before `DATE`.
- The first rows must be the workouts from `DATE` if any exist.
- Then continue backward in time.
- Do not read from stale rebuilt measurements unless explicitly requested for a rebuild operation.

### 5. Charts

For `/api/dashboard/charts?date=DATE&days=N`:

- `history` must end at `DATE`.
- `history.dates[-1]` must equal `DATE`.
- Missing days inside the range must stay in the series as `null`, not be dropped.
- `pmc` must be calculated with `DATE` as the end date.

### 6. Frontend navigation

When moving between dates:

- Previous/next buttons must change the dashboard anchor date.
- All requests must use that date.
- Late responses from an older selection must be ignored.
- The UI must never keep showing a later day after the user has moved backward.

## Things that must not happen

- No fallback from selected date to “latest available date” for health cards.
- No chart series that shifts its end date because the selected day has missing data.
- No workout list sourced from `workout_cache_rebuilt` in normal dashboard operation.
- No mixed-date dashboard where cards come from one date and workouts/PMC come from another.

## Operational note

This contract exists because date-anchor regressions have recurred multiple times during performance tuning and Influx measurement changes.

Any future tuning must preserve this contract.
