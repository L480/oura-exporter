import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
import responses

from oura_exporter import __main__ as cli
from oura_exporter.backfill import backfill
from oura_exporter.config import ConfigError, Settings
from oura_exporter.definitions import help_texts, load_definitions
from oura_exporter.openmetrics import render_openmetrics
from oura_exporter.points import JOB, Point
from oura_exporter.storage import TokenStore

from .helpers import WALL, Rig, epoch, register_endpoints
from .test_cli import base_env, seed_token

NOW = datetime.fromtimestamp(WALL, tz=UTC)
SAMPLE = re.compile(r"^([a-z0-9_]+)(\{[^}]*\})? (\S+) (\d+\.\d{3})$")


def run(rig: Rig, output: Path, start: date, end: date, now: datetime = NOW) -> int:
    return backfill(rig.exporter._fetcher._client, load_definitions(), start, end, output, now)


class TestRender:
    def point(self, metric: str, value: float, timestamp: int, **labels: str) -> Point:
        return Point(metric, tuple(sorted({"job": JOB, **labels}.items())), timestamp, value)

    def test_families_series_and_samples_are_ordered(self) -> None:
        text = render_openmetrics(
            [
                self.point("b", 2.0, 2000),
                self.point("a", 3.0, 3000, x="2"),
                self.point("a", 1.0, 1500, x="2"),
                self.point("a", 9.0, 500, x="1"),
                self.point("a", 4.0, 3000, x="2"),
            ],
            {"a": "A help."},
        )
        assert text.splitlines() == [
            "# HELP a A help.",
            "# TYPE a gauge",
            'a{job="oura-exporter",x="1"} 9.0 0.500',
            'a{job="oura-exporter",x="2"} 1.0 1.500',
            'a{job="oura-exporter",x="2"} 4.0 3.000',
            "# TYPE b gauge",
            'b{job="oura-exporter"} 2.0 2.000',
            "# EOF",
        ]

    def test_label_values_and_help_are_escaped(self) -> None:
        text = render_openmetrics(
            [self.point("a", 1.0, 1000, title='say "hi"\\\nnow')], {"a": "line\\one\ntwo"}
        )
        assert "# HELP a line\\\\one\\ntwo" in text
        assert 'title="say \\"hi\\"\\\\\\nnow"' in text

    def test_series_without_labels_have_no_braces(self) -> None:
        assert "\na 1.0 1.000\n" in render_openmetrics([Point("a", (), 1000, 1.0)], {})

    def test_an_empty_export_is_just_the_terminator(self) -> None:
        assert render_openmetrics([], {}) == "# EOF\n"

    def test_timestamps_keep_milliseconds(self) -> None:
        assert "1791270180.123" in render_openmetrics([Point("a", (), 1_791_270_180_123, 1.0)], {})


