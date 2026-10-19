import asyncio
import os
import time
import uuid

import psycopg
import pytest
from conftest import caller, cleanup_database, schema
from psycopg import sql
from toolframe_sdk import Migration, PluginError

from toolframe_sql_postgres.plugin import ADMIN_SERVICE, SQL_SERVICE
from toolframe_sql_postgres.provision import provision


async def invoke(storage, method, **params):
    return await storage.invoke(SQL_SERVICE, method, params)


async def test_owner_isolation_and_sql_contract(storage):
    with caller():
        row = await invoke(
            storage,
            "execute_returning",
            sql="INSERT INTO entries(label) VALUES (?) RETURNING id",
            parameters=["alpha ? 100%"],
            mutation_id="create",
        )
        assert row == {"rows": [{"id": 1}], "rowcount": 1}
        assert await invoke(
            storage, "query", sql="SELECT label FROM entries WHERE id=?", parameters=[1]
        ) == [{"label": "alpha ? 100%"}]
        for statement in [
            "SELECT * FROM pg_roles",
            "SELECT * FROM public.entries",
            "SELECT pg_sleep(1)",
            "SELECT set_config('role','none',true)",
            "SELECT * FROM _toolframe_identity",
            "SELECT 1; DELETE FROM entries",
            "DELETE FROM entries",
            "WITH removed AS (DELETE FROM entries RETURNING *) SELECT * FROM removed",
        ]:
            with pytest.raises(PluginError):
                await invoke(storage, "query", sql=statement)
        with pytest.raises(PluginError):
            await storage.invoke(ADMIN_SERVICE, "inspect", {"owner": "beta"})
    with caller("beta"):
        assert await invoke(storage, "query", sql="SELECT * FROM entries") == []
    with (
        caller(application="another-application"),
        pytest.raises(PluginError, match="different application"),
    ):
        await invoke(storage, "query", sql="SELECT * FROM entries")


async def test_restricted_database_role_cannot_read_other_store(storage):
    owner = storage.settings.store_names("alpha")
    other = storage.settings.store_names("beta")
    async with await psycopg.AsyncConnection.connect(
        storage.settings.store_dsn("alpha")
    ) as connection:
        await connection.execute(
            sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner["runtime"]))
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await connection.execute(
                sql.SQL("SELECT * FROM {}.entries").format(
                    sql.Identifier(other["schema"])
                )
            )


async def test_transactions_receipts_fencing_and_poison(storage):
    with caller():
        tx = (await invoke(storage, "begin", mutation_id="first"))["transaction"]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES (?)",
            parameters=["retained"],
            transaction=tx,
        )
    for owner, generation, root in [
        ("beta", "generation-1", "root-1"),
        ("alpha", "different", "root-1"),
        ("alpha", "generation-1", "different"),
    ]:
        with (
            caller(owner, generation, root),
            pytest.raises(PluginError, match="not owned"),
        ):
            await invoke(storage, "query", sql="SELECT * FROM entries", transaction=tx)
    with caller():
        assert (await invoke(storage, "commit", transaction=tx))["committed"]
        assert (await invoke(storage, "begin", mutation_id="first"))["committed"]
        assert (await invoke(storage, "receipt", mutation_id="first"))["receipt"][
            "committed"
        ]
        tx = (await invoke(storage, "begin", mutation_id="second"))["transaction"]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('rollback')",
            transaction=tx,
        )
        with pytest.raises(PluginError):
            await invoke(
                storage, "execute", sql="INSERT INTO missing VALUES (1)", transaction=tx
            )
        with pytest.raises(PluginError, match="poisoned"):
            await invoke(storage, "commit", transaction=tx)
        await invoke(storage, "rollback", transaction=tx)
        assert await invoke(storage, "query", sql="SELECT label FROM entries") == [
            {"label": "retained"}
        ]


