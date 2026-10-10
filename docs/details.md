# Details

Everything the [README](../README.md) leaves out. All settings are documented in
[`.env.example`](../.env.example).

## Authorization

The exporter uses OAuth. Register the redirect URI (`http://localhost:8000/callback` by default)
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

## Receiver

The exporter pushes with Prometheus remote write 1.0 (`OURA_REMOTE_WRITE_URL`, optional basic
auth through `OURA_REMOTE_WRITE_USERNAME` and `OURA_REMOTE_WRITE_PASSWORD(_FILE)`). The receiver
needs an out-of-order window of at least `OURA_LOOKBACK_DAYS`:

```yaml
storage:
  tsdb:
    out_of_order_time_window: 7d
```

Prometheus also needs `--web.enable-remote-write-receiver`. For Mimir and Grafana Cloud enable
`out_of_order_time_window` for the tenant. Without it the receiver answers HTTP 400 ("too old")
and `oura_exporter_remote_write_samples_total{result="rejected"}` grows.

A receiver cannot overwrite a sample. Prometheus remote write 1.0 drops a whole request at the
first conflicting sample (same timestamp as the newest sample of the series, other value). The
exporter answers a rejected batch (HTTP 400) by splitting it in halves, at most 64 requests per
push, until it knows which samples are refused; everything else is stored.

`/metrics` carries only the exporter's own health, scrape it like any exporter:

```yaml
scrape_configs:
  - job_name: oura-exporter-health
    static_configs:
      - targets: ["oura-exporter:8000"]
```

Pushed series have no label that identifies the person, add it with `external_labels` or
relabeling on the receiver side. Keep the lookback at 3 days or more: sleep documents reach
about a day before it, and a late ring sync can add samples long after the fact.

## How values behave

- **Fetch and push.** Every `OURA_FETCH_INTERVAL` (default 600 s; 3600 s for the profile) a
  category is fetched from the Oura API. Every `OURA_POLL_INTERVAL` (default 120 s) the exporter
  pushes from the documents of the last successful fetch, without API requests. A category whose
  last fetch is older than 3 fetch intervals is no longer pushed, so a long outage shows up as
  stale series (logged once at info, and again when it recovers). A failed fetch keeps the cache
  and is retried in the next cycle.
- **Window.** Every fetch reads `[now - OURA_LOOKBACK_DAYS, now]` of every category, in ranges
  of at most 30 days (7 for heart rate and battery), and follows `next_token`. Nothing is
  fetched from a watermark: phone heart rate and late ring syncs can add older samples later.
  Samples older than the window are dropped.
- **De-duplication.** Each `(series, timestamp)` is sent at most once. The exporter remembers
  what it delivered; the memory is updated after a successful push only, so failures retry in the
  next cycle. After a restart the whole window is sent again: identical samples are harmless,
  samples that differ from what is stored are isolated and counted as rejected.
- **Samples** (`heartrate`, `ring_battery_level`) sit at their own `timestamp`.
- **Events** (`sleep`, `workout`, `session`, `enhanced_tag`, `rest_mode_period`) sit at one
  time of the document: sleep at `bedtime_end`, workouts and sessions at `start_datetime`, tags
  and rest mode at `start_time`. Workouts, sessions, tags and rest mode also get
  `<prefix>duration_seconds`. A tag without an end is an instant (0 s), rest mode without an
  end is skipped until it ends. Labels: `sleep_type`; `activity`, `intensity`, `source` for
  workouts; `session_type`, `mood`; `tag_type_code`, `custom_name`. Free text (workout `label`,
  tag `comment`) and the `email` are never exported.
- **Embedded series** are exported with the time of each item: heart rate and HRV of a night
  (`timestamp + i * interval`), MET per minute, session heart rate, HRV and motion count, and the
  digit strings of the sleep phases (30 s and 5 min), `movement_30_sec` and `class_5_min`. Empty
  items are skipped. The help text of each series lists the codes.
- **Daily documents** (`daily_*`, `sleep_time`, `vo2_max`) can be revised during the day, and a
  receiver cannot overwrite a sample. The newest document of a category is pushed at fetch time
  in every push cycle, like a gauge: its current value is always within Prometheus' 5-minute
  lookback (keep `OURA_POLL_INTERVAL` below that). Every older day is pushed once, 12 hours after it
  ended in the local time zone, at 23:59:59 of that day. `last_over_time(x[1d])` per day then
  gives the final value.
- **Profile** (`personal_info`, `ring_configuration`) is fetched hourly and pushed at the time of
  every push cycle, like the newest daily document.
- **Revisions.** A receiver keeps the first value of a timestamp, so values Oura still revises
  are held back instead of pushed early. Activity series (`met`, `class_5_min`) of a day that has
  not settled stop from the second-to-last `class_5_min` slot on, because Oura still revises the last two
  (the last holds the last synced minute; Oura pads `met` with 0.9 up to the end of the day).
  Sleep periods are pushed once the ring has synced at least 30 minutes after they ended (the
  end of the last `class_5_min` slot is the sync time; the ring does not sync while asleep). A value that changes after it was delivered anyway is not sent again; it is
  counted in `oura_exporter_sample_revisions_total{category}`, the details are logged at debug.
