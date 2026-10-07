import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from oura_exporter.definitions import Category, load_definitions
from oura_exporter.points import JOB, DeliveryLog, Point, build_points, settled_at, to_ms

from .helpers import WALL, epoch, load_fixture

NOW = datetime.fromtimestamp(WALL, tz=UTC)
CUTOFF = NOW - timedelta(days=3)


def category(name: str) -> Category:
    return next(c for c in load_definitions() if c.name == name)


def documents(name: str) -> list[dict[str, Any]]:
    payload = load_fixture("vO2_max" if name == "vo2_max" else name)
    return payload.get("data", [payload])


def points(name: str, *, live: bool = True, now: datetime = NOW, **kwargs: Any) -> list[Point]:
    return build_points(category(name), documents(name), now, live=live, **kwargs)


def series(
    found: list[Point], name: str, since: float = 0.0, **labels: str
) -> list[tuple[float, float]]:
    return sorted(
        (point.timestamp_ms / 1000, point.value)
        for point in found
        if point.name == name
        and labels.items() <= dict(point.labels).items()
        and point.timestamp_ms / 1000 >= since
    )


def test_to_ms_rounds_to_milliseconds() -> None:
    assert to_ms(datetime(2026, 10, 6, 14, 0, 0, 1500, tzinfo=UTC)) == int(WALL * 1000) + 2


class TestSamples:
    def test_every_sample_sits_at_its_own_time(self) -> None:
        found = points("heartrate")
        assert series(found, "oura_heartrate_bpm") == [
            (epoch("2026-10-02T12:00:00+00:00"), 99.0),
            (epoch("2026-10-05T02:00:00+00:00"), 52.0),
            (epoch("2026-10-06T06:59:00+00:00"), 64.0),
            (epoch("2026-10-06T07:03:00+00:00"), 61.0),
            (epoch("2026-10-06T07:09:00+00:00"), 57.0),
        ]
        assert series(found, "oura_heartrate_source") == [
            (epoch("2026-10-02T12:00:00+00:00"), 5.0),
            (epoch("2026-10-05T02:00:00+00:00"), 3.0),
            (epoch("2026-10-06T06:59:00+00:00"), 1.0),
            (epoch("2026-10-06T07:03:00+00:00"), 1.0),
            (epoch("2026-10-06T07:09:00+00:00"), 2.0),
        ]

    def test_the_cutoff_drops_older_samples(self) -> None:
        found = points("heartrate", cutoff=CUTOFF)
        assert [value for _, value in series(found, "oura_heartrate_bpm")] == [
            52.0,
            64.0,
            61.0,
            57.0,
        ]

    def test_every_point_carries_the_job_label(self) -> None:
        for name in ("heartrate", "sleep", "daily_sleep", "personal_info"):
            assert all(dict(point.labels)["job"] == JOB for point in points(name))

    def test_booleans_become_numbers(self) -> None:
        found = points("ring_battery_level")
        assert {value for _, value in series(found, "oura_ring_battery_charging")} <= {0.0, 1.0}
        assert series(found, "oura_ring_battery_level_percent")[-1] == (
            epoch("2026-10-06T07:30:00+00:00"),
            74.0,
        )

    def test_samples_without_a_usable_time_are_skipped_with_one_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        docs = [{"bpm": 60, "timestamp": "garbage"}, {"bpm": 61}, {"bpm": 62, "timestamp": 5}]
        with caplog.at_level(logging.WARNING):
            found = build_points(category("heartrate"), docs, NOW, live=True)
        assert found == []
        assert len(caplog.records) == 3


