from __future__ import annotations

import pytest

from lineage_utility.domain import (
    AssetField,
    DataAsset,
    FieldMapping,
    LineageGraph,
    Process,
)


def build_graph(*, metadata: dict | None = None) -> LineageGraph:
    source = DataAsset(
        qualified_name="mssql://server/source/dbo/orders",
        name="orders",
        fields=(AssetField("OrderID"),),
    )
    target = DataAsset(
        qualified_name="mssql://server/dw/dbo/orders",
        name="orders",
        fields=(AssetField("OrderID"),),
    )
    process = Process(
        qualified_name="process://orders",
        name="Load orders",
        process_type="Process",
        inputs=(source.qualified_name,),
        outputs=(target.qualified_name,),
    )
    return LineageGraph(
        artifact_name="orders",
        artifact_qualified_name="artifact://orders",
        artifact_kind="test",
        assets=(source, target),
        processes=(process,),
        field_mappings=(
            FieldMapping(
                process_qualified_name=process.qualified_name,
                source_asset_qualified_name=source.qualified_name,
                source_field="OrderID",
                target_asset_qualified_name=target.qualified_name,
                target_field="OrderID",
            ),
        ),
        metadata=metadata or {},
    )


def test_fingerprint_ignores_operational_metadata() -> None:
    first = build_graph(metadata={"run_id": "one"})
    second = build_graph(metadata={"run_id": "two"})

    assert first.fingerprint == second.fingerprint
    assert LineageGraph.from_dict(first.to_dict()) == first


def test_field_mapping_must_follow_process_edges() -> None:
    graph = build_graph()
    process = graph.processes[0]

    with pytest.raises(ValueError, match="source is not an input"):
        LineageGraph(
            artifact_name=graph.artifact_name,
            artifact_qualified_name=graph.artifact_qualified_name,
            artifact_kind=graph.artifact_kind,
            assets=graph.assets,
            processes=(
                Process(
                    qualified_name=process.qualified_name,
                    name=process.name,
                    process_type=process.process_type,
                    inputs=(graph.assets[1].qualified_name,),
                    outputs=process.outputs,
                ),
            ),
            field_mappings=graph.field_mappings,
        )


def test_duplicate_field_mappings_are_rejected() -> None:
    graph = build_graph()

    with pytest.raises(ValueError, match="duplicate field mappings"):
        LineageGraph(
            artifact_name=graph.artifact_name,
            artifact_qualified_name=graph.artifact_qualified_name,
            artifact_kind=graph.artifact_kind,
            assets=graph.assets,
            processes=graph.processes,
            field_mappings=(
                graph.field_mappings[0],
                graph.field_mappings[0],
            ),
        )

