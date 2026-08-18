"""Typed, publisher-neutral lineage graph."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

CURRENT_SCHEMA_VERSION = "1.0"


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value


def _mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


@dataclass(frozen=True, slots=True)
class AssetField:
    name: str
    data_type: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.name, "AssetField.name")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"name": self.name}
        if self.data_type:
            result["data_type"] = self.data_type
        if self.attributes:
            result["attributes"] = dict(self.attributes)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AssetField":
        return cls(
            name=str(value["name"]),
            data_type=(
                str(value["data_type"]) if value.get("data_type") else None
            ),
            attributes=_mapping(value.get("attributes")),
        )


@dataclass(frozen=True, slots=True)
class DataAsset:
    qualified_name: str
    name: str
    asset_type: str = "mssql_table"
    fields: tuple[AssetField, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.qualified_name, "DataAsset.qualified_name")
        _required_text(self.name, "DataAsset.name")
        _required_text(self.asset_type, "DataAsset.asset_type")
        folded = [item.name.casefold() for item in self.fields]
        if len(folded) != len(set(folded)):
            raise ValueError(
                f"Asset '{self.qualified_name}' has duplicate field names."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualified_name": self.qualified_name,
            "name": self.name,
            "asset_type": self.asset_type,
            "fields": [item.to_dict() for item in self.fields],
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DataAsset":
        return cls(
            qualified_name=str(value["qualified_name"]),
            name=str(value["name"]),
            asset_type=str(value.get("asset_type", "mssql_table")),
            fields=tuple(
                AssetField.from_dict(item)
                for item in value.get("fields", [])
            ),
            attributes=_mapping(value.get("attributes")),
        )


@dataclass(frozen=True, slots=True)
class Process:
    qualified_name: str
    name: str
    process_type: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    attributes: Mapping[str, Any] = field(default_factory=dict)
    legacy_qualified_names: tuple[str, ...] = ()
    source_reference: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.qualified_name, "Process.qualified_name")
        _required_text(self.name, "Process.name")
        _required_text(self.process_type, "Process.process_type")
        if not self.inputs:
            raise ValueError(f"Process '{self.qualified_name}' has no inputs.")
        if not self.outputs:
            raise ValueError(f"Process '{self.qualified_name}' has no outputs.")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "qualified_name": self.qualified_name,
            "name": self.name,
            "process_type": self.process_type,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "attributes": dict(self.attributes),
            "legacy_qualified_names": list(self.legacy_qualified_names),
        }
        if self.source_reference:
            result["source_reference"] = self.source_reference
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Process":
        return cls(
            qualified_name=str(value["qualified_name"]),
            name=str(value["name"]),
            process_type=str(value["process_type"]),
            inputs=tuple(str(item) for item in value.get("inputs", [])),
            outputs=tuple(str(item) for item in value.get("outputs", [])),
            attributes=_mapping(value.get("attributes")),
            legacy_qualified_names=tuple(
                str(item)
                for item in value.get("legacy_qualified_names", [])
            ),
            source_reference=(
                str(value["source_reference"])
                if value.get("source_reference")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class FieldMapping:
    process_qualified_name: str
    source_asset_qualified_name: str
    source_field: str
    target_asset_qualified_name: str
    target_field: str
    transform: str = "passthrough"

    def __post_init__(self) -> None:
        for field_name in (
            "process_qualified_name",
            "source_asset_qualified_name",
            "source_field",
            "target_asset_qualified_name",
            "target_field",
            "transform",
        ):
            _required_text(getattr(self, field_name), f"FieldMapping.{field_name}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_qualified_name": self.process_qualified_name,
            "source_asset_qualified_name": self.source_asset_qualified_name,
            "source_field": self.source_field,
            "target_asset_qualified_name": self.target_asset_qualified_name,
            "target_field": self.target_field,
            "transform": self.transform,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FieldMapping":
        return cls(
            process_qualified_name=str(value["process_qualified_name"]),
            source_asset_qualified_name=str(
                value["source_asset_qualified_name"]
            ),
            source_field=str(value["source_field"]),
            target_asset_qualified_name=str(
                value["target_asset_qualified_name"]
            ),
            target_field=str(value["target_field"]),
            transform=str(value.get("transform", "passthrough")),
        )


@dataclass(frozen=True, slots=True)
class LineageGraph:
    artifact_name: str
    artifact_qualified_name: str
    artifact_kind: str
    assets: tuple[DataAsset, ...]
    processes: tuple[Process, ...]
    field_mappings: tuple[FieldMapping, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = CURRENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _required_text(self.artifact_name, "LineageGraph.artifact_name")
        _required_text(
            self.artifact_qualified_name,
            "LineageGraph.artifact_qualified_name",
        )
        _required_text(self.artifact_kind, "LineageGraph.artifact_kind")
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported lineage schema version '{self.schema_version}'. "
                f"Expected '{CURRENT_SCHEMA_VERSION}'."
            )
        self.validate()

    def validate(self) -> None:
        if not self.assets:
            raise ValueError("A lineage graph must contain at least one asset.")
        if not self.processes:
            raise ValueError("A lineage graph must contain at least one process.")

        assets = {item.qualified_name: item for item in self.assets}
        if len(assets) != len(self.assets):
            raise ValueError("A lineage graph contains duplicate asset identities.")
        processes = {item.qualified_name: item for item in self.processes}
        if len(processes) != len(self.processes):
            raise ValueError(
                "A lineage graph contains duplicate process identities."
            )

        field_names = {
            qualified_name: {field.name.casefold() for field in asset.fields}
            for qualified_name, asset in assets.items()
        }
        for process in self.processes:
            missing = [
                qualified_name
                for qualified_name in (*process.inputs, *process.outputs)
                if qualified_name not in assets
            ]
            if missing:
                raise ValueError(
                    f"Process '{process.qualified_name}' references unknown "
                    f"asset(s): {', '.join(missing)}"
                )

        for mapping in self.field_mappings:
            if mapping.process_qualified_name not in processes:
                raise ValueError(
                    "Field mapping references unknown process "
                    f"'{mapping.process_qualified_name}'."
                )
            process = processes[mapping.process_qualified_name]
            if mapping.source_asset_qualified_name not in process.inputs:
                raise ValueError(
                    "Field mapping source is not an input of process "
                    f"'{mapping.process_qualified_name}'."
                )
            if mapping.target_asset_qualified_name not in process.outputs:
                raise ValueError(
                    "Field mapping target is not an output of process "
                    f"'{mapping.process_qualified_name}'."
                )
            for asset_qn, field_name in (
                (
                    mapping.source_asset_qualified_name,
                    mapping.source_field,
                ),
                (
                    mapping.target_asset_qualified_name,
                    mapping.target_field,
                ),
            ):
                if asset_qn not in assets:
                    raise ValueError(
                        f"Field mapping references unknown asset '{asset_qn}'."
                    )
                if field_name.casefold() not in field_names[asset_qn]:
                    raise ValueError(
                        f"Field mapping references unknown field "
                        f"'{asset_qn}#{field_name}'."
                    )
        mapping_keys = {
            (
                item.process_qualified_name,
                item.source_asset_qualified_name,
                item.source_field.casefold(),
                item.target_asset_qualified_name,
                item.target_field.casefold(),
                item.transform,
            )
            for item in self.field_mappings
        }
        if len(mapping_keys) != len(self.field_mappings):
            raise ValueError("A lineage graph contains duplicate field mappings.")

    def to_dict(self, *, include_metadata: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "artifact": {
                "name": self.artifact_name,
                "qualified_name": self.artifact_qualified_name,
                "kind": self.artifact_kind,
                "attributes": dict(self.attributes),
            },
            "assets": [item.to_dict() for item in self.assets],
            "processes": [item.to_dict() for item in self.processes],
            "field_mappings": [
                item.to_dict() for item in self.field_mappings
            ],
        }
        if include_metadata and self.metadata:
            result["metadata"] = dict(self.metadata)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LineageGraph":
        artifact = value.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ValueError("Canonical lineage model is missing 'artifact'.")
        return cls(
            schema_version=str(
                value.get("schema_version", CURRENT_SCHEMA_VERSION)
            ),
            artifact_name=str(artifact["name"]),
            artifact_qualified_name=str(artifact["qualified_name"]),
            artifact_kind=str(artifact["kind"]),
            attributes=_mapping(artifact.get("attributes")),
            assets=tuple(
                DataAsset.from_dict(item) for item in value.get("assets", [])
            ),
            processes=tuple(
                Process.from_dict(item)
                for item in value.get("processes", [])
            ),
            field_mappings=tuple(
                FieldMapping.from_dict(item)
                for item in value.get("field_mappings", [])
            ),
            metadata=_mapping(value.get("metadata")),
        )

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(include_metadata=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
