import logging
from datetime import datetime

from oura_exporter.api import Document, OuraApiError, OuraClient
from oura_exporter.definitions import Category

logger = logging.getLogger(__name__)

FIELDS_REJECTED_STATUS = frozenset({400, 422})


class DocumentFetcher:
    def __init__(self, client: OuraClient) -> None:
        self._client = client
        self._without_fields: set[str] = set()

    def fetch(self, category: Category, start: datetime, end: datetime) -> list[Document]:
        if category.kind == "single":
            return self._single(category)
        documents: list[Document] = []
        for window_start, window_end in category.windows(start, end):
            documents += self._window(category, window_start, window_end)
        return documents

    def _single(self, category: Category) -> list[Document]:
        body = self._client.get_document(category.endpoint)
        items = body.get("data")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return [body]

    def _window(self, category: Category, start: datetime, end: datetime) -> list[Document]:
        if category.name not in self._without_fields:
            try:
                return self._client.get_documents(category.endpoint, category.params(start, end))
            except OuraApiError as exc:
                if exc.status_code not in FIELDS_REJECTED_STATUS:
                    raise
                rejected = exc.status_code
            documents = self._client.get_documents(
                category.endpoint, category.params(start, end, with_fields=False)
            )
            self._without_fields.add(category.name)
            logger.warning(
                "%s: Oura rejected the fields parameter (HTTP %s); not sending it again",
                category.name,
                rejected,
            )
            return documents
        return self._client.get_documents(
            category.endpoint, category.params(start, end, with_fields=False)
        )
