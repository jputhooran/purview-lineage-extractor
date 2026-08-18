"""Publisher plugin contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from ..domain import LineageGraph


@dataclass(frozen=True, slots=True)
class PublishResult:
    publisher: str
    artifact_qualified_name: str
    success: bool
    process_guids: Mapping[str, str] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)


class Publisher(Protocol):
    plugin_name: str
    instance_name: str

    def publish(self, graph: LineageGraph) -> PublishResult:
        """Publish and verify one lineage graph."""