class TestBackfill:
    def test_writes_openmetrics_with_measurement_times(self, rig: Rig, tmp_path: Path) -> None:
        output = tmp_path / "out.om"
        count = run(rig, output, date(2026, 10, 1), date(2026, 10, 6))
        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[-1] == "# EOF"
        assert sum(1 for line in lines if SAMPLE.match(line)) == count
        text = "\n".join(lines)
        assert (
            'oura_heartrate_bpm{job="oura-exporter"} 99.0 '
            f"{epoch('2026-10-02T12:00:00+00:00'):.3f}" in text
        )
        assert "# HELP oura_heartrate_bpm Heart rate, in beats per minute." in text
        assert "# TYPE oura_heartrate_bpm gauge" in text

    def test_completed_days_are_stamped_at_their_last_second_and_nothing_at_fetch_time(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.om"
        run(rig, output, date(2026, 10, 1), date(2026, 10, 6))
        scores = [
            line
            for line in output.read_text(encoding="utf-8").splitlines()
            if line.startswith("oura_daily_readiness_score")
        ]
        assert scores == [
            'oura_daily_readiness_score{job="oura-exporter"} 74.0 '
            f"{epoch('2026-10-04T23:59:59+00:00'):.3f}",
            'oura_daily_readiness_score{job="oura-exporter"} 80.0 '
            f"{epoch('2026-10-05T23:59:59+00:00'):.3f}",
        ]
        assert "oura_personal_info" not in output.read_text(encoding="utf-8")
        assert "oura_ring_size" not in output.read_text(encoding="utf-8")
        assert len(rig.calls("personal_info")) == 0

    def test_a_later_run_includes_the_newest_day(self, rig: Rig, tmp_path: Path) -> None:
        output = tmp_path / "out.om"
        later = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        run(rig, output, date(2026, 10, 1), date(2026, 10, 6), later)
        assert epoch("2026-10-06T23:59:59+00:00").__format__(".3f") in output.read_text(
            encoding="utf-8"
        )

    def test_the_range_is_requested_in_chunks(self, rig: Rig, tmp_path: Path) -> None:
        run(rig, tmp_path / "out.om", date(2026, 7, 1), date(2026, 10, 6))
        heart = rig.calls("heartrate")
        assert len(heart) == 14
        assert heart[0].request.params["start_datetime"].startswith("2026-07-01")
        assert len(rig.calls("sleep")) == 4
        assert rig.calls("sleep")[0].request.params["start_date"] == "2026-07-01"

    def test_the_file_is_valid_input_for_every_sample_line(self, rig: Rig, tmp_path: Path) -> None:
        output = tmp_path / "out.om"
        run(rig, output, date(2026, 10, 1), date(2026, 10, 6))
        helps = help_texts(load_definitions())
        seen: set[str] = set()
        for line in output.read_text(encoding="utf-8").splitlines():
            if line.startswith("# HELP "):
                assert line.split(" ", 3)[2] in helps
            elif line.startswith("# TYPE "):
                seen.add(line.split(" ")[2])
            elif line != "# EOF":
                match = SAMPLE.match(line)
                assert match, line
                assert match.group(1) in seen

    def test_failures_propagate(self, rig: Rig, tmp_path: Path) -> None:
        rig.rsps.replace(
            responses.GET,
            "https://oura.test/v2/usercollection/sleep",
            body=requests.ConnectionError("x"),
        )
        with pytest.raises(Exception, match="sleep"):
            run(rig, tmp_path / "out.om", date(2026, 10, 1), date(2026, 10, 6))
        assert not (tmp_path / "out.om").exists()


class TestCommand:
    @pytest.fixture
    def frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Frozen(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Frozen:  # type: ignore[override]
                return cls.fromtimestamp(WALL, tz)

        class Day(date):
            @classmethod
            def today(cls) -> Day:
                return cls(2026, 10, 6)

        monkeypatch.setattr(cli, "datetime", Frozen)
        monkeypatch.setattr(cli, "date", Day)

    def env(self, tmp_path: Path) -> dict[str, str]:
        env = base_env(tmp_path, OURA_API_BASE_URL="https://oura.test")
        del env["OURA_REMOTE_WRITE_URL"]
        return env

    def test_backfill_needs_no_remote_write_url_and_no_server(
        self,
        tmp_path: Path,
        rsps: responses.RequestsMock,
        frozen: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        seed_token(tmp_path / "data")
        register_endpoints(rsps)
        output = tmp_path / "out.om"
        with caplog.at_level(logging.INFO):
            code = cli.main(
                [
                    "backfill",
                    "--start",
                    "2026-10-04",
                    "--end",
                    "2026-10-06",
                    "--output",
                    str(output),
                ],
                self.env(tmp_path),
            )
        assert code == 0
        assert output.read_text(encoding="utf-8").endswith("# EOF\n")
        assert "wrote" in caplog.text
        assert not any(call.request.method == "POST" for call in rsps.calls)
        store = TokenStore(tmp_path / "data" / "oauth_token.json")
        store.acquire_lock()
        store.release_lock()

    def test_the_end_defaults_to_today(
        self, tmp_path: Path, rsps: responses.RequestsMock, frozen: None
    ) -> None:
        seed_token(tmp_path / "data")
        register_endpoints(rsps)
        code = cli.main(
            ["backfill", "--start", "2026-10-04", "--output", str(tmp_path / "out.om")],
            self.env(tmp_path),
        )
        assert code == 0
        params = next(c for c in rsps.calls if "/sleep" in (c.request.url or "")).request.params
        assert params["end_date"] == "2026-10-07"

    def test_an_end_before_the_start_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(
            ["backfill", "--start", "2026-10-06", "--end", "2026-10-01", "--output", "x"],
            self.env(tmp_path),
        )
        assert code == 2
        assert "--end" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["backfill", "--output", "x"],
            ["backfill", "--start", "2026-10-01"],
            ["backfill", "--start", "yesterday", "--output", "x"],
        ],
    )
    def test_bad_arguments_exit_with_usage_errors(self, tmp_path: Path, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as caught:
            cli.main(argv, self.env(tmp_path))
        assert caught.value.code == 2

    def test_a_running_service_blocks_the_backfill(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        env = self.env(tmp_path)
        seed_token(tmp_path / "data")
        running = TokenStore(Path(env["OURA_TOKEN_PATH"]))
        running.prepare()
        running.acquire_lock()
        try:
            with caplog.at_level(logging.ERROR):
                code = cli.main(
                    ["backfill", "--start", "2026-10-04", "--output", str(tmp_path / "o.om")], env
                )
        finally:
            running.release_lock()
        assert code == 2
        assert "another oura-exporter instance" in caplog.text

    def test_configuration_errors_exit_with_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["backfill", "--start", "2026-10-04", "--output", "x"], {})
        assert code == 2
        assert "OURA_CLIENT_ID" in capsys.readouterr().err

    def test_serving_without_a_url_is_a_configuration_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = self.env(tmp_path)
        assert cli.main([], env) == 2
        assert "OURA_REMOTE_WRITE_URL is required" in capsys.readouterr().err
        settings = Settings.from_env(env, remote_write=False)
        with pytest.raises(ConfigError, match="OURA_REMOTE_WRITE_URL"):
            cli._serve(settings)