async def test_mutation_replay_and_root_cleanup(storage):
    with caller():
        params = {
            "sql": "INSERT INTO entries(label) VALUES (?) RETURNING id",
            "parameters": ["once"],
            "mutation_id": "once",
        }
        result = await invoke(storage, "execute_returning", **params)
        assert await invoke(storage, "execute_returning", **params) == result
        with pytest.raises(PluginError, match="different SQL"):
            await invoke(
                storage, "execute_returning", **{**params, "parameters": ["different"]}
            )
        tx = (await invoke(storage, "begin", mutation_id="lost"))["transaction"]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('lost')",
            transaction=tx,
        )
    with caller(admin=True):
        await storage.invoke(ADMIN_SERVICE, "cleanup_root", {"root_id": "root-1"})
    with caller(root="new"):
        assert await invoke(
            storage, "query", sql="SELECT count(*) AS n FROM entries"
        ) == [{"n": 1}]


async def test_expiry_releases_connection_and_write_lock(storage):
    with caller(deadline=time.time() + 0.15):
        tx = (await invoke(storage, "begin", mutation_id="expires"))["transaction"]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('lost')",
            transaction=tx,
        )
    await asyncio.sleep(0.3)
    with caller(root="new"):
        assert await invoke(storage, "query", sql="SELECT * FROM entries") == []
    assert not storage.transactions
    assert not storage.connections


async def test_migration_backup_and_history(storage):
    with caller():
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('backup')",
            mutation_id="seed",
        )
    change = Migration(
        revision="v2",
        parent="v1",
        sql="ALTER TABLE entries ADD COLUMN rank INTEGER NOT NULL DEFAULT 1",
        dialects={
            "postgresql": "ALTER TABLE entries ADD COLUMN rank INTEGER NOT NULL DEFAULT 1"
        },
    )
    upgrade = schema(change)
    with caller(admin=True):
        with pytest.raises(PluginError, match="explicit migration"):
            await storage.invoke(
                ADMIN_SERVICE, "ensure_store", {"owner": "alpha", "schema": upgrade}
            )
        plan = await storage.invoke(
            ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": upgrade}
        )
        with pytest.raises(PluginError, match="changed"):
            await storage.invoke(
                ADMIN_SERVICE,
                "apply",
                {"owner": "alpha", "schema": upgrade, "plan_id": "stale"},
            )
        result = await storage.invoke(
            ADMIN_SERVICE,
            "apply",
            {"owner": "alpha", "schema": upgrade, "plan_id": plan["plan_id"]},
        )
        assert result["committed"]
        from toolframe_sql_postgres.backup import verify_backup

        assert verify_backup(result["backup"])["application_id"] == "application-1"
        assert (await storage.invoke(ADMIN_SERVICE, "inspect", {"owner": "alpha"}))[
            "revision"
        ] == "v2"
        changed = schema(
            change.model_copy(
                update={
                    "dialects": {
                        "postgresql": "ALTER TABLE entries ADD COLUMN changed TEXT"
                    }
                }
            )
        )
        with pytest.raises(PluginError, match="history"):
            await storage.invoke(
                ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": changed}
            )
    with caller():
        assert await invoke(storage, "query", sql="SELECT rank FROM entries") == [
            {"rank": 1}
        ]


def test_setup_is_idempotent_and_refuses_unowned_database(database):
    env = database["env"]
    assert (
        provision(
            database["admin"],
            database["database"],
            "application-1",
            ["alpha", "beta"],
            previous=env,
        )
        == env
    )
    with pytest.raises(PluginError, match="existing database"):
        provision(database["admin"], database["database"], "unrelated", ["alpha"])


def test_environment_is_required_and_not_taken_from_plugin_config(monkeypatch):
    from toolframe_sql_postgres.config import Settings

    monkeypatch.delenv("TOOLFRAME_POSTGRES_DSN", raising=False)
    with pytest.raises(PluginError, match="runtime environment"):
        Settings.from_env()