class TestEvents:
    def test_sleep_scalars_sit_at_the_end_with_the_type_label(self) -> None:
        found = points("sleep")
        long_sleep = series(
            found, "oura_sleep_total_sleep_duration_seconds", sleep_type="long_sleep"
        )
        assert (epoch("2026-10-06T06:58:41+00:00"), 26040.0) in long_sleep
        nap = series(found, "oura_sleep_total_sleep_duration_seconds", sleep_type="late_nap")
        assert [timestamp for timestamp, _ in nap] == [epoch("2026-10-06T14:35:00+00:00")]
        assert len(series(found, "oura_sleep_total_sleep_duration_seconds")) == 4

    def test_embedded_series_start_at_their_own_timestamp(self) -> None:
        found = points("sleep")
        start = epoch("2026-10-05T23:12:41+00:00")
        assert series(found, "oura_sleep_heart_rate_bpm", start, sleep_type="long_sleep")[:3] == [
            (start, 56.0),
            (start + 300, 54.0),
            (start + 900, 53.0),
        ]
        assert (start + 600, 50.0) in series(found, "oura_sleep_hrv_milliseconds", start)

    def test_digit_strings_are_decoded_per_character(self) -> None:
        found = points("sleep")
        start = epoch("2026-10-05T23:12:41+00:00")
        assert series(found, "oura_sleep_phase_5_min", start, sleep_type="long_sleep")[:4] == [
            (start, 4.0),
            (start + 300, 2.0),
            (start + 600, 2.0),
            (start + 900, 3.0),
        ]
        assert series(found, "oura_sleep_phase_30_sec", start, sleep_type="long_sleep")[4] == (
            start + 120,
            3.0,
        )
        assert series(found, "oura_sleep_movement_30_sec", start, sleep_type="long_sleep")[3] == (
            start + 90,
            2.0,
        )

    def test_enums_and_nested_documents(self) -> None:
        found = points("sleep")
        end = epoch("2026-10-06T06:58:41+00:00")
        assert (end, 2.0) in series(found, "oura_sleep_sleep_algorithm_version")
        assert (end, 1.0) in series(found, "oura_sleep_sleep_analysis_reason")
        assert (end, 82.0) in series(found, "oura_sleep_readiness_score")
        assert (end, -0.25) in series(found, "oura_sleep_readiness_temperature_deviation_celsius")
        assert (end, 0.0) in series(found, "oura_sleep_low_battery_alert")

    def test_spans_get_a_duration(self) -> None:
        found = points("workout")
        assert series(found, "oura_workout_duration_seconds", activity="cycling") == [
            (epoch("2026-10-05T17:00:00+00:00"), 2700.0)
        ]
        labels = {
            point.labels for point in found if point.name == "oura_workout_calories_kilocalories"
        }
        assert (
            ("activity", "cycling"),
            ("intensity", "moderate"),
            ("job", JOB),
            ("source", "autodetected"),
        ) in labels
        assert not any("label" in dict(point.labels) for point in found)

    def test_null_labels_and_values_are_left_out(self) -> None:
        walking = [p for p in points("workout") if dict(p.labels).get("activity") == "walking"]
        assert {p.name for p in walking} == {
            "oura_workout_duration_seconds",
            "oura_workout_calories_kilocalories",
        }

    def test_tags_without_an_end_are_instants_and_rest_mode_waits_for_its_end(self) -> None:
        tags = points("enhanced_tag")
        assert series(tags, "oura_enhanced_tag_duration_seconds") == [
            (epoch("2026-10-05T19:00:00+00:00"), 0.0),
            (epoch("2026-10-06T10:00:00+00:00"), 5400.0),
        ]
        assert ("custom_name", "Sauna") in next(p.labels for p in tags if p.value == 5400.0)
        assert series(points("rest_mode_period"), "oura_rest_mode_duration_seconds") == [
            (epoch("2026-10-04T09:00:00+00:00"), 86400.0)
        ]

    def test_sessions(self) -> None:
        found = points("session")
        start = epoch("2026-10-05T20:00:00+00:00")
        assert series(found, "oura_session_heart_rate_bpm", session_type="meditation") == [
            (start, 62.0),
            (start + 5, 60.5),
            (start + 15, 59.0),
        ]
        assert series(found, "oura_session_heart_rate_variability_milliseconds") == [
            (start + 5, 45.0),
            (start + 10, 48.0),
        ]
        assert ("mood", "good") in next(
            p.labels for p in found if p.name == "oura_session_motion_count"
        )
        assert series(found, "oura_session_duration_seconds") == [
            (start, 600.0),
            (epoch("2026-10-06T09:00:00+00:00"), 300.0),
        ]

    def test_a_negative_span_is_dropped(self) -> None:
        docs = [
            {
                "start_datetime": "2026-10-06T10:00:00+00:00",
                "end_datetime": "2026-10-06T09:00:00+00:00",
            }
        ]
        assert build_points(category("session"), docs, NOW, live=True) == []

    def test_an_unparseable_end_is_dropped_with_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        docs = [{"start_datetime": "2026-10-06T10:00:00+00:00", "end_datetime": "later"}]
        with caplog.at_level(logging.WARNING):
            assert build_points(category("session"), docs, NOW, live=True) == []
        assert "oura_session_duration_seconds" in caplog.text

    def test_malformed_series_are_dropped_with_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        docs = [
            {
                "bedtime_end": "2026-10-06T07:00:00+00:00",
                "bedtime_start": "2026-10-05T23:00:00+00:00",
                "heart_rate": {
                    "interval": 0,
                    "items": [1.0],
                    "timestamp": "2026-10-05T23:00:00+00:00",
                },
                "hrv": {"interval": 300, "items": "nope", "timestamp": "2026-10-05T23:00:00+00:00"},
                "movement_30_sec": 5,
                "sleep_phase_5_min": "12",
                "app_sleep_phase_5_min": "x1y2",
                "sleep_phase_30_sec": "1",
            }
        ]
        with caplog.at_level(logging.WARNING):
            found = build_points(category("sleep"), docs, NOW, live=True)
        names = {point.name for point in found}
        assert "oura_sleep_heart_rate_bpm" not in names
        assert "oura_sleep_hrv_milliseconds" not in names
        assert "oura_sleep_movement_30_sec" not in names
        assert len(series(found, "oura_sleep_phase_5_min")) == 2
        assert [v for _, v in series(found, "oura_sleep_app_phase_5_min")] == [1.0, 2.0]
        assert caplog.text.count("ignoring unexpected series") == 3

    def test_a_string_series_without_a_start_is_dropped(self) -> None:
        docs = [{"bedtime_end": "2026-10-06T07:00:00+00:00", "sleep_phase_5_min": "1234"}]
        assert build_points(category("sleep"), docs, NOW, live=True) == []


