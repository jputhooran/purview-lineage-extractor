# Lineage Utility

`lineage-utility` is the production-oriented successor to the POC lineage
scripts. It runs multiple ETL lineage extractors through one versioned domain
model and publishes them through pluggable catalog adapters. The original
`lineage-extractor`, SSIS packages, and SQL assets remain untouched.

## Capabilities

- Config-driven multi-job runs with independent targets and failure isolation.
- Built-in SSIS, SQL Server stored-procedure, and JSON-model extractors.
- A validated canonical lineage model with deterministic fingerprints.
- A Microsoft Purview publisher with passwordless Azure Identity,
  bounded retries, stable GUID reuse, DataSet fallback, and read-back checks.
- Incremental publishing, atomic state, dry-run plans, and run manifests.
- Python entry-point contracts for additional ETLs and publishers.
- POC-compatible JSON output during migration.

## Layout

```text
lineage-utility/
  configs/                  Example multi-ETL configuration
  docs/                     Extension documentation
  scripts/                  PowerShell runner
  src/lineage_utility/
    contracts/              Extractor, publisher, credential, state protocols
    domain/                 Canonical graph and legacy adapters
    extractors/
      ssis/
      sqlserver/
      json_model.py
    orchestration/          Config loader, state store, multi-job runner
    plugins/                Built-in and entry-point registry
    publishers/purview/     Auth, Atlas client, mapper, publisher, type defs
    schemas/                Bundled configuration and graph JSON schemas
    observability/          Text and JSON logging
    cli.py
  tests/
```

## Install

Python 3.11 or newer is required. Create an isolated environment:

```powershell
cd .\lineage-utility
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install ".[all]"
```

Install only what the deployment uses:

```powershell
# SSIS and canonical JSON only
python -m pip install .

# SQL Server stored procedures
python -m pip install ".[sqlserver]"

# Purview publishing
python -m pip install ".[purview]"
```

For development, use `python -m pip install -e ".[dev]"`.

### SQL Server prerequisites

- Microsoft ODBC Driver 18 or 17 for SQL Server.
- Windows authentication access to `sys.procedures` and `sys.sql_modules`.
- Microsoft ScriptDom is preferred. The parser discovers
  `Microsoft.SqlServer.TransactSql.ScriptDom.dll` in an explicit configured
  path, `SCRIPTDOM_DLL`, local NuGet caches, and common installed locations.
- `sqlglot` with the T-SQL dialect is the strict fallback when ScriptDom
  cannot load.

No C# build or C# subprocess is required.

### Purview prerequisites

Grant the runtime identity permission to manage Atlas entities and, when
`manage_types` is enabled, custom type definitions. Supported authentication:

| `auth.type` | Intended use |
|---|---|
| `default` | Production credential chain, workload identity, or environment |
| `managed_identity` | Azure-hosted system/user-assigned managed identity |
| `azure_cli` | Local development after `az login` |

Tokens use the `https://purview.azure.net/.default` scope. Do not place
credentials or client secrets in configuration.

## Configure

Copy `configs\lineage.example.yml`. Paths are resolved relative to the YAML
file. `${NAME}` requires an environment variable; `${NAME:-default}` supplies
a default.

```yaml
version: 1
runtime:
  output_dir: artifacts/models
  state_file: artifacts/state.json
  manifest_dir: artifacts/manifests

publishers:
  catalog:
    plugin: purview
    account: ${PURVIEW_ACCOUNT}
    auth:
      type: default

jobs:
  - name: nightly-ssis
    extractor: ssis
    source:
      path: ../ssis
      include: "*.dtsx"
      exclude: ["*.disabled.dtsx"]
    publishers: [catalog]
    continue_on_error: true

  - name: warehouse-procedures
    extractor: sqlserver-stored-procedure
    source:
      server: '${SQLSERVER_HOST}'
      database: Warehouse
      schema: dbo
      backend: auto
      procedures:
        - usp_LoadOrders
        - schema: etl
          name: usp_LoadCustomers
    publishers: [catalog]
```

An SSIS directory scan is deliberately top-level only, preventing `obj` and
`bin` copies from being parsed. Each package and each procedure is a separate
target, so a failure does not suppress sibling targets.

## Run

```powershell
python -m lineage_utility plugins
python -m lineage_utility validate --config .\configs\lineage.example.yml
python -m lineage_utility plan --config .\configs\lineage.example.yml
python -m lineage_utility run --config .\configs\lineage.example.yml
python -m lineage_utility run --config .\configs\lineage.example.yml `
  --job ssis-packages --force
```

`plan` performs extraction and writes canonical models and a manifest without
remote writes or state advancement. `run` skips a publisher when both the
graph and publisher configuration are unchanged. `--force` bypasses that
check. Any failed target produces exit code 1; successful siblings remain
published unless `--fail-fast` or `continue_on_error: false` is set.

Runs sharing a state file are mutually exclusive to prevent duplicate remote
writes or lost state. A process crash can leave a `.lock` file beside the
state file; remove it only after confirming that no lineage run is active.

PowerShell wrapper:

```powershell
.\scripts\run-lineage.ps1 -Plan
.\scripts\run-lineage.ps1 -Job ssis-packages
```

Use `--log-format json` for machine-ingested logs.

## Canonical model

Every extractor returns schema version `1.0` with:

- assets and fields;
- processes with typed input and output edges;
- field mappings tied to one process;
- artifact attributes and operational metadata.

Construction rejects duplicate identities, invalid edges, missing fields, and
duplicate mappings. The fingerprint excludes operational metadata so run IDs
and parser diagnostics do not trigger catalog writes. See
`src\lineage_utility\schemas\lineage-graph-v1.schema.json`.

The `json-model` extractor accepts canonical or legacy POC models, providing a
low-friction bridge for other ETLs. `emit_legacy_model: true` writes both
formats.

## Stored-procedure SQL scope

Supported:

- Permanent-table `INSERT ... SELECT`.
- Aliases and cross-database names.
- Joins and join-table column lineage.
- Scalar expressions with one mapping per referenced source column.

Explicitly rejected rather than guessed:

- Dynamic SQL.
- Temporary tables.
- CTEs.
- Set operators.
- Nested queries and unsupported statement shapes.

ScriptDom is the authoritative parser. The `sqlglot` fallback is intentionally
strict and may reject valid T-SQL outside this scope.

## Extending ETL support

External packages register factories in:

- `lineage_utility.extractors`
- `lineage_utility.publishers`

See `docs\plugin-authoring.md` for contracts and examples. The built-in
`json-model` extractor can also onboard an ETL that already emits the
canonical schema without adding a Python plugin.

## Build and test

```powershell
python -m compileall -q src tests
python -m pyflakes src tests
python -m pytest
python -m build
```
