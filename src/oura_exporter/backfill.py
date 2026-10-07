import logging
from collections.abc import Sequence
from datetime import date, datetime, time
from pathlib import Path

from oura_exporter.api import OuraClient
from oura_exporter.definitions import Category, help_texts
from oura_exporter.fetching import DocumentFetcher
from oura_exporter.openmetrics import render_openmetrics
from oura_exporter.points import Point, build_points

logger = logging.getLogger(__name__)


def backfill(
    client: OuraClient,
    categories: Sequence[Category],
    start: date,
    end: date,
    output: Path,
    now: datetime,
) -> int:
    first = datetime.combine(start, time.min).astimezone()
    last = datetime.combine(end, time(23, 59, 59)).astimezone()
    fetcher = DocumentFetcher(client)
    warned: set[tuple[str, str]] = set()
    points: list[Point] = []
    for category in categories:
        if category.kind == "single":
            continue
        documents = fetcher.fetch(category, first, last)
        found = build_points(category, documents, now, live=False, warned=warned)
        logger.info("%s: %d documents, %d samples", category.name, len(documents), len(found))
        points += found
    output.write_text(render_openmetrics(points, help_texts(categories)), encoding="utf-8")
    return len(points)
