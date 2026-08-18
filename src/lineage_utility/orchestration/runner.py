"""Config-driven extraction, publication, state, and manifest runner."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..contracts import PublishResult, StateEntry
from ..domain import write_graph
from ..domain.legacy import graph_to_legacy, write_legacy_model
from ..plugins import PluginRegistry
from .config import JobConfig, PublisherConfig, UtilityConfig
from .state import JsonStateStore, RunLock

LOGGER = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _publication_fingerprint(
    graph_fingerprint: str,
    publisher: PublisherConfig,
) -> str:
    payload = (
        f"{graph_fingerprint}:{publisher.fingerprint}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_file_stem(identifier: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", identifier).strip(".-")
    if not normalized:
        normalized = "lineage-target"
    if normalized != identifier:
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:8]
        normalized = f"{normalized}-{digest}"
    return normalized


@dataclass(slots=True)
class TargetRunResult:
    job: str
    target: str
    status: str
    source_uri: str
    model_path: str | None = None
    fingerprint: str | None = None
    publications: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "target": self.target,
            "status": self.status,
            "source_uri": self.source_uri,
            "model_path": self.model_path,
            "fingerprint": self.fingerprint,
            "publications": self.publications,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    started_at: str
    completed_at: str
    dry_run: bool
    results: tuple[TargetRunResult, ...]
    manifest_path: Path

    @property
    def succeeded(self) -> int:
        return sum(item.status in ("success", "dry-run") for item in self.results)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.results)

    @property
    def is_success(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "dry_run": self.dry_run,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "results": [item.to_dict() for item in self.results],
        }


class UtilityRunner:
    def __init__(
        self,
        *,
        config: UtilityConfig,
        registry: PluginRegistry,
    ) -> None:
        self._config = config
        self._registry = registry
        self._state = JsonStateStore(config.state_file)
        self._publishers: dict[str, Any] = {}

    def validate_plugins(self) -> None:
        for job in self._config.jobs:
            self._registry.create_extractor(job.extractor)
        for publisher in self._config.publishers.values():
            self._registry.create_publisher(
                publisher.plugin,
                publisher.name,
                publisher.settings,
            )

    def _publisher(self, name: str) -> Any:
        if name not in self._publishers:
            config = self._config.publishers[name]
            self._publishers[name] = self._registry.create_publisher(
                config.plugin,
                config.name,
                config.settings,
            )
        return self._publishers[name]

    def run(
        self,
        *,
        selected_jobs: set[str] | None = None,
        dry_run: bool = False,
        force: bool = False,
        fail_fast: bool = False,
    ) -> RunSummary:
        lock_path = self._config.state_file.with_name(
            self._config.state_file.name + ".lock"
        )
        with RunLock(lock_path):
            self._state = JsonStateStore(self._config.state_file)
            return self._run_unlocked(
                selected_jobs=selected_jobs,
                dry_run=dry_run,
                force=force,
                fail_fast=fail_fast,
            )

    def _run_unlocked(
        self,
        *,
        selected_jobs: set[str] | None,
        dry_run: bool,
        force: bool,
        fail_fast: bool,
    ) -> RunSummary:
        run_id = (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        started_at = _utc_now()
        results: list[TargetRunResult] = []
        abort_all_jobs = False
        for job in self._config.selected_jobs(selected_jobs):
            extractor = self._registry.create_extractor(job.extractor)
            try:
                targets = extractor.discover(
                    job.source,
                    base_dir=self._config.base_dir,
                )
                if not targets:
                    raise ValueError(
                        f"Extractor '{job.extractor}' discovered no targets."
                    )
                identifiers = [item.identifier for item in targets]
                if any(
                    not isinstance(item, str) or not item.strip()
                    for item in identifiers
                ):
                    raise ValueError(
                        f"Extractor '{job.extractor}' returned an empty "
                        "target identifier."
                    )
                if len(set(identifiers)) != len(identifiers):
                    raise ValueError(
                        f"Extractor '{job.extractor}' returned duplicate "
                        "target identifiers."
                    )
            except Exception as exc:
                results.append(
                    TargetRunResult(
                        job=job.name,
                        target="<discovery>",
                        status="failed",
                        source_uri="",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                LOGGER.exception("Job '%s' discovery failed.", job.name)
                if fail_fast:
                    abort_all_jobs = True
                    break
                continue
            for target in targets:
                result = self._run_target(
                    job,
                    target,
                    extractor=extractor,
                    dry_run=dry_run,
                    force=force,
                )
                results.append(result)
                if result.status == "failed" and fail_fast:
                    abort_all_jobs = True
                    break
                if result.status == "failed" and not job.continue_on_error:
                    break
            if abort_all_jobs:
                break

        completed_at = _utc_now()
        manifest_path = (
            self._config.manifest_dir / f"{run_id}.manifest.json"
        )
        summary = RunSummary(
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            dry_run=dry_run,
            results=tuple(results),
            manifest_path=manifest_path,
        )
        self._write_manifest(summary)
        return summary

    def _run_target(
        self,
        job: JobConfig,
        target: Any,
        *,
        extractor: Any,
        dry_run: bool,
        force: bool,
    ) -> TargetRunResult:
        result = TargetRunResult(
            job=job.name,
            target=target.display_name,
            status="failed",
            source_uri=target.source_uri,
        )
        try:
            graph = extractor.extract(target)
            output_dir = (
                job.output_dir
                or self._config.output_dir / job.name
            )
            model_path = output_dir / (
                f"{_safe_file_stem(target.identifier)}.lineage.json"
            )
            write_graph(graph, model_path)
            if job.emit_legacy_model:
                write_legacy_model(
                    graph_to_legacy(graph),
                    output_dir
                    / f"{_safe_file_stem(target.identifier)}.legacy.json",
                )
            result.model_path = str(model_path)
            result.fingerprint = graph.fingerprint

            for publisher_name in job.publishers:
                publisher_config = self._config.publishers[publisher_name]
                state_key = json.dumps(
                    [job.name, target.identifier, publisher_name],
                    separators=(",", ":"),
                )
                publication_fingerprint = _publication_fingerprint(
                    graph.fingerprint,
                    publisher_config,
                )
                current = self._state.get(state_key)
                if not force and current and (
                    current.fingerprint == publication_fingerprint
                ):
                    result.publications.append(
                        {
                            "publisher": publisher_name,
                            "status": "unchanged",
                            "details": dict(current.details),
                        }
                    )
                    continue
                if dry_run:
                    result.publications.append(
                        {
                            "publisher": publisher_name,
                            "status": "planned",
                        }
                    )
                    continue
                publication: PublishResult = self._publisher(
                    publisher_name
                ).publish(graph)
                if not publication.success:
                    raise RuntimeError(
                        f"Publisher '{publisher_name}' reported failure."
                    )
                publication_details = {
                    "process_guids": dict(publication.process_guids),
                    **dict(publication.details),
                }
                result.publications.append(
                    {
                        "publisher": publisher_name,
                        "status": "published",
                        "details": publication_details,
                    }
                )
                self._state.put(
                    state_key,
                    StateEntry(
                        fingerprint=publication_fingerprint,
                        updated_at=_utc_now(),
                        details=publication_details,
                    ),
                )
            result.status = "dry-run" if dry_run else "success"
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception(
                "Target '%s' in job '%s' failed.",
                target.display_name,
                job.name,
            )
        return result

    def _write_manifest(self, summary: RunSummary) -> None:
        path = summary.manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(summary.to_dict(), indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
