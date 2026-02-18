# InfluxDB v2 local notes

This project now uses Homebrew `influxdb@2` + `influxdb-cli`.

## Installed versions

- `influxd`: InfluxDB 2.8.0
- `influx` CLI: 2.7.5

## Service status

The service is enabled and configured to start automatically at login via Homebrew LaunchAgent:

```bash
brew services list | awk 'NR==1 || /influx/'
```

## Start / stop / restart / disable

- Start and enable auto-start:

```bash
brew services start influxdb@2
```

- Stop service now (keeps auto-start setting for next time unless disabled):

```bash
brew services stop influxdb@2
```

- Restart service:

```bash
brew services restart influxdb@2
```

- Disable auto-start (unload LaunchAgent) and stop:

```bash
brew services stop influxdb@2
```

- Start without auto-start (manual foreground run):

```bash
INFLUXD_CONFIG_PATH="/opt/homebrew/etc/influxdb2/config.yml" /opt/homebrew/opt/influxdb@2/bin/influxd
```

## Quick health checks

```bash
curl -sS http://localhost:8086/health
influx version
```

## Setup completed in this environment

- Organization: `auroran`
- Bucket: `health`
- Bucket was created and is ready
- All-access API token was created
- Token was stored at `secrets.yaml` under `influxdb_token`

## Optional bucket checks

List buckets (requires an admin/all-access token):

```bash
influx bucket list --host http://localhost:8086 --org auroran --token "$INFLUXDB_TOKEN"
```
