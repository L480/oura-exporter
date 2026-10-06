import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    PlatformCollector,
    ProcessCollector,
)
from prometheus_client.metrics_core import (
    GaugeMetricFamily,
    InfoMetricFamily,
    StateSetMetricFamily,
)
from prometheus_client.metrics_core import Metric as MetricFamily

from oura_exporter import __version__
from oura_exporter.api import Document, OuraApiError, OuraClient, RateLimitedError
from oura_exporter.auth import AuthError, TokenManager
from oura_exporter.definitions import Category, Metric, Snapshot, build_snapshot

logger = logging.getLogger(__name__)

FORBIDDEN_RETRY_SECONDS = 3600.0
FIELDS_REJECTED_STATUS = frozenset({400, 422})


def _family(metric: Metric, value: float | str) -> MetricFamily:
    if metric.type == "enum":
        return StateSetMetricFamily(
            metric.full_name, metric.help, {state: state == value for state in metric.states}
        )
    if metric.type == "info":
        return InfoMetricFamily(metric.full_name, metric.help, {metric.name: str(value)})
    return GaugeMetricFamily(metric.full_name, metric.help, float(value))


def _timestamp_help(category: Category) -> str:
    if category.kind == "daily":
        return "Local midnight of the day of the exported document, Unix time in seconds."
    return "Time of the exported sample, Unix time in seconds."


