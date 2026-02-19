# Suunto to InfluxDB: Full Export Guide

This guide explains how to get **all possible Suunto data** and load it into this project’s InfluxDB bucket.

## Suunto bulk export/load options (quick matrix)

| Option | Coverage | Effort | Speed | Best use |
|---|---|---:|---:|---|
| Suunto app single export (FIT/GPX) | Single workout only | Medium (manual) | Slow for history | Spot fixes / one-off workouts |
| Suunto support full data request (GDPR/data portability) | Potentially full account history | Low | Medium (wait for support) | One-time historical backfill |
| Suunto API (`suunto_client.py`) | Daily metrics + workouts (endpoint dependent) | Medium | Fast once approved | Ongoing sync automation |
| Strava relay (Suunto->Strava->`sync_strava.py`) | Workouts mostly, not all wellness fields | Low | Fast | Good temporary workaround |

## Reality check first

- The Suunto mobile app typically supports easy export of **single activities** (FIT/GPX).
- Bulk "export all" is usually done via a **data request to Suunto Support** (GDPR/data portability request).
- In this repo, the most complete direct ingestion path for daily health metrics is the **Suunto API** route (`suunto_client.py`), which requires API credentials.

## What you likely want for missing metrics

To fill missing `sleep`, `hrv`, and `resting_hr`, ask for or pull:

- daily summaries/dailies
- sleep sessions
- HRV
- resting heart rate
- workout sessions

## Option 1 (quick): Export workouts from app one-by-one

Use this if you mainly need activities/workouts:

1. Open activity in Suunto app.
2. Tap the menu (three dots).
3. Export as **FIT** (preferred) or GPX.
4. Repeat for needed workouts.

This is not practical for full history, but works immediately.

## Option 2 (recommended for full history): Request full Suunto data export

Use Suunto Support and request a complete export package:

1. Open Suunto Support chat/contact form.
2. Ask for a **full data export** (GDPR/data portability style request).
3. Explicitly ask to include:
   - activities/workouts
   - dailies (steps, sleep, HRV, resting HR if available)
   - sleep/recovery datasets
4. Wait for export package (often a zip with FIT/other files).

Suggested message:

```text
Please provide a full data export of my Suunto account for portability:
all activities/workouts, daily summaries (steps, sleep, HRV, resting HR),
sleep/recovery data, and metadata in original/raw formats where possible.
```

## Option 3 (best for ongoing sync): Use Suunto API into this project

This repo already contains `suunto_client.py` and `/api/suunto/sync`.

### 1) Get API credentials

Obtain `client_id` and `client_secret` for Suunto API access.

### 2) Store credentials

Put in `secrets.json`:

```json
{
  "suunto_client_id": "YOUR_CLIENT_ID",
  "suunto_client_secret": "YOUR_CLIENT_SECRET"
}
```

### 3) Sync to InfluxDB

With app running:

```bash
curl "http://localhost:8512/api/suunto/sync"
```

This writes daily metrics into `daily_health` in your InfluxDB `health` bucket.

## How to load exports into this repo today

- **Workouts:** easiest path is Suunto -> Strava sync -> run `python3 sync_strava.py`.
- **Daily health (sleep/HRV/resting HR):** use Suunto API route above.
- **Bulk export files:** use `sync_suunto.py` (added in this repo) to import csv/json/gpx/tcx and optional fit.

### `sync_suunto.py` usage

1. Create folder `suunto_export/` in repo root.
2. Put your exported files there (or unzip export there).
3. Dry-run parser:

```bash
python3 sync_suunto.py --input-dir suunto_export --dry-run
```

4. Write to InfluxDB:

```bash
python3 sync_suunto.py --input-dir suunto_export
```

Notes:
- FIT parsing is optional and requires `fitparse` installed (`python3 -m pip install fitparse`).
- Imported points are tagged with `source=suunto_export`.
- Workouts go to `workouts`; daily metrics go to `daily_health`.

## Recommended workflow for your setup

1. Keep Strava sync for workouts (`sync_strava.py`).
2. Get Suunto API credentials for daily health.
3. Run `/api/suunto/sync` regularly (or add a cron job).
4. If only zip export is available, add a converter/import script next.

## Next step I can do for you

If you share your Suunto API credentials setup status, I can add:

- a `sync_suunto.py` CLI job (non-HTTP),
- deduped daily writes,
- a cron-ready command,
- and optional importer for Suunto export zip/FIT files.

---

# Polar options (while Suunto API is pending)

If you also have long history in Polar, this is often the fastest way to improve coverage.

## Polar bulk export options

### 1) Official full-account download (recommended first)

From your Polar account page (`account.polar.com`), use **Download your data**.

- This is the account-level portability export.
- Polar sends an email when export is ready.
- Download window is typically time-limited.

Important:
- This export may not include all algorithm-derived fields in directly reusable form.
- Still useful as a one-time archive/backfill source.

### 2) Polar Flow individual session export

From Polar Flow web, you can export individual sessions (commonly TCX/other formats).

- Good quality per workout
- Not ideal for years of history unless automated

### 3) Bulk session export via tooling

There are community scripts/tools to bulk-export Polar Flow sessions (often TCX).

- Useful for historical migration
- Treat as unofficial and review before use

## How to load Polar export into this repo

Current status:
- `sync_suunto.py` already supports `csv/json/gpx/tcx` and optional `fit`.
- That means many Polar-exported files can already be ingested if dropped into an input folder.

Suggested flow:

1. Export/download from Polar (account export and/or sessions).
2. Place files in a directory, for example `polar_export/`.
3. Dry-run parser:

```bash
python3 sync_suunto.py --input-dir polar_export --dry-run
```

4. Write to InfluxDB:

```bash
python3 sync_suunto.py --input-dir polar_export
```

## Practical recommendation for your setup

1. Use Strava full export for long workout backfill now.
2. Request Suunto full export (already in progress) and later API access.
3. Add Polar account export in parallel for decade-scale completeness.
4. Import all exports into Influx and tag sources (`suunto_export`, `polar_export`, `strava_export`).
