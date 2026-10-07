import base64
import builtins
import errno
import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses

from oura_exporter.api import build_session
from oura_exporter.auth import (
    AUTHORIZE_URL,
    AuthError,
    ConsentRequired,
    OAuthClient,
    TokenManager,
    authenticate,
    parse_authorization_response,
)
from oura_exporter.config import ConfigError, Settings
from oura_exporter.storage import Token, TokenStore

from .helpers import TOKEN_URL, FakeClock

CLIENT_ID = "client-id"
SECRET = "client-secret"
REDIRECT = "http://localhost:8000/callback"
SCOPES = ("personal", "daily", "heartrate", "spo2", "stress")
NOW = 1_800_000_000.0


@dataclass
class Env:
    store: TokenStore
    oauth: OAuthClient
    manager: TokenManager
    clock: FakeClock
    rsps: responses.RequestsMock

    def settings(self, **extra: str) -> Settings:
        return Settings.from_env(
            {
                "OURA_CLIENT_ID": CLIENT_ID,
                "OURA_CLIENT_SECRET": SECRET,
                "OURA_REMOTE_WRITE_URL": "http://prometheus.test/api/v1/write",
                "OURA_REDIRECT_URI": REDIRECT,
                "OURA_TOKEN_PATH": str(self.store.token_path),
                "OURA_SCOPES": " ".join(SCOPES),
                **extra,
            }
        )

    def token_calls(self) -> list[dict[str, str]]:
        return [form(call) for call in self.rsps.calls if call.request.url == TOKEN_URL]

    def fresh_manager(self) -> TokenManager:
        return TokenManager(self.store, self.oauth, self.clock)


@pytest.fixture
def env(tmp_path: Any, rsps: responses.RequestsMock) -> Iterator[Env]:
    session = build_session(retries=False)
    store = TokenStore(tmp_path / "data" / "oauth_token.json")
    store.prepare()
    oauth = OAuthClient(session, CLIENT_ID, SECRET, REDIRECT, SCOPES, TOKEN_URL)
    clock = FakeClock(NOW)
    yield Env(store, oauth, TokenManager(store, oauth, clock), clock, rsps)
    session.close()


def form(call: responses.Call) -> dict[str, str]:
    body = call.request.body
    assert isinstance(body, str)
    return {key: values[0] for key, values in parse_qs(body).items()}


def token_response(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "access_token": "access-2",
        "refresh_token": "refresh-2",
        "expires_in": 86400,
        "token_type": "bearer",
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not None}


def token_endpoint(rsps: responses.RequestsMock, **kwargs: Any) -> None:
    rsps.post(TOKEN_URL, **kwargs)


def stored_token(env: Env, expires_in: float | None = 3600.0, **overrides: Any) -> Token:
    values: dict[str, Any] = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": (
            None if expires_in is None else datetime.fromtimestamp(NOW + expires_in, tz=UTC)
        ),
        "client_id": CLIENT_ID,
        "scope": "personal daily",
    }
    values.update(overrides)
    token = Token(**values)
    env.store.save_token(token)
    return token


