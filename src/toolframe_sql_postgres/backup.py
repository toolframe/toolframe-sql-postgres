"""Private database dumps, with bounded subprocesses and no credentials in argv."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from toolframe_sdk import PluginError


def command_environment(dsn):
    values = conninfo_to_dict(dsn)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PG") and not key.startswith("TOOLFRAME_POSTGRES")
    }
    password = values.pop("password", None)
    if password is not None:
        environment["PGPASSWORD"] = password
    return make_conninfo(**values), environment


async def run_tool(program, arguments, dsn, *, timeout):
    public_dsn, environment = command_environment(dsn)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            program,
            *arguments,
            "--no-password",
            "--dbname",
            public_dsn,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(max(0.01, timeout)):
            output, _ = await process.communicate()
        if process.returncode:
            raise PluginError(
                "postgres_tool_failed",
                "PostgreSQL client failed; check client version and database access",
            )
        return output
    except OSError, TimeoutError:
        raise PluginError(
            "postgres_tool_failed",
            "PostgreSQL client is unavailable or exceeded its deadline",
        ) from None
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()


async def client_major(program):
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            program,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(5):
            output, _ = await process.communicate()
        match = re.search(rb"\(PostgreSQL\) (\d+)", output)
        if process.returncode or not match:
            raise ValueError
        return int(match[1])
    except OSError, TimeoutError, ValueError:
        raise PluginError(
            "postgres_tool_failed", "PostgreSQL client version is unavailable"
        ) from None
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()


async def require_matching_client(program, dsn, expected=None):
    async with await psycopg.AsyncConnection.connect(
        dsn, connect_timeout=5
    ) as connection:
        major = connection.info.server_version // 10000
    if await client_major(program) != major or (
        expected is not None and expected != major
    ):
        raise PluginError(
            "postgres_version_mismatch",
            "Use PostgreSQL client tools and restore target with the same major version as the source database",
        )
    return major


async def create_backup(settings, directory, plan, deadline):
    major = await require_matching_client(settings.pg_dump, settings.dsn)
    directory = Path(directory) / "backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifier = f"{int(time.time())}-{uuid.uuid4().hex}"
    path = directory / f"{identifier}.dump"
    temporary = directory / f".{identifier}.partial"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        await run_tool(
            settings.pg_dump,
            ["--format=custom", "--file", str(temporary)],
            settings.dsn,
            timeout=deadline - time.time(),
        )
        with temporary.open("rb") as source:
            checksum = hashlib.file_digest(source, "sha256").hexdigest()
        metadata = {
            "format": 1,
            "application_id": settings.application_id,
            "owners": sorted(settings.stores),
            "sha256": checksum,
            "plan": plan,
            "created": time.time(),
            "core_state_included": False,
            "postgres_major": major,
        }
        temporary.replace(path)
        manifest = path.with_suffix(".json")
        descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(metadata, stream, sort_keys=True)
        return path
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def verify_backup(path):
    path = Path(path)
    metadata = json.loads(path.with_suffix(".json").read_text())
    with path.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    if (
        metadata.get("format") != 1
        or checksum != metadata.get("sha256")
        or not isinstance(metadata.get("application_id"), str)
        or not isinstance(metadata.get("postgres_major"), int)
        or not isinstance(metadata.get("owners"), list)
        or not metadata["owners"]
        or any(
            not isinstance(owner, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", owner)
            for owner in metadata["owners"]
        )
    ):
        raise PluginError("backup_invalid", "Backup checksum or format does not match")
    return metadata
