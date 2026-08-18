"""Stored-procedure lineage extraction using ScriptDom or strict sqlglot."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from ...domain.legacy import LegacyLineageModel as LineageModel

PROCESS_TYPE = "poc_sql_stored_procedure"
SCRIPTDOM_PACKAGE_VERSION = "180.78.1"
Backend = Literal["auto", "scriptdom", "sqlglot"]

PROCEDURE_DEFINITION_SQL = """
SELECT sm.definition
FROM sys.procedures AS p
INNER JOIN sys.schemas AS s ON s.schema_id = p.schema_id
INNER JOIN sys.sql_modules AS sm ON sm.object_id = p.object_id
WHERE s.name = ?
  AND p.name = ?;
"""


class StoredProcedureLineageError(RuntimeError):
    """Base error for stored-procedure extraction."""


class UnsupportedSqlError(StoredProcedureLineageError):
    """Raised when a procedure uses a deliberately unsupported SQL shape."""


class ScriptDomUnavailable(StoredProcedureLineageError):
    """Raised when the optional ScriptDom runtime cannot be loaded."""


def _load_pyodbc() -> Any:
    try:
        import pyodbc
    except ModuleNotFoundError as exc:
        raise StoredProcedureLineageError(
            "SQL Server extraction requires the 'sqlserver' extra. Install "
            "with: pip install 'lineage-utility[sqlserver]'"
        ) from exc
    return pyodbc


@dataclass(frozen=True)
class StoredProcedureOptions:
    server: str
    database: str
    schema: str
    procedure: str


@dataclass(frozen=True)
class TableIdentity:
    server: str
    database: str
    schema: str
    table: str
    alias: str

    @property
    def qualified_name(self) -> str:
        server = self.server.replace("\\", ".")
        return (
            f"mssql://{server}/{self.database}/{self.schema}/{self.table}".lower()
        )

    @classmethod
    def from_parts(
        cls,
        parts: list[str],
        alias: str | None,
        options: StoredProcedureOptions,
    ) -> "TableIdentity":
        server = options.server
        database = options.database
        schema = options.schema
        if len(parts) == 1:
            table = parts[0]
        elif len(parts) == 2:
            schema, table = parts
        elif len(parts) == 3:
            database, schema, table = parts
        elif len(parts) == 4:
            server, database, schema, table = parts
        else:
            raise UnsupportedSqlError(
                f"Unsupported table name '{'.'.join(parts)}'."
            )
        return cls(server, database, schema, table, alias or table)


def _unsupported(number: int, reason: str) -> UnsupportedSqlError:
    return UnsupportedSqlError(f"INSERT statement {number}: {reason}.")


def _driver_name(requested: str | None = None) -> str:
    pyodbc = _load_pyodbc()
    installed = {driver.casefold(): driver for driver in pyodbc.drivers()}
    if requested:
        match = installed.get(requested.casefold())
        if not match:
            raise StoredProcedureLineageError(
                f"ODBC driver '{requested}' is not installed."
            )
        return match
    for preferred in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if preferred.casefold() in installed:
            return installed[preferred.casefold()]
    raise StoredProcedureLineageError(
        "Install ODBC Driver 18 or 17 for SQL Server before reading procedures."
    )


def build_connection_string(
    options: StoredProcedureOptions,
    *,
    odbc_driver: str | None = None,
) -> str:
    driver = _driver_name(odbc_driver)
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={options.server};"
        f"DATABASE={options.database};"
        "Trusted_Connection=yes;"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
        "APP=LineagePOC.LineageExtractor;"
        "Connection Timeout=30;"
    )


def read_procedure_definition(
    options: StoredProcedureOptions,
    *,
    odbc_driver: str | None = None,
) -> str:
    pyodbc = _load_pyodbc()
    connection_string = build_connection_string(
        options, odbc_driver=odbc_driver
    )
    with pyodbc.connect(connection_string, timeout=30, autocommit=True) as connection:
        connection.timeout = 30
        cursor = connection.cursor()
        row = cursor.execute(
            PROCEDURE_DEFINITION_SQL, options.schema, options.procedure
        ).fetchone()
    if not row or not isinstance(row[0], str):
        raise StoredProcedureLineageError(
            f"Stored procedure [{options.database}].[{options.schema}]."
            f"[{options.procedure}] was not found or its definition is encrypted."
        )
    return row[0]


def discover_scriptdom_dll(explicit: str | Path | None = None) -> Path | None:
    """Find ScriptDom in an explicit path or the standard NuGet cache."""
    configured = explicit or os.environ.get("SCRIPTDOM_DLL")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not candidate.is_file():
            raise ScriptDomUnavailable(f"ScriptDom DLL was not found: {candidate}")
        return candidate

    package_root = (
        Path.home()
        / ".nuget"
        / "packages"
        / "microsoft.sqlserver.transactsql.scriptdom"
    )
    if not package_root.is_dir():
        return None

    preferred = package_root / SCRIPTDOM_PACKAGE_VERSION
    version_dirs = [preferred] if preferred.is_dir() else []
    version_dirs.extend(
        path
        for path in sorted(package_root.iterdir(), reverse=True)
        if path.is_dir() and path != preferred
    )
    for version_dir in version_dirs:
        for target in ("net8.0", "netstandard2.1", "netstandard2.0", "net472"):
            candidate = (
                version_dir
                / "lib"
                / target
                / "Microsoft.SqlServer.TransactSql.ScriptDom.dll"
            )
            if candidate.is_file():
                return candidate.resolve()
    return None


@lru_cache(maxsize=4)
def _load_scriptdom(dll_path: str) -> SimpleNamespace:
    try:
        from pythonnet import load

        load("coreclr")
        import clr

        clr.AddReference(dll_path)
        from Microsoft.SqlServer.TransactSql.ScriptDom import (
            ColumnReferenceExpression,
            InsertStatement,
            NamedTableReference,
            QualifiedJoin,
            QuerySpecification,
            SelectInsertSource,
            SelectScalarExpression,
            Sql160ScriptGenerator,
            TSql160Parser,
            TSqlFragment,
        )
        from System.Collections import IEnumerable
        from System.IO import StringReader
    except Exception as exc:
        raise ScriptDomUnavailable(
            f"Could not load ScriptDom through pythonnet: {exc}"
        ) from exc
    return SimpleNamespace(
        ColumnReferenceExpression=ColumnReferenceExpression,
        InsertStatement=InsertStatement,
        NamedTableReference=NamedTableReference,
        QualifiedJoin=QualifiedJoin,
        QuerySpecification=QuerySpecification,
        SelectInsertSource=SelectInsertSource,
        SelectScalarExpression=SelectScalarExpression,
        Sql160ScriptGenerator=Sql160ScriptGenerator,
        TSql160Parser=TSql160Parser,
        TSqlFragment=TSqlFragment,
        IEnumerable=IEnumerable,
        StringReader=StringReader,
    )


def _scriptdom_nodes(root, target_type, runtime: SimpleNamespace) -> list:
    """Traverse ScriptDom fragment properties without requiring a C# visitor."""
    found = []
    stack = [root]
    seen: set[tuple[str, int, int]] = set()
    while stack:
        node = stack.pop()
        node_type = node.GetType()
        key = (
            node_type.FullName,
            int(getattr(node, "StartOffset", -1)),
            int(getattr(node, "FragmentLength", -1)),
        )
        if key in seen:
            continue
        seen.add(key)
        if isinstance(node, target_type):
            found.append(node)
        children = []
        for prop in node_type.GetProperties():
            if prop.GetIndexParameters().Length:
                continue
            try:
                value = prop.GetValue(node, None)
            except Exception:
                continue
            if isinstance(value, runtime.TSqlFragment):
                children.append(value)
            elif (
                isinstance(value, runtime.IEnumerable)
                and not isinstance(value, str)
            ):
                try:
                    children.extend(
                        item
                        for item in value
                        if isinstance(item, runtime.TSqlFragment)
                    )
                except Exception:
                    continue
        stack.extend(reversed(children))
    return sorted(found, key=lambda item: int(item.StartOffset))


