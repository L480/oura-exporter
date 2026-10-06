import logging
from pathlib import Path

import pytest

from oura_exporter.config import (
    ConfigError,
    Settings,
    parse_listen_address,
    parse_port,
)

BASE = {"OURA_CLIENT_ID": "client"}


def env(**extra: str) -> dict[str, str]:
    return {**BASE, **extra}


def test_defaults() -> None:
    settings = Settings.from_env(BASE)
    assert settings.client_id == "client"
    assert settings.client_secret is None
    assert settings.redirect_uri == "http://localhost:8000/callback"
    assert settings.scopes == ("personal", "daily", "heartrate", "spo2", "stress")
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
            "OURA_REDIRECT_URI": "   ",
            "PORT": "",
            "OURA_POLL_INTERVAL": " ",
            "LOGLEVEL": "",
            "OURA_SCOPES": "",
            "OURA_AUTH_CODE": "  ",
        }
    )
    assert settings.client_id == "client"
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
    assert settings.warnings == ()


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({}, "OURA_CLIENT_ID"),
        ({"OURA_CLIENT_ID": "  "}, "OURA_CLIENT_ID"),
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
