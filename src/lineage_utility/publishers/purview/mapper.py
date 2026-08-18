"""Map canonical lineage graphs to Atlas bulk entities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ...domain import FieldMapping, LineageGraph


@dataclass(slots=True)
class ProcessExpectation:
    placeholder_guid: str
    name: str
    qualified_name: str
    legacy_qualified_names: tuple[str, ...]
    expected_column_pairs: int
    expected_inputs: int
    expected_outputs: int
    guid: str | None = None


@dataclass(frozen=True, slots=True)
class EntityBatch:
    entities: list[dict[str, Any]]
    labels: Mapping[str, str]
    processes: list[ProcessExpectation]


def build_column_mapping_json(
    mappings: tuple[FieldMapping, ...],
) -> str:
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for mapping in mappings:
        key = (
            mapping.source_asset_qualified_name,
            mapping.target_asset_qualified_name,
        )
        groups.setdefault(key, []).append(
            {
                "Source": mapping.source_field,
                "Sink": mapping.target_field,
            }
        )
    payload = [
        {
            "DatasetMapping": {"Source": source, "Sink": target},
            "ColumnMapping": columns,
        }
        for (source, target), columns in groups.items()
    ]
    return json.dumps(payload, separators=(",", ":"))


def _guess_type(field_name: str) -> str:
    normalized = field_name.casefold()
    if normalized.endswith("id") or normalized.endswith("key"):
        return "int"
    if "date" in normalized or "time" in normalized:
        return "datetime"
    return "nvarchar"


def map_graph(
    graph: LineageGraph,
    *,
    dataset_type: str,
    column_type: str | None,
    process_type: str,
    process_type_definition: Mapping[str, Any] | None,
    with_columns: bool,
) -> EntityBatch:
    entities: list[dict[str, Any]] = []
    labels: dict[str, str] = {}
    next_placeholder = -1

    def next_guid() -> str:
        nonlocal next_placeholder
        value = str(next_placeholder)
        next_placeholder -= 1
        return value

    asset_guids: dict[str, str] = {}
    for asset in graph.assets:
        guid = next_guid()
        asset_guids[asset.qualified_name] = guid
        labels[guid] = f"asset {asset.name}"
        entity: dict[str, Any] = {
            "typeName": dataset_type,
            "guid": guid,
            "attributes": {
                "qualifiedName": asset.qualified_name,
                "name": asset.name,
            },
        }
        if with_columns and column_type:
            column_references = []
            for field in asset.fields:
                field_guid = next_guid()
                field_type = field.data_type or _guess_type(field.name)
                entities.append(
                    {
                        "typeName": column_type,
                        "guid": field_guid,
                        "attributes": {
                            "qualifiedName": (
                                f"{asset.qualified_name}#{field.name}"
                            ),
                            "name": field.name,
                            "data_type": field_type,
                        },
                    }
                )
                column_references.append(
                    {"guid": field_guid, "typeName": column_type}
                )
            entity["relationshipAttributes"] = {
                "columns": column_references
            }
        entities.append(entity)

    expectations = []
    declared_attributes = (
        {
            item["name"]
            for item in process_type_definition.get("attributes", [])
        }
        if process_type_definition
        else set()
    )
    for process in graph.processes:
        mappings = tuple(
            item
            for item in graph.field_mappings
            if item.process_qualified_name == process.qualified_name
        )
        input_references = [
            {
                "guid": asset_guids[qualified_name],
                "typeName": dataset_type,
            }
            for qualified_name in process.inputs
        ]
        output_references = [
            {
                "guid": asset_guids[qualified_name],
                "typeName": dataset_type,
            }
            for qualified_name in process.outputs
        ]
        guid = next_guid()
        labels[guid] = f"process {process.name}"
        attributes: dict[str, Any] = {
            "qualifiedName": process.qualified_name,
            "name": process.name,
            "inputs": input_references,
            "outputs": output_references,
        }
        if process_type_definition:
            unknown = set(process.attributes) - declared_attributes
            if unknown:
                raise ValueError(
                    f"Process '{process.qualified_name}' supplied undeclared "
                    f"{process_type} attributes: {', '.join(sorted(unknown))}"
                )
            attributes.update(process.attributes)
            if "transformExpressions" in declared_attributes:
                transforms = sorted(
                    {
                        item.transform
                        for item in mappings
                        if item.transform != "passthrough"
                    }
                )
                attributes["transformExpressions"] = (
                    "; ".join(transforms) if transforms else "(direct copy)"
                )
            if "columnMapping" in declared_attributes:
                attributes["columnMapping"] = (
                    build_column_mapping_json(mappings)
                    if with_columns and mappings
                    else "[]"
                )
        entities.append(
            {
                "typeName": process_type,
                "guid": guid,
                "attributes": attributes,
            }
        )
        expectations.append(
            ProcessExpectation(
                placeholder_guid=guid,
                name=process.name,
                qualified_name=process.qualified_name,
                legacy_qualified_names=process.legacy_qualified_names,
                expected_column_pairs=len(mappings) if with_columns else 0,
                expected_inputs=len(input_references),
                expected_outputs=len(output_references),
            )
        )
    return EntityBatch(entities, labels, expectations)
