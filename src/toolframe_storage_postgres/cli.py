"""Provisioning and restore utilities, separate from the runtime entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from toolframe_sdk import PluginError

from .backup import require_matching_client, run_tool, verify_backup
from .config import names
from .provision import provision


def read_environment(path):
    path = Path(path)
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise PluginError(
            "credentials_permissions",
            "Environment file must be a private regular file (0600)",
        )
    values = json.loads(path.read_text())
    if not isinstance(values, dict) or any(
        not isinstance(k, str)
        or not isinstance(v, str)
        or not k.startswith("TOOLFRAME_POSTGRES_")
        for k, v in values.items()
    ):
        raise PluginError(
            "credentials_invalid", "Invalid PostgreSQL environment document"
        )
    if (
        "TOOLFRAME_POSTGRES_SETUP_DSN" in values
        or "TOOLFRAME_POSTGRES_TEST_ADMIN_DSN" in values
    ):
        raise PluginError(
            "credentials_invalid",
            "Provisioning credentials must not be stored in runtime output",
        )
    return values


def save_environment(path, values):
    path = Path(path)
    if path.is_symlink():
        raise PluginError(
            "credentials_invalid", "Environment output cannot be a symbolic link"
        )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(values, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def restore(admin_dsn, database, backup, *, pg_restore="pg_restore"):
    """Restore only into a newly provisioned database. Failure leaves it for diagnosis."""
    metadata = verify_backup(backup)
    await require_matching_client(pg_restore, admin_dsn, metadata["postgres_major"])
    application = metadata["application_id"]
    environment = provision(admin_dsn, database, application, metadata["owners"])
    target = make_conninfo(admin_dsn, dbname=database)
    await run_tool(
        pg_restore,
        [
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            str(Path(backup).resolve()),
        ],
        target,
        timeout=300,
    )
    manager = names(application, database=database)["manager"]
    with psycopg.connect(target) as connection:
        schemas = {"_toolframe_platform": manager}
        for owner in metadata["owners"]:
            ns = names(application, owner, database=database)
            schemas.update(
                {ns["schema"]: ns["controller"], ns["metadata"]: ns["controller"]}
            )
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname NOT IN ('information_schema','public')"
            ).fetchall()
        }
        if actual != set(schemas):
            raise PluginError(
                "backup_inventory_mismatch",
                "Restored schema inventory differs from the backup owners; target remains isolated for inspection",
            )
        for schema, owner_role in schemas.items():
            connection.execute(
                sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                    sql.Identifier(schema), sql.Identifier(owner_role)
                )
            )
            tables = connection.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN ('r','p')",
                (schema,),
            ).fetchall()
            for (table,) in tables:
                connection.execute(
                    sql.SQL("ALTER TABLE {}.{} OWNER TO {}").format(
                        sql.Identifier(schema),
                        sql.Identifier(table),
                        sql.Identifier(owner_role),
                    )
                )
    # Reapply grants and defaults to both restored and subsequently created objects.
    provision(
        admin_dsn, database, application, metadata["owners"], previous=environment
    )
    return environment


def parser():
    result = argparse.ArgumentParser(
        description="Provision and restore Toolframe PostgreSQL stores"
    )
    commands = result.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--application-id", required=True)
    setup.add_argument("--database", required=True)
    setup.add_argument("--owner", action="append", required=True)
    setup.add_argument("--output", required=True, type=Path)
    recovery = commands.add_parser("restore")
    recovery.add_argument("--database", required=True)
    recovery.add_argument("--backup", required=True, type=Path)
    recovery.add_argument("--output", required=True, type=Path)
    run = commands.add_parser("run")
    run.add_argument("--env-file", required=True, type=Path)
    run.add_argument("argv", nargs=argparse.REMAINDER)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            values = read_environment(args.env_file)
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            if not command:
                raise PluginError("command_missing", "Supply a command after --")
            environment = dict(os.environ)
            for key in (
                "TOOLFRAME_POSTGRES_SETUP_DSN",
                "TOOLFRAME_POSTGRES_TEST_ADMIN_DSN",
            ):
                environment.pop(key, None)
            environment.update(values)
            os.execvpe(command[0], command, environment)
        admin = os.environ.get("TOOLFRAME_POSTGRES_SETUP_DSN")
        if not admin:
            raise PluginError(
                "setup_credentials_missing",
                "Set TOOLFRAME_POSTGRES_SETUP_DSN for provisioning",
            )
        if args.command == "setup":
            previous = read_environment(args.output) if args.output.exists() else None
            values = provision(
                admin, args.database, args.application_id, args.owner, previous=previous
            )
        else:
            if args.output.exists():
                raise PluginError(
                    "output_exists",
                    "Restore output must be a new private environment file",
                )
            values = asyncio.run(
                restore(
                    admin,
                    args.database,
                    args.backup,
                    pg_restore=os.environ.get(
                        "TOOLFRAME_POSTGRES_PG_RESTORE", "pg_restore"
                    ),
                )
            )
        save_environment(args.output, values)
        print(
            json.dumps(
                {
                    "database": args.database,
                    "environment_file": str(args.output),
                    "credentials_printed": False,
                }
            )
        )
    except PluginError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except psycopg.Error, ValueError, OSError:
        print(
            "PostgreSQL setup/restore failed; inspect connectivity, client tools and resource permissions. No credentials were printed.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
