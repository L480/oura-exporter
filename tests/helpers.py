import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import responses
from prometheus_client import generate_latest

from oura_exporter.api import OuraClient, build_session
from oura_exporter.auth import OAuthClient, TokenManager
from oura_exporter.definitions import load_definitions
from oura_exporter.exporter import Exporter
from oura_exporter.storage import Token, TokenStore

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://oura.test"
TOKEN_URL = f"{BASE_URL}/oauth/token"
TODAY = date(2026, 10, 6)
ENDPOINTS = (
    "daily_activity",
    "daily_readiness",
    "daily_resilience",
    "daily_sleep",
    "daily_spo2",
    "daily_stress",
    "sleep",
    "heartrate",
    "ring_battery_level",
    "personal_info",
)


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


WALL = 1_800_000_000.0


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
    exporter = Exporter(
        OuraClient(session, tokens, BASE_URL),
        tokens,
        load_definitions(),
        300,
        monotonic=mono,
        wall=wall,
        today=lambda: TODAY,
    )
    register_endpoints(rsps)
    return Rig(exporter, tokens, mono, wall, rsps)
