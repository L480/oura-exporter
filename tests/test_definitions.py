import copy
import logging
import re
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from oura_exporter.config import ConfigError
from oura_exporter.definitions import (
    MAX_WARNED,
    Category,
    Snapshot,
    build_snapshot,
    extract_values,
    load_definitions,
    parse_definitions,
)

from .helpers import TODAY, epoch, load_fixture

NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
CATEGORY_NAMES = [
    "daily_activity",
    "daily_readiness",
    "daily_resilience",
    "daily_sleep",
    "daily_spo2",
    "daily_stress",
    "sleep",
    "heartrate",
    "ring_battery_level",
    "personal_info",
]


def make_category(
    metrics: list[dict[str, Any]] | None = None, kind: str = "daily", **extra: Any
) -> Category:
    raw = {
        "name": "alpha",
        "kind": kind,
        "prefix": "oura_alpha_",
        "metrics": metrics or [{"name": "v", "help": "Value."}],
        **extra,
    }
    return parse_definitions({"categories": [raw]}, "test")[0]


def valid() -> dict[str, Any]:
    return {
        "categories": [
            {
                "name": "alpha",
                "kind": "daily",
                "prefix": "oura_alpha_",
                "metrics": [{"name": "score", "help": "Score."}],
            }
        ]
    }


def by_name(name: str) -> Category:
    return next(category for category in load_definitions() if category.name == name)


class TestPackagedDefinitions:
    def test_categories_and_kinds(self) -> None:
        categories = load_definitions()
        assert [category.name for category in categories] == CATEGORY_NAMES
        kinds = {category.name: category.kind for category in categories}
        assert {name: kinds[name] for name in ("heartrate", "ring_battery_level")} == {
            "heartrate": "latest",
            "ring_battery_level": "latest",
        }
        assert kinds["personal_info"] == "single"
        assert all(kinds[name] == "daily" for name in CATEGORY_NAMES[:7])

    def test_every_exposed_name_is_unique_and_valid(self) -> None:
        exposed: list[str] = []
        for category in load_definitions():
            assert category.prefix.endswith("_")
            assert not category.prefix.startswith("oura_exporter_")
            for metric in category.metrics:
                assert NAME_PATTERN.match(metric.full_name)
                assert metric.help.strip()
                exposed.append(metric.exposed_name)
            if category.timestamp_name:
                exposed.append(category.timestamp_name)
        assert len(exposed) == len(set(exposed))

    def test_every_path_is_unique_within_its_category(self) -> None:
        for category in load_definitions():
            paths = [metric.path for metric in category.metrics]
            assert len(paths) == len(set(paths)), category.name

    def test_no_email_and_no_labels(self) -> None:
        for category in load_definitions():
            for metric in category.metrics:
                assert "email" not in metric.full_name
                assert "email" not in metric.path

    def test_special_metrics(self) -> None:
        sleep = by_name("sleep")
        assert dict(sleep.select) == {"type": "long_sleep"}
        assert sleep.sort_by == "bedtime_end"
        personal = by_name("personal_info")
        assert personal.refresh_interval == 3600
        assert [metric.type for metric in personal.metrics] == ["gauge", "gauge", "gauge", "info"]
        resilience = {metric.name: metric for metric in by_name("daily_resilience").metrics}
        assert resilience["level"].mapping == {
            "limited": 1.0,
            "adequate": 2.0,
            "solid": 3.0,
            "strong": 4.0,
            "exceptional": 5.0,
        }
        source = {metric.name: metric for metric in by_name("heartrate").metrics}["source"]
        assert source.type == "enum"
        assert set(source.states) == {"awake", "rest", "sleep", "session", "live", "workout"}

    def test_every_metric_is_exported_for_complete_documents(self) -> None:
        for category in load_definitions():
            payload = load_fixture(category.name)
            documents = [payload] if category.kind == "single" else payload["data"]
            snapshot = build_snapshot(category, documents)
            missing = {metric.name for metric in category.metrics} - set(snapshot.values)
            assert not missing, (category.name, missing)


