import contextlib
import http.client
import http.server
import importlib.metadata
import json
import logging
import signal
import socket
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import prometheus_client
import pytest
import responses

import oura_exporter
from oura_exporter import __main__ as cli
from oura_exporter import __version__
from oura_exporter import auth as auth_module
from oura_exporter.auth import AUTHORIZE_URL, ConsentRequired
from oura_exporter.exporter import Exporter
from oura_exporter.storage import TokenStore

from .helpers import BASE_URL, WRITE_URL, decode_write_request, register_endpoints

ROOT = Path(__file__).resolve().parents[1]
WILDCARD = "0.0.0.0"  # noqa: S104


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    block = threading.Event()

    def do_GET(self) -> None:
        if self.path == "/slow":
            self.block.wait(5)
        if self.path not in {"/metrics", "/slow"}:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(Handler.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(b"ok")

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def local_server() -> Iterator[http.server.ThreadingHTTPServer]:
    Handler.status = 200
    Handler.block = threading.Event()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    yield server
    Handler.block.set()
    server.shutdown()
    server.server_close()
    thread.join(5)


def seed_token(directory: Path, **overrides: Any) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "oauth_token.json"
    body = {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "client_id": "cid",
    }
    body.update(overrides)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "OURA_CLIENT_ID": "cid",
        "OURA_CLIENT_SECRET": "secret",
        "OURA_REMOTE_WRITE_URL": "http://prometheus.test/api/v1/write",
        "OURA_TOKEN_PATH": str(tmp_path / "data" / "oauth_token.json"),
        **extra,
    }


class NoTty:
    def isatty(self) -> bool:
        return False


class Tty:
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", NoTty())