def read_token_file(env: Env) -> dict[str, Any]:
    return json.loads(env.store.token_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def run_inputs(
    monkeypatch: pytest.MonkeyPatch, answers: list[str | type[BaseException]]
) -> list[str]:
    prompts: list[str] = []
    queue = iter(answers)

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        answer = next(queue)
        if isinstance(answer, type):
            raise answer
        return answer

    monkeypatch.setattr(builtins, "input", fake_input)
    return prompts


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class TestOAuthClient:
    def test_pending_authorization_values(self, env: Env) -> None:
        first = env.oauth.new_pending()
        second = env.oauth.new_pending()
        assert 43 <= len(first.code_verifier) <= 128
        assert set(first.code_verifier) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
        )
        assert len(first.state) >= 32
        assert first.code_verifier != second.code_verifier
        assert first.state != second.state
        assert first.client_id == CLIENT_ID
        assert first.redirect_uri == REDIRECT
        assert first.scopes == SCOPES

    def test_authorize_url(self, env: Env) -> None:
        pending = env.oauth.new_pending()
        url = env.oauth.authorize_url(pending)
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZE_URL
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert query == {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "scope": "personal daily heartrate spo2 stress",
            "state": pending.state,
            "code_challenge": challenge_for(pending.code_verifier),
            "code_challenge_method": "S256",
        }
        assert env.oauth.authorize_url(pending) == url

    def test_code_exchange_request(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        result = env.oauth.exchange_code("the-code", "the-verifier")
        assert result["access_token"] == "access-2"
        call = env.rsps.calls[0]
        assert form(call) == {
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": "the-code",
            "redirect_uri": REDIRECT,
            "code_verifier": "the-verifier",
        }
        expected = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
        assert call.request.headers["Authorization"] == f"Basic {expected}"

    def test_refresh_request(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        env.oauth.refresh("refresh-1")
        call = env.rsps.calls[0]
        assert form(call) == {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
        }
        assert call.request.headers["Authorization"].startswith("Basic ")

    def test_no_basic_auth_without_a_secret(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        session = requests.Session()
        try:
            public = OAuthClient(session, CLIENT_ID, None, REDIRECT, SCOPES, TOKEN_URL)
            public.refresh("refresh-1")
        finally:
            session.close()
        call = env.rsps.calls[0]
        assert "Authorization" not in call.request.headers
        assert form(call)["client_id"] == CLIENT_ID

    @pytest.mark.parametrize(
        ("status", "body", "expect_in_message"),
        [
            (
                400,
                {"error": "invalid_grant", "error_description": "Refresh token expired"},
                "invalid_grant: Refresh token expired",
            ),
            (401, {"error": "invalid_client"}, "invalid_client"),
            (400, "not json", "HTTP 400"),
            (401, {"unexpected": 1}, "HTTP 401"),
        ],
    )
    def test_rejections_are_permanent(
        self, env: Env, status: int, body: Any, expect_in_message: str
    ) -> None:
        if isinstance(body, dict):
            token_endpoint(env.rsps, status=status, json=body)
        else:
            token_endpoint(env.rsps, status=status, body=body)
        with pytest.raises(AuthError) as caught:
            env.oauth.refresh("refresh-1")
        assert caught.value.permanent is True
        assert expect_in_message in str(caught.value)

    @pytest.mark.parametrize("status", [403, 404, 429, 500, 502, 503])
    def test_other_http_errors_are_transient(self, env: Env, status: int) -> None:
        token_endpoint(env.rsps, status=status, json={"error": "whatever"})
        with pytest.raises(AuthError) as caught:
            env.oauth.exchange_code("c", "v")
        assert caught.value.permanent is False
        assert str(status) in str(caught.value)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"body": "<html>gateway</html>"},
            {"json": ["not", "an", "object"]},
            {"json": {"refresh_token": "r"}},
            {"json": {"access_token": ""}},
            {"json": {"access_token": 5}},
        ],
    )
    def test_unusable_success_responses_are_transient(
        self, env: Env, kwargs: dict[str, Any]
    ) -> None:
        token_endpoint(env.rsps, status=200, **kwargs)
        with pytest.raises(AuthError) as caught:
            env.oauth.refresh("refresh-1")
        assert caught.value.permanent is False

    @pytest.mark.parametrize(
        "failure",
        [requests.ConnectionError("refused"), requests.Timeout("timed out")],
    )
    def test_network_errors_are_transient(self, env: Env, failure: Exception) -> None:
        token_endpoint(env.rsps, body=failure)
        with pytest.raises(AuthError) as caught:
            env.oauth.refresh("refresh-1")
        assert caught.value.permanent is False
        assert "unreachable" in str(caught.value)
        assert len(env.rsps.calls) == 1

    def test_secrets_never_appear_in_error_messages(self, env: Env) -> None:
        token_endpoint(env.rsps, status=400, json={"error": "invalid_grant"})
        with pytest.raises(AuthError) as caught:
            env.oauth.refresh("super-secret-refresh-token")
        assert "super-secret-refresh-token" not in str(caught.value)
        assert SECRET not in str(caught.value)


