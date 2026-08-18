"""Incremental-run state contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class StateEntry:
    fingerprint: str
    updated_at: str
    details: Mapping[str, Any] = field(default_factory=dict)


class StateStore(Protocol):
    def get(self, key: str) -> StateEntry | None:
        """Return a persisted state entry."""

    def put(self, key: str, entry: StateEntry) -> None:
        """Persist a state entry atomically."""

