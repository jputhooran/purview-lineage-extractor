"""Validated YAML configuration for multi-job lineage runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

ENVIRONMENT_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)


def _expand_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        resolved = os.environ.get(name)
        if resolved is not None:
            return resolved
        if default is not None:
            return default
        raise ValueError(
            f"Required environment variable '{name}' is not set."
        )

    return ENVIRONMENT_PATTERN.sub(replace, value)


def _expand_environment(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_text(value)
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _expand_environment(item) for key, item in value.items()
        }
    return value


def _path(value: Any, *, base_dir: Path, default: str) -> Path:
    raw = value if value is not None else default
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Configured path must be a non-empty string.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _boolean(value: Any, *, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"Configuration '{name}' must be true or false.")
    return value


def _json_mapping(value: Mapping[str, Any], *, name: str) -> dict[str, Any]:
    normalized = dict(value)
    if not all(isinstance(key, str) for key in normalized):
        raise ValueError(f"Configuration '{name}' keys must be strings.")
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(
            f"Configuration '{name}' must contain JSON-compatible values."
        ) from exc
    return normalized


def _identifier(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*",
        value,
    ):
        raise ValueError(
            f"Configuration '{name}' must start with an alphanumeric "
            "character and contain only letters, numbers, '.', '_', or '-'."
        )
    return value


@dataclass(frozen=True, slots=True)
class PublisherConfig:
    name: str
    plugin: str
    settings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "plugin": self.plugin,
                "settings": dict(self.settings),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class JobConfig:
    name: str
    extractor: str
    source: Mapping[str, Any]
    publishers: tuple[str, ...] = ()
    enabled: bool = True
    continue_on_error: bool = True
    output_dir: Path | None = None
    emit_legacy_model: bool = False


@dataclass(frozen=True, slots=True)
class UtilityConfig:
    source_path: Path
    output_dir: Path
    state_file: Path
    manifest_dir: Path
    publishers: Mapping[str, PublisherConfig]
    jobs: tuple[JobConfig, ...]
    version: int = 1

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent

    def selected_jobs(self, names: set[str] | None) -> tuple[JobConfig, ...]:
        enabled = tuple(item for item in self.jobs if item.enabled)
        if not names:
            return enabled
        available = {item.name for item in self.jobs}
        missing = names - available
        if missing:
            raise ValueError(
                f"Unknown job(s): {', '.join(sorted(missing))}"
            )
        disabled = {
            item.name
            for item in self.jobs
            if item.name in names and not item.enabled
        }
        if disabled:
            raise ValueError(
                f"Selected job(s) are disabled: {', '.join(sorted(disabled))}"
            )
        return tuple(item for item in enabled if item.name in names)


def load_config(path: str | Path) -> UtilityConfig:
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {source_path}")
    try:
        raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in '{source_path}': {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Utility configuration must be a YAML object.")
    raw = _expand_environment(raw)
    version = raw.get("version", 1)
    if version != 1:
        raise ValueError(
            f"Unsupported configuration version '{version}'. Expected 1."
        )
    base_dir = source_path.parent
    runtime = raw.get("runtime") or {}
    if not isinstance(runtime, dict):
        raise ValueError("Configuration 'runtime' must be an object.")
    output_dir = _path(
        runtime.get("output_dir"),
        base_dir=base_dir,
        default="artifacts/models",
    )
    state_file = _path(
        runtime.get("state_file"),
        base_dir=base_dir,
        default="artifacts/state.json",
    )
    manifest_dir = _path(
        runtime.get("manifest_dir"),
        base_dir=base_dir,
        default="artifacts/manifests",
    )

    raw_publishers = raw.get("publishers") or {}
    if not isinstance(raw_publishers, dict):
        raise ValueError("Configuration 'publishers' must be an object.")
    publishers: dict[str, PublisherConfig] = {}
    for name, value in raw_publishers.items():
        name = _identifier(name, name="publishers key")
        if not isinstance(value, dict):
            raise ValueError(f"Publisher '{name}' must be an object.")
        plugin = value.get("plugin")
        if not isinstance(plugin, str) or not plugin.strip():
            raise ValueError(f"Publisher '{name}' requires 'plugin'.")
        publishers[str(name)] = PublisherConfig(
            name=str(name),
            plugin=plugin.strip(),
            settings=_json_mapping(
                {
                    key: item
                    for key, item in value.items()
                    if key != "plugin"
                },
                name=f"publishers.{name}",
            ),
        )

    raw_jobs = raw.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError("Configuration requires at least one job.")
    jobs = []
    names = set()
    for value in raw_jobs:
        if not isinstance(value, dict):
            raise ValueError("Every job must be an object.")
        name = value.get("name")
        extractor = value.get("extractor")
        source = value.get("source")
        name = _identifier(name, name="jobs[].name")
        if name in names:
            raise ValueError(f"Duplicate job name '{name}'.")
        names.add(name)
        if not isinstance(extractor, str) or not extractor.strip():
            raise ValueError(f"Job '{name}' requires 'extractor'.")
        if not isinstance(source, dict):
            raise ValueError(f"Job '{name}' requires a source object.")
        publisher_names = value.get("publishers") or []
        if not isinstance(publisher_names, list) or not all(
            isinstance(item, str) for item in publisher_names
        ):
            raise ValueError(
                f"Job '{name}' publishers must be an array of names."
            )
        if len(set(publisher_names)) != len(publisher_names):
            raise ValueError(
                f"Job '{name}' contains duplicate publisher names."
            )
        missing_publishers = set(publisher_names) - set(publishers)
        if missing_publishers:
            raise ValueError(
                f"Job '{name}' references unknown publisher(s): "
                f"{', '.join(sorted(missing_publishers))}"
            )
        job_output = value.get("output_dir")
        jobs.append(
            JobConfig(
                name=name,
                extractor=extractor.strip(),
                source=_json_mapping(
                    source,
                    name=f"jobs.{name}.source",
                ),
                publishers=tuple(publisher_names),
                enabled=_boolean(
                    value.get("enabled"),
                    name=f"jobs.{name}.enabled",
                    default=True,
                ),
                continue_on_error=_boolean(
                    value.get("continue_on_error"),
                    name=f"jobs.{name}.continue_on_error",
                    default=True,
                ),
                output_dir=(
                    _path(
                        job_output,
                        base_dir=base_dir,
                        default=str(output_dir / name),
                    )
                    if job_output is not None
                    else None
                ),
                emit_legacy_model=_boolean(
                    value.get("emit_legacy_model"),
                    name=f"jobs.{name}.emit_legacy_model",
                    default=False,
                ),
            )
        )
    return UtilityConfig(
        source_path=source_path,
        output_dir=output_dir,
        state_file=state_file,
        manifest_dir=manifest_dir,
        publishers=publishers,
        jobs=tuple(jobs),
        version=version,
    )