def _scriptdom_sql(fragment, generator) -> str:
    return re.sub(r"\s+", " ", generator.GenerateScript(fragment).strip())


def _scriptdom_table(reference, options: StoredProcedureOptions) -> TableIdentity:
    parts = [identifier.Value for identifier in reference.SchemaObject.Identifiers]
    alias = reference.Alias.Value if reference.Alias is not None else None
    return TableIdentity.from_parts(parts, alias, options)


def _ensure_permanent(table: TableIdentity, statement_number: int) -> None:
    if table.table.startswith("#"):
        raise _unsupported(statement_number, "temporary tables are not supported")


def _new_model(
    options: StoredProcedureOptions,
    definition: str,
) -> LineageModel:
    normalized_server = options.server.replace("\\", ".").lower()
    procedure_name = (
        f"{options.database}.{options.schema}.{options.procedure}"
    )
    procedure_qn = (
        f"mssql-sp://{normalized_server}/{options.database}/"
        f"{options.schema}/{options.procedure}"
    ).lower()
    return {
        "artifact_kind": "stored_procedure",
        "artifact_name": procedure_name,
        "artifact_qualified_name": procedure_qn,
        "process_type": PROCESS_TYPE,
        "process_attributes": {
            "procedureName": options.procedure,
            "procedureSchema": options.schema,
            "databaseName": options.database,
            "serverName": options.server,
            "definitionHash": hashlib.sha256(
                definition.encode("utf-8")
            ).hexdigest(),
        },
        "tables": {},
        "data_flows": [],
        "column_mappings": [],
    }


