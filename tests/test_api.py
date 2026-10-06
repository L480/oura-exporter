import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import requests
import responses
from responses import matchers
from urllib3.exceptions import MaxRetryError

from oura_exporter import __version__
from oura_exporter.api import (
    MAX_PAGES,
    OuraApiError,
    OuraClient,
    RateLimitedError,
    build_session,
    parse_retry_after,
)
from oura_exporter.auth import AuthError, OAuthClient, TokenManager, describe_request_error
from oura_exporter.storage import Token, TokenStore

from .helpers import BASE_URL, TOKEN_URL, FakeClock, api_url

ENDPOINT = "daily_activity"
URL = api_url(ENDPOINT)
NOW = 1_800_000_000.0


@dataclass
class Api:
    client: OuraClient
    manager: TokenManager
    rsps: responses.RequestsMock

    def token_calls(self) -> int:
        return len([call for call in self.rsps.calls if call.request.url == TOKEN_URL])

    def api_calls(self) -> list[responses.Call]:
        return [call for call in self.rsps.calls if call.request.url != TOKEN_URL]


@pytest.fixture
def api(tmp_path: Any, rsps: responses.RequestsMock) -> Iterator[Api]:
    session = build_session()
    oauth_session = build_session(retries=False)
    store = TokenStore(tmp_path / "data" / "oauth_token.json")
    store.prepare()
    oauth = OAuthClient(
        oauth_session, "cid", "secret", "http://localhost/cb", ("daily",), TOKEN_URL
    )
    manager = TokenManager(store, oauth, FakeClock(NOW))
    manager.token = Token(
        "access-1", "refresh-1", datetime.fromtimestamp(NOW + 3600, tz=UTC), "cid", None
    )
    yield Api(OuraClient(session, manager, BASE_URL), manager, rsps)
    session.close()
    oauth_session.close()


def page(*ids: int, next_token: str | None = None) -> dict[str, Any]:
    return {"data": [{"id": number} for number in ids], "next_token": next_token}


def bearer(call: responses.Call) -> str | None:
    value = call.request.headers.get("Authorization")
    return str(value) if value is not None else None


class TestSession:
    def test_user_agent_and_retry_policy(self) -> None:
        session = build_session()
        try:
            assert session.headers["User-Agent"] == f"oura-exporter/{__version__}"
            for scheme in ("http://example.org", "https://example.org"):
                retry = session.get_adapter(scheme).max_retries
                assert retry.total == 2
                assert retry.backoff_factor == 1
                assert set(retry.status_forcelist) == {500, 502, 503, 504}
                assert retry.allowed_methods == {"GET"}
                assert retry.respect_retry_after_header is False
                assert retry.raise_on_status is False
        finally:
            session.close()

    def test_token_session_does_not_retry(self) -> None:
        session = build_session(retries=False)
        try:
            assert session.get_adapter("https://example.org").max_retries.total == 0
            assert session.headers["User-Agent"] == f"oura-exporter/{__version__}"
        finally:
            session.close()


class TestRetryAfter:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, 60.0),
            ("5", 5.0),
            ("120", 120.0),
            ("2.5", 2.5),
            ("0", 1.0),
            ("-30", 1.0),
            ("99999", 3600.0),
            ("soon", 60.0),
            ("", 60.0),
            ("nan", 60.0),
            ("inf", 60.0),
            ("Wed, 21 Oct 2026 07:28:00 GMT", 60.0),
        ],
    )
    def test_parsing_and_clamping(self, value: str | None, expected: float) -> None:
        assert parse_retry_after(value) == expected


class TestDescribeRequestError:
    def test_uses_the_root_cause_and_hides_the_url(self) -> None:
        wrapped = MaxRetryError(None, "/v2/x?token=1", reason=OSError("connection refused"))
        text = describe_request_error(requests.ConnectionError(wrapped))
        assert text == "ConnectionError: connection refused"

    def test_falls_back_to_the_message(self) -> None:
        assert describe_request_error(requests.Timeout("timed out")) == "Timeout: timed out"
        assert describe_request_error(requests.RequestException()) == "RequestException: "

    def test_is_bounded(self) -> None:
        assert len(describe_request_error(requests.Timeout("x" * 1000))) == 200


