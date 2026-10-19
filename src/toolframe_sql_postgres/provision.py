"""Explicit, administrator-run provisioning; never invoked by the runtime plugin."""

from __future__ import annotations

import json
import re
import secrets

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from toolframe_sdk import PluginError

from .config import names


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise PluginError(
            "database_name_invalid", "Use a lowercase PostgreSQL database identifier"
        )
    return value


def role(
    connection, name, *, password=None, existing=False, memberships=(), created=None
):
    found = connection.execute(
        "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin,rolinherit FROM pg_roles WHERE rolname=%s",
        (name,),
    ).fetchone()
    if found:
        if not existing or any(found[:5]) or found[5] != bool(password) or found[6]:
            raise PluginError(
                "role_conflict",
                "Existing role cannot be adopted; use matching setup credentials",
            )
        parents = connection.execute(
            "SELECT parent.rolname FROM pg_auth_members m JOIN pg_roles member ON member.oid=m.member JOIN pg_roles parent ON parent.oid=m.roleid WHERE member.rolname=%s",
            (name,),
        ).fetchall()
        if {row[0] for row in parents} != set(memberships):
            raise PluginError(
                "role_conflict",
                "Existing role membership differs from the provisioning contract",
            )
        return
    attributes = (
        sql.SQL("LOGIN PASSWORD {} NOINHERIT").format(sql.Literal(password))
        if password
        else sql.SQL("NOLOGIN NOINHERIT")
    )
    connection.execute(
        sql.SQL(
            "CREATE ROLE {} {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        ).format(sql.Identifier(name), attributes)
    )

    if created is not None:
        created.append(("role", name))


def provision(admin_dsn, database, application_id, owners, *, previous=None):
    created = []
    try:
        return _provision(
            admin_dsn,
            database,
            application_id,
            owners,
            previous=previous,
            created=created,
        )
    except BaseException:
        if not created:
            raise
        # Roll back only resources created by this invocation. Existing data is
        # never adopted or dropped as a response to a failed setup attempt.
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                for kind, name in reversed(created):
                    if kind == "database":
                        admin.execute(
                            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                                sql.Identifier(name)
                            )
                        )
                for kind, name in reversed(created):
                    if kind == "role":
                        if previous:
                            admin.execute(
                                sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                                    sql.Identifier(database), sql.Identifier(name)
                                )
                            )
                        admin.execute(
                            sql.SQL("DROP ROLE {}").format(sql.Identifier(name))
                        )
        except psycopg.Error:
            raise PluginError(
                "setup_cleanup_failed",
                "Setup failed and its new resources need administrator inspection before retrying",
            ) from None
        raise


