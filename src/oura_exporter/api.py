import logging
import math
from collections.abc import Mapping
from typing import Any, Literal

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from oura_exporter import __version__
from oura_exporter.auth import TokenManager, describe_request_error

logger = logging.getLogger(__name__)

TIMEOUT = (5.0, 30.0)
MAX_PAGES = 20
RETRY_AFTER_DEFAULT = 60.0
RETRY_AFTER_MIN = 1.0
RETRY_AFTER_MAX = 3600.0

type Reason = Literal[
    "network", "invalid_response", "auth", "forbidden", "rate_limited", "http_error"
]
type Document = dict[str, Any]


class OuraApiError(Exception):
    def __init__(self, message: str, reason: Reason, status_code: int | None = None) -> None:
        super().__init__(message)
        self.reason: Reason = reason
        self.status_code = status_code


class RateLimitedError(OuraApiError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(
            f"rate limited by Oura; retry after {retry_after:.0f}s", "rate_limited", 429
        )
        self.retry_after = retry_after


def build_session(*, retries: bool = True) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = f"oura-exporter/{__version__}"
    if retries:
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=2,
                backoff_factor=1,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods={"GET"},
                respect_retry_after_header=False,
                raise_on_status=False,
            )
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session


def parse_retry_after(value: str | None) -> float:
    if value is None:
        return RETRY_AFTER_DEFAULT
    try:
        seconds = float(value)
    except ValueError:
        return RETRY_AFTER_DEFAULT
    if not math.isfinite(seconds):
        return RETRY_AFTER_DEFAULT
    return min(max(seconds, RETRY_AFTER_MIN), RETRY_AFTER_MAX)


def _excerpt(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


class OuraClient:
    def __init__(self, session: requests.Session, tokens: TokenManager, base_url: str) -> None:
        self._session = session
        self._tokens = tokens
        self._base_url = base_url.rstrip("/")

    def _send(self, endpoint: str, params: Mapping[str, str], token: str) -> requests.Response:
        try:
            return self._session.get(
                f"{self._base_url}/v2/usercollection/{endpoint}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OuraApiError(
                f"request for {endpoint} failed ({describe_request_error(exc)})", "network"
            ) from exc

    def _get(self, endpoint: str, params: Mapping[str, str]) -> Any:
        used = self._tokens.access_token()
        response = self._send(endpoint, params, used)
        if response.status_code == 401:
            if not self._tokens.handle_unauthorized(used):
                raise OuraApiError(f"Oura rejected the access token for {endpoint}", "auth", 401)
            used = self._tokens.access_token()
            response = self._send(endpoint, params, used)
            if response.status_code == 401:
                raise OuraApiError(f"Oura rejected the access token for {endpoint}", "auth", 401)
        return self._interpret(endpoint, response, used)

    def _interpret(self, endpoint: str, response: requests.Response, used: str) -> Any:
        status = response.status_code
        if 200 <= status < 300:
            self._tokens.mark_working(used)
            try:
                return response.json()
            except ValueError as exc:
                raise OuraApiError(
                    f"{endpoint} returned invalid JSON", "invalid_response", status
                ) from exc
        if status == 403:
            raise OuraApiError(
                f"{endpoint} is forbidden (HTTP 403): scope not granted or Oura membership expired",
                "forbidden",
                status,
            )
        if status == 429:
            raise RateLimitedError(parse_retry_after(response.headers.get("Retry-After")))
        raise OuraApiError(
            f"{endpoint} answered HTTP {status}: {_excerpt(response.text)}", "http_error", status
        )

    def get_documents(
        self, endpoint: str, params: Mapping[str, str] | None = None
    ) -> list[Document]:
        query = dict(params or {})
        documents: list[Document] = []
        for _ in range(MAX_PAGES):
            body = self._get(endpoint, query)
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise OuraApiError(f"{endpoint} returned an unexpected body", "invalid_response")
            for item in body["data"]:
                if not isinstance(item, dict):
                    raise OuraApiError(
                        f"{endpoint} returned a non-object document", "invalid_response"
                    )
                documents.append(item)
            next_token = body.get("next_token")
            if not isinstance(next_token, str) or not next_token:
                return documents
            query["next_token"] = next_token
        logger.warning(
            "%s: stopped after %d pages although more data is available", endpoint, MAX_PAGES
        )
        return documents

    def get_document(self, endpoint: str) -> Document:
        body = self._get(endpoint, {})
        if not isinstance(body, dict):
            raise OuraApiError(f"{endpoint} returned an unexpected body", "invalid_response")
        return body
