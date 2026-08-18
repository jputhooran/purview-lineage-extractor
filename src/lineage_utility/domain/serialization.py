"""Atomic canonical lineage graph serialization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import LineageGraph


def read_graph(path: str | Path) -> LineageGraph:
    source = Path(path)
    try:
        value: Any = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in '{source}': {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Lineage model '{source}' must contain a JSON object.")
    return LineageGraph.from_dict(value)


def write_graph(graph: LineageGraph, path: str | Path) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(graph.to_dict(), indent=2, sort_keys=False),
        encoding="utf-8",
    )
    temporary.replace(output)
    return output

