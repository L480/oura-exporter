import logging
import os

import cramjam
import pytest
import requests
import responses

from oura_exporter.api import build_session
from oura_exporter.points import JOB, Point
from oura_exporter.remote_write import (
    BATCH_SIZE,
    RemoteWriter,
    encode_write_request,
    snappy_literal,
)

from .helpers import decode_write_request

URL = "http://prometheus.test/api/v1/write"


def point(value: float = 1.0, timestamp: int = 1_791_270_180_000, name: str = "oura_x") -> Point:
    return Point(name, (("job", JOB),), timestamp, value)


@pytest.fixture
def writer(rsps: responses.RequestsMock) -> RemoteWriter:
    session = build_session(retries=False)
    yield_writer = RemoteWriter(session, URL, wall=lambda: 1234.0)
    return yield_writer


class TestEncoding:
    def test_matches_a_real_protobuf_decoder(self) -> None:
        points = [
            Point("oura_b", (("job", JOB), ("zone", "a")), 2000, -0.5),
            Point("oura_a", (("job", JOB),), 3000, 2.0),
            Point("oura_a", (("job", JOB),), 1000, float("inf")),
            Point("oura_a", (("job", JOB),), 1_791_270_180_123, 1e300),
            Point("oura_ü", (("job", JOB), ("name", "ünï ☃")), 0, 0.0),
        ]
        decoded = decode_write_request(snappy_literal(encode_write_request(points)))
        assert decoded[("oura_a", (("job", JOB),))] == [
            (1000, float("inf")),
            (3000, 2.0),
            (1_791_270_180_123, 1e300),
        ]
        assert decoded[("oura_b", (("job", JOB), ("zone", "a")))] == [(2000, -0.5)]
        assert decoded[("oura_ü", (("job", JOB), ("name", "ünï ☃")))] == [(0, 0.0)]

    def test_labels_are_sorted_with_the_name_included(self) -> None:
        body = encode_write_request([Point("m", (("a", "1"), ("job", JOB)), 1, 1.0)])
        names = [bytes(name) for name in (b"__name__", b"a", b"job")]
        positions = [body.index(name) for name in names]
        assert positions == sorted(positions)

    def test_samples_of_a_series_are_sorted_by_time(self) -> None:
        body = encode_write_request([point(1.0, 3000), point(2.0, 1000), point(3.0, 2000)])
        decoded = decode_write_request(snappy_literal(body))
        assert next(iter(decoded.values())) == [(1000, 2.0), (2000, 3.0), (3000, 1.0)]

    def test_an_empty_request_is_empty(self) -> None:
        assert encode_write_request([]) == b""


class TestSnappy:
    @pytest.mark.parametrize(
        "size", [0, 1, 59, 60, 61, 255, 256, 257, 300, 65535, 65536, 65537, 140_000]
    )
    def test_round_trips_through_a_real_decoder(self, size: int) -> None:
        data = os.urandom(size)
        assert bytes(cramjam.snappy.decompress_raw(snappy_literal(data))) == data

    def test_repetitive_data_round_trips_too(self) -> None:
        data = b"abc" * 50_000
        assert bytes(cramjam.snappy.decompress_raw(snappy_literal(data))) == data

    def test_starts_with_the_uncompressed_length(self) -> None:
        assert snappy_literal(b"") == b"\x00"
        assert snappy_literal(b"hi") == b"\x02\x04hi"
        assert snappy_literal(b"x" * 128)[:2] == b"\x80\x01"


