# Purview Lineage Extractor

Purview Lineage Extractor is a config-driven Python utility that extracts
technical lineage from ETL metadata, normalizes it into a versioned graph, and
publishes it to Microsoft Purview Data Map.

Built-in extractors support SQL Server Integration Services (SSIS) packages,
SQL Server stored procedures, and pre-generated JSON lineage models. The plugin
contracts allow additional ETL platforms and catalog publishers to be added
without changing the orchestration layer.

## Features

- Run multiple lineage jobs from one YAML configuration.
- Extract table-level and column-level lineage from SSIS `.dtsx` packages.
- Parse SQL Server `INSERT ... SELECT` stored procedures with Microsoft
  ScriptDom and a strict `sqlglot` fallback.
- Capture direct mappings, scalar expressions, joins, and referenced source
  columns.
- Publish SQL tables, columns, and processes to Microsoft Purview.
- Reuse stable entity identities so repeated runs update rather than duplicate.
- Skip unchanged publications using deterministic graph fingerprints.
- Isolate failures by package or stored procedure.
- Generate canonical model files and run manifests for auditability.
- Discover third-party extractors and publishers through Python entry points.
- Authenticate without embedded secrets using Azure Identity.

## Architecture

```mermaid
flowchart LR
    A[SSIS packages] --> D[Extractor plugins]
    B[SQL Server procedures] --> D
    C[JSON lineage models] --> D
    D --> E[Canonical lineage graph]
    E --> F[Job orchestrator]
    F --> G[Purview publisher]
    F --> H[Models and manifests]
    F <--> I[Incremental state]
    G --> J[Microsoft Purview Data Map]
```

Every extractor emits the same schema-versioned graph of assets, fields,
processes, edges, and field mappings. Publishers consume that graph without
depending on source-specific parser details.

## Supported sources

| Extractor | Input | Lineage coverage |
|---|---|---|
| `ssis` | SSIS `.dtsx` files | Data flows, source and destination tables, columns, mappings, and transformation expressions |
| `sqlserver-stored-procedure` | SQL Server procedure definitions | Permanent-table `INSERT ... SELECT`, aliases, joins, cross-database references, and scalar expressions |
| `json-model` | Canonical lineage JSON | Assets, fields, processes, edges, and field mappings supplied by another system |

## Repository layout

```text
purview-lineage-extractor/
  configs/                  Example multi-ETL configuration
  docs/                     Plugin authoring documentation
  scripts/                  PowerShell runner
  src/lineage_utility/
    contracts/              Extractor and publisher protocols
    domain/                 Canonical graph and validation
    extractors/             Built-in source adapters
    orchestration/          Configuration, state, and job runner
    plugins/                Built-in and entry-point registry
    publishers/purview/     Authentication, Atlas client, mapping, and verification
    schemas/                Configuration and lineage JSON schemas
    observability/          Text and JSON logging
    cli.py                  Command-line interface
  tests/                    Unit and compatibility tests
```

## Requirements

- Python 3.11 or newer.
- Microsoft ODBC Driver 18 or 17 for SQL Server when extracting stored
  procedures.
- Read access to `sys.procedures` and `sys.sql_modules`.
- An Azure identity with permission to manage Purview Data Map entities.
- Permission to manage custom type definitions when `manage_types: true`.

No C# build or subprocess is required.

## Installation

```powershell
git clone https://github.com/jijo-ms/purview-lineage-extractor.git
cd .\purview-lineage-extractor

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install ".[all]"
```

Install only the integrations required by the deployment:

```powershell
# SSIS and JSON extraction
python -m pip install .

# SQL Server stored-procedure extraction
python -m pip install ".[sqlserver]"

# Microsoft Purview publishing
python -m pip install ".[purview]"
```

For development:

```powershell
python -m pip install -e ".[dev]"
```

### ScriptDom discovery

ScriptDom is the preferred T-SQL parser. The utility searches:

1. The configured `scriptdom_dll` path.
2. The `SCRIPTDOM_DLL` environment variable.
3. Local NuGet package caches.
4. Common installed locations.

If ScriptDom cannot be loaded, the utility uses `sqlglot` with its T-SQL
dialect. The fallback is intentionally strict and rejects SQL shapes it cannot
model safely.

## Quick start

Create a working configuration from the example:

```powershell
Copy-Item .\configs\lineage.example.yml .\configs\lineage.yml
```

Set the Purview account name. Authentication uses the Azure Identity credential
chain by default:

```powershell
$env:PURVIEW_ACCOUNT = "my-purview-account"
az login
```

Edit `configs\lineage.yml` to point to the SSIS package directory and SQL
Server objects to extract. Paths are resolved relative to the YAML file.

Validate, preview, and run:

