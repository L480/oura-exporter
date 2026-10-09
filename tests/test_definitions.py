import copy
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oura_exporter.config import ConfigError
from oura_exporter.definitions import (
    Category,
    help_texts,
    load_definitions,
    parse_definitions,
)

NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
CATEGORY_KINDS = {
    "sleep": "event",
    "daily_sleep": "daily",
    "daily_readiness": "daily",
    "daily_activity": "daily",
    "heartrate": "sample",
    "daily_stress": "daily",
    "daily_resilience": "daily",
    "daily_spo2": "daily",
    "ring_battery_level": "sample",
    "workout": "event",
    "session": "event",
    "sleep_time": "daily",
    "vo2_max": "daily",
    "daily_cardiovascular_age": "daily",
    "enhanced_tag": "event",
    "rest_mode_period": "event",
    "personal_info": "single",
    "ring_configuration": "single",
}


HORIZON_SERIES = {
    "name": "s",
    "help": "S.",
    "type": "string",
    "interval": 300,
    "start": "timestamp",
    "sync_horizon": True,
}


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


def event() -> dict[str, Any]:
    raw = valid()
    raw["categories"][0].update(kind="event", time_path="start", end_path="end")
    return raw


class TestPackagedDefinitions:
    def test_categories_are_ordered_by_interest_and_cover_every_endpoint(self) -> None:
        assert {c.name: c.kind for c in load_definitions()} == CATEGORY_KINDS
        assert [c.name for c in load_definitions()] == list(CATEGORY_KINDS)

    def test_every_exposed_name_is_unique_and_valid(self) -> None:
        exposed: list[str] = []
        for category in load_definitions():
            assert category.prefix.endswith("_")
            assert not category.prefix.startswith("oura_exporter_")
            names = [metric.exposed_name for metric in category.metrics]
            names += [series.full_name for series in category.series]
            names += [category.duration_name] if category.duration_name else []
            for name in names:
                assert NAME_PATTERN.match(name)
            exposed += names
        assert len(exposed) == len(set(exposed))
        assert all(help_text.strip() for help_text in help_texts(load_definitions()).values())

    def test_every_path_is_unique_within_its_category(self) -> None:
        for category in load_definitions():
            paths = [metric.path for metric in category.metrics]
            assert len(paths) == len(set(paths)), category.name

    def test_no_pii_and_no_free_text(self) -> None:
        for category in load_definitions():
            for path in [metric.path for metric in category.metrics] + [
                label.path for label in category.labels
            ]:
                assert path not in {("email",), ("label",), ("comment",), ("text",)}

    def test_headline_values_come_first_and_contributors_last(self) -> None:
        for name in ("daily_activity", "daily_readiness", "daily_sleep"):
            names = [metric.name for metric in by_name(name).metrics]
            contributors = [n for n in names if n.startswith("contributors_")]
            assert names[len(names) - len(contributors) :] == contributors

    def test_events_have_their_own_time_paths(self) -> None:
        times = {
            category.name: ".".join(category.time_path or ())
            for category in load_definitions()
            if category.kind == "event"
        }
        assert times == {
            "sleep": "bedtime_end",
            "workout": "start_datetime",
            "session": "start_datetime",
            "enhanced_tag": "start_time",
            "rest_mode_period": "start_time",
        }
        assert by_name("heartrate").time_path == ("timestamp",)

    def test_labels_are_low_cardinality_fields_only(self) -> None:
        labels = {
            category.name: {label.name: ".".join(label.path) for label in category.labels}
            for category in load_definitions()
            if category.labels
        }
        assert labels == {
            "sleep": {"sleep_type": "type"},
            "workout": {"activity": "activity", "intensity": "intensity", "source": "source"},
            "session": {"session_type": "type", "mood": "mood"},
            "enhanced_tag": {"tag_type_code": "tag_type_code", "custom_name": "custom_name"},
            "ring_configuration": {"ring": "id"},
        }

    def test_embedded_series(self) -> None:
        series = {
            category.name: {item.name: (item.type, item.interval) for item in category.series}
            for category in load_definitions()
            if category.series
        }
        assert series["sleep"] == {
            "heart_rate_bpm": ("samples", None),
            "hrv_milliseconds": ("samples", None),
            "phase_5_min": ("string", 300.0),
            "phase_30_sec": ("string", 30.0),
            "app_phase_5_min": ("string", 300.0),
            "movement_30_sec": ("string", 30.0),
        }
        assert series["daily_activity"] == {
            "met": ("samples", None),
            "class_5_min": ("string", 300.0),
        }
        assert set(series["session"]) == {
            "heart_rate_bpm",
            "heart_rate_variability_milliseconds",
            "motion_count",
        }

    def test_enums_are_numeric_mappings(self) -> None:
        resilience = {metric.name: metric for metric in by_name("daily_resilience").metrics}
        assert resilience["level"].mapping == {
            "limited": 1.0,
            "adequate": 2.0,
            "solid": 3.0,
            "strong": 4.0,
            "exceptional": 5.0,
        }
        source = {metric.name: metric for metric in by_name("heartrate").metrics}["source"]
        assert source.mapping == {
            "awake": 1.0,
            "rest": 2.0,
            "sleep": 3.0,
            "session": 4.0,
            "live": 5.0,
            "workout": 6.0,
        }

    def test_the_vo2_max_endpoint_keeps_oura_spelling(self) -> None:
        assert by_name("vo2_max").endpoint == "vO2_max"

    def test_duration_names(self) -> None:
        assert by_name("workout").duration_name == "oura_workout_duration_seconds"
        assert by_name("heartrate").duration_name is None
        assert by_name("enhanced_tag").no_end == "zero"
        assert by_name("rest_mode_period").no_end == "skip"

    def test_profile_is_refreshed_hourly(self) -> None:
        assert by_name("personal_info").refresh_interval == 3600
        assert by_name("ring_configuration").refresh_interval == 3600


