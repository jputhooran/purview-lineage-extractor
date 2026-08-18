"""Atomic JSON state store for incremental publication."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from ..contracts import StateEntry


class RunLock:
    """Fail fast when another process owns the same utility state."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()
        self._token = uuid.uuid4().hex
        self._acquired = False

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = None
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self._path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                break
            except FileExistsError as exc:
                try:
                    owner = self._path.read_text(
                        encoding="utf-8",
                        errors="replace",
                    ).strip()
                except FileNotFoundError:
                    continue
                detail = f" Owner: {owner}" if owner else ""
                raise RuntimeError(
                    f"Another lineage run owns lock '{self._path}'."
                    f"{detail} Remove a stale lock only after confirming no "
                    "run is active."
                ) from exc
        if descriptor is None:
            raise RuntimeError(
                f"Could not acquire lineage run lock '{self._path}'."
            )
        payload = json.dumps(
            {
                "token": self._token,
                "pid": os.getpid(),
            }
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
        except OSError:
            self._path.unlink(missing_ok=True)
            raise
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._acquired = False
            return
        if value.get("token") != self._token:
            raise RuntimeError(
                f"Run lock ownership changed unexpectedly: {self._path}"
            )
        self._path.unlink()
        self._acquired = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


class JsonStateStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()
        self._lock = threading.Lock()
        self._entries = self._read()

    def _read(self) -> dict[str, StateEntry]:
        if not self._path.exists():
            return {}
        try:
            value: Any = json.loads(
                self._path.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid state file '{self._path}': {exc}"
            ) from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError(
                f"Unsupported or invalid state file: {self._path}"
            )
        entries = value.get("entries")
        if not isinstance(entries, dict):
            raise ValueError(
                f"State file entries must be an object: {self._path}"
            )
        return {
            key: StateEntry(
                fingerprint=str(item["fingerprint"]),
                updated_at=str(item["updated_at"]),
                details=dict(item.get("details") or {}),
            )
            for key, item in entries.items()
        }

    def get(self, key: str) -> StateEntry | None:
        return self._entries.get(key)

    def put(self, key: str, entry: StateEntry) -> None:
        with self._lock:
            self._entries[key] = entry
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": {
                            name: {
                                "fingerprint": value.fingerprint,
                                "updated_at": value.updated_at,
                                "details": dict(value.details),
                            }
                            for name, value in sorted(
                                self._entries.items()
                            )
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self._path)
