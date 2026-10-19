"""Runtime credentials come exclusively from the process environment."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from toolframe_sdk import PluginError


def names(
    application_id: str, owner: str | None = None, *, database: str
) -> dict[str, str]:
    application = hashlib.sha256(
        (application_id + "\0" + database).encode()
    ).hexdigest()[:20]
    if owner is None:
        return {"manager": f"tf_{application}_manager"}
    suffix = hashlib.sha256(owner.encode()).hexdigest()[:20]
    return {
        "schema": f"tf_{suffix}",
        "metadata": f"tf_{suffix}_meta",
        "controller": f"tf_{application}_{suffix}_ctl",
        "runtime": f"tf_{application}_{suffix}_run",
    }


@dataclass(frozen=True, repr=False)
class Settings:
    application_id: str
    dsn: str
    stores: dict[str, dict[str, str]]
    pg_dump: str
    pg_restore: str
    connection_limit: int

    @classmethod
    def from_env(cls):
        try:
            application = os.environ["TOOLFRAME_POSTGRES_APPLICATION_ID"]
            dsn = os.environ["TOOLFRAME_POSTGRES_DSN"]
            stores = json.loads(os.environ["TOOLFRAME_POSTGRES_STORES"])
            connection = conninfo_to_dict(dsn)
            database = connection["dbname"]
            if (
                connection.get("user")
                != names(application, database=database)["manager"]
            ):
                raise ValueError("Use the application-scoped manager from setup")
            if (
                not application
                or len(application) > 256
                or not isinstance(stores, dict)
            ):
                raise ValueError
            for owner, credentials in stores.items():
                if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", owner):
                    raise ValueError
                if set(credentials) != {"user", "password"}:
                    raise ValueError
                if (
                    credentials["user"]
                    != names(application, owner, database=database)["controller"]
                ):
                    raise ValueError
                if (
                    not isinstance(credentials["password"], str)
                    or not credentials["password"]
                ):
                    raise ValueError
            limit = int(os.environ.get("TOOLFRAME_POSTGRES_CONNECTION_LIMIT", "16"))
            if not 1 <= limit <= 128:
                raise ValueError
            return cls(
                application,
                dsn,
                stores,
                os.environ.get("TOOLFRAME_POSTGRES_PG_DUMP", "pg_dump"),
                os.environ.get("TOOLFRAME_POSTGRES_PG_RESTORE", "pg_restore"),
                limit,
            )
        except Exception:  # noqa: BLE001 - malformed secrets must never appear in diagnostics
            raise PluginError(
                "postgres_config_invalid",
                "Set TOOLFRAME_POSTGRES_APPLICATION_ID, TOOLFRAME_POSTGRES_DSN and "
                "TOOLFRAME_POSTGRES_STORES in the runtime environment using setup output",
            ) from None

    def store_dsn(self, owner):
        if owner not in self.stores:
            raise PluginError(
                "store_not_provisioned",
                "Provision the plugin store with toolframe-postgres setup",
            )
        return make_conninfo(self.dsn, **self.stores[owner])

    def store_names(self, owner):
        return names(
            self.application_id, owner, database=conninfo_to_dict(self.dsn)["dbname"]
        )