class OuraCollector:
    def __init__(self, categories: Sequence[Category]) -> None:
        self._categories = tuple(categories)
        self._snapshots: dict[str, Snapshot] = {}
        self._lock = threading.Lock()

    def update(self, category: str, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshots[category] = snapshot

    def describe(self) -> list[MetricFamily]:
        return []

    def collect(self) -> Iterator[MetricFamily]:
        with self._lock:
            snapshots = dict(self._snapshots)
        for category in self._categories:
            snapshot = snapshots.get(category.name)
            if snapshot is None:
                continue
            for metric in category.metrics:
                value = snapshot.values.get(metric.name)
                if value is not None:
                    yield _family(metric, value)
            if snapshot.timestamp is not None and category.timestamp_name is not None:
                yield GaugeMetricFamily(
                    category.timestamp_name, _timestamp_help(category), snapshot.timestamp
                )


@dataclass(slots=True)
class CategoryState:
    next_due: float = 0.0
    send_fields: bool = True
    failure: str | None = None


class Exporter:
    def __init__(
        self,
        client: OuraClient,
        tokens: TokenManager,
        categories: Sequence[Category],
        poll_interval: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._client = client
        self._tokens = tokens
        self._categories = tuple(categories)
        self._poll_interval = poll_interval
        self._monotonic = monotonic
        self._wall = wall
        self._today = today
        self._states = {category.name: CategoryState() for category in self._categories}
        self._paused_until = 0.0
        self._warned: set[tuple[str, str]] = set()

        self.registry = CollectorRegistry()
        self._collector = OuraCollector(self._categories)
        self.registry.register(self._collector)
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)

        Gauge(
            "oura_exporter_build_info",
            "Build information of the exporter.",
            ["version"],
            registry=self.registry,
        ).labels(__version__).set(1)
        self._auth_ok = Gauge(
            "oura_exporter_auth_ok",
            "1 if the Oura API accepted the last request, 0 after an authentication failure.",
            registry=self.registry,
        )
        self._auth_ok.set(1)
        Gauge(
            "oura_exporter_token_persisted",
            "1 if the current OAuth token is saved to disk, 0 if saving the rotated token failed.",
            registry=self.registry,
        ).set_function(lambda: 1.0 if tokens.persisted else 0.0)
        self._up = Gauge(
            "oura_exporter_category_up",
            "1 if the last fetch of the category succeeded, 0 if it failed.",
            ["category"],
            registry=self.registry,
        )
        self._last_success = Gauge(
            "oura_exporter_category_last_success_timestamp_seconds",
            "Unix time of the last successful fetch of the category.",
            ["category"],
            registry=self.registry,
        )
        self._errors = Counter(
            "oura_exporter_category_errors",
            "Failed fetches of the category by reason.",
            ["category", "reason"],
            registry=self.registry,
        )

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll(stop)
                self._tokens.retry_persist()
            except Exception:
                logger.exception("poll cycle failed")
            if stop.wait(self._poll_interval):
                return

    def poll(self, stop: threading.Event | None = None) -> None:
        if self._monotonic() < self._paused_until:
            logger.debug("paused after a rate limit; skipping this cycle")
            self._mark_not_refreshed(self._categories)
            return
        for index, category in enumerate(self._categories):
            if stop is not None and stop.is_set():
                return
            state = self._states[category.name]
            now = self._monotonic()
            if now < state.next_due:
                continue
            if not self._poll_category(category, state, now):
                self._mark_not_refreshed(self._categories[index + 1 :])
                return

    def _mark_not_refreshed(self, categories: Sequence[Category]) -> None:
        now = self._monotonic()
        for category in categories:
            if now >= self._states[category.name].next_due:
                self._up.labels(category.name).set(0)

    def _poll_category(self, category: Category, state: CategoryState, now: float) -> bool:
        try:
            snapshot = self._fetch(category, state)
        except RateLimitedError as exc:
            self._paused_until = now + exc.retry_after
            self._failed(category, state, "rate_limited", now, exc)
            return False
        except AuthError as exc:
            self._auth_ok.set(0)
            self._failed(category, state, "auth", now, exc)
            return False
        except OuraApiError as exc:
            self._failed(category, state, exc.reason, now, exc)
            if exc.reason == "auth":
                self._auth_ok.set(0)
                return False
            return True
        except Exception as exc:
            if state.failure != "internal":
                logger.exception("%s: unexpected error while fetching", category.name)
            self._failed(category, state, "internal", now, exc)
            return True
        self._succeeded(category, state, snapshot, now)
        return True

    def _fetch(self, category: Category, state: CategoryState) -> Snapshot:
        if category.kind == "single":
            documents = [self._client.get_document(category.endpoint)]
        else:
            documents = self._fetch_documents(category, state)
        return build_snapshot(category, documents, self._warned)

    def _fetch_documents(self, category: Category, state: CategoryState) -> list[Document]:
        today = self._today()
        if state.send_fields:
            try:
                return self._client.get_documents(category.endpoint, category.params(today))
            except OuraApiError as exc:
                if exc.status_code not in FIELDS_REJECTED_STATUS:
                    raise
                rejected = exc.status_code
            documents = self._client.get_documents(
                category.endpoint, category.params(today, with_fields=False)
            )
            state.send_fields = False
            logger.warning(
                "%s: Oura rejected the fields parameter (HTTP %s); not sending it again",
                category.name,
                rejected,
            )
            return documents
        return self._client.get_documents(
            category.endpoint, category.params(today, with_fields=False)
        )

    def _succeeded(
        self, category: Category, state: CategoryState, snapshot: Snapshot, now: float
    ) -> None:
        self._collector.update(category.name, snapshot)
        self._up.labels(category.name).set(1)
        self._last_success.labels(category.name).set(self._wall())
        self._auth_ok.set(1)
        state.next_due = now + (category.refresh_interval or self._poll_interval)
        if state.failure is not None:
            logger.info("%s: recovered after %s failure", category.name, state.failure)
            state.failure = None
        else:
            logger.debug("%s: updated, %d series", category.name, len(snapshot.values))

    def _failed(
        self, category: Category, state: CategoryState, reason: str, now: float, exc: Exception
    ) -> None:
        self._up.labels(category.name).set(0)
        self._errors.labels(category.name, reason).inc()
        retry_in = FORBIDDEN_RETRY_SECONDS if reason == "forbidden" else 0.0
        state.next_due = now + retry_in
        changed = state.failure != reason
        state.failure = reason
        if reason == "internal":
            if not changed:
                logger.debug("%s: unexpected error again", category.name, exc_info=exc)
        elif changed:
            hint = f"; retrying in {retry_in:.0f}s" if retry_in else ""
            logger.warning("%s: fetch failed (%s): %s%s", category.name, reason, exc, hint)
        else:
            logger.debug("%s: fetch still failing (%s): %s", category.name, reason, exc)
