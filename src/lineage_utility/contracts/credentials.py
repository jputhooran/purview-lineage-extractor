"""Authentication contract used by remote publishers."""

from __future__ import annotations

from typing import Protocol


class TokenProvider(Protocol):
    def get_token(self, scope: str) -> str:
        """Return a bearer token for the requested Microsoft Entra scope."""