class TestTitleAndSummary:
    @pytest.mark.parametrize(
        ("name", "kind", "title", "summary"),
        [
            ("alpha", "daily", "Alpha", "latest day"),
            ("daily_sleep", "daily", "Daily sleep", "latest day"),
            ("ring_battery_level", "latest", "Ring battery level", "most recent sample"),
            ("personal_info", "single", "Personal info", "profile"),
        ],
    )
    def test_defaults(self, name: str, kind: str, title: str, summary: str) -> None:
        raw = {
            "name": name,
            "kind": kind,
            "prefix": "oura_x_",
            "metrics": [{"name": "v", "help": "Value."}],
        }
        parsed = parse_definitions({"categories": [raw]}, "test")[0]
        assert parsed.title == title
        assert parsed.summary == summary

    def test_explicit_values_win(self) -> None:
        parsed = make_category(title="Custom title", summary="every full moon")
        assert parsed.title == "Custom title"
        assert parsed.summary == "every full moon"

    @pytest.mark.parametrize(
        ("name", "title", "summary"),
        [
            ("daily_activity", "Activity", "latest day"),
            ("daily_readiness", "Readiness", "latest day"),
            ("daily_resilience", "Resilience", "latest day"),
            ("daily_sleep", "Sleep score", "latest day"),
            ("daily_spo2", "SpO2", "latest night"),
            ("daily_stress", "Stress", "latest day"),
            ("sleep", "Sleep", "main sleep of the latest night"),
            ("heartrate", "Heart rate", "most recent sample"),
            ("ring_battery_level", "Ring battery", "most recent sample"),
            ("personal_info", "Profile", "refreshed hourly"),
        ],
    )
    def test_packaged_categories(self, name: str, title: str, summary: str) -> None:
        packaged = by_name(name)
        assert packaged.title == title
        assert packaged.summary == summary


class TestRequestParameters:
    def test_daily(self) -> None:
        params = by_name("daily_resilience").params(TODAY)
        assert params == {
            "start_date": "2026-09-29",
            "end_date": "2026-10-07",
            "fields": "contributors,day,level",
        }
        assert by_name("daily_resilience").params(TODAY, with_fields=False) == {
            "start_date": "2026-09-29",
            "end_date": "2026-10-07",
        }

    def test_daily_with_select_and_sort_by(self) -> None:
        fields = by_name("sleep").params(TODAY)["fields"].split(",")
        assert fields == sorted(fields)
        assert {"day", "type", "bedtime_end", "bedtime_start", "total_sleep_duration"} <= set(
            fields
        )

    def test_dotted_select_and_sort_keys_contribute_their_first_segment(self) -> None:
        category = make_category(
            [{"name": "v", "help": "V", "path": "m.n"}],
            select={"a.b": 1},
            sort_by="c.d",
        )
        assert category.fields == ("a", "c", "day", "m")

    def test_end_date_is_tomorrow(self) -> None:
        params = make_category().params(date(2026, 12, 31))
        assert params["start_date"] == "2026-12-24"
        assert params["end_date"] == "2027-01-01"

    def test_latest(self) -> None:
        assert by_name("heartrate").params(TODAY) == {
            "latest": "true",
            "fields": "bpm,source,timestamp",
        }
        assert by_name("ring_battery_level").params(TODAY, with_fields=False) == {"latest": "true"}

    def test_single_documents_list_only_metric_fields(self) -> None:
        assert by_name("personal_info").fields == ("age", "biological_sex", "height", "weight")

    def test_single_has_no_parameters(self) -> None:
        assert by_name("personal_info").params(TODAY) == {}
        assert by_name("personal_info").params(TODAY, with_fields=False) == {}


