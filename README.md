# Toolframe PostgreSQL storage

`storage-postgres` stores Toolframe plugin data in PostgreSQL. Entry Register,
Auth, MCP Auth and OIDC Auth use a separate schema per plugin inside one database
per application environment. Database access is loaded **only from the runtime
process environment**; credentials do not belong in `toolframe.toml`.

This release requires Toolframe SDK 0.3 and Core 0.5.3 or 0.6. Run one Core per
application. Core 0.6 can separately store its management journal in a dedicated
PostgreSQL database; SQLite remains the default. Configuration, private keys and
artifacts still need a persistent application home. PostgreSQL storage does not
make Core stateless or enable multiple replicas.
Existing SQLite data is not imported. Plugins are trusted Python code.

## Quick start

Use Python 3.14+, PostgreSQL 16+, and `pg_dump` / `pg_restore` with the **same major
version** as the server. PostgreSQL 16 is the integration-test baseline.

The coordinated SDK/Core/plugin changes must be built together while they are
unreleased. From a directory containing those matching repository checkouts:

```sh
# Local development: build the runtime wheels into one directory.
uv build --wheel --no-sources --out-dir ./wheelhouse ./toolframe-sdk
uv build --wheel --no-sources --out-dir ./wheelhouse ./fastapi-cordis
uv build --wheel --no-sources --out-dir ./wheelhouse ./toolframe-core
uv build --wheel --no-sources --out-dir ./wheelhouse ./toolframe-storage-postgres
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python --find-links ./wheelhouse \
  toolframe-core==0.6.0 toolframe-storage-postgres==0.1.0
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

Add the provider to `app/toolframe.toml`, using the wheel's actual path and keeping
its directory in `packages.find_links` so SDK 0.3 can be resolved:

```toml
[plugins.storage-postgres]
source = "../wheelhouse/toolframe_storage_postgres-0.1.0-py3-none-any.whl"
enabled = true

[bindings]
"toolframe.storage.sql" = "storage-postgres"

[packages]
find_links = ["../wheelhouse"]
```

Enable only one storage provider. Build/install the matching business plugins
before enabling them: Entry Register 0.3, Auth 0.2, MCP Auth 0.2 and OIDC Auth 0.2.
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

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