def _touch_table(model: LineageModel, table: TableIdentity) -> None:
    model["tables"].setdefault(
        table.qualified_name,
        {
            "qualified_name": table.qualified_name,
            "server": table.server,
            "database": table.database,
            "schema": table.schema,
            "table": table.table,
            "columns": [],
        },
    )


def _touch_column(
    model: LineageModel,
    table: TableIdentity,
    column: str,
) -> None:
    _touch_table(model, table)
    columns = model["tables"][table.qualified_name]["columns"]
    if column.casefold() not in {item.casefold() for item in columns}:
        columns.append(column)


def _finalize_model(model: LineageModel) -> LineageModel:
    ordered_tables = {}
    for qualified_name in sorted(model["tables"], key=str.casefold):
        table = model["tables"][qualified_name]
        table["columns"] = sorted(table["columns"], key=str.casefold)
        ordered_tables[qualified_name] = table
    model["tables"] = ordered_tables
    return model


def _resolve_scriptdom_columns(
    fragment,
    tables: list[TableIdentity],
    runtime: SimpleNamespace,
    generator,
) -> list[tuple[TableIdentity, str]]:
    resolved = []
    seen = set()
    for reference in _scriptdom_nodes(
        fragment, runtime.ColumnReferenceExpression, runtime
    ):
        identifiers = [
            identifier.Value
            for identifier in reference.MultiPartIdentifier.Identifiers
        ]
        if not identifiers:
            continue
        column = identifiers[-1]
        if len(identifiers) == 1:
            if len(tables) != 1:
                raise StoredProcedureLineageError(
                    f"Column '{column}' is unqualified while the SELECT reads "
                    "multiple tables. Qualify it with a table alias."
                )
            table = tables[0]
        else:
            qualifier = identifiers[-2]
            matches = [
                candidate
                for candidate in tables
                if qualifier.casefold()
                in (candidate.alias.casefold(), candidate.table.casefold())
            ]
            if len(matches) != 1:
                raise StoredProcedureLineageError(
                    f"Column reference '{_scriptdom_sql(reference, generator)}' "
                    "could not be resolved to exactly one FROM table."
                )
            table = matches[0]
        key = (table.qualified_name.casefold(), column.casefold())
        if key not in seen:
            seen.add(key)
            resolved.append((table, column))
    return resolved


