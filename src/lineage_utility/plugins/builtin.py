"""Register plugins shipped with the utility."""

from __future__ import annotations

from ..extractors.json_model import JsonModelExtractor
from ..extractors.sqlserver import SqlServerStoredProcedureExtractor
from ..extractors.ssis import SsisExtractor
from .registry import PluginRegistry


def create_builtin_registry(
    *,
    load_external: bool = True,
) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_extractor("ssis", SsisExtractor)
    registry.register_extractor(
        "sqlserver-stored-procedure",
        SqlServerStoredProcedureExtractor,
    )
    registry.register_extractor("json-model", JsonModelExtractor)

    from ..publishers.purview import create_purview_publisher

    registry.register_publisher("purview", create_purview_publisher)
    if load_external:
        registry.load_entry_point_plugins()
    return registry

