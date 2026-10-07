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

`example/oura.prom` is the exposition produced from the fixtures in `tests/fixtures`, and the
metric list in `README.md` is rendered from `metrics.yml`. Tests compare against both. After
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
- A category also takes optional `title` and `summary` keys for the README list. The
  defaults are the name with spaces, and `latest day`, `most recent sample` or `profile`
  depending on its kind.
- Add the field to the matching file in `tests/fixtures` (and its `_nulls` variant), then
  regenerate the golden files.

## Invariants a PR must not break

1. **Never lose the newest refresh token.** Oura refresh tokens are single-use. A refreshed
   token is assigned in memory first, then written atomically, and a failed write is retried
   and surfaced through `oura_exporter_token_persisted`.
2. **Never exchange an authorization code with a freshly generated PKCE verifier.** Without
   a matching pending authorization the exporter asks for a new code instead.
3. **No tokens, secrets or personal data in logs or labels.** Data metrics have no labels.
4. **Any field can be `null`**, the user may untick scopes on the consent page. Parsing is
   tolerant and drops a series instead of failing the category.
5. **`/metrics` only starts after authentication succeeded**, and the exporter has to keep
   working with `--read-only --cap-drop=ALL --security-opt no-new-privileges`.

## Commits

Plain, descriptive commit messages without type prefixes. Releases are cut by pushing a `v*`
tag, which triggers the image build and the GitHub Release.