def _build_with_scriptdom(
    options: StoredProcedureOptions,
    definition: str,
    dll_path: Path,
) -> LineageModel:
    runtime = _load_scriptdom(str(dll_path))
    fragment, errors = runtime.TSql160Parser(True).Parse(
        runtime.StringReader(definition)
    )
    if errors.Count:
        detail = os.linesep.join(
            f"line {error.Line}, column {error.Column}: {error.Message}"
            for error in errors
        )
        raise StoredProcedureLineageError(
            f"ScriptDom could not parse the procedure:{os.linesep}{detail}"
        )

    inserts = _scriptdom_nodes(fragment, runtime.InsertStatement, runtime)
    if not inserts:
        raise StoredProcedureLineageError(
            "No INSERT ... SELECT statement was found. Dynamic SQL, temporary "
            "tables, and procedures without a table-to-table INSERT are outside "
            "the supported scope."
        )

    model = _new_model(options, definition)
    procedure_qn = model["artifact_qualified_name"]
    generator = runtime.Sql160ScriptGenerator()
    for index, statement in enumerate(inserts, start=1):
        specification = statement.InsertSpecification
        if not isinstance(specification.Target, runtime.NamedTableReference):
            raise _unsupported(
                index, "the INSERT target is not a permanent named table"
            )
        if not isinstance(specification.InsertSource, runtime.SelectInsertSource):
            raise _unsupported(
                index, "the INSERT source is not a SELECT statement"
            )
        if statement.WithCtesAndXmlNamespaces is not None:
            raise _unsupported(index, "CTEs are not supported")
        query = specification.InsertSource.Select
        if not isinstance(query, runtime.QuerySpecification):
            raise _unsupported(
                index,
                "set operators and nested query expressions are not supported",
            )
        nested_queries = _scriptdom_nodes(
            query, runtime.QuerySpecification, runtime
        )
        if len(nested_queries) > 1:
            raise _unsupported(index, "nested queries are not supported")

        target = _scriptdom_table(specification.Target, options)
        _ensure_permanent(target, index)
        source_references = (
            _scriptdom_nodes(
                query.FromClause, runtime.NamedTableReference, runtime
            )
            if query.FromClause is not None
            else []
        )
        source_tables = [
            _scriptdom_table(reference, options)
            for reference in source_references
        ]
        if not source_tables:
            raise _unsupported(index, "no permanent source table was found")
        aliases = set()
        for table in source_tables:
            _ensure_permanent(table, index)
            alias_key = table.alias.casefold()
            if alias_key in aliases:
                raise StoredProcedureLineageError(
                    f"Duplicate table alias '{table.alias}' is not supported."
                )
            aliases.add(alias_key)

        target_columns = [
            column.MultiPartIdentifier.Identifiers[
                column.MultiPartIdentifier.Identifiers.Count - 1
            ].Value
            for column in specification.Columns
        ]
        select_elements = list(query.SelectElements)
        if not target_columns:
            raise _unsupported(
                index, "the INSERT must specify its target column list"
            )
        if not all(
            isinstance(element, runtime.SelectScalarExpression)
            for element in select_elements
        ):
            raise _unsupported(
                index,
                "SELECT * and non-scalar select elements are not supported",
            )
        if len(target_columns) != len(select_elements):
            raise _unsupported(
                index,
                f"the INSERT has {len(target_columns)} target columns but the "
                f"SELECT has {len(select_elements)} expressions",
            )

        _touch_table(model, target)
        for table in source_tables:
            _touch_table(model, table)

        flow_ref = f"{procedure_qn}/statement/insert-{index}"
        if len(inserts) == 1:
            flow_name = f"{options.schema}.{options.procedure}"
            process_name = (
                f"SQL: {options.database}.{options.schema}.{options.procedure}"
            )
        else:
            flow_name = (
                f"{options.schema}.{options.procedure} / INSERT {index}"
            )
            process_name = (
                f"SQL: {options.database}.{options.schema}.{options.procedure} "
                f"/ INSERT {index}"
            )

        join_conditions = []
        if query.FromClause is not None:
            for join in _scriptdom_nodes(
                query.FromClause, runtime.QualifiedJoin, runtime
            ):
                if join.SearchCondition is None:
                    continue
                condition = _scriptdom_sql(join.SearchCondition, generator)
                if condition.casefold() not in {
                    item.casefold() for item in join_conditions
                }:
                    join_conditions.append(condition)
                for table, column in _resolve_scriptdom_columns(
                    join.SearchCondition, source_tables, runtime, generator
                ):
                    _touch_column(model, table, column)

        model["data_flows"].append(
            {
                "name": flow_name,
                "ref_id": flow_ref,
                "process_qualified_name": flow_ref,
                "process_name": process_name,
                "source_qns": sorted(
                    {table.qualified_name for table in source_tables},
                    key=str.casefold,
                ),
                "sink_qns": [target.qualified_name],
                "process_attributes": {
                    "statementType": "INSERT_SELECT",
                    "joinConditions": (
                        "; ".join(join_conditions)
                        if join_conditions
                        else "(none)"
                    ),
                },
            }
        )

        for target_column, select_element in zip(
            target_columns, select_elements, strict=True
        ):
            expression = select_element.Expression
            source_columns = _resolve_scriptdom_columns(
                expression, source_tables, runtime, generator
            )
            if not source_columns:
                raise _unsupported(
                    index,
                    f"target column '{target_column}' is populated without a "
                    "resolvable source column",
                )
            via = (
                "passthrough"
                if isinstance(expression, runtime.ColumnReferenceExpression)
                else _scriptdom_sql(expression, generator)
            )
            _touch_column(model, target, target_column)
            for source_table, source_column in source_columns:
                _touch_column(model, source_table, source_column)
                model["column_mappings"].append(
                    {
                        "source_qn": source_table.qualified_name,
                        "source_column": source_column,
                        "sink_qn": target.qualified_name,
                        "sink_column": target_column,
                        "via": via,
                        "data_flow": flow_name,
                        "data_flow_ref": flow_ref,
                    }
                )
    return _finalize_model(model)


