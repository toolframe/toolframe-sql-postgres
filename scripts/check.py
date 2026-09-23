"""Require real database and first-party coverage in full integration checks."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

if not os.environ.get("TOOLFRAME_POSTGRES_TEST_ADMIN_DSN"):
    raise SystemExit(
        "Set TOOLFRAME_POSTGRES_TEST_ADMIN_DSN for an isolated disposable PostgreSQL server"
    )
for module in [
    "toolframe_auth",
    "toolframe_mcp_auth",
    "toolframe_oidc_auth",
    "toolframe_demo_entry_register",
]:
    importlib.import_module(module)
root = Path(__file__).resolve().parents[1]
subprocess.run(
    [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"],
    cwd=root,
    check=True,
)
subprocess.run(
    [sys.executable, "-m", "pytest", "--tb=short", "-q"], cwd=root, check=True
)
