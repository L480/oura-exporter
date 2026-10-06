import os
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.metrics_core import Metric

from .helpers import Rig

GOLDEN = Path(__file__).resolve().parents[1] / "example" / "oura.prom"


class Fixed:
    def __init__(self, families: list[Metric]) -> None:
        self._families = families

    def collect(self) -> list[Metric]:
        return self._families


def data_metrics(registry: CollectorRegistry) -> str:
    families = [
        family
        for family in registry.collect()
        if family.name.startswith("oura_") and not family.name.startswith("oura_exporter_")
    ]
    filtered = CollectorRegistry()
    filtered.register(Fixed(families))
    return generate_latest(filtered).decode()


def test_exposition_matches_the_example_file(rig: Rig) -> None:
    rig.exporter.poll()
    text = data_metrics(rig.exporter.registry)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_example_file_contains_no_personal_data(rig: Rig) -> None:
    rig.exporter.poll()
    text = data_metrics(rig.exporter.registry)
    assert "oura_exporter_" not in text
    assert "process_" not in text
    assert "python_info" not in text
    assert "email" not in text
    assert "example.invalid" not in text
