import logging
import struct
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

import requests
from requests.auth import HTTPBasicAuth

from oura_exporter.api import TIMEOUT
from oura_exporter.auth import describe_request_error
from oura_exporter.points import Point

logger = logging.getLogger(__name__)

BATCH_SIZE = 5000
SNAPPY_CHUNK = 65536
EXCERPT_LIMIT = 200
HEADERS = {
    "Content-Type": "application/x-protobuf",
    "Content-Encoding": "snappy",
    "X-Prometheus-Remote-Write-Version": "0.1.0",
}
REJECTION_KINDS = (
    ("out_of_order", ("out of order",)),
    ("out_of_bounds", ("out of bounds", "too old")),
    ("duplicate", ("duplicate sample",)),
)


def _varint(number: int) -> bytes:
    number &= (1 << 64) - 1
    out = bytearray()
    while number > 0x7F:
        out.append((number & 0x7F) | 0x80)
        number >>= 7
    out.append(number)
    return bytes(out)


def _delimited(field_number: int, payload: bytes) -> bytes:
    return _varint(field_number << 3 | 2) + _varint(len(payload)) + payload


def _label(name: str, value: str) -> bytes:
    return _delimited(1, name.encode()) + _delimited(2, value.encode())


def _sample(timestamp_ms: int, value: float) -> bytes:
    return b"\x09" + struct.pack("<d", value) + b"\x10" + _varint(timestamp_ms)


def encode_write_request(points: Iterable[Point]) -> bytes:
    series: dict[tuple[str, tuple[tuple[str, str], ...]], list[Point]] = {}
    for point in points:
        series.setdefault(point.key, []).append(point)
    out = bytearray()
    for (name, labels), members in series.items():
        body = bytearray()
        for label_name, label_value in sorted({"__name__": name, **dict(labels)}.items()):
            body += _delimited(1, _label(label_name, label_value))
        for point in sorted(members, key=lambda member: member.timestamp_ms):
            body += _delimited(2, _sample(point.timestamp_ms, point.value))
        out += _delimited(1, bytes(body))
    return bytes(out)


def snappy_literal(data: bytes) -> bytes:
    out = bytearray(_varint(len(data)))
    for start in range(0, len(data), SNAPPY_CHUNK):
        chunk = data[start : start + SNAPPY_CHUNK]
        length = len(chunk) - 1
        if length < 60:
            out.append(length << 2)
        elif length < 256:
            out += bytes((60 << 2, length))
        else:
            out += bytes((61 << 2,)) + length.to_bytes(2, "little")
        out += chunk
    return bytes(out)


def _rejection_kind(text: str) -> str:
    lowered = text.lower()
    for kind, needles in REJECTION_KINDS:
        if any(needle in lowered for needle in needles):
            return kind
    return "other"


def _excerpt(text: str) -> str:
    return " ".join(text.split())[:EXCERPT_LIMIT]


@dataclass(slots=True)
class Delivery:
    delivered: list[Point] = field(default_factory=list)
    sent: int = 0
    rejected: int = 0
    failure: str | None = None


class RemoteWriter:
    def __init__(
        self,
        session: requests.Session,
        url: str,
        username: str | None = None,
        password: str | None = None,
        *,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._session = session
        self._url = url
        self._auth = None if username is None else HTTPBasicAuth(username, password or "")
        self._wall = wall
        self._warned: set[str] = set()
        self._failure: str | None = None
        self.last_success: float | None = None

    def send(self, points: Sequence[Point]) -> Delivery:
        delivery = Delivery()
        ordered = sorted(points, key=lambda point: (point.key, point.timestamp_ms))
        for start in range(0, len(ordered), BATCH_SIZE):
            batch = ordered[start : start + BATCH_SIZE]
            if not self._post(batch, delivery):
                break
        return delivery

    def _post(self, batch: Sequence[Point], delivery: Delivery) -> bool:
        body = snappy_literal(encode_write_request(batch))
        try:
            response = self._session.post(
                self._url, data=body, headers=HEADERS, auth=self._auth, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            self._failed(delivery, "network", describe_request_error(exc))
            return False
        status = response.status_code
        if 200 <= status < 300:
            delivery.sent += len(batch)
        elif status == 400:
            delivery.rejected += len(batch)
            self._warn_rejected(response.text)
        else:
            reason = (
                "rate_limited"
                if status == 429
                else "server_error"
                if status >= 500
                else "client_error"
            )
            self._failed(delivery, reason, f"HTTP {status}: {_excerpt(response.text)}")
            return False
        delivery.delivered.extend(batch)
        self.last_success = self._wall()
        if self._failure is not None:
            logger.info("remote write recovered after %s failure", self._failure)
            self._failure = None
        return True

    def _failed(self, delivery: Delivery, reason: str, detail: str) -> None:
        delivery.failure = reason
        if reason != self._failure:
            logger.warning("remote write failed (%s): %s", reason, detail)
        else:
            logger.debug("remote write still failing (%s): %s", reason, detail)
        self._failure = reason

    def _warn_rejected(self, text: str) -> None:
        kind = _rejection_kind(text)
        if kind in self._warned:
            logger.debug("remote write rejected samples (%s): %s", kind, _excerpt(text))
            return
        self._warned.add(kind)
        logger.warning(
            "remote write: the receiver rejected samples (%s), later ones are logged at debug: %s",
            kind,
            _excerpt(text),
        )
