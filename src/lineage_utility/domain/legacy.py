"""Adapter for the proven POC V1 dictionary model."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import AssetField, DataAsset, FieldMapping, LineageGraph, Process

LegacyLineageModel = dict[str, Any]

SSIS_PROCESS_TYPE = "poc_ssis_package"
STORED_PROCEDURE_PROCESS_TYPE = "poc_sql_stored_procedure"


def _package_qualified_name(model: LegacyLineageModel) -> str:
    explicit = model.get("package_qualified_name")
    if explicit:
        return str(explicit).rstrip("/")
    package_file = str(model.get("package_file") or "")
    package_name = str(model.get("package_name") or "package")
    if package_file:
        source = str(Path(package_file).resolve()).casefold()
        file_name = Path(package_file).name
    else:
        source = package_name.casefold()
        file_name = package_name
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return (
        f"poc-ssis://unscoped/{digest}/package/"
        f"{quote(file_name.casefold(), safe='')}"
    )


def _artifact_identity(model: LegacyLineageModel) -> tuple[str, str, str]:
    kind = str(model.get("artifact_kind") or "ssis_package")
    name = str(
        model.get("artifact_name")
        or model.get("package_name")
        or "unknown"
    )
    qualified_name = str(
        model.get("artifact_qualified_name")
        or _package_qualified_name(model)
    ).rstrip("/")
    return kind, name, qualified_name


def graph_from_legacy(
    model: LegacyLineageModel,
    *,
    metadata: dict[str, Any] | None = None,
) -> LineageGraph:
    """Convert an existing POC model without changing its identities."""
    if not isinstance(model.get("tables"), dict):
        raise ValueError("Legacy model field 'tables' must be an object.")
    if not isinstance(model.get("data_flows"), list):
        raise ValueError("Legacy model field 'data_flows' must be an array.")
    if not isinstance(model.get("column_mappings"), list):
        raise ValueError(
            "Legacy model field 'column_mappings' must be an array."
        )

    artifact_kind, artifact_name, artifact_qn = _artifact_identity(model)
    process_type = str(model.get("process_type") or SSIS_PROCESS_TYPE)
    global_process_attributes = dict(model.get("process_attributes") or {})

    assets = []
    for qualified_name, value in model["tables"].items():
        if not isinstance(value, dict):
            raise ValueError(
                f"Legacy table '{qualified_name}' must be an object."
            )
        attributes = {
            key: item
            for key, item in value.items()
            if key not in ("qualified_name", "columns", "table")
        }
        assets.append(
            DataAsset(
                qualified_name=str(qualified_name),
                name=str(
                    value.get("table")
                    or str(qualified_name).rsplit("/", 1)[-1]
                ),
                fields=tuple(
                    AssetField(name=str(column))
                    for column in value.get("columns", [])
                ),
                attributes=attributes,
            )
        )

    processes = []
    process_by_reference: dict[str, str] = {}
    process_by_name: dict[str, list[str]] = {}
    for flow in model["data_flows"]:
        flow_name = str(flow["name"])
        source_reference = str(flow.get("ref_id") or flow_name)
        process_qn = str(
            flow.get("process_qualified_name")
            or (
                f"{artifact_qn}/data-flow/"
                f"{quote(source_reference.casefold(), safe='')}"
            )
        )
        attributes = dict(global_process_attributes)
        if artifact_kind == "ssis_package":
            attributes.update(
                {
                    "packageName": str(model.get("package_name") or ""),
                    "packageFile": str(model.get("package_file") or ""),
                }
            )
        attributes.update(flow.get("process_attributes") or {})
        legacy_identities: tuple[str, ...] = ()
        if artifact_kind == "ssis_package":
            legacy_identities = (
                f"poc-ssis://{artifact_name}/"
                f"{flow_name.lower().replace(' ', '-')}".lower(),
            )
        processes.append(
            Process(
                qualified_name=process_qn,
                name=str(
                    flow.get("process_name")
                    or f"SSIS: {artifact_name} / {flow_name}"
                ),
                process_type=process_type,
                inputs=tuple(str(item) for item in flow.get("source_qns", [])),
                outputs=tuple(str(item) for item in flow.get("sink_qns", [])),
                attributes=attributes,
                legacy_qualified_names=legacy_identities,
                source_reference=source_reference,
            )
        )
        process_by_reference[source_reference] = process_qn
        process_by_name.setdefault(flow_name, []).append(process_qn)

    mappings = []
    for value in model["column_mappings"]:
        reference = value.get("data_flow_ref")
        process_qn = (
            process_by_reference.get(str(reference)) if reference else None
        )
        if not process_qn:
            candidates = process_by_name.get(str(value.get("data_flow")), [])
            if len(candidates) != 1:
                raise ValueError(
                    "Column mapping could not be assigned to exactly one "
                    f"process: {value!r}"
                )
            process_qn = candidates[0]
        mappings.append(
            FieldMapping(
                process_qualified_name=process_qn,
                source_asset_qualified_name=str(value["source_qn"]),
                source_field=str(value["source_column"]),
                target_asset_qualified_name=str(value["sink_qn"]),
                target_field=str(value["sink_column"]),
                transform=str(value.get("via") or "passthrough"),
            )
        )

    source_metadata = {
        key: value
        for key, value in model.items()
        if key
        not in (
            "tables",
            "data_flows",
            "column_mappings",
            "process_attributes",
        )
    }
    source_metadata.update(metadata or {})
    return LineageGraph(
        artifact_name=artifact_name,
        artifact_qualified_name=artifact_qn,
        artifact_kind=artifact_kind,
        attributes=global_process_attributes,
        assets=tuple(assets),
        processes=tuple(processes),
        field_mappings=tuple(mappings),
        metadata=source_metadata,
    )


def graph_to_legacy(graph: LineageGraph) -> LegacyLineageModel:
    """Serialize the canonical graph into the shared POC-compatible shape."""
    process_types = {item.process_type for item in graph.processes}
    if len(process_types) != 1:
        raise ValueError(
            "Legacy serialization requires one process type per graph."
        )
    tables = {}
    for asset in graph.assets:
        tables[asset.qualified_name] = {
            "qualified_name": asset.qualified_name,
            **dict(asset.attributes),
            "table": asset.name,
            "columns": [field.name for field in asset.fields],
        }
    data_flows = []
    for process in graph.processes:
        data_flows.append(
            {
                "name": process.source_reference or process.name,
                "ref_id": process.source_reference or process.qualified_name,
                "process_qualified_name": process.qualified_name,
                "process_name": process.name,
                "source_qns": list(process.inputs),
                "sink_qns": list(process.outputs),
                "process_attributes": dict(process.attributes),
            }
        )
    mappings = []
    process_lookup = {
        item.qualified_name: item for item in graph.processes
    }
    for mapping in graph.field_mappings:
        process = process_lookup[mapping.process_qualified_name]
        mappings.append(
            {
                "source_qn": mapping.source_asset_qualified_name,
                "source_column": mapping.source_field,
                "sink_qn": mapping.target_asset_qualified_name,
                "sink_column": mapping.target_field,
                "via": mapping.transform,
                "data_flow": process.source_reference or process.name,
                "data_flow_ref": (
                    process.source_reference or process.qualified_name
                ),
            }
        )
    return {
        "artifact_kind": graph.artifact_kind,
        "artifact_name": graph.artifact_name,
        "artifact_qualified_name": graph.artifact_qualified_name,
        "process_type": next(iter(process_types)),
        "process_attributes": dict(graph.attributes),
        "tables": tables,
        "data_flows": data_flows,
        "column_mappings": mappings,
    }


def read_legacy_model(path: str | Path) -> LegacyLineageModel:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Legacy lineage model '{path}' must be an object.")
    return value


def write_legacy_model(
    model: LegacyLineageModel,
    path: str | Path,
) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(model, indent=2), encoding="utf-8")
    temporary.replace(output)
    return output

