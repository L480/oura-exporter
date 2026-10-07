# Details

Everything the [README](../README.md) leaves out. All settings are documented in
[`.env.example`](../.env.example).

## Authorization

The exporter only supports OAuth. Oura removed personal access tokens, so `OURA_ACCESS_TOKEN` is
ignored with a warning. Register the redirect URI (`http://localhost:8000/callback` by default)
in your Oura application exactly as it is configured in `OURA_REDIRECT_URI`.

**Interactive.** `docker compose run --rm -it oura-exporter` prints an authorization URL and asks
for the code. Open the URL and approve access. The browser is redirected to the redirect URI,
which does not have to answer: copy the whole address, or only its `code` value, into the prompt.
You get three attempts. Stop the service first (`docker compose stop`), a data directory can only
be used by one instance.

**Headless.** Without a terminal the exporter logs the URL and exits with status 1
(`docker compose logs oura-exporter`). Open the URL on any machine, approve access, put the code
(or the redirect address) into `OURA_AUTH_CODE`, or a file holding it into `OURA_AUTH_CODE_FILE`,
and start again.

- The URL is the same after every restart as long as the client ID, redirect URI and scopes
  stay the same: the PKCE verifier is kept in the data directory.
- Codes are single-use and short-lived. If one is rejected the same URL can be used again.
- The variable is ignored once a token exists and can be removed.
- With `restart: unless-stopped` the container keeps restarting until a token exists.

**Re-authorization.** If Oura rejects the refresh token (access revoked, or the token was used
elsewhere), the exporter keeps running with the last values, logs an error and sets
`oura_exporter_auth_ok` to 0. Stop it and authorize again, interactively or with the headless
steps above.

## Data directory

The token, the pending authorization and a lock live in the directory of `OURA_TOKEN_PATH`
(`/data/oauth_token.json` in the image). Oura refresh tokens are single-use, so every refresh
writes a new one: the file is replaced atomically, and `oura_exporter_token_persisted` drops to
0 if that fails.

- `/data` must be a directory writable by UID 6872, the user of the image. The compose file's
  named volume is created with the right owner. For a bind mount, or when you override `user:`
  in compose, `chown` it to that user, for example `mkdir data && sudo chown 6872:6872 data &&
  chmod 700 data`.
- Never bind-mount a single file. Docker creates a directory for a missing source file and the
  exporter refuses to start.
- One instance per directory, enforced by a lock. Two instances would invalidate each other's
  refresh tokens.

## Prometheus

Metrics carry no label that identifies the person, add it in the scrape configuration. Oura
data changes a few times a day, so a modest interval is enough:

```yaml
scrape_configs:
  - job_name: oura
    scrape_interval: 5m
    static_configs:
      - targets: ["oura-exporter:8000"]
        labels:
          person: alice
```

## How values behave

- **Latest document.** Daily categories export the newest document of the last 7 days. `sleep`
  uses the `long_sleep` document of the latest day that ends last, `heartrate` and
  `ring_battery_level` ask Oura for the most recent sample, and the profile is refreshed hourly.
- **Age.** `<prefix>timestamp_seconds` is the local midnight of the document's day, or the time
  of the sample. `time() - oura_daily_sleep_timestamp_seconds` is how old the data is.
- **Missing values.** A field Oura reports as `null`, for example because a scope was not
  granted or there is not enough data, is left out: the series disappears instead of showing 0.
- **Errors.** When a request fails the previous values stay. `oura_exporter_category_up` shows the
  failure and the timestamps show the age.
- **Ordinal values.** `oura_daily_resilience_level`: 1 limited, 2 adequate, 3 solid, 4 strong,
  5 exceptional. `oura_daily_stress_day_summary`: 1 restored, 2 normal, 3 stressful.
- **State sets.** `oura_heartrate_source{oura_heartrate_source="rest"}` has one series per state
  (awake, rest, sleep, session, live, workout), the current one is 1.
- **Info.** `oura_personal_info_biological_sex_info{biological_sex="male"}` is always 1.
- **Time zone.** `TZ` decides what "today" is and where a day starts. It defaults to UTC in the
  image. An unknown name falls back to UTC silently in the C library, so the exporter warns.
- **Own definitions.** `OURA_METRICS_CONFIG` points to a replacement for the packaged
  [`metrics.yml`](../src/oura_exporter/metrics.yml), see [CONTRIBUTING](../CONTRIBUTING.md).

## Exporter metrics

| Metric | Meaning |
| --- | --- |
| `oura_exporter_build_info{version}` | Always 1. |
| `oura_exporter_auth_ok` | 1 after a successful API response, 0 after an authentication failure. |
| `oura_exporter_token_persisted` | 0 while a refreshed token could not be saved to disk. |
| `oura_exporter_category_up{category}` | 1 if the last fetch succeeded, 0 if it failed or was skipped because a rate limit or an authentication failure ended the cycle. |
| `oura_exporter_category_last_success_timestamp_seconds{category}` | Time of the last successful fetch. |
| `oura_exporter_category_errors_total{category,reason}` | Failed fetches. Reasons: `network`, `invalid_response`, `auth`, `forbidden`, `rate_limited`, `http_error`, `internal`. |

The standard `process_*` metrics and `python_info` are exposed as well.

## Example alert rules

