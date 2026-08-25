# Contributing

Thank you for helping improve Purview Lineage Extractor.

## Before you start

- Use an issue to discuss substantial features, new extractors, schema changes,
  or behavior that changes published identities.
- Keep changes focused and preserve stable qualified names unless the change is
  explicitly a migration.
- Never commit credentials, access tokens, customer data, or production ETL
  artifacts.

## Development setup

Python 3.11 or newer is required.

```powershell
git clone https://github.com/jputhooran/purview-lineage-extractor.git
cd .\purview-lineage-extractor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Making a change

1. Create a branch from the latest `main`.
2. Make the smallest complete change that addresses the issue.
3. Add or update tests for behavior changes.
4. Update documentation when configuration, commands, supported SQL, or plugin
   contracts change.
5. Run the checks below.
6. Open a pull request with the problem, approach, and verification results.

Do not push directly to `main`.

## Architecture rules

- Extractors convert source metadata into the canonical domain model. They must
  not call Purview APIs.
- Publishers consume the canonical model. They must not contain source-parser
  logic.
- Orchestration coordinates plugins, state, and failures without depending on a
  specific ETL platform.
- Unsupported or ambiguous lineage must be rejected explicitly rather than
  guessed.
- Qualified-name changes require special care because they affect Purview
  identity and GUID reuse.
- Configuration files must reference environment variables for deployment
  values and credentials.

See [the plugin authoring guide](docs/plugin-authoring.md) before adding an
extractor or publisher.

## Tests

Run the complete local checks:

```powershell
python -m compileall -q src tests
python -m pyflakes src tests
python -m pytest
python -m build
```

Use the smallest focused test while developing, then run the full suite before
opening a pull request. Unit tests must not require a live Purview account.

## Test fixtures

Fixtures must be synthetic and safe to publish:

- no real customer, employee, financial, or operational data;
- no production server, database, tenant, subscription, or account names;
- no usernames, workstation names, email addresses, or absolute user paths;
- no passwords, tokens, keys, certificates, or connection secrets; and
- no proprietary package logic copied from an employer or client.

Use fictitious schemas and integrated-security connection strings where a
connection definition is required to exercise a parser.

## Pull requests

Pull requests should:

- explain why the change is needed;
- describe any identity, model, or compatibility impact;
- include relevant test results;
- remain small enough to review confidently; and
- receive at least one approving review before merge.

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
