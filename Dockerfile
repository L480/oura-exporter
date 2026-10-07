# syntax=docker/dockerfile:1.7

FROM python:3.14-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie
RUN groupadd --system --gid 6872 oura-exporter \
    && useradd --system --uid 6872 --gid 6872 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin oura-exporter \
    && install -d -m 0700 -o 6872 -g 6872 /data
COPY --from=build /app/.venv /app/.venv
# pip is not needed at runtime and its vendored libraries only trigger CVE findings.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip* \
    /usr/local/bin/pip* \
    /usr/local/lib/python3.14/ensurepip/_bundled
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# A path, not a secret.
# hadolint ignore=DL3064
ENV OURA_TOKEN_PATH=/data/oauth_token.json
USER 6872:6872
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["oura-exporter", "--healthcheck"]
ENTRYPOINT ["oura-exporter"]
