"""
Small helpers for reading SQL text and for turning Python tests into SQL.

  - parse_select(sql)       finds the table, columns and WHERE parts of a SELECT
  - has_limit(sql)          tells whether the SQL has a LIMIT
  - resolve_row_field(...)  works out which column a Python expression like
                            `row["price"]` or `row[2]` refers to
  - condition_to_sql(...)   turns a Python `if` test into SQL, e.g.
                            `row["price"] > 10`  ->  `price > 10`

parse_select only understands simple queries of the form
`SELECT ... FROM one_table [WHERE ...] [LIMIT n]`. For anything more complex
(JOINs, subqueries, INSERT/UPDATE, ...) it returns no table. The SQL text is
still kept, so the AI in Stage 2 can read it anyway.

These helpers use regular expressions ("regex"), a small pattern language
for searching text. Each pattern below is used by only one or two functions.
"""
from __future__ import annotations

import ast
import re
from typing import Dict, List, Optional, Set, Tuple

from .ast_model import Condition

_SELECT_RE = re.compile(
    r"""^\s*SELECT\s+(?P<columns>.*?)\s+FROM\s+(?P<table>[A-Za-z_]\w*)
        (?:\s+(?:AS\s+)?(?!(?:WHERE|LIMIT)\b)(?P<alias>[A-Za-z_]\w*))?
        (?:\s+WHERE\s+(?P<where>.*?))?
        (?:\s+LIMIT\s+\d+)?\s*;?\s*$""",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)
