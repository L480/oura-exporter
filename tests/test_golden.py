import os
from datetime import UTC, datetime
from pathlib import Path

from .helpers import Rig

GOLDEN = Path(__file__).resolve().parents[1] / "example" / "samples.txt"


def dump_samples(rig: Rig) -> str:
    lines: set[tuple[str, str, int, str]] = set()
    for request in rig.writes():
        for (name, labels), samples in request.items():
            selector = ",".join(f'{key}="{value}"' for key, value in labels)
            for timestamp, value in samples:
                lines.add((name, selector, timestamp, repr(value)))
    return "".join(
        f"{name}{{{selector}}} {datetime.fromtimestamp(ts / 1000, tz=UTC).isoformat()} {value}\n"
        for name, selector, ts, value in sorted(lines)
    )


def test_pushed_samples_match_the_example_file(rig: Rig) -> None:
    rig.exporter.poll()
    text = dump_samples(rig)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_example_file_contains_no_personal_data(rig: Rig) -> None:
    rig.exporter.poll()
    text = dump_samples(rig)
    assert "oura_exporter_" not in text
    assert "process_" not in text
    assert "python_info" not in text
    assert "email" not in text
    assert "example.invalid" not in text
    assert "Commute" not in text
    assert "Two beers" not in text
