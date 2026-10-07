from collections.abc import Iterable, Mapping

from oura_exporter.points import Labels, Point


def _escape(text: str, *, quotes: bool = False) -> str:
    escaped = text.replace("\\", "\\\\").replace("\n", "\\n")
    return escaped.replace('"', '\\"') if quotes else escaped


def _selector(name: str, labels: Labels) -> str:
    if not labels:
        return name
    pairs = ",".join(f'{key}="{_escape(value, quotes=True)}"' for key, value in labels)
    return f"{name}{{{pairs}}}"


def render_openmetrics(points: Iterable[Point], helps: Mapping[str, str]) -> str:
    families: dict[str, dict[Labels, dict[int, float]]] = {}
    for point in points:
        families.setdefault(point.name, {}).setdefault(point.labels, {})[point.timestamp_ms] = (
            point.value
        )
    lines: list[str] = []
    for name in sorted(families):
        if name in helps:
            lines.append(f"# HELP {name} {_escape(helps[name])}")
        lines.append(f"# TYPE {name} gauge")
        for labels in sorted(families[name]):
            selector = _selector(name, labels)
            for timestamp_ms, value in sorted(families[name][labels].items()):
                lines.append(f"{selector} {value!r} {timestamp_ms / 1000:.3f}")
    lines.append("# EOF")
    return "\n".join(lines) + "\n"
