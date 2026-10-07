import logging
from pathlib import Path

import pytest

from oura_exporter.config import (
    ConfigError,
    Settings,
    parse_listen_address,
    parse_port,
)

BASE = {"OURA_CLIENT_ID": "client", "OURA_REMOTE_WRITE_URL": "http://prom:9090/api/v1/write"}
WITH_SECRET = {**BASE, "OURA_CLIENT_SECRET": "secret"}
HTTP_WARNING = (
    "OURA_API_BASE_URL uses plain http; access tokens are sent unencrypted "
    "(only use this for a local mock server)"
)


def time_zone_warning(value: str) -> str:
    return (
        f"TZ={value} is not a known time zone; the C library falls back to UTC, "
        'so "today" and the day timestamps use UTC'
    )


def env(**extra: str) -> dict[str, str]:
    return {**BASE, **extra}


def test_defaults() -> None:
    settings = Settings.from_env(BASE)
    assert settings.client_id == "client"
    assert settings.client_secret is None
    assert settings.redirect_uri == "http://localhost:8000/callback"
    assert settings.scopes == (
        "personal",
        "daily",
        "heartrate",
        "spo2",
        "stress",
        "workout",
        "session",
        "tag",
        "heart_health",
        "ring_configuration",
    )
    assert settings.lookback_days == 3
    assert settings.remote_write_url == "http://prom:9090/api/v1/write"
    assert settings.remote_write_username is None
    assert settings.remote_write_password is None
    assert settings.token_path == Path("~/.config/oura-exporter/oauth_token.json").expanduser()
    assert settings.auth_code is None
    assert settings.auth_code_file is None
    assert not settings.has_auth_code
    assert settings.read_auth_code() is None
    assert settings.poll_interval == 300
    assert settings.metrics_config is None
    assert settings.api_base_url == "https://api.ouraring.com"
    assert settings.token_url == "https://api.ouraring.com/oauth/token"
    assert settings.port == 8000
    assert settings.listen_address == "0.0.0.0"  # noqa: S104
    assert settings.log_level == logging.INFO
    assert len(settings.warnings) == 1
    assert "OURA_CLIENT_SECRET" in settings.warnings[0]
    assert "normally requires" in settings.warnings[0]


def test_values_are_stripped_and_empty_means_unset() -> None:
    settings = Settings.from_env(
        {
            "OURA_CLIENT_ID": "  client \n",
            "OURA_REMOTE_WRITE_URL": " http://prom:9090/api/v1/write ",
            "OURA_LOOKBACK_DAYS": " ",
            "OURA_REMOTE_WRITE_USERNAME": "",
            "OURA_REMOTE_WRITE_PASSWORD": " ",
            "OURA_REDIRECT_URI": "   ",
            "PORT": "",
            "OURA_POLL_INTERVAL": " ",
            "LOGLEVEL": "",
            "OURA_SCOPES": "",
            "OURA_AUTH_CODE": "  ",
        }
    )
    assert settings.client_id == "client"
    assert settings.remote_write_url == "http://prom:9090/api/v1/write"
    assert settings.lookback_days == 3
    assert settings.remote_write_username is None
    assert settings.remote_write_password is None
    assert settings.redirect_uri == "http://localhost:8000/callback"
    assert settings.port == 8000
    assert settings.poll_interval == 300
    assert settings.log_level == logging.INFO
    assert settings.scopes[0] == "personal"
    assert settings.auth_code is None


