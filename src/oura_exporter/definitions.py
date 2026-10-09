import contextlib
import logging
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml

from oura_exporter.config import ConfigError

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
ENDPOINT_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
RESERVED_PREFIX = "oura_exporter_"
RESERVED_LABELS = frozenset({"job", "le", "quantile"})
MAX_WARNED = 1000
SAMPLE_CHUNK_DAYS = 7
CHUNK_DAYS = 30

type MetricType = Literal["gauge", "info"]
type Kind = Literal["daily", "sample", "event", "single"]
type SeriesType = Literal["samples", "string"]
type NoEnd = Literal["skip", "zero"]
type Document = Mapping[str, Any]

METRIC_TYPES = ("gauge", "info")
KINDS = ("daily", "sample", "event", "single")
SERIES_TYPES = ("samples", "string")
NO_END = ("skip", "zero")
CATEGORY_KEYS = frozenset(
    {
        "name",
        "title",
        "summary",
        "endpoint",
        "kind",
        "prefix",
        "refresh_interval",
        "time_path",
        "end_path",
        "no_end",
        "labels",
        "series",
        "metrics",
    }
)
DEFAULT_SUMMARIES = {
    "daily": "daily value",
    "sample": "every sample",
    "event": "every event",
    "single": "profile",
}
CONTRIBUTOR_PREFIX = "contributors_"
METRIC_KEYS = frozenset({"name", "help", "path", "type", "mapping"})
LABEL_KEYS = frozenset({"name", "path"})
SERIES_KEYS = frozenset({"name", "help", "path", "type", "interval", "start", "sync_horizon"})


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    full_name: str
    help: str
    path: tuple[str, ...]
    type: MetricType = "gauge"
    mapping: Mapping[str, float] | None = None

    @property
    def exposed_name(self) -> str:
        return f"{self.full_name}_info" if self.type == "info" else self.full_name


@dataclass(frozen=True, slots=True)
class Label:
    name: str
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Series:
    name: str
    full_name: str
    help: str
    path: tuple[str, ...]
    type: SeriesType
    interval: float | None = None
    start: tuple[str, ...] | None = None
    sync_horizon: bool = False


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    endpoint: str
    kind: Kind
    prefix: str
    metrics: tuple[Metric, ...]
    series: tuple[Series, ...] = ()
    labels: tuple[Label, ...] = ()
    refresh_interval: int | None = None
    time_path: tuple[str, ...] | None = None
    end_path: tuple[str, ...] | None = None
    no_end: NoEnd = "skip"
    title: str = ""
    summary: str = ""

    @property
    def horizon_series(self) -> Series | None:
        return next((series for series in self.series if series.sync_horizon), None)

    @property
    def duration_name(self) -> str | None:
        return None if self.end_path is None else f"{self.prefix}duration_seconds"

    @property
    def fields(self) -> tuple[str, ...]:
        paths = [metric.path for metric in self.metrics]
        paths += [label.path for label in self.labels]
        for series in self.series:
            paths.append(series.path)
            if series.start is not None:
                paths.append(series.start)
        for optional in (self.time_path, self.end_path):
            if optional is not None:
                paths.append(optional)
        names = {path[0] for path in paths}
        if self.kind == "daily":
            names.add("day")
        return tuple(sorted(names))

    def windows(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        step = timedelta(days=SAMPLE_CHUNK_DAYS if self.kind == "sample" else CHUNK_DAYS)
        windows: list[tuple[datetime, datetime]] = []
        current = start
        while current < end:
            windows.append((current, min(current + step, end)))
            current += step
        return windows or [(start, end)]

    def params(self, start: datetime, end: datetime, *, with_fields: bool = True) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.kind in ("daily", "event"):
            params["start_date"] = start.astimezone().date().isoformat()
            params["end_date"] = (end.astimezone().date() + timedelta(days=1)).isoformat()
        elif self.kind == "sample":
            params["start_datetime"] = start.astimezone(UTC).isoformat()
            params["end_datetime"] = end.astimezone(UTC).isoformat()
        if with_fields and self.kind != "single":
            params["fields"] = ",".join(self.fields)
        return params


def walk(document: Document, path: Iterable[str]) -> Any:
    current: Any = document
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError, OverflowError, OSError):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed.timestamp()
        return parsed
    return None


def parse_day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def number(raw: object) -> float | None:
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, int | float):
        try:
            value = float(raw)
        except OverflowError:
            return None
        return value if math.isfinite(value) else None
    return None


def gauge_value(metric: Metric, raw: object) -> float | None:
    if metric.mapping is not None:
        if isinstance(raw, str) and raw in metric.mapping:
            return float(metric.mapping[raw])
        return None
    return number(raw)


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{where}: expected a mapping with string keys")
    return value


