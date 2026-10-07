import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import cramjam
import requests
import responses
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from prometheus_client import generate_latest

from oura_exporter.api import OuraClient, build_session
from oura_exporter.auth import OAuthClient, TokenManager
from oura_exporter.definitions import load_definitions
from oura_exporter.exporter import Exporter
from oura_exporter.remote_write import RemoteWriter
from oura_exporter.storage import Token, TokenStore

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://oura.test"
TOKEN_URL = f"{BASE_URL}/oauth/token"
WRITE_URL = "http://prometheus.test/api/v1/write"
ENDPOINTS = (
    "daily_activity",
    "daily_cardiovascular_age",
    "daily_readiness",
    "daily_resilience",
    "daily_sleep",
    "daily_spo2",
    "daily_stress",
    "enhanced_tag",
    "heartrate",
    "personal_info",
    "rest_mode_period",
    "ring_battery_level",
    "ring_configuration",
    "session",
    "sleep",
    "sleep_time",
    "vO2_max",
    "workout",
)
type Labels = dict[str, str]


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def api_url(endpoint: str) -> str:
    return f"{BASE_URL}/v2/usercollection/{endpoint}"


def register_endpoint(
    rsps: responses.RequestsMock, endpoint: str, *, nulls: bool = False, replace: bool = False
) -> None:
    name = (
        f"{endpoint}_nulls"
        if nulls and (FIXTURES / f"{endpoint}_nulls.json").exists()
        else endpoint
    )
    method = rsps.replace if replace else rsps.add
    method(responses.GET, api_url(endpoint), json=load_fixture(name))


def register_endpoints(
    rsps: responses.RequestsMock,
    *,
    nulls: bool = False,
    only: Iterable[str] | None = None,
    replace: bool = False,
) -> None:
    for endpoint in only if only is not None else ENDPOINTS:
        register_endpoint(rsps, endpoint, nulls=nulls, replace=replace)


def epoch(iso: str) -> float:
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


WALL = datetime(2026, 10, 6, 16, 0, tzinfo=UTC).timestamp()
CATEGORY_ORDER = tuple(category.name for category in load_definitions())


@dataclass
class Rig:
    exporter: Exporter
    tokens: TokenManager
    mono: FakeClock
    wall: FakeClock
    rsps: responses.RequestsMock

    def advance(self, seconds: float) -> None:
        self.mono.advance(seconds)
        self.wall.advance(seconds)

    def value(self, name: str, **labels: str) -> float | None:
        return self.exporter.registry.get_sample_value(name, labels or None)

    def up(self, category: str) -> float | None:
        return self.value("oura_exporter_category_up", category=category)

    def errors(self, category: str, reason: str) -> float | None:
        return self.value("oura_exporter_category_errors_total", category=category, reason=reason)

    def calls(self, endpoint: str) -> list[responses.Call]:
        return [
            call
            for call in self.rsps.calls
            if urlsplit(call.request.url or "").path.endswith(f"/{endpoint}")
        ]

    def text(self) -> str:
        return generate_latest(self.exporter.registry).decode()

    def writes(
        self,
    ) -> list[dict[tuple[str, tuple[tuple[str, str], ...]], list[tuple[int, float]]]]:
        return [
            decode_write_request(call.request.body)
            for call in self.rsps.calls
            if (call.request.url or "") == WRITE_URL
        ]

    def pushed(self, name: str, **labels: str) -> list[tuple[int, float]]:
        found: list[tuple[int, float]] = []
        for request in self.writes():
            for (series_name, series_labels), samples in request.items():
                if series_name == name and labels.items() <= dict(series_labels).items():
                    found += samples
        return sorted(found)

    def pushed_count(self) -> int:
        return sum(len(s) for request in self.writes() for s in request.values())


def build_rig(
    tmp_path: Path, rsps: responses.RequestsMock, sessions: list[requests.Session]
) -> Rig:
    session = build_session()
    oauth_session = build_session(retries=False)
    sessions.extend([session, oauth_session])
    store = TokenStore(tmp_path / "data" / "oauth_token.json")
    store.prepare()
    oauth = OAuthClient(
        oauth_session, "cid", "secret", "http://localhost/cb", ("daily",), TOKEN_URL
    )
    wall = FakeClock(WALL)
    mono = FakeClock(1000.0)
    tokens = TokenManager(store, oauth, wall)
    tokens.token = Token(
        "access-1", "refresh-1", datetime.fromtimestamp(WALL + 10 * 86400, tz=UTC), "cid"
    )
    write_session = build_session(retries=False)
    sessions.append(write_session)
    exporter = Exporter(
        OuraClient(session, tokens, BASE_URL),
        tokens,
        load_definitions(),
        RemoteWriter(write_session, WRITE_URL, wall=wall),
        300,
        3,
        monotonic=mono,
        wall=wall,
    )
    register_endpoints(rsps)
    rsps.add(responses.POST, WRITE_URL, status=204)
    return Rig(exporter, tokens, mono, wall, rsps)


def _write_request_class() -> Any:
    pool = descriptor_pool.DescriptorPool()
    proto = descriptor_pb2.FileDescriptorProto(
        name="remote.proto", package="prometheus", syntax="proto3"
    )
    field = descriptor_pb2.FieldDescriptorProto
    label = proto.message_type.add(name="Label")
    label.field.add(name="name", number=1, type=field.TYPE_STRING, label=field.LABEL_OPTIONAL)
    label.field.add(name="value", number=2, type=field.TYPE_STRING, label=field.LABEL_OPTIONAL)
    sample = proto.message_type.add(name="Sample")
    sample.field.add(name="value", number=1, type=field.TYPE_DOUBLE, label=field.LABEL_OPTIONAL)
    sample.field.add(name="timestamp", number=2, type=field.TYPE_INT64, label=field.LABEL_OPTIONAL)
    series = proto.message_type.add(name="TimeSeries")
    series.field.add(
        name="labels",
        number=1,
        type=field.TYPE_MESSAGE,
        type_name=".prometheus.Label",
        label=field.LABEL_REPEATED,
    )
    series.field.add(
        name="samples",
        number=2,
        type=field.TYPE_MESSAGE,
        type_name=".prometheus.Sample",
        label=field.LABEL_REPEATED,
    )
    request = proto.message_type.add(name="WriteRequest")
    request.field.add(
        name="timeseries",
        number=1,
        type=field.TYPE_MESSAGE,
        type_name=".prometheus.TimeSeries",
        label=field.LABEL_REPEATED,
    )
    pool.Add(proto)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("prometheus.WriteRequest"))


WriteRequest = _write_request_class()


def decode_write_request(
    body: bytes | str | None,
) -> dict[tuple[str, tuple[tuple[str, str], ...]], list[tuple[int, float]]]:
    assert isinstance(body, bytes)
    message = WriteRequest()
    message.ParseFromString(bytes(cramjam.snappy.decompress_raw(body)))
    decoded: dict[tuple[str, tuple[tuple[str, str], ...]], list[tuple[int, float]]] = {}
    for series in message.timeseries:
        labels = {label.name: label.value for label in series.labels}
        name = labels.pop("__name__")
        decoded.setdefault((name, tuple(sorted(labels.items()))), []).extend(
            (sample.timestamp, sample.value) for sample in series.samples
        )
    return decoded
