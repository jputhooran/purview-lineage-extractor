"""Small, retrying Apache Atlas HTTP client for Microsoft Purview."""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from ...contracts import TokenProvider
from .auth import PURVIEW_SCOPE

LOGGER = logging.getLogger(__name__)
TRANSIENT_STATUS_CODES = frozenset({401, 408, 429, 500, 502, 503, 504})


class AtlasTransportError(RuntimeError):
    """Raised when Atlas cannot be reached after retries."""


@dataclass(frozen=True, slots=True)
class AtlasResponse:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    initial_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("Retry max_attempts must be at least 1.")
        if (
            self.initial_delay_seconds < 0
            or self.maximum_delay_seconds < 0
        ):
            raise ValueError("Retry delays cannot be negative.")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("Retry jitter_ratio must be between 0 and 1.")

    def delay(self, attempt: int) -> float:
        base = min(
            self.initial_delay_seconds * (2**attempt),
            self.maximum_delay_seconds,
        )
        jitter = base * self.jitter_ratio * random.random()
        return base + jitter


class AtlasClient:
    def __init__(
        self,
        *,
        account: str,
        token_provider: TokenProvider,
        timeout_seconds: int = 90,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9-]+", account):
            raise ValueError(f"Invalid Purview account name '{account}'.")
        if timeout_seconds <= 0:
            raise ValueError("Purview timeout_seconds must be positive.")
        self.account = account
        self.base_url = (
            f"https://{account}.purview.azure.com/catalog/api/atlas/v2"
        )
        self._token_provider = token_provider
        self._timeout_seconds = timeout_seconds
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> AtlasResponse:
        url = (
            path
            if path.startswith("https://")
            else f"{self.base_url}/{path.lstrip('/')}"
        )
        encoded = (
            json.dumps(body).encode("utf-8") if body is not None else None
        )
        last_network_error: Exception | None = None
        for attempt in range(self._retry_policy.max_attempts):
            token = self._token_provider.get_token(PURVIEW_SCOPE)
            request = urllib.request.Request(
                url,
                data=encoded,
                method=method.upper(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-ms-client-request-id": str(uuid.uuid4()),
                },
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._timeout_seconds,
                ) as response:
                    raw = response.read().decode("utf-8")
                    return AtlasResponse(
                        status_code=response.status,
                        body=json.loads(raw) if raw else {},
                    )
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8")
                try:
                    error_body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    error_body = {"raw": raw}
                if (
                    exc.code not in TRANSIENT_STATUS_CODES
                    or attempt + 1 >= self._retry_policy.max_attempts
                ):
                    return AtlasResponse(exc.code, error_body)
                delay = self._retry_policy.delay(attempt)
                LOGGER.warning(
                    "Transient Atlas HTTP %s; retrying in %.1fs "
                    "(attempt %s/%s)",
                    exc.code,
                    delay,
                    attempt + 1,
                    self._retry_policy.max_attempts,
                )
                self._sleep(delay)
            except urllib.error.URLError as exc:
                last_network_error = exc
                if attempt + 1 >= self._retry_policy.max_attempts:
                    break
                delay = self._retry_policy.delay(attempt)
                LOGGER.warning(
                    "Atlas network request failed; retrying in %.1fs "
                    "(attempt %s/%s)",
                    delay,
                    attempt + 1,
                    self._retry_policy.max_attempts,
                )
                self._sleep(delay)
        raise AtlasTransportError(
            f"Purview Atlas request failed after "
            f"{self._retry_policy.max_attempts} attempts: {url}"
        ) from last_network_error

    def get(self, path: str) -> AtlasResponse:
        return self.request("GET", path)

    def post(
        self,
        path: str,
        body: dict[str, Any],
    ) -> AtlasResponse:
        return self.request("POST", path, body)

    def put(
        self,
        path: str,
        body: dict[str, Any],
    ) -> AtlasResponse:
        return self.request("PUT", path, body)
