from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from lineage_utility.contracts import ExtractionTarget, PublishResult
from lineage_utility.domain import (
    AssetField,
    DataAsset,
    FieldMapping,
    LineageGraph,
    Process,
)
from lineage_utility.orchestration import UtilityRunner, load_config
from lineage_utility.orchestration.state import RunLock
from lineage_utility.plugins import PluginRegistry


def graph_for(identifier: str) -> LineageGraph:
    source_qn = f"asset://{identifier}/source"
    target_qn = f"asset://{identifier}/target"
    process_qn = f"process://{identifier}"
    return LineageGraph(
        artifact_name=identifier,
        artifact_qualified_name=f"artifact://{identifier}",
        artifact_kind="test",
        assets=(
            DataAsset(
                qualified_name=source_qn,
                name="source",
                fields=(AssetField("id"),),
            ),
            DataAsset(
                qualified_name=target_qn,
                name="target",
                fields=(AssetField("id"),),
            ),
        ),
        processes=(
            Process(
                qualified_name=process_qn,
                name=identifier,
                process_type="Process",
                inputs=(source_qn,),
                outputs=(target_qn,),
            ),
        ),
        field_mappings=(
            FieldMapping(
                process_qualified_name=process_qn,
                source_asset_qualified_name=source_qn,
                source_field="id",
                target_asset_qualified_name=target_qn,
                target_field="id",
            ),
        ),
    )


class FakeExtractor:
    plugin_name = "fake"

    def discover(
        self,
        source: Mapping[str, Any],
        *,
        base_dir: Path,
    ) -> Sequence[ExtractionTarget]:
        del base_dir
        return [
            ExtractionTarget(
                identifier=name,
                display_name=name,
                source_uri=f"fake://{name}",
            )
            for name in source["targets"]
        ]

    def extract(self, target: ExtractionTarget) -> LineageGraph:
        if target.identifier == "bad":
            raise ValueError("bad target")
        return graph_for(target.identifier)


class FakePublisher:
    plugin_name = "fake-publisher"
    calls: list[str] = []

    def __init__(self, name: str, config: Mapping[str, Any]) -> None:
        self.instance_name = name
        self.config = config

    def publish(self, graph: LineageGraph) -> PublishResult:
        self.calls.append(graph.artifact_name)
        return PublishResult(
            publisher=self.instance_name,
            artifact_qualified_name=graph.artifact_qualified_name,
            success=True,
            process_guids={
                graph.processes[0].qualified_name: f"guid-{graph.artifact_name}"
            },
        )


def registry() -> PluginRegistry:
    value = PluginRegistry()
    value.register_extractor("fake", FakeExtractor)
    value.register_publisher(
        "fake-publisher",
        lambda name, config: FakePublisher(name, config),
    )
    return value


def write_config(
    path: Path,
    *,
    targets: tuple[str, ...] = ("one", "two"),
) -> Path:
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "runtime:",
                "  output_dir: artifacts/models",
                "  state_file: artifacts/state.json",
                "  manifest_dir: artifacts/manifests",
                "publishers:",
                "  catalog:",
                "    plugin: fake-publisher",
                "    endpoint: test",
                "jobs:",
                "  - name: sample",
                "    extractor: fake",
                "    source:",
                "      targets:",
                *[f"        - {item}" for item in targets],
                "    publishers: [catalog]",
                "    continue_on_error: true",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_runner_persists_state_and_skips_unchanged_models(
    tmp_path: Path,
) -> None:
    FakePublisher.calls.clear()
    config = load_config(write_config(tmp_path / "lineage.yml"))

    first = UtilityRunner(config=config, registry=registry()).run()
    second = UtilityRunner(config=config, registry=registry()).run()
    forced = UtilityRunner(config=config, registry=registry()).run(force=True)

    assert first.is_success and second.is_success and forced.is_success
    assert FakePublisher.calls == ["one", "two", "one", "two"]
    assert all(
        item.publications[0]["status"] == "unchanged"
        for item in second.results
    )
    state = json.loads(config.state_file.read_text(encoding="utf-8"))
    assert len(state["entries"]) == 2
    assert first.manifest_path.is_file()


def test_plan_writes_models_and_manifest_without_remote_state(
    tmp_path: Path,
) -> None:
    FakePublisher.calls.clear()
    config = load_config(write_config(tmp_path / "lineage.yml"))

    summary = UtilityRunner(config=config, registry=registry()).run(
        dry_run=True
    )

    assert summary.is_success
    assert FakePublisher.calls == []
    assert not config.state_file.exists()
    assert summary.manifest_path.is_file()
    assert all(Path(item.model_path).is_file() for item in summary.results)
    assert all(
        item.publications[0]["status"] == "planned"
        for item in summary.results
    )


def test_target_failures_are_isolated(tmp_path: Path) -> None:
    config = load_config(
        write_config(
            tmp_path / "lineage.yml",
            targets=("bad", "good"),
        )
    )

    summary = UtilityRunner(config=config, registry=registry()).run()

    assert [item.status for item in summary.results] == [
        "failed",
        "success",
    ]
    assert summary.failed == 1


def test_config_rejects_missing_environment_variable(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path / "lineage.yml")
    content = path.read_text(encoding="utf-8").replace(
        "endpoint: test",
        "endpoint: ${MISSING_LINEAGE_TEST_ENV}",
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="MISSING_LINEAGE_TEST_ENV"):
        load_config(path)


def test_config_rejects_unsafe_job_name(tmp_path: Path) -> None:
    path = write_config(tmp_path / "lineage.yml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "name: sample",
            "name: ../escape",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"jobs\[\]\.name"):
        load_config(path)


def test_job_stop_does_not_suppress_later_jobs(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "lineage.yml",
        targets=("bad", "not-run"),
    )
    content = path.read_text(encoding="utf-8")
    content = content.replace(
        "continue_on_error: true",
        "\n".join(
            [
                "continue_on_error: false",
                "  - name: later",
                "    extractor: fake",
                "    source:",
                "      targets: [good]",
                "    publishers: [catalog]",
            ]
        ),
    )
    path.write_text(content, encoding="utf-8")
    config = load_config(path)

    summary = UtilityRunner(config=config, registry=registry()).run()

    assert [(item.job, item.target) for item in summary.results] == [
        ("sample", "bad"),
        ("later", "good"),
    ]


def test_concurrent_run_is_rejected(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "lineage.yml"))
    lock_path = config.state_file.with_name(
        config.state_file.name + ".lock"
    )

    with RunLock(lock_path):
        with pytest.raises(RuntimeError, match="Another lineage run"):
            UtilityRunner(config=config, registry=registry()).run()

    assert not lock_path.exists()