class TestRequests:
    def test_sends_bearer_token_and_params(self, api: Api) -> None:
        api.rsps.get(URL, json=page(1))
        api.client.get_documents(ENDPOINT, {"start_date": "2026-10-01", "fields": "a,b"})
        call = api.api_calls()[0]
        assert bearer(call) == "Bearer access-1"
        assert call.request.params == {"start_date": "2026-10-01", "fields": "a,b"}
        assert call.request.headers["User-Agent"] == f"oura-exporter/{__version__}"

    def test_trailing_slash_in_the_base_url(self, api: Api) -> None:
        api.rsps.get(URL, json=page(1))
        session = requests.Session()
        try:
            client = OuraClient(session, api.manager, f"{BASE_URL}/")
            assert client.get_documents(ENDPOINT) == [{"id": 1}]
        finally:
            session.close()
        assert api.api_calls()[0].request.url == URL

    def test_get_document(self, api: Api) -> None:
        api.rsps.get(api_url("personal_info"), json={"id": "x", "age": 3})
        assert api.client.get_document("personal_info") == {"id": "x", "age": 3}
        assert api.api_calls()[0].request.params == {}

    def test_get_document_requires_an_object(self, api: Api) -> None:
        api.rsps.get(api_url("personal_info"), json=["nope"])
        with pytest.raises(OuraApiError) as caught:
            api.client.get_document("personal_info")
        assert caught.value.reason == "invalid_response"

    def test_missing_token_propagates_the_auth_error(self, api: Api) -> None:
        api.manager.token = None
        with pytest.raises(AuthError) as caught:
            api.client.get_documents(ENDPOINT)
        assert caught.value.permanent is True
        assert api.api_calls() == []


class TestPagination:
    def test_follows_next_token_and_keeps_other_params(self, api: Api) -> None:
        base = {"start_date": "2026-10-01", "fields": "a,b"}
        api.rsps.get(
            URL,
            json=page(1, 2, next_token="T1"),
            match=[matchers.query_param_matcher(base, strict_match=True)],
        )
        api.rsps.get(
            URL,
            json=page(3, next_token="T2"),
            match=[matchers.query_param_matcher({**base, "next_token": "T1"}, strict_match=True)],
        )
        api.rsps.get(
            URL,
            json=page(4),
            match=[matchers.query_param_matcher({**base, "next_token": "T2"}, strict_match=True)],
        )
        documents = api.client.get_documents(ENDPOINT, base)
        assert [document["id"] for document in documents] == [1, 2, 3, 4]
        assert len(api.api_calls()) == 3

    def test_caller_params_are_not_mutated(self, api: Api) -> None:
        api.rsps.get(URL, json=page(1, next_token="T1"))
        api.rsps.get(URL, json=page(2))
        params = {"a": "1"}
        api.client.get_documents(ENDPOINT, params)
        assert params == {"a": "1"}

    def test_stops_after_twenty_pages_with_a_warning(
        self, api: Api, caplog: pytest.LogCaptureFixture
    ) -> None:
        api.rsps.get(URL, json=page(1, next_token="more"))
        with caplog.at_level(logging.WARNING):
            documents = api.client.get_documents(ENDPOINT)
        assert MAX_PAGES == 20
        assert len(documents) == 20
        assert len(api.api_calls()) == 20
        assert "stopped after 20 pages" in caplog.text

    def test_exactly_twenty_pages_do_not_warn(
        self, api: Api, caplog: pytest.LogCaptureFixture
    ) -> None:
        for number in range(19):
            api.rsps.get(URL, json=page(number, next_token=f"T{number}"))
        api.rsps.get(URL, json=page(99))
        with caplog.at_level(logging.WARNING):
            documents = api.client.get_documents(ENDPOINT)
        assert len(documents) == 20
        assert caplog.text == ""

    @pytest.mark.parametrize(
        "body",
        [
            [],
            "text",
            {"nodata": []},
            {"data": "x"},
            {"data": {"a": 1}},
            {"data": [1]},
            {"data": ["x"]},
        ],
    )
    def test_invalid_page_shapes(self, api: Api, body: Any) -> None:
        api.rsps.get(URL, json=body)
        with pytest.raises(OuraApiError) as caught:
            api.client.get_documents(ENDPOINT)
        assert caught.value.reason == "invalid_response"

    def test_invalid_second_page_discards_everything(self, api: Api) -> None:
        api.rsps.get(URL, json=page(1, next_token="T1"))
        api.rsps.get(URL, json={"data": "broken"})
        with pytest.raises(OuraApiError) as caught:
            api.client.get_documents(ENDPOINT)
        assert caught.value.reason == "invalid_response"

    def test_empty_next_token_ends_pagination(self, api: Api) -> None:
        api.rsps.get(URL, json=page(1, next_token=""))
        assert api.client.get_documents(ENDPOINT) == [{"id": 1}]
        assert len(api.api_calls()) == 1


