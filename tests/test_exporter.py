import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import requests
import responses
from prometheus_client import ProcessCollector

from oura_exporter import __version__
from oura_exporter import exporter as exporter_module
from oura_exporter.api import OuraApiError
from oura_exporter.definitions import load_definitions
from oura_exporter.exporter import Exporter
from oura_exporter.remote_write import RemoteWriter
from oura_exporter.storage import Token

from .helpers import (
    CATEGORY_ORDER,
    TOKEN_URL,
    WALL,
    WRITE_URL,
    Rig,
    api_url,
    build_rig,
    epoch,
    load_fixture,
    register_endpoint,
)

CATEGORIES = list(CATEGORY_ORDER)
SINGLES = {"personal_info", "ring_configuration"}


def error_reasons(rig: Rig, category: str) -> set[str]:
    return {
        sample.labels["reason"]
        for family in rig.exporter.registry.collect()
        if family.name == "oura_exporter_category_errors"
        for sample in family.samples
        if sample.name.endswith("_total") and sample.labels["category"] == category
    }


def process_metrics_available() -> bool:
    return bool(list(ProcessCollector(registry=None).collect()))


def poll_records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "oura_exporter.exporter" and record.levelno == level
    ]


def api_calls(rig: Rig) -> list[responses.Call]:
    return [call for call in rig.rsps.calls if call.request.method == "GET"]


def sent(rig: Rig) -> float | None:
    return rig.value("oura_exporter_remote_write_samples_total", result="sent")


@pytest.fixture
def decoupled(tmp_path: Any, rsps: responses.RequestsMock) -> Iterator[Rig]:
    sessions: list[requests.Session] = []
    yield build_rig(tmp_path, rsps, sessions, poll_interval=120, fetch_interval=600)
    for session in sessions:
        session.close()


def stamp(offset: float) -> int:
    return int((WALL + offset) * 1000)


