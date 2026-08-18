from __future__ import annotations

from pathlib import Path

import pytest

from lineage_utility.extractors.sqlserver.extractor import (
    SqlServerStoredProcedureExtractor,
)
from lineage_utility.extractors.sqlserver.parser import (
    StoredProcedureLineageError,
    StoredProcedureOptions,
    UnsupportedSqlError,
    build_model_from_definition,
)
from lineage_utility.extractors.ssis import SsisExtractor

FIXTURES = Path(__file__).with_name("fixtures")
OPTIONS = StoredProcedureOptions(
    server=r"localhost\MSSQLSERVER2",
    database="SPLineageDW",
    schema="dbo",
    procedure="usp_LoadProcedureSales",
)


@pytest.mark.parametrize(
    ("file_name", "assets", "processes", "mappings"),
    [
        ("Package.dtsx", 2, 1, 6),
        ("ComplexPackage.dtsx", 6, 2, 18),
    ],
)
def test_ssis_compatibility(
    file_name: str,
    assets: int,
    processes: int,
    mappings: int,
) -> None:
    extractor = SsisExtractor()
    target = extractor.discover(
        {"path": str(FIXTURES / file_name)},
        base_dir=FIXTURES,
    )[0]
    graph = extractor.extract(target)

    assert len(graph.assets) == assets
    assert len(graph.processes) == processes
    assert len(graph.field_mappings) == mappings
    assert graph.artifact_qualified_name.endswith(
        f"/package/{file_name.lower()}"
    )
    assert all(item.source_reference for item in graph.processes)


def test_ssis_folder_discovery_is_non_recursive_and_excludable() -> None:
    targets = SsisExtractor().discover(
        {
            "path": str(FIXTURES),
            "exclude": ["Complex*"],
        },
        base_dir=FIXTURES,
    )

    assert [item.display_name for item in targets] == ["Package.dtsx"]


def test_stored_procedure_expression_and_join_lineage() -> None:
    definition = (
        FIXTURES / "usp_LoadProcedureSales.sql"
    ).read_text(encoding="utf-8")
    model, backend = build_model_from_definition(
        OPTIONS,
        definition,
        backend="sqlglot",
    )

    assert backend == "sqlglot"
    assert len(model["tables"]) == 3
    assert len(model["data_flows"]) == 1
    assert len(model["column_mappings"]) == 7
    mappings = {
        (
            item["source_column"],
            item["sink_column"],
            item["via"],
        )
        for item in model["column_mappings"]
    }
    assert (
        "CustomerName",
        "CustomerNameUpper",
        "UPPER(o.CustomerName)",
    ) in mappings
    assert (
        "Quantity",
        "TotalAmount",
        "CAST (o.Quantity * o.UnitPrice AS DECIMAL (18, 2))",
    ) in mappings
    assert (
        "OrderDate",
        "OrderMonth",
        "DATEFROMPARTS(YEAR(o.OrderDate), MONTH(o.OrderDate), 1)",
    ) in mappings


def test_stored_procedure_plugin_discovers_multiple_targets() -> None:
    targets = SqlServerStoredProcedureExtractor().discover(
        {
            "server": r"localhost\MSSQLSERVER2",
            "database": "SPLineageDW",
            "procedures": [
                "usp_One",
                {"schema": "etl", "name": "usp_Two"},
            ],
        },
        base_dir=FIXTURES,
    )

    assert [item.identifier for item in targets] == [
        "dbo.usp_One",
        "etl.usp_Two",
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "EXEC sp_executesql "
            "N'INSERT dbo.t(a) SELECT a FROM dbo.s';",
            "No INSERT ... SELECT",
        ),
        (
            "WITH c AS (SELECT a FROM dbo.s) "
            "INSERT dbo.t(a) SELECT a FROM c;",
            "CTEs are not supported",
        ),
        (
            "INSERT #t(a) SELECT a FROM dbo.s;",
            "temporary tables are not supported",
        ),
        (
            "INSERT dbo.t(a) SELECT a FROM dbo.s "
            "UNION ALL SELECT a FROM dbo.s2;",
            "set operators",
        ),
        (
            "INSERT dbo.t(a) SELECT x.a FROM "
            "(SELECT a FROM dbo.s) AS x;",
            "nested queries",
        ),
    ],
)
def test_unsupported_sql_is_rejected(
    body: str,
    message: str,
) -> None:
    definition = f"CREATE PROCEDURE dbo.p AS BEGIN {body} END;"

    with pytest.raises(
        (StoredProcedureLineageError, UnsupportedSqlError),
        match=message,
    ):
        build_model_from_definition(
            OPTIONS,
            definition,
            backend="sqlglot",
        )