- **Missing values.** A field Oura reports as `null`, for example because a scope was not
  granted or there is not enough data, is left out.
- **Errors.** A failed fetch leaves the category as it is. A failed push is retried with the
  next cycle. `oura_exporter_category_up` shows fetch failures.
- **Enums** are numbers. Examples: `oura_daily_resilience_level` 1 limited, 2 adequate, 3 solid,
  4 strong, 5 exceptional; `oura_daily_stress_day_summary` 1 restored, 2 normal, 3 stressful;
  `oura_heartrate_source` 1 awake, 2 rest, 3 sleep, 4 session, 5 live, 6 workout. All codes are
  in the help text of the metric.
- **Info.** `oura_personal_info_biological_sex_info{biological_sex="male"}` is always 1.
- **Time zone.** `TZ` decides what "today" is and where a day ends. It defaults to UTC in the
  image. An unknown name falls back to UTC silently in the C library, so the exporter warns.
- **Own definitions.** `OURA_METRICS_CONFIG` points to a replacement for the packaged
  [`metrics.yml`](../src/oura_exporter/metrics.yml), see [CONTRIBUTING](../CONTRIBUTING.md).

## Backfill

`oura-exporter backfill --start 2026-01-01 [--end 2026-03-31] --output history.om` fetches a date
range and writes OpenMetrics text with timestamps, for `promtool`. It uses the same environment,
token and data directory as the service and must not run while the service holds the lock
(`docker compose stop` first). The `--end` default is today; `OURA_REMOTE_WRITE_URL` is not
needed. Completed days use the settled rule (23:59:59 of the day), nothing is stamped at fetch
time, so the profile is left out.

```bash
docker compose run --rm -v "$PWD:/out" oura-exporter backfill --start 2026-01-01 --output /out/history.om
promtool tsdb create-blocks-from openmetrics history.om ./blocks
# move the blocks into the receiver's data directory (Prometheus: --storage.tsdb.path)
```

Blocks older than the head's out-of-order window can only be imported this way. Prometheus picks
up new blocks without a restart. Mimir and Grafana Cloud need their own import path.

## Exporter metrics

| Metric | Meaning |
| --- | --- |
| `oura_exporter_build_info{version}` | Always 1. |
| `oura_exporter_auth_ok` | 1 after a successful API response, 0 after an authentication failure. |
| `oura_exporter_token_persisted` | 0 while a refreshed token could not be saved to disk. |
| `oura_exporter_category_up{category}` | 1 if the last fetch succeeded, 0 if it failed or was skipped because a rate limit or an authentication failure ended the cycle. |
| `oura_exporter_category_last_success_timestamp_seconds{category}` | Time of the last successful fetch. |
| `oura_exporter_category_fetches_total{category}` | Successful fetches. |
| `oura_exporter_category_errors_total{category,reason}` | Failed fetches. Reasons: `network`, `invalid_response`, `auth`, `forbidden`, `rate_limited`, `http_error`, `internal`. |
| `oura_exporter_remote_write_samples_total{result}` | Pushed samples: `sent`, or `rejected` (samples the receiver refused with HTTP 400, found by splitting the batch). |
| `oura_exporter_remote_write_failures_total{reason}` | Failed requests, retried next cycle. Reasons: `network`, `rate_limited`, `server_error`, `client_error`. |
| `oura_exporter_remote_write_last_success_timestamp_seconds` | Time of the last successful request. |
| `oura_exporter_sample_revisions_total{category}` | Samples Oura changed after delivery; not sent again. |

The standard `process_*` metrics and `python_info` are exposed as well.

## Example alert rules

```yaml
groups:
  - name: oura
    rules:
      - alert: OuraDataStale
        expr: absent_over_time(oura_daily_sleep_score{job="oura-exporter"}[2d])
        for: 1h
      - alert: OuraPushFailing
        expr: time() - oura_exporter_remote_write_last_success_timestamp_seconds > 3600
        for: 15m
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
- Daily values are revised by Oura. A day is pushed as final 12 hours after it ended, sleep once the ring
  has synced 30 minutes after it ended; later revisions are counted, not sent, because a receiver cannot
  overwrite a sample.
- Everything inside `OURA_LOOKBACK_DAYS` is read again in every fetch, which costs one request
  per category and range. Raise `OURA_FETCH_INTERVAL` before raising the lookback.
- Documents are cached in memory between fetches; nothing new reaches the receiver until the
  next fetch, at most `OURA_FETCH_INTERVAL` after Oura has it.
- HTTP 403 means the scope was not granted or the Oura membership has expired. The category is
  reported as `forbidden` and retried hourly. HTTP 401 on one category after another category was
  fetched in the same cycle is treated the same way (Oura answers 401 for a missing scope); a 401
  on the first category still counts as an authentication failure.
- Samples dated more than a minute ahead of the clock are dropped, the first drop per category is
  logged. Prometheus rejects them as out of bounds.
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

## Disclaimer

- This script is NOT authorized by Oura.
  - We are not responsible for any damages caused by using this script.
- This script is not intended to overload these sites or services.
  - When using this script, please keep your request frequency within a sensible range.
