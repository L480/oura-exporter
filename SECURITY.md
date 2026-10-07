# Security Policy

## Supported versions

Only the latest published release (image tags `latest` / `X.Y.Z`) is supported with security
fixes. There is no LTS branch.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities. Report privately via
[GitHub Security Advisories](https://github.com/L480/oura-exporter/security/advisories/new).

Expect an initial response within a few days.

## Threat model of a deployment

This exporter holds a long-lived credential for personal health data and publishes that data.
The deployment, not just the code, is part of the security surface:

- **`/metrics` serves personal health data without authentication.** Bind it to localhost or a
  private network, as the compose example does (`127.0.0.1:8000`). If it must be reachable
  from elsewhere, put a reverse proxy that authenticates in front of it.
- **The token file grants read access to the user's Oura data until it is revoked.** It is
  written with mode `0600` in a `0700` directory. Protect the data volume and every backup of
  it. If it leaks, revoke the application's access in the Oura account settings; a new
  authorization then issues a new token.
- **Prefer `OURA_CLIENT_SECRET_FILE`** over `OURA_CLIENT_SECRET`. Environment variables are
  visible through `docker inspect` and to anyone who can read the process environment, a
  mounted file is not.
- **Logs never contain tokens, refresh tokens or the client secret.** The authorization URL in
  the logs is not secret: it carries only the client ID, the PKCE challenge and a random
  `state`. An authorization code is single-use and useless without the verifier kept in the
  data volume.
- **Refresh tokens are single-use.** Run one instance per data directory (a lock enforces it)
  and keep that directory on storage that survives restarts, or the next refresh needs a new
  authorization.
- **The container is hardened by default**: non-root user (UID 6872), read-only root file
  system, no capabilities, `no-new-privileges`. The only writable path is `/data`.