class TestTitleAndSummary:
    @pytest.mark.parametrize(
        ("kind", "summary"),
        [
            ("daily", "daily value"),
            ("sample", "every sample"),
            ("event", "every event"),
            ("single", "profile"),
        ],
    )
    def test_defaults(self, kind: str, summary: str) -> None:
        raw = valid()
        raw["categories"][0]["kind"] = kind
        raw["categories"][0]["name"] = "daily_sleep_score"
        if kind == "event":
            raw["categories"][0]["time_path"] = "start"
        category = parse_definitions(raw, "test")[0]
        assert category.title == "Daily sleep score"
        assert category.summary == summary

    def test_explicit_values_win(self) -> None:
        raw = valid()
        raw["categories"][0].update(title="Mine", summary="custom")
        category = parse_definitions(raw, "test")[0]
        assert (category.title, category.summary) == ("Mine", "custom")


class TestRequestParameters:
    NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)

    def params(self, name: str, **kwargs: Any) -> dict[str, str]:
        return by_name(name).params(self.NOW - timedelta(days=3), self.NOW, **kwargs)

    def test_daily_and_event_use_dates(self) -> None:
        for name in ("daily_readiness", "sleep", "workout"):
            params = self.params(name)
            assert params["start_date"] == "2026-10-03"
            assert params["end_date"] == "2026-10-07"

    def test_samples_use_datetimes(self) -> None:
        params = self.params("heartrate")
        assert params["start_datetime"] == "2026-10-03T14:00:00+00:00"
        assert params["end_datetime"] == "2026-10-06T14:00:00+00:00"
        assert "start_date" not in params

    def test_fields_list_every_top_level_name(self) -> None:
        fields = set(self.params("sleep")["fields"].split(","))
        assert {"bedtime_end", "bedtime_start", "type", "heart_rate", "hrv", "readiness"} <= fields
        assert set(self.params("workout")["fields"].split(",")) >= {
            "start_datetime",
            "end_datetime",
            "activity",
            "calories",
        }
        assert "day" in self.params("daily_stress")["fields"].split(",")
        assert "fields" not in self.params("sleep", with_fields=False)

    def test_single_documents_have_no_parameters(self) -> None:
        assert self.params("personal_info") == {}

    def test_windows_are_chunked(self) -> None:
        start = self.NOW - timedelta(days=20)
        samples = by_name("heartrate").windows(start, self.NOW)
        assert [(b - a).days for a, b in samples] == [7, 7, 6]
        assert samples[0][0] == start
        assert samples[-1][1] == self.NOW
        assert len(by_name("sleep").windows(start, self.NOW)) == 1
        assert len(by_name("sleep").windows(self.NOW - timedelta(days=70), self.NOW)) == 3

    def test_an_empty_range_is_still_one_window(self) -> None:
        assert by_name("sleep").windows(self.NOW, self.NOW) == [(self.NOW, self.NOW)]


