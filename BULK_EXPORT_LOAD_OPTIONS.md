# Bulk Export and Load Options (All Sources)

This is the single reference file for bulk export/backfill options and load paths into this project.

Covered sources:
- Strava
- Apple Health
- Suunto
- Polar
- Oura
- Garmin

---

## Quick decision matrix

| Source | Best bulk export option | Ongoing sync option | What it is best for |
|---|---|---|---|
| Strava | Account bulk export (ZIP) | Strava API (`sync_strava.py`) | Long workout history and activity details |
| Apple Health | iPhone "Export All Health Data" (`export.xml`) | Periodic re-export + import script | Sleep/steps/HRV/HR where present |
| Suunto | Support data portability export | Suunto API (when approved) | Daily health + workouts |
| Polar | Polar account data download + Flow exports | Polar API/Flow integrations (if available) | Historical workouts and some wellness |
| Oura | Membership data export + Oura API | Oura API (PAT/OAuth) | Sleep/readiness/recovery metrics |
| Garmin | Garmin data management export | Garmin Connect API / integrations | Long workout history and training files |

---

## Strava

### Bulk export
- Use Strava account export:
  - `Settings -> My Account -> Download or Delete Your Account`
  - Receive ZIP by email

### Load into this repo
- Current fast route: run `python3 sync_strava.py` (API-based, pulls activities).
- Optional next step: add dedicated ZIP importer for full offline backfill.

---

## Apple Health

### Bulk export
- On iPhone Health app: profile -> **Export All Health Data**
- Produces `export.xml` (and optional CDA/XML extras)

### Load into this repo
- Use `python3 apple_health_sync.py --xml apple_health_export/export.xml`

### Important note about your current sleep question
- Yes, your export **does** contain sleep records.
- In your current file, recent sleep entries are mostly:
  - `HKCategoryTypeIdentifierSleepAnalysis`
  - value `HKCategoryValueSleepAnalysisInBed`
- If importer logic only accepts `Asleep*` records, many nights can appear missing.

---

## Suunto

### Bulk export options
1. App single-workout export (manual, not scalable)
2. Support data portability request (best one-time backfill)
3. Suunto API (best for ongoing automated sync, once approved)

### Load into this repo
- Use `sync_suunto.py` for export files:

```bash
python3 sync_suunto.py --input-dir suunto_export --dry-run
python3 sync_suunto.py --input-dir suunto_export
```

Supports `csv/json/gpx/tcx` and optional `fit` (`fitparse` dependency).

---

## Polar

### Bulk export options
1. Polar account-level data download (`account.polar.com`)
2. Polar Flow session exports (individual; can be automated with tools)
3. API/integration routes where available

### Load into this repo
- Put exports into e.g. `polar_export/` and run:

```bash
python3 sync_suunto.py --input-dir polar_export --dry-run
python3 sync_suunto.py --input-dir polar_export
```

---

## Oura

### Bulk export options
1. Oura Membership Hub export (CSV/download)
2. Oura API v2 (personal access token for personal data; OAuth for multi-user apps)

### Load into this repo
- If CSV/JSON: place in folder and import with `sync_suunto.py`
- For API-based pipeline: add a dedicated `sync_oura.py` for daily incremental sync

---

## Garmin

### Bulk export options
1. Garmin account data management export (full account dump)
2. Garmin Connect activity exports (`FIT/TCX/GPX`)
3. Garmin Connect developer APIs (requires approval)

### Load into this repo
- Put exported files in e.g. `garmin_export/` and run:

```bash
python3 sync_suunto.py --input-dir garmin_export --dry-run
python3 sync_suunto.py --input-dir garmin_export
```

---

## Recommended migration plan (for your case)

1. Backfill workouts with Strava full export + `sync_strava.py`.
2. Keep importing Apple Health regularly for daily wellness.
3. While waiting for Suunto API, ingest Suunto/Polar/Garmin/Oura exports via `sync_suunto.py`.
4. Once Suunto API is approved, move Suunto to incremental API sync.
5. Keep source tagging in Influx (e.g. `source=suunto_export`, `source=apple_health_export`) for traceability.

---

## Notes on data completeness

- Different vendors expose different fields in export bundles.
- "Full export" does not always include all algorithm-derived metrics.
- Missing fields in dashboard usually mean one of:
  1. source export does not include that metric
  2. metric exists under a different type/label than importer currently maps
  3. timezone/day-bucketing needs adjustment
