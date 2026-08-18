"""SQL Server stored-procedure extractor plugin."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...contracts import ExtractionTarget
from ...domain import LineageGraph
from ...domain.legacy import graph_from_legacy
from .parser import StoredProcedureOptions, parse_stored_procedure


def _required_text(
    source: Mapping[str, Any],
    name: str,
) -> str:
    value = source.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"SQL Server stored-procedure source requires '{name}'."
        )
    return value.strip()


def _procedure_specs(source: Mapping[str, Any]) -> list[tuple[str, str]]:
    default_schema = str(source.get("schema") or "dbo")
    raw = source.get("procedures")
    if raw is None:
        raw = [source.get("procedure")]
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            "SQL Server source requires 'procedure' or 'procedures'."
        )
    specs = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            specs.append((default_schema, item.strip()))
        elif isinstance(item, Mapping):
            name = item.get("name")
            schema = item.get("schema") or default_schema
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    "Each SQL Server procedure object requires 'name'."
                )
            specs.append((str(schema), name.strip()))
        else:
            raise ValueError(
                "SQL Server 'procedures' entries must be names or objects."
            )
    return specs


def _identifier(schema: str, procedure: str) -> str:
    value = f"{schema}.{procedure}"
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")


class SqlServerStoredProcedureExtractor:
    plugin_name = "sqlserver-stored-procedure"

    def discover(
        self,
        source: Mapping[str, Any],
        *,
        base_dir: Path,
    ) -> Sequence[ExtractionTarget]:
        del base_dir
        server = _required_text(source, "server")
        database = _required_text(source, "database")
        common_options = {
            "server": server,
            "database": database,
            "backend": str(source.get("backend") or "auto"),
            "scriptdom_dll": source.get("scriptdom_dll"),
            "odbc_driver": source.get("odbc_driver"),
        }
        targets = []
        for schema, procedure in _procedure_specs(source):
            normalized_server = server.replace("\\", ".").lower()
            source_uri = (
                f"mssql-sp://{normalized_server}/{database}/"
                f"{schema}/{procedure}"
            ).lower()
            targets.append(
                ExtractionTarget(
                    identifier=_identifier(schema, procedure),
                    display_name=f"{database}.{schema}.{procedure}",
                    source_uri=source_uri,
                    options={
                        **common_options,
                        "schema": schema,
                        "procedure": procedure,
                    },
                )
            )
        return targets

    def extract(self, target: ExtractionTarget) -> LineageGraph:
        options = target.options
        stored_procedure = StoredProcedureOptions(
            server=str(options["server"]),
            database=str(options["database"]),
            schema=str(options["schema"]),
            procedure=str(options["procedure"]),
        )
        legacy_model, backend = parse_stored_procedure(
            stored_procedure,
            backend=str(options["backend"]),
            scriptdom_dll=options.get("scriptdom_dll"),
            odbc_driver=(
                str(options["odbc_driver"])
                if options.get("odbc_driver")
                else None
            ),
        )
        return graph_from_legacy(
            legacy_model,
            metadata={
                "extractor": self.plugin_name,
                "parser_backend": backend,
                "source_uri": target.source_uri,
            },
        )

