import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest
import requests
import responses

from oura_exporter import __version__
from oura_exporter import exporter as exporter_module
from oura_exporter.definitions import load_definitions
from oura_exporter.exporter import Exporter, OuraCollector
from oura_exporter.storage import Token

from .helpers import (
    ENDPOINTS,
    TODAY,
    TOKEN_URL,
    WALL,
    Rig,
    api_url,
    build_rig,
    epoch,
    load_fixture,
    register_endpoint,
    register_endpoints,
)

CATEGORIES = list(ENDPOINTS)


def poll_records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "oura_exporter.exporter" and record.levelno == level
    ]


class TestFullPoll:
    def test_exports_every_category(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert rig.value("oura_daily_activity_score") == 78
        assert rig.value("oura_daily_activity_steps") == 8731
        assert rig.value("oura_daily_activity_contributors_stay_active") == 61
        assert rig.value("oura_daily_activity_timestamp_seconds") == epoch("2026-10-06T00:00:00Z")
        assert rig.value("oura_daily_readiness_temperature_deviation_celsius") == -0.21
        assert rig.value("oura_daily_resilience_level") == 3
        assert rig.value("oura_daily_sleep_score") == 84
        assert rig.value("oura_daily_spo2_average_percent") == 96.84
        assert rig.value("oura_daily_stress_day_summary") == 2
        assert rig.value("oura_sleep_total_sleep_duration_seconds") == 26040
        assert rig.value("oura_heartrate_bpm") == 57
        assert rig.value("oura_heartrate_timestamp_seconds") == epoch("2026-10-06T07:09:00Z")
        assert rig.value("oura_ring_battery_level_percent") == 74
        assert rig.value("oura_ring_battery_charging") == 0
        assert rig.value("oura_personal_info_age_years") == 34
        assert rig.value("oura_personal_info_timestamp_seconds") is None
        assert "email" not in rig.text()
        assert "person@example" not in rig.text()

    def test_state_sets_and_info_metrics(self, rig: Rig) -> None:
        rig.exporter.poll()
        states = ["awake", "rest", "sleep", "session", "live", "workout"]
        for state in states:
            expected = 1 if state == "rest" else 0
            assert rig.value("oura_heartrate_source", oura_heartrate_source=state) == expected
        assert rig.value("oura_personal_info_biological_sex_info", biological_sex="male") == 1
        text = rig.text()
        assert 'oura_heartrate_source{oura_heartrate_source="rest"} 1.0' in text
        assert 'oura_personal_info_biological_sex_info{biological_sex="male"} 1.0' in text
        assert "# TYPE oura_personal_info_biological_sex_info gauge" in text

    def test_self_metrics(self, rig: Rig) -> None:
        assert rig.value("oura_exporter_build_info", version=__version__) == 1
        assert rig.value("oura_exporter_auth_ok") == 1
        assert rig.value("oura_exporter_token_persisted") == 1
        assert rig.up("daily_activity") is None
        rig.exporter.poll()
        for category in CATEGORIES:
            assert rig.up(category) == 1
            assert (
                rig.value(
                    "oura_exporter_category_last_success_timestamp_seconds", category=category
                )
                == WALL
            )
        rig.tokens.persisted = False
        assert rig.value("oura_exporter_token_persisted") == 0

    def test_request_parameters(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert len(rig.rsps.calls) == 10
        activity = rig.calls("daily_activity")[0].request.params
        assert activity["start_date"] == "2026-09-29"
        assert activity["end_date"] == "2026-10-07"
        assert "steps" in activity["fields"].split(",")
        assert rig.calls("heartrate")[0].request.params["latest"] == "true"
        assert rig.calls("personal_info")[0].request.params == {}

    def test_collector_describes_nothing_and_registers_once(self) -> None:
        collector = OuraCollector(load_definitions())
        assert collector.describe() == []
        assert list(collector.collect()) == []

    def test_an_empty_answer_counts_as_success_and_clears_the_series(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert rig.value("oura_daily_activity_score") == 78
        rig.advance(300)
        rig.rsps.replace(
            responses.GET, api_url("daily_activity"), json={"data": [], "next_token": None}
        )
        rig.exporter.poll()
        assert rig.up("daily_activity") == 1
        assert rig.value("oura_daily_activity_score") is None
        assert rig.value("oura_daily_activity_timestamp_seconds") is None


class TestSnapshots:
    def test_null_fields_remove_previously_exported_series(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert rig.value("oura_daily_activity_score") == 78
        assert rig.value("oura_daily_sleep_score") == 84
        assert rig.value("oura_ring_battery_charging") == 0
        assert rig.value("oura_personal_info_age_years") == 34

        rig.advance(3600)
        register_endpoints(rig.rsps, nulls=True, replace=True)
        rig.exporter.poll()

        assert rig.value("oura_daily_activity_score") is None
        assert rig.value("oura_daily_activity_contributors_stay_active") is None
        assert rig.value("oura_daily_activity_steps") == 8731
        assert rig.value("oura_daily_readiness_score") is None
        assert rig.value("oura_daily_readiness_temperature_deviation_celsius") is None
        assert rig.value("oura_daily_sleep_score") is None
        assert rig.value("oura_daily_spo2_average_percent") is None
        assert rig.value("oura_daily_stress_day_summary") is None
        assert rig.value("oura_sleep_total_sleep_duration_seconds") is None
        assert rig.value("oura_sleep_time_in_bed_seconds") == 28080
        assert rig.value("oura_ring_battery_charging") is None
        assert rig.value("oura_ring_battery_level_percent") == 74
        assert rig.value("oura_personal_info_age_years") is None
        assert rig.value("oura_personal_info_biological_sex_info", biological_sex="male") is None
        assert all(rig.up(category) == 1 for category in CATEGORIES)

    def test_failure_keeps_the_snapshot(self, rig: Rig) -> None:
        rig.exporter.poll()
        first_success = rig.value(
            "oura_exporter_category_last_success_timestamp_seconds", category="daily_activity"
        )
        rig.advance(300)
        rig.rsps.replace(responses.GET, api_url("daily_activity"), status=500, body="boom")
        rig.exporter.poll()

        assert rig.value("oura_daily_activity_score") == 78
        assert rig.up("daily_activity") == 0
        assert rig.errors("daily_activity", "http_error") == 1
        assert (
            rig.value(
                "oura_exporter_category_last_success_timestamp_seconds", category="daily_activity"
            )
            == first_success
        )
        assert rig.up("daily_readiness") == 1

        rig.advance(300)
        rig.exporter.poll()
        assert rig.errors("daily_activity", "http_error") == 2

        rig.advance(300)
        register_endpoint(rig.rsps, "daily_activity", replace=True)
        rig.exporter.poll()
        assert rig.up("daily_activity") == 1
        assert (
            rig.value(
                "oura_exporter_category_last_success_timestamp_seconds", category="daily_activity"
            )
            == first_success + 900
        )

    def test_successful_fetch_replaces_values(self, rig: Rig) -> None:
        rig.exporter.poll()
        rig.advance(300)
        payload = load_fixture("daily_activity")
        payload["data"][1]["score"] = 55
        rig.rsps.replace(responses.GET, api_url("daily_activity"), json=payload)
        rig.exporter.poll()
        assert rig.value("oura_daily_activity_score") == 55


class TestScheduling:
    def test_refresh_interval_is_honoured(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert len(rig.calls("personal_info")) == 1
        assert len(rig.calls("daily_activity")) == 1
        rig.advance(300)
        rig.exporter.poll()
        assert len(rig.calls("personal_info")) == 1
        assert len(rig.calls("daily_activity")) == 2
        rig.advance(3300)
        rig.exporter.poll()
        assert len(rig.calls("personal_info")) == 2
        assert len(rig.calls("daily_activity")) == 3

    def test_categories_run_every_poll_without_a_refresh_interval(self, rig: Rig) -> None:
        for _ in range(3):
            rig.exporter.poll()
            rig.advance(300)
        assert len(rig.calls("heartrate")) == 3

    def test_forbidden_categories_are_retried_after_an_hour(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.rsps.replace(responses.GET, api_url("daily_sleep"), status=403, json={"detail": "no"})
        with caplog.at_level(logging.DEBUG, logger="oura_exporter.exporter"):
            rig.exporter.poll()
        assert rig.up("daily_sleep") == 0
        assert rig.errors("daily_sleep", "forbidden") == 1
        assert rig.up("daily_stress") == 1
        warnings = poll_records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert "forbidden" in warnings[0].getMessage()
        assert "3600" in warnings[0].getMessage()
        assert "scope" in warnings[0].getMessage()

        for _ in range(11):
            rig.advance(300)
            rig.exporter.poll()
        assert len(rig.calls("daily_sleep")) == 1
        assert len(rig.calls("daily_stress")) == 12

        rig.advance(300)
        rig.exporter.poll()
        assert len(rig.calls("daily_sleep")) == 2
        assert rig.errors("daily_sleep", "forbidden") == 2

    def test_rate_limit_pauses_everything_and_ends_the_cycle(self, rig: Rig) -> None:
        rig.rsps.replace(
            responses.GET, api_url("daily_activity"), status=429, headers={"Retry-After": "120"}
        )
        rig.exporter.poll()
        assert len(rig.rsps.calls) == 1
        assert rig.up("daily_activity") == 0
        assert rig.errors("daily_activity", "rate_limited") == 1
        assert rig.up("daily_readiness") is None

        rig.advance(60)
        rig.exporter.poll()
        assert len(rig.rsps.calls) == 1

        register_endpoint(rig.rsps, "daily_activity", replace=True)
        rig.advance(61)
        rig.exporter.poll()
        assert len(rig.rsps.calls) == 1 + 10
        assert all(rig.up(category) == 1 for category in CATEGORIES)

    def test_poll_returns_when_asked_to_stop(self, rig: Rig) -> None:
        stop = threading.Event()
        stop.set()
        rig.exporter.poll(stop)
        assert len(rig.rsps.calls) == 0

    def test_poll_stops_between_categories(self, rig: Rig) -> None:
        stop = threading.Event()
        original = rig.exporter._fetch

        def fetch_then_stop(*args: Any) -> Any:
            stop.set()
            return original(*args)

        rig.exporter._fetch = fetch_then_stop  # type: ignore[method-assign]
        rig.exporter.poll(stop)
        assert len(rig.rsps.calls) == 1


class TestFailureIsolation:
    def test_one_broken_category_does_not_stop_the_others(self, rig: Rig) -> None:
        rig.rsps.replace(responses.GET, api_url("daily_readiness"), body="<html>oops</html>")
        rig.exporter.poll()
        assert rig.up("daily_readiness") == 0
        assert rig.errors("daily_readiness", "invalid_response") == 1
        assert rig.up("daily_activity") == 1
        assert rig.up("personal_info") == 1
        assert len(rig.rsps.calls) == 10

    def test_network_errors_do_not_stop_the_other_categories(self, rig: Rig) -> None:
        rig.rsps.replace(
            responses.GET, api_url("daily_activity"), body=requests.ConnectionError("down")
        )
        rig.exporter.poll()
        assert rig.errors("daily_activity", "network") == 1
        assert rig.up("daily_sleep") == 1

    def test_unexpected_exceptions_are_logged_once_and_isolated(
        self,
        rig: Rig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        real = exporter_module.build_snapshot

        def explode(category: Any, documents: Any, warned: Any = None) -> Any:
            if category.name == "daily_stress":
                raise RuntimeError("bug")
            return real(category, documents, warned)

        monkeypatch.setattr(exporter_module, "build_snapshot", explode)
        with caplog.at_level(logging.DEBUG, logger="oura_exporter.exporter"):
            rig.exporter.poll()
            rig.advance(300)
            rig.exporter.poll()

        assert rig.errors("daily_stress", "internal") == 2
        assert rig.up("daily_stress") == 0
        assert rig.up("sleep") == 1
        assert rig.up("personal_info") == 1
        errors = poll_records(caplog, logging.ERROR)
        assert len(errors) == 1
        assert errors[0].exc_info is not None
        assert "RuntimeError" in "".join(str(part) for part in errors[0].exc_info)
        assert len(poll_records(caplog, logging.WARNING)) == 0

    def test_authentication_failure_ends_the_cycle(self, rig: Rig) -> None:
        assert rig.tokens.token is not None
        rig.tokens.token = Token(
            "old", "refresh-1", datetime.fromtimestamp(WALL - 5, tz=UTC), "cid"
        )
        rig.rsps.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 0
        assert rig.errors("daily_activity", "auth") == 1
        assert rig.up("daily_activity") == 0
        assert rig.up("daily_readiness") is None
        assert len(rig.calls("daily_activity")) == 0

        rig.advance(300)
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 0
        assert rig.errors("daily_activity", "auth") == 2
        assert len(rig.rsps.calls) == 1

        rig.advance(3600)
        rig.rsps.replace(
            responses.POST,
            TOKEN_URL,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 1
        assert all(rig.up(category) == 1 for category in CATEGORIES)

    def test_rejected_access_token_ends_the_cycle(self, rig: Rig) -> None:
        rig.rsps.replace(responses.GET, api_url("daily_activity"), status=401)
        rig.rsps.post(
            TOKEN_URL,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 0
        assert rig.errors("daily_activity", "auth") == 1
        assert rig.up("daily_readiness") is None
        assert len(rig.calls("daily_activity")) == 2

        rig.advance(300)
        rig.exporter.poll()
        assert len(rig.calls("daily_activity")) == 3
        assert len(rig.rsps.calls) == 1 + 2 + 1

        register_endpoint(rig.rsps, "daily_activity", replace=True)
        rig.advance(300)
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 1


class TestLogging:
    def test_routine_success_is_quiet(self, rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="oura_exporter.exporter"):
            rig.exporter.poll()
            rig.advance(300)
            rig.exporter.poll()
        assert poll_records(caplog, logging.INFO) == []
        assert poll_records(caplog, logging.WARNING) == []
        assert len(poll_records(caplog, logging.DEBUG)) > 0

    def test_state_changes_are_logged_and_repeats_are_not(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="oura_exporter.exporter"):
            rig.rsps.replace(responses.GET, api_url("daily_sleep"), status=500, body="boom")
            rig.exporter.poll()
            assert len(poll_records(caplog, logging.WARNING)) == 1

            rig.advance(300)
            rig.exporter.poll()
            assert len(poll_records(caplog, logging.WARNING)) == 1

            rig.rsps.replace(responses.GET, api_url("daily_sleep"), status=503, body="busy")
            rig.advance(300)
            rig.exporter.poll()
            assert len(poll_records(caplog, logging.WARNING)) == 1

            rig.rsps.replace(responses.GET, api_url("daily_sleep"), status=403)
            rig.advance(300)
            rig.exporter.poll()
            warnings = poll_records(caplog, logging.WARNING)
            assert len(warnings) == 2
            assert "forbidden" in warnings[1].getMessage()

            register_endpoint(rig.rsps, "daily_sleep", replace=True)
            rig.advance(3600)
            rig.exporter.poll()
            infos = poll_records(caplog, logging.INFO)
            assert len(infos) == 1
            assert "recovered" in infos[0].getMessage()
            assert "daily_sleep" in infos[0].getMessage()

            rig.advance(300)
            rig.exporter.poll()
            assert len(poll_records(caplog, logging.INFO)) == 1
            assert len(poll_records(caplog, logging.WARNING)) == 2


class TestFieldsFallback:
    @pytest.mark.parametrize("status", [400, 422])
    def test_rejected_fields_are_dropped_for_good(
        self, rig: Rig, caplog: pytest.LogCaptureFixture, status: int
    ) -> None:
        rig.rsps.replace(
            responses.GET, api_url("daily_activity"), status=status, json={"detail": "x"}
        )
        register_endpoint(rig.rsps, "daily_activity")
        with caplog.at_level(logging.WARNING, logger="oura_exporter.exporter"):
            rig.exporter.poll()
            rig.advance(300)
            rig.exporter.poll()
            rig.advance(300)
            rig.exporter.poll()

        calls = rig.calls("daily_activity")
        assert [("fields" in call.request.params) for call in calls] == [True, False, False, False]
        assert rig.up("daily_activity") == 1
        assert rig.value("oura_daily_activity_score") == 78
        assert rig.errors("daily_activity", "http_error") is None
        warnings = poll_records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert "fields" in warnings[0].getMessage()
        assert str(status) in warnings[0].getMessage()
        assert all("fields" in call.request.params for call in rig.calls("daily_readiness"))

    def test_the_fallback_is_per_category(self, rig: Rig) -> None:
        rig.rsps.replace(responses.GET, api_url("daily_activity"), status=422)
        register_endpoint(rig.rsps, "daily_activity")
        rig.exporter.poll()
        assert "fields" in rig.calls("daily_readiness")[0].request.params
        assert "fields" not in rig.calls("daily_activity")[1].request.params

    def test_a_failing_retry_keeps_sending_fields(self, rig: Rig) -> None:
        rig.rsps.replace(responses.GET, api_url("daily_activity"), status=422)
        rig.rsps.add(responses.GET, api_url("daily_activity"), status=404, body="gone")
        rig.exporter.poll()
        assert rig.up("daily_activity") == 0
        assert rig.errors("daily_activity", "http_error") == 1
        rig.advance(300)
        rig.exporter.poll()
        calls = rig.calls("daily_activity")
        assert [("fields" in call.request.params) for call in calls][:3] == [True, False, True]

    @pytest.mark.parametrize("status", [403, 404, 418])
    def test_other_statuses_do_not_trigger_the_fallback(self, rig: Rig, status: int) -> None:
        rig.rsps.replace(responses.GET, api_url("daily_activity"), status=status)
        rig.exporter.poll()
        assert len(rig.calls("daily_activity")) == 1
        assert rig.up("daily_activity") == 0


class TestRunLoop:
    def test_polls_repeatedly_until_stopped(
        self, tmp_path: Any, rsps: responses.RequestsMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions: list[requests.Session] = []
        try:
            rig = build_rig(tmp_path, rsps, sessions)
            fast = Exporter(
                rig.exporter._client,
                rig.tokens,
                load_definitions(),
                0.01,
                today=lambda: TODAY,
            )
            persist_calls: list[int] = []
            monkeypatch.setattr(rig.tokens, "retry_persist", lambda: persist_calls.append(1))
            stop = threading.Event()
            thread = threading.Thread(target=fast.run, args=(stop,))
            thread.start()
            deadline = time.monotonic() + 10
            while len(persist_calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            thread.join(10)
            assert not thread.is_alive()
            assert len(persist_calls) >= 3
            assert fast.registry.get_sample_value("oura_daily_activity_score") == 78
        finally:
            for session in sessions:
                session.close()

    def test_run_survives_unexpected_errors_in_a_cycle(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stop = threading.Event()
        calls: list[int] = []

        def flaky(_stop: threading.Event | None = None) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("cycle bug")
            stop.set()

        monkeypatch.setattr(rig.exporter, "poll", flaky)
        monkeypatch.setattr(rig.exporter, "_poll_interval", 0)
        with caplog.at_level(logging.ERROR, logger="oura_exporter.exporter"):
            rig.exporter.run(stop)
        assert len(calls) == 2
        assert "poll cycle failed" in caplog.text

    def test_run_does_nothing_when_already_stopped(self, rig: Rig) -> None:
        stop = threading.Event()
        stop.set()
        rig.exporter.run(stop)
        assert len(rig.rsps.calls) == 0