class TestFullPoll:
    def test_polls_every_category_and_pushes_samples(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert all(rig.up(category) == 1 for category in CATEGORIES)
        assert len(rig.rsps.calls) == len(CATEGORIES) + len(rig.writes())
        assert rig.value("oura_exporter_auth_ok") == 1
        assert sent(rig) == rig.pushed_count() > 0
        assert rig.value("oura_exporter_remote_write_samples_total", result="rejected") == 0

    def test_data_is_not_exposed_on_metrics(self, rig: Rig) -> None:
        rig.exporter.poll()
        names = {family.name for family in rig.exporter.registry.collect()}
        assert not any(
            name.startswith("oura_") and not name.startswith("oura_exporter_") for name in names
        )

    def test_samples_carry_their_measurement_time(self, rig: Rig) -> None:
        rig.exporter.poll()
        heart = rig.pushed("oura_heartrate_bpm", job="oura-exporter")
        assert [(ts / 1000, value) for ts, value in heart] == [
            (epoch("2026-10-05T02:00:00+00:00"), 52.0),
            (epoch("2026-10-06T06:59:00+00:00"), 64.0),
            (epoch("2026-10-06T07:03:00+00:00"), 61.0),
            (epoch("2026-10-06T07:09:00+00:00"), 57.0),
        ]
        start = epoch("2026-10-05T23:12:41+00:00")
        hrv = rig.pushed("oura_sleep_hrv_milliseconds", sleep_type="long_sleep")
        assert (int((start + 300) * 1000), 47.0) in hrv
        score = rig.pushed("oura_daily_readiness_score")
        assert [(ts / 1000, value) for ts, value in score] == [
            (epoch("2026-10-04T23:59:59+00:00"), 74.0),
            (epoch("2026-10-05T23:59:59+00:00"), 80.0),
            (WALL, 86.0),
        ]

    def test_request_window_follows_the_lookback(self, rig: Rig) -> None:
        rig.exporter.poll()
        params = rig.calls("heartrate")[0].request.params
        assert params["start_datetime"] == "2026-10-03T16:00:00+00:00"
        assert params["end_datetime"] == "2026-10-06T16:00:00+00:00"
        daily = rig.calls("daily_readiness")[0].request.params
        assert daily["start_date"] == "2026-10-03"
        assert daily["end_date"] == "2026-10-07"
        assert "fields" in daily

    def test_single_documents_in_a_list_are_pushed_too(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert rig.pushed("oura_ring_size") == [(int(WALL * 1000), 9.0)]
        assert rig.pushed("oura_personal_info_age_years") == [(int(WALL * 1000), 34.0)]

    def test_self_metrics(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert rig.value("oura_exporter_build_info", version=__version__) == 1
        assert rig.value("oura_exporter_token_persisted") == 1
        assert rig.value("oura_exporter_remote_write_last_success_timestamp_seconds") == WALL
        for category in CATEGORIES:
            assert rig.value("oura_exporter_sample_revisions_total", category=category) == 0
        for category in CATEGORIES:
            assert (
                rig.value(
                    "oura_exporter_category_last_success_timestamp_seconds", category=category
                )
                == WALL
            )

    def test_an_empty_answer_counts_as_success(self, rig: Rig) -> None:
        for endpoint in ("workout", "daily_stress"):
            rig.rsps.replace(
                responses.GET, api_url(endpoint), json={"data": [], "next_token": None}
            )
        rig.exporter.poll()
        assert rig.up("workout") == 1
        assert rig.pushed("oura_daily_stress_day_summary") == []


class TestDeduplication:
    def test_a_second_poll_at_the_same_time_sends_nothing(self, rig: Rig) -> None:
        rig.exporter.poll()
        writes = len(rig.writes())
        total = sent(rig)
        rig.mono.advance(300)
        rig.exporter.poll()
        assert len(rig.writes()) == writes
        assert sent(rig) == total

    def test_later_polls_resend_only_fetch_time_samples(self, rig: Rig) -> None:
        rig.exporter.poll()
        writes = len(rig.writes())
        rig.advance(300)
        rig.exporter.poll()
        later = rig.writes()[writes:]
        timestamps = {ts for request in later for samples in request.values() for ts, _ in samples}
        assert timestamps == {int((WALL + 300) * 1000)}
        names = {name for request in later for name, _ in request}
        assert "oura_daily_readiness_score" in names
        assert "oura_heartrate_bpm" not in names
        assert "oura_sleep_heart_rate_bpm" not in names
        assert "oura_personal_info_age_years" in names

    def test_new_samples_appear_as_they_arrive(self, rig: Rig) -> None:
        rig.exporter.poll()
        payload = load_fixture("heartrate")
        payload["data"].append(
            {"bpm": 70, "source": "live", "timestamp": "2026-10-06T07:30:00+00:00"}
        )
        rig.rsps.replace(responses.GET, api_url("heartrate"), json=payload)
        rig.mono.advance(300)
        rig.exporter.poll()
        heart = rig.pushed("oura_heartrate_bpm")
        assert heart[-1] == (int(epoch("2026-10-06T07:30:00+00:00") * 1000), 70.0)
        assert len(heart) == 5

    def test_revisions_are_counted_not_sent(self, rig: Rig) -> None:
        rig.exporter.poll()
        payload = load_fixture("sleep")
        payload["data"][1]["total_sleep_duration"] = 26100
        rig.rsps.replace(responses.GET, api_url("sleep"), json=payload)
        rig.mono.advance(300)
        rig.exporter.poll()
        assert rig.value("oura_exporter_sample_revisions_total", category="sleep") == 1
        end = int(epoch("2026-10-06T06:58:41+00:00") * 1000)
        name = "oura_sleep_total_sleep_duration_seconds"
        assert (end, 26100.0) not in rig.pushed(name, sleep_type="long_sleep")
        assert (end, 26100.0) not in rig.stored(name, sleep_type="long_sleep")
        assert rig.value("oura_exporter_remote_write_samples_total", result="rejected") == 0
        rig.mono.advance(300)
        rig.exporter.poll()
        assert rig.value("oura_exporter_sample_revisions_total", category="sleep") == 1


class TestRemoteWriteResults:
    def test_a_rejected_batch_is_delivered_and_counted(self, rig: Rig) -> None:
        rig.rsps.replace(
            responses.POST, WRITE_URL, status=400, body="duplicate sample for timestamp"
        )
        rig.exporter.poll()
        assert rig.value("oura_exporter_remote_write_samples_total", result="rejected") > 0
        assert sent(rig) == 0
        writes = len(rig.writes())
        rig.mono.advance(300)
        rig.exporter.poll()
        assert len(rig.writes()) == writes
        assert all(rig.up(category) == 1 for category in CATEGORIES)

    def test_failed_pushes_are_retried_in_the_next_cycle(self, rig: Rig) -> None:
        rig.rsps.replace(responses.POST, WRITE_URL, status=503, body="down")
        rig.exporter.poll()
        assert rig.value("oura_exporter_remote_write_failures_total", reason="server_error") == len(
            CATEGORIES
        )
        assert sent(rig) == 0
        assert rig.value("oura_exporter_remote_write_last_success_timestamp_seconds") == 0
        assert all(rig.up(category) == 1 for category in CATEGORIES)

        rig.rsps.replace(responses.POST, WRITE_URL, status=204)
        rig.mono.advance(1)
        rig.exporter.poll()
        assert {(ts, value) for ts, value in rig.pushed("oura_heartrate_bpm")} >= {
            (int(epoch("2026-10-06T07:09:00+00:00") * 1000), 57.0)
        }
        assert len(rig.calls("personal_info")) == 1
        assert set(rig.pushed("oura_personal_info_age_years")) == {(int(WALL * 1000), 34.0)}
        assert rig.value("oura_exporter_remote_write_last_success_timestamp_seconds") == WALL

    def test_network_errors_count_as_failures(self, rig: Rig) -> None:
        rig.rsps.replace(responses.POST, WRITE_URL, body=requests.ConnectionError("down"))
        rig.exporter.poll()
        assert rig.value("oura_exporter_remote_write_failures_total", reason="network") == len(
            CATEGORIES
        )

    def test_health_series_exist_before_the_first_push(self, rig: Rig) -> None:
        assert sent(rig) == 0
        assert rig.value("oura_exporter_remote_write_samples_total", result="rejected") == 0


class TestSleepSettling:
    NAME = "oura_sleep_total_sleep_duration_seconds"

    def extend_nap(self, rig: Rig, end: str) -> None:
        payload = load_fixture("sleep")
        nap = payload["data"][2]
        nap["bedtime_end"] = end
        nap["total_sleep_duration"] = 3000
        rig.rsps.replace(responses.GET, api_url("sleep"), json=payload)

    def sync_until(self, rig: Rig, slots: int) -> None:
        payload = load_fixture("daily_activity")
        today = next(d for d in payload["data"] if d["day"] == "2026-10-06")
        today["class_5_min"] = "3" * slots
        rig.rsps.replace(responses.GET, api_url("daily_activity"), json=payload)

    def test_a_sleep_period_waits_for_a_ring_sync_after_its_end(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert rig.stored(self.NAME, sleep_type="late_nap") == []
        self.extend_nap(rig, "2026-10-06T15:10:00+00:00")
        rig.advance(3 * 3600 + 301)
        rig.exporter.poll()
        assert rig.stored(self.NAME, sleep_type="late_nap") == []
        self.sync_until(rig, 140)
        rig.advance(301)
        rig.exporter.poll()
        assert rig.stored(self.NAME, sleep_type="late_nap") == []
        rig.advance(301)
        rig.exporter.poll()
        assert rig.stored(self.NAME, sleep_type="late_nap") == [
            (int(epoch("2026-10-06T15:10:00+00:00") * 1000), 3000.0)
        ]
        assert rig.value("oura_exporter_remote_write_samples_total", result="rejected") == 0

    def test_the_first_cycle_holds_sleep_back_until_a_sync_is_known(self, rig: Rig) -> None:
        rig.exporter._synced_until = None
        rig.exporter.poll()
        assert rig.stored(self.NAME) == []
        assert rig.stored("oura_daily_activity_score")
        rig.advance(301)
        rig.exporter.poll()
        assert len(rig.stored(self.NAME)) == 2

    def test_the_known_sync_never_moves_back(self, rig: Rig) -> None:
        rig.exporter.poll()
        assert rig.exporter._synced_until == datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
        self.sync_until(rig, 150)
        rig.advance(301)
        rig.exporter.poll()
        assert rig.exporter._synced_until == datetime(2026, 10, 6, 16, 30, tzinfo=UTC)
        self.sync_until(rig, 20)
        rig.advance(301)
        rig.exporter.poll()
        assert rig.exporter._synced_until == datetime(2026, 10, 6, 16, 30, tzinfo=UTC)


class TestRestartAfterRevision:
    def test_a_conflicting_slot_does_not_cost_the_rest_of_the_window(
        self, tmp_path: Any, rsps: responses.RequestsMock
    ) -> None:
        sessions: list[requests.Session] = []
        try:
            rig = build_rig(tmp_path, rsps, sessions)
            rig.exporter.poll()
            activity = load_fixture("daily_activity")
            for document in activity["data"]:
                text = document["class_5_min"]
                document["class_5_min"] = text[:16] + "4" + text[17:]
            rsps.replace(responses.GET, api_url("daily_activity"), json=activity)
            heart = load_fixture("heartrate")
            heart["data"].append(
                {"bpm": 70, "source": "live", "timestamp": "2026-10-06T07:30:00+00:00"}
            )
            rsps.replace(responses.GET, api_url("heartrate"), json=heart)
            restarted = Exporter(
                rig.exporter._fetcher._client,
                rig.tokens,
                load_definitions(),
                RemoteWriter(sessions[-1], WRITE_URL, wall=rig.wall),
                300,
                300,
                3,
                monotonic=rig.mono,
                wall=rig.wall,
            )
            restarted.poll()
            registry = restarted.registry
            rejected = registry.get_sample_value(
                "oura_exporter_remote_write_samples_total", {"result": "rejected"}
            )
            assert rejected == 1
            assert rig.stored("oura_heartrate_bpm")[-1] == (
                int(epoch("2026-10-06T07:30:00+00:00") * 1000),
                70.0,
            )
            slots = rig.stored("oura_daily_activity_class_5_min")
            assert len(slots) == 3 * 19 - 2
            assert slots[-1][1] == 5.0
        finally:
            for session in sessions:
                session.close()


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
        first, second = CATEGORY_ORDER[:2]
        rig.rsps.replace(responses.GET, api_url(first), status=429, headers={"Retry-After": "120"})
        rig.exporter.poll()
        assert len(rig.rsps.calls) == 1
        assert rig.up(first) == 0
        assert rig.errors(first, "rate_limited") == 1
        assert rig.up(second) == 0

        rig.advance(60)
        rig.exporter.poll()
        assert len(rig.rsps.calls) == 1

        register_endpoint(rig.rsps, first, replace=True)
        rig.advance(61)
        rig.exporter.poll()
        assert len(rig.rsps.calls) - len(rig.writes()) == 1 + len(CATEGORIES)
        assert all(rig.up(category) == 1 for category in CATEGORIES)

    def test_poll_returns_when_asked_to_stop(self, rig: Rig) -> None:
        stop = threading.Event()
        stop.set()
        rig.exporter.poll(stop)
        assert len(rig.rsps.calls) == 0

    def test_poll_stops_between_categories(self, rig: Rig) -> None:
        stop = threading.Event()
        original = rig.exporter._fetcher.fetch

        def fetch_then_stop(*args: Any) -> Any:
            stop.set()
            return original(*args)

        rig.exporter._fetcher.fetch = fetch_then_stop  # type: ignore[method-assign]
        rig.exporter.poll(stop)
        assert len(rig.calls(CATEGORY_ORDER[0])) == 1
        assert rig.up(CATEGORY_ORDER[1]) is None


class TestFailureIsolation:
    def test_one_broken_category_does_not_stop_the_others(self, rig: Rig) -> None:
        broken = "daily_readiness"
        rig.rsps.replace(responses.GET, api_url(broken), body="<html>oops</html>")
        rig.exporter.poll()
        assert rig.up(broken) == 0
        assert rig.errors(broken, "invalid_response") == 1
        assert CATEGORY_ORDER.index(broken) < len(CATEGORY_ORDER) - 1
        assert all(rig.up(category) == 1 for category in CATEGORY_ORDER if category != broken)
        assert rig.pushed("oura_daily_readiness_score") == []
        assert rig.pushed("oura_daily_sleep_score") != []

    def test_network_errors_do_not_stop_the_other_categories(self, rig: Rig) -> None:
        failing = "daily_activity"
        rig.rsps.replace(responses.GET, api_url(failing), body=requests.ConnectionError("down"))
        rig.exporter.poll()
        assert rig.errors(failing, "network") == 1
        assert CATEGORY_ORDER.index(failing) < len(CATEGORY_ORDER) - 1
        assert all(rig.up(category) == 1 for category in CATEGORY_ORDER if category != failing)

    def test_unexpected_exceptions_are_logged_once_and_isolated(
        self,
        rig: Rig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        real = exporter_module.build_points
        failing = "daily_stress"

        def explode(category: Any, *args: Any, **kwargs: Any) -> Any:
            if category.name == failing:
                raise RuntimeError("bug")
            return real(category, *args, **kwargs)

        monkeypatch.setattr(exporter_module, "build_points", explode)
        with caplog.at_level(logging.DEBUG, logger="oura_exporter.exporter"):
            rig.exporter.poll()
            rig.advance(300)
            rig.exporter.poll()

        assert rig.errors(failing, "internal") == 2
        assert rig.up(failing) == 0
        assert CATEGORY_ORDER.index(failing) < len(CATEGORY_ORDER) - 1
        assert all(rig.up(category) == 1 for category in CATEGORY_ORDER if category != failing)
        errors = poll_records(caplog, logging.ERROR)
        assert len(errors) == 1
        assert errors[0].exc_info is not None
        assert "RuntimeError" in "".join(str(part) for part in errors[0].exc_info)
        assert len(poll_records(caplog, logging.WARNING)) == 0

    def test_authentication_failure_ends_the_cycle(self, rig: Rig) -> None:
        first, second = CATEGORY_ORDER[:2]
        assert rig.tokens.token is not None
        rig.tokens.token = Token(
            "old", "refresh-1", datetime.fromtimestamp(WALL - 5, tz=UTC), "cid"
        )
        rig.rsps.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 0
        assert rig.errors(first, "auth") == 1
        assert rig.up(first) == 0
        assert rig.up(second) == 0
        assert error_reasons(rig, second) == set()
        assert len(rig.calls(first)) == 0

        rig.advance(300)
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 0
        assert rig.errors(first, "auth") == 2
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
        first, second = CATEGORY_ORDER[:2]
        rig.rsps.replace(responses.GET, api_url(first), status=401)
        rig.rsps.post(
            TOKEN_URL,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 0
        assert rig.errors(first, "auth") == 1
        assert rig.up(second) == 0
        assert len(rig.calls(first)) == 2

        rig.advance(300)
        rig.exporter.poll()
        assert len(rig.calls(first)) == 3
        assert len(rig.rsps.calls) == 1 + 2 + 1

        register_endpoint(rig.rsps, first, replace=True)
        rig.advance(300)
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 1

    def test_rejected_token_after_a_fetched_category_only_fails_that_category(
        self, rig: Rig
    ) -> None:
        first, failing = CATEGORY_ORDER[:2]
        rig.rsps.replace(responses.GET, api_url(failing), status=401)
        rig.rsps.post(
            TOKEN_URL,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 1
        assert rig.up(first) == 1
        assert rig.up(failing) == 0
        assert error_reasons(rig, failing) == {"forbidden"}
        rest = CATEGORY_ORDER[2:]
        assert all(rig.up(name) == 1 for name in rest)


class TestAbortedCycles:
    def limit(self, rig: Rig, endpoint: str, retry_after: int = 120) -> None:
        rig.rsps.replace(
            responses.GET,
            api_url(endpoint),
            status=429,
            headers={"Retry-After": str(retry_after)},
        )

    def test_a_rate_limit_marks_every_due_category_down_quietly(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        first = CATEGORY_ORDER[0]
        self.limit(rig, first)
        with caplog.at_level(logging.DEBUG, logger="oura_exporter.exporter"):
            rig.exporter.poll()
        assert {category: rig.up(category) for category in CATEGORIES} == dict.fromkeys(
            CATEGORIES, 0
        )
        assert error_reasons(rig, first) == {"rate_limited"}
        for category in CATEGORY_ORDER[1:]:
            assert error_reasons(rig, category) == set()
            assert not any(
                record.getMessage().startswith(f"{category}:") for record in caplog.records
            )
        assert len(poll_records(caplog, logging.WARNING)) == 1
        assert poll_records(caplog, logging.INFO) == []

    def test_a_rate_limit_mid_cycle_leaves_categories_that_are_not_due_alone(
        self, rig: Rig
    ) -> None:
        fetched, limited, rest = CATEGORY_ORDER[:2], CATEGORY_ORDER[2], CATEGORY_ORDER[3:]
        assert set(rest) >= SINGLES
        rig.exporter.poll()
        rig.advance(300)
        self.limit(rig, limited)
        rig.exporter.poll()
        expected = dict.fromkeys(fetched, 1) | {limited: 0}
        expected |= {category: 1 if category in SINGLES else 0 for category in rest}
        assert {category: rig.up(category) for category in CATEGORIES} == expected
        assert error_reasons(rig, limited) == {"rate_limited"}
        assert error_reasons(rig, rest[0]) == set()

    def test_a_later_success_sets_every_category_up_again(self, rig: Rig) -> None:
        first = CATEGORY_ORDER[0]
        self.limit(rig, first)
        rig.exporter.poll()
        register_endpoint(rig.rsps, first, replace=True)
        rig.advance(121)
        rig.exporter.poll()
        assert all(rig.up(category) == 1 for category in CATEGORIES)

    def test_categories_that_become_due_during_a_pause_are_marked_down(self, rig: Rig) -> None:
        rig.exporter.poll()
        rig.advance(3300)
        self.limit(rig, CATEGORY_ORDER[0], retry_after=3600)
        rig.exporter.poll()
        assert rig.up("personal_info") == 1

        rig.advance(300)
        calls = len(api_calls(rig))
        rig.exporter.poll()
        assert len(api_calls(rig)) == calls
        assert rig.up("personal_info") == 0
        assert error_reasons(rig, "personal_info") == set()

    def test_an_authentication_failure_marks_the_rest_of_the_cycle_down(self, rig: Rig) -> None:
        fetched, failing, rest = CATEGORY_ORDER[:0], CATEGORY_ORDER[0], CATEGORY_ORDER[1:]
        assert set(rest) >= SINGLES
        rig.exporter.poll()
        rig.advance(300)
        rig.rsps.replace(responses.GET, api_url(failing), status=401)
        rig.rsps.post(
            TOKEN_URL,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        rig.exporter.poll()
        assert rig.value("oura_exporter_auth_ok") == 0
        expected = dict.fromkeys(fetched, 1) | {failing: 0}
        expected |= {category: 1 if category in SINGLES else 0 for category in rest}
        assert {category: rig.up(category) for category in CATEGORIES} == expected
        assert error_reasons(rig, failing) == {"auth"}
        assert error_reasons(rig, rest[0]) == set()


class TestStandardMetrics:
    def test_platform_info_is_exposed(self, rig: Rig) -> None:
        names = {family.name for family in rig.exporter.registry.collect()}
        assert "python_info" in names
        assert 'python_info{implementation="' in rig.text()

    @pytest.mark.skipif(
        not process_metrics_available(), reason="ProcessCollector yields nothing without /proc"
    )
    def test_process_metrics_are_exposed(self, rig: Rig) -> None:
        names = {family.name for family in rig.exporter.registry.collect()}
        assert {
            "process_resident_memory_bytes",
            "process_cpu_seconds",
            "process_start_time_seconds",
        } <= names
        assert (rig.value("process_resident_memory_bytes") or 0) > 0


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
        with caplog.at_level(logging.WARNING, logger="oura_exporter.fetching"):
            rig.exporter.poll()
            rig.advance(300)
            rig.exporter.poll()
            rig.advance(300)
            rig.exporter.poll()

        calls = rig.calls("daily_activity")
        assert [("fields" in call.request.params) for call in calls] == [True, False, False, False]
        assert rig.up("daily_activity") == 1
        assert rig.pushed("oura_daily_activity_score")[-1][1] == 78
        assert rig.errors("daily_activity", "http_error") is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
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


class TestChunking:
    def test_a_long_lookback_is_split_into_ranges(
        self, tmp_path: Any, rsps: responses.RequestsMock
    ) -> None:
        sessions: list[requests.Session] = []
        try:
            rig = build_rig(tmp_path, rsps, sessions)
            long = Exporter(
                rig.exporter._fetcher._client,
                rig.tokens,
                load_definitions(),
                RemoteWriter(sessions[-1], WRITE_URL),
                300,
                300,
                20,
                monotonic=rig.mono,
                wall=rig.wall,
            )
            long.poll()
            assert len(rig.calls("heartrate")) == 3
            assert len(rig.calls("daily_readiness")) == 1
            starts = [call.request.params["start_datetime"] for call in rig.calls("heartrate")]
            assert starts == sorted(starts)
        finally:
            for session in sessions:
                session.close()


class TestRunLoop:
    def test_polls_repeatedly_until_stopped(
        self, tmp_path: Any, rsps: responses.RequestsMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions: list[requests.Session] = []
        try:
            rig = build_rig(tmp_path, rsps, sessions)
            fast = Exporter(
                rig.exporter._fetcher._client,
                rig.tokens,
                load_definitions(),
                RemoteWriter(sessions[-1], WRITE_URL),
                0.01,
                0.01,
                3,
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
            assert (
                fast.registry.get_sample_value(
                    "oura_exporter_category_up", {"category": "daily_activity"}
                )
                == 1
            )
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


class TestDecoupledFetching:
    def test_fetches_follow_their_own_interval(self, decoupled: Rig) -> None:
        for _ in range(10):
            decoupled.exporter.poll()
            decoupled.advance(120)
        assert len(decoupled.calls("daily_readiness")) == 2
        assert len(decoupled.calls("personal_info")) == 1
        assert len(decoupled.calls("ring_configuration")) == 1
        assert decoupled.value("oura_exporter_category_fetches_total", category="sleep") == 2
        assert (
            decoupled.value("oura_exporter_category_fetches_total", category="personal_info") == 1
        )

    def test_fetch_counter_starts_at_zero_and_ignores_failures(
        self, decoupled: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for category in CATEGORIES:
            assert decoupled.value("oura_exporter_category_fetches_total", category=category) == 0
        decoupled.exporter.poll()
        assert all(
            decoupled.value("oura_exporter_category_fetches_total", category=category) == 1
            for category in CATEGORIES
        )
        decoupled.advance(600)
        monkeypatch.setattr(decoupled.exporter._fetcher, "fetch", self.failing)
        decoupled.exporter.poll()
        assert decoupled.value("oura_exporter_category_fetches_total", category="sleep") == 1
        assert decoupled.errors("sleep", "network") == 1

    @staticmethod
    def failing(*_args: Any, **_kwargs: Any) -> Any:
        raise OuraApiError("down", "network")

    def test_newest_daily_value_and_single_documents_are_pushed_every_cycle(
        self, decoupled: Rig
    ) -> None:
        for _ in range(5):
            decoupled.exporter.poll()
            decoupled.advance(120)
        expected = [(stamp(120 * i)) for i in range(5)]
        score = decoupled.pushed("oura_daily_readiness_score")
        assert [ts for ts, value in score if value == 86.0] == expected
        age = decoupled.pushed("oura_personal_info_age_years")
        assert [ts for ts, _ in age] == expected
        assert len(decoupled.pushed("oura_heartrate_bpm")) == 4

    def test_a_cache_older_than_three_fetch_intervals_is_not_pushed(
        self,
        decoupled: Rig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        decoupled.exporter.poll()
        real = decoupled.exporter._fetcher.fetch
        monkeypatch.setattr(decoupled.exporter._fetcher, "fetch", self.failing)
        with caplog.at_level(logging.INFO, logger="oura_exporter.exporter"):
            for _ in range(20):
                decoupled.advance(120)
                decoupled.exporter.poll()
            score = [ts for ts, _ in decoupled.pushed("oura_daily_readiness_score")]
            assert max(score) == stamp(1680)
            age = [ts for ts, _ in decoupled.pushed("oura_personal_info_age_years")]
            assert max(age) == stamp(2400)
            stale = [r for r in poll_records(caplog, logging.INFO) if "older than" in r.message]
            assert len(stale) == len(CATEGORIES) - len(SINGLES)
            assert decoupled.up("sleep") == 0

            monkeypatch.setattr(decoupled.exporter._fetcher, "fetch", real)
            decoupled.advance(120)
            decoupled.exporter.poll()
        assert max(ts for ts, _ in decoupled.pushed("oura_daily_readiness_score")) == stamp(2520)
        assert decoupled.up("sleep") == 1
        resumed = [r for r in poll_records(caplog, logging.INFO) if "pushing values" in r.message]
        assert len(resumed) == len(stale)

    def test_a_rate_limit_stops_fetching_but_not_pushing(self, decoupled: Rig) -> None:
        decoupled.exporter.poll()
        decoupled.advance(600)
        decoupled.rsps.replace(
            responses.GET,
            api_url(CATEGORY_ORDER[0]),
            status=429,
            headers={"Retry-After": "3600"},
        )
        decoupled.exporter.poll()
        assert stamp(600) in [ts for ts, _ in decoupled.pushed("oura_daily_readiness_score")]
        before = len(api_calls(decoupled))
        decoupled.advance(120)
        decoupled.exporter.poll()
        assert len(api_calls(decoupled)) == before
        assert stamp(720) in [ts for ts, _ in decoupled.pushed("oura_daily_readiness_score")]
        assert decoupled.up("daily_readiness") == 0

    def test_an_authentication_failure_stops_later_fetches_but_not_pushing(
        self, decoupled: Rig
    ) -> None:
        decoupled.exporter.poll()
        decoupled.advance(600)
        decoupled.rsps.replace(responses.GET, api_url(CATEGORY_ORDER[0]), status=401)
        decoupled.rsps.post(
            TOKEN_URL,
            json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        )
        decoupled.exporter.poll()
        assert decoupled.value("oura_exporter_auth_ok") == 0
        assert len(decoupled.calls("daily_readiness")) == 1
        assert stamp(600) in [ts for ts, _ in decoupled.pushed("oura_daily_readiness_score")]

    def test_settled_samples_are_pushed_from_the_cache_without_a_fetch(
        self, tmp_path: Any, rsps: responses.RequestsMock
    ) -> None:
        sessions: list[requests.Session] = []
        try:
            rig = build_rig(tmp_path, rsps, sessions, poll_interval=3600, fetch_interval=86400)
            rig.wall.now = epoch("2026-10-06T11:00:00+00:00")
            rig.exporter.poll()
            settled = int(epoch("2026-10-05T23:59:59+00:00") * 1000)
            assert (settled, 80.0) not in rig.pushed("oura_daily_readiness_score")
            rig.advance(3600)
            rig.exporter.poll()
            assert (settled, 80.0) in rig.pushed("oura_daily_readiness_score")
            assert len(rig.calls("daily_readiness")) == 1
        finally:
            for session in sessions:
                session.close()

    def test_unexpected_fetch_errors_are_logged_once_and_keep_the_cache(
        self,
        decoupled: Rig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        decoupled.exporter.poll()
        real = decoupled.exporter._fetcher.fetch

        def explode(category: Any, *args: Any, **kwargs: Any) -> Any:
            if category.name == "sleep":
                raise RuntimeError("bug")
            return real(category, *args, **kwargs)

        monkeypatch.setattr(decoupled.exporter._fetcher, "fetch", explode)
        with caplog.at_level(logging.DEBUG, logger="oura_exporter.exporter"):
            for _ in range(2):
                decoupled.advance(600)
                decoupled.exporter.poll()
        assert decoupled.errors("sleep", "internal") == 2
        assert len(poll_records(caplog, logging.ERROR)) == 1
        assert decoupled.up("sleep") == 0
        assert decoupled.up("daily_readiness") == 1

    def test_a_sample_building_error_clears_when_it_stops(
        self, decoupled: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = exporter_module.build_points
        broken = True

        def flaky(category: Any, *args: Any, **kwargs: Any) -> Any:
            if broken and category.name == "daily_stress":
                raise RuntimeError("bug")
            return real(category, *args, **kwargs)

        monkeypatch.setattr(exporter_module, "build_points", flaky)
        decoupled.exporter.poll()
        assert decoupled.up("daily_stress") == 0
        broken = False
        decoupled.advance(120)
        decoupled.exporter.poll()
        assert decoupled.up("daily_stress") == 1
