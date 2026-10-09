import requests

from oura_exporter.points import Point
from oura_exporter.remote_write import encode_write_request, snappy_literal

from .helpers import WALL, FakeClock, FakeTsdb

WALL_MS = round(WALL * 1000)


def point(name: str, timestamp_ms: int, value: float) -> Point:
    return Point(name, (("job", "t"),), timestamp_ms, value)


def push(tsdb: FakeTsdb, *points: Point) -> tuple[int, str]:
    body = snappy_literal(encode_write_request(points))
    request = requests.Request("POST", "http://tsdb.test/write", data=body).prepare()
    status, _, text = tsdb.handle(request)
    return status, text


def stored(tsdb: FakeTsdb, name: str) -> list[tuple[int, float]]:
    return sorted(
        item
        for (series, _), values in tsdb.samples.items()
        if series == name
        for item in values.items()
    )


def make(ooo_window_s: float = 3600) -> FakeTsdb:
    return FakeTsdb(FakeClock(WALL), ooo_window_s)


def test_in_order_samples_are_appended() -> None:
    tsdb = make()
    assert push(tsdb, point("a", 1000, 1), point("a", 2000, 2)) == (204, "")
    assert stored(tsdb, "a") == [(1000, 1), (2000, 2)]


def test_same_value_at_head_is_a_no_op() -> None:
    tsdb = make()
    push(tsdb, point("a", 1000, 1))
    assert push(tsdb, point("a", 1000, 1))[0] == 204
    assert stored(tsdb, "a") == [(1000, 1)]


def test_other_value_at_head_is_a_duplicate_conflict() -> None:
    tsdb = make()
    push(tsdb, point("a", 1000, 1))
    status, text = push(tsdb, point("a", 1000, 2))
    assert status == 400
    assert (
        text
        == "duplicate sample for timestamp 1000; overrides not allowed: existing 1.0, new value 2.0"
    )
    assert stored(tsdb, "a") == [(1000, 1)]


def test_older_sample_is_stored_when_new() -> None:
    tsdb = make()
    push(tsdb, point("a", 2000, 2))
    assert push(tsdb, point("a", 1000, 1))[0] == 204
    assert stored(tsdb, "a") == [(1000, 1), (2000, 2)]


def test_older_sample_keeps_the_first_value_silently() -> None:
    tsdb = make()
    push(tsdb, point("a", 1000, 1), point("a", 2000, 2))
    assert push(tsdb, point("a", 1000, 9))[0] == 204
    assert stored(tsdb, "a") == [(1000, 1), (2000, 2)]


def test_sample_beyond_the_ooo_window_is_out_of_bounds() -> None:
    tsdb = make(ooo_window_s=10)
    push(tsdb, point("a", 100_000, 1))
    status, text = push(tsdb, point("a", 89_999, 1))
    assert status == 400
    assert "out of bounds" in text
    assert push(tsdb, point("a", 90_000, 1))[0] == 204


def test_window_is_measured_from_the_newest_sample_of_any_series() -> None:
    tsdb = make(ooo_window_s=10)
    push(tsdb, point("a", 5000, 1), point("b", 100_000, 1))
    assert push(tsdb, point("a", 2000, 1))[0] == 400
    assert push(tsdb, point("a", 95_000, 1))[0] == 204


def test_future_samples_are_out_of_bounds() -> None:
    tsdb = make()
    assert push(tsdb, point("a", WALL_MS + 600_000, 1))[0] == 204
    status, text = push(tsdb, point("b", WALL_MS + 600_001, 1))
    assert (status, text) == (400, "out of bounds: timestamp is too far in the future")


def test_a_conflict_in_the_second_series_rolls_back_the_first() -> None:
    tsdb = make()
    push(tsdb, point("b", 1000, 1))
    status, _ = push(tsdb, point("a", 1000, 1), point("a", 2000, 2), point("b", 1000, 5))
    assert status == 400
    assert stored(tsdb, "a") == []
    assert "a" not in {series for series, _ in tsdb.head}
    assert stored(tsdb, "b") == [(1000, 1)]


def test_a_conflict_inside_one_series_rolls_back_its_earlier_samples() -> None:
    tsdb = make()
    push(tsdb, point("a", 3000, 3))
    status, _ = push(tsdb, point("a", 1000, 1), point("a", 3000, 4))
    assert status == 400
    assert stored(tsdb, "a") == [(3000, 3)]