def _sqlglot_table(table, options: StoredProcedureOptions) -> TableIdentity:
    parts = [part.name for part in table.parts]
    return TableIdentity.from_parts(parts, table.alias or None, options)


def _sqlglot_is_temporary(table) -> bool:
    return any(
        bool(identifier.args.get("temporary"))
        for identifier in table.find_all(type(table.this))
        if hasattr(identifier, "args")
    ) or table.name.startswith("#")


def _sqlglot_sql(expression) -> str:
    sql = re.sub(r"\s+", " ", expression.sql(dialect="tsql").strip())
    sql = sql.replace("CAST(", "CAST (")
    sql = re.sub(r"\s+AS NUMERIC\(", " AS DECIMAL (", sql)
    return sql


def _resolve_sqlglot_columns(
    expression,
    tables: list[TableIdentity],
    exp,
) -> list[tuple[TableIdentity, str]]:
    resolved = []
    seen = set()
    for reference in expression.find_all(exp.Column):
        if isinstance(reference.this, exp.Star):
            continue
        identifiers = [part.name for part in reference.parts]
        if not identifiers:
            continue
        column = identifiers[-1]
        if len(identifiers) == 1:
            if len(tables) != 1:
                raise StoredProcedureLineageError(
                    f"Column '{column}' is unqualified while the SELECT reads "
                    "multiple tables. Qualify it with a table alias."
                )
            table = tables[0]
        else:
            qualifier = identifiers[-2]
            matches = [
                candidate
                for candidate in tables
                if qualifier.casefold()
                in (candidate.alias.casefold(), candidate.table.casefold())
            ]
            if len(matches) != 1:
                raise StoredProcedureLineageError(
                    f"Column reference '{_sqlglot_sql(reference)}' could not be "
                    "resolved to exactly one FROM table."
                )
            table = matches[0]
        key = (table.qualified_name.casefold(), column.casefold())
        if key not in seen:
            seen.add(key)
            resolved.append((table, column))
    return resolved