class TestSending:
    def test_posts_the_documented_headers(
        self, writer: RemoteWriter, rsps: responses.RequestsMock
    ) -> None:
        rsps.add(responses.POST, URL, status=204)
        delivery = writer.send([point()])
        request = rsps.calls[0].request
        assert request.headers["Content-Type"] == "application/x-protobuf"
        assert request.headers["Content-Encoding"] == "snappy"
        assert request.headers["X-Prometheus-Remote-Write-Version"] == "0.1.0"
        assert request.headers["User-Agent"].startswith("oura-exporter/")
        assert "Authorization" not in request.headers
        assert decode_write_request(request.body) == {
            ("oura_x", (("job", JOB),)): [(1_791_270_180_000, 1.0)]
        }
        assert (delivery.sent, delivery.rejected, delivery.failure) == (1, 0, None)
        assert delivery.delivered == [point()]
        assert writer.last_success == 1234.0

    def test_basic_auth(self, rsps: responses.RequestsMock) -> None:
        rsps.add(responses.POST, URL, status=200)
        with build_session(retries=False) as session:
            RemoteWriter(session, URL, "user", "secret").send([point()])
        assert rsps.calls[0].request.headers["Authorization"] == "Basic dXNlcjpzZWNyZXQ="

    def test_batches_hold_at_most_5000_samples_and_stay_in_order(
        self, writer: RemoteWriter, rsps: responses.RequestsMock
    ) -> None:
        rsps.add(responses.POST, URL, status=204)
        points = [point(float(i), 1000 + i) for i in range(BATCH_SIZE * 2 + 7)]
        delivery = writer.send(points)
        sizes = [
            sum(len(samples) for samples in decode_write_request(call.request.body).values())
            for call in rsps.calls
        ]
        assert sizes == [BATCH_SIZE, BATCH_SIZE, 7]
        assert delivery.sent == len(points)
        timestamps = [
            ts
            for call in rsps.calls
            for samples in decode_write_request(call.request.body).values()
            for ts, _ in samples
        ]
        assert timestamps == sorted(timestamps)

    def test_nothing_to_send_makes_no_request(
        self, writer: RemoteWriter, rsps: responses.RequestsMock
    ) -> None:
        assert writer.send([]).sent == 0
        assert len(rsps.calls) == 0

    def test_a_400_counts_as_delivered_and_warns_once_per_kind(
        self, writer: RemoteWriter, rsps: responses.RequestsMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        rsps.add(responses.POST, URL, status=400, body="out of order sample\n")
        with caplog.at_level(logging.DEBUG):
            first = writer.send([point(), point(2.0, 5)])
            second = writer.send([point()])
        assert (first.sent, first.rejected, first.failure) == (0, 2, None)
        assert len(first.delivered) == 2
        assert second.rejected == 1
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "out_of_order" in warnings[0].getMessage()
        assert "out of order sample" in warnings[0].getMessage()
        assert any(
            r.levelno == logging.DEBUG and "rejected" in r.getMessage() for r in caplog.records
        )
        assert writer.last_success == 1234.0

    @pytest.mark.parametrize(
        ("text", "kind"),
        [
            ("duplicate sample for timestamp", "duplicate"),
            ("out of bounds", "out_of_bounds"),
            ("too old sample", "out_of_bounds"),
            ("something else", "other"),
        ],
    )
    def test_rejection_kinds(
        self,
        writer: RemoteWriter,
        rsps: responses.RequestsMock,
        caplog: pytest.LogCaptureFixture,
        text: str,
        kind: str,
    ) -> None:
        rsps.add(responses.POST, URL, status=400, body=text)
        with caplog.at_level(logging.WARNING):
            writer.send([point()])
        assert f"({kind})" in caplog.text

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (429, "rate_limited"),
            (500, "server_error"),
            (503, "server_error"),
            (401, "client_error"),
            (404, "client_error"),
        ],
    )
    def test_other_statuses_are_not_delivered(
        self, writer: RemoteWriter, rsps: responses.RequestsMock, status: int, reason: str
    ) -> None:
        rsps.add(responses.POST, URL, status=status, body="nope")
        delivery = writer.send([point()])
        assert (delivery.sent, delivery.rejected, delivery.failure) == (0, 0, reason)
        assert delivery.delivered == []
        assert writer.last_success is None

    def test_network_errors_are_not_delivered(
        self, writer: RemoteWriter, rsps: responses.RequestsMock
    ) -> None:
        rsps.add(responses.POST, URL, body=requests.ConnectionError("boom"))
        delivery = writer.send([point()])
        assert delivery.failure == "network"
        assert delivery.delivered == []

    def test_a_failing_batch_stops_the_rest_but_keeps_earlier_ones(
        self, writer: RemoteWriter, rsps: responses.RequestsMock
    ) -> None:
        rsps.add(responses.POST, URL, status=204)
        rsps.add(responses.POST, URL, status=503)
        points = [point(float(i), 1000 + i) for i in range(BATCH_SIZE + 3)]
        delivery = writer.send(points)
        assert delivery.sent == BATCH_SIZE
        assert len(delivery.delivered) == BATCH_SIZE
        assert delivery.failure == "server_error"
        assert len(rsps.calls) == 2

    def test_failures_warn_on_change_and_recovery_is_logged(
        self, writer: RemoteWriter, rsps: responses.RequestsMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        rsps.add(responses.POST, URL, status=503, body="down")
        rsps.add(responses.POST, URL, status=503, body="down")
        rsps.add(responses.POST, URL, status=429, body="slow")
        rsps.add(responses.POST, URL, status=204)
        with caplog.at_level(logging.DEBUG):
            for _ in range(4):
                writer.send([point()])
        levels = [(r.levelno, r.getMessage()) for r in caplog.records]
        warnings = [message for level, message in levels if level == logging.WARNING]
        assert len(warnings) == 2
        assert "server_error" in warnings[0]
        assert "HTTP 503: down" in warnings[0]
        assert "rate_limited" in warnings[1]
        assert any(
            "still failing (server_error)" in m for level, m in levels if level == logging.DEBUG
        )
        assert any(
            "recovered after rate_limited" in m for level, m in levels if level == logging.INFO
        )
