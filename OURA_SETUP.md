# Oura API Setup

This branch adds Oura API v2 support using OAuth2.

## Local Secrets

Add these keys to `secrets.json` or export them as environment variables:

```json
{
  "oura_client_id": "your-client-id",
  "oura_client_secret": "your-client-secret",
  "oura_redirect_uri": "http://localhost:8080/callback"
}
```

Do not commit `secrets.json` or `oura_tokens.json`; JSON files are ignored by this repo.

## Authorize

The Oura app callback URI is:

```text
http://localhost:8080/callback
```

Run:

```bash
./refresh_oura_tokens.py
```

Open the printed authorization URL, approve access, and the helper will save
`oura_tokens.json`.

If the browser shows a localhost callback page on your own machine instead of
reaching the server, copy the `code` query parameter from the callback URL and
run:

```bash
./refresh_oura_tokens.py --code "PASTE_CODE_HERE"
```

By default the helper requests the same scopes as the Oura app example:

```text
email personal daily heartrate tag workout session spo2 ring_configuration stress heart_health
```

## Sync

Dry run:

```bash
./sync_oura.py --days 7 --dry-run
```

Write to InfluxDB:

```bash
./sync_oura.py --days 30
```

The sync writes Oura daily summaries into the existing `daily_health`
measurement using dashboard-compatible fields.

Oura is treated as primary for sleep, HRV, sleep heart-rate, readiness, steps,
and calorie fields. Fitbit remains the preferred source for weight, and Strava
remains the source for workouts/training load.