class TestSelection:
    def test_unsorted_documents_pick_the_latest_day(self) -> None:
        documents = load_fixture("daily_readiness")["data"]
        assert [doc["day"] for doc in documents] != sorted(doc["day"] for doc in documents)
        snapshot = build_snapshot(by_name("daily_readiness"), documents)
        assert snapshot.values["score"] == 86.0
        assert snapshot.timestamp == epoch("2026-10-06T00:00:00+00:00")

    def test_select_and_sort_by_choose_the_main_sleep(self) -> None:
        documents = load_fixture("sleep")["data"]
        snapshot = build_snapshot(by_name("sleep"), documents)
        assert snapshot.values["total_sleep_duration_seconds"] == 26040.0
        assert snapshot.values["bedtime_end_timestamp_seconds"] == epoch(
            "2026-10-06T06:58:41+00:00"
        )
        assert snapshot.values["bedtime_start_timestamp_seconds"] == epoch(
            "2026-10-05T23:12:41+00:00"
        )
        assert snapshot.timestamp == epoch("2026-10-06T00:00:00+00:00")

    def test_documents_without_a_parseable_day_are_ignored(self) -> None:
        documents = [
            {"day": None, "v": 1},
            {"day": "garbage", "v": 2},
            {"day": 5, "v": 3},
            {"v": 4},
            {"day": "2026-10-01T00:00:00", "v": 5},
            {"day": "2026-10-01", "v": 6},
        ]
        snapshot = build_snapshot(make_category(), documents)
        assert snapshot.values == {"v": 6.0}

    def test_nothing_usable_gives_an_empty_snapshot(self) -> None:
        assert build_snapshot(make_category(), [{"day": "nope", "v": 1}]) == Snapshot()
        assert build_snapshot(make_category(), []) == Snapshot()
        assert Snapshot().timestamp is None
        assert dict(Snapshot().values) == {}

    def test_select_without_a_match_gives_an_empty_snapshot(self) -> None:
        documents = [{"day": "2026-10-06", "type": "late_nap", "v": 1}]
        category = make_category(select={"type": "long_sleep"})
        assert build_snapshot(category, documents) == Snapshot()

    def test_sort_by_breaks_ties_between_documents_of_the_same_day(self) -> None:
        category = make_category(sort_by="end")
        documents = [
            {"day": "2026-10-06", "end": "2026-10-06T08:00:00+00:00", "v": 1},
            {"day": "2026-10-06", "end": "2026-10-06T06:00:00+00:00", "v": 2},
            {"day": "2026-10-06", "end": "not a time", "v": 3},
            {"day": "2026-10-06", "v": 4},
        ]
        assert build_snapshot(category, documents).values == {"v": 1.0}

    def test_the_day_wins_over_sort_by(self) -> None:
        category = make_category(sort_by="end")
        documents = [
            {"day": "2026-10-06", "end": "2026-10-06T01:00:00+00:00", "v": 1},
            {"day": "2026-10-05", "end": "2026-10-09T01:00:00+00:00", "v": 2},
        ]
        assert build_snapshot(category, documents).values == {"v": 1.0}

    def test_complete_ties_prefer_the_last_document(self) -> None:
        documents = [{"day": "2026-10-06", "v": 1}, {"day": "2026-10-06", "v": 2}]
        assert build_snapshot(make_category(), documents).values == {"v": 2.0}

    def test_day_timestamp_is_local_midnight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        snapshot = build_snapshot(make_category(), [{"day": "2026-10-06", "v": 1}])
        assert snapshot.timestamp == epoch("2026-10-05T22:00:00+00:00")

    def test_latest_picks_the_newest_sample(self) -> None:
        documents = load_fixture("heartrate")["data"]
        snapshot = build_snapshot(by_name("heartrate"), documents)
        assert snapshot.values == {"bpm": 57.0, "source": "rest"}
        assert snapshot.timestamp == epoch("2026-10-06T07:09:00+00:00")

    def test_latest_treats_naive_timestamps_as_utc_and_skips_garbage(self) -> None:
        category = make_category([{"name": "v", "help": "V", "path": "bpm"}], kind="latest")
        documents = [
            {"timestamp": "2026-10-06T07:00:00", "bpm": 1},
            {"timestamp": "2026-10-06T07:01:00+00:00", "bpm": 2},
            {"timestamp": "garbage", "bpm": 3},
            {"timestamp": None, "bpm": 4},
            {"bpm": 5},
        ]
        snapshot = build_snapshot(category, documents)
        assert snapshot.values == {"v": 2.0}
        assert snapshot.timestamp == epoch("2026-10-06T07:01:00+00:00")

    def test_latest_without_samples(self) -> None:
        category = make_category(kind="latest")
        assert build_snapshot(category, []) == Snapshot()
        assert build_snapshot(category, [{"timestamp": "bad"}]) == Snapshot()

    def test_single_uses_the_document_and_has_no_timestamp(self) -> None:
        document = load_fixture("personal_info")
        snapshot = build_snapshot(by_name("personal_info"), [document])
        assert snapshot.timestamp is None
        assert snapshot.values == {
            "age_years": 34.0,
            "weight_kilograms": 78.5,
            "height_meters": 1.82,
            "biological_sex": "male",
        }
        assert build_snapshot(by_name("personal_info"), []) == Snapshot()