class TestTokenManager:
    def test_valid_token_needs_no_network(self, env: Env) -> None:
        env.manager.token = stored_token(env, expires_in=3600)
        assert env.manager.access_token() == "access-1"
        assert len(env.rsps.calls) == 0

    def test_no_token_is_a_permanent_error(self, env: Env) -> None:
        with pytest.raises(AuthError) as caught:
            env.manager.access_token()
        assert caught.value.permanent is True

    @pytest.mark.parametrize(("expires_in", "refreshes"), [(59, True), (60, True), (61, False)])
    def test_refresh_margin_is_sixty_seconds(
        self, env: Env, expires_in: float, refreshes: bool
    ) -> None:
        token_endpoint(env.rsps, json=token_response())
        env.manager.token = stored_token(env, expires_in=expires_in)
        expected = "access-2" if refreshes else "access-1"
        assert env.manager.access_token() == expected

    def test_unknown_expiry_is_treated_as_valid_and_never_refreshed_in_a_loop(
        self, env: Env
    ) -> None:
        env.manager.token = stored_token(env, expires_in=None)
        for _ in range(5):
            assert env.manager.access_token() == "access-1"
        assert len(env.rsps.calls) == 0

    def test_refresh_persists_the_rotated_refresh_token(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        env.manager.token = stored_token(env, expires_in=10)
        assert env.manager.access_token() == "access-2"
        assert env.token_calls() == [
            {"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": "refresh-1"}
        ]
        saved = read_token_file(env)
        assert saved["access_token"] == "access-2"
        assert saved["refresh_token"] == "refresh-2"
        assert saved["client_id"] == CLIENT_ID
        assert saved["scope"] == "personal daily"
        assert datetime.fromisoformat(saved["expires_at"]).timestamp() == NOW + 86400
        assert env.manager.persisted is True

    def test_refresh_response_without_refresh_token_keeps_the_old_one(
        self, env: Env, caplog: pytest.LogCaptureFixture
    ) -> None:
        token_endpoint(env.rsps, json=token_response(refresh_token=None))
        env.manager.token = stored_token(env, expires_in=10)
        with caplog.at_level(logging.WARNING):
            env.manager.refresh()
        assert env.manager.token is not None
        assert env.manager.token.refresh_token == "refresh-1"
        assert read_token_file(env)["refresh_token"] == "refresh-1"
        assert "keeping the previous one" in caplog.text

    @pytest.mark.parametrize(
        "expires_in", ["null", "0", "-5", '"3600"', "true", "NaN", "Infinity", "1e30"]
    )
    def test_refresh_response_without_usable_expiry(
        self, env: Env, caplog: pytest.LogCaptureFixture, expires_in: str
    ) -> None:
        body = (
            '{"access_token": "access-2", "refresh_token": "refresh-2", '
            f'"expires_in": {expires_in}}}'
        )
        token_endpoint(env.rsps, body=body, content_type="application/json")
        env.manager.token = stored_token(env, expires_in=10)
        with caplog.at_level(logging.WARNING):
            env.manager.refresh()
        assert env.manager.token is not None
        assert env.manager.token.expires_at is None
        assert "expiry is unknown" in caplog.text
        assert env.manager.access_token() == "access-2"
        assert len(env.rsps.calls) == 1

    def test_refreshed_scope_comes_from_the_response(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response(scope="personal daily"))
        env.manager.token = stored_token(
            env, expires_in=10, scope="personal daily heartrate spo2 stress"
        )
        refreshed = env.manager.refresh()
        assert refreshed.scope == "personal daily"
        assert read_token_file(env)["scope"] == "personal daily"

    @pytest.mark.parametrize("granted", [None, "", "   ", 42, ["personal", 1], []])
    def test_refresh_keeps_the_previous_scope_when_the_response_has_none(
        self, env: Env, granted: Any
    ) -> None:
        token_endpoint(env.rsps, json=token_response(scope=granted))
        env.manager.token = stored_token(env, expires_in=10, scope="personal daily")
        assert env.manager.refresh().scope == "personal daily"
        assert read_token_file(env)["scope"] == "personal daily"

    def test_refreshed_scope_may_be_a_list(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response(scope=["personal", "spo2"]))
        env.manager.token = stored_token(env, expires_in=10, scope="personal daily")
        assert env.manager.refresh().scope == "personal spo2"

    def test_refreshed_scope_fills_in_an_unknown_previous_scope(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response(scope="personal"))
        env.manager.token = stored_token(env, expires_in=10, scope=None)
        assert env.manager.refresh().scope == "personal"

    def test_install_sets_the_token_and_persisted(self, env: Env) -> None:
        token = Token("access-9", "refresh-9", None, CLIENT_ID)
        env.manager.install(token)
        assert env.manager.token == token
        assert env.manager.persisted is True
        env.manager.install(token, persisted=False)
        assert env.manager.persisted is False

    def test_install_clears_the_refresh_backoff(self, env: Env) -> None:
        token_endpoint(env.rsps, status=503)
        env.manager.token = stored_token(env, expires_in=10)
        with pytest.raises(AuthError):
            env.manager.refresh()
        with pytest.raises(AuthError, match="paused"):
            env.manager.refresh()

        env.rsps.replace(
            responses.POST,
            TOKEN_URL,
            json=token_response(access_token="access-3", refresh_token="refresh-3"),
        )
        env.manager.install(Token("access-9", "refresh-9", None, CLIENT_ID))
        assert env.manager.refresh().access_token == "access-3"
        assert len(env.rsps.calls) == 2

    def test_install_forgets_distrusted_tokens(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        env.manager.token = stored_token(env, expires_in=3600)
        assert env.manager.handle_unauthorized("access-1") is True
        assert env.manager.handle_unauthorized("access-2") is False

        env.rsps.replace(
            responses.POST,
            TOKEN_URL,
            json=token_response(access_token="access-3", refresh_token="refresh-3"),
        )
        env.manager.install(Token("access-2", "refresh-2", None, CLIENT_ID))
        assert env.manager.handle_unauthorized("access-2") is True
        assert env.manager.token is not None
        assert env.manager.token.access_token == "access-3"

    def test_refresh_stamps_the_current_client_on_old_format_tokens(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        env.manager.token = stored_token(env, expires_in=10, client_id=None, scope=None)
        env.manager.refresh()
        saved = read_token_file(env)
        assert saved["client_id"] == CLIENT_ID
        assert saved["scope"] is None

    def test_refresh_without_refresh_token_is_permanent(self, env: Env) -> None:
        env.manager.token = stored_token(env, expires_in=10, refresh_token=None)
        with pytest.raises(AuthError) as caught:
            env.manager.access_token()
        assert caught.value.permanent is True
        assert len(env.rsps.calls) == 0

    def test_persist_failure_keeps_working_and_retries(
        self,
        env: Env,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        token_endpoint(env.rsps, json=token_response())
        env.manager.token = stored_token(env, expires_in=10)
        real_save = env.store.save_token

        def failing_save(token: Token) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(env.store, "save_token", failing_save)
        with caplog.at_level(logging.INFO):
            assert env.manager.access_token() == "access-2"
        assert env.manager.persisted is False
        assert str(env.store.token_path) in caplog.text
        assert "still running with the in-memory token" in caplog.text
        assert "requires re-authorization" in caplog.text
        assert read_token_file(env)["access_token"] == "access-1"

        env.manager.retry_persist()
        assert env.manager.persisted is False

        monkeypatch.setattr(env.store, "save_token", real_save)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            env.manager.retry_persist()
        assert env.manager.persisted is True
        assert read_token_file(env)["refresh_token"] == "refresh-2"
        assert "saved" in caplog.text

    def test_retry_persist_is_a_noop_when_everything_is_saved(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.manager.token = stored_token(env)

        def must_not_run(token: Token) -> None:
            raise AssertionError("unexpected save")

        monkeypatch.setattr(env.store, "save_token", must_not_run)
        env.manager.retry_persist()
        TokenManager(env.store, env.oauth, env.clock).retry_persist()

    def test_transient_failure_backs_off_for_sixty_seconds(self, env: Env) -> None:
        token_endpoint(env.rsps, status=503)
        env.manager.token = stored_token(env, expires_in=10)
        with pytest.raises(AuthError) as first:
            env.manager.access_token()
        assert first.value.permanent is False
        assert len(env.rsps.calls) == 1

        env.clock.advance(59)
        with pytest.raises(AuthError) as suppressed:
            env.manager.access_token()
        assert suppressed.value.permanent is False
        assert "paused" in str(suppressed.value)
        assert len(env.rsps.calls) == 1

        env.clock.advance(2)
        env.rsps.replace(responses.POST, TOKEN_URL, json=token_response())
        assert env.manager.access_token() == "access-2"
        assert len(env.rsps.calls) == 2

    def test_permanent_failure_backs_off_for_an_hour_and_logs_once(
        self, env: Env, caplog: pytest.LogCaptureFixture
    ) -> None:
        token_endpoint(env.rsps, status=400, json={"error": "invalid_grant"})
        env.manager.token = stored_token(env, expires_in=10)
        with caplog.at_level(logging.ERROR), pytest.raises(AuthError) as first:
            env.manager.access_token()
        assert first.value.permanent is True
        errors = [record for record in caplog.records if record.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "re-authorize" in errors[0].getMessage()
        assert "OURA_AUTH_CODE" in errors[0].getMessage()

        env.clock.advance(3599)
        with pytest.raises(AuthError) as suppressed:
            env.manager.access_token()
        assert suppressed.value.permanent is True
        assert len(env.rsps.calls) == 1
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

        env.clock.advance(2)
        with pytest.raises(AuthError):
            env.manager.access_token()
        assert len(env.rsps.calls) == 2

    def test_unauthorized_with_a_replaced_token_retries_without_refreshing(self, env: Env) -> None:
        env.manager.token = Token("newer", "refresh-1", None, CLIENT_ID)
        assert env.manager.handle_unauthorized("older") is True
        assert len(env.rsps.calls) == 0

    def test_unauthorized_refreshes_once_and_distrusts_the_new_token(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        env.manager.token = stored_token(env, expires_in=3600)
        assert env.manager.handle_unauthorized("access-1") is True
        assert env.manager.token is not None
        assert env.manager.token.access_token == "access-2"
        assert len(env.rsps.calls) == 1
        assert env.manager.handle_unauthorized("access-2") is False
        assert env.manager.handle_unauthorized("access-2") is False
        assert len(env.rsps.calls) == 1

    def test_mark_working_clears_the_suspicion(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        env.manager.token = stored_token(env, expires_in=3600)
        assert env.manager.handle_unauthorized("access-1") is True
        env.manager.mark_working("access-2")
        env.rsps.replace(
            responses.POST,
            TOKEN_URL,
            json=token_response(access_token="access-3", refresh_token="refresh-3"),
        )
        assert env.manager.handle_unauthorized("access-2") is True
        assert env.manager.token is not None
        assert env.manager.token.access_token == "access-3"
        assert len(env.rsps.calls) == 2

    def test_unauthorized_propagates_refresh_failures(self, env: Env) -> None:
        token_endpoint(env.rsps, status=400, json={"error": "invalid_grant"})
        env.manager.token = stored_token(env, expires_in=3600)
        with pytest.raises(AuthError):
            env.manager.handle_unauthorized("access-1")

    def test_unauthorized_without_any_token(self, env: Env) -> None:
        with pytest.raises(AuthError) as caught:
            env.manager.handle_unauthorized("whatever")
        assert caught.value.permanent is True


class TestParseAuthorizationResponse:
    STATE = "expected-state"

    def parse(self, text: str) -> str:
        return parse_authorization_response(text, self.STATE)

    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("RAWCODE123", "RAWCODE123"),
            ("  RAWCODE123\n", "RAWCODE123"),
            (f"{REDIRECT}?code=URLCODE&state=expected-state", "URLCODE"),
            (f"{REDIRECT}?state=expected-state&scope=personal%20daily&code=URLCODE", "URLCODE"),
            ("https://example.org/cb?code=NOSTATE", "NOSTATE"),
            ("?code=QUERYONLY&state=expected-state", "QUERYONLY"),
            ("/callback?code=PATHONLY", "PATHONLY"),
            ("code=BARE&state=expected-state", "BARE"),
            ("state=expected-state&code=BARE2", "BARE2"),
        ],
    )
    def test_accepted_forms(self, text: str, code: str) -> None:
        assert self.parse(text) == code

    def test_error_parameter(self) -> None:
        with pytest.raises(AuthError, match="access_denied") as caught:
            self.parse(f"{REDIRECT}?error=access_denied&state=expected-state")
        assert caught.value.permanent is True

    def test_error_parameter_with_description(self) -> None:
        with pytest.raises(AuthError, match=r"invalid_scope \(bad scope\)"):
            self.parse("error=invalid_scope&error_description=bad%20scope")

    def test_state_mismatch(self) -> None:
        with pytest.raises(AuthError, match="different authorization attempt"):
            self.parse(f"{REDIRECT}?code=X&state=someone-elses")

    @pytest.mark.parametrize("text", ["", "   ", "two words", "has\ttab"])
    def test_empty_or_whitespace_raw_code(self, text: str) -> None:
        with pytest.raises(AuthError):
            self.parse(text)

    @pytest.mark.parametrize(
        "text",
        [
            f"{REDIRECT}?state=expected-state",
            f"{REDIRECT}?code=&state=expected-state",
            f"{REDIRECT}",
        ],
    )
    def test_url_without_a_code(self, text: str) -> None:
        with pytest.raises(AuthError, match="no usable code"):
            self.parse(text)

    def test_url_code_with_whitespace(self) -> None:
        with pytest.raises(AuthError, match="no usable code"):
            self.parse(f"{REDIRECT}?code=a%20b")


def public_url(exc: ConsentRequired) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(exc.authorize_url).query).items()}


class TestAuthenticateWithStoredToken:
    def test_valid_token_is_used_without_network(self, env: Env) -> None:
        token = stored_token(env, expires_in=3600)
        authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        assert env.manager.token == token
        assert len(env.rsps.calls) == 0
        assert env.store.load_pending() is None

    def test_old_format_token_without_client_id_is_accepted(self, env: Env) -> None:
        stored_token(env, expires_in=3600, client_id=None, scope=None)
        authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        assert env.manager.token is not None
        assert env.manager.token.access_token == "access-1"

    def test_token_of_another_client_is_ignored(
        self, env: Env, caplog: pytest.LogCaptureFixture
    ) -> None:
        stored_token(env, expires_in=3600, client_id="someone-else")
        with caplog.at_level(logging.WARNING), pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        assert "different client_id" in caplog.text
        assert env.manager.token is None
        assert len(env.rsps.calls) == 0

    def test_auth_code_is_ignored_when_a_token_exists(
        self, env: Env, caplog: pytest.LogCaptureFixture
    ) -> None:
        stored_token(env, expires_in=3600)
        with caplog.at_level(logging.INFO):
            authenticate(
                env.manager,
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="ignored-code"),
                interactive=False,
            )
        assert "ignored because a stored token exists" in caplog.text
        assert len(env.rsps.calls) == 0
        assert env.store.load_pending() is None

    def test_auth_code_file_is_not_read_when_a_token_exists(
        self, env: Env, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        stored_token(env, expires_in=3600)
        missing = tmp_path / "no-such-file"
        with caplog.at_level(logging.INFO):
            authenticate(
                env.manager,
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE_FILE=str(missing)),
                interactive=False,
            )
        assert "ignored because a stored token exists" in caplog.text
        assert "does not exist" not in caplog.text

    def test_expired_token_is_refreshed_at_startup(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        stored_token(env, expires_in=-10)
        authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        assert env.manager.token is not None
        assert env.manager.token.access_token == "access-2"
        assert read_token_file(env)["refresh_token"] == "refresh-2"

    def test_transient_refresh_failure_continues_with_a_warning(
        self, env: Env, caplog: pytest.LogCaptureFixture
    ) -> None:
        token_endpoint(env.rsps, status=503)
        token = stored_token(env, expires_in=-10)
        with caplog.at_level(logging.WARNING):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        assert env.manager.token == token
        assert "poll loop will retry" in caplog.text

    def test_permanent_refresh_failure_falls_through_to_consent(self, env: Env) -> None:
        token_endpoint(env.rsps, status=400, json={"error": "invalid_grant"})
        stored_token(env, expires_in=-10)
        with pytest.raises(ConsentRequired) as caught:
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        assert env.manager.token is None
        assert "state" in public_url(caught.value)
        assert env.store.load_pending() is not None

    def test_consent_after_a_permanent_startup_failure_does_not_inherit_the_backoff(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stored_token(env, expires_in=-10)
        env.rsps.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
        env.rsps.post(TOKEN_URL, json=token_response(expires_in=30))
        env.rsps.post(
            TOKEN_URL, json=token_response(access_token="access-3", refresh_token="refresh-3")
        )
        run_inputs(monkeypatch, ["PROMPTED"])
        authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=True)
        assert env.manager.token is not None
        assert env.manager.token.access_token == "access-2"

        assert env.manager.access_token() == "access-3"
        calls = env.token_calls()
        assert [call["grant_type"] for call in calls] == [
            "refresh_token",
            "authorization_code",
            "refresh_token",
        ]
        assert calls[2]["refresh_token"] == "refresh-2"
        assert read_token_file(env)["refresh_token"] == "refresh-3"

    def test_the_stored_token_starts_without_leftover_state(self, env: Env) -> None:
        stored_token(env, expires_in=3600)
        env.manager.persisted = False
        authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        assert env.manager.persisted is True

    def test_expired_token_without_refresh_token_falls_through_to_consent(self, env: Env) -> None:
        stored_token(env, expires_in=-10, refresh_token=None)
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        assert len(env.rsps.calls) == 0


class TestAuthenticateConsent:
    def test_without_a_tty_the_url_is_stable_across_runs(self, env: Env) -> None:
        with pytest.raises(ConsentRequired) as first:
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        with pytest.raises(ConsentRequired) as second:
            authenticate(
                env.fresh_manager(), env.store, env.oauth, env.settings(), interactive=False
            )
        assert first.value.authorize_url == second.value.authorize_url
        message = str(first.value)
        assert "docker compose run --rm -it" in message
        assert "OURA_AUTH_CODE" in message
        assert "OURA_AUTH_CODE_FILE" in message
        pending = env.store.load_pending()
        assert pending is not None
        query = public_url(first.value)
        assert query["state"] == pending.state
        assert query["code_challenge"] == challenge_for(pending.code_verifier)
        assert query["scope"] == "personal daily heartrate spo2 stress"
        assert len(env.rsps.calls) == 0

    def test_changed_settings_start_a_new_authorization(self, env: Env) -> None:
        with pytest.raises(ConsentRequired) as first:
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        session = build_session(retries=False)
        try:
            other_oauth = OAuthClient(
                session, CLIENT_ID, SECRET, REDIRECT, ("personal",), TOKEN_URL
            )
            with pytest.raises(ConsentRequired) as second:
                authenticate(
                    env.fresh_manager(),
                    env.store,
                    other_oauth,
                    env.settings(OURA_SCOPES="personal"),
                    interactive=False,
                )
        finally:
            session.close()
        assert first.value.authorize_url != second.value.authorize_url
        assert public_url(second.value)["scope"] == "personal"

    def test_code_without_a_pending_authorization_never_reaches_oura(self, env: Env) -> None:
        with pytest.raises(ConsentRequired) as caught:
            authenticate(
                env.manager,
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="unmatched"),
                interactive=False,
            )
        assert "no pending authorization matches this code" in str(caught.value)
        assert "OURA_AUTH_CODE" in str(caught.value)
        assert len(env.rsps.calls) == 0
        assert env.store.load_pending() is not None

    def test_code_with_a_pending_authorization_uses_its_verifier(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response(scope="personal daily heartrate spo2 stress"))
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        pending = env.store.load_pending()
        assert pending is not None

        manager = env.fresh_manager()
        authenticate(
            manager, env.store, env.oauth, env.settings(OURA_AUTH_CODE="CODE123"), interactive=False
        )
        assert env.token_calls() == [
            {
                "client_id": CLIENT_ID,
                "grant_type": "authorization_code",
                "code": "CODE123",
                "redirect_uri": REDIRECT,
                "code_verifier": pending.code_verifier,
            }
        ]
        assert manager.token is not None
        assert manager.token.access_token == "access-2"
        assert manager.persisted is True
        saved = read_token_file(env)
        assert saved["refresh_token"] == "refresh-2"
        assert saved["client_id"] == CLIENT_ID
        assert saved["scope"] == "personal daily heartrate spo2 stress"
        assert datetime.fromisoformat(saved["expires_at"]).timestamp() == NOW + 86400
        assert env.store.load_pending() is None
        assert oct(env.store.token_path.stat().st_mode & 0o777) == "0o600"

    def test_code_from_a_file_is_read_when_consent_is_needed(self, env: Env, tmp_path: Any) -> None:
        token_endpoint(env.rsps, json=token_response())
        code_file = tmp_path / "code.txt"
        settings = env.settings(OURA_AUTH_CODE_FILE=str(code_file))
        with pytest.raises(ConsentRequired) as first:
            authenticate(env.manager, env.store, env.oauth, settings, interactive=False)
        assert "docker compose run" in str(first.value)
        code_file.write_text("FILECODE\n", encoding="utf-8")
        authenticate(env.fresh_manager(), env.store, env.oauth, settings, interactive=False)
        assert env.token_calls()[0]["code"] == "FILECODE"

    def test_code_exchange_without_a_refresh_token_cannot_be_renewed(
        self, env: Env, caplog: pytest.LogCaptureFixture
    ) -> None:
        token_endpoint(env.rsps, json=token_response(refresh_token=None))
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        with caplog.at_level(logging.WARNING):
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="CODE"),
                interactive=False,
            )
        assert "cannot be renewed" in caplog.text
        assert read_token_file(env)["refresh_token"] is None

    def test_full_redirect_url_is_accepted_as_the_code(self, env: Env) -> None:
        token_endpoint(env.rsps, json=token_response())
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        pending = env.store.load_pending()
        assert pending is not None
        redirect = f"{REDIRECT}?code=FROMURL&state={pending.state}"
        authenticate(
            env.fresh_manager(),
            env.store,
            env.oauth,
            env.settings(OURA_AUTH_CODE=redirect),
            interactive=False,
        )
        assert env.token_calls()[0]["code"] == "FROMURL"

    def test_state_mismatch_is_rejected_without_a_token_request(self, env: Env) -> None:
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        redirect = f"{REDIRECT}?code=STOLEN&state=another-attempt"
        with pytest.raises(ConsentRequired) as caught:
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE=redirect),
                interactive=False,
            )
        assert "different authorization attempt" in str(caught.value)
        assert len(env.rsps.calls) == 0
        assert env.store.load_pending() is not None

    def test_access_denied_is_reported_without_a_token_request(self, env: Env) -> None:
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        pending = env.store.load_pending()
        assert pending is not None
        redirect = f"{REDIRECT}?error=access_denied&state={pending.state}"
        with pytest.raises(ConsentRequired) as caught:
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE=redirect),
                interactive=False,
            )
        assert "access_denied" in str(caught.value)
        assert len(env.rsps.calls) == 0

    def test_rejected_code_keeps_the_pending_authorization(self, env: Env) -> None:
        token_endpoint(env.rsps, status=400, json={"error": "invalid_grant"})
        with pytest.raises(ConsentRequired) as first:
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        pending = env.store.load_pending()
        with pytest.raises(ConsentRequired) as rejected:
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="USED"),
                interactive=False,
            )
        assert "single-use" in str(rejected.value)
        assert "invalid_grant" in str(rejected.value)
        assert rejected.value.authorize_url == first.value.authorize_url
        assert env.store.load_pending() == pending
        assert not env.store.token_path.exists()

        env.rsps.replace(responses.POST, TOKEN_URL, json=token_response())
        authenticate(
            env.fresh_manager(),
            env.store,
            env.oauth,
            env.settings(OURA_AUTH_CODE="FRESH"),
            interactive=False,
        )
        assert env.store.token_path.exists()

    def test_transient_exchange_failure_keeps_the_pending_authorization(self, env: Env) -> None:
        token_endpoint(env.rsps, status=503)
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        with pytest.raises(ConsentRequired) as caught:
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="CODE"),
                interactive=False,
            )
        assert "try again" in str(caught.value)
        assert env.store.load_pending() is not None

    @pytest.mark.parametrize(
        ("granted", "expect_warning"),
        [
            ("personal daily", True),
            ("personal daily heartrate spo2 stress", False),
            ("personal,daily,heartrate,spo2,stress", False),
            (["personal", "daily", "heartrate", "spo2"], True),
            (None, False),
            (42, False),
        ],
    )
    def test_missing_granted_scopes_are_reported(
        self,
        env: Env,
        caplog: pytest.LogCaptureFixture,
        granted: Any,
        expect_warning: bool,
    ) -> None:
        token_endpoint(env.rsps, json=token_response(scope=granted))
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        with caplog.at_level(logging.WARNING):
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="CODE"),
                interactive=False,
            )
        warned = "did not grant the scopes" in caplog.text
        assert warned is expect_warning
        if granted == "personal daily":
            assert "heartrate, spo2, stress" in caplog.text

    def test_failure_to_save_the_token_is_a_config_error(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token_endpoint(env.rsps, json=token_response())
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)

        def failing_save(token: Token) -> None:
            raise OSError(errno.EROFS, "Read-only file system")

        monkeypatch.setattr(env.store, "save_token", failing_save)
        with pytest.raises(ConfigError, match="code is used up"):
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="CODE"),
                interactive=False,
            )

    def test_failure_to_save_the_pending_authorization_is_a_config_error(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing_save(pending: Any) -> None:
            raise OSError(errno.EROFS, "Read-only file system")

        monkeypatch.setattr(env.store, "save_pending", failing_save)
        with pytest.raises(ConfigError, match="pending authorization"):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)


