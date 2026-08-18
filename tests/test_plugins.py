from __future__ import annotations

import pytest

from lineage_utility.extractors.ssis import SsisExtractor
from lineage_utility.plugins import PluginRegistry, create_builtin_registry
from lineage_utility.schemas import read_schema


def test_builtin_registry_is_discoverable() -> None:
    registry = create_builtin_registry(load_external=False)

    assert registry.extractor_names == (
        "json-model",
        "sqlserver-stored-procedure",
        "ssis",
    )
    assert registry.publisher_names == ("purview",)


def test_duplicate_plugin_registration_is_rejected() -> None:
    registry = PluginRegistry()
    registry.register_extractor("ssis", SsisExtractor)

    with pytest.raises(ValueError, match="Duplicate extractor"):
        registry.register_extractor("SSIS", SsisExtractor)


def test_json_schemas_are_packaged_resources() -> None:
    assert read_schema("config-v1")["properties"]["version"] == {"const": 1}
    assert read_schema("lineage-graph-v1")["properties"][
        "schema_version"
    ] == {"const": "1.0"}