class TestVersion:
    def test_version_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().out == f"oura-exporter {__version__}\n"

    def test_version_matches_pyproject(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            assert tomllib.load(handle)["project"]["version"] == __version__

    def test_version_falls_back_when_the_package_is_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.delattr(oura_exporter, "__version__")
        monkeypatch.setattr(importlib.metadata, "version", missing)
        assert oura_exporter.__version__ == "0.0.0"

    def test_unknown_package_attributes_raise(self) -> None:
        with pytest.raises(AttributeError, match="no_such_thing"):
            _ = oura_exporter.no_such_thing

    @pytest.mark.parametrize(
        ("call", "forbidden"),
        [
            ("main(['--version'])", ["requests", "yaml", "prometheus_client", "urllib3"]),
            (
                "main(['--healthcheck'], {'PORT': '9'})",
                ["requests", "yaml", "prometheus_client", "urllib3", "importlib.metadata"],
            ),
        ],
    )
    def test_cheap_commands_stay_light(self, call: str, forbidden: list[str]) -> None:
        script = (
            "import sys\n"
            "from oura_exporter.__main__ import main\n"
            f"{call}\n"
            f"print(sorted(set({forbidden!r}) & set(sys.modules)))\n"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.stdout.splitlines()[-1] == "[]"

    def test_unknown_flags_are_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as caught:
            cli.main(["--frobnicate"])
        assert caught.value.code == 2
        assert "--frobnicate" in capsys.readouterr().err


class TestHealthcheckTarget:
    @pytest.mark.parametrize(
        ("environ", "host", "url"),
        [
            ({}, "127.0.0.1", "http://127.0.0.1:8000/metrics"),
            (
                {"LISTEN_ADDRESS": WILDCARD, "PORT": "9100"},
                "127.0.0.1",
                "http://127.0.0.1:9100/metrics",
            ),
            ({"LISTEN_ADDRESS": "  "}, "127.0.0.1", "http://127.0.0.1:8000/metrics"),
            ({"LISTEN_ADDRESS": "::"}, "::1", "http://[::1]:8000/metrics"),
            ({"LISTEN_ADDRESS": "::1"}, "::1", "http://[::1]:8000/metrics"),
            ({"LISTEN_ADDRESS": "fe80::1", "PORT": "1"}, "fe80::1", "http://[fe80::1]:1/metrics"),
            ({"LISTEN_ADDRESS": "192.168.1.5"}, "192.168.1.5", "http://192.168.1.5:8000/metrics"),
            (
                {"LISTEN_ADDRESS": "localhost", "PORT": "65535"},
                "localhost",
                "http://localhost:65535/metrics",
            ),
        ],
    )
    def test_wildcards_and_ipv6(self, environ: dict[str, str], host: str, url: str) -> None:
        assert cli.healthcheck_target(environ)[0] == host
        assert cli.healthcheck_url(environ) == url

    def test_only_port_and_listen_address_matter(self) -> None:
        assert cli.healthcheck_url({"OURA_POLL_INTERVAL": "garbage", "LOGLEVEL": "x"}) == (
            "http://127.0.0.1:8000/metrics"
        )


class TestHealthcheck:
    def run(self, server: http.server.ThreadingHTTPServer) -> int:
        return cli.main(["--healthcheck"], {"PORT": str(server.server_address[1])})

    def test_ok(self, local_server: http.server.ThreadingHTTPServer) -> None:
        assert self.run(local_server) == 0

    @pytest.mark.parametrize("status", [500, 503, 404, 204])
    def test_any_other_status_fails(
        self,
        local_server: http.server.ThreadingHTTPServer,
        status: int,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        Handler.status = status
        assert self.run(local_server) == 1
        assert str(status) in capsys.readouterr().err

    def test_connection_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["--healthcheck"], {"PORT": str(free_port())}) == 1
        assert "failed" in capsys.readouterr().err

    def test_timeout(
        self,
        local_server: http.server.ThreadingHTTPServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cli, "HEALTHCHECK_TIMEOUT", 0.2)
        original = http.client.HTTPConnection.request

        def slow(self: http.client.HTTPConnection, method: str, url: str, **kwargs: Any) -> None:
            original(self, method, "/slow", **kwargs)

        monkeypatch.setattr(http.client.HTTPConnection, "request", slow)
        assert self.run(local_server) == 1

    def test_invalid_port(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["--healthcheck"], {"PORT": "not-a-port"}) == 1
        assert "PORT" in capsys.readouterr().err

    def test_garbage_response(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def answer() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.recv(1024)
                connection.sendall(b"this is not http\r\n\r\n")

        thread = threading.Thread(target=answer)
        thread.start()
        try:
            assert cli.main(["--healthcheck"], {"PORT": str(port)}) == 1
        finally:
            thread.join(5)
            listener.close()


class TestExitCodes:
    def test_missing_client_id_is_a_configuration_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main([], {}) == 2
        assert "OURA_CLIENT_ID" in capsys.readouterr().err

    def test_invalid_setting(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main([], base_env(tmp_path, OURA_POLL_INTERVAL="5")) == 2
        assert "OURA_POLL_INTERVAL" in capsys.readouterr().err

    def test_broken_metrics_definition(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        broken = tmp_path / "metrics.yml"
        broken.write_text("categories: nope\n", encoding="utf-8")
        with caplog.at_level(logging.ERROR):
            code = cli.main([], base_env(tmp_path, OURA_METRICS_CONFIG=str(broken)))
        assert code == 2
        assert "configuration error" in caplog.text
        assert "categories" in caplog.text

    def test_token_path_that_is_a_directory(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = base_env(tmp_path)
        Path(env["OURA_TOKEN_PATH"]).mkdir(parents=True)
        with caplog.at_level(logging.ERROR):
            assert cli.main([], env) == 2
        assert "is a directory" in caplog.text

    def test_second_instance_is_refused(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = base_env(tmp_path)
        seed_token(tmp_path / "data")
        first = TokenStore(Path(env["OURA_TOKEN_PATH"]))
        first.prepare()
        first.acquire_lock()
        try:
            with caplog.at_level(logging.ERROR):
                assert cli.main([], env) == 2
        finally:
            first.release_lock()
        assert "another oura-exporter instance" in caplog.text

    def test_consent_required_without_a_tty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            code = cli.main([], base_env(tmp_path))
        assert code == 1
        assert AUTHORIZE_URL in caplog.text
        assert "client_id=cid" in caplog.text
        assert "no stored Oura token" in caplog.text
        assert "secret" not in caplog.text.replace("client_secret", "")

    @pytest.mark.parametrize(
        ("level", "expected"),
        [("INFO", logging.ERROR), ("WARNING", logging.ERROR), ("DEBUG", logging.DEBUG)],
    )
    def test_urllib3_retry_chatter_is_quiet_unless_debugging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str, expected: int
    ) -> None:
        urllib3_logger = logging.getLogger("urllib3")
        monkeypatch.setattr(urllib3_logger, "level", urllib3_logger.level)
        assert cli.main([], base_env(tmp_path, LOGLEVEL=level)) == 1
        assert urllib3_logger.level == expected

    def test_code_without_a_pending_authorization(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR):
            code = cli.main([], base_env(tmp_path, OURA_AUTH_CODE="whatever"))
        assert code == 1
        assert "no pending authorization matches" in caplog.text

    def test_startup_warnings_are_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = base_env(tmp_path)
        del env["OURA_CLIENT_SECRET"]
        with caplog.at_level(logging.WARNING):
            cli.main([], env)
        assert "OURA_CLIENT_SECRET" in caplog.text

    def test_time_zone_and_http_warnings_are_logged_at_startup(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = base_env(tmp_path, TZ="Nowhere/Land", OURA_API_BASE_URL="http://127.0.0.1:9")
        with caplog.at_level(logging.WARNING):
            assert cli.main([], env) == 1
        assert "TZ=Nowhere/Land is not a known time zone" in caplog.text
        assert "OURA_API_BASE_URL uses plain http" in caplog.text

    def test_keyboard_interrupt_during_consent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupted(*args: Any, **kwargs: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(auth_module, "authenticate", interrupted)
        assert cli.main([], base_env(tmp_path)) == 130

    @pytest.mark.parametrize(("stdin", "interactive"), [(Tty(), True), (NoTty(), False)])
    def test_interactivity_follows_stdin(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stdin: Any,
        interactive: bool,
    ) -> None:
        seen: list[bool] = []

        def spy(*args: Any, interactive: bool, **kwargs: Any) -> None:
            seen.append(interactive)
            raise ConsentRequired("stop here", "https://example.invalid/authorize")

        monkeypatch.setattr(sys, "stdin", stdin)
        monkeypatch.setattr(auth_module, "authenticate", spy)
        assert cli.main([], base_env(tmp_path)) == 1
        assert seen == [interactive]

    def test_port_already_in_use(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        seed_token(tmp_path / "data")
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            env = base_env(tmp_path, PORT=str(port), LISTEN_ADDRESS="127.0.0.1")
            with caplog.at_level(logging.ERROR):
                assert cli.main([], env) == 1
        assert "cannot listen" in caplog.text
        assert TokenStore(Path(env["OURA_TOKEN_PATH"])).load_token() is not None


class TestRun:
    def run_service(
        self,
        tmp_path: Path,
        rsps: responses.RequestsMock,
        monkeypatch: pytest.MonkeyPatch,
        signal_number: int,
        before_stop: Callable[[int], None] | None = None,
    ) -> tuple[int, str, int]:
        seed_token(tmp_path / "data")
        register_endpoints(rsps)
        rsps.add(responses.POST, WRITE_URL, status=204)
        port = free_port()
        env = base_env(
            tmp_path,
            PORT=str(port),
            LISTEN_ADDRESS="127.0.0.1",
            OURA_API_BASE_URL=BASE_URL,
        )
        handlers: dict[int, Any] = {}
        real_signal = signal.signal
        scraped: list[str] = []

        def recording_signal(number: int, handler: Any) -> Any:
            handlers[number] = handler
            return real_signal(number, handler)

        original_run = Exporter.run

        def run_once(self: Exporter, stop: threading.Event) -> None:
            self.poll(stop)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/metrics")
                scraped.append(connection.getresponse().read().decode())
            finally:
                connection.close()
            if before_stop is not None:
                before_stop(port)
            assert not stop.is_set()
            handlers[signal_number](signal_number, None)
            assert stop.is_set()
            original_run(self, stop)

        monkeypatch.setattr(signal, "signal", recording_signal)
        monkeypatch.setattr(Exporter, "run", run_once)
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
        code = cli.main([], env)
        assert {number: signal.getsignal(number) for number in previous} == previous
        return code, scraped[0], port

    @pytest.mark.parametrize("signal_number", [signal.SIGTERM, signal.SIGINT])
    def test_serves_metrics_and_stops_on_signal(
        self,
        tmp_path: Path,
        rsps: responses.RequestsMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        signal_number: int,
    ) -> None:
        with caplog.at_level(logging.INFO):
            code, body, port = self.run_service(tmp_path, rsps, monkeypatch, signal_number)
        assert code == 0
        assert "oura_exporter_build_info" in body
        assert "oura_daily_activity_score" not in body
        assert 'oura_exporter_category_up{category="daily_activity"} 1.0' in body
        pushed = [
            decode_write_request(call.request.body)
            for call in rsps.calls
            if call.request.url == WRITE_URL
        ]
        assert any(name == "oura_daily_activity_score" for request in pushed for name, _ in request)
        assert "serving metrics" in caplog.text
        assert "stopped" in caplog.text
        with (
            pytest.raises(ConnectionRefusedError),
            socket.create_connection(("127.0.0.1", port), timeout=1),
        ):
            pass
        store = TokenStore(tmp_path / "data" / "oauth_token.json")
        store.acquire_lock()
        store.release_lock()

    def test_the_server_only_starts_after_authentication(
        self,
        tmp_path: Path,
        rsps: responses.RequestsMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[str] = []
        real_authenticate = auth_module.authenticate
        real_start = prometheus_client.start_http_server

        def authenticate(*args: Any, **kwargs: Any) -> None:
            events.append("authenticate")
            real_authenticate(*args, **kwargs)

        def start(*args: Any, **kwargs: Any) -> Any:
            events.append("listen")
            return real_start(*args, **kwargs)

        monkeypatch.setattr(auth_module, "authenticate", authenticate)
        monkeypatch.setattr(prometheus_client, "start_http_server", start)
        code, _, _ = self.run_service(tmp_path, rsps, monkeypatch, signal.SIGTERM)
        assert code == 0
        assert events == ["authenticate", "listen"]
