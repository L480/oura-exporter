import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from oura_exporter.definitions import (
    MAX_WARNED,
    Category,
    Document,
    Metric,
    Series,
    gauge_value,
    number,
    parse_datetime,
    parse_day,
    walk,
)

logger = logging.getLogger(__name__)

JOB = "oura-exporter"
SETTLE_DELAY = timedelta(hours=12)
LAST_SECOND = time(23, 59, 59)
DIGITS = frozenset("0123456789")
FUTURE_SKEW_MS = 60_000

type Labels = tuple[tuple[str, str], ...]
type Key = tuple[str, Labels]
type Warned = set[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Point:
    name: str
    labels: Labels
    timestamp_ms: int
    value: float

    @property
    def key(self) -> Key:
        return (self.name, self.labels)


def to_ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


def _warn(warned: Warned, name: str, raw: object, message: str) -> None:
    key = (name, repr(raw)[:100])
    if key not in warned and len(warned) < MAX_WARNED:
        warned.add(key)
        logger.warning("%s: %s %s; the series is omitted", name, message, key[1])


def _convert(metric: Metric, raw: object) -> float | str | None:
    if metric.type == "gauge":
        return gauge_value(metric, raw)
    return str(raw) if isinstance(raw, str | int | float | bool) else None


def _labels(category: Category, document: Document) -> dict[str, str]:
    labels = {"job": JOB}
    for label in category.labels:
        raw = walk(document, label.path)
        if isinstance(raw, str) and raw:
            labels[label.name] = raw
    return labels


def _freeze(labels: Mapping[str, str]) -> Labels:
    return tuple(sorted(labels.items()))


def _scalar_points(
    category: Category,
    document: Document,
    labels: Mapping[str, str],
    timestamp_ms: int,
    warned: Warned,
) -> list[Point]:
    points: list[Point] = []
    for metric in category.metrics:
        raw = walk(document, metric.path)
        if raw is None:
            continue
        value = _convert(metric, raw)
        if value is None:
            _warn(warned, metric.full_name, raw, "ignoring unexpected value")
            continue
        if metric.type == "info":
            points.append(
                Point(
                    metric.exposed_name,
                    _freeze({**labels, metric.name: str(value)}),
                    timestamp_ms,
                    1.0,
                )
            )
        else:
            points.append(Point(metric.exposed_name, _freeze(labels), timestamp_ms, float(value)))
    return points


def _duration_points(
    category: Category,
    document: Document,
    labels: Mapping[str, str],
    start: datetime,
    warned: Warned,
) -> list[Point]:
    if category.end_path is None or category.duration_name is None:
        return []
    raw = walk(document, category.end_path)
    if raw is None:
        seconds = 0.0 if category.no_end == "zero" else None
    else:
        end = parse_datetime(raw)
        if end is None:
            _warn(warned, category.duration_name, raw, "ignoring unparseable end time")
            return []
        seconds = (end - start).total_seconds()
    if seconds is None or seconds < 0:
        return []
    return [Point(category.duration_name, _freeze(labels), to_ms(start), seconds)]


def _sample_points(
    series: Series, document: Document, labels: Mapping[str, str], warned: Warned
) -> list[Point]:
    raw = walk(document, series.path)
    if raw is None:
        return []
    frozen = _freeze(labels)
    if series.type == "samples":
        if not isinstance(raw, Mapping):
            _warn(warned, series.full_name, raw, "ignoring unexpected series")
            return []
        start = parse_datetime(raw.get("timestamp"))
        interval = number(raw.get("interval"))
        items = raw.get("items")
        if start is None or interval is None or interval <= 0 or not isinstance(items, list):
            _warn(warned, series.full_name, raw, "ignoring unexpected series")
            return []
        values: Iterable[tuple[int, float | None]] = (
            (index, number(item)) for index, item in enumerate(items)
        )
    else:
        start = parse_datetime(walk(document, series.start or ()))
        interval = series.interval
        if start is None or interval is None or not isinstance(raw, str):
            _warn(warned, series.full_name, raw, "ignoring unexpected series")
            return []
        values = ((index, float(char)) for index, char in enumerate(raw) if char in DIGITS)
    start_ms = to_ms(start)
    points: list[Point] = []
    for index, value in values:
        if value is not None:
            points.append(
                Point(series.full_name, frozen, start_ms + round(index * interval * 1000), value)
            )
    return points


def _horizon_ms(series: Series, document: Document) -> int | None:
    raw = walk(document, series.path)
    start = parse_datetime(walk(document, series.start or ()))
    if not isinstance(raw, str) or not raw or start is None or series.interval is None:
        return None
    return to_ms(start) + round((len(raw) - 1) * series.interval * 1000)


def _document_points(
    category: Category,
    document: Document,
    labels: Mapping[str, str],
    timestamp_ms: int | None,
    warned: Warned,
    *,
    span_start: datetime | None = None,
    held_back: bool = False,
) -> list[Point]:
    points: list[Point] = []
    for series in category.series:
        points += _sample_points(series, document, labels, warned)
    if held_back and category.horizon_series is not None:
        limit = _horizon_ms(category.horizon_series, document)
        points = [point for point in points if limit is not None and point.timestamp_ms < limit]
    if span_start is not None:
        points += _duration_points(category, document, labels, span_start, warned)
    if timestamp_ms is not None:
        points += _scalar_points(category, document, labels, timestamp_ms, warned)
    return points


def settled_at(day: date, now: datetime) -> datetime | None:
    end = datetime.combine(day, LAST_SECOND).astimezone()
    return end if now >= end + timedelta(seconds=1) + SETTLE_DELAY else None


def build_points(
    category: Category,
    documents: Iterable[Document],
    now: datetime,
    *,
    live: bool,
    cutoff: datetime | None = None,
    warned: Warned | None = None,
) -> list[Point]:
    seen = warned if warned is not None else set()
    points: list[Point] = []
    if category.kind == "daily":
        points = _daily_points(category, documents, now, live, seen)
    elif category.kind == "single":
        for document in documents:
            if live:
                points += _document_points(
                    category, document, _labels(category, document), to_ms(now), seen
                )
    else:
        for document in documents:
            raw = walk(document, category.time_path or ())
            moment = parse_datetime(raw)
            if moment is None:
                _warn(seen, f"{category.prefix}timestamp", raw, "ignoring unparseable time")
                continue
            if not _settled_event(category, document, moment, now):
                continue
            span = moment if category.kind == "event" else None
            points += _document_points(
                category,
                document,
                _labels(category, document),
                to_ms(moment),
                seen,
                span_start=span,
            )
    if cutoff is not None:
        cutoff_ms = to_ms(cutoff)
        points = [point for point in points if point.timestamp_ms >= cutoff_ms]
    return _drop_future(category, points, now, seen)


def _settled_event(category: Category, document: Document, moment: datetime, now: datetime) -> bool:
    if category.settle_delay <= 0:
        return True
    reference = moment
    if category.end_path is not None:
        end = parse_datetime(walk(document, category.end_path))
        if end is None:
            return False
        reference = end
    return reference + timedelta(seconds=category.settle_delay) <= now


def _drop_future(
    category: Category, points: list[Point], now: datetime, seen: Warned
) -> list[Point]:
    horizon = to_ms(now) + FUTURE_SKEW_MS
    ahead = [point for point in points if point.timestamp_ms > horizon]
    if not ahead:
        return points
    key = (category.name, "future")
    if key not in seen and len(seen) < MAX_WARNED:
        seen.add(key)
        skew = (max(point.timestamp_ms for point in ahead) - to_ms(now)) // 1000
        logger.info(
            "%s: dropping %d samples dated up to %ds in the future, e.g. %s",
            category.name,
            len(ahead),
            skew,
            ahead[0].name,
        )
    return [point for point in points if point.timestamp_ms <= horizon]


def _daily_points(
    category: Category,
    documents: Iterable[Document],
    now: datetime,
    live: bool,
    warned: Warned,
) -> list[Point]:
    by_day: dict[date, Document] = {}
    for document in documents:
        day = parse_day(document.get("day"))
        if day is not None:
            by_day[day] = document
    newest = max(by_day, default=None)
    points: list[Point] = []
    for day, document in sorted(by_day.items()):
        labels = _labels(category, document)
        settled = settled_at(day, now)
        if live and day == newest:
            timestamp_ms: int | None = to_ms(now)
        else:
            timestamp_ms = None if settled is None else to_ms(settled)
        points += _document_points(
            category, document, labels, timestamp_ms, warned, held_back=settled is None
        )
    return points


class DeliveryLog:
    def __init__(self) -> None:
        self._values: dict[tuple[Key, int], float] = {}

    def __len__(self) -> int:
        return len(self._values)

    def split(self, points: Iterable[Point]) -> tuple[list[Point], list[Point]]:
        unique: dict[tuple[Key, int], Point] = {}
        for point in points:
            unique[(point.key, point.timestamp_ms)] = point
        new: list[Point] = []
        revised: list[Point] = []
        for slot, point in unique.items():
            known = self._values.get(slot)
            if known is None:
                new.append(point)
            elif known != point.value:
                revised.append(point)
        return new, revised

    def record(self, points: Iterable[Point]) -> None:
        for point in points:
            self._values[(point.key, point.timestamp_ms)] = point.value

    def note(self, revised: Iterable[Point]) -> None:
        for point in revised:
            slot = (point.key, point.timestamp_ms)
            logger.debug(
                "%s: value revised at %d (%s -> %s), not sent",
                point.name,
                point.timestamp_ms,
                self._values[slot],
                point.value,
            )
            self._values[slot] = point.value

    def prune(self, cutoff: datetime) -> None:
        cutoff_ms = to_ms(cutoff)
        self._values = {slot: value for slot, value in self._values.items() if slot[1] >= cutoff_ms}