class TestErrorMapping:
    def test_forbidden(self, api: Api) -> None:
        api.rsps.get(URL, status=403, json={"detail": "nope"})
        with pytest.raises(OuraApiError) as caught:
            api.client.get_documents(ENDPOINT)
        assert caught.value.reason == "forbidden"
        assert caught.value.status_code == 403
        assert "scope" in str(caught.value)
        assert "membership" in str(caught.value)

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"Retry-After": "120"}, 120.0),
            ({"Retry-After": "0"}, 1.0),
            ({"Retry-After": "86400"}, 3600.0),
            ({"Retry-After": "later"}, 60.0),
            ({}, 60.0),
        ],
    )
    def test_rate_limited(self, api: Api, headers: dict[str, str], expected: float) -> None:
        api.rsps.get(URL, status=429, headers=headers)
        with pytest.raises(RateLimitedError) as caught:
            api.client.get_documents(ENDPOINT)
        assert caught.value.retry_after == expected
        assert caught.value.reason == "rate_limited"
        assert isinstance(caught.value, OuraApiError)

    @pytest.mark.parametrize("status", [400, 404, 422, 500, 502, 503, 504])
    def test_other_statuses_are_http_errors(self, api: Api, status: int) -> None:
        api.rsps.get(URL, status=status, body="something went wrong")
        with pytest.raises(OuraApiError) as caught:
            api.client.get_documents(ENDPOINT)
        assert caught.value.reason == "http_error"
        assert caught.value.status_code == status
        assert str(status) in str(caught.value)
        assert "something went wrong" in str(caught.value)

    def test_http_error_bodies_are_truncated(self, api: Api) -> None:
        api.rsps.get(URL, status=500, body="x" * 5000 + "\nsecond line")
        with pytest.raises(OuraApiError) as caught:
            api.client.get_documents(ENDPOINT)
        excerpt = str(caught.value).split("HTTP 500: ", 1)[1]
        assert len(excerpt) == 200
        assert "second line" not in excerpt

    @pytest.mark.parametrize(
        "failure", [requests.ConnectionError("refused"), requests.Timeout("timed out")]
    )
    def test_network_errors(self, api: Api, failure: Exception) -> None:
        api.rsps.get(URL, body=failure)
        with pytest.raises(OuraApiError) as caught:
            api.client.get_documents(ENDPOINT)
        assert caught.value.reason == "network"
        assert type(failure).__name__ in str(caught.value)

    def test_invalid_json(self, api: Api) -> None:
        api.rsps.get(URL, status=200, body="<html>maintenance</html>")
        with pytest.raises(OuraApiError) as caught:
            api.client.get_documents(ENDPOINT)
        assert caught.value.reason == "invalid_response"

    def test_the_access_token_never_appears_in_errors(self, api: Api) -> None:
        api.rsps.get(URL, status=500, body="boom")
        with pytest.raises(OuraApiError) as caught:
            api.client.get_documents(ENDPOINT)
        assert "access-1" not in str(caught.value)


class TestUnauthorized:
    def test_refreshes_once_and_retries(self, api: Api) -> None:
        api.rsps.get(URL, status=401)
        api.rsps.get(URL, json=page(1))
        api.rsps.post(
            TOKEN_URL,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        assert api.client.get_documents(ENDPOINT) == [{"id": 1}]
        assert [bearer(call) for call in api.api_calls()] == ["Bearer access-1", "Bearer access-2"]
        assert api.token_calls() == 1

    def test_a_working_token_may_be_refreshed_again_later(self, api: Api) -> None:
        api.rsps.get(URL, status=401)
        api.rsps.get(URL, json=page(1))
        api.rsps.get(URL, status=401)
        api.rsps.get(URL, json=page(2))
        api.rsps.post(
            TOKEN_URL,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        api.rsps.post(
            TOKEN_URL,
            json={"access_token": "access-3", "refresh_token": "refresh-3", "expires_in": 3600},
        )
        assert api.client.get_documents(ENDPOINT) == [{"id": 1}]
        assert api.client.get_documents(ENDPOINT) == [{"id": 2}]
        assert api.token_calls() == 2
        assert bearer(api.api_calls()[-1]) == "Bearer access-3"

    def test_repeated_401_does_not_cause_a_refresh_storm(self, api: Api) -> None:
        api.rsps.get(URL, status=401)
        api.rsps.post(
            TOKEN_URL,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        for _ in range(3):
            with pytest.raises(OuraApiError) as caught:
                api.client.get_documents(ENDPOINT)
            assert caught.value.reason == "auth"
            assert caught.value.status_code == 401
        assert api.token_calls() == 1
        assert len(api.api_calls()) == 4

    def test_failing_refresh_propagates_the_auth_error(self, api: Api) -> None:
        api.rsps.get(URL, status=401)
        api.rsps.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
        with pytest.raises(AuthError) as caught:
            api.client.get_documents(ENDPOINT)
        assert caught.value.permanent is True
        assert len(api.api_calls()) == 1
