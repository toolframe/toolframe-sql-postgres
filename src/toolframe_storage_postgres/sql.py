"""A bounded SQL surface for managed calls; PostgreSQL roles enforce ownership."""

from __future__ import annotations

from pglast import ast, parse_sql
from pglast.stream import RawStream
from pglast.visitors import Visitor
from toolframe_sdk import PluginError

# These are builtins in pg_catalog, which is first in the fixed search_path.
FUNCTIONS = frozenset(
    [
        "abs",
        "avg",
        "count",
        "length",
        "char_length",
        "lower",
        "upper",
        "min",
        "max",
        "sum",
        "round",
        "trim",
        "btrim",
        "ltrim",
        "rtrim",
        "substr",
        "substring",
        "replace",
        "concat",
        "concat_ws",
        "greatest",
        "least",
        "nullif",
    ]
)
MUTATIONS = (ast.InsertStmt, ast.UpdateStmt, ast.DeleteStmt)
DDL = (ast.CreateStmt, ast.IndexStmt, ast.AlterTableStmt, ast.DropStmt)


def parameters(source: str, count: int) -> str:
    """Convert SDK qmarks to PostgreSQL AST parameters, respecting SQL quoting."""
    result = []
    i = 0
    position = 0
    while i < len(source):
        char = source[i]
        if source.startswith("--", i):
            end = source.find("\n", i)
            end = len(source) if end < 0 else end
            result.append(source[i:end])
            i = end
        elif source.startswith("/*", i):
            start = i
            depth = 1
            i += 2
            while i < len(source) and depth:
                if source.startswith("/*", i):
                    depth += 1
                    i += 2
                elif source.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise ValueError("Unclosed SQL comment")
            result.append(source[start:i])
        elif char in "\"'":
            start = i
            quote = char
            i += 1
            # Escape-string syntax is outside the SDK parameter contract.
            if quote == "'" and start and source[start - 1] in "eE":
                raise ValueError("Use parameters instead of escape-string literals")
            while i < len(source):
                if source[i] == quote:
                    i += 1
                    if i < len(source) and source[i] == quote:
                        i += 1
                        continue
                    break
                i += 1
            else:
                raise ValueError("Unclosed SQL literal")
            result.append(source[start:i])
        elif char == "$":
            raise ValueError("Use SDK qmark parameters; dollar quoting is unsupported")
        elif char == "?":
            position += 1
            result.append(f"${position}")
            i += 1
        else:
            result.append(char)
            i += 1
    if position != count:
        raise ValueError("SQL parameter count does not match")
    return "".join(result)


class Guard(Visitor):
    def __init__(self, *, read_only=False, migration=False):
        self.read_only, self.migration = read_only, migration

    def visit(self, ancestors, node):
        if self.read_only and isinstance(node, MUTATIONS):
            raise ValueError("Read-only query cannot mutate")
        if isinstance(node, ast.RangeVar) and (
            node.schemaname
            or node.catalogname
            or node.relname.startswith(("_toolframe", "pg_"))
            or node.relpersistence != "p"
        ):
            raise ValueError("Use unqualified permanent store-owned tables")
        if isinstance(node, ast.IndexStmt) and node.concurrent:
            raise ValueError("Concurrent indexes cannot run in a migration transaction")
        if isinstance(node, ast.A_Expr) and node.name and len(node.name) != 1:
            raise ValueError("Qualified operators are outside the SQL contract")
        if isinstance(node, ast.FuncCall):
            parts = [part.sval for part in node.funcname]
            if len(parts) != 1 or parts[0].lower() not in FUNCTIONS:
                raise ValueError("Function is outside the managed SQL contract")
        if isinstance(node, ast.TypeName):
            parts = [part.sval for part in node.names]
            if any(
                part
                in {"regclass", "regproc", "regprocedure", "regnamespace", "regrole"}
                for part in parts
            ):
                raise ValueError("Catalog identity casts are unsupported")
        if isinstance(node, ast.SelectStmt) and (node.intoClause or node.lockingClause):
            raise ValueError("SELECT INTO and explicit row locking are unsupported")
        if isinstance(node, ast.DropStmt):
            for name in node.objects:
                if len(name) != 1 or name[0].sval.startswith(("_toolframe", "pg_")):
                    raise ValueError("Only unqualified store objects may be dropped")
        if isinstance(node, ast.AlterTableCmd) and node.subtype.name not in {
            "AT_AddColumn",
            "AT_DropColumn",
            "AT_AlterColumnType",
            "AT_SetNotNull",
            "AT_DropNotNull",
            "AT_ColumnDefault",
            "AT_AddConstraint",
            "AT_DropConstraint",
            "AT_ValidateConstraint",
        }:
            raise ValueError("Unsupported table alteration")


def prepare(source: str, values, method: str):
    try:
        if not isinstance(source, str) or len(source) > 900_000:
            raise ValueError
        tree = parse_sql(parameters(source, len(values)))
        if len(tree) != 1:
            raise ValueError("Exactly one SQL statement is required")
        statement = tree[0].stmt
        allowed = (ast.SelectStmt,) if method == "query" else MUTATIONS
        if not isinstance(statement, allowed):
            raise TypeError("Unsupported SQL statement")
        Guard(read_only=method == "query")(statement)
        returning = getattr(statement, "returningClause", None)
        if method == "execute_returning" and not returning:
            raise ValueError("Mutation must include RETURNING")
        if method == "execute" and returning:
            raise ValueError("Use execute_returning for returned rows")
        # AsyncRawCursor accepts native $n placeholders and does not reinterpret '%'.
        return RawStream()(statement), list(values)
    except Exception:  # noqa: BLE001 - parser diagnostics can contain bound/literal secrets
        raise PluginError(
            "sql_not_supported",
            "SQL is outside the managed PostgreSQL contract or has invalid parameters",
        ) from None


def migration_statements(source):
    try:
        statements = parse_sql(source)
        for raw in statements:
            if not isinstance(raw.stmt, (*DDL, *MUTATIONS)):
                raise TypeError
            if isinstance(raw.stmt, ast.DropStmt) and raw.stmt.removeType.name not in {
                "OBJECT_TABLE",
                "OBJECT_INDEX",
            }:
                raise ValueError
            Guard(migration=True)(raw.stmt)
            yield RawStream()(raw.stmt)
    except Exception:  # noqa: BLE001 - redact SQL before crossing the RPC boundary
        raise PluginError(
            "migration_sql_invalid",
            "Migration must contain transactional store-local table/index DDL or DML",
        ) from None