def _build_with_sqlglot(
    options: StoredProcedureOptions,
    definition: str,
) -> LineageModel:
    try:
        from sqlglot import exp, parse

        roots = parse(definition, read="tsql", error_level="raise")
    except Exception as exc:
        raise StoredProcedureLineageError(
            f"sqlglot could not parse the procedure: {exc}"
        ) from exc

    inserts = [
        insert
        for root in roots
        for insert in root.find_all(exp.Insert)
    ]
    if not inserts:
        raise StoredProcedureLineageError(
            "No INSERT ... SELECT statement was found. Dynamic SQL, temporary "
            "tables, and procedures without a table-to-table INSERT are outside "
            "the supported scope."
        )

    model = _new_model(options, definition)
    procedure_qn = model["artifact_qualified_name"]
    for index, insert in enumerate(inserts, start=1):
        target_schema = insert.this
        if isinstance(target_schema, exp.Schema):
            target_expression = target_schema.this
            target_columns = [column.name for column in target_schema.expressions]
        else:
            target_expression = target_schema
            target_columns = []
        if not isinstance(target_expression, exp.Table):
            raise _unsupported(
                index, "the INSERT target is not a permanent named table"
            )
        if insert.args.get("with_") is not None:
            raise _unsupported(index, "CTEs are not supported")
        query = insert.expression
        if not isinstance(query, exp.Select):
            raise _unsupported(
                index,
                "set operators and nested query expressions are not supported",
            )
        if list(query.find_all(exp.Subquery)) or len(
            list(query.find_all(exp.Select))
        ) > 1:
            raise _unsupported(index, "nested queries are not supported")

        target = _sqlglot_table(target_expression, options)
        if _sqlglot_is_temporary(target_expression):
            raise _unsupported(index, "temporary tables are not supported")

        source_expressions = []
        from_clause = query.args.get("from_")
        if from_clause is not None and from_clause.this is not None:
            source_expressions.append(from_clause.this)
        source_expressions.extend(
            join.this for join in query.args.get("joins") or []
        )
        if not source_expressions:
            raise _unsupported(index, "no permanent source table was found")
        if not all(
            isinstance(source, exp.Table) for source in source_expressions
        ):
            raise _unsupported(index, "nested or computed table sources are not supported")

        source_tables = []
        aliases = set()
        for source in source_expressions:
            if _sqlglot_is_temporary(source):
                raise _unsupported(index, "temporary tables are not supported")
            table = _sqlglot_table(source, options)
            alias_key = table.alias.casefold()
            if alias_key in aliases:
                raise StoredProcedureLineageError(
                    f"Duplicate table alias '{table.alias}' is not supported."
                )
            aliases.add(alias_key)
            source_tables.append(table)

        select_elements = list(query.expressions)
        if not target_columns:
            raise _unsupported(
                index, "the INSERT must specify its target column list"
            )
        if any(
            isinstance(element, exp.Star)
            or any(True for _ in element.find_all(exp.Star))
            for element in select_elements
        ):
            raise _unsupported(
                index,
                "SELECT * and non-scalar select elements are not supported",
            )
        if len(target_columns) != len(select_elements):
            raise _unsupported(
                index,
                f"the INSERT has {len(target_columns)} target columns but the "
                f"SELECT has {len(select_elements)} expressions",
            )

        _touch_table(model, target)
        for table in source_tables:
            _touch_table(model, table)

        flow_ref = f"{procedure_qn}/statement/insert-{index}"
        if len(inserts) == 1:
            flow_name = f"{options.schema}.{options.procedure}"
            process_name = (
                f"SQL: {options.database}.{options.schema}.{options.procedure}"
            )
        else:
            flow_name = (
                f"{options.schema}.{options.procedure} / INSERT {index}"
            )
            process_name = (
                f"SQL: {options.database}.{options.schema}.{options.procedure} "
                f"/ INSERT {index}"
            )

        join_conditions = []
        for join in query.args.get("joins") or []:
            condition = join.args.get("on")
            if condition is None:
                continue
            condition_sql = _sqlglot_sql(condition)
            if condition_sql.casefold() not in {
                item.casefold() for item in join_conditions
            }:
                join_conditions.append(condition_sql)
            for table, column in _resolve_sqlglot_columns(
                condition, source_tables, exp
            ):
                _touch_column(model, table, column)

        model["data_flows"].append(
            {
                "name": flow_name,
                "ref_id": flow_ref,
                "process_qualified_name": flow_ref,
                "process_name": process_name,
                "source_qns": sorted(
                    {table.qualified_name for table in source_tables},
                    key=str.casefold,
                ),
                "sink_qns": [target.qualified_name],
                "process_attributes": {
                    "statementType": "INSERT_SELECT",
                    "joinConditions": (
                        "; ".join(join_conditions)
                        if join_conditions
                        else "(none)"
                    ),
                },
            }
        )

        for target_column, select_element in zip(
            target_columns, select_elements, strict=True
        ):
            expression = (
                select_element.this
                if isinstance(select_element, exp.Alias)
                else select_element
            )
            source_columns = _resolve_sqlglot_columns(
                expression, source_tables, exp
            )
            if not source_columns:
                raise _unsupported(
                    index,
                    f"target column '{target_column}' is populated without a "
                    "resolvable source column",
                )
            via = (
                "passthrough"
                if isinstance(expression, exp.Column)
                else _sqlglot_sql(expression)
            )
            _touch_column(model, target, target_column)
            for source_table, source_column in source_columns:
                _touch_column(model, source_table, source_column)
                model["column_mappings"].append(
                    {
                        "source_qn": source_table.qualified_name,
                        "source_column": source_column,
                        "sink_qn": target.qualified_name,
                        "sink_column": target_column,
                        "via": via,
                        "data_flow": flow_name,
                        "data_flow_ref": flow_ref,
                    }
                )
    return _finalize_model(model)