def test_all_values() -> None:
    settings = Settings.from_env(
        {
            "OURA_CLIENT_ID": "id",
            "OURA_REMOTE_WRITE_URL": "https://mimir.example.org/api/v1/push",
            "OURA_REMOTE_WRITE_USERNAME": "writer",
            "OURA_REMOTE_WRITE_PASSWORD": " hunter2 ",
            "OURA_LOOKBACK_DAYS": "7",
            "OURA_CLIENT_SECRET": " s3cret ",
            "OURA_REDIRECT_URI": "https://example.org/cb",
            "OURA_SCOPES": "email  daily\tspo2 daily",
            "OURA_TOKEN_PATH": "/data/token.json",
            "OURA_AUTH_CODE": "abc",
            "OURA_POLL_INTERVAL": "600",
            "OURA_METRICS_CONFIG": "/etc/metrics.yml",
            "OURA_API_BASE_URL": "http://127.0.0.1:9/",
            "PORT": "9100",
            "LISTEN_ADDRESS": "127.0.0.1",
            "LOGLEVEL": "debug",
        }
    )
    assert settings.client_secret == "s3cret"
    assert settings.remote_write_url == "https://mimir.example.org/api/v1/push"
    assert settings.remote_write_username == "writer"
    assert settings.remote_write_password == "hunter2"
    assert settings.lookback_days == 7
    assert settings.redirect_uri == "https://example.org/cb"
    assert settings.scopes == ("email", "daily", "spo2")
    assert settings.token_path == Path("/data/token.json")
    assert settings.auth_code == "abc"
    assert settings.has_auth_code
    assert settings.read_auth_code() == "abc"
    assert settings.poll_interval == 600
    assert settings.metrics_config == Path("/etc/metrics.yml")
    assert settings.api_base_url == "http://127.0.0.1:9"
    assert settings.token_url == "http://127.0.0.1:9/oauth/token"
    assert settings.port == 9100
    assert settings.listen_address == "127.0.0.1"
    assert settings.log_level == logging.DEBUG
    assert settings.warnings == (HTTP_WARNING,)


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({}, "OURA_CLIENT_ID"),
        ({"OURA_CLIENT_ID": "  "}, "OURA_CLIENT_ID"),
        ({"OURA_CLIENT_ID": "id"}, "OURA_REMOTE_WRITE_URL is required"),
        (env(OURA_REMOTE_WRITE_URL="prometheus:9090"), "OURA_REMOTE_WRITE_URL"),
        (env(OURA_REMOTE_WRITE_URL="ftp://prometheus/write"), "OURA_REMOTE_WRITE_URL"),
        (env(OURA_LOOKBACK_DAYS="0"), "OURA_LOOKBACK_DAYS"),
        (env(OURA_LOOKBACK_DAYS="-2"), "OURA_LOOKBACK_DAYS"),
        (env(OURA_LOOKBACK_DAYS="three"), "OURA_LOOKBACK_DAYS"),
        (env(OURA_REMOTE_WRITE_USERNAME="u"), "go together"),
        (env(OURA_REMOTE_WRITE_PASSWORD="p"), "go together"),
        (
            env(
                OURA_REMOTE_WRITE_USERNAME="u",
                OURA_REMOTE_WRITE_PASSWORD="p",
                OURA_REMOTE_WRITE_PASSWORD_FILE="/x",
            ),
            "OURA_REMOTE_WRITE_PASSWORD_FILE",
        ),
        (
            env(OURA_REMOTE_WRITE_USERNAME="u", OURA_REMOTE_WRITE_PASSWORD_FILE="/does/not/exist"),
            "OURA_REMOTE_WRITE_PASSWORD_FILE",
        ),
        (env(OURA_CLIENT_SECRET="a", OURA_CLIENT_SECRET_FILE="/x"), "OURA_CLIENT_SECRET"),
        (env(OURA_AUTH_CODE="a", OURA_AUTH_CODE_FILE="/x"), "OURA_AUTH_CODE"),
        (env(OURA_CLIENT_SECRET_FILE="/does/not/exist"), "OURA_CLIENT_SECRET_FILE"),
        (env(OURA_REDIRECT_URI="localhost:8000/callback"), "OURA_REDIRECT_URI"),
        (env(OURA_REDIRECT_URI="ftp://example.org/cb"), "OURA_REDIRECT_URI"),
        (env(OURA_REDIRECT_URI="/callback"), "OURA_REDIRECT_URI"),
        (env(OURA_REDIRECT_URI="http://"), "OURA_REDIRECT_URI"),
        (env(OURA_REDIRECT_URI="http://[::1/cb"), "OURA_REDIRECT_URI"),
        (env(OURA_REDIRECT_URI="http://a b/cb"), "OURA_REDIRECT_URI"),
        (env(OURA_POLL_INTERVAL="59"), "OURA_POLL_INTERVAL"),
        (env(OURA_POLL_INTERVAL="-1"), "OURA_POLL_INTERVAL"),
        (env(OURA_POLL_INTERVAL="soon"), "OURA_POLL_INTERVAL"),
        (env(OURA_POLL_INTERVAL="90.5"), "OURA_POLL_INTERVAL"),
        (env(OURA_API_BASE_URL="ftp://example.org"), "OURA_API_BASE_URL"),
        (env(OURA_API_BASE_URL="https://example.org/?x=1"), "OURA_API_BASE_URL"),
        (env(OURA_API_BASE_URL="https://example.org/#frag"), "OURA_API_BASE_URL"),
        (env(PORT="0"), "PORT"),
        (env(PORT="65536"), "PORT"),
        (env(PORT="http"), "PORT"),
        (env(LOGLEVEL="loud"), "LOGLEVEL"),
        (env(LOGLEVEL="NOTSET"), "LOGLEVEL"),
    ],
)
def test_validation_errors_name_the_variable(environ: dict[str, str], variable: str) -> None:
    with pytest.raises(ConfigError, match=variable):
        Settings.from_env(environ)


