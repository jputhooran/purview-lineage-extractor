# Security policy

## Supported versions

Security fixes are applied to the latest code on `main`. Older commits and
unmaintained forks are not supported.

## Reporting a vulnerability

Do not report security vulnerabilities through a public issue.

Use **Report a vulnerability** on the repository's **Security** tab to submit a
private report. If private vulnerability reporting is unavailable, contact the
repository owner through their GitHub profile before sharing technical details.

Include:

- the affected version or commit;
- the potential impact;
- reproduction steps or a proof of concept;
- affected files or components; and
- any suggested mitigation.

Maintainers will acknowledge a complete report as soon as practical, validate
the issue, coordinate remediation, and credit the reporter unless anonymity is
requested.

## Sensitive information

Configuration must not contain client secrets, passwords, access tokens, or
connection credentials. Use Azure Identity and environment variables as
documented in the README.

If a credential is committed accidentally, revoke or rotate it immediately.
Removing it from the latest commit is not sufficient because Git history may
still contain the value.

## Scope

Security reports may include:

- credential exposure;
- unsafe authentication or authorization behavior;
- injection or path traversal;
- malicious lineage model handling;
- unintended remote writes;
- dependency vulnerabilities with a demonstrated impact; or
- disclosure of sensitive source metadata.

Parser limitations that reject unsupported input are not vulnerabilities unless
they can be used to bypass a documented security boundary.