def build_model_from_definition(
    options: StoredProcedureOptions,
    definition: str,
    *,
    backend: Backend = "auto",
    scriptdom_dll: str | Path | None = None,
) -> tuple[LineageModel, str]:
    """Build a shared lineage model and return the backend actually used."""
    if backend not in ("auto", "scriptdom", "sqlglot"):
        raise ValueError(f"Unsupported parser backend: {backend}")
    if backend in ("auto", "scriptdom"):
        dll_path = discover_scriptdom_dll(scriptdom_dll)
        if dll_path is not None:
            try:
                return (
                    _build_with_scriptdom(options, definition, dll_path),
                    "scriptdom",
                )
            except ScriptDomUnavailable:
                if backend == "scriptdom":
                    raise
        elif backend == "scriptdom":
            raise ScriptDomUnavailable(
                "ScriptDom was requested but no DLL was discovered. Set "
                "SCRIPTDOM_DLL or restore the pinned NuGet package."
            )
    return _build_with_sqlglot(options, definition), "sqlglot"


def parse_stored_procedure(
    options: StoredProcedureOptions,
    *,
    backend: Backend = "auto",
    scriptdom_dll: str | Path | None = None,
    odbc_driver: str | None = None,
) -> tuple[LineageModel, str]:
    definition = read_procedure_definition(
        options, odbc_driver=odbc_driver
    )
    return build_model_from_definition(
        options,
        definition,
        backend=backend,
        scriptdom_dll=scriptdom_dll,
    )


def print_summary(
    model: LineageModel,
    *,
    backend: str,
    output_path: str | Path,
) -> None:
    print("=" * 68)
    print(f"Stored procedure: {model['artifact_name']}")
    print(f"Parser backend  : {backend}")
    print("=" * 68)
    for flow in model["data_flows"]:
        print(f"\n{flow['name']}")
        print(f"  Sources: {', '.join(flow['source_qns'])}")
        print(f"  Sinks  : {', '.join(flow['sink_qns'])}")
        for mapping in model["column_mappings"]:
            if mapping["data_flow_ref"] != flow["ref_id"]:
                continue
            via = (
                ""
                if mapping["via"] == "passthrough"
                else f" [{mapping['via']}]"
            )
            print(
                f"  {mapping['source_column']} -> "
                f"{mapping['sink_column']}{via}"
            )
    print(
        f"\nModel: {len(model['tables'])} tables, "
        f"{len(model['data_flows'])} process, "
        f"{len(model['column_mappings'])} column mappings"
    )
    print(f"Written to: {Path(output_path).resolve()}")
