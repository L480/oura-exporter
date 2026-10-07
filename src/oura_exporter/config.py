import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_REDIRECT_URI = "http://localhost:8000/callback"
DEFAULT_SCOPES = (
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
DEFAULT_TOKEN_PATH = "~/.config/oura-exporter/oauth_token.json"  # noqa: S105
DEFAULT_API_BASE_URL = "https://api.ouraring.com"
DEFAULT_POLL_INTERVAL = 300
DEFAULT_LOOKBACK_DAYS = 3
MIN_POLL_INTERVAL = 60
DEFAULT_PORT = 8000
DEFAULT_LISTEN_ADDRESS = "0.0.0.0"  # noqa: S104


class ConfigError(Exception):
    """Invalid or unusable configuration."""


def _get(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None:
        return None
    return value.strip() or None


def _read_file(variable: str, path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{variable}: cannot read {path}: {exc}") from exc


def _http_url(name: str, value: str) -> str:
    try:
        parts = urlsplit(value)
        valid = parts.scheme in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid or any(char.isspace() for char in value):
        raise ConfigError(f"{name} must be an absolute http(s) URL, got {value!r}")
    return value


def _integer(name: str, value: str, minimum: int, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None
    if number < minimum or (maximum is not None and number > maximum):
        bound = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ConfigError(f"{name} must be {bound}, got {number}")
    return number


def _log_level(value: str) -> int:
    levels = {name: level for name, level in logging.getLevelNamesMapping().items() if level}
    try:
        return levels[value.upper()]
    except KeyError:
        raise ConfigError(f"LOGLEVEL must be one of {', '.join(levels)}, got {value!r}") from None


def _time_zone_warning(value: str) -> str | None:
    name = value.removeprefix(":")
    if any(char.isdigit() for char in name):
        return None
    with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
        ZoneInfo(name)
        return None
    return (
        f"TZ={value} is not a known time zone; the C library falls back to UTC, "
        'so "today" and the day timestamps use UTC'
    )


def parse_port(environ: Mapping[str, str]) -> int:
    value = _get(environ, "PORT")
    return DEFAULT_PORT if value is None else _integer("PORT", value, 1, 65535)


def parse_listen_address(environ: Mapping[str, str]) -> str:
    return _get(environ, "LISTEN_ADDRESS") or DEFAULT_LISTEN_ADDRESS


@dataclass(frozen=True, slots=True)
class Settings:
    client_id: str
    client_secret: str | None
    redirect_uri: str
    scopes: tuple[str, ...]
    token_path: Path
    auth_code: str | None
    auth_code_file: Path | None
    poll_interval: int
    lookback_days: int
    remote_write_url: str | None
    remote_write_username: str | None
    remote_write_password: str | None
    metrics_config: Path | None
    api_base_url: str
    port: int
    listen_address: str
    log_level: int
    warnings: tuple[str, ...] = ()

    @property
    def token_url(self) -> str:
        return f"{self.api_base_url}/oauth/token"

    @property
    def has_auth_code(self) -> bool:
        return self.auth_code is not None or self.auth_code_file is not None

    def read_auth_code(self) -> str | None:
        if self.auth_code is not None:
            return self.auth_code
        if self.auth_code_file is None:
            return None
        try:
            text = self.auth_code_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.warning(
                "OURA_AUTH_CODE_FILE %s does not exist; treating the code as not provided",
                self.auth_code_file,
            )
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"OURA_AUTH_CODE_FILE: cannot read {self.auth_code_file}: {exc}"
            ) from exc
        return text or None

    @classmethod
    def from_env(cls, environ: Mapping[str, str], *, remote_write: bool = True) -> Settings:
        warnings: list[str] = []

        client_id = _get(environ, "OURA_CLIENT_ID")
        if client_id is None:
            raise ConfigError("OURA_CLIENT_ID is required")

        secret = _get(environ, "OURA_CLIENT_SECRET")
        secret_file = _get(environ, "OURA_CLIENT_SECRET_FILE")
        if secret is not None and secret_file is not None:
            raise ConfigError("set only one of OURA_CLIENT_SECRET and OURA_CLIENT_SECRET_FILE")
        if secret_file is not None:
            secret = _read_file("OURA_CLIENT_SECRET_FILE", Path(secret_file))
            if not secret:
                raise ConfigError(f"OURA_CLIENT_SECRET_FILE: {secret_file} is empty")
        if secret is None:
            warnings.append(
                "OURA_CLIENT_SECRET(_FILE) is not set; Oura's token endpoint normally requires it"
            )

        redirect_uri = _http_url(
            "OURA_REDIRECT_URI", _get(environ, "OURA_REDIRECT_URI") or DEFAULT_REDIRECT_URI
        )

        raw_scopes = _get(environ, "OURA_SCOPES")
        scopes = tuple(dict.fromkeys(raw_scopes.split())) if raw_scopes else DEFAULT_SCOPES

        token_path_value = _get(environ, "OURA_TOKEN_PATH") or DEFAULT_TOKEN_PATH
        try:
            token_path = Path(token_path_value).expanduser().absolute()
        except RuntimeError as exc:
            raise ConfigError(f"OURA_TOKEN_PATH: {exc}") from exc

        auth_code = _get(environ, "OURA_AUTH_CODE")
        auth_code_file = _get(environ, "OURA_AUTH_CODE_FILE")
        if auth_code is not None and auth_code_file is not None:
            raise ConfigError("set only one of OURA_AUTH_CODE and OURA_AUTH_CODE_FILE")

        poll_value = _get(environ, "OURA_POLL_INTERVAL")
        poll_interval = (
            DEFAULT_POLL_INTERVAL
            if poll_value is None
            else _integer("OURA_POLL_INTERVAL", poll_value, MIN_POLL_INTERVAL)
        )

        lookback_value = _get(environ, "OURA_LOOKBACK_DAYS")
        lookback_days = (
            DEFAULT_LOOKBACK_DAYS
            if lookback_value is None
            else _integer("OURA_LOOKBACK_DAYS", lookback_value, 1)
        )

        remote_write_url = _get(environ, "OURA_REMOTE_WRITE_URL")
        if remote_write_url is None:
            if remote_write:
                raise ConfigError("OURA_REMOTE_WRITE_URL is required")
        else:
            remote_write_url = _http_url("OURA_REMOTE_WRITE_URL", remote_write_url)
        username = _get(environ, "OURA_REMOTE_WRITE_USERNAME")
        password = _get(environ, "OURA_REMOTE_WRITE_PASSWORD")
        password_file = _get(environ, "OURA_REMOTE_WRITE_PASSWORD_FILE")
        if password is not None and password_file is not None:
            raise ConfigError(
                "set only one of OURA_REMOTE_WRITE_PASSWORD and OURA_REMOTE_WRITE_PASSWORD_FILE"
            )
        if password_file is not None:
            password = _read_file("OURA_REMOTE_WRITE_PASSWORD_FILE", Path(password_file))
        if (password is not None) != (username is not None):
            raise ConfigError(
                "OURA_REMOTE_WRITE_USERNAME and OURA_REMOTE_WRITE_PASSWORD(_FILE) go together"
            )

        metrics_config = _get(environ, "OURA_METRICS_CONFIG")

        api_base_url = _http_url(
            "OURA_API_BASE_URL", _get(environ, "OURA_API_BASE_URL") or DEFAULT_API_BASE_URL
        )
        base_parts = urlsplit(api_base_url)
        if base_parts.query or base_parts.fragment:
            raise ConfigError(
                f"OURA_API_BASE_URL must not contain a query or fragment, got {api_base_url!r}"
            )

        if base_parts.scheme == "http":
            warnings.append(
                "OURA_API_BASE_URL uses plain http; access tokens are sent unencrypted "
                "(only use this for a local mock server)"
            )

        log_value = _get(environ, "LOGLEVEL")
        log_level = logging.INFO if log_value is None else _log_level(log_value)

        time_zone = _get(environ, "TZ")
        if time_zone is not None and (message := _time_zone_warning(time_zone)) is not None:
            warnings.append(message)

        return cls(
            client_id=client_id,
            client_secret=secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            token_path=token_path,
            auth_code=auth_code,
            auth_code_file=Path(auth_code_file) if auth_code_file is not None else None,
            poll_interval=poll_interval,
            lookback_days=lookback_days,
            remote_write_url=remote_write_url,
            remote_write_username=username,
            remote_write_password=password,
            metrics_config=Path(metrics_config) if metrics_config is not None else None,
            api_base_url=api_base_url.rstrip("/"),
            port=parse_port(environ),
            listen_address=parse_listen_address(environ),
            log_level=log_level,
            warnings=tuple(warnings),
        )
