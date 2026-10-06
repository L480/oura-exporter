import errno
import fcntl
import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oura_exporter import storage
from oura_exporter.config import ConfigError
from oura_exporter.storage import PendingAuthorization, Token, TokenStore, write_json_atomic


def names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def make_store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "data" / "oauth_token.json")


def pending(**overrides: Any) -> PendingAuthorization:
    values: dict[str, Any] = {
        "code_verifier": "v" * 64,
        "state": "state-1",
        "client_id": "client",
        "redirect_uri": "http://localhost:8000/callback",
        "scopes": ("personal", "daily"),
        "created_at": datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return PendingAuthorization(**values)


class TestWriteJsonAtomic:
    def test_writes_valid_json_with_private_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "file.json"
        write_json_atomic(target, {"a": 1, "b": [1, 2], "c": None})
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2], "c": None}
        assert mode(target) == 0o600
        assert names(tmp_path) == ["file.json"]

    def test_replacing_resets_a_permissive_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "file.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o644)
        write_json_atomic(target, {"new": True})
        assert mode(target) == 0o600
        assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}

    def test_target_is_untouched_when_serialization_fails(self, tmp_path: Path) -> None:
        target = tmp_path / "file.json"
        write_json_atomic(target, {"old": 1})
        with pytest.raises(TypeError):
            write_json_atomic(target, {"bad": object()})
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": 1}
        assert names(tmp_path) == ["file.json"]

    def test_target_is_untouched_when_fsync_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "file.json"
        write_json_atomic(target, {"old": 1})

        def failing_fsync(fd: int) -> None:
            raise OSError(errno.EIO, "disk error")

        monkeypatch.setattr(storage.os, "fsync", failing_fsync)
        with pytest.raises(OSError, match="disk error"):
            write_json_atomic(target, {"new": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": 1}
        assert names(tmp_path) == ["file.json"]

    def test_temp_file_is_removed_when_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "file.json"
        write_json_atomic(target, {"old": 1})

        def failing_replace(self: Path, other: Path) -> Path:
            raise OSError(errno.EXDEV, "cross-device")

        monkeypatch.setattr(Path, "replace", failing_replace)
        with pytest.raises(OSError, match="cross-device"):
            write_json_atomic(target, {"new": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": 1}
        assert names(tmp_path) == ["file.json"]

    def test_directory_fsync_is_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_fsync = os.fsync
        calls: list[int] = []

        def flaky_fsync(fd: int) -> None:
            calls.append(fd)
            if len(calls) > 1:
                raise OSError(errno.EINVAL, "directories cannot be synced here")
            real_fsync(fd)

        monkeypatch.setattr(storage.os, "fsync", flaky_fsync)
        target = tmp_path / "file.json"
        write_json_atomic(target, {"ok": True})
        assert len(calls) == 2
        assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}

    def test_missing_directory_fails_without_leftovers(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            write_json_atomic(tmp_path / "missing" / "file.json", {})
        assert names(tmp_path) == []


class TestPrepare:
    def test_creates_the_directory_privately_and_leaves_nothing_behind(
        self, tmp_path: Path
    ) -> None:
        store = TokenStore(tmp_path / "a" / "b" / "oauth_token.json")
        store.prepare()
        assert store.data_dir.is_dir()
        assert mode(store.data_dir) == 0o700
        assert names(store.data_dir) == []

    def test_is_idempotent_and_keeps_existing_files(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.save_token(Token("a"))
        store.prepare()
        assert store.load_token() == Token("a")

    def test_directory_instead_of_file_explains_the_docker_pitfall(self, tmp_path: Path) -> None:
        token_path = tmp_path / "oauth_token.json"
        token_path.mkdir()
        with pytest.raises(ConfigError) as caught:
            TokenStore(token_path).prepare()
        message = str(caught.value)
        assert str(token_path) in message
        assert "is a directory" in message
        assert "bind mount" in message
        assert "mount a directory" in message

    def test_unwritable_directory_names_directory_and_uid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def denied(path: Path, data: Any) -> None:
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(storage, "write_json_atomic", denied)
        store = make_store(tmp_path)
        with pytest.raises(ConfigError) as caught:
            store.prepare()
        assert str(store.data_dir) in str(caught.value)
        assert f"uid {os.getuid()}" in str(caught.value)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
    def test_read_only_directory(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        data.mkdir()
        data.chmod(0o500)
        try:
            with pytest.raises(ConfigError, match="not writable"):
                TokenStore(data / "oauth_token.json").prepare()
        finally:
            data.chmod(0o700)

    def test_stat_errors_on_the_token_path_are_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def denied(self: Path) -> bool:
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(Path, "is_dir", denied)
        store = make_store(tmp_path)
        store.prepare()
        assert store.data_dir.exists()

    def test_parent_that_is_a_file(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(ConfigError, match="not writable"):
            TokenStore(blocker / "sub" / "oauth_token.json").prepare()


class TestLock:
    def test_second_instance_is_refused(self, tmp_path: Path) -> None:
        first = make_store(tmp_path)
        first.prepare()
        first.acquire_lock()
        second = make_store(tmp_path)
        try:
            with pytest.raises(ConfigError) as caught:
                second.acquire_lock()
        finally:
            first.release_lock()
        assert "another oura-exporter instance" in str(caught.value)
        assert "single-use refresh tokens" in str(caught.value)

    def test_lock_can_be_taken_again_after_release(self, tmp_path: Path) -> None:
        first = make_store(tmp_path)
        first.prepare()
        first.acquire_lock()
        first.release_lock()
        first.release_lock()
        second = make_store(tmp_path)
        second.acquire_lock()
        second.release_lock()

    def test_unsupported_filesystem_only_warns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def unsupported(fd: int, operation: int) -> None:
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(fcntl, "flock", unsupported)
        store = make_store(tmp_path)
        store.prepare()
        with caplog.at_level(logging.WARNING):
            store.acquire_lock()
        assert "without an instance lock" in caplog.text

    def test_unopenable_lock_file_only_warns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        real_open = os.open

        def selective_open(path: Any, flags: int, mode: int = 0o777) -> int:
            if str(path).endswith(storage.LOCK_FILE):
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, flags, mode)

        store = make_store(tmp_path)
        store.prepare()
        monkeypatch.setattr(storage.os, "open", selective_open)
        with caplog.at_level(logging.WARNING):
            store.acquire_lock()
        assert "without an instance lock" in caplog.text


class TestTokenFile:
    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert make_store(tmp_path).load_token() is None

    def test_round_trip_and_private_mode(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.prepare()
        token = Token(
            access_token="access",
            refresh_token="refresh",
            expires_at=datetime(2026, 10, 7, 8, 9, 10, tzinfo=UTC),
            client_id="client",
            scope="personal daily",
        )
        store.save_token(token)
        assert store.load_token() == token
        assert mode(store.token_path) == 0o600
        assert set(json.loads(store.token_path.read_text(encoding="utf-8"))) == {
            "access_token",
            "refresh_token",
            "expires_at",
            "client_id",
            "scope",
        }

    def test_corrupt_json_is_ignored_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.token_path.write_text("{not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert store.load_token() is None
        assert "corrupt" in caplog.text
        assert store.token_path.read_text(encoding="utf-8") == "{not json"

    def test_invalid_utf8_is_ignored_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.token_path.write_bytes(b"\xff\xfe\x00")
        with caplog.at_level(logging.WARNING):
            assert store.load_token() is None
        assert "not valid UTF-8" in caplog.text

    @pytest.mark.parametrize(
        "content",
        ["[]", '"text"', "{}", '{"access_token": ""}', '{"access_token": 5}', "null"],
    )
    def test_wrong_shape_is_ignored_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
    ) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.token_path.write_text(content, encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert store.load_token() is None
        assert "ignoring token file" in caplog.text

    def test_unreadable_file_is_a_config_error(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.token_path.mkdir(parents=True)
        with pytest.raises(ConfigError, match="cannot read token file"):
            store.load_token()

    def test_permission_denied_is_a_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.token_path.write_text("{}", encoding="utf-8")

        def denied(self: Path, *args: Any, **kwargs: Any) -> str:
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(Path, "read_text", denied)
        with pytest.raises(ConfigError, match="Permission denied"):
            store.load_token()

    def test_loads_a_file_written_by_the_old_version(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.token_path.write_text(
            json.dumps(
                {
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "expires_at": "2026-10-06T10:00:00.123456+00:00",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        token = store.load_token()
        assert token == Token(
            access_token="old-access",
            refresh_token="old-refresh",
            expires_at=datetime(2026, 10, 6, 10, 0, 0, 123456, tzinfo=UTC),
            client_id=None,
            scope=None,
        )

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-10-06T10:00:00", datetime(2026, 10, 6, 10, tzinfo=UTC)),
            ("2026-10-06T12:00:00+02:00", datetime(2026, 10, 6, 10, tzinfo=UTC)),
            (None, None),
            ("", None),
            (12345, None),
        ],
    )
    def test_expiry_forms(self, tmp_path: Path, raw: Any, expected: datetime | None) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.token_path.write_text(
            json.dumps({"access_token": "a", "expires_at": raw}), encoding="utf-8"
        )
        token = store.load_token()
        assert token is not None
        assert token.expires_at == expected

    def test_unparsable_expiry_is_treated_as_unknown(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.token_path.write_text(
            json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": "tomorrow"}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            token = store.load_token()
        assert token == Token("a", "r", None)
        assert "unparsable expires_at" in caplog.text

    def test_optional_fields_of_the_wrong_type_are_dropped(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.token_path.write_text(
            json.dumps(
                {
                    "access_token": "a",
                    "refresh_token": 5,
                    "client_id": "",
                    "scope": ["x"],
                    "extra": 1,
                }
            ),
            encoding="utf-8",
        )
        assert store.load_token() == Token("a")


class TestPendingFile:
    def test_round_trip_and_private_mode(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.prepare()
        original = pending()
        store.save_pending(original)
        assert store.pending_path == store.data_dir / "pending_auth.json"
        assert store.load_pending() == original
        assert mode(store.pending_path) == 0o600

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert make_store(tmp_path).load_pending() is None

    def test_old_format_without_state_and_client_id_counts_as_absent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.pending_path.write_text(
            json.dumps(
                {
                    "code_verifier": "x" * 43,
                    "redirect_uri": "http://localhost:8000/callback",
                    "scopes": ["email", "personal"],
                    "created_at": "2026-10-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            assert store.load_pending() is None
        assert "state" in caplog.text
        assert "client_id" in caplog.text

    @pytest.mark.parametrize(
        "content",
        [
            "{broken",
            "[]",
            '{"code_verifier": "v", "state": "s", "client_id": "c", "redirect_uri": "r"}',
        ],
    )
    def test_corrupt_or_incomplete_files_count_as_absent(
        self, tmp_path: Path, content: str
    ) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.pending_path.write_text(content, encoding="utf-8")
        assert store.load_pending() is None

    def test_missing_or_garbled_created_at_is_tolerated(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.prepare()
        for created in (None, "yesterday"):
            body = pending().to_json()
            body["created_at"] = created
            store.pending_path.write_text(json.dumps(body), encoding="utf-8")
            loaded = store.load_pending()
            assert loaded is not None
            assert loaded.state == "state-1"

    def test_matches_compares_client_redirect_and_scope_set(self) -> None:
        current = pending()
        assert current.matches("client", "http://localhost:8000/callback", ["daily", "personal"])
        assert not current.matches("other", "http://localhost:8000/callback", ["personal", "daily"])
        assert not current.matches("client", "http://localhost:9/callback", ["personal", "daily"])
        assert not current.matches("client", "http://localhost:8000/callback", ["personal"])
        assert not current.matches(
            "client", "http://localhost:8000/callback", ["personal", "daily", "spo2"]
        )

    def test_clear_is_idempotent(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.prepare()
        store.save_pending(pending())
        store.clear_pending()
        store.clear_pending()
        assert not store.pending_path.exists()

    def test_clear_failure_only_warns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store = make_store(tmp_path)
        store.prepare()

        def failing_unlink(self: Path, missing_ok: bool = False) -> None:
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        with caplog.at_level(logging.WARNING):
            store.clear_pending()
        assert "cannot remove" in caplog.text
