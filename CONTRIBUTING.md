# Contributing

Thanks for considering a contribution to `oura-exporter`.

## Development setup

Requires [uv](https://docs.astral.sh/uv/). Python 3.14 is picked up from `.python-version`.

```bash
uv sync
```

## Before opening a PR

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

All four must pass; `pytest` also enforces a coverage floor of 85 %. CI runs the same checks
plus `hadolint`, Trivy scans of the repository and of the built image, and a Docker build
with a smoke test.

## Golden files

`example/samples.txt` is the dump of the samples pushed for the fixtures in `tests/fixtures`
(series, timestamp, value), and the metric list in `README.md` is rendered from `metrics.yml`. Tests compare against both. After
changing `metrics.yml` or the fixtures, regenerate them and review the diff:

```bash
UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py tests/test_readme.py --no-cov
```

`UPDATE_GOLDEN=1` also rewrites the README metric list, but only the block between the
`BEGIN METRICS` and `END METRICS` markers.

## Adding a metric

Metrics are YAML only, add an entry to `src/oura_exporter/metrics.yml`:

- Name with the unit as a suffix (`_seconds`, `_percent`, `_celsius`, ...); scores are unitless.
- `path` is the dotted path into the Oura document, it defaults to the name.
- A missing or `null` value must mean "no series", never `0`.
- Every category has a `kind`: `daily` (one document per `day`), `sample` (one document is one
  sample at `timestamp`), `event` (a document with its own `time_path` and optionally an
  `end_path` for `<prefix>duration_seconds`) or `single` (profile). `labels` turn low-cardinality
  string fields into labels (never free text), `series` export embedded time series: `type:
  samples` for `{interval, items, timestamp}` objects, `type: string` for digit strings with a
  fixed `interval` and a `start` path. In a `daily` category one `type: string` series can set
  `sync_horizon: true`: while a day has not settled, the series of its document stop before the
  last slot of that string (the slot Oura is still filling).
- An `event` category can set `settle_delay` (seconds): the document is skipped until that long
  after its end (`end_path`, else `time_path`).
- Enums are gauges with a `mapping` to numbers; document the codes in `help`.
- A category also takes optional `title` and `summary` keys for the README list. The
  defaults are the name with spaces, and `daily value`, `every sample`, `every event` or
  `profile` depending on its kind.
- The order in the file is the order of the README list: headline values first. `contributors_*` metrics are listed on a line of their own, without the
  prefix.
- Add the field to the matching file in `tests/fixtures` (and its `_nulls` variant), then
  regenerate the golden files.
- `tests/test_openapi.py` checks `tests/fixtures/openapi.json` (Oura's OpenAPI spec): every
  `/v2/usercollection/*` endpoint needs a category, every numeric, boolean or enum field must
  be mapped or sit on the commented skip list in that test. Replace the file with the
  current spec to find new fields.

## Invariants a PR must not break

1. **Never lose the newest refresh token.** Oura refresh tokens are single-use. A refreshed
   token is assigned in memory first, then written atomically, and a failed write is retried
   and surfaced through `oura_exporter_token_persisted`.
2. **Never exchange an authorization code with a freshly generated PKCE verifier.** Without
   a matching pending authorization the exporter asks for a new code instead.
3. **No tokens, secrets or personal data in logs or labels.** Labels are low-cardinality
   fields from the Oura documents, never free text or the email.
4. **Any field can be `null`**, the user may untick scopes on the consent page. Parsing is
   tolerant and drops a series instead of failing the category.
5. **`/metrics` only starts after authentication succeeded**, and the exporter has to keep
   working with `--read-only --cap-drop=ALL --security-opt no-new-privileges`.
6. **A (series, timestamp) is sent at most once**; a later value is counted, never sent
   (receivers cannot overwrite, Prometheus drops the whole request on such a conflict).
7. **Values Oura still revises are not pushed** (sync horizon, settle delay).

## Commits

Plain, descriptive commit messages without type prefixes. Releases are cut by pushing a `v*`
tag, which triggers the image build and the GitHub Release.
