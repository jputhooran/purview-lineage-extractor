"""SSIS extractor plugin."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...contracts import ExtractionTarget
from ...domain import LineageGraph
from ...domain.legacy import graph_from_legacy
from .parser import parse_dtsx


def _required_source_path(
    source: Mapping[str, Any],
    base_dir: Path,
) -> Path:
    raw = source.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("SSIS source requires a non-empty 'path'.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"SSIS source path does not exist: {path}")
    return path


def _identifier(path: Path) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", path.stem).strip("-")
    return value or "ssis-package"


class SsisExtractor:
    plugin_name = "ssis"

    def discover(
        self,
        source: Mapping[str, Any],
        *,
        base_dir: Path,
    ) -> Sequence[ExtractionTarget]:
        path = _required_source_path(source, base_dir)
        if path.is_file():
            if path.suffix.casefold() != ".dtsx":
                raise ValueError(f"SSIS source is not a .dtsx file: {path}")
            files = [path]
        else:
            include = str(source.get("include") or "*.dtsx")
            files = sorted(
                candidate
                for candidate in path.glob(include)
                if candidate.is_file()
                and candidate.suffix.casefold() == ".dtsx"
            )
        excluded = source.get("exclude") or []
        if not isinstance(excluded, list) or not all(
            isinstance(item, str) for item in excluded
        ):
            raise ValueError("SSIS source 'exclude' must be an array of globs.")
        files = [
            candidate
            for candidate in files
            if not any(
                fnmatch.fnmatch(candidate.name, pattern)
                for pattern in excluded
            )
        ]
        if not files:
            raise FileNotFoundError(
                f"No top-level SSIS packages matched under: {path}"
            )
        return [
            ExtractionTarget(
                identifier=_identifier(candidate),
                display_name=candidate.name,
                source_uri=candidate.as_uri(),
                options={"path": str(candidate)},
            )
            for candidate in files
        ]

    def extract(self, target: ExtractionTarget) -> LineageGraph:
        path = Path(str(target.options["path"]))
        legacy_model = parse_dtsx(str(path))
        return graph_from_legacy(
            legacy_model,
            metadata={
                "extractor": self.plugin_name,
                "source_uri": target.source_uri,
            },
        )

