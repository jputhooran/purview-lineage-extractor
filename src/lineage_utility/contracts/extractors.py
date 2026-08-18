"""Extractor plugin contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from ..domain import LineageGraph


@dataclass(frozen=True, slots=True)
class ExtractionTarget:
    identifier: str
    display_name: str
    source_uri: str
    options: Mapping[str, Any] = field(default_factory=dict)


class Extractor(Protocol):
    plugin_name: str

    def discover(
        self,
        source: Mapping[str, Any],
        *,
        base_dir: Path,
    ) -> Sequence[ExtractionTarget]:
        """Resolve one job source into independently extractable targets."""

    def extract(self, target: ExtractionTarget) -> LineageGraph:
        """Extract and validate one canonical lineage graph."""