class TestValidation:
    def test_minimal_definition_is_valid(self) -> None:
        category = parse_definitions(valid(), "test")[0]
        assert category.endpoint == "alpha"
        assert category.metrics[0].full_name == "oura_alpha_score"
        assert category.metrics[0].path == ("score",)
        assert category.metrics[0].type == "gauge"
        assert category.refresh_interval is None
        assert category.time_path is None

    def test_default_endpoint_can_be_overridden(self) -> None:
        raw = valid()
        raw["categories"][0]["endpoint"] = "vO2_max"
        assert parse_definitions(raw, "test")[0].endpoint == "vO2_max"

    def test_sample_time_path_defaults_to_timestamp(self) -> None:
        raw = valid()
        raw["categories"][0]["kind"] = "sample"
        assert parse_definitions(raw, "test")[0].time_path == ("timestamp",)

    def test_duration_only_events_are_valid(self) -> None:
        raw = event()
        raw["categories"][0].pop("metrics")
        category = parse_definitions(raw, "test")[0]
        assert category.metrics == ()
        assert category.duration_name == "oura_alpha_duration_seconds"

    def test_series_definitions(self) -> None:
        raw = valid()
        raw["categories"][0]["series"] = [
            {"name": "hr", "path": "heart_rate", "type": "samples", "help": "Heart rate."},
            {
                "name": "phase",
                "type": "string",
                "interval": 300,
                "start": "bedtime_start",
                "help": "Phase.",
            },
        ]
        samples, string = parse_definitions(raw, "test")[0].series
        assert (samples.full_name, samples.path, samples.interval) == (
            "oura_alpha_hr",
            ("heart_rate",),
            None,
        )
        assert (string.path, string.interval, string.start) == (
            ("phase",),
            300.0,
            ("bedtime_start",),
        )

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda d: d.update(extra=1), "unknown key.*extra"),
            (lambda d: d.update(categories=[]), "'categories' must be a non-empty list"),
            (lambda d: d.update(categories="x"), "'categories' must be a non-empty list"),
            (lambda d: d["categories"][0].update(foo=1), "unknown key.*foo"),
            (lambda d: d["categories"][0].update(title=""), "'title' must be a non-empty string"),
            (lambda d: d["categories"][0].update(title=5), "'title' must be a non-empty string"),
            (lambda d: d["categories"][0].update(summary=""), "'summary' must be a non-empty"),
            (lambda d: d["categories"].__setitem__(0, "text"), "expected a mapping"),
            (lambda d: d["categories"][0].pop("name"), "'name' must be a non-empty string"),
            (lambda d: d["categories"][0].update(name="1abc"), "'name' must match"),
            (
                lambda d: d["categories"].append(copy.deepcopy(d["categories"][0])),
                "duplicate category",
            ),
            (lambda d: d["categories"][0].pop("kind"), "'kind' must be one of"),
            (lambda d: d["categories"][0].update(kind="latest"), "'kind' must be one of"),
            (lambda d: d["categories"][0].update(endpoint="a/b"), "'endpoint' must match"),
            (lambda d: d["categories"][0].update(endpoint=""), "'endpoint' must be a non-empty"),
            (lambda d: d["categories"][0].pop("prefix"), "'prefix' must be a non-empty string"),
            (lambda d: d["categories"][0].update(prefix="oura-alpha-"), "'prefix' must match"),
            (lambda d: d["categories"][0].update(prefix="oura_exporter_x_"), "reserved"),
            (lambda d: d["categories"][0].update(refresh_interval=0), "refresh_interval"),
            (lambda d: d["categories"][0].update(refresh_interval="60"), "refresh_interval"),
            (lambda d: d["categories"][0].update(refresh_interval=True), "refresh_interval"),
            (lambda d: d["categories"][0].update(select={"a": 1}), "unknown key.*select"),
            (lambda d: d["categories"][0].update(time_path="day"), "'time_path' is only valid"),
            (lambda d: d["categories"][0].update(kind="event"), "'time_path' must be a non-empty"),
            (lambda d: d["categories"][0].update(end_path="x"), "'end_path' is only valid"),
            (lambda d: d["categories"][0].update(no_end="zero"), "'no_end' needs 'end_path'"),
            (
                lambda d: d.update(categories=[event()["categories"][0] | {"no_end": "never"}]),
                "'no_end' must be one of",
            ),
            (
                lambda d: d["categories"][0].pop("metrics"),
                "needs 'metrics', 'series' or 'end_path'",
            ),
            (lambda d: d["categories"][0].update(metrics=[]), "needs 'metrics', 'series'"),
            (lambda d: d["categories"][0].update(metrics="x"), "'metrics' must be a list"),
            (lambda d: d["categories"][0]["metrics"][0].update(foo=1), "unknown key.*foo"),
            (lambda d: d["categories"][0]["metrics"].__setitem__(0, "text"), "expected a mapping"),
            (lambda d: d["categories"][0]["metrics"][0].pop("name"), "'name' must be a non-empty"),
            (
                lambda d: d["categories"][0]["metrics"][0].update(name="has space"),
                "'name' must match",
            ),
            (lambda d: d["categories"][0]["metrics"][0].pop("help"), "'help' must be a non-empty"),
            (lambda d: d["categories"][0]["metrics"][0].update(path="a..b"), "empty segment"),
            (lambda d: d["categories"][0]["metrics"][0].update(path=".a"), "empty segment"),
            (
                lambda d: d["categories"][0]["metrics"][0].update(path=5),
                "'path' must be a non-empty",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(type="enum"),
                "'type' must be one of",
            ),
            (
                lambda d: d["categories"][0]["metrics"][0].update(type="info", mapping={"a": 1}),
                "only valid for gauges",
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
                lambda d: d["categories"][0]["metrics"].extend(
                    [{"name": "x_info", "help": "X."}, {"name": "x", "type": "info", "help": "X."}]
                ),
                "duplicate metric name",
            ),
            (lambda d: d["categories"][0].update(labels="x"), "'labels' must be a list"),
            (lambda d: d["categories"][0].update(labels=[{"name": "job"}]), "reserved"),
            (lambda d: d["categories"][0].update(labels=[{"name": "__x"}]), "reserved"),
            (lambda d: d["categories"][0].update(labels=[{"name": "le"}]), "reserved"),
            (lambda d: d["categories"][0].update(labels=[{"name": "a", "x": 1}]), "unknown key.*x"),
            (lambda d: d["categories"][0].update(labels=[{"name": "a b"}]), "'name' must match"),
            (
                lambda d: d["categories"][0].update(labels=[{"name": "a"}, {"name": "a"}]),
                "duplicate label",
            ),
            (lambda d: d["categories"][0].update(series="x"), "'series' must be a list"),
            (
                lambda d: d["categories"][0].update(series=[{"name": "s", "help": "S."}]),
                "'type' must be one of",
            ),
            (
                lambda d: d["categories"][0].update(
                    series=[{"name": "s", "help": "S.", "type": "string"}]
                ),
                "'interval' must be a positive",
            ),
            (
                lambda d: d["categories"][0].update(
                    series=[{"name": "s", "help": "S.", "type": "string", "interval": 0}]
                ),
                "'interval' must be a positive",
            ),
            (
                lambda d: d["categories"][0].update(
                    series=[{"name": "s", "help": "S.", "type": "string", "interval": 30}]
                ),
                "'start' must be a non-empty",
            ),
            (
                lambda d: d["categories"][0].update(
                    series=[{"name": "s", "help": "S.", "type": "samples", "interval": 30}]
                ),
                "only valid for type 'string'",
            ),
            (
                lambda d: d["categories"][0].update(
                    series=[{"name": "score", "help": "S.", "type": "samples"}]
                ),
                "duplicate metric name",
            ),
            (
                lambda d: d["categories"][0].update(
                    series=[{**HORIZON_SERIES, "sync_horizon": "yes"}]
                ),
                "'sync_horizon' must be true or false",
            ),
            (
                lambda d: d["categories"][0].update(
                    series=[{"name": "s", "help": "S.", "type": "samples", "sync_horizon": True}]
                ),
                "'sync_horizon' is only valid for type 'string' in kind 'daily'",
            ),
            (
                lambda d: d["categories"][0].update(kind="sample", series=[HORIZON_SERIES]),
                "'sync_horizon' is only valid for type 'string' in kind 'daily'",
            ),
            (
                lambda d: d["categories"][0].update(
                    series=[HORIZON_SERIES, {**HORIZON_SERIES, "name": "t"}]
                ),
                "only one series may set 'sync_horizon'",
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

    def test_the_duration_name_must_be_free(self) -> None:
        raw = event()
        raw["categories"][0]["metrics"].append({"name": "duration_seconds", "help": "D."})
        with pytest.raises(ConfigError, match="duplicate metric name"):
            parse_definitions(raw, "test")

    def test_full_names_in_the_reserved_namespace_are_rejected(self) -> None:
        raw = valid()
        raw["categories"][0]["prefix"] = "oura_"
        raw["categories"][0]["metrics"][0]["name"] = "exporter_auth_ok"
        with pytest.raises(ConfigError, match="reserved prefix"):
            parse_definitions(raw, "test")

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


def test_the_horizon_series_is_exposed_on_the_category() -> None:
    raw = valid()
    raw["categories"][0]["series"] = [HORIZON_SERIES]
    horizon = parse_definitions(raw, "test")[0].horizon_series
    assert horizon is not None
    assert horizon.name == "s"
    assert by_name("daily_activity").horizon_series is not None
    assert by_name("heartrate").horizon_series is None
