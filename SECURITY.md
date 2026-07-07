# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository
instead of opening a public issue: go to the **Security** tab of this
repository, then **Report a vulnerability**. This lets us discuss and fix
the issue before it is publicly disclosed.

Do not open a public GitHub issue for a suspected vulnerability.

## Response expectations

This is a solo-maintainer, beta-stage (`0.x`) project. There is no
dedicated security team and no SLA. In good faith, expect a best-effort
acknowledgement within 14 days. Fix timelines depend on severity and
maintainer availability; there is no guaranteed turnaround time.

## Supported versions

Only the latest released version on PyPI receives security fixes. There
are no backports to older releases while the project is pre-1.0.

## Scope

The following are **by design**, and reports about them will be closed as
expected behavior rather than treated as vulnerabilities:

- **Executing notebook SQL with the caller's privileges.** This tool
  re-executes the SQL stored in a DuckDB UI notebook against a target
  database, with whatever privileges the invoking user/process has. See
  the [Safety model](README.md#safety-model) section of the README for the
  full threat model and the mitigations already in place (transaction
  rollback by default, `--read-only`, `--no-external-access`, the
  execution confirmation prompt).
- **Known, documented `CREATE SECRET` masking limitations.** The README's
  Safety model section documents that masking only covers `CREATE SECRET`
  statement text: credentials embedded in other SQL forms (for example an
  `ATTACH 'postgres://user:password@host/db' AS pg;` connection string) or
  exposed via query results (for example
  `SELECT * FROM duckdb_secrets();`) are not masked. These are known,
  already-documented limitations, not vulnerabilities in themselves.

The following **are** in scope and should be reported privately:

- A **masking bypass**: a `CREATE SECRET` statement whose parameter values
  leak into the rendered HTML despite the masking that is supposed to
  cover it (i.e. the masking itself fails on input it claims to handle,
  as opposed to the already-documented gaps above).
- Any other vulnerability not covered by the "by design" notes above, for
  example in output-path confinement, notebook/database file handling, or
  the HTML rendering pipeline.
