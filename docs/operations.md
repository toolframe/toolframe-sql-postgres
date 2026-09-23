# Setup, environment and recovery

Provision a fresh database for each application environment. Development and
production may share a server, but must have different database names and their
own Toolframe application identities. `setup` requires PostgreSQL 16+ and a
cluster administrator able to create databases, roles and grants.

## Runtime environment

| Variable | Purpose |
| --- | --- |
| `TOOLFRAME_POSTGRES_APPLICATION_ID` | Must equal Core's application ID. |
| `TOOLFRAME_POSTGRES_DSN` | Application manager DSN; used for identity checks and full database dumps. |
| `TOOLFRAME_POSTGRES_STORES` | JSON map from plugin ID to its generated `user` and `password`. |
| `TOOLFRAME_POSTGRES_CONNECTION_LIMIT` | Maximum active store connections, default 16, range 1–128. |
| `TOOLFRAME_POSTGRES_PG_DUMP` | `pg_dump` executable, default found on `PATH`. |
| `TOOLFRAME_POSTGRES_PG_RESTORE` | `pg_restore` executable for the restore CLI. |

Setup returns the first three values in a mode-0600 JSON file. The application
manager is a database owner with read access to all stores for backups, without
cluster-wide superuser, database creation or role creation rights. Store controller
roles own their schemas and can migrate them; ordinary SQL switches to a
restricted role. Never replace generated runtime credentials with an administrator
DSN. Set TLS/libpq connection parameters on the setup DSN for remote deployments;
setup preserves them in runtime output.

Runtime credentials are not a sandbox: Core and trusted plugin processes inherit
environment variables. Do not install untrusted Python plugins in this deployment.
Use separate application processes, credentials and network boundaries for tenants.

## Add a plugin store

Stop or drain Core, run `setup` again with the same database/application/output file
and the requested owners, then restart Core with the updated environment. Setup
verifies retained credentials and ownership. Existing owners are retained when
omitted; omission never deletes data. An unrelated existing database or role is
rejected. Failed provisioning attempts clean up only newly created resources;
a cleanup failure requires administrator inspection. Back up the private output
file: an existing database cannot be silently adopted without its credentials.

## Migrations and backups

Core uses `storage.admin.plan` and explicit `apply` for changes to existing schema
history. Plans include the selected PostgreSQL SQL and checksums. Apply rechecks the
plan under the store's write lock. A changed, removed or downgraded migration is
rejected. First initialization needs no database dump; every later schema change
requires a successful whole-application `pg_dump` before transactional DDL starts.
An active transaction for that store must drain first.

Dumps and SHA-256 manifests live in Core's allocated storage directory under
`postgres/backups/`, with private file permissions. They include every provisioned
store and receipt/history metadata. Tool major versions must match the server;
a missing client, backup failure or version mismatch prevents schema changes.
Backups are not automatically pruned or uploaded. Monitor available disk space
and copy them to durable storage under an explicit retention policy.

A dump is a consistent database snapshot. Restoring it rolls **all stores** back
to that snapshot, including writes committed after it was taken. Core state,
configuration, authentication private keys and file assets are not in the dump.
Back up the application home separately and retain its stable application ID.
In particular, lost auth/OIDC keys must be restored, not regenerated over existing
store data. An initialized store without its retained key is refused by Core.

## Restore to a new database

Use only trusted operator-created dumps and manifests. Their checksum detects
accidental corruption, not malicious replacement. PostgreSQL restore executes
archive SQL with administrative rights; it is not an upload endpoint.

```sh
# Supply TOOLFRAME_POSTGRES_SETUP_DSN through your secret manager first.
toolframe-postgres restore \
  --database toolframe_recovery \
  --backup /private/backups/123-example.dump \
  --output /private/recovery-environment.json
```

The target database and output file must be new. The source, restore client and
target server must have the same major version. Restore recreates schema ownership,
restricted roles, current grants and default grants, then checks the owner inventory.
A failed restore leaves the newly created target isolated for administrator
inspection; it does not overwrite the live database or change Core's environment.
After validation, restart the single Core with the new runtime environment and the
matching application-home backup. Retain the original database until the recovery
has been verified. There is no SQLite-to-PostgreSQL import or automatic cutover.
