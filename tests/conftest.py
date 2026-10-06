import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
import responses

from .helpers import Rig, build_rig


@pytest.fixture(autouse=True)
def utc_timezone() -> Iterator[None]:
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def rig(tmp_path: Path, rsps: responses.RequestsMock) -> Iterator[Rig]:
    sessions: list[requests.Session] = []
    yield build_rig(tmp_path, rsps, sessions)
    for session in sessions:
        session.close()