```powershell
lineage-util plugins
lineage-util validate --config .\configs\lineage.yml
lineage-util plan --config .\configs\lineage.yml
lineage-util run --config .\configs\lineage.yml
```

`plan` performs extraction and writes models and a manifest without publishing
or advancing incremental state. Review the generated models before the first
`run`.

## Configuration

```yaml
version: 1

runtime:
  output_dir: ../artifacts/models
  state_file: ../artifacts/state.json
  manifest_dir: ../artifacts/manifests

publishers:
  purview:
    plugin: purview
    account: ${PURVIEW_ACCOUNT}
    auth:
      type: ${PURVIEW_AUTH_TYPE:-default}
    manage_types: true
    allow_process_fallback: true

jobs:
  - name: ssis-packages
    extractor: ssis
    source:
      path: ${SSIS_PACKAGE_DIR:-../tests/fixtures}
      include: "*.dtsx"
      exclude: ["*.disabled.dtsx"]
    publishers: [purview]
    continue_on_error: true

  - name: warehouse-procedures
    enabled: false
    extractor: sqlserver-stored-procedure
    source:
      server: '${SQLSERVER_HOST:-localhost}'
      database: ${SQLSERVER_DATABASE:-Warehouse}
      schema: ${SQLSERVER_SCHEMA:-dbo}
      backend: auto
      procedures:
        - usp_LoadOrders
    publishers: [purview]
    continue_on_error: true
```

Environment variables use `${NAME}` for a required value and
`${NAME:-default}` for a value with a default. Keep credentials out of YAML.

An SSIS directory scan is top-level only, which prevents generated `obj` and
`bin` copies from being parsed. Each package and stored procedure is an
independent target, so one failure does not suppress successful siblings.

## Commands

| Command | Purpose |
|---|---|
| `lineage-util plugins` | List available extractor and publisher plugins |
| `lineage-util validate --config <file>` | Validate configuration and construct plugins |
| `lineage-util plan --config <file>` | Extract models without remote writes |
| `lineage-util run --config <file>` | Extract and publish configured jobs |
| `lineage-util run --config <file> --job <name>` | Run one named job |
| `lineage-util run --config <file> --force` | Publish even when fingerprints are unchanged |
| `lineage-util run --config <file> --fail-fast` | Stop after the first failed target |

Use `--log-format json` for machine-ingested logs. Any failed target produces
exit code 1.

The PowerShell wrapper exposes the common workflow:

```powershell
.\scripts\run-lineage.ps1 -Config .\configs\lineage.yml -Plan
.\scripts\run-lineage.ps1 -Config .\configs\lineage.yml -Job ssis-packages
```

## Purview publication

The publisher:

- creates required custom process type definitions when enabled;
- publishes physical SQL assets as `mssql_table` and `mssql_column`;
- attaches column mappings and transformation expressions to process entities;
- preserves process GUIDs through deterministic qualified names;
- retries bounded transient Atlas API failures;
- falls back to generic DataSet entities when configured; and
- reads entities back to verify inputs, outputs, and column mappings.

Authentication supports:

| `auth.type` | Recommended use |
|---|---|
| `default` | Workload identity, environment credential, managed identity, or local Azure CLI |
| `managed_identity` | Azure-hosted system-assigned or user-assigned identity |
| `azure_cli` | Explicit local development with `az login` |

Tokens use the `https://purview.azure.net/.default` scope.

## Stored-procedure SQL scope

Supported:

- Permanent-table `INSERT ... SELECT`.
- Aliases and cross-database table names.
- Inner and outer joins.
- Direct column mappings.
- Scalar expressions with one mapping per referenced source column.

Rejected rather than guessed:

- Dynamic SQL.
- Temporary tables.
- Common table expressions.
- Set operators.
- Nested queries and unsupported statement shapes.

This conservative behavior prevents uncertain lineage from being published as
fact.

## Canonical lineage model

The version `1.0` model contains:

- assets and fields;
- processes with typed input and output edges;
- field mappings associated with a process; and
- source attributes and operational metadata.

Validation rejects duplicate identities, invalid edges, missing fields, and
duplicate mappings. Fingerprints exclude operational metadata so run IDs and
parser diagnostics do not trigger catalog writes.

The JSON Schema is available at
`src\lineage_utility\schemas\lineage-graph-v1.schema.json`.

## Adding another ETL

External Python packages can register factories in these entry-point groups:

- `lineage_utility.extractors`
- `lineage_utility.publishers`

See [`docs/plugin-authoring.md`](docs/plugin-authoring.md) for the contracts and
a complete example. A system that already emits the canonical JSON schema can
use the `json-model` extractor without implementing a Python plugin.

## Development

```powershell
python -m compileall -q src tests
python -m pyflakes src tests
python -m pytest
python -m build
```

The test suite does not require a live Purview account. Live SQL Server and
Purview checks should use isolated non-production resources.
