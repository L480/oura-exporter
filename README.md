# oura-exporter

[![CI](https://github.com/L480/oura-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/L480/oura-exporter/actions/workflows/ci.yml)
[![GHCR](https://img.shields.io/badge/ghcr.io-l480%2Foura--exporter-blue?logo=github)](https://github.com/L480/oura-exporter/pkgs/container/oura-exporter)

Prometheus exporter for [Oura Ring](https://ouraring.com) data: daily activity, readiness,
sleep, resilience, SpO2 and stress scores, the latest heart rate and ring battery level, and
the profile. A fork of [legnoh/oura-exporter](https://github.com/legnoh/oura-exporter),
rewritten around OAuth only, with atomically saved single-use refresh tokens, tolerant parsing
of missing fields and a hardened container image.

## Quick start

1. Create an application in the [Oura developer portal](https://cloud.ouraring.com/oauth/applications)
   and register the redirect URI `http://localhost:8000/callback`.
2. `cp .env.example .env` and fill in `OURA_CLIENT_ID` and `OURA_CLIENT_SECRET`.
3. Authorize once: `docker compose run --rm -it oura-exporter`, open the printed URL, approve
   access and paste the `code` (or the whole redirect URL). Stop with Ctrl-C afterwards.
4. `docker compose up -d`, metrics are served on `http://127.0.0.1:8000/metrics`.

`/data` in the container must be a directory writable by UID 6872, never a single mounted file.
All settings are documented in [`.env.example`](./.env.example).

## Metrics

See [`metrics.yml`](./src/oura_exporter/metrics.yml) for the definitions and
[`example/oura.prom`](./example/oura.prom) for a complete exposition.

## Disclaimer

- This script is NOT authorized by Oura.
  - We are not responsible for any damages caused by using this script.
- This script is not intended to overload these sites or services.
  - When using this script, please keep your request frequency within a sensible range.