class TestExtraction:
    @pytest.mark.parametrize(
        ("document", "expected"),
        [
            ({"a": {"b": 5}}, {"v": 5.0}),
            ({"a": {"b": 5.25}}, {"v": 5.25}),
            ({"a": {"b": 0}}, {"v": 0.0}),
            ({"a": {"b": True}}, {"v": 1.0}),
            ({"a": {"b": False}}, {"v": 0.0}),
            ({"a": {"b": None}}, {}),
            ({"a": None}, {}),
            ({"a": {}}, {}),
            ({}, {}),
            ({"a": "text"}, {}),
            ({"a": [1]}, {}),
        ],
    )
    def test_gauge_paths(self, document: dict[str, Any], expected: dict[str, float]) -> None:
        category = make_category([{"name": "v", "help": "V", "path": "a.b"}])
        assert extract_values(category, document) == expected

    @pytest.mark.parametrize("bad", ["7", [1], {"x": 1}, float("nan"), float("inf"), 10**400])
    def test_unusable_gauge_values_are_dropped_with_a_warning(
        self, bad: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        category = make_category([{"name": "v", "help": "V"}])
        with caplog.at_level(logging.WARNING):
            assert extract_values(category, {"v": bad}) == {}
        assert "oura_alpha_v" in caplog.text
        assert "ignoring unexpected value" in caplog.text

    def test_warnings_are_logged_once_per_metric_and_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        category = make_category([{"name": "v", "help": "V"}, {"name": "w", "help": "W"}])
        warned: set[tuple[str, str]] = set()
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                extract_values(category, {"v": "bad", "w": "bad"}, warned)
            assert len(caplog.records) == 2
            extract_values(category, {"v": "other"}, warned)
        assert len(caplog.records) == 3
        assert len(warned) == 3

    def test_warning_memory_is_bounded(self, caplog: pytest.LogCaptureFixture) -> None:
        category = make_category([{"name": "v", "help": "V"}])
        warned = {("x", str(number)) for number in range(MAX_WARNED)}
        with caplog.at_level(logging.WARNING):
            extract_values(category, {"v": "bad"}, warned)
        assert caplog.text == ""
        assert len(warned) == MAX_WARNED

    def test_mapping(self, caplog: pytest.LogCaptureFixture) -> None:
        category = make_category([{"name": "v", "help": "V", "mapping": {"low": 1, "high": 2.5}}])
        assert extract_values(category, {"v": "low"}) == {"v": 1.0}
        assert extract_values(category, {"v": "high"}) == {"v": 2.5}
        assert extract_values(category, {"v": None}) == {}
        with caplog.at_level(logging.WARNING):
            assert extract_values(category, {"v": "other"}) == {}
            assert extract_values(category, {"v": 1}) == {}
        assert len(caplog.records) == 2

    def test_enum(self, caplog: pytest.LogCaptureFixture) -> None:
        category = make_category([{"name": "v", "help": "V", "type": "enum", "states": ["a", "b"]}])
        assert extract_values(category, {"v": "a"}) == {"v": "a"}
        assert extract_values(category, {"v": None}) == {}
        warned: set[tuple[str, str]] = set()
        with caplog.at_level(logging.WARNING):
            assert extract_values(category, {"v": "c"}, warned) == {}
            assert extract_values(category, {"v": "c"}, warned) == {}
            assert extract_values(category, {"v": 5}, warned) == {}
        assert len(caplog.records) == 2

    def test_info(self) -> None:
        category = make_category([{"name": "v", "help": "V", "type": "info"}])
        assert extract_values(category, {"v": "male"}) == {"v": "male"}
        assert extract_values(category, {"v": 5}) == {"v": "5"}
        assert extract_values(category, {"v": None}) == {}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-10-06T06:58:41+02:00", epoch("2026-10-06T04:58:41+00:00")),
            ("2026-10-06T06:58:41Z", epoch("2026-10-06T06:58:41+00:00")),
            ("2026-10-06T06:58:41", epoch("2026-10-06T06:58:41+00:00")),
            ("2026-10-06T06:58:41.250000+00:00", epoch("2026-10-06T06:58:41.25+00:00")),
        ],
    )
    def test_timestamp_transform(self, raw: str, expected: float) -> None:
        category = make_category([{"name": "v", "help": "V", "transform": "timestamp"}])
        assert extract_values(category, {"v": raw}) == {"v": expected}

    @pytest.mark.parametrize("bad", ["yesterday", "", 1759734300, ["2026-10-06"]])
    def test_invalid_timestamps_are_dropped_with_a_warning(
        self, bad: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        category = make_category([{"name": "v", "help": "V", "transform": "timestamp"}])
        with caplog.at_level(logging.WARNING):
            assert extract_values(category, {"v": bad}) == {}
        assert "ignoring unexpected value" in caplog.text

    def test_nested_documents_with_nulls_remove_series(self) -> None:
        category = by_name("daily_activity")
        full = build_snapshot(category, load_fixture("daily_activity")["data"])
        nulls = build_snapshot(category, load_fixture("daily_activity_nulls")["data"])
        assert "score" in full.values
        assert "contributors_stay_active" in full.values
        assert "score" not in nulls.values
        assert not [name for name in nulls.values if name.startswith("contributors_")]
        assert nulls.values["steps"] == 8731.0


class TestValidation:
    def test_minimal_definition_is_valid(self) -> None:
        category = parse_definitions(valid(), "test")[0]
        assert category.endpoint == "alpha"
        assert category.metrics[0].full_name == "oura_alpha_score"
        assert category.metrics[0].path == ("score",)
        assert category.metrics[0].type == "gauge"
        assert category.refresh_interval is None

    def test_default_endpoint_can_be_overridden(self) -> None:
        raw = valid()
        raw["categories"][0]["endpoint"] = "vO2_max"
        assert parse_definitions(raw, "test")[0].endpoint == "vO2_max"

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda d: d.update(extra=1), "unknown key.*extra"),
            (lambda d: d.update(categories=[]), "'categories' must be a non-empty list"),
            (lambda d: d.update(categories="x"), "'categories' must be a non-empty list"),
            (lambda d: d["categories"][0].update(foo=1), "unknown key.*foo"),
            (lambda d: d["categories"][0].update(title=""), "'title' must be a non-empty string"),
            (lambda d: d["categories"][0].update(title="  "), "'title' must be a non-empty string"),
            (lambda d: d["categories"][0].update(title=5), "'title' must be a non-empty string"),
            (lambda d: d["categories"][0].update(summary=""), "'summary' must be a non-empty"),
            (lambda d: d["categories"][0].update(summary=["x"]), "'summary' must be a non-empty"),
            (lambda d: d["categories"].__setitem__(0, "text"), "expected a mapping"),
            (lambda d: d["categories"][0].pop("name"), "'name' must be a non-empty string"),
            (lambda d: d["categories"][0].update(name="1abc"), "'name' must match"),
            (lambda d: d["categories"][0].update(name="a-b"), "'name' must match"),
            (lambda d: d["categories"][0].update(name=5), "'name' must be a non-empty string"),
            (
                lambda d: d["categories"].append(copy.deepcopy(d["categories"][0])),
                "duplicate category",
            ),
            (lambda d: d["categories"][0].pop("kind"), "'kind' must be one of"),
            (lambda d: d["categories"][0].update(kind="weekly"), "'kind' must be one of"),
            (lambda d: d["categories"][0].update(endpoint="a/b"), "'endpoint' must match"),
            (lambda d: d["categories"][0].update(endpoint=""), "'endpoint' must be a non-empty"),
            (lambda d: d["categories"][0].pop("prefix"), "'prefix' must be a non-empty string"),
            (lambda d: d["categories"][0].update(prefix="oura-alpha-"), "'prefix' must match"),
            (lambda d: d["categories"][0].update(prefix="oura_exporter_x_"), "reserved"),
            (lambda d: d["categories"][0].update(refresh_interval=0), "refresh_interval"),
            (lambda d: d["categories"][0].update(refresh_interval=-5), "refresh_interval"),
            (lambda d: d["categories"][0].update(refresh_interval="60"), "refresh_interval"),
            (lambda d: d["categories"][0].update(refresh_interval=True), "refresh_interval"),
            (lambda d: d["categories"][0].update(refresh_interval=1.5), "refresh_interval"),
            (lambda d: d["categories"][0].update(select=["x"]), "'select'.*mapping"),
            (lambda d: d["categories"][0].update(select={"a": [1]}), "'select' values"),
            (lambda d: d["categories"][0].update(sort_by=""), "'sort_by'"),
            (lambda d: d["categories"][0].update(sort_by=5), "'sort_by'"),
            (
                lambda d: d["categories"][0].update(kind="latest", sort_by="x"),
                "only valid for kind 'daily'",
            ),
            (
                lambda d: d["categories"][0].update(kind="single", select={"a": 1}),
                "only valid for kind 'daily'",
            ),
            (lambda d: d["categories"][0].pop("metrics"), "'metrics' must be a non-empty list"),
            (lambda d: d["categories"][0].update(metrics=[]), "'metrics' must be a non-empty list"),
            (lambda d: d["categories"][0]["metrics"][0].update(foo=1), "unknown key.*foo"),
            (lambda d: d["categories"][0]["metrics"].__setitem__(0, "text"), "expected a mapping"),
            (lambda d: d["categories"][0]["metrics"][0].pop("name"), "'name' must be a non-empty"),
            (
                lambda d: d["categories"][0]["metrics"][0].update(name="has space"),
                "'name' must match",
            ),
            (lambda d: d["categories"][0]["metrics"][0].pop("help"), "'help' must be a non-empty"),
            (
                lambda d: d["categories"][0]["metrics"][0].update(help="  "),
                "'help' must be a non-empty",
            ),
            (lambda d: d["categories"][0]["metrics"][0].update(path="a..b"), "empty segment"),
            (lambda d: d["categories"][0]["metrics"][0].update(path=".a"), "empty segment"),
            (
                lambda d: d["categories"][0]["metrics"][0].update(path=5),
                "'path' must be a non-empty",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(type="counter"),
                "'type' must be one of",
            ),
            (lambda d: d["categories"][0]["metrics"][0].update(type="enum"), "'states' must be"),
            (
                lambda d: d["categories"][0]["metrics"][0].update(type="enum", states=[]),
                "'states' must be",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(type="enum", states=["a", "a"]),
                "'states' must be",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(type="enum", states=["a", 1]),
                "'states' must be",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(states=["a"]),
                "'states' is only valid",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(type="info", states=["a"]),
                "'states' is only valid",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(
                    type="enum", states=["a"], mapping={"a": 1}
                ),
                "only valid for gauges",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(
                    type="info", transform="timestamp"
                ),
                "only valid for gauges",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(
                    mapping={"a": 1}, transform="timestamp"
                ),
                "mutually exclusive",
            ),
            (lambda d: d["categories"][0]["metrics"][0].update(mapping={}), "'mapping' must map"),
            (
                lambda d: d["categories"][0]["metrics"][0].update(mapping={"a": "1"}),
                "'mapping' must map",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(mapping={"a": True}),
                "'mapping' must map",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(mapping={"a": float("nan")}),
                "'mapping' must map",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(mapping=["a"]),
                "expected a mapping",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(transform="epoch"),
                "'transform' must be",
            ),
            (
                lambda d: d["categories"][0]["metrics"].append({"name": "score", "help": "Again."}),
                "duplicate metric name",
            ),
            (
                lambda d: d["categories"].append(
                    {**copy.deepcopy(d["categories"][0]), "name": "beta"}
                ),
                "duplicate metric name",
            ),
            (
                lambda d: d["categories"][0]["metrics"].append(
                    {"name": "timestamp_seconds", "help": "X."}
                ),
                "duplicate metric name",
            ),
            (
                lambda d: d["categories"][0]["metrics"].extend(
                    [{"name": "x_info", "help": "X."}, {"name": "x", "type": "info", "help": "X."}]
                ),
                "duplicate metric name",
            ),
        ],
    )
    def test_invalid_definitions(
        self, mutate: Callable[[dict[str, Any]], Any], message: str
    ) -> None:
        raw = valid()
        mutate(raw)
        with pytest.raises(ConfigError, match=message):
            parse_definitions(raw, "test")

    def test_full_names_in_the_reserved_namespace_are_rejected(self) -> None:
        raw = valid()
        raw["categories"][0]["prefix"] = "oura_"
        raw["categories"][0]["metrics"][0]["name"] = "exporter_auth_ok"
        with pytest.raises(ConfigError, match="reserved prefix"):
            parse_definitions(raw, "test")

    def test_the_timestamp_name_is_free_for_single_documents(self) -> None:
        raw = valid()
        raw["categories"][0]["kind"] = "single"
        raw["categories"][0]["metrics"].append({"name": "timestamp_seconds", "help": "T."})
        assert parse_definitions(raw, "test")[0].timestamp_name is None

    def test_errors_name_the_location(self) -> None:
        raw = valid()
        raw["categories"][0]["metrics"][0]["type"] = "weird"
        with pytest.raises(ConfigError) as caught:
            parse_definitions(raw, "my.yml")
        assert "my.yml" in str(caught.value)
        assert "alpha" in str(caught.value)
        assert "score" in str(caught.value)

    def test_root_must_be_a_mapping(self) -> None:
        with pytest.raises(ConfigError, match="expected a mapping"):
            parse_definitions(["categories"], "test")


class TestLoading:
    def test_override_file(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.yml"
        path.write_text(
            "categories:\n"
            "  - name: custom\n"
            "    kind: single\n"
            "    endpoint: personal_info\n"
            "    prefix: custom_\n"
            "    metrics:\n"
            "      - {name: age, help: Age.}\n",
            encoding="utf-8",
        )
        categories = load_definitions(path)
        assert [category.name for category in categories] == ["custom"]
        assert categories[0].endpoint == "personal_info"

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="OURA_METRICS_CONFIG"):
            load_definitions(tmp_path / "absent.yml")

    def test_yaml_syntax_error(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.yml"
        path.write_text("categories: [unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_definitions(path)

    def test_schema_error_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.yml"
        path.write_text("categories:\n  - name: x\n    bogus: 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=r"metrics\.yml.*unknown key"):
            load_definitions(path)

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.yml"
        path.write_text("", encoding="utf-8")
        with pytest.raises(ConfigError, match="expected a mapping"):
            load_definitions(path)

    def test_unsafe_yaml_tags_are_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "metrics.yml"
        path.write_text("categories: !!python/object/apply:os.getcwd []\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_definitions(path)