def _provision(admin_dsn, database, application_id, owners, *, previous=None, created):
    """Provision fresh resources or add owners using a previous setup result.

    Returns environment values. Callers must save them privately, never print them.
    Existing databases/roles are only reused when the stored application identity
    and the previous credentials match. Other application resources are untouched.
    """
    identifier(database)
    if not application_id or len(application_id) > 256:
        raise PluginError("application_id_invalid", "Application identity is required")
    owners = sorted(set(owners))
    if not owners or any(
        not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", owner) for owner in owners
    ):
        raise PluginError("owner_invalid", "Supply stable plugin IDs to provision")
    manager = names(application_id, database=database)["manager"]
    stored = json.loads(previous["TOOLFRAME_POSTGRES_STORES"]) if previous else {}
    manager_password = secrets.token_hex(32)
    manager_dsn = make_conninfo(
        admin_dsn, dbname=database, user=manager, password=manager_password
    )
    if previous:
        if previous["TOOLFRAME_POSTGRES_APPLICATION_ID"] != application_id:
            raise PluginError(
                "identity_mismatch", "Setup output belongs to a different application"
            )
        manager_dsn = previous["TOOLFRAME_POSTGRES_DSN"]
        parsed = conninfo_to_dict(manager_dsn)
        if parsed.get("dbname") != database or parsed.get("user") != manager:
            raise PluginError(
                "identity_mismatch", "Setup output belongs to a different database"
            )
        manager_dsn = make_conninfo(
            admin_dsn, dbname=database, user=manager, password=parsed["password"]
        )
        # Prove that the retained credentials work before touching any existing resource.
        with psycopg.connect(manager_dsn) as check:
            identity = check.execute(
                "SELECT application_id FROM _toolframe_platform.identity"
            ).fetchall()
            if identity != [(application_id,)]:
                raise PluginError(
                    "identity_mismatch", "Database application identity differs"
                )
        for owner, credentials in stored.items():
            ns = names(application_id, owner, database=database)
            if credentials.get("user") != ns["controller"]:
                raise PluginError(
                    "identity_mismatch",
                    "Retained store credentials belong to another owner",
                )
            with psycopg.connect(make_conninfo(manager_dsn, **credentials)) as check:
                identity = check.execute(
                    sql.SQL("SELECT application_id,owner FROM {}.identity").format(
                        sql.Identifier(ns["metadata"])
                    )
                ).fetchall()
                if identity != [(application_id, owner)]:
                    raise PluginError("identity_mismatch", "Store identity differs")
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        if admin.info.server_version < 160000:
            raise PluginError(
                "postgres_version_unsupported",
                "Provisioning requires PostgreSQL 16 or newer",
            )
        found = admin.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s",
            (database,),
        ).fetchone()
        if found and (not previous or found[0] != manager):
            raise PluginError(
                "database_conflict", "Refusing to adopt an existing database"
            )
        role(
            admin,
            manager,
            password=manager_password,
            existing=bool(previous),
            created=created,
        )
        if not found:
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(manager)
                )
            )
            created.append(("database", database))
        admin.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(database)
            )
        )
        for owner in owners:
            ns = names(application_id, owner, database=database)
            creds = stored.get(owner) or {
                "user": ns["controller"],
                "password": secrets.token_hex(32),
            }
            role(
                admin,
                ns["controller"],
                password=creds["password"],
                existing=owner in stored,
                memberships=(ns["runtime"],),
                created=created,
            )
            role(admin, ns["runtime"], existing=owner in stored, created=created)
            admin.execute(
                sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(
                    sql.Identifier(ns["runtime"]), sql.Identifier(ns["controller"])
                )
            )
            admin.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(ns["controller"])
                )
            )
            stored[owner] = creds
    with psycopg.connect(make_conninfo(admin_dsn, dbname=database)) as db:
        db.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        db.execute(
            sql.SQL(
                "CREATE SCHEMA IF NOT EXISTS _toolframe_platform AUTHORIZATION {}"
            ).format(sql.Identifier(manager))
        )
        db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(manager)))
        db.execute(
            "CREATE TABLE IF NOT EXISTS _toolframe_platform.identity(application_id TEXT PRIMARY KEY)"
        )
        current = db.execute(
            "SELECT application_id FROM _toolframe_platform.identity"
        ).fetchall()
        if current and current != [(application_id,)]:
            raise PluginError(
                "identity_mismatch", "Database application identity differs"
            )
        db.execute(
            "INSERT INTO _toolframe_platform.identity VALUES (%s) ON CONFLICT DO NOTHING",
            (application_id,),
        )
        db.execute("RESET ROLE")
        for owner in owners:
            ns = names(application_id, owner, database=database)
            for key in ("schema", "metadata"):
                db.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
                        sql.Identifier(ns[key]), sql.Identifier(ns["controller"])
                    )
                )
                db.execute(
                    sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(
                        sql.Identifier(ns[key])
                    )
                )
                db.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(ns[key]), sql.Identifier(manager)
                    )
                )
            db.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(ns["schema"]), sql.Identifier(ns["runtime"])
                )
            )
            db.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(ns["controller"]))
            )
            for who, permissions in (
                (ns["runtime"], "SELECT, INSERT, UPDATE, DELETE"),
                (manager, "SELECT"),
            ):
                db.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT {} ON TABLES TO {}"
                    ).format(
                        sql.Identifier(ns["schema"]),
                        sql.SQL(permissions),
                        sql.Identifier(who),
                    )
                )
                db.execute(
                    sql.SQL("GRANT {} ON ALL TABLES IN SCHEMA {} TO {}").format(
                        sql.SQL(permissions),
                        sql.Identifier(ns["schema"]),
                        sql.Identifier(who),
                    )
                )
                db.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}"
                    ).format(sql.Identifier(ns["schema"]), sql.Identifier(who))
                )
                db.execute(
                    sql.SQL(
                        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}"
                    ).format(sql.Identifier(ns["schema"]), sql.Identifier(who))
                )
            db.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
                ).format(sql.Identifier(ns["metadata"]), sql.Identifier(manager))
            )
            db.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {}.identity(application_id TEXT NOT NULL, owner TEXT NOT NULL)"
                ).format(sql.Identifier(ns["metadata"]))
            )
            identity = db.execute(
                sql.SQL("SELECT application_id,owner FROM {}.identity").format(
                    sql.Identifier(ns["metadata"])
                )
            ).fetchall()
            if identity and identity != [(application_id, owner)]:
                raise PluginError("identity_mismatch", "Store identity differs")
            if not identity:
                db.execute(
                    sql.SQL("INSERT INTO {}.identity VALUES(%s,%s)").format(
                        sql.Identifier(ns["metadata"])
                    ),
                    (application_id, owner),
                )
            db.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {}.migrations(position INTEGER PRIMARY KEY, revision TEXT UNIQUE NOT NULL, checksum TEXT NOT NULL)"
                ).format(sql.Identifier(ns["metadata"]))
            )
            db.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {}.receipts(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)"
                ).format(sql.Identifier(ns["metadata"]))
            )
            db.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                    sql.Identifier(ns["metadata"]), sql.Identifier(manager)
                )
            )
            db.execute("RESET ROLE")
    return {
        "TOOLFRAME_POSTGRES_APPLICATION_ID": application_id,
        "TOOLFRAME_POSTGRES_DSN": manager_dsn,
        "TOOLFRAME_POSTGRES_STORES": json.dumps(stored, separators=(",", ":")),
    }
