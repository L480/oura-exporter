import contextlib
import logging
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml

from oura_exporter.config import ConfigError

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
ENDPOINT_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
RESERVED_PREFIX = "oura_exporter_"
LOOKBACK_DAYS = 7
MAX_WARNED = 1000
MIN_DATETIME = datetime.min.replace(tzinfo=UTC)

type MetricType = Literal["gauge", "enum", "info"]
type Kind = Literal["daily", "latest", "single"]
type Document = Mapping[str, Any]
type Scalar = str | int | float | bool

METRIC_TYPES = ("gauge", "enum", "info")
KINDS = ("daily", "latest", "single")
CATEGORY_KEYS = frozenset(
    {
        "name",
        "title",
        "summary",
        "endpoint",
        "kind",
        "prefix",
        "refresh_interval",
        "select",
        "sort_by",
        "metrics",
    }
)
DEFAULT_SUMMARIES = {"daily": "latest day", "latest": "most recent sample", "single": "profile"}
METRIC_KEYS = frozenset({"name", "help", "path", "type", "mapping", "states", "transform"})


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    full_name: str
    help: str
    path: tuple[str, ...]
    type: MetricType = "gauge"
    mapping: Mapping[str, float] | None = None
    states: tuple[str, ...] = ()
    transform: Literal["timestamp"] | None = None

    @property
    def exposed_name(self) -> str:
        return f"{self.full_name}_info" if self.type == "info" else self.full_name


@dataclass(frozen=True, slots=True)
class Snapshot:
    values: Mapping[str, float | str] = field(default_factory=dict)
    timestamp: float | None = None


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    endpoint: str
    kind: Kind
    prefix: str
    metrics: tuple[Metric, ...]
    refresh_interval: int | None = None
    select: Mapping[str, Scalar] = field(default_factory=dict)
    sort_by: str | None = None
    title: str = ""
    summary: str = ""

    @property
    def timestamp_name(self) -> str | None:
        return None if self.kind == "single" else f"{self.prefix}timestamp_seconds"

    @property
    def fields(self) -> tuple[str, ...]:
        names = {metric.path[0] for metric in self.metrics}
        if self.kind == "daily":
            names.add("day")
            names.update(key.split(".")[0] for key in self.select)
            if self.sort_by:
                names.add(self.sort_by.split(".")[0])
        elif self.kind == "latest":
            names.add("timestamp")
        return tuple(sorted(names))

    def params(self, today: date, *, with_fields: bool = True) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.kind == "daily":
            params["start_date"] = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
            params["end_date"] = (today + timedelta(days=1)).isoformat()
        elif self.kind == "latest":
            params["latest"] = "true"
        if with_fields and self.kind != "single":
            params["fields"] = ",".join(self.fields)
        return params


def _walk(document: Document, path: Iterable[str]) -> Any:
    current: Any = document
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError, OverflowError, OSError):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed.timestamp()
        return parsed
    return None


def _parse_day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _selected(document: Document, select: Mapping[str, Scalar]) -> bool:
    return all(_walk(document, key.split(".")) == wanted for key, wanted in select.items())


def _pick_daily(category: Category, documents: Iterable[Document]) -> tuple[Document, float] | None:
    best: tuple[tuple[date, datetime], Document] | None = None
    sort_path = category.sort_by.split(".") if category.sort_by else None
    for document in documents:
        if not _selected(document, category.select):
            continue
        day = _parse_day(document.get("day"))
        if day is None:
            continue
        tie_break = _parse_datetime(_walk(document, sort_path)) if sort_path else None
        key = (day, tie_break or MIN_DATETIME)
        if best is None or key >= best[0]:
            best = (key, document)
    if best is None:
        return None
    midnight = datetime.combine(best[0][0], time.min).astimezone()
    return best[1], midnight.timestamp()


def _pick_latest(documents: Iterable[Document]) -> tuple[Document, float] | None:
    best: tuple[datetime, Document] | None = None
    for document in documents:
        sampled = _parse_datetime(document.get("timestamp"))
        if sampled is not None and (best is None or sampled >= best[0]):
            best = (sampled, document)
    return None if best is None else (best[1], best[0].timestamp())


def _gauge_value(metric: Metric, raw: object) -> float | None:
    if metric.mapping is not None:
        if isinstance(raw, str) and raw in metric.mapping:
            return float(metric.mapping[raw])
        return None
    if metric.transform == "timestamp":
        parsed = _parse_datetime(raw)
        return None if parsed is None else parsed.timestamp()
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, int | float):
        try:
            number = float(raw)
        except OverflowError:
            return None
        return number if math.isfinite(number) else None
    return None


def _convert(metric: Metric, raw: object) -> float | str | None:
    if metric.type == "gauge":
        return _gauge_value(metric, raw)
    if metric.type == "enum":
        return raw if isinstance(raw, str) and raw in metric.states else None
    return str(raw)


