import contextlib
import fcntl
import json
import logging
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from oura_exporter.config import ConfigError

logger = logging.getLogger(__name__)

PENDING_FILE = "pending_auth.json"
LOCK_FILE = ".lock"
PROBE_FILE = ".write-probe"
_ABSENT: object = object()


def _fsync_directory(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_json_atomic(path: Path, data: Mapping[str, object]) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        replaced = True
    finally:
        if not replaced:
            with contextlib.suppress(OSError):
                tmp.unlink()
    _fsync_directory(path.parent)


def _optional_str(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) and value else None


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Token:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    client_id: str | None = None
    scope: str | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "client_id": self.client_id,
            "scope": self.scope,
        }

    @classmethod
    def from_json(cls, raw: object) -> Token:
        if not isinstance(raw, dict):
            raise ValueError("token file is not a JSON object")
        access_token = _optional_str(raw, "access_token")
        if access_token is None:
            raise ValueError("token file has no access_token")
        expires_at: datetime | None = None
        raw_expiry = raw.get("expires_at")
        if isinstance(raw_expiry, str) and raw_expiry:
            try:
                expires_at = _parse_datetime(raw_expiry)
            except ValueError:
                logger.warning("token file has an unparsable expires_at; treating it as unknown")
        return cls(
            access_token=access_token,
            refresh_token=_optional_str(raw, "refresh_token"),
            expires_at=expires_at,
            client_id=_optional_str(raw, "client_id"),
            scope=_optional_str(raw, "scope"),
        )


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    code_verifier: str
    state: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    created_at: datetime

    def matches(self, client_id: str, redirect_uri: str, scopes: Sequence[str]) -> bool:
        return (
            self.client_id == client_id
            and self.redirect_uri == redirect_uri
            and set(self.scopes) == set(scopes)
        )

    def to_json(self) -> dict[str, object]:
        return {
            "code_verifier": self.code_verifier,
            "state": self.state,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scopes": list(self.scopes),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: object) -> PendingAuthorization:
        if not isinstance(raw, dict):
            raise ValueError("pending authorization file is not a JSON object")
        code_verifier = _optional_str(raw, "code_verifier")
        state = _optional_str(raw, "state")
        client_id = _optional_str(raw, "client_id")
        redirect_uri = _optional_str(raw, "redirect_uri")
        if code_verifier is None or state is None or client_id is None or redirect_uri is None:
            raise ValueError(
                "pending authorization file lacks code_verifier, state, client_id or redirect_uri"
            )
        scopes = raw.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
            raise ValueError("pending authorization file has invalid scopes")
        created = _optional_str(raw, "created_at")
        try:
            created_at = _parse_datetime(created) if created else datetime.now(UTC)
        except ValueError:
            created_at = datetime.now(UTC)
        return cls(
            code_verifier=code_verifier,
            state=state,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=tuple(scopes),
            created_at=created_at,
        )


class TokenStore:
    def __init__(self, token_path: Path) -> None:
        self.token_path = token_path
        self.data_dir = token_path.parent
        self.pending_path = self.data_dir / PENDING_FILE
        self._lock_fd: int | None = None

    def prepare(self) -> None:
        try:
            is_directory = self.token_path.is_dir()
        except OSError:
            is_directory = False
        if is_directory:
            raise ConfigError(
                f"{self.token_path} is a directory. Docker creates a directory when a bind mount's "
                "source file does not exist; mount a directory (the whole data dir) instead of a "
                "single file."
            )
        try:
            self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            probe = self.data_dir / PROBE_FILE
            write_json_atomic(probe, {})
            probe.unlink()
        except OSError as exc:
            raise ConfigError(
                f"token directory {self.data_dir} is not writable for uid {os.getuid()} ({exc}); "
                "the directory must be writable by that user"
            ) from exc

    def acquire_lock(self) -> None:
        lock_path = self.data_dir / LOCK_FILE
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            logger.warning("cannot open %s (%s); running without an instance lock", lock_path, exc)
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ConfigError(
                f"another oura-exporter instance uses this token directory ({self.data_dir}); "
                "running two would invalidate each other's single-use refresh tokens"
            ) from None
        except OSError as exc:
            os.close(fd)
            logger.warning("cannot lock %s (%s); running without an instance lock", lock_path, exc)
            return
        self._lock_fd = fd

    def release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _read_json(self, path: Path, what: str) -> object:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _ABSENT
        except OSError as exc:
            raise ConfigError(f"cannot read {what} {path}: {exc}") from exc
        except UnicodeDecodeError:
            logger.warning("ignoring %s %s: not valid UTF-8", what, path)
            return _ABSENT
        try:
            parsed: object = json.loads(text)
        except ValueError as exc:
            logger.warning("ignoring corrupt %s %s: %s", what, path, exc)
            return _ABSENT
        return parsed

    def load_token(self) -> Token | None:
        raw = self._read_json(self.token_path, "token file")
        if raw is _ABSENT:
            return None
        try:
            return Token.from_json(raw)
        except ValueError as exc:
            logger.warning("ignoring token file %s: %s", self.token_path, exc)
            return None

    def save_token(self, token: Token) -> None:
        write_json_atomic(self.token_path, token.to_json())

    def load_pending(self) -> PendingAuthorization | None:
        raw = self._read_json(self.pending_path, "pending authorization file")
        if raw is _ABSENT:
            return None
        try:
            return PendingAuthorization.from_json(raw)
        except ValueError as exc:
            logger.warning("ignoring pending authorization file %s: %s", self.pending_path, exc)
            return None

    def save_pending(self, pending: PendingAuthorization) -> None:
        write_json_atomic(self.pending_path, pending.to_json())

    def clear_pending(self) -> None:
        try:
            self.pending_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("cannot remove %s: %s", self.pending_path, exc)