class TestDaily:
    def test_the_newest_day_is_stamped_at_fetch_time_older_days_at_the_last_second(self) -> None:
        found = points("daily_readiness")
        assert series(found, "oura_daily_readiness_score") == [
            (epoch("2026-10-04T23:59:59+00:00"), 74.0),
            (epoch("2026-10-05T23:59:59+00:00"), 80.0),
            (WALL, 86.0),
        ]

    def test_documents_without_a_day_are_ignored(self) -> None:
        docs = [{"score": 1}, {"day": "x", "score": 2}, {"day": "2026-10-05", "score": 3}]
        found = build_points(category("daily_readiness"), docs, NOW, live=True)
        assert [(p.timestamp_ms, p.value) for p in found] == [(to_ms(NOW), 3.0)]

    def test_a_day_settles_twelve_hours_after_it_ends(self) -> None:
        docs = [{"day": "2026-10-05", "score": 70}, {"day": "2026-10-06", "score": 80}]
        before = datetime(2026, 10, 6, 11, 59, 59, tzinfo=UTC)
        at = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
        waiting = build_points(category("daily_readiness"), docs, before, live=True)
        assert [(p.timestamp_ms, p.value) for p in waiting] == [(to_ms(before), 80.0)]
        settled = build_points(category("daily_readiness"), docs, at, live=True)
        assert [(p.timestamp_ms / 1000, p.value) for p in settled] == [
            (epoch("2026-10-05T23:59:59+00:00"), 70.0),
            (at.timestamp(), 80.0),
        ]

    def test_settled_at(self) -> None:
        day = NOW.date() - timedelta(days=1)
        assert settled_at(day, NOW) == datetime(2026, 10, 5, 23, 59, 59, tzinfo=UTC)
        assert settled_at(NOW.date(), NOW) is None

    def test_local_time_decides_where_a_day_ends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        docs = [{"day": "2026-10-04", "score": 70}, {"day": "2026-10-05", "score": 80}]
        found = build_points(category("daily_readiness"), docs, NOW, live=True)
        assert found[0].timestamp_ms / 1000 == epoch("2026-10-04T21:59:59+00:00")

    def test_backfill_has_no_fetch_time_samples(self) -> None:
        found = points("daily_readiness", live=False)
        assert series(found, "oura_daily_readiness_score") == [
            (epoch("2026-10-04T23:59:59+00:00"), 74.0),
            (epoch("2026-10-05T23:59:59+00:00"), 80.0),
        ]
        later = points("daily_readiness", live=False, now=NOW + timedelta(days=1))
        assert len(series(later, "oura_daily_readiness_score")) == 3

    def test_series_inside_daily_documents_keep_their_measurement_time(self) -> None:
        found = points("daily_activity")
        start = epoch("2026-10-06T04:00:00+00:00")
        met = series(found, "oura_daily_activity_met")
        assert met[0][0] == epoch("2026-10-04T04:00:00+00:00")
        assert (start + 60, 1.0) in met
        classes = series(found, "oura_daily_activity_class_5_min")
        assert (start, 1.0) in classes
        assert (start + 300 * 18, 5.0) in classes

    def test_nested_values_and_mappings(self) -> None:
        found = points("daily_resilience")
        assert series(found, "oura_daily_resilience_level")[-1] == (WALL, 3.0)
        assert series(found, "oura_daily_resilience_contributors_stress")[-1][0] == WALL
        found = points("sleep_time")
        assert series(found, "oura_sleep_time_recommendation") == [
            (epoch("2026-10-05T23:59:59+00:00"), 2.0)
        ]
        assert series(found, "oura_sleep_time_status")[-1] == (WALL, 1.0)
        assert series(found, "oura_sleep_time_optimal_bedtime_start_offset_seconds") == [
            (epoch("2026-10-05T23:59:59+00:00"), -7200.0)
        ]

    def test_the_last_document_of_a_day_wins(self) -> None:
        docs = [{"day": "2026-10-06", "vo2_max": 40}, {"day": "2026-10-06", "vo2_max": 41}]
        found = build_points(category("vo2_max"), docs, NOW, live=True)
        assert [p.value for p in found] == [41.0]