async def test_database_backup_restores_pre_migration_data_and_grants(
    storage, database, monkeypatch, tmp_path
):
    from toolframe_sdk import RuntimeContext

    from toolframe_sql_postgres.cli import restore
    from toolframe_sql_postgres.plugin import PostgresProvider

    with caller():
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('snapshot')",
            mutation_id="snapshot",
        )
    change = Migration(
        revision="v2",
        parent="v1",
        sql="ALTER TABLE entries ADD COLUMN rank INTEGER",
        dialects={"postgresql": "ALTER TABLE entries ADD COLUMN rank INTEGER"},
    )
    with caller(admin=True):
        plan = await storage.invoke(
            ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": schema(change)}
        )
        result = await storage.invoke(
            ADMIN_SERVICE,
            "apply",
            {"owner": "alpha", "schema": schema(change), "plan_id": plan["plan_id"]},
        )
    target = "tf_restore_" + uuid.uuid4().hex
    try:
        environment = await restore(
            database["admin"],
            target,
            result["backup"],
            pg_restore=os.environ.get("TOOLFRAME_POSTGRES_PG_RESTORE", "pg_restore"),
        )
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        restored = PostgresProvider(
            RuntimeContext(
                None,
                plugin_id="sql-postgres",
                generation="restored",
                state_directory=tmp_path / "restored",
            )
        )
        await restored.activate()
        try:
            with caller():
                assert await invoke(restored, "query", sql="SELECT * FROM entries") == [
                    {"id": 1, "label": "snapshot"}
                ]
                await invoke(
                    restored,
                    "execute",
                    sql="INSERT INTO entries(label) VALUES ('new')",
                    mutation_id="new",
                )
            with caller("beta"):
                assert (
                    await invoke(restored, "query", sql="SELECT * FROM entries") == []
                )
            with caller(admin=True):
                assert (
                    await restored.invoke(ADMIN_SERVICE, "inspect", {"owner": "alpha"})
                )["revision"] == "v1"
        finally:
            await restored.dispose()
        with pytest.raises(PluginError, match="existing database"):
            await restore(
                database["admin"],
                target,
                result["backup"],
                pg_restore=os.environ.get(
                    "TOOLFRAME_POSTGRES_PG_RESTORE", "pg_restore"
                ),
            )
    finally:
        cleanup_database(database["admin"], target, ["alpha", "beta"])


async def test_backup_failure_does_not_change_schema(storage, monkeypatch):
    from dataclasses import replace

    storage.settings = replace(storage.settings, pg_dump="/missing/toolframe-pg-dump")
    change = Migration(
        revision="v2",
        parent="v1",
        sql="ALTER TABLE entries ADD COLUMN rank INTEGER",
        dialects={"postgresql": "ALTER TABLE entries ADD COLUMN rank INTEGER"},
    )
    with caller(admin=True):
        plan = await storage.invoke(
            ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": schema(change)}
        )
        with pytest.raises(PluginError, match="client"):
            await storage.invoke(
                ADMIN_SERVICE,
                "apply",
                {
                    "owner": "alpha",
                    "schema": schema(change),
                    "plan_id": plan["plan_id"],
                },
            )
        assert (await storage.invoke(ADMIN_SERVICE, "inspect", {"owner": "alpha"}))[
            "revision"
        ] == "v1"


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE other.secret",
        "ALTER TABLE entries OWNER TO postgres",
        "CREATE FUNCTION hack() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    ],
)
async def test_migration_rejects_cross_store_or_privilege_changes(storage, statement):
    change = Migration(
        revision="v2", parent="v1", sql="SELECT 1", dialects={"postgresql": statement}
    )
    with caller(admin=True), pytest.raises(PluginError):
        await storage.invoke(
            ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": schema(change)}
        )


async def test_cancelled_transaction_releases_its_connection_immediately(storage):
    ns = storage.settings.store_names("alpha")
    async with await psycopg.AsyncConnection.connect(
        storage.settings.store_dsn("alpha")
    ) as blocker:
        await blocker.execute(
            sql.SQL("LOCK TABLE {}.entries IN ACCESS EXCLUSIVE MODE").format(
                sql.Identifier(ns["schema"])
            )
        )
        with caller():
            token = (await invoke(storage, "begin", mutation_id="cancelled"))[
                "transaction"
            ]
            request = asyncio.create_task(
                invoke(storage, "query", sql="SELECT * FROM entries", transaction=token)
            )
            await asyncio.sleep(0.1)
            assert not request.done()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        assert not storage.transactions
        assert not storage.connections


