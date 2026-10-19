"""Managed PostgreSQL operations, isolated by application and stable plugin owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    Plugin,
    PluginDefinition,
    PluginError,
    ServiceSpec,
    StoreSchema,
    digest,
)

from .backup import create_backup
from .config import Settings
from .sql import migration_statements, prepare

SQL_SERVICE = "toolframe.sql"
ADMIN_SERVICE = "toolframe.sql.admin"


def encoded(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


@dataclass
class Transaction:
    connection: psycopg.AsyncConnection
    owner: str
    generation: str
    root_id: str
    deadline: float
    mutation_id: str
    poisoned: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PostgresProvider(Plugin):
    def __init__(self, context):
        super().__init__(context)
        self.settings = None
        self.transactions = {}
        self.sweeper = None
        self.administration = asyncio.Lock()
        self.connections = set()

    async def activate(self):
        self.settings = Settings.from_env()
        if not self.context.state_directory:
            raise PluginError(
                "storage_directory_missing",
                "Core must allocate a private backup directory",
            )
        self.directory = Path(self.context.state_directory) / "postgres"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.capacity = asyncio.Semaphore(self.settings.connection_limit)
        try:
            async with await psycopg.AsyncConnection.connect(
                self.settings.dsn, row_factory=dict_row, connect_timeout=5
            ) as connection:
                identity = await (
                    await connection.execute(
                        "SELECT application_id FROM _toolframe_platform.identity"
                    )
                ).fetchall()
                if identity != [{"application_id": self.settings.application_id}]:
                    raise PluginError(
                        "store_identity_mismatch",
                        "Database belongs to a different application",
                    )
        except psycopg.Error:
            raise PluginError(
                "postgres_unavailable",
                "Cannot access the provisioned application database",
            ) from None
        self.sweeper = asyncio.create_task(self._expire())

    async def _expire(self):
        while True:
            await asyncio.sleep(0.1)
            for token, transaction in list(self.transactions.items()):
                if (
                    transaction.deadline <= time.time()
                    and not transaction.lock.locked()
                ):
                    await self.rollback(token)

    async def health(self):
        try:
            async with asyncio.timeout(5):
                async with await psycopg.AsyncConnection.connect(
                    self.settings.dsn, connect_timeout=5
                ) as connection:
                    await connection.execute("SELECT 1")
            return {"runtime": "ready", "business": "ready"}
        except psycopg.Error, TimeoutError:
            raise PluginError(
                "postgres_unavailable", "Application database is unavailable"
            ) from None

    async def dispose(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        for token in list(self.transactions):
            await self.rollback(token)
        for connection in list(self.connections):
            await self.release(connection)
        await super().dispose()

    def identity(self, *, admin=False, owner=None):
        info = self.context.call_info
        if admin:
            if not info.get("management"):
                raise PluginError("forbidden", "Storage administration is host-only")
        else:
            owner = info.get("owner")
            if not info.get("owner_generation") or not info.get("root_id"):
                raise PluginError(
                    "identity_missing",
                    "Storage requires a host-issued root and owner generation",
                )
        if info.get("deadline", 0) <= time.time():
            raise PluginError("deadline_exceeded", "Storage root has expired")
        if not owner or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", owner):
            raise PluginError(
                "invalid_owner", "Storage owner must be a stable plugin ID"
            )
        if info.get("application_id") != self.settings.application_id:
            raise PluginError(
                "store_identity_mismatch", "Database belongs to a different application"
            )
        self.settings.store_dsn(owner)
        return owner, info

    async def connect(self, owner, deadline):
        acquired = False
        connection = None
        try:
            async with asyncio.timeout(max(0.01, deadline - time.time())):
                await self.capacity.acquire()
                acquired = True
                connection = await psycopg.AsyncConnection.connect(
                    self.settings.store_dsn(owner),
                    row_factory=dict_row,
                    connect_timeout=5,
                )
                self.connections.add(connection)
                ns = self.settings.store_names(owner)
                await connection.execute(
                    sql.SQL("SET search_path TO pg_catalog, {}").format(
                        sql.Identifier(ns["schema"])
                    )
                )
                await self.timeout(connection, deadline)
                identity = await (
                    await connection.execute(
                        sql.SQL("SELECT application_id,owner FROM {}.identity").format(
                            sql.Identifier(ns["metadata"])
                        )
                    )
                ).fetchall()
                if identity != [
                    {"application_id": self.settings.application_id, "owner": owner}
                ]:
                    raise PluginError(
                        "store_identity_mismatch",
                        "Store belongs to another application or owner",
                    )
                await connection.commit()
                return connection
        except BaseException:
            if connection:
                await self.release(connection)
            elif acquired:
                self.capacity.release()
            raise

    async def timeout(self, connection, deadline):
        duration = max(1, int((deadline - time.time()) * 1000))
        await connection.execute(
            "SELECT set_config('statement_timeout', %s, false), set_config('lock_timeout', %s, false)",
            (str(duration), str(duration)),
        )

    async def release(self, connection):
        if connection in self.connections:
            self.connections.remove(connection)
            try:
                await connection.close()
            finally:
                self.capacity.release()

    async def lock_owner(self, connection, owner):
        key = int.from_bytes(
            hashlib.sha256(
                (self.settings.application_id + "\0" + owner).encode()
            ).digest()[:8],
            "big",
            signed=True,
        )
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (key,))

    async def inspect(self, connection, owner):
        ns = self.settings.store_names(owner)
        history = await (
            await connection.execute(
                sql.SQL("SELECT * FROM {}.migrations ORDER BY position").format(
                    sql.Identifier(ns["metadata"])
                )
            )
        ).fetchall()
        return {
            "exists": bool(history),
            "history": history,
            "revision": history[-1]["revision"] if history else None,
        }

    async def plan(self, connection, owner, schema):
        try:
            selected = [
                item.for_dialect("postgresql")
                for item in StoreSchema.model_validate(schema).migrations
            ]
        except ValueError:
            raise PluginError(
                "schema_dialect_unsupported",
                "Every migration must declare PostgreSQL SQL",
            ) from None
        history = (await self.inspect(connection, owner))["history"]
        for index, row in enumerate(history):
            if index >= len(selected) or (row["revision"], row["checksum"]) != (
                selected[index].revision,
                selected[index].checksum,
            ):
                raise PluginError(
                    "schema_incompatible",
                    "Migration history was changed, removed or downgraded",
                )
        pending = [item.model_dump() for item in selected[len(history) :]]
        for item in pending:
            list(migration_statements(item["sql"]))
        value = {
            "owner": owner,
            "current": history[-1]["revision"] if history else None,
            "target": selected[-1].revision if selected else None,
            "pending": pending,
            "history": history,
            "requires_apply": bool(history and pending),
        }
        value["plan_id"] = digest(value)
        return value

    async def admin(self, method, params):
        if method == "cleanup_root":
            if not self.context.call_info.get("management"):
                raise PluginError("forbidden", "Root cleanup is host-only")
            if (
                self.context.call_info.get("application_id")
                != self.settings.application_id
            ):
                raise PluginError(
                    "store_identity_mismatch",
                    "Database belongs to a different application",
                )
            for token, transaction in list(self.transactions.items()):
                if transaction.root_id == params["root_id"]:
                    async with transaction.lock:
                        await self.rollback(token)
            return {}
        owner, info = self.identity(admin=True, owner=params.get("owner"))
        async with self.administration:
            connection = await self.connect(owner, info["deadline"])
            try:
                if method == "inspect":
                    return await self.inspect(connection, owner)
                if method == "plan":
                    return await self.plan(connection, owner, params["schema"])
                if method not in {"ensure_store", "apply"}:
                    raise PluginError("method_not_found", method)
                if any(tx.owner == owner for tx in self.transactions.values()):
                    raise PluginError(
                        "store_busy", "Drain all owner transactions before migration"
                    )
                await self.lock_owner(connection, owner)
                plan = await self.plan(connection, owner, params["schema"])
                if method == "apply" and params.get("plan_id") != plan["plan_id"]:
                    raise PluginError(
                        "stale_plan", "Store schema changed after migration planning"
                    )
                if method == "ensure_store" and plan["requires_apply"]:
                    raise PluginError(
                        "migration_required",
                        "Existing data requires explicit migration apply",
                        plan,
                    )
                if not plan["pending"]:
                    return {"revision": plan["target"], "committed": False}
                backup = None
                if plan["history"]:
                    backup = await create_backup(
                        self.settings, self.directory, plan, info["deadline"]
                    )
                ns = self.settings.store_names(owner)
                await connection.execute(
                    sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                        sql.Identifier(ns["schema"])
                    )
                )
                for position, item in enumerate(plan["pending"], len(plan["history"])):
                    for statement in migration_statements(item["sql"]):
                        await connection.execute(statement)
                    checksum = hashlib.sha256(item["sql"].encode()).hexdigest()
                    await connection.execute(
                        sql.SQL("INSERT INTO {}.migrations VALUES(%s,%s,%s)").format(
                            sql.Identifier(ns["metadata"])
                        ),
                        (position, item["revision"], checksum),
                    )
                await connection.commit()
                return {
                    "revision": plan["target"],
                    "committed": True,
                    "backup": str(backup) if backup else None,
                }
            finally:
                await self.release(connection)

    def mutation_id(self, params):
        value = params.get("mutation_id")
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-zA-Z0-9_.:-]{1,128}", value
        ):
            raise PluginError(
                "mutation_id_invalid", "A bounded stable mutation ID is required"
            )
        return value

    async def receipt(self, connection, owner, identifier, fingerprint=None):
        ns = self.settings.store_names(owner)
        row = await (
            await connection.execute(
                sql.SQL(
                    "SELECT fingerprint,result FROM {}.receipts WHERE id=%s"
                ).format(sql.Identifier(ns["metadata"])),
                (identifier,),
            )
        ).fetchone()
        if row and fingerprint is not None and row["fingerprint"] != fingerprint:
            raise PluginError(
                "mutation_conflict",
                "Mutation ID already refers to different SQL/parameters",
            )
        return json.loads(row["result"]) if row else None

    async def save_receipt(self, connection, owner, identifier, fingerprint, result):
        ns = self.settings.store_names(owner)
        await connection.execute(
            sql.SQL("INSERT INTO {}.receipts VALUES(%s,%s,%s)").format(
                sql.Identifier(ns["metadata"])
            ),
            (identifier, fingerprint, encoded(result)),
        )

    async def rollback(self, token):
        transaction = self.transactions.pop(token, None)
        if transaction:
            await self.release(transaction.connection)

    def transaction(self, token, owner, info):
        transaction = self.transactions.get(token)
        if not transaction or (
            transaction.owner,
            transaction.generation,
            transaction.root_id,
        ) != (owner, info["owner_generation"], info["root_id"]):
            raise PluginError(
                "transaction_invalid",
                "Transaction is not owned by this root and generation",
            )
        if transaction.deadline <= time.time():
            raise PluginError("transaction_expired", "Transaction deadline exceeded")
        return transaction

    async def business(self, method, params):
        owner, info = self.identity()
        token = params.get("transaction")
        transaction = self.transaction(token, owner, info) if token else None
        if transaction:
            async with transaction.lock:
                if self.transactions.get(token) is not transaction:
                    raise PluginError(
                        "transaction_invalid", "Transaction was already settled"
                    )
                return await self.operation(owner, info, method, params, transaction)
        return await self.operation(owner, info, method, params, None)

    async def operation(self, owner, info, method, params, transaction):
        if transaction and transaction.poisoned and method != "rollback":
            raise PluginError(
                "transaction_poisoned",
                "A failed statement poisoned this transaction; roll it back",
            )
        connection = (
            transaction.connection
            if transaction
            else await self.connect(owner, info["deadline"])
        )
        keep = transaction is not None
        try:
            if method == "rollback":
                if not transaction:
                    raise PluginError(
                        "transaction_required", "Rollback needs a transaction"
                    )
                await self.rollback(params["transaction"])
                return {"rolled_back": True}
            await self.timeout(connection, info["deadline"])
            if method == "receipt":
                return {
                    "receipt": await self.receipt(
                        connection, owner, self.mutation_id(params)
                    )
                }
            if method == "begin":
                if transaction:
                    raise PluginError(
                        "nested_transaction", "Nested transactions are unsupported"
                    )
                identifier = self.mutation_id(params)
                await self.lock_owner(connection, owner)
                prior = await self.receipt(connection, owner, identifier, "transaction")
                if prior is not None:
                    return prior
                token = uuid.uuid4().hex
                self.transactions[token] = Transaction(
                    connection,
                    owner,
                    info["owner_generation"],
                    info["root_id"],
                    info["deadline"],
                    identifier,
                )
                keep = True
                return {"transaction": token, "mutation_id": identifier}
            if method == "commit":
                if not transaction:
                    raise PluginError(
                        "transaction_required", "Commit needs a transaction"
                    )
                result = {"committed": True, "mutation_id": transaction.mutation_id}
                await self.save_receipt(
                    connection, owner, transaction.mutation_id, "transaction", result
                )
                await connection.commit()
                self.transactions.pop(params["transaction"])
                await self.release(connection)
                return result
            if method not in {"query", "execute", "execute_returning"}:
                raise PluginError("method_not_found", method)
            query, values = prepare(params["sql"], params.get("parameters", []), method)
            fingerprint = digest(
                {
                    "sql": params["sql"],
                    "parameters": params.get("parameters", []),
                    "method": method,
                }
            )
            identifier = None
            if method != "query" and not transaction:
                identifier = self.mutation_id(params)
                await self.lock_owner(connection, owner)
                prior = await self.receipt(connection, owner, identifier, fingerprint)
                if prior is not None:
                    return prior
            ns = self.settings.store_names(owner)
            await connection.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(ns["runtime"]))
            )
            async with psycopg.AsyncRawCursor(
                connection, row_factory=dict_row
            ) as cursor:
                await cursor.execute(query, values)
                if method in {"query", "execute_returning"}:
                    rows = await cursor.fetchmany(10001)
                    if len(rows) > 10000:
                        raise PluginError(
                            "result_too_large",
                            "Use LIMIT/pagination; query exceeds 10,000 rows",
                        )
                    result = (
                        rows
                        if method == "query"
                        else {"rows": rows, "rowcount": cursor.rowcount}
                    )
                else:
                    result = {"rowcount": cursor.rowcount, "lastrowid": None}
            await connection.execute("RESET ROLE")
            if len(encoded(result).encode()) > 900_000:
                raise PluginError(
                    "result_too_large", "Query exceeds the RPC result budget"
                )
            if identifier:
                await self.save_receipt(
                    connection, owner, identifier, fingerprint, result
                )
                await connection.commit()
            return result
        except BaseException:
            if transaction:
                transaction.poisoned = True
                # A cancelled RPC must not retain a connection/owner lock until
                # the root deadline. A commit error can have an unknown outcome:
                # discard its connection and reconcile by durable mutation ID.
                if method == "commit" or asyncio.current_task().cancelling():
                    await self.rollback(params["transaction"])
            raise
        finally:
            if not keep:
                await self.release(connection)

    async def invoke(self, service, method, params):
        try:
            deadline = self.context.call_info.get("deadline")
            # Cleanup remains callable after the root expires.
            timeout = (
                None
                if method == "cleanup_root" or deadline is None
                else max(0, deadline - time.time())
            )
            async with asyncio.timeout(timeout):
                if service == ADMIN_SERVICE:
                    return await self.admin(method, params)
                if service == SQL_SERVICE:
                    return await self.business(method, params)
                raise PluginError("method_not_found", method)
        except psycopg.Error, TimeoutError, ValueError, TypeError:
            # Driver diagnostics can contain credentials or complete row values.
            raise PluginError(
                "sql_failed",
                "PostgreSQL operation failed or exceeded its deadline; inspect database availability and schema",
            ) from None


plugin = PluginDefinition(
    id="sql-postgres",
    runtime_validation="separate_application",
    kind="provider",
    provider_kind="sql",
    factory="toolframe_sql_postgres.plugin:PostgresProvider",
    authorization=AuthorizationSpec(),
    services={
        SQL_SERVICE: ServiceSpec(
            methods={
                name: MethodSpec(
                    audience="internal", read_only=name in {"query", "receipt"}
                )
                for name in (
                    "query",
                    "execute",
                    "execute_returning",
                    "begin",
                    "commit",
                    "rollback",
                    "receipt",
                )
            }
        ),
        ADMIN_SERVICE: ServiceSpec(
            methods={
                name: MethodSpec(audience="internal")
                for name in ("inspect", "ensure_store", "plan", "apply", "cleanup_root")
            }
        ),
    },
)