def _check_keys(data: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _string(data: Mapping[str, Any], key: str, where: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: '{key}' must be a non-empty string")
    return value


def _identifier(data: Mapping[str, Any], key: str, where: str) -> str:
    value = _string(data, key, where)
    if not NAME_PATTERN.match(value):
        raise ConfigError(f"{where}: '{key}' must match {NAME_PATTERN.pattern}, got {value!r}")
    return value


def _path(
    data: Mapping[str, Any], key: str, where: str, default: str | None = None
) -> tuple[str, ...]:
    text = _string(data, key, where, default)
    path = tuple(text.split("."))
    if not all(path):
        raise ConfigError(f"{where}: '{key}' has an empty segment: {text!r}")
    return path


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _parse_metric(raw: object, prefix: str, where: str) -> Metric:
    data = _mapping(raw, where)
    _check_keys(data, METRIC_KEYS, where)
    name = _identifier(data, "name", where)
    where = f"{where} ({name})"
    help_text = _string(data, "help", where)
    path = _path(data, "path", where, default=name)

    metric_type = data.get("type", "gauge")
    if metric_type not in METRIC_TYPES:
        raise ConfigError(f"{where}: 'type' must be one of {', '.join(METRIC_TYPES)}")
    mapping_raw = data.get("mapping")
    if metric_type != "gauge" and mapping_raw is not None:
        raise ConfigError(f"{where}: 'mapping' is only valid for gauges")

    mapping: dict[str, float] | None = None
    if mapping_raw is not None:
        mapping_data = _mapping(mapping_raw, f"{where}: 'mapping'")
        if not mapping_data or not all(_is_number(value) for value in mapping_data.values()):
            raise ConfigError(f"{where}: 'mapping' must map strings to finite numbers")
        mapping = {key: float(value) for key, value in mapping_data.items()}

    return Metric(
        name=name,
        full_name=f"{prefix}{name}",
        help=help_text,
        path=path,
        type=metric_type,
        mapping=mapping,
    )


def _parse_label(raw: object, where: str) -> Label:
    data = _mapping(raw, where)
    _check_keys(data, LABEL_KEYS, where)
    name = _identifier(data, "name", where)
    where = f"{where} ({name})"
    if name in RESERVED_LABELS or name.startswith("__"):
        raise ConfigError(f"{where}: label name {name!r} is reserved")
    return Label(name=name, path=_path(data, "path", where, default=name))


def _parse_series(raw: object, prefix: str, kind: Kind, where: str) -> Series:
    data = _mapping(raw, where)
    _check_keys(data, SERIES_KEYS, where)
    name = _identifier(data, "name", where)
    where = f"{where} ({name})"
    help_text = _string(data, "help", where)
    path = _path(data, "path", where, default=name)
    series_type = data.get("type")
    if series_type not in SERIES_TYPES:
        raise ConfigError(f"{where}: 'type' must be one of {', '.join(SERIES_TYPES)}")
    interval: float | None = None
    start: tuple[str, ...] | None = None
    if series_type == "string":
        interval = number(data.get("interval"))
        if interval is None or interval <= 0:
            raise ConfigError(f"{where}: 'interval' must be a positive number of seconds")
        start = _path(data, "start", where)
    elif "interval" in data or "start" in data:
        raise ConfigError(f"{where}: 'interval' and 'start' are only valid for type 'string'")
    sync_horizon = data.get("sync_horizon", False)
    if not isinstance(sync_horizon, bool):
        raise ConfigError(f"{where}: 'sync_horizon' must be true or false")
    if sync_horizon and (series_type != "string" or kind != "daily"):
        raise ConfigError(
            f"{where}: 'sync_horizon' is only valid for type 'string' in kind 'daily'"
        )
    return Series(
        name=name,
        full_name=f"{prefix}{name}",
        help=help_text,
        path=path,
        type=series_type,
        interval=interval,
        start=start,
        sync_horizon=sync_horizon,
    )


def _parse_list(data: Mapping[str, Any], key: str, where: str) -> list[Any]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"{where}: '{key}' must be a list")
    return value


def _parse_category(raw: object, where: str) -> Category:
    data = _mapping(raw, where)
    _check_keys(data, CATEGORY_KEYS, where)
    name = _identifier(data, "name", where)
    where = f"{where} ({name})"
    kind = data.get("kind")
    if kind not in KINDS:
        raise ConfigError(f"{where}: 'kind' must be one of {', '.join(KINDS)}")
    endpoint = _string(data, "endpoint", where, default=name)
    if not ENDPOINT_PATTERN.match(endpoint):
        raise ConfigError(f"{where}: 'endpoint' must match {ENDPOINT_PATTERN.pattern}")
    prefix = _identifier(data, "prefix", where)
    default_title = name.replace("_", " ")
    title = _string(data, "title", where, default=default_title[:1].upper() + default_title[1:])
    summary = _string(data, "summary", where, default=DEFAULT_SUMMARIES[kind])

    refresh_interval = data.get("refresh_interval")
    if refresh_interval is not None and not _is_positive_int(refresh_interval):
        raise ConfigError(f"{where}: 'refresh_interval' must be a positive integer (seconds)")

    time_path: tuple[str, ...] | None = None
    if kind == "sample":
        time_path = _path(data, "time_path", where, default="timestamp")
    elif kind == "event":
        time_path = _path(data, "time_path", where)
    elif "time_path" in data:
        raise ConfigError(f"{where}: 'time_path' is only valid for kinds 'sample' and 'event'")
    end_path: tuple[str, ...] | None = None
    if "end_path" in data:
        if kind != "event":
            raise ConfigError(f"{where}: 'end_path' is only valid for kind 'event'")
        end_path = _path(data, "end_path", where)
    no_end = data.get("no_end", "skip")
    if no_end not in NO_END:
        raise ConfigError(f"{where}: 'no_end' must be one of {', '.join(NO_END)}")
    if "no_end" in data and end_path is None:
        raise ConfigError(f"{where}: 'no_end' needs 'end_path'")

    labels = tuple(
        _parse_label(item, f"{where}: labels[{index}]")
        for index, item in enumerate(_parse_list(data, "labels", where))
    )
    series = tuple(
        _parse_series(item, prefix, kind, f"{where}: series[{index}]")
        for index, item in enumerate(_parse_list(data, "series", where))
    )
    if sum(item.sync_horizon for item in series) > 1:
        raise ConfigError(f"{where}: only one series may set 'sync_horizon'")
    metrics = tuple(
        _parse_metric(item, prefix, f"{where}: metrics[{index}]")
        for index, item in enumerate(_parse_list(data, "metrics", where))
    )
    if not (metrics or series or end_path):
        raise ConfigError(f"{where}: needs 'metrics', 'series' or 'end_path'")
    return Category(
        name=name,
        endpoint=endpoint,
        kind=kind,
        prefix=prefix,
        metrics=metrics,
        series=series,
        labels=labels,
        refresh_interval=refresh_interval,
        time_path=time_path,
        end_path=end_path,
        no_end=no_end,
        title=title,
        summary=summary,
    )


def _check_unique(categories: tuple[Category, ...], source: str) -> None:
    names: set[str] = set()
    exposed: set[str] = set()
    for category in categories:
        if category.name in names:
            raise ConfigError(f"{source}: duplicate category name {category.name!r}")
        names.add(category.name)
        if category.prefix.startswith(RESERVED_PREFIX):
            raise ConfigError(
                f"{source}: category {category.name!r}: prefix must not start with "
                f"{RESERVED_PREFIX!r} (reserved for the exporter's own metrics)"
            )
        label_names = [label.name for label in category.labels]
        if len(set(label_names)) != len(label_names):
            raise ConfigError(f"{source}: category {category.name!r}: duplicate label name")
        sample_names = [metric.exposed_name for metric in category.metrics]
        sample_names += [series.full_name for series in category.series]
        if category.duration_name is not None:
            sample_names.append(category.duration_name)
        for sample_name in sample_names:
            if sample_name in exposed:
                raise ConfigError(f"{source}: duplicate metric name {sample_name!r}")
            if sample_name.startswith(RESERVED_PREFIX):
                raise ConfigError(f"{source}: metric name {sample_name!r} uses a reserved prefix")
            exposed.add(sample_name)


def parse_definitions(data: object, source: str = "metrics definitions") -> tuple[Category, ...]:
    root = _mapping(data, source)
    _check_keys(root, frozenset({"categories"}), source)
    raw_categories = root.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise ConfigError(f"{source}: 'categories' must be a non-empty list")
    categories = tuple(
        _parse_category(item, f"{source}: categories[{index}]")
        for index, item in enumerate(raw_categories)
    )
    _check_unique(categories, source)
    return categories


def load_definitions(path: Path | None = None) -> tuple[Category, ...]:
    if path is None:
        source = "packaged metrics.yml"
        text = resources.files("oura_exporter").joinpath("metrics.yml").read_text(encoding="utf-8")
    else:
        source = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"OURA_METRICS_CONFIG: cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    return parse_definitions(data, source)


def help_texts(categories: Iterable[Category]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for category in categories:
        for metric in category.metrics:
            texts[metric.exposed_name] = metric.help
        for series in category.series:
            texts[series.full_name] = series.help
        if category.duration_name is not None:
            texts[category.duration_name] = "Duration of the event, in seconds."
    return texts


def _metric_label(metric: Metric) -> str:
    if metric.type == "info":
        return f"{metric.name}_info"
    if metric.mapping:
        return f"{metric.name} ({min(metric.mapping.values()):g}-{max(metric.mapping.values()):g})"
    return metric.name


def render_metric_list(categories: Iterable[Category]) -> str:
    lines: list[str] = []
    for category in categories:
        names: list[str] = []
        contributors: list[str] = []
        for metric in category.metrics:
            label = _metric_label(metric)
            if metric.name.startswith(CONTRIBUTOR_PREFIX):
                contributors.append(label.removeprefix(CONTRIBUTOR_PREFIX))
            else:
                names.append(label)
        names += [series.name for series in category.series]
        if category.duration_name is not None:
            names.append("duration_seconds")
        lines.append(f"- **{category.title}** · `{category.prefix}*` · {category.summary}<br>")
        if names:
            lines.append("  " + _code_list(names) + ("<br>" if contributors else ""))
        if contributors:
            lines.append(f"  contributors (`{CONTRIBUTOR_PREFIX}*`): " + _code_list(contributors))
    return "\n".join(lines)


def _code_list(names: Iterable[str]) -> str:
    return ", ".join(f"`{name}`" for name in names)