async def test_unknown_commit_can_be_reconciled_by_mutation_id(storage, monkeypatch):
    with caller():
        token = (await invoke(storage, "begin", mutation_id="uncertain"))["transaction"]
        await invoke(
            storage,
            "execute",
            sql="INSERT INTO entries(label) VALUES ('committed')",
            transaction=token,
        )
        lost_connection = storage.transactions[token].connection
        original = psycopg.AsyncConnection.commit

        async def lost_reply(connection):
            await original(connection)
            if connection is lost_connection:
                raise psycopg.OperationalError("simulated acknowledgement loss")

        monkeypatch.setattr(psycopg.AsyncConnection, "commit", lost_reply)
        with pytest.raises(PluginError):
            await invoke(storage, "commit", transaction=token)
        assert not storage.transactions
        assert not storage.connections
        assert (await invoke(storage, "receipt", mutation_id="uncertain"))["receipt"][
            "committed"
        ]
        assert (await invoke(storage, "begin", mutation_id="uncertain"))["committed"]
        assert await invoke(
            storage, "query", sql="SELECT count(*) AS n FROM entries"
        ) == [{"n": 1}]


def test_failed_setup_removes_only_new_resources(database, monkeypatch):
    import importlib

    module = importlib.import_module("toolframe_sql_postgres.provision")
    original = module.role
    target = "tf_failed_" + uuid.uuid4().hex

    def failed_role(*args, **kwargs):
        original(*args, **kwargs)
        if args[1].endswith("_ctl"):
            raise RuntimeError("simulated setup interruption")

    monkeypatch.setattr(module, "role", failed_role)
    with pytest.raises(RuntimeError, match="interruption"):
        provision(database["admin"], target, "application-1", ["alpha"])
    with psycopg.connect(database["admin"]) as connection:
        assert not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (target,)
        ).fetchall()
        assert connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (database["database"],)
        ).fetchall()


async def test_backup_version_mismatch_prevents_migration(storage, monkeypatch):
    from toolframe_sql_postgres import backup

    async def wrong_version(program):
        return 1

    monkeypatch.setattr(backup, "client_major", wrong_version)
    change = Migration(
        revision="v2",
        parent="v1",
        sql="SELECT 1",
        dialects={"postgresql": "ALTER TABLE entries ADD COLUMN rank INTEGER"},
    )
    with caller(admin=True):
        plan = await storage.invoke(
            ADMIN_SERVICE, "plan", {"owner": "alpha", "schema": schema(change)}
        )
        with pytest.raises(PluginError, match="same major version"):
            await storage.invoke(
                ADMIN_SERVICE,
                "apply",
                {
                    "owner": "alpha",
                    "schema": schema(change),
                    "plan_id": plan["plan_id"],
                },
            )
        assert (await storage.invoke(ADMIN_SERVICE, "inspect", {"owner": "alpha"}))[
            "revision"
        ] == "v1"


async def test_health_reports_database_connectivity_without_credentials(storage):
    from dataclasses import replace

    from psycopg.conninfo import make_conninfo

    assert (await storage.health())["business"] == "ready"
    settings = storage.settings
    storage.settings = replace(
        settings, dsn=make_conninfo(settings.dsn, host="127.0.0.1", port="1")
    )
    try:
        with pytest.raises(PluginError) as failed:
            await storage.health()
        assert str(failed.value) == "Application database is unavailable"
    finally:
        storage.settings = settings
    assert (await storage.health())["business"] == "ready"


async def test_store_role_cannot_connect_to_another_application_database(
    storage, database
):
    from psycopg.conninfo import make_conninfo

    target = "tf_test_" + uuid.uuid4().hex
    try:
        provision(database["admin"], target, "application-2", ["alpha"])
        dsn = make_conninfo(storage.settings.store_dsn("alpha"), dbname=target)
        with pytest.raises(
            psycopg.OperationalError, match="permission denied for database"
        ):
            await psycopg.AsyncConnection.connect(dsn)
    finally:
        cleanup_database(
            database["admin"], target, ["alpha"], application="application-2"
        )