def test_unresolvable_home_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: Path) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", fail)
    with pytest.raises(ConfigError, match="OURA_TOKEN_PATH"):
        Settings.from_env(BASE)


@pytest.mark.parametrize("level", ["DEBUG", "info", "Warning", "error", "critical"])
def test_log_level_is_case_insensitive(level: str) -> None:
    settings = Settings.from_env(env(LOGLEVEL=level))
    assert settings.log_level == getattr(logging, level.upper())


def test_client_secret_file_is_read_and_stripped(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("  from-file\n", encoding="utf-8")
    settings = Settings.from_env(env(OURA_CLIENT_SECRET_FILE=str(secret)))
    assert settings.client_secret == "from-file"
    assert settings.warnings == ()


def test_empty_client_secret_file_is_an_error(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"OURA_CLIENT_SECRET_FILE.*empty"):
        Settings.from_env(env(OURA_CLIENT_SECRET_FILE=str(secret)))


def test_auth_code_file_is_read_lazily(tmp_path: Path) -> None:
    code_file = tmp_path / "code"
    settings = Settings.from_env(env(OURA_AUTH_CODE_FILE=str(code_file)))
    assert settings.has_auth_code
    assert settings.auth_code is None
    code_file.write_text("  the-code\n", encoding="utf-8")
    assert settings.read_auth_code() == "the-code"


def test_missing_auth_code_file_counts_as_not_provided(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings.from_env(env(OURA_AUTH_CODE_FILE=str(tmp_path / "absent")))
    with caplog.at_level(logging.WARNING):
        assert settings.read_auth_code() is None
    assert "does not exist" in caplog.text


def test_empty_auth_code_file_counts_as_not_provided(tmp_path: Path) -> None:
    code_file = tmp_path / "code"
    code_file.write_text("\n", encoding="utf-8")
    assert Settings.from_env(env(OURA_AUTH_CODE_FILE=str(code_file))).read_auth_code() is None


def test_unreadable_auth_code_file_is_a_config_error(tmp_path: Path) -> None:
    settings = Settings.from_env(env(OURA_AUTH_CODE_FILE=str(tmp_path)))
    with pytest.raises(ConfigError, match="OURA_AUTH_CODE_FILE"):
        settings.read_auth_code()


def test_personal_access_token_is_ignored_with_a_warning() -> None:
    settings = Settings.from_env(env(OURA_CLIENT_SECRET="s", OURA_ACCESS_TOKEN="PAT-VALUE-123"))
    assert len(settings.warnings) == 1
    assert "OURA_ACCESS_TOKEN" in settings.warnings[0]
    assert "removed" in settings.warnings[0]
    assert "PAT-VALUE-123" not in settings.warnings[0]


def test_parse_port_and_listen_address_work_standalone() -> None:
    assert parse_port({}) == 8000
    assert parse_port({"PORT": " 9100 "}) == 9100
    assert parse_listen_address({}) == "0.0.0.0"  # noqa: S104
    assert parse_listen_address({"LISTEN_ADDRESS": " ::1 "}) == "::1"
    with pytest.raises(ConfigError, match="PORT"):
        parse_port({"PORT": "x"})


@pytest.mark.parametrize(
    "value", ["UTC", "Europe/Berlin", ":Europe/Berlin", "Asia/Tokyo", "  America/New_York \n"]
)
def test_known_time_zone_names_do_not_warn(value: str) -> None:
    assert Settings.from_env({**WITH_SECRET, "TZ": value}).warnings == ()


@pytest.mark.parametrize(
    "value",
    ["UTC0", "CET-1CEST,M3.5.0,M10.5.0/3", "EST5EDT", "<+03>-3", ":UTC0", "JST-9", "Etc/GMT+1"],
)
def test_posix_time_zone_strings_do_not_warn(value: str) -> None:
    assert Settings.from_env({**WITH_SECRET, "TZ": value}).warnings == ()


@pytest.mark.parametrize(
    "value",
    ["Mars/Phobos", "Berlin", ":Nope/Zone", "Europe", "../etc/passwd", "/etc/localtime", ":"],
)
def test_unknown_time_zones_warn(value: str) -> None:
    settings = Settings.from_env({**WITH_SECRET, "TZ": value})
    assert settings.warnings == (time_zone_warning(value),)


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_time_zone_counts_as_unset(value: str) -> None:
    assert Settings.from_env({**WITH_SECRET, "TZ": value}).warnings == ()


def test_unknown_time_zone_is_reported_as_written_without_whitespace() -> None:
    settings = Settings.from_env({**WITH_SECRET, "TZ": "  :Nowhere/Land \n"})
    assert settings.warnings == (time_zone_warning(":Nowhere/Land"),)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:9", "HTTP://example.org", "http://mock:8080/base/"]
)
def test_plain_http_api_base_url_warns(url: str) -> None:
    settings = Settings.from_env({**WITH_SECRET, "OURA_API_BASE_URL": url})
    assert settings.warnings == (HTTP_WARNING,)


@pytest.mark.parametrize("url", ["https://api.ouraring.com", "https://oura.test/", "HTTPS://x.org"])
def test_https_api_base_url_does_not_warn(url: str) -> None:
    assert Settings.from_env({**WITH_SECRET, "OURA_API_BASE_URL": url}).warnings == ()


def test_warnings_accumulate_without_duplicates() -> None:
    settings = Settings.from_env(
        env(OURA_ACCESS_TOKEN="pat", TZ="Nowhere", OURA_API_BASE_URL="http://127.0.0.1:9")
    )
    assert len(settings.warnings) == 4
    assert len(set(settings.warnings)) == 4
    assert HTTP_WARNING in settings.warnings
    assert time_zone_warning("Nowhere") in settings.warnings
    assert any("OURA_CLIENT_SECRET" in warning for warning in settings.warnings)
    assert any("OURA_ACCESS_TOKEN" in warning for warning in settings.warnings)


def test_the_remote_write_password_can_come_from_a_file(tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text("from-file\n", encoding="utf-8")
    settings = Settings.from_env(
        env(OURA_REMOTE_WRITE_USERNAME="writer", OURA_REMOTE_WRITE_PASSWORD_FILE=str(secret))
    )
    assert settings.remote_write_username == "writer"
    assert settings.remote_write_password == "from-file"


def test_the_remote_write_url_is_optional_for_commands_that_do_not_push() -> None:
    settings = Settings.from_env({"OURA_CLIENT_ID": "client"}, remote_write=False)
    assert settings.remote_write_url is None
    configured = Settings.from_env(BASE, remote_write=False)
    assert configured.remote_write_url == BASE["OURA_REMOTE_WRITE_URL"]
