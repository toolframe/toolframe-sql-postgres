"""Storage compatibility through first-party public service contracts."""

import asyncio
import json
import re
import secrets
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from toolframe_sdk import PluginError, Principal, RuntimeContext, call_context

from toolframe_storage_postgres.plugin import (
    ADMIN_SERVICE,
    SQL_SERVICE,
    PostgresProvider,
)

pytest.importorskip("toolframe_auth")
pytest.importorskip("toolframe_mcp_auth")
pytest.importorskip("toolframe_oidc_auth")
pytest.importorskip("toolframe_demo_entry_register")
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from toolframe_auth.plugin import AuthProvider
from toolframe_auth.plugin import plugin as auth_definition
from toolframe_demo_entry_register.plugin import plugin as entry_definition
from toolframe_demo_entry_register.service import EntryRegister
from toolframe_mcp_auth.plugin import McpAuthProvider
from toolframe_mcp_auth.plugin import plugin as mcp_definition
from toolframe_oidc_auth.plugin import OidcAuth
from toolframe_oidc_auth.plugin import plugin as oidc_definition

OWNERS = ["auth", "mcp-auth", "oidc-auth", "entry-register"]
CATALOG = {
    name: {"category": "system" if name.startswith("system.") else "business"}
    for name in [
        "notes.read",
        "notes.write",
        "system.auth.self",
        "system.auth.passwords",
        "system.auth.mcp_keys",
    ]
}


@contextmanager
def actor(owner="auth", principal=None, **extra):
    token = call_context.set(
        {
            "application_id": "application-1",
            "owner": owner,
            "owner_generation": "one",
            "root_id": "consumer-root",
            "deadline": time.time() + 30,
            "scope_catalog": CATALOG,
            "principal": principal or Principal(kind="local").model_dump(),
            "chain": [owner],
            **extra,
        }
    )
    try:
        yield
    finally:
        call_context.reset(token)


class Peer:
    def __init__(self, owner, router):
        self.owner, self.router = owner, router

    async def request(self, method, params, timeout=None):
        assert method == "service.call"
        current = call_context.get() or {}
        with actor(self.owner, current.get("principal")):
            return await self.router[params["alias"]].invoke(
                SQL_SERVICE if params["alias"] == "storage" else "toolframe.auth",
                params["method"],
                params["params"],
            )


@pytest.fixture
async def application(database, tmp_path):
    storage = PostgresProvider(
        RuntimeContext(
            None,
            plugin_id="storage-postgres",
            generation="one",
            state_directory=tmp_path / "storage",
        )
    )
    await storage.activate()
    try:
        for definition in [
            auth_definition,
            mcp_definition,
            oidc_definition,
            entry_definition,
        ]:
            with actor(management=True):
                await storage.invoke(
                    ADMIN_SERVICE,
                    "ensure_store",
                    {"owner": definition.id, "schema": definition.store.model_dump()},
                )
        router = {"storage": storage}

        def context(owner, **kwargs):
            return RuntimeContext(
                Peer(owner, router), plugin_id=owner, generation="one", **kwargs
            )

        auth = AuthProvider(
            context(
                "auth",
                private_secrets={"password-lookup": "ab" * 32},
                config={
                    "federated_identities": [
                        {
                            "issuer": "https://identity.test",
                            "subject": "user",
                            "label": "User",
                            "scopes": ["notes.read", "notes.write"],
                        }
                    ]
                },
            )
        )
        router["auth"] = auth
        mcp = McpAuthProvider(context("mcp-auth"))
        secret = tmp_path / "client-secret"
        secret.write_text("test-client-secret")
        secret.chmod(0o600)
        oidc = OidcAuth(
            context(
                "oidc-auth",
                private_secrets={"flow-key": "cd" * 32},
                config={
                    "public_url": "https://app.test",
                    "scopes": ["notes.read", "notes.write"],
                    "dynamic_registration": True,
                    "providers": {
                        "fixture": {
                            "issuer": "https://identity.test",
                            "server_metadata_url": "https://identity.test/.well-known/openid-configuration",
                            "client_id": "app",
                            "client_secret_file": str(secret),
                        }
                    },
                    "clients": [
                        {
                            "client_id": "agent",
                            "client_name": "Agent",
                            "redirect_uris": ["http://127.0.0.1:4444/callback"],
                        }
                    ],
                },
            )
        )
        entries = EntryRegister(context("entry-register"))
        for provider in [auth, mcp, oidc, entries]:
            with actor(provider.context.plugin_id):
                await provider.activate()
        yield auth, mcp, oidc, entries
    finally:
        await storage.dispose()


@pytest.mark.parametrize("database", [OWNERS], indirect=True)
async def test_entry_crud_and_optimistic_conflict(application):
    entries = application[3]
    with actor("entry-register"):
        created = await entries.create_entry("PostgreSQL", 1.123456789)
        assert created["id"] == 1 and created["value"] == 1.123456789
        updated = await entries.update_entry(created["id"], 1, "Edited", 42.5)
        assert updated["version"] == 2
        with pytest.raises(PluginError, match="has changed"):
            await entries.update_entry(created["id"], 1, "Stale", 0)
        assert (await entries.list_entries_tool())["entries"] == [updated]


