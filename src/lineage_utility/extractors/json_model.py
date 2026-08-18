"""Extractor for canonical or POC-compatible JSON lineage models."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import ExtractionTarget
from ..domain import LineageGraph
from ..domain.legacy import graph_from_legacy


def _identifier(path: Path) -> str:
    return (
        re.sub(r"[^a-zA-Z0-9_.-]+", "-", path.stem).strip("-")
        or "json-model"
    )


class JsonModelExtractor:
    plugin_name = "json-model"

    def discover(
        self,
        source: Mapping[str, Any],
        *,
        base_dir: Path,
    ) -> Sequence[ExtractionTarget]:
        raw = source.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("JSON model source requires a non-empty 'path'.")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"JSON model path does not exist: {path}")
        if path.is_file():
            files = [path]
        else:
            include = str(source.get("include") or "*.json")
            files = sorted(
                item for item in path.glob(include) if item.is_file()
            )
        excluded = source.get("exclude") or []
        if not isinstance(excluded, list):
            raise ValueError("JSON model 'exclude' must be an array of globs.")
        files = [
            item
            for item in files
            if not any(
                fnmatch.fnmatch(item.name, str(pattern))
                for pattern in excluded
            )
        ]
        if not files:
            raise FileNotFoundError(f"No JSON lineage models matched: {path}")
        requested_format = str(source.get("format") or "auto").lower()
        if requested_format not in ("auto", "canonical", "legacy"):
            raise ValueError(
                "JSON model format must be auto, canonical, or legacy."
            )
        return [
            ExtractionTarget(
                identifier=_identifier(item),
                display_name=item.name,
                source_uri=item.as_uri(),
                options={
                    "path": str(item),
                    "format": requested_format,
                },
            )
            for item in files
        ]

    def extract(self, target: ExtractionTarget) -> LineageGraph:
        path = Path(str(target.options["path"]))
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"JSON lineage model must be an object: {path}")
        model_format = str(target.options.get("format") or "auto")
        if model_format == "canonical" or (
            model_format == "auto" and "schema_version" in value
        ):
            return LineageGraph.from_dict(value)
        return graph_from_legacy(
            value,
            metadata={
                "extractor": self.plugin_name,
                "source_uri": target.source_uri,
            },
        )

