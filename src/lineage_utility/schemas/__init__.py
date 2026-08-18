"""Bundled JSON schemas for configuration and canonical lineage graphs."""

from __future__ import annotations

from importlib.resources import files
from typing import Any

import json

SCHEMAS = {
    "config-v1": "config-v1.schema.json",
    "lineage-graph-v1": "lineage-graph-v1.schema.json",
}


def read_schema(name: str) -> dict[str, Any]:
    try:
        file_name = SCHEMAS[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown schema '{name}'. Available: "
            f"{', '.join(sorted(SCHEMAS))}"
        ) from exc
    resource = files(__package__).joinpath(file_name)
    value: Any = json.loads(resource.read_text(encoding="utf-8"))
    return value


__all__ = ["SCHEMAS", "read_schema"]