class TestSingle:
    def test_profile_is_stamped_at_fetch_time(self) -> None:
        found = points("personal_info")
        assert {p.timestamp_ms for p in found} == {to_ms(NOW)}
        assert series(found, "oura_personal_info_age_years") == [(WALL, 34.0)]
        info = next(p for p in found if p.name == "oura_personal_info_biological_sex_info")
        assert info.value == 1.0
        assert dict(info.labels) == {"biological_sex": "male", "job": JOB}
        assert not any("email" in str(p) for p in found)

    def test_info_series_carry_the_document_labels(self) -> None:
        found = points("ring_configuration")
        color = next(p for p in found if p.name == "oura_ring_color_info")
        assert dict(color.labels) == {
            "color": "stealth_black",
            "job": JOB,
            "ring": "00000000-0000-4000-8000-000000001501",
        }
        assert series(found, "oura_ring_size") == [(WALL, 9.0)]

    def test_backfill_skips_single_documents(self) -> None:
        assert points("personal_info", live=False) == []


class TestValues:
    def test_unusable_values_are_dropped_with_one_warning_each(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        docs = [
            {"day": "2026-10-06", "score": "high", "contributors": {"timing": float("nan")}},
            {"day": "2026-10-05", "score": "high"},
        ]
        with caplog.at_level(logging.WARNING):
            warned: set[tuple[str, str]] = set()
            found = build_points(category("daily_sleep"), docs, NOW, live=True, warned=warned)
            build_points(category("daily_sleep"), docs, NOW, live=True, warned=warned)
        assert found == []
        assert caplog.text.count("oura_daily_sleep_score") == 1
        assert caplog.text.count("oura_daily_sleep_contributors_timing") == 1

    def test_unknown_enum_values_are_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        docs = [{"timestamp": "2026-10-06T07:00:00+00:00", "bpm": 60, "source": "teleport"}]
        with caplog.at_level(logging.WARNING):
            found = build_points(category("heartrate"), docs, NOW, live=True)
        assert [p.name for p in found] == ["oura_heartrate_bpm"]
        assert "teleport" in caplog.text

    def test_info_values_must_be_scalars(self) -> None:
        docs = [{"biological_sex": {"x": 1}, "age": 5}]
        found = build_points(category("personal_info"), docs, NOW, live=True)
        assert [p.name for p in found] == ["oura_personal_info_age_years"]

    def test_warning_memory_is_bounded(self) -> None:
        warned: set[tuple[str, str]] = {(str(i), "x") for i in range(1000)}
        build_points(
            category("daily_sleep"),
            [{"day": "2026-10-06", "score": "x"}],
            NOW,
            live=True,
            warned=warned,
        )
        assert len(warned) == 1000

    def test_odd_documents_are_tolerated(self) -> None:
        docs = [
            {"day": "2026-10-06", "score": 10**400, "contributors": 5},
            {"day": "2026-10-05", "contributors": {"timing": 3}},
        ]
        found = build_points(category("daily_sleep"), docs, NOW, live=True)
        assert [p.name for p in found] == ["oura_daily_sleep_contributors_timing"]

    def test_naive_timestamps_are_read_as_utc(self) -> None:
        docs = [{"bpm": 60, "timestamp": "2026-10-06T07:00:00"}]
        found = build_points(category("heartrate"), docs, NOW, live=True)
        assert found[0].timestamp_ms / 1000 == epoch("2026-10-06T07:00:00+00:00")

    def test_a_samples_series_must_be_an_object(self, caplog: pytest.LogCaptureFixture) -> None:
        docs = [{"bedtime_end": "2026-10-06T07:00:00+00:00", "heart_rate": [1, 2]}]
        with caplog.at_level(logging.WARNING):
            assert build_points(category("sleep"), docs, NOW, live=True) == []
        assert "oura_sleep_heart_rate_bpm" in caplog.text

    def test_non_string_labels_are_ignored(self) -> None:
        docs = [{"start_datetime": "2026-10-06T10:00:00+00:00", "activity": 5, "calories": 1}]
        found = build_points(category("workout"), docs, NOW, live=True)
        assert all("activity" not in dict(p.labels) for p in found)


class TestDeliveryLog:
    def point(self, value: float, timestamp: int = 1000, name: str = "m") -> Point:
        return Point(name, (("job", JOB),), timestamp, value)

    def test_only_new_or_changed_samples_are_fresh(self) -> None:
        log = DeliveryLog()
        first = [self.point(1.0), self.point(2.0, 2000)]
        assert log.fresh(first) == first
        assert log.record(first) == 0
        assert len(log) == 2
        assert log.fresh(first) == []
        assert log.fresh([self.point(5.0)]) == [self.point(5.0)]
        assert log.fresh([self.point(1.0, 3000)]) == [self.point(1.0, 3000)]

    def test_nothing_is_remembered_until_recorded(self) -> None:
        log = DeliveryLog()
        assert log.fresh([self.point(1.0)]) == [self.point(1.0)]
        assert log.fresh([self.point(1.0)]) == [self.point(1.0)]

    def test_duplicates_in_one_batch_collapse_to_the_last(self) -> None:
        log = DeliveryLog()
        assert log.fresh([self.point(1.0), self.point(2.0)]) == [self.point(2.0)]

    def test_revisions_are_counted_and_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = DeliveryLog()
        log.record([self.point(1.0)])
        with caplog.at_level(logging.DEBUG):
            assert log.record([self.point(2.0)]) == 1
        assert "revised" in caplog.text
        assert log.fresh([self.point(2.0)]) == []

    def test_prune_keeps_the_window(self) -> None:
        log = DeliveryLog()
        log.record([self.point(1.0, 1000), self.point(2.0, 5000)])
        log.prune(datetime.fromtimestamp(3.0, tz=UTC))
        assert len(log) == 1
        assert log.fresh([self.point(1.0, 1000)]) == [self.point(1.0, 1000)]
