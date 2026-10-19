# Development and verification

Use matching sibling checkouts of SDK 0.4, Auth/MCP Auth/OIDC Auth 0.3 and Entry
Register 0.4. The committed lockfile's development sources are relative to this
layout; when using independent Git worktrees, materialize a separate test workspace
or build matching wheels and install them with `uv pip install --no-sources`.
Do not accidentally resolve dependencies from another task's checkout.

```sh
uv sync --locked --group dev --group compatibility
# Supply a DSN for a disposable PostgreSQL 16 test server through the environment.
# Set TOOLFRAME_POSTGRES_PG_DUMP / PG_RESTORE if the matching clients are not on PATH.
uv run --no-sync python scripts/check.py
uv build --no-sources
```

`TOOLFRAME_POSTGRES_TEST_ADMIN_DSN` must point to an isolated server, not production.
Tests create random `tf_test_…`/`tf_restore_…` databases and app-scoped roles and
remove only their own resources. Full checks fail if that DSN or a first-party
consumer dependency is missing. Bare `pytest` intentionally skips database tests
without a DSN and skips consumer tests without their dependencies; skips do not
establish integration support. Credentials are not written to test artifacts.
Use `--tb=short` when running integration tests to avoid automatic fixture dumps.

The suite covers owner/database-role isolation, migration dialects and history,
transaction fencing/rollback/expiry/cancellation, mutation receipts and lost commit
acknowledgement, failed provisioning, failed backup/version checks, and restoration
of data, schema history and grants into a fresh database. Consumer checks exercise
Entry Register optimistic updates, password sessions, MCP keys, and OAuth PKCE,
refresh rotation and replay rejection. SQLite regression suites remain in their
own provider/consumer repositories.

Installed-application acceptance is separate from package tests: build matching
wheels, install them into a distinct Core application home, provision its actual
application ID, and start Core through the environment helper. Verify Dashboard
creation/editing, password scopes, MCP client reads/revocation, OIDC login with a
controlled signed identity provider, OAuth exchange/replay, and persistence after
Core restart. Inspect the original application and back up its state before any
installation work; fresh PostgreSQL verification must preserve existing SQLite
data/configuration. Record revisions and results under an ignored local directory
or CI artifacts, not as permanent claims about later revisions.

This release does not verify multi-Core deployments, production PostgreSQL HA,
SQLite imports, arbitrary third-party SQL, cross-store transactions, untrusted
plugin isolation, or external managed-service backup automation. Remote CI pins
refer to coordinated repository commits; publish those commits before running
cross-repository CI. Local implementation does not push them automatically.

Core's optional `validate --runtime` scratch mode is deliberately unavailable for
this provider: changing only the local application home would reuse the inherited
database credentials. The provider declares `runtime_validation="separate_application"`
to reject that operation before any scratch worker starts. Use normal preflight
validation on the live application and a distinct provisioned application/database
for runtime checks.
