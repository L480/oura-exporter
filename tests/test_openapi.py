import json
from pathlib import Path
from typing import Any

import pytest

from oura_exporter.config import DEFAULT_SCOPES
from oura_exporter.definitions import Category, load_definitions

SPEC = json.loads((Path(__file__).parent / "fixtures" / "openapi.json").read_text(encoding="utf-8"))
SCHEMAS = SPEC["components"]["schemas"]
PREFIX = "/v2/usercollection/"
NUMERIC = {"integer", "number", "boolean", "enum"}

SKIPPED_ENDPOINTS = {
    "tag": "deprecated by Oura in favour of enhanced_tag",
}
SKIPPED_FIELDS = {
    ("heartrate", "timestamp_unix"): "the same instant as timestamp, in milliseconds",
    ("ring_battery_level", "timestamp_unix"): "the same instant as timestamp, in milliseconds",
    ("sleep", "period"): "internal period identifier, not a measurement",
}
SCOPES_NOT_IN_SPEC = {"stress": "requested since 0.2.0, missing from the OpenAPI scope list"}
type Leaf = tuple[tuple[str, ...], str, str]


def resolve(node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = SCHEMAS[node["$ref"].rsplit("/", 1)[-1]]
    return node


def variants(node: dict[str, Any]) -> list[dict[str, Any]]:
    node = resolve(node)
    if "anyOf" in node:
        return [v for v in (resolve(option) for option in node["anyOf"]) if v.get("type") != "null"]
    if "allOf" in node:
        return [resolve(option) for option in node["allOf"]]
    return [node]


def leaves(node: dict[str, Any], path: tuple[str, ...] = ()) -> list[Leaf]:
    found: list[Leaf] = []
    for variant in variants(node):
        kind = variant.get("type")
        if "enum" in variant:
            found.append((path, "enum", ""))
        elif kind == "object" and "properties" in variant:
            for name, child in variant["properties"].items():
                found += leaves(child, (*path, name))
        elif kind == "array":
            found += leaves(variant["items"], (*path, "[]"))
        elif kind in {"object", None}:
            continue
        else:
            found.append((path, kind, variant.get("description", "")))
    return found


def endpoints() -> dict[str, list[Leaf]]:
    result: dict[str, list[Leaf]] = {}
    for path, methods in SPEC["paths"].items():
        if not path.startswith(PREFIX) or "{" in path:
            continue
        schema = methods["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        first = variants(schema)[0]
        if "data" in first.get("properties", {}):
            result[path.removeprefix(PREFIX)] = leaves(first["properties"]["data"]["items"])
        else:
            result[path.removeprefix(PREFIX)] = leaves(schema)
    return result


ENDPOINTS = endpoints()


def mapped(category: Category) -> set[tuple[str, ...]]:
    paths = {metric.path for metric in category.metrics}
    paths |= {label.path for label in category.labels}
    for optional in (category.time_path, category.end_path):
        if optional is not None:
            paths.add(optional)
    for series in category.series:
        paths.add(series.path)
        if series.start is not None:
            paths.add(series.start)
    return paths


def covered(path: tuple[str, ...], paths: set[tuple[str, ...]]) -> bool:
    return any(path[: len(known)] == known for known in paths)


def category_for(endpoint: str) -> Category | None:
    return next((c for c in load_definitions() if c.endpoint == endpoint), None)


def requires_mapping(kind: str, description: str) -> bool:
    return kind in NUMERIC or "every character corresponds" in description


def test_the_spec_lists_the_endpoints_we_know() -> None:
    assert len(ENDPOINTS) >= 19
    assert {"heartrate", "sleep", "personal_info", "vO2_max", "ring_configuration"} <= set(
        ENDPOINTS
    )


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
def test_every_endpoint_is_mapped_or_skipped(endpoint: str) -> None:
    if endpoint in SKIPPED_ENDPOINTS:
        assert category_for(endpoint) is None
        return
    assert category_for(endpoint) is not None, f"{endpoint} has no category in metrics.yml"


@pytest.mark.parametrize("endpoint", sorted(set(ENDPOINTS) - set(SKIPPED_ENDPOINTS)))
def test_every_numeric_boolean_and_enum_field_is_mapped_or_skipped(endpoint: str) -> None:
    category = category_for(endpoint)
    assert category is not None
    paths = mapped(category)
    missing = [
        ".".join(path)
        for path, kind, description in ENDPOINTS[endpoint]
        if requires_mapping(kind, description)
        and not covered(path, paths)
        and (endpoint, ".".join(path)) not in SKIPPED_FIELDS
    ]
    assert not missing, f"{endpoint}: unmapped fields {missing}"


@pytest.mark.parametrize("endpoint", sorted(set(ENDPOINTS) - set(SKIPPED_ENDPOINTS)))
def test_every_configured_path_exists_in_the_spec(endpoint: str) -> None:
    category = category_for(endpoint)
    assert category is not None
    known = {path for path, _, _ in ENDPOINTS[endpoint]}
    series_roots = {series.path for series in category.series}
    for path in mapped(category):
        assert path in known or path in series_roots or (*path, "[]") in known, (
            endpoint,
            ".".join(path),
        )
    for series in category.series:
        assert any(known_path[: len(series.path)] == series.path for known_path in known)


def test_skipped_fields_exist_and_are_really_unmapped() -> None:
    for (endpoint, dotted), reason in SKIPPED_FIELDS.items():
        assert reason
        assert tuple(dotted.split(".")) in {path for path, _, _ in ENDPOINTS[endpoint]}
        category = category_for(endpoint)
        assert category is not None
        assert not covered(tuple(dotted.split(".")), mapped(category))


def test_skipped_endpoints_are_in_the_spec() -> None:
    assert set(SKIPPED_ENDPOINTS) <= set(ENDPOINTS)


def test_pii_and_free_text_are_never_mapped() -> None:
    free_text = {
        "personal_info": {"email"},
        "workout": {"label"},
        "enhanced_tag": {"comment"},
    }
    for endpoint, names in free_text.items():
        category = category_for(endpoint)
        assert category is not None
        assert not {path[0] for path in mapped(category)} & names


def test_default_scopes_are_known_scopes() -> None:
    spec_scopes = set(
        SPEC["components"]["securitySchemes"]["OAuth2"]["flows"]["authorizationCode"]["scopes"]
    )
    assert set(DEFAULT_SCOPES) <= spec_scopes | set(SCOPES_NOT_IN_SPEC)
    assert "email" not in DEFAULT_SCOPES
    assert {"workout", "session", "tag", "heart_health"} <= set(DEFAULT_SCOPES)