```yaml
groups:
  - name: oura
    rules:
      - alert: OuraDataStale
        expr: time() - oura_daily_sleep_timestamp_seconds > 2 * 86400
        for: 1h
      - alert: OuraAuthFailing
        expr: oura_exporter_auth_ok == 0
        for: 30m
      - alert: OuraTokenNotPersisted
        expr: oura_exporter_token_persisted == 0
        for: 15m
      - alert: OuraCategoryDown
        expr: oura_exporter_category_up == 0
        for: 1h
```

## Limitations

- Data only arrives when the Oura app syncs with the ring. Sleep data needs the app to be
  opened; activity and stress may sync in the background.
- Heart rate is the most recent sample only, not a time series.
- HTTP 403 means the scope was not granted or the Oura membership has expired. The category is
  reported as `forbidden` and retried hourly.
- One exporter serves one Oura account. Run one instance per person, each with its own data
  directory and port.
- The API is polled, webhooks are not used. Rate limits (HTTP 429) are respected through the
  `Retry-After` header.

## Running from source

Requires [uv](https://docs.astral.sh/uv/); Python 3.14 is selected from `.python-version`.

```bash
git clone https://github.com/L480/oura-exporter.git && cd oura-exporter
uv sync
cp .env.example .env   # edit it
uv run --env-file .env oura-exporter
```

`oura-exporter --version` prints the version and `oura-exporter --healthcheck` probes the local
`/metrics` endpoint, which is what the image's `HEALTHCHECK` runs. Exit codes: 0 clean stop,
1 authorization missing or the port could not be bound, 2 invalid configuration.

## Migrating from legnoh/oura-exporter

- Personal access tokens are gone. `OURA_ACCESS_TOKEN` is ignored; use OAuth as above.
- The `email` label is removed from every metric and `oura_personal_info_email_info` no longer
  exists. The `email` scope is no longer requested.
- The token must live in a directory, not a single mounted file. The image runs as UID 6872 and
  uses `/data`. An existing token file is accepted: put it into the data directory as
  `oauth_token.json` while the old container is stopped.
- The default poll interval is 300 s instead of 60 s.
- `uv run main.py` became `oura-exporter`, and `config/metrics.yml` is now packaged as
  `src/oura_exporter/metrics.yml`.
- Metrics are renamed to carry units, and `info` metrics became numbers or state sets:

| Old | New |
| --- | --- |
| `oura_daily_activity_active_calories` | `oura_daily_activity_active_calories_kilocalories` |
| `oura_daily_activity_equivalent_walking_distance` | `oura_daily_activity_equivalent_walking_distance_meters` |
| `oura_daily_activity_high_activity_time` | `oura_daily_activity_high_activity_time_seconds` |
| `oura_daily_activity_low_activity_time` | `oura_daily_activity_low_activity_time_seconds` |
| `oura_daily_activity_medium_activity_time` | `oura_daily_activity_medium_activity_time_seconds` |
| `oura_daily_activity_non_wear_time` | `oura_daily_activity_non_wear_time_seconds` |
| `oura_daily_activity_resting_time` | `oura_daily_activity_resting_time_seconds` |
| `oura_daily_activity_sedentary_time` | `oura_daily_activity_sedentary_time_seconds` |
| `oura_daily_activity_target_calories` | `oura_daily_activity_target_calories_kilocalories` |
| `oura_daily_activity_total_calories` | `oura_daily_activity_total_calories_kilocalories` |
| `oura_daily_readiness_temperature_deviation` | `oura_daily_readiness_temperature_deviation_celsius` |
| `oura_daily_readiness_temperature_trend_deviation` | `oura_daily_readiness_temperature_trend_deviation_celsius` |
| `oura_daily_resilience_level_info{val}` | `oura_daily_resilience_level` (1-5) |
| `oura_daily_spo2_spo2_percentage_average` | `oura_daily_spo2_average_percent` |
| `oura_daily_stress_stress_high` | `oura_daily_stress_stress_high_seconds` |
| `oura_daily_stress_recovery_high` | `oura_daily_stress_recovery_high_seconds` |
| `oura_daily_stress_day_summary_info{val}` | `oura_daily_stress_day_summary` (1-3) |
| `oura_heartrate_source_info{val}` | `oura_heartrate_source{oura_heartrate_source="rest"}` (state set) |
| `oura_personal_info_age` | `oura_personal_info_age_years` |
| `oura_personal_info_weight` | `oura_personal_info_weight_kilograms` |
| `oura_personal_info_height` | `oura_personal_info_height_meters` |
| `oura_personal_info_biological_sex_info{val}` | `oura_personal_info_biological_sex_info{biological_sex}` |
| `oura_personal_info_email_info` | removed |

All other names are unchanged. `oura_heartrate_bpm` is now the most recent sample instead of the
last entry of a 24 hour window. New: `oura_sleep_*`, `oura_ring_battery_*`,
`oura_daily_readiness_contributors_sleep_regularity`,
`oura_daily_spo2_breathing_disturbance_index`, the `*_timestamp_seconds` metrics and the
`oura_exporter_*` self-metrics.

## Disclaimer

- This script is NOT authorized by Oura.
  - We are not responsible for any damages caused by using this script.
- This script is not intended to overload these sites or services.
  - When using this script, please keep your request frequency within a sensible range.
