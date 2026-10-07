import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    PlatformCollector,
    ProcessCollector,
)

from oura_exporter import __version__
from oura_exporter.api import OuraApiError, OuraClient, RateLimitedError
from oura_exporter.auth import AuthError, TokenManager
from oura_exporter.definitions import Category
from oura_exporter.fetching import DocumentFetcher
from oura_exporter.points import DeliveryLog, Point, build_points
from oura_exporter.remote_write import RemoteWriter

logger = logging.getLogger(__name__)

FORBIDDEN_RETRY_SECONDS = 3600.0


@dataclass(slots=True)
class CategoryState:
    next_due: float = 0.0
    failure: str | None = None


class Exporter:
    def __init__(
        self,
        client: OuraClient,
        tokens: TokenManager,
        categories: Sequence[Category],
        writer: RemoteWriter,
        poll_interval: float,
        lookback_days: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._fetcher = DocumentFetcher(client)
        self._writer = writer
        self._lookback = timedelta(days=lookback_days)
        self._log = DeliveryLog()
        self._tokens = tokens
        self._categories = tuple(categories)
        self._poll_interval = poll_interval
        self._monotonic = monotonic
        self._wall = wall
        self._states = {category.name: CategoryState() for category in self._categories}
        self._paused_until = 0.0
        self._cycle_fetched = False
        self._warned: set[tuple[str, str]] = set()

        self.registry = CollectorRegistry()
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
        self._samples = Counter(
            "oura_exporter_remote_write_samples",
            "Samples pushed by result: sent, or rejected by the receiver (a batch answered "
            "with HTTP 400 counts as rejected).",
            ["result"],
            registry=self.registry,
        )
        for result in ("sent", "rejected"):
            self._samples.labels(result)
        self._failures = Counter(
            "oura_exporter_remote_write_failures",
            "Failed remote write requests by reason, retried in the next cycle.",
            ["reason"],
            registry=self.registry,
        )
        self._revisions = Counter(
            "oura_exporter_sample_revisions",
            "Samples whose value changed after they were delivered and were sent again; the "
            "receiver decides which value it keeps.",
            registry=self.registry,
        )
        self._write_success = Gauge(
            "oura_exporter_remote_write_last_success_timestamp_seconds",
            "Unix time of the last successful remote write request.",
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
        self._cycle_fetched = False
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
        moment = datetime.fromtimestamp(self._wall(), tz=UTC)
        cutoff = moment - self._lookback
        try:
            documents = self._fetcher.fetch(category, cutoff, moment)
            points = build_points(
                category, documents, moment, live=True, cutoff=cutoff, warned=self._warned
            )
        except RateLimitedError as exc:
            self._paused_until = now + exc.retry_after
            self._failed(category, state, "rate_limited", now, exc)
            return False
        except AuthError as exc:
            self._auth_ok.set(0)
            self._failed(category, state, "auth", now, exc)
            return False
        except OuraApiError as exc:
            if exc.reason == "auth" and self._cycle_fetched:
                self._failed(category, state, "forbidden", now, exc)
                return True
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
        self._cycle_fetched = True
        self._succeeded(category, state, now)
        self._deliver(category, state, points, cutoff, now)
        return True

    def _deliver(
        self,
        category: Category,
        state: CategoryState,
        points: Sequence[Point],
        cutoff: datetime,
        now: float,
    ) -> None:
        fresh = self._log.fresh(points)
        if fresh:
            delivery = self._writer.send(fresh)
            self._revisions.inc(self._log.record(delivery.delivered))
            self._samples.labels("sent").inc(delivery.sent)
            self._samples.labels("rejected").inc(delivery.rejected)
            if delivery.delivered and self._writer.last_success is not None:
                self._write_success.set(self._writer.last_success)
            if delivery.failure is not None:
                self._failures.labels(delivery.failure).inc()
                state.next_due = now
        self._log.prune(cutoff)
        logger.debug("%s: %d samples in the window, %d new", category.name, len(points), len(fresh))

    def _succeeded(self, category: Category, state: CategoryState, now: float) -> None:
        self._up.labels(category.name).set(1)
        self._last_success.labels(category.name).set(self._wall())
        self._auth_ok.set(1)
        state.next_due = now + (category.refresh_interval or self._poll_interval)
        if state.failure is not None:
            logger.info("%s: recovered after %s failure", category.name, state.failure)
            state.failure = None

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
