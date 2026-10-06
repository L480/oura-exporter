import argparse
import http.client
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping, Sequence
from types import FrameType

from oura_exporter.config import ConfigError, Settings, parse_listen_address, parse_port

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
HEALTHCHECK_TIMEOUT = 3.0


def healthcheck_target(environ: Mapping[str, str]) -> tuple[str, int]:
    host = parse_listen_address(environ)
    if host == "0.0.0.0":  # noqa: S104
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    return host, parse_port(environ)


def healthcheck_url(environ: Mapping[str, str]) -> str:
    host, port = healthcheck_target(environ)
    return f"http://[{host}]:{port}/metrics" if ":" in host else f"http://{host}:{port}/metrics"


def healthcheck(environ: Mapping[str, str]) -> int:
    try:
        host, port = healthcheck_target(environ)
        url = healthcheck_url(environ)
    except ConfigError as exc:
        print(f"healthcheck: {exc}", file=sys.stderr)
        return 1
    connection = http.client.HTTPConnection(host, port, timeout=HEALTHCHECK_TIMEOUT)
    try:
        connection.request("GET", "/metrics")
        status = connection.getresponse().status
    except (OSError, http.client.HTTPException) as exc:
        print(f"healthcheck: GET {url} failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    if status != 200:
        print(f"healthcheck: GET {url} answered HTTP {status}", file=sys.stderr)
        return 1
    return 0


def _serve(settings: Settings) -> int:
    from prometheus_client import start_http_server

    from oura_exporter.api import OuraClient, build_session
    from oura_exporter.auth import AUTHORIZE_URL, OAuthClient, TokenManager, authenticate
    from oura_exporter.definitions import load_definitions
    from oura_exporter.exporter import Exporter
    from oura_exporter.storage import TokenStore

    definitions = load_definitions(settings.metrics_config)
    store = TokenStore(settings.token_path)
    store.prepare()
    store.acquire_lock()
    api_session = build_session()
    oauth_session = build_session(retries=False)
    try:
        oauth = OAuthClient(
            oauth_session,
            settings.client_id,
            settings.client_secret,
            settings.redirect_uri,
            settings.scopes,
            settings.token_url,
            AUTHORIZE_URL,
        )
        tokens = TokenManager(store, oauth)
        authenticate(tokens, store, oauth, settings, interactive=sys.stdin.isatty())
        exporter = Exporter(
            OuraClient(api_session, tokens, settings.api_base_url),
            tokens,
            definitions,
            settings.poll_interval,
        )
        try:
            server, _ = start_http_server(
                settings.port, addr=settings.listen_address, registry=exporter.registry
            )
        except OSError as exc:
            logger.error("cannot listen on %s:%d: %s", settings.listen_address, settings.port, exc)
            return 1
        logger.info(
            "serving metrics on http://%s:%d/metrics", settings.listen_address, settings.port
        )
        stop = threading.Event()

        def request_stop(_signum: int, _frame: FrameType | None) -> None:
            stop.set()

        previous = {
            number: signal.signal(number, request_stop)
            for number in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            exporter.run(stop)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
            server.shutdown()
            server.server_close()
        logger.info("stopped")
        return 0
    finally:
        api_session.close()
        oauth_session.close()
        store.release_lock()


def serve(environ: Mapping[str, str]) -> int:
    # The heavy imports are deferred so --version and --healthcheck start fast.
    from oura_exporter.auth import ConsentRequired

    try:
        settings = Settings.from_env(environ)
    except ConfigError as exc:
        print(f"oura-exporter: configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=settings.log_level, format=LOG_FORMAT)
    logging.getLogger("urllib3").setLevel(
        logging.DEBUG if settings.log_level <= logging.DEBUG else logging.ERROR
    )
    for warning in settings.warnings:
        logger.warning("%s", warning)
    try:
        return _serve(settings)
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 2
    except ConsentRequired as exc:
        logger.error("%s", exc)
        logger.error("Authorization URL: %s", exc.authorize_url)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oura-exporter", description="Prometheus exporter for Oura Ring data."
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="probe the local /metrics endpoint; exit 0 if it answers HTTP 200",
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    if args.version:
        from oura_exporter import __version__

        print(f"oura-exporter {__version__}")
        return 0
    if args.healthcheck:
        return healthcheck(env)
    return serve(env)


if __name__ == "__main__":
    sys.exit(main())
