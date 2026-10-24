# Toolframe PostgreSQL storage

`sql-postgres` stores Toolframe plugin data in PostgreSQL. Entry Register,
Auth, MCP Auth and OIDC Auth use a separate schema per plugin inside one database
per application environment. Database access is loaded **only from the runtime
process environment**; credentials do not belong in `toolframe.toml`.

This release requires Toolframe SDK 0.4 and Core 0.7. Run one Core per
application. Core 0.7 can separately store its management journal in a dedicated
PostgreSQL database; SQLite remains the default. Configuration, private keys and
artifacts still need a persistent application home. PostgreSQL storage does not
make Core stateless or enable multiple replicas.
Existing SQLite data is not imported. Plugins are trusted Python code.

## Quick start

Use Python 3.14+, PostgreSQL 16+, and `pg_dump` / `pg_restore` with the **same major
version** as the server. PostgreSQL 16 is the integration-test baseline.

Install the host and provider CLI from PyPI:

```sh
uv venv --python 3.14.7 .venv
uv pip install --python .venv/bin/python \
  toolframe-core==0.7.0 toolframe-sql-postgres==0.2.1
.venv/bin/toolframe-core --home ./app init
```

Read `[application].id` from `app/toolframe.toml`. Supply an administrator DSN from
your secret manager as `TOOLFRAME_POSTGRES_SETUP_DSN`. It is used only by setup.
The CLI output file contains generated runtime credentials and must remain private.

```sh
.venv/bin/toolframe-postgres setup \
  --application-id '<id from app/toolframe.toml>' \
  --database toolframe_development \
  --owner entry-register --owner auth --owner mcp-auth --owner oidc-auth \
  --output ./app/.toolframe/postgres-environment.json
unset TOOLFRAME_POSTGRES_SETUP_DSN
```

Add the provider to `app/toolframe.toml`:

```toml
[plugins.sql-postgres]
source = "pypi:toolframe-sql-postgres==0.2.1"
enabled = true

[bindings]
"toolframe.sql" = "sql-postgres"
```

This configuration sends every SQL consumer to PostgreSQL. Core builds with
multiple SQL provider support can also run SQLite, using an explicit per-plugin
override such as `"entry-register/sql" = "sql-sqlite"` in `[bindings]`.
Choose bindings before initializing stores: changing a retained store's provider
is rejected and does not copy data. Earlier Core builds require one enabled
storage provider. The package version alone does not identify multiple-provider
support; build the matching Core revision when using mixed providers.

Install the matching business plugins
before enabling them: Entry Register 0.4, Auth 0.3, MCP Auth 0.3 and OIDC Auth 0.3.
Each store owner must be provisioned before Core starts its plugin.

```sh
.venv/bin/toolframe-postgres run \
  --env-file ./app/.toolframe/postgres-environment.json -- \
  .venv/bin/toolframe-core --home ./app serve
```

The `run` helper exports the private document as environment variables and replaces
itself with Core. It removes setup/test administrator variables. Containers can
inject the same runtime values directly; the provider does not read that file.

- [Setup, environment and recovery](docs/operations.md)
- [SQL and isolation contract](docs/storage-contract.md)
- [Development and verification](docs/development.md)
- [Domain glossary](docs/glossary.md) and [architecture decisions](docs/adr/0001-plugin-storage-boundary.md)

For release validation and Trusted Publisher setup, see [releasing](docs/releasing.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

For development hooks and contribution rules, see [Contributing](CONTRIBUTING.md).
