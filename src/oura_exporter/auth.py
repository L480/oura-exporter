import base64
import contextlib
import hashlib
import logging
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from oura_exporter.config import ConfigError, Settings
from oura_exporter.storage import PendingAuthorization, Token, TokenStore

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
EXPIRY_MARGIN_SECONDS = 60.0
TRANSIENT_BACKOFF_SECONDS = 60.0
PERMANENT_BACKOFF_SECONDS = 3600.0
MAX_PROMPT_ATTEMPTS = 3
_QUERY_START = re.compile(r"(?:^|&)(?:code|error|state)=")


class AuthError(Exception):
    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class ConsentRequired(Exception):
    def __init__(self, message: str, authorize_url: str) -> None:
        super().__init__(message)
        self.authorize_url = authorize_url


def _error_detail(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        parts = [str(body[key])[:200] for key in ("error", "error_description") if body.get(key)]
        if parts:
            return ": ".join(parts)
    return f"HTTP {response.status_code}"


def describe_request_error(exc: requests.RequestException) -> str:
    cause = getattr(exc.args[0], "reason", None) if exc.args else None
    return f"{type(exc).__name__}: {cause or exc}"[:200]


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthClient:
    def __init__(
        self,
        session: requests.Session,
        client_id: str,
        client_secret: str | None,
        redirect_uri: str,
        scopes: Sequence[str],
        token_url: str,
        authorize_url: str = AUTHORIZE_URL,
        timeout: tuple[float, float] = (5.0, 30.0),
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._scopes = tuple(scopes)
        self._token_url = token_url
        self._authorize_url = authorize_url
        self._timeout = timeout

    @property
    def client_id(self) -> str:
        return self._client_id

    def new_pending(self) -> PendingAuthorization:
        return PendingAuthorization(
            code_verifier=secrets.token_urlsafe(64),
            state=secrets.token_urlsafe(32),
            client_id=self._client_id,
            redirect_uri=self._redirect_uri,
            scopes=self._scopes,
            created_at=datetime.now(UTC),
        )

    def authorize_url(self, pending: PendingAuthorization) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": pending.client_id,
                "redirect_uri": pending.redirect_uri,
                "scope": " ".join(pending.scopes),
                "state": pending.state,
                "code_challenge": _pkce_challenge(pending.code_verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{self._authorize_url}?{query}"

    def exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": verifier,
            },
            "code exchange",
        )

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}, "token refresh"
        )

    def _token_request(self, form: Mapping[str, str], action: str) -> dict[str, Any]:
        auth = (self._client_id, self._client_secret) if self._client_secret else None
        try:
            response = self._session.post(
                self._token_url,
                data={"client_id": self._client_id, **form},
                auth=auth,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise AuthError(
                f"{action}: token endpoint unreachable ({describe_request_error(exc)})"
            ) from exc
        status = response.status_code
        if status in {400, 401}:
            raise AuthError(f"{action}: rejected by Oura ({_error_detail(response)})", True)
        if not 200 <= status < 300:
            raise AuthError(f"{action}: token endpoint answered HTTP {status}")
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
            raise AuthError(f"{action}: token endpoint returned an unusable response")
        if not body["access_token"]:
            raise AuthError(f"{action}: token endpoint returned an empty access token")
        return body


def token_from_response(
    response: Mapping[str, Any], *, previous: Token | None, client_id: str | None, now: float
) -> Token:
    refresh_token = response.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = previous.refresh_token if previous is not None else None
        if previous is not None:
            logger.warning("token response has no refresh_token; keeping the previous one")
        else:
            logger.warning("token response has no refresh_token; the token cannot be renewed")

    expires_at: datetime | None = None
    expires_in = response.get("expires_in")
    if (
        isinstance(expires_in, int | float)
        and not isinstance(expires_in, bool)
        and math.isfinite(expires_in)
        and expires_in > 0
    ):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            expires_at = datetime.fromtimestamp(now + expires_in, tz=UTC)
    if expires_at is None:
        logger.warning("token response has no usable expires_in; the expiry is unknown")

    scope = response.get("scope")
    if previous is not None and previous.scope is not None:
        scope = previous.scope
    return Token(
        access_token=response["access_token"],
        refresh_token=refresh_token,
        expires_at=expires_at,
        client_id=client_id,
        scope=scope if isinstance(scope, str) and scope else None,
    )


class TokenManager:
    def __init__(
        self, store: TokenStore, oauth: OAuthClient, clock: Callable[[], float] = time.time
    ) -> None:
        self._store = store
        self._oauth = oauth
        self._clock = clock
        self.token: Token | None = None
        self.persisted = True
        self._next_refresh_at = 0.0
        self._last_error: AuthError | None = None
        self._suspect: set[str] = set()

    def now(self) -> float:
        return self._clock()

    def needs_refresh(self) -> bool:
        token = self.token
        if token is None or token.expires_at is None:
            return False
        return token.expires_at.timestamp() <= self._clock() + EXPIRY_MARGIN_SECONDS

    def access_token(self) -> str:
        token = self.token
        if token is None:
            raise AuthError("no Oura token available", True)
        if self.needs_refresh():
            token = self.refresh()
        return token.access_token

    def refresh(self) -> Token:
        now = self._clock()
        token = self.token
        if token is None or not token.refresh_token:
            raise AuthError("no refresh token available", True)
        if now < self._next_refresh_at:
            last = self._last_error
            raise AuthError(
                f"token refresh paused for another {self._next_refresh_at - now:.0f}s after an "
                f"earlier failure ({last})",
                last.permanent if last is not None else False,
            )
        try:
            response = self._oauth.refresh(token.refresh_token)
        except AuthError as exc:
            self._last_error = exc
            if exc.permanent:
                self._next_refresh_at = now + PERMANENT_BACKOFF_SECONDS
                logger.error(
                    "Oura rejected the stored refresh token (%s); re-authorize by restarting the "
                    "exporter interactively or with a new OURA_AUTH_CODE",
                    exc,
                )
            else:
                self._next_refresh_at = now + TRANSIENT_BACKOFF_SECONDS
            raise
        self._next_refresh_at = 0.0
        self._last_error = None
        refreshed = token_from_response(
            response,
            previous=token,
            client_id=token.client_id or self._oauth.client_id,
            now=now,
        )
        self.token = refreshed
        self._persist(refreshed)
        logger.info("refreshed the Oura access token")
        return refreshed

    def _persist(self, token: Token) -> None:
        try:
            self._store.save_token(token)
        except OSError as exc:
            self.persisted = False
            logger.error(
                "rotated token could not be saved to %s (%s); still running with the in-memory "
                "token and retrying; a restart before the save succeeds requires re-authorization",
                self._store.token_path,
                exc,
            )
        else:
            self.persisted = True

    def retry_persist(self) -> None:
        if self.persisted or self.token is None:
            return
        try:
            self._store.save_token(self.token)
        except OSError as exc:
            logger.debug("token still cannot be saved: %s", exc)
        else:
            self.persisted = True
            logger.info("rotated token saved to %s", self._store.token_path)

    def handle_unauthorized(self, used_token: str) -> bool:
        current = self.token
        if current is not None and current.access_token != used_token:
            return True
        if used_token in self._suspect:
            return False
        self._suspect = {self.refresh().access_token}
        return True

    def mark_working(self, used_token: str) -> None:
        self._suspect.discard(used_token)


def parse_authorization_response(text: str, expected_state: str) -> str:
    value = text.strip()
    if not value:
        raise AuthError("no authorization code entered", True)
    if "://" in value or "?" in value or value.startswith("/"):
        query: str | None = urlsplit(value).query
    elif _QUERY_START.search(value):
        query = value
    else:
        query = None
    if query is None:
        if any(char.isspace() for char in value):
            raise AuthError("the authorization code must not contain whitespace", True)
        return value
    params = parse_qs(query, keep_blank_values=True)
    error = params.get("error", [""])[0]
    if error:
        description = params.get("error_description", [""])[0]
        detail = f"{error} ({description})" if description else error
        raise AuthError(f"authorization was not granted: {detail}", True)
    state = params.get("state", [""])[0]
    if state and not secrets.compare_digest(state.encode(), expected_state.encode()):
        raise AuthError(
            "the response is from a different authorization attempt (state mismatch); "
            "open the authorization URL again",
            True,
        )
    code = params.get("code", [""])[0].strip()
    if not code or any(char.isspace() for char in code):
        raise AuthError("the response contains no usable code parameter", True)
    return code


def _rejected_message(error: AuthError) -> str:
    if error.permanent:
        return (
            f"authorization code rejected ({error}); codes are single-use and short-lived, "
            "open the URL again and use the new code"
        )
    return (
        f"could not exchange the authorization code ({error}); try again, and if it keeps "
        "failing open the URL again for a new code"
    )


def _exchange(oauth: OAuthClient, pending: PendingAuthorization, text: str) -> dict[str, Any]:
    code = parse_authorization_response(text, pending.state)
    return oauth.exchange_code(code, pending.code_verifier)


def _prompt_for_code(
    oauth: OAuthClient, pending: PendingAuthorization, authorize_url: str
) -> dict[str, Any]:
    print(
        "Open this URL, approve access, then paste the code (or the full URL you were "
        "redirected to) below:"
    )
    print(authorize_url, flush=True)
    last_error: AuthError | None = None
    for attempt in range(1, MAX_PROMPT_ATTEMPTS + 1):
        try:
            text = input("Authorization code or redirect URL: ")
        except EOFError:
            raise ConsentRequired("no input available on stdin", authorize_url) from None
        try:
            return _exchange(oauth, pending, text)
        except AuthError as exc:
            last_error = exc
            print(f"Authorization failed: {exc}", flush=True)
            if attempt < MAX_PROMPT_ATTEMPTS:
                print("Try again.", flush=True)
    message = (
        _rejected_message(last_error) if last_error is not None else "no authorization code entered"
    )
    raise ConsentRequired(
        f"giving up after {MAX_PROMPT_ATTEMPTS} attempts: {message}", authorize_url
    )


def _warn_missing_scopes(requested: Sequence[str], granted: object) -> None:
    if isinstance(granted, str):
        granted_scopes = {scope for scope in re.split(r"[\s,]+", granted) if scope}
    elif isinstance(granted, list):
        granted_scopes = {scope for scope in granted if isinstance(scope, str)}
    else:
        return
    missing = [scope for scope in requested if scope not in granted_scopes]
    if missing:
        logger.warning(
            "Oura did not grant the scopes %s; the related metrics will be absent or fail with 403",
            ", ".join(missing),
        )


def _finish_consent(
    manager: TokenManager, store: TokenStore, settings: Settings, response: Mapping[str, Any]
) -> None:
    token = token_from_response(
        response, previous=None, client_id=settings.client_id, now=manager.now()
    )
    try:
        store.save_token(token)
    except OSError as exc:
        raise ConfigError(
            f"authorization succeeded but the token could not be saved to {store.token_path}: "
            f"{exc}; the code is used up, authorize again once the directory is writable"
        ) from exc
    store.clear_pending()
    manager.token = token
    manager.persisted = True
    logger.info("authorization complete; token stored at %s", store.token_path)
    _warn_missing_scopes(settings.scopes, response.get("scope"))


def _consent(
    manager: TokenManager,
    store: TokenStore,
    oauth: OAuthClient,
    settings: Settings,
    interactive: bool,
) -> None:
    pending = store.load_pending()
    fresh = pending is None or not pending.matches(
        settings.client_id, settings.redirect_uri, settings.scopes
    )
    if pending is None or fresh:
        pending = oauth.new_pending()
        try:
            store.save_pending(pending)
        except OSError as exc:
            raise ConfigError(
                f"cannot save the pending authorization to {store.pending_path}: {exc}"
            ) from exc
    authorize_url = oauth.authorize_url(pending)

    code = settings.read_auth_code()
    if code is not None:
        if fresh:
            raise ConsentRequired(
                "no pending authorization matches this code; open the URL below, then set "
                "OURA_AUTH_CODE to the new code",
                authorize_url,
            )
        try:
            response = _exchange(oauth, pending, code)
        except AuthError as exc:
            raise ConsentRequired(_rejected_message(exc), authorize_url) from exc
    elif interactive:
        response = _prompt_for_code(oauth, pending, authorize_url)
    else:
        raise ConsentRequired(
            "no stored Oura token; open the URL below and approve access, then either run "
            "interactively (for example `docker compose run --rm -it oura-exporter`) and paste "
            "the code, or set OURA_AUTH_CODE / OURA_AUTH_CODE_FILE to the code (or the full "
            "redirect URL) and restart",
            authorize_url,
        )
    _finish_consent(manager, store, settings, response)


def _stored_token_usable(manager: TokenManager, settings: Settings) -> bool:
    if manager.needs_refresh():
        try:
            manager.refresh()
        except AuthError as exc:
            if exc.permanent:
                logger.warning("the stored token cannot be renewed (%s); authorizing again", exc)
                return False
            logger.warning("token refresh failed (%s); the poll loop will retry", exc)
    if settings.has_auth_code:
        logger.info("OURA_AUTH_CODE(_FILE) is ignored because a stored token exists")
    return True


def authenticate(
    manager: TokenManager,
    store: TokenStore,
    oauth: OAuthClient,
    settings: Settings,
    interactive: bool,
) -> None:
    token = store.load_token()
    if token is not None and token.client_id is not None and token.client_id != settings.client_id:
        logger.warning("ignoring the stored token: it was issued to a different client_id")
        token = None
    if token is not None:
        manager.token = token
        if _stored_token_usable(manager, settings):
            return
        manager.token = None
    _consent(manager, store, oauth, settings, interactive)