def extract_values(
    category: Category, document: Document, warned: set[tuple[str, str]] | None = None
) -> dict[str, float | str]:
    seen = warned if warned is not None else set()
    values: dict[str, float | str] = {}
    for metric in category.metrics:
        raw = _walk(document, metric.path)
        if raw is None:
            continue
        value = _convert(metric, raw)
        if value is None:
            key = (metric.full_name, repr(raw)[:100])
            if key not in seen and len(seen) < MAX_WARNED:
                seen.add(key)
                logger.warning(
                    "%s: ignoring unexpected value %s; the series is omitted",
                    metric.full_name,
                    key[1],
                )
            continue
        values[metric.name] = value
    return values


def build_snapshot(
    category: Category, documents: Iterable[Document], warned: set[tuple[str, str]] | None = None
) -> Snapshot:
    if category.kind == "single":
        chosen: tuple[Document, float | None] | None = next(
            ((document, None) for document in documents), None
        )
    elif category.kind == "daily":
        chosen = _pick_daily(category, documents)
    else:
        chosen = _pick_latest(documents)
    if chosen is None:
        return Snapshot()
    document, timestamp = chosen
    return Snapshot(extract_values(category, document, warned), timestamp)


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
    path_text = _string(data, "path", where, default=name)
    path = tuple(path_text.split("."))
    if not all(path):
        raise ConfigError(f"{where}: 'path' has an empty segment: {path_text!r}")

    metric_type = data.get("type", "gauge")
    if metric_type not in METRIC_TYPES:
        raise ConfigError(f"{where}: 'type' must be one of {', '.join(METRIC_TYPES)}")
    mapping_raw = data.get("mapping")
    states_raw = data.get("states")
    transform = data.get("transform")

    if metric_type != "gauge" and (mapping_raw is not None or transform is not None):
        raise ConfigError(f"{where}: 'mapping' and 'transform' are only valid for gauges")
    if metric_type != "enum" and states_raw is not None:
        raise ConfigError(f"{where}: 'states' is only valid for enum metrics")
    if mapping_raw is not None and transform is not None:
        raise ConfigError(f"{where}: 'mapping' and 'transform' are mutually exclusive")

    mapping: dict[str, float] | None = None
    if mapping_raw is not None:
        mapping_data = _mapping(mapping_raw, f"{where}: 'mapping'")
        if not mapping_data or not all(_is_number(value) for value in mapping_data.values()):
            raise ConfigError(f"{where}: 'mapping' must map strings to finite numbers")
        mapping = {key: float(value) for key, value in mapping_data.items()}

    states: tuple[str, ...] = ()
    if metric_type == "enum":
        if (
            not isinstance(states_raw, list)
            or not states_raw
            or not all(isinstance(state, str) and state for state in states_raw)
            or len(set(states_raw)) != len(states_raw)
        ):
            raise ConfigError(f"{where}: 'states' must be a non-empty list of unique strings")
        states = tuple(states_raw)

    if transform is not None and transform != "timestamp":
        raise ConfigError(f"{where}: 'transform' must be 'timestamp'")

    return Metric(
        name=name,
        full_name=f"{prefix}{name}",
        help=help_text,
        path=path,
        type=metric_type,
        mapping=mapping,
        states=states,
        transform=transform,
    )


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

    select: dict[str, Scalar] = {}
    if "select" in data:
        select_data = _mapping(data["select"], f"{where}: 'select'")
        if not all(isinstance(value, str | int | float | bool) for value in select_data.values()):
            raise ConfigError(f"{where}: 'select' values must be strings, numbers or booleans")
        select = dict(select_data)
    sort_by = data.get("sort_by")
    if sort_by is not None and (not isinstance(sort_by, str) or not sort_by.strip()):
        raise ConfigError(f"{where}: 'sort_by' must be a non-empty string")
    if kind != "daily" and (select or sort_by is not None):
        raise ConfigError(f"{where}: 'select' and 'sort_by' are only valid for kind 'daily'")

    metrics_raw = data.get("metrics")
    if not isinstance(metrics_raw, list) or not metrics_raw:
        raise ConfigError(f"{where}: 'metrics' must be a non-empty list")
    metrics = tuple(
        _parse_metric(item, prefix, f"{where}: metrics[{index}]")
        for index, item in enumerate(metrics_raw)
    )
    return Category(
        name=name,
        endpoint=endpoint,
        kind=kind,
        prefix=prefix,
        metrics=metrics,
        refresh_interval=refresh_interval,
        select=select,
        sort_by=sort_by,
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
        sample_names = [metric.exposed_name for metric in category.metrics]
        if category.timestamp_name is not None:
            sample_names.append(category.timestamp_name)
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


def _metric_label(metric: Metric) -> str:
    if metric.type == "info":
        return f"{metric.name}_info"
    if metric.type == "enum":
        return f"{metric.name} (state set)"
    if metric.mapping:
        return f"{metric.name} ({min(metric.mapping.values()):g}-{max(metric.mapping.values()):g})"
    return metric.name


def render_metric_list(categories: Iterable[Category]) -> str:
    lines: list[str] = []
    for category in categories:
        names = [_metric_label(metric) for metric in category.metrics]
        if category.timestamp_name is not None:
            names.append("timestamp_seconds")
        lines.append(f"- **{category.title}** · `{category.prefix}*` · {category.summary}<br>")
        lines.append("  " + ", ".join(f"`{name}`" for name in names))
    return "\n".join(lines)