@pytest.mark.parametrize("database", [OWNERS], indirect=True)
async def test_password_sessions_and_mcp_keys_survive_provider_restart(application):
    auth, mcp, _, _ = application
    with actor():
        request = {
            "label": "Reader",
            "password": "test-password-1",
            "scopes": ["notes.read"],
        }
        concurrent = await asyncio.gather(
            *(auth.invoke("toolframe.auth", "create", request) for _ in range(2)),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in concurrent) == 1
        assert [e.code for e in concurrent if isinstance(e, PluginError)] == [
            "password_conflict"
        ]
        login = await auth.invoke(
            "toolframe.auth", "login", {"password": request["password"]}
        )
        fresh = AuthProvider(auth.context)
        await fresh.activate()
        assert (
            await fresh.invoke("toolframe.auth", "resolve", {"token": login["token"]})
        )["principal"]["scopes"] == ("notes.read",)
        await fresh.invoke(
            "toolframe.auth", "revoke", {"id": login["principal"]["credential_id"]}
        )
        with pytest.raises(PluginError):
            await fresh.invoke("toolframe.auth", "resolve", {"token": login["token"]})
    with actor("mcp-auth"):
        keys = await asyncio.gather(
            *(
                mcp.invoke(
                    "toolframe.mcp-auth",
                    "create",
                    {"label": f"Client {i}", "scopes": ["notes.read"]},
                )
                for i in range(3)
            )
        )
        fresh = McpAuthProvider(mcp.context)
        await fresh.activate()
        token = keys[0]["token"]
        assert (await fresh.invoke("toolframe.mcp-auth", "verify", {"token": token}))[
            "principal"
        ]["kind"] == "mcp_key"
        await fresh.invoke("toolframe.mcp-auth", "revoke", {"id": keys[0]["key"]["id"]})
        with pytest.raises(PluginError):
            await fresh.invoke("toolframe.mcp-auth", "verify", {"token": token})


async def http(oidc, path, data=None, *, method="GET", browser="", principal=None):
    with actor("oidc-auth", principal):
        return await oidc.invoke(
            "toolframe.oidc",
            "http",
            {
                "method": method,
                "path": path,
                "query": urlencode(data or {}) if method == "GET" else "",
                "body": urlencode(data or {}) if method == "POST" else "",
                "browser": browser,
                "headers": {},
            },
        )


@pytest.mark.parametrize("database", [OWNERS], indirect=True)
async def test_oauth_pkce_rotation_replay_and_restart(application):
    auth, _, oidc, _ = application
    with actor("oidc-auth"):
        login = await auth.invoke(
            "toolframe.auth",
            "federated_login",
            {"issuer": "https://identity.test", "subject": "user"},
        )
    verifier = secrets.token_urlsafe(48)
    authorize = {
        "client_id": "agent",
        "redirect_uri": "http://127.0.0.1:4444/callback",
        "response_type": "code",
        "state": "client-state",
        "scope": "notes.read",
        "resource": "https://app.test/mcp",
        "code_challenge_method": "S256",
        "code_challenge": create_s256_code_challenge(verifier),
    }
    consent = await http(
        oidc, "/oauth/authorize", authorize, principal=login["principal"]
    )
    assert consent["status"] == 200
    fields = dict(
        re.findall(r'name="(transaction|csrf)" value="([^"]+)"', consent["body"])
    )
    approved = await http(
        oidc,
        "/oauth/consent",
        {**fields, "decision": "allow"},
        method="POST",
        browser=consent["browser"],
        principal=login["principal"],
    )
    assert approved["status"] == 302
    code = parse_qs(urlsplit(approved["headers"]["Location"]).query)["code"][0]
    request = {
        "grant_type": "authorization_code",
        "client_id": "agent",
        "code": code,
        "redirect_uri": authorize["redirect_uri"],
        "resource": authorize["resource"],
        "code_verifier": verifier,
    }
    assert (
        await http(
            oidc, "/oauth/token", {**request, "code_verifier": "x" * 43}, method="POST"
        )
    )["status"] == 400
    result = await http(oidc, "/oauth/token", request, method="POST")
    assert result["status"] == 200
    token = json.loads(result["body"])
    assert (await http(oidc, "/oauth/token", request, method="POST"))["status"] == 400
    restarted = OidcAuth(oidc.context)
    with actor("oidc-auth"):
        await restarted.activate()
        identity = await restarted.invoke(
            "toolframe.oauth.tokens",
            "verify",
            {"token": token["access_token"], "resource": authorize["resource"]},
        )
        assert identity["principal"]["scopes"] == ("notes.read",)
    refresh = {
        "grant_type": "refresh_token",
        "client_id": "agent",
        "resource": authorize["resource"],
        "refresh_token": token["refresh_token"],
    }
    second = await http(restarted, "/oauth/token", refresh, method="POST")
    assert second["status"] == 200
    assert (await http(restarted, "/oauth/token", refresh, method="POST"))[
        "status"
    ] == 400
    with actor("oidc-auth"), pytest.raises(PluginError):
        await restarted.invoke(
            "toolframe.oauth.tokens",
            "verify",
            {
                "token": json.loads(second["body"])["access_token"],
                "resource": authorize["resource"],
            },
        )