_COND_SPLIT_RE = re.compile(r"\s+AND\s+", re.IGNORECASE)
_COND_RE = re.compile(
    r"""^\s*(?P<col>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*
        (?P<op>!=|<>|>=|<=|=|>|<|LIKE|IS\s+NOT\b|IS\b)\s*
        (?P<val>.+?)\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def has_limit(sql: str) -> bool:
    """True if the SQL contains `LIMIT <number>`."""
    return bool(_LIMIT_RE.search(sql))


def parse_select(sql: str) -> Tuple[Optional[str], List[str], List[Condition]]:
    """Split `SELECT cols FROM table [WHERE ...] [LIMIT n]` into its parts.

    Returns (table, columns, where). For example
        "SELECT id, name FROM users WHERE age > 18"
    gives ("users", ["id", "name"], [Condition for "age > 18"]).
    If the SQL has another shape, table is None and the lists are empty."""
    m = _SELECT_RE.match(sql.strip())
    if not m:
        return None, [], []
    columns_text, where_text = m.group("columns"), m.group("where")
    if m.group("alias"):
        # With only one table, `o.order_id` just means `order_id`: drop the prefix.
        columns_text = _strip_prefix(columns_text, m.group("alias"))
        where_text = _strip_prefix(where_text, m.group("alias")) if where_text else where_text
    columns = [c.strip() for c in columns_text.split(",")]
    return m.group("table"), columns, _parse_where(where_text)


_SQL_TEXT_RE = re.compile(r"('(?:[^']|'')*')")  # a text value in quotes, like 'abc'


def _strip_prefix(sql: str, alias: str) -> str:
    """Remove `alias.` in front of column names, e.g. `o.status` -> `status`.
    Text inside quotes is left unchanged."""
    pieces = _SQL_TEXT_RE.split(sql)  # odd positions are the quoted parts
    return "".join(
        piece if i % 2 else re.sub(rf"\b{re.escape(alias)}\.(?=[A-Za-z_*])", "", piece)
        for i, piece in enumerate(pieces)
    )


def _parse_where(where_text: Optional[str]) -> List[Condition]:
    """Split a WHERE clause on AND, e.g. "a = 1 AND b > 2" -> two Conditions."""
    if not where_text:
        return []
    conditions = []
    for part in _COND_SPLIT_RE.split(where_text.strip()):
        part = part.strip().rstrip(";")
        if part:
            conditions.append(Condition(raw=part, sql=part if _COND_RE.match(part) else None))
    return conditions


# ---------------------------------------------------------------------------
# Turning a Python `if` test into SQL
# ---------------------------------------------------------------------------

# Python comparison -> SQL comparison (Python's `==` is `=` in SQL).
_CMP_OP_SQL = {ast.Eq: "=", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
# Used when the two sides are swapped: `10 < x` means the same as `x > 10`.
_FLIP_OP = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}


_ALIAS_ACCESSOR_RE = re.compile(r"^(?P<var>\w+)(?:\.(?P<attr>\w+)|\['(?P<key>\w+)'\]|\[(?P<idx>\d+)\])$")


def _column_at(columns: Optional[List[str]], idx: int) -> Optional[str]:
    """The name of column number `idx` in a query's column list.

    For `SELECT id, name FROM ...`, position 1 is "name". Returns None when
    the name can't be known, for example with `SELECT *`."""
    if not columns or columns == ["*"] or not 0 <= idx < len(columns):
        return None
    col = columns[idx]
    # `expr AS name` -> name; `t.col` -> col
    col = re.split(r"\s+AS\s+", col, flags=re.IGNORECASE)[-1]
    return col.split(".")[-1].strip() or None


def resolve_row_field(
    node: ast.AST,
    loop_vars: Set[str],
    aliases: Optional[Dict[str, str]] = None,
    columns: Optional[Dict[str, List[str]]] = None,
) -> Optional[Tuple[str, str]]:
    """Work out which loop variable and column an expression reads.

    `row["price"]`, `row.price` and `row[2]` all read a column from the row
    variable `row`. The result is (loop variable, column name), for example
    ("row", "price"), or None if the expression isn't a column read.

    `aliases` handles short names for columns. After `status = order[2]`,
    the word `status` really means `order[2]`, so aliases contains
    {"status": "order[2]"}.

    `columns` gives the column list of the query each loop goes over. It is
    needed to turn a position like `row[2]` into a real column name."""
    if isinstance(node, ast.Name) and aliases and node.id in aliases:
        m = _ALIAS_ACCESSOR_RE.match(aliases[node.id])
        if not m or m.group("var") not in loop_vars:
            return None
        var = m.group("var")
        if m.group("attr") or m.group("key"):
            return var, m.group("attr") or m.group("key")
        col = _column_at((columns or {}).get(var), int(m.group("idx")))
        return (var, col) if col else None
    if isinstance(node, ast.Subscript):
        value = node.value
        if isinstance(value, ast.Name) and value.id in loop_vars:
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return value.id, key.value
            if isinstance(key, ast.Constant) and isinstance(key.value, int):
                col = _column_at((columns or {}).get(value.id), key.value)
                return (value.id, col) if col else None
    elif isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id in loop_vars:
            return node.value.id, node.attr
    return None


def condition_to_sql(
    node: ast.AST,
    loop_vars: Set[str],
    aliases: Optional[Dict[str, str]] = None,
    columns: Optional[Dict[str, List[str]]] = None,
) -> Optional[Condition]:
    """Turn a Python `if` test into SQL, if possible.

    Works for comparisons between a column and a value, and for `and` / `or`
    combinations of those. Examples:
        row["price"] > 10               ->  price > 10
        row["city"] == name             ->  city = ?   (with `name` as a parameter)
        row["date"] is None             ->  date IS NULL
        a["id"] == b["a_id"]            ->  a.id = b.a_id   (compares two loops' rows)
    Returns None when the test can't be translated."""
    if isinstance(node, ast.BoolOp):
        joiner = " AND " if isinstance(node.op, ast.And) else " OR "
        parts = [condition_to_sql(v, loop_vars, aliases, columns) for v in node.values]
        if any(p is None for p in parts):
            return None
        sql = joiner.join(f"({p.sql})" for p in parts)
        params = [p for part in parts for p in part.bound_params]
        qualified = (
            joiner.join(f"({p.qualified_sql})" for p in parts) if all(p.qualified_sql for p in parts) else None
        )
        return Condition(raw=ast.unparse(node), sql=sql, bound_params=params, qualified_sql=qualified)

    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        op_type = type(node.ops[0])
        if op_type in (ast.Is, ast.IsNot):
            # `x is None` / `x is not None` -> IS NULL / IS NOT NULL
            left, right = node.left, node.comparators[0]
            field_node = right if isinstance(left, ast.Constant) and left.value is None else left
            other = left if field_node is right else right
            field = resolve_row_field(field_node, loop_vars, aliases, columns)
            if field and isinstance(other, ast.Constant) and other.value is None:
                null_op = "IS NULL" if op_type is ast.Is else "IS NOT NULL"
                return Condition(
                    raw=ast.unparse(node), sql=f"{field[1]} {null_op}",
                    qualified_sql=f"{field[0]}.{field[1]} {null_op}",
                )
            return None
        if op_type not in _CMP_OP_SQL:
            return None
        sql_op = _CMP_OP_SQL[op_type]
        left_field = resolve_row_field(node.left, loop_vars, aliases, columns)
        right_field = resolve_row_field(node.comparators[0], loop_vars, aliases, columns)

        if left_field and right_field and left_field[0] != right_field[0]:
            # Rows from two different loops are compared: this is how nested
            # loops match rows by hand, like a SQL JOIN. Each column gets its
            # loop variable in front so it's clear which row it comes from.
            sql = f"{left_field[0]}.{left_field[1]} {sql_op} {right_field[0]}.{right_field[1]}"
            return Condition(raw=ast.unparse(node), sql=sql, qualified_sql=sql)
        if left_field and not right_field:
            return _build_condition(node, left_field, sql_op, node.comparators[0])
        if right_field and not left_field:
            return _build_condition(node, right_field, _FLIP_OP.get(sql_op, sql_op), node.left)

    return None


def _build_condition(node: ast.AST, field: Tuple[str, str], sql_op: str, other: ast.AST) -> Condition:
    """Build `column <op> value`. A fixed value (like 10 or 'x') is written
    into the SQL; anything else becomes a `?` parameter."""
    raw = ast.unparse(node)
    var, column = field
    if isinstance(other, ast.Constant):
        rhs = repr(other.value)
        return Condition(raw=raw, sql=f"{column} {sql_op} {rhs}", qualified_sql=f"{var}.{column} {sql_op} {rhs}")
    return Condition(
        raw=raw, sql=f"{column} {sql_op} ?", bound_params=[ast.unparse(other)],
        qualified_sql=f"{var}.{column} {sql_op} ?",
    )