class TestInteractive:
    def test_prompt_accepts_a_code(
        self, env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        token_endpoint(env.rsps, json=token_response())
        prompts = run_inputs(monkeypatch, ["PROMPTED"])
        authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=True)
        assert len(prompts) == 1
        assert env.token_calls()[0]["code"] == "PROMPTED"
        printed = capsys.readouterr().out
        assert AUTHORIZE_URL in printed
        assert env.manager.token is not None
        assert env.store.load_pending() is None

    def test_prompt_retries_after_bad_input(
        self, env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        token_endpoint(env.rsps, status=400, json={"error": "invalid_grant"})
        answers: list[str | type[BaseException]] = [
            "two words",
            f"{REDIRECT}?code=A&state=wrong",
            "REJECTED",
        ]
        prompts = run_inputs(monkeypatch, answers)
        with pytest.raises(ConsentRequired) as caught:
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=True)
        assert len(prompts) == 3
        assert "giving up after 3 attempts" in str(caught.value)
        assert "single-use" in str(caught.value)
        assert len(env.token_calls()) == 1
        assert env.store.load_pending() is not None
        printed = capsys.readouterr().out
        assert "whitespace" in printed
        assert "different authorization attempt" in printed
        assert "Try again." in printed

    def test_prompt_recovers_after_a_rejected_code(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.rsps.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
        env.rsps.post(TOKEN_URL, json=token_response())
        prompts = run_inputs(monkeypatch, ["STALE", "GOOD"])
        authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=True)
        assert len(prompts) == 2
        assert [call["code"] for call in env.token_calls()] == ["STALE", "GOOD"]
        assert env.manager.token is not None

    def test_prompt_gives_up_when_stdin_is_closed(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_inputs(monkeypatch, [EOFError])
        with pytest.raises(ConsentRequired, match="no input available"):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=True)

    def test_a_provided_code_without_a_pending_authorization_falls_back_to_the_prompt(
        self,
        env: Env,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        token_endpoint(env.rsps, json=token_response())
        prompts = run_inputs(monkeypatch, ["PROMPTED"])
        with caplog.at_level(logging.WARNING):
            authenticate(
                env.manager,
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="STALE"),
                interactive=True,
            )
        assert "the provided authorization code was not usable" in caplog.text
        assert "no pending authorization matches it" in caplog.text
        assert "asking interactively instead" in caplog.text
        assert "STALE" not in caplog.text
        assert len(prompts) == 1
        calls = env.token_calls()
        assert [call["code"] for call in calls] == ["PROMPTED"]
        printed = capsys.readouterr().out
        assert f"code_challenge={challenge_for(calls[0]['code_verifier'])}" in printed
        assert env.manager.token is not None
        assert env.store.load_pending() is None

    def test_a_rejected_provided_code_falls_back_to_the_prompt_with_the_same_url(
        self,
        env: Env,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(ConsentRequired) as first:
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        pending = env.store.load_pending()
        assert pending is not None
        env.rsps.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
        env.rsps.post(TOKEN_URL, json=token_response())
        prompts = run_inputs(monkeypatch, ["GOOD"])
        with caplog.at_level(logging.WARNING):
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="USED"),
                interactive=True,
            )
        assert "the provided authorization code was not usable" in caplog.text
        assert "invalid_grant" in caplog.text
        assert len(prompts) == 1
        calls = env.token_calls()
        assert [call["code"] for call in calls] == ["USED", "GOOD"]
        assert {call["code_verifier"] for call in calls} == {pending.code_verifier}
        assert first.value.authorize_url in capsys.readouterr().out
        assert env.store.load_pending() is None

    def test_a_provided_url_with_the_wrong_state_falls_back_to_the_prompt(
        self,
        env: Env,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        token_endpoint(env.rsps, json=token_response())
        run_inputs(monkeypatch, ["GOOD"])
        with caplog.at_level(logging.WARNING):
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE=f"{REDIRECT}?code=STOLEN&state=another-attempt"),
                interactive=True,
            )
        assert "state mismatch" in caplog.text
        assert [call["code"] for call in env.token_calls()] == ["GOOD"]

    def test_a_usable_provided_code_needs_no_prompt(
        self,
        env: Env,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with pytest.raises(ConsentRequired):
            authenticate(env.manager, env.store, env.oauth, env.settings(), interactive=False)
        token_endpoint(env.rsps, json=token_response())
        prompts = run_inputs(monkeypatch, [])
        with caplog.at_level(logging.WARNING):
            authenticate(
                env.fresh_manager(),
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="GOOD"),
                interactive=True,
            )
        assert prompts == []
        assert "not usable" not in caplog.text
        assert [call["code"] for call in env.token_calls()] == ["GOOD"]

    def test_the_prompt_still_gives_up_after_an_unusable_provided_code(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prompts = run_inputs(monkeypatch, ["two words", "still two", "three words"])
        with pytest.raises(ConsentRequired, match="giving up after 3 attempts"):
            authenticate(
                env.manager,
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="STALE"),
                interactive=True,
            )
        assert len(prompts) == 3
        assert len(env.rsps.calls) == 0
        assert env.store.load_pending() is not None

    def test_without_a_tty_an_unusable_provided_code_still_fails(self, env: Env) -> None:
        with pytest.raises(ConsentRequired, match="no pending authorization matches"):
            authenticate(
                env.manager,
                env.store,
                env.oauth,
                env.settings(OURA_AUTH_CODE="STALE"),
                interactive=False,
            )
        assert len(env.rsps.calls) == 0
