"""
Stage 1: read Python code and record facts about its database use.

The code is never run. Instead, Python's built-in `ast` module turns the
source text into an AST ("abstract syntax tree"): a tree of objects, one per
statement and expression, such as ast.For for a `for` loop or ast.Call for a
function call. We walk through that tree and fill in the classes from
ast_model.py (queries, loops, appends, totals, ...).

Queries are recognised when written in either of these two ways:
  1. rows = db.execute("SELECT ...")
  2. cursor.execute("SELECT ...")
     rows = cursor.fetchall()        (or cursor.fetchone())

Values from a row can be read in three ways, and all are understood:
  row.id        row["id"]        row[0]
also through a short name, e.g. `customer_id = customer[0]` and later
`customer_id`. These short names are called "aliases" below.

At the end, rewrites.py is asked to build faster SQL for any loop shapes it
fully understands.
"""
from __future__ import annotations

import ast
import re
from typing import Callable, Dict, List, Optional, Set, Tuple

from .ast_model import (
    AggregationOp, AppendOp, Condition, ExistsCheck, FetchedTable, ListUsage, LoopNode, ProcedureAST,
    QueryCall,
)
from .rewrites import build_rewrite_hints
from .sql_facts import condition_to_sql, has_limit, parse_select, resolve_row_field
from .value_sql import FieldResolver, SqlExpr, ValueTranslator, track_loop_values

Aliases = Dict[str, str]  # short name -> what it stands for, e.g. {"customer_id": "customer[0]"}


# ---------------------------------------------------------------------------
# Turning expressions into simple text
# ---------------------------------------------------------------------------

def _resolve_expr(node: ast.AST, aliases: Aliases) -> str:
    """Write an expression as short text, e.g. "row.id", "row[0]" or "row['id']".

    Aliases are replaced by what they stand for, so `customer_id` becomes
    "customer[0]" after `customer_id = customer[0]`. Anything else is written
    back as normal Python code."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)

    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        base = aliases.get(node.value.id, node.value.id)
        return f"{base}.{node.attr}"

    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        base = aliases.get(node.value.id, node.value.id)
        idx_node = node.slice
        if isinstance(idx_node, ast.Index):  # pragma: no cover (only needed on Python 3.8 and older)
            idx_node = idx_node.value
        if isinstance(idx_node, ast.Constant) and isinstance(idx_node.value, int):
            return f"{base}[{idx_node.value}]"
        if isinstance(idx_node, ast.Constant) and isinstance(idx_node.value, str):
            return f"{base}['{idx_node.value}']"

    try:
        return ast.unparse(node)
    except Exception:
        return "<expr>"


def _maybe_record_alias(stmt: ast.stmt, aliases: Aliases) -> None:
    """Remember short names for row values.

    After `price = row[2]`, the name `price` means `row[2]`, so we store
    {"price": "row[2]"} in `aliases`. If `price` is later given some other
    value, the stored alias is removed because it is no longer true."""
    if not (
        isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
    ):
        return
    target = stmt.targets[0].id
    value = stmt.value
    is_field_access = (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)) or (
        isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name)
    )
    if is_field_access:
        aliases[target] = _resolve_expr(value, aliases)
    else:
        aliases.pop(target, None)


# ---------------------------------------------------------------------------
# Recognising database calls
# ---------------------------------------------------------------------------

def _cursor_key(node: ast.AST) -> Optional[str]:
    """The name of the object a method is called on, as text:
    `cursor` -> "cursor", `self.cursor` -> "self.cursor", anything else -> None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _cursor_key(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_execute_call(node: ast.AST) -> bool:
    """True for a call like `something.execute(sql, ...)`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and bool(node.args)
    )


def _is_fetch_call(node: ast.AST) -> bool:
    """True for `something.fetchall()` or `something.fetchone()`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("fetchall", "fetchone")
    )


def _is_scalar_fetch(node: ast.AST) -> bool:
    """True for `cursor.fetchone()[0]`: reading one single value, such as
    the result of `SELECT COUNT(*) ...`."""
    return (
        isinstance(node, ast.Subscript) and _is_fetch_call(node.value) and node.value.func.attr == "fetchone"
        and isinstance(node.slice, ast.Constant) and node.slice.value == 0
    )


# Matches SQL that starts with a single total, e.g. "SELECT COUNT(*) FROM ...".
_AGG_SELECT_RE = re.compile(r"^\s*SELECT\s+(?P<agg>(?:COUNT|SUM|AVG|MIN|MAX)\s*\([^)]*\))\s+FROM\b", re.I)
# Python comparison -> the same comparison written in SQL.
_CMP_SYMBOL = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=", ast.Eq: "=", ast.NotEq: "!="}


def _emptiness_test(test: ast.expr) -> Optional[Tuple[str, bool]]:
    """Recognise a test that checks whether a variable holds any rows.

    Returns (variable name, negated):
      - negated=True means "is empty":  `not v`, `v is None`, `len(v) == 0`, `len(v) < 1`
      - negated=False means "has rows": `v`, `v is not None`, `len(v) > 0`, `len(v) != 0`, `len(v) >= 1`
    Returns None for any other test."""
    if isinstance(test, ast.Name):
        return test.id, False
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _emptiness_test(test.operand)
        return (inner[0], not inner[1]) if inner else None
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return None
    op, left, right = test.ops[0], test.left, test.comparators[0]
    if isinstance(left, ast.Name) and isinstance(right, ast.Constant) and right.value is None:
        if isinstance(op, ast.Is):
            return left.id, True
        if isinstance(op, ast.IsNot):
            return left.id, False
    if (
        isinstance(left, ast.Call) and isinstance(left.func, ast.Name) and left.func.id == "len"
        and len(left.args) == 1 and isinstance(left.args[0], ast.Name)
        and isinstance(right, ast.Constant) and type(right.value) is int
    ):
        var, n = left.args[0].id, right.value
        if (isinstance(op, ast.Eq) and n == 0) or (isinstance(op, ast.Lt) and n == 1) or (
            isinstance(op, ast.LtE) and n == 0
        ):
            return var, True
        if (isinstance(op, (ast.Gt, ast.NotEq)) and n == 0) or (isinstance(op, ast.GtE) and n == 1):
            return var, False
    return None


def _exists_check(body: List[ast.stmt], queries: List[QueryCall]) -> Optional[ExistsCheck]:
    """Detect a loop body that only checks whether its query found anything.

    The loop body must look like this (alias lines like `x = row[0]` may
    appear anywhere and are ignored):
        cursor.execute(...)
        rows = cursor.fetchall()      # or fetchone()
        if not rows:                  # or `if rows:`, `if len(rows) == 0:`, ...
            ...                       # must not use `rows` itself
    Returns an ExistsCheck describing it, or None."""
    work = [s for s in body if not _is_alias_assign(s)]
    if len(work) != 3 or not isinstance(work[2], ast.If) or work[2].orelse:
        return None
    fetch = work[1]
    if not (
        isinstance(fetch, ast.Assign) and len(fetch.targets) == 1 and isinstance(fetch.targets[0], ast.Name)
        and _is_fetch_call(fetch.value)
    ):
        return None
    check = _emptiness_test(work[2].test)
    if check is None or check[0] != fetch.targets[0].id:
        return None
    if not any(q.result_var == check[0] and q.fetch_kind in ("fetchall", "fetchone") for q in queries):
        return None
    # The code inside the `if` must not use the rows themselves -- only the
    # fact that there were (or weren't) any.
    if any(isinstance(n, ast.Name) and n.id == check[0] for s in work[2].body for n in ast.walk(s)):
        return None
    return ExistsCheck(query_var=check[0], negated=check[1], raw=ast.unparse(work[2].test))


def _result_guard(body: List[ast.stmt], queries: List[QueryCall]) -> Optional[Condition]:
    """Detect a loop body that reads one total and then tests it.

    The loop body must look like this (alias lines are ignored):
        cursor.execute("SELECT COUNT(*) FROM ...")
        n = cursor.fetchone()[0]
        if n > 2:                     # any comparison with a number
            ...
    The test is returned as SQL with the total written out, e.g.
    `COUNT(*) > 2`. In SQL this becomes a HAVING clause: a filter applied
    after rows have been grouped and counted."""
    work = [s for s in body if not _is_alias_assign(s)]
    if len(work) != 3 or not isinstance(work[2], ast.If) or work[2].orelse:
        return None
    fetch, test = work[1], work[2].test
    if not (
        isinstance(fetch, ast.Assign) and len(fetch.targets) == 1 and isinstance(fetch.targets[0], ast.Name)
        and _is_scalar_fetch(fetch.value)
    ):
        return None
    var = fetch.targets[0].id
    query = next((q for q in queries if q.result_var == var and q.fetch_kind == "fetchone_scalar"), None)
    m = _AGG_SELECT_RE.match(query.raw_sql) if query else None
    if not m or not (isinstance(test, ast.Compare) and len(test.ops) == 1 and type(test.ops[0]) in _CMP_SYMBOL):
        return None
    left, right, op = test.left, test.comparators[0], _CMP_SYMBOL[type(test.ops[0])]
    if isinstance(left, ast.Name) and left.id == var and isinstance(right, ast.Constant):
        const = right.value
    elif isinstance(right, ast.Name) and right.id == var and isinstance(left, ast.Constant):
        const, op = left.value, {">": "<", ">=": "<=", "<": ">", "<=": ">="}.get(op, op)
    else:
        return None
    if not isinstance(const, (int, float)) or isinstance(const, bool):
        return None
    agg = re.sub(r"\s+", "", m.group("agg"))
    return Condition(raw=ast.unparse(test), sql=f"{agg} {op} {const!r}")


def _extract_sql_and_params(arg: ast.AST, aliases: Aliases) -> Optional[Tuple[str, Dict[str, str]]]:
    """Get the SQL text from the first argument of execute().

    Returns (sql_text, params). Python values pasted into the SQL are
    replaced by markers __PARAM_0__, __PARAM_1__, ..., and `params` says
    which Python expression each marker stands for. For example
        f"SELECT * FROM t WHERE id = {row[0]}"
    gives ("SELECT * FROM t WHERE id = __PARAM_0__", {"__PARAM_0__": "row[0]"}).

    Understands a plain string, an f-string, "..." % values and
    "...".format(values). Returns None for anything else."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value, {}

    if isinstance(arg, ast.JoinedStr):  # f-string
        parts: List[str] = []
        params: Dict[str, str] = {}
        i = 0
        for value in arg.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                token = f"__PARAM_{i}__"
                i += 1
                params[token] = _resolve_expr(value.value, aliases)
                parts.append(token)
        return "".join(parts), params

    if (
        isinstance(arg, ast.BinOp)
        and isinstance(arg.op, ast.Mod)
        and isinstance(arg.left, ast.Constant)
        and isinstance(arg.left.value, str)
    ):  # "..." % (values)  /  "..." % value
        template = arg.left.value
        values = list(arg.right.elts) if isinstance(arg.right, (ast.Tuple, ast.List)) else [arg.right]
        params: Dict[str, str] = {}

        def _replace_percent(_m: "re.Match[str]") -> str:
            idx = len(params)
            token = f"__PARAM_{idx}__"
            if idx < len(values):
                params[token] = _resolve_expr(values[idx], aliases)
            return token

        return re.sub(r"%[sd]", _replace_percent, template), params

    if (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "format"
        and isinstance(arg.func.value, ast.Constant)
        and isinstance(arg.func.value.value, str)
    ):  # "...".format(values)
        template = arg.func.value.value
        values = arg.args
        params: Dict[str, str] = {}

        def _replace_braces(_m: "re.Match[str]") -> str:
            idx = len(params)
            token = f"__PARAM_{idx}__"
            if idx < len(values):
                params[token] = _resolve_expr(values[idx], aliases)
            return token

        return re.sub(r"\{\}", _replace_braces, template), params

    return None


# Finds the first table name after FROM.
_FROM_TABLE_RE = re.compile(r"FROM\s+([A-Za-z_]\w*)", re.IGNORECASE)


def _build_query_call(cursor: str, sql_raw: str, params: Dict[str, str], lineno: int) -> QueryCall:
    """Create a QueryCall object for one query."""
    table, columns, where = parse_select(sql_raw)
    if table is None:
        # parse_select only understands simple one-table SELECTs. For other
        # SQL (JOINs, subqueries, ...) at least find the first table name.
        m = _FROM_TABLE_RE.search(sql_raw)
        table = m.group(1) if m else None
    return QueryCall(
        lineno=lineno, cursor=cursor, raw_sql=sql_raw, bound_params=params,
        table=table, columns=columns, where=where, has_limit=has_limit(sql_raw),
    )


# ---------------------------------------------------------------------------
# Recognising appends, running totals and counters
# ---------------------------------------------------------------------------

def _is_zero_init(stmt: Optional[ast.stmt]) -> Optional[str]:
    """If `stmt` is `name = 0` (or `name = 0.0`), return `name`; else None."""
    if (
        isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, ast.Constant) and stmt.value.value in (0, 0.0)
    ):
        return stmt.targets[0].id
    return None


def _is_false_init(stmt: Optional[ast.stmt]) -> Optional[str]:
    """If `stmt` is `name = False`, return `name`; else None."""
    if (
        isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, ast.Constant) and stmt.value.value is False
    ):
        return stmt.targets[0].id
    return None


def _extract_append(stmt: ast.stmt, aliases: Aliases) -> Optional[AppendOp]:
    """Recognise `target.append({...})`, `target.append(x)`,
    `target.extend(x)` or `target.add(x)` and return an AppendOp."""
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return None
    call = stmt.value
    if not (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)):
        return None
    target = call.func.value.id

    if call.func.attr == "append" and call.args and isinstance(call.args[0], ast.Dict):
        field_map = {
            k.value: _resolve_expr(v, aliases)
            for k, v in zip(call.args[0].keys, call.args[0].values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        return AppendOp(lineno=stmt.lineno, kind="append", target=target, field_map=field_map)

    if call.func.attr in ("append", "extend", "add") and call.args:
        return AppendOp(
            lineno=stmt.lineno, kind=call.func.attr, target=target,
            source_expr=_resolve_expr(call.args[0], aliases),
        )
    return None


def _extract_dict_assign(stmt: ast.stmt, aliases: Aliases) -> Optional[AppendOp]:
    """Recognise `some_dict[key] = value`, which is often used to group
    rows by a key, and return an AppendOp of kind "dict_assign"."""
    if (
        isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Subscript) and isinstance(stmt.targets[0].value, ast.Name)
    ):
        target = stmt.targets[0]
        return AppendOp(
            lineno=stmt.lineno, kind="dict_assign", target=target.value.id,
            key_expr=_resolve_expr(target.slice, aliases), source_expr=_resolve_expr(stmt.value, aliases),
        )
    return None


def _added_operand(stmt: ast.stmt, acc: str) -> Optional[ast.expr]:
    """If `stmt` adds something to the variable `acc`, return what is added.

    All three ways of writing it are understood:
        acc += x        acc = acc + x        acc = x + acc
    In each case the `x` part is returned. Otherwise returns None."""
    if (
        isinstance(stmt, ast.AugAssign) and isinstance(stmt.op, ast.Add)
        and isinstance(stmt.target, ast.Name) and stmt.target.id == acc
    ):
        return stmt.value
    if (
        isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == acc
        and isinstance(stmt.value, ast.BinOp) and isinstance(stmt.value.op, ast.Add)
    ):
        left, right = stmt.value.left, stmt.value.right
        if isinstance(left, ast.Name) and left.id == acc:
            return right
        if isinstance(right, ast.Name) and right.id == acc:
            return left
    return None


def _extract_loop_aggregation(
    for_stmt: ast.For, prev_stmt: Optional[ast.stmt], loop_vars: Set[str], aliases: Aliases,
    loop_columns: Optional[Dict[str, List[str]]] = None,
    zero_inits: Optional[Dict[str, int]] = None,
) -> List[AggregationOp]:
    """Find running totals, counters and flags that a loop builds by hand.

    Examples of what is found:
        total = 0                        count = 0
        for row in rows:                 for row in rows:
            total += row[1]                  if row[2] is not None:
                                                 count += 1
    and
        found = False
        for row in rows:
            if row[0] == wanted:
                found = True

    In SQL these would be SUM(), COUNT() and EXISTS(). Several totals can be
    updated in the same loop, even inside the same `if`; each one becomes
    its own AggregationOp. An `if` can also be inside another `if`; the
    tests are then combined with AND.

    `prev_stmt` is the statement just before the `for` line.
    `zero_inits` lists `name = 0` lines seen earlier (name -> line number),
    so a total that was set to 0 one or two loops further out is found too.

    This looks at the loop body exactly as written, keeping each `if`
    around the lines inside it, so the `if` tests are known."""
    body = for_stmt.body

    seeds: Dict[str, int] = {}
    zero_var = _is_zero_init(prev_stmt)
    if zero_var:
        seeds[zero_var] = prev_stmt.lineno
    for var, lineno in (zero_inits or {}).items():
        seeds.setdefault(var, lineno)

    def adds(stmts: List[ast.stmt]) -> Optional[List[Tuple[ast.stmt, str, ast.expr]]]:
        """If every statement adds to a known total, return (statement, total's
        name, value added) for each. If any statement is something else, None."""
        found = []
        for s in stmts:
            hit = next(((acc, op) for acc in seeds for op in [_added_operand(s, acc)] if op is not None), None)
            if hit is None:
                return None
            found.append((s, *hit))
        return found

    def guarded_adds(stmt: ast.If, tests: List[ast.expr]) -> Optional[Tuple[list, List[ast.expr]]]:
        """Look inside `if a:` (and any `if b:` directly inside it, with no
        else) for additions. Returns (the additions, [a, b, ...]) or None."""
        tests = tests + [stmt.test]
        if len(stmt.body) == 1 and isinstance(stmt.body[0], ast.If) and not stmt.body[0].orelse:
            return guarded_adds(stmt.body[0], tests)
        found = adds(stmt.body)
        return (found, tests) if found else None

    aggs: List[AggregationOp] = []
    for stmt in body:
        top = adds([stmt])
        if top:
            s, acc, operand = top[0]
            aggs.append(AggregationOp(
                lineno=s.lineno, acc_var=acc, init_value="0", op="add",
                operand=_resolve_expr(operand, aliases), condition=None, init_lineno=seeds[acc],
            ))
        elif isinstance(stmt, ast.If) and not stmt.orelse and not _is_skip_if(stmt):
            nested = guarded_adds(stmt, [])
            if nested:
                guarded, tests = nested
                test = tests[0] if len(tests) == 1 else ast.BoolOp(op=ast.And(), values=tests)
                condition = condition_to_sql(test, loop_vars, aliases, loop_columns)
                aggs.extend(
                    AggregationOp(
                        lineno=s.lineno, acc_var=acc, init_value="0", op="add",
                        operand=_resolve_expr(operand, aliases), condition=condition, init_lineno=seeds[acc],
                    )
                    for s, acc, operand in guarded
                )
    if aggs:
        return aggs

    false_var = _is_false_init(prev_stmt)
    if false_var:
        for stmt in body:
            if not isinstance(stmt, ast.If):
                continue
            sets_true = any(
                isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                and s.targets[0].id == false_var and isinstance(s.value, ast.Constant) and s.value.value is True
                for s in stmt.body
            )
            if sets_true:
                return [AggregationOp(
                    lineno=stmt.lineno, acc_var=false_var, init_value="False", op="set_true",
                    condition=condition_to_sql(stmt.test, loop_vars, aliases, loop_columns),
                    init_lineno=prev_stmt.lineno,
                )]
    return []


def _attach_append_guards(
    body: List[ast.stmt], appends: List[AppendOp], to_condition: Callable[[ast.expr], Optional[Condition]],
) -> None:
    """Store the `if` test that directly surrounds an append.

    Only an `if` with no else, containing nothing but appends, counts:
        if row[3] == "open":
            results.append(row)
    The test is stored in each AppendOp's `guard`:
      - a test on a row value is written as SQL (`status = 'open'`);
      - a test on a plain variable against a number, such as
        `if total > 100:`, is kept as `total > 100`. When `total` is a
        running total, this later becomes a SQL HAVING clause;
      - any other test is stored as Python text only (sql is None)."""
    by_line = {a.lineno: a for a in appends}
    for stmt in body:
        if not (isinstance(stmt, ast.If) and not stmt.orelse and stmt.body):
            continue
        guarded = [by_line.get(s.lineno) for s in stmt.body]
        if not all(guarded):
            continue
        cond = to_condition(stmt.test) or _var_comparison(stmt.test) or Condition(raw=ast.unparse(stmt.test))
        for a in guarded:
            a.guard = cond


def _mark_branch_appends(body: List[ast.stmt], appends: List[AppendOp], conditions: List[Condition]) -> None:
    """Set `in_branch = True` on appends that are hidden inside other code.

    An append is "not in a branch" when it runs exactly once for every row
    that passes the loop's filters and its own guard. That is the case when
    it is written:
      - directly in the loop body,
      - inside an `if` that contains only appends (its guard), or
      - inside the `if` that holds all the loop's work (a filter that was
        turned into SQL).
    Anywhere else (another if/else, a try block, ...) sets in_branch=True."""
    direct: Set[int] = set()
    filter_sqls = {c.raw for c in conditions}
    for s in body:
        direct.add(s.lineno)
        if isinstance(s, ast.If) and not s.orelse and s.body:
            is_guard = all(isinstance(x, ast.Expr) for x in s.body)
            is_filter = ast.unparse(s.test) in filter_sqls
            if is_guard or is_filter:
                direct.update(x.lineno for x in s.body)
    for a in appends:
        a.in_branch = a.lineno not in direct


def _var_comparison(test: ast.expr) -> Optional[Condition]:
    """Turn `name > 100` (or `100 < name`, etc.) into a Condition with sql
    `name > 100`. Returns None for any other kind of test."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and type(test.ops[0]) in _CMP_SYMBOL):
        return None
    left, right, op = test.left, test.comparators[0], _CMP_SYMBOL[type(test.ops[0])]
    if isinstance(right, ast.Name) and isinstance(left, ast.Constant):
        left, right, op = right, left, {">": "<", ">=": "<=", "<": ">", "<=": ">="}.get(op, op)
    if not (isinstance(left, ast.Name) and isinstance(right, ast.Constant)):
        return None
    if not isinstance(right.value, (int, float)) or isinstance(right.value, bool):
        return None
    return Condition(raw=ast.unparse(test), sql=f"{left.id} {op} {right.value!r}")


# Each comparison and its opposite, e.g. "==" and "!=", "<" and ">=".
_NEGATED_CMP = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt,
    ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}


def _negate(test: ast.expr) -> ast.expr:
    """Return the opposite test:
    `a != b` -> `a == b`, `x is None` -> `x is not None`, and otherwise `not (test)`."""
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and type(test.ops[0]) in _NEGATED_CMP:
        return ast.Compare(left=test.left, ops=[_NEGATED_CMP[type(test.ops[0])]()], comparators=test.comparators)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return test.operand
    return ast.UnaryOp(op=ast.Not(), operand=test)


def _filter_tests(body: List[ast.stmt]) -> List[ast.expr]:
    """Find the tests that decide which rows the loop actually works on
    ("filters").

    Two kinds of `if` are filters:
      - `if X: continue` skips every row where X is true, so the rows that
        are kept are those where X is false. We store the opposite of X.
      - an `if X:` with no else that contains all of the loop's work
        (only alias lines like `x = row[0]` may be outside it).

    An if/else that only computes a value (for example a status text for
    every row) is not a filter, because every row still continues.
    """
    tests: List[ast.expr] = []
    rest: List[ast.stmt] = []
    for s in body:
        if _is_alias_assign(s):
            continue
        if (
            isinstance(s, ast.If) and not s.orelse and len(s.body) == 1
            and isinstance(s.body[0], ast.Continue)
        ):
            tests.append(_negate(s.test))
        else:
            rest.append(s)
    if len(rest) == 1 and isinstance(rest[0], ast.If) and not rest[0].orelse:
        tests.append(rest[0].test)
    return tests


def _is_skip_if(stmt: ast.stmt) -> bool:
    """True for `if X: continue` (with no else): a line that skips some rows."""
    return (
        isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1
        and isinstance(stmt.body[0], ast.Continue)
    )


def _is_alias_assign(stmt: ast.stmt) -> bool:
    """True for `name = row[i]` or `name = row.attr`: this only gives a value
    a shorter name, it doesn't do any real work."""
    return (
        isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, (ast.Subscript, ast.Attribute)) and isinstance(stmt.value.value, ast.Name)
    )


def _body_guard(body: List[ast.stmt]) -> Optional[ast.If]:
    """If all the work in `body` is inside one `if` (with no else), return
    that `if`. Alias lines like `x = row[0]` outside it are allowed."""
    work = [s for s in body if not _is_alias_assign(s)]
    if len(work) == 1 and isinstance(work[0], ast.If) and not work[0].orelse:
        return work[0]
    return None


# ---------------------------------------------------------------------------
# Making if/try blocks "flat"
# ---------------------------------------------------------------------------

def _flatten(stmts: List[ast.stmt]) -> List[ast.stmt]:
    """Return the statements with every `if` and `try` block opened up, so
    the lines inside them appear in the list directly.

    We only need to *find* queries, appends and loops, not decide which
    branch would run, so this makes sure nothing inside an `if` is missed.
    The `if` tests themselves are read separately by the functions that
    need them (for example _filter_tests and _extract_loop_aggregation)."""
    out: List[ast.stmt] = []
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            out.extend(_flatten(stmt.body))
            out.extend(_flatten(stmt.orelse))
        elif isinstance(stmt, ast.Try):
            out.extend(_flatten(stmt.body))
            for handler in stmt.handlers:
                out.extend(_flatten(handler.body))
        else:
            out.append(stmt)
    return out


# ---------------------------------------------------------------------------
# Walking through the code
# ---------------------------------------------------------------------------

# What walk_statements() returns: queries, appends, totals and loops found.
WalkResult = Tuple[List[QueryCall], List[AppendOp], List[AggregationOp], List[LoopNode]]


class _Builder:
    """Walks through the code and remembers things it has already seen.

    Some facts are only useful later: for example, a query result stored in
    `rows` near the top of a function is needed when a loop further down
    says `for row in rows:`. This class keeps those facts between steps."""

    def __init__(self) -> None:
        # Variable name -> the table its rows came from.
        self.fetched_tables: Dict[str, FetchedTable] = {}
        # The column list of each query result, and of the query each loop
        # goes over. Needed to turn `row[2]` into a real column name.
        self.result_columns: Dict[str, List[str]] = {}   # result variable -> columns
        self.loop_columns: Dict[str, List[str]] = {}     # loop variable -> columns
        # `name = 0` lines seen so far (name -> line number). A total can be
        # set to 0 in an outer loop and added to in an inner loop.
        self.zero_inits: Dict[str, int] = {}
        # For lists filled by a loop over query rows: each dict key's value
        # written as SQL. This lets a later loop over that list, such as
        # `for r in my_list: if r["is_big"]:`, have its test written as SQL.
        self.list_fields: Dict[str, Dict[str, SqlExpr]] = {}
        self.list_append_sites: Dict[str, int] = {}       # list name -> how many places add to it
        self.list_assumptions: Dict[str, Set[str]] = {}   # list name -> notes on what its SQL relies on

    def _field_resolver(self, loop_var: str, iter_source: str, aliases: Aliases) -> Optional[FieldResolver]:
        """Return a function that turns a row access like `row["x"]` into SQL.

        If the loop goes over query rows, the SQL is simply the column name.
        If it goes over a list we filled earlier (see list_fields), the SQL
        is whatever we recorded for that dict key. Otherwise returns None."""
        if iter_source in self.list_fields and self.list_append_sites.get(iter_source) == 1:
            fields = self.list_fields[iter_source]

            def from_list(node: ast.AST) -> Optional[SqlExpr]:
                f = resolve_row_field(node, {loop_var}, aliases, None)
                return fields.get(f[1]) if f else None
            return from_list
        if iter_source in self.result_columns:
            def from_query(node: ast.AST) -> Optional[SqlExpr]:
                f = resolve_row_field(node, {loop_var}, aliases, self.loop_columns)
                return (f[1], "value") if f else None
            return from_query
        return None

    def _condition_translator(
        self, iter_source: str, loop_vars: Set[str], aliases: Aliases, resolver: Optional[FieldResolver]
    ) -> Callable[[ast.expr], Optional[Condition]]:
        """Return a function that turns an `if` test into a Condition.

        For loops over query rows, condition_to_sql() is tried first. For
        loops over a list of dicts we built earlier, the dict keys are not
        real column names, so only the recorded SQL for each key is used."""
        from_list = iter_source in self.list_fields and resolver is not None

        def translate(test: ast.expr) -> Optional[Condition]:
            if not from_list:
                c = condition_to_sql(test, loop_vars, aliases, self.loop_columns)
                if c is not None:
                    return c
            if resolver is None:
                return None
            tr = ValueTranslator(resolver)
            e = tr.expr(test, {})
            if e is None or e[1] != "bool":
                return None
            assumptions = set(tr.assumptions) | (self.list_assumptions.get(iter_source, set()) if from_list else set())
            return Condition(raw=ast.unparse(test), sql=e[0], assumptions=sorted(assumptions))
        return translate

    def _record_field_sql(
        self, body: List[ast.stmt], appends: List[AppendOp], loop_var: str, iter_source: str,
        resolver: FieldResolver,
    ) -> None:
        """For each `append({...})` in the loop, work out which dict values
        can be written as SQL, and store them in the AppendOp's `field_sql`.

        Uses value_sql.track_loop_values(), which follows the variables
        computed in the loop body."""
        by_line = {a.lineno: a for a in appends}
        tr = ValueTranslator(resolver)
        source_fields = self.list_fields.get(iter_source)

        def on_append(call: ast.Call, env: Dict[str, SqlExpr]) -> None:
            a = by_line.get(call.lineno)
            if a is None or a.kind != "append" or not call.args:
                return
            arg = call.args[0]
            fields: Dict[str, SqlExpr] = {}
            if isinstance(arg, ast.Dict):
                for k, v in zip(arg.keys, arg.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        e = tr.expr(v, env)
                        if e is not None and e[1] != "datetime":
                            fields[k.value] = e
            elif isinstance(arg, ast.Name) and arg.id == loop_var and source_fields is not None:
                fields = dict(source_fields)  # the whole row is appended, so its fields stay the same
            a.field_sql = {k: sql for k, (sql, _) in fields.items()}
            self.list_fields[a.target] = fields
            self.list_assumptions[a.target] = set(tr.assumptions) | self.list_assumptions.get(iter_source, set())

        track_loop_values(body, tr, on_append)

    def _record_provenance(self, query: QueryCall) -> None:
        """Remember which table the query's result variable holds rows of."""
        # fetchone()[0] gives a single value (like a count), not rows, so skip it.
        if query.result_var and query.table and query.fetch_kind != "fetchone_scalar":
            self.fetched_tables[query.result_var] = FetchedTable(
                var_name=query.result_var, table_name=query.table, raw_sql=query.raw_sql, lineno=query.lineno,
            )
            self.result_columns[query.result_var] = query.columns

    def walk_statements(
        self, stmts: List[ast.stmt], aliases: Aliases, loop_vars: Set[str] = frozenset()
    ) -> WalkResult:
        """Go through `stmts` one by one and collect queries, appends and
        loops. Loop bodies are handled by calling this function again on
        the body (this is called "recursion").

        `aliases` holds the short names known so far, and `loop_vars` the
        names of the loops we are currently inside."""
        queries: List[QueryCall] = []
        appends: List[AppendOp] = []
        aggregations: List[AggregationOp] = []
        loops: List[LoopNode] = []

        flat = _flatten(stmts)
        i, n = 0, len(flat)
        while i < n:
            stmt = flat[i]
            prev_stmt = flat[i - 1] if i > 0 else None

            # Query style 1: rows = db.execute(SQL)
            if isinstance(stmt, ast.Assign) and _is_execute_call(stmt.value):
                call = stmt.value
                parsed = _extract_sql_and_params(call.args[0], aliases)
                if parsed:
                    var_name = stmt.targets[0].id if isinstance(stmt.targets[0], ast.Name) else None
                    q = _build_query_call(_cursor_key(call.func.value) or "?", parsed[0], parsed[1], stmt.lineno)
                    q.result_var, q.fetch_kind = var_name, "direct"
                    self._record_provenance(q)
                    queries.append(q)
                i += 1
                continue

            # Query style 2: cursor.execute(SQL), usually followed on the next
            # line by rows = cursor.fetchall() / fetchone() / fetchone()[0]
            if isinstance(stmt, ast.Expr) and _is_execute_call(stmt.value):
                call = stmt.value
                cursor = _cursor_key(call.func.value) or "?"
                parsed = _extract_sql_and_params(call.args[0], aliases)
                var_name, fetch_kind, consumed_next = None, None, False
                if parsed and i + 1 < n:
                    nxt = flat[i + 1]
                    if (
                        isinstance(nxt, ast.Assign) and len(nxt.targets) == 1
                        and isinstance(nxt.targets[0], ast.Name) and _is_fetch_call(nxt.value)
                        and _cursor_key(nxt.value.func.value) == cursor
                    ):
                        var_name, fetch_kind, consumed_next = nxt.targets[0].id, nxt.value.func.attr, True
                    elif (
                        isinstance(nxt, ast.Assign) and len(nxt.targets) == 1
                        and isinstance(nxt.targets[0], ast.Name) and _is_scalar_fetch(nxt.value)
                        and _cursor_key(nxt.value.value.func.value) == cursor
                    ):
                        var_name, fetch_kind, consumed_next = nxt.targets[0].id, "fetchone_scalar", True
                if parsed:
                    q = _build_query_call(cursor, parsed[0], parsed[1], stmt.lineno)
                    q.result_var, q.fetch_kind = var_name, fetch_kind
                    self._record_provenance(q)
                    queries.append(q)
                i += 2 if consumed_next else 1  # skip the fetch line too, if we used it
                continue

            # A `for` loop: for x in y:  /  for a, b in y:  /  for x in some_call(...):
            if isinstance(stmt, ast.For):
                target_ok = isinstance(stmt.target, ast.Name) or (
                    isinstance(stmt.target, (ast.Tuple, ast.List))
                    and all(isinstance(el, ast.Name) for el in stmt.target.elts)
                )
                if target_ok:
                    # A copy, so short names made inside the loop don't leak out of it.
                    child_aliases = dict(aliases)

                    if isinstance(stmt.target, ast.Name):
                        loop_var = stmt.target.id
                    else:
                        # `for a, b in rows:` has no single row variable. We make up
                        # a name for the row and record a = row[0], b = row[1].
                        loop_var = f"__row_L{stmt.lineno}__"
                        for pos, elt in enumerate(stmt.target.elts):
                            child_aliases[elt.id] = f"{loop_var}[{pos}]"

                    iter_source = (
                        stmt.iter.id if isinstance(stmt.iter, ast.Name) else _resolve_expr(stmt.iter, aliases)
                    )
                    child_loop_vars = set(loop_vars) | {loop_var}
                    if iter_source in self.result_columns:
                        self.loop_columns[loop_var] = self.result_columns[iter_source]

                    # First collect everything inside the loop body...
                    body_queries, body_appends, body_aggs, nested = self.walk_statements(
                        stmt.body, child_aliases, child_loop_vars
                    )
                    # ...then look at the loop as a whole.
                    resolver = self._field_resolver(loop_var, iter_source, child_aliases)
                    to_condition = self._condition_translator(
                        iter_source, child_loop_vars, child_aliases, resolver
                    )
                    # Filters are read from the loop body as written (not the
                    # flattened version), because we need to see the `if` lines.
                    conditions, opaque_filters = [], []
                    for test in _filter_tests(stmt.body):
                        c = to_condition(test)
                        if c is not None:
                            conditions.append(c)
                        else:
                            opaque_filters.append(ast.unparse(test))
                    body_aggs = body_aggs + _extract_loop_aggregation(
                        stmt, prev_stmt, child_loop_vars, child_aliases, self.loop_columns, self.zero_inits
                    )
                    _attach_append_guards(stmt.body, body_appends, to_condition)
                    _mark_branch_appends(stmt.body, body_appends, conditions)
                    if resolver is not None:
                        self._record_field_sql(stmt.body, body_appends, loop_var, iter_source, resolver)
                    guard_if = _body_guard(stmt.body)
                    guard = (
                        condition_to_sql(guard_if.test, child_loop_vars, child_aliases, self.loop_columns)
                        if guard_if is not None else None
                    )
                    loops.append(
                        LoopNode(
                            lineno=stmt.lineno, loop_var=loop_var, iter_source=iter_source,
                            iterates_query_result=iter_source in self.fetched_tables,
                            conditions=conditions, opaque_filters=opaque_filters, guard=guard,
                            result_guard=_result_guard(stmt.body, body_queries),
                            exists_check=_exists_check(stmt.body, body_queries),
                            queries=body_queries, appends=body_appends,
                            aggregations=body_aggs, nested_loops=nested,
                        )
                    )
                    i += 1
                    continue

            appended = _extract_append(stmt, aliases) or _extract_dict_assign(stmt, aliases)
            if appended:
                appends.append(appended)
                i += 1
                continue

            # A normal assignment. It may be a short name for a row value
            # (`name = row[1]`) or the start of a running total (`total = 0`).
            zero_var = _is_zero_init(stmt)
            if zero_var:
                self.zero_inits[zero_var] = stmt.lineno
            _maybe_record_alias(stmt, aliases)
            i += 1

        return queries, appends, aggregations, loops


# Matches a row access written as text: "row.city", "row['city']" or "row[0]".
_ACCESSOR_RE = re.compile(r"^(?P<var>\w+)(?:\.(?P<attr>\w+)|\['(?P<key>\w+)'\]|\[(?P<idx>-?\d+)\])$")


def _row_accessors(
    var: str, appends: List[AppendOp], aggregations: List[AggregationOp]
) -> List[Tuple[str, Optional[str]]]:
    """Find every place the row variable `var` is read in these appends
    and totals, e.g. "row[0]" or "row.city".

    Each result also includes the name the code gives that value, when it
    has one: the dict key in `{"city": row[3]}`, or the total's name in
    `total += row["price"]`. Otherwise the name is None."""
    exprs: List[Tuple[str, Optional[str]]] = []
    for a in appends:
        exprs.extend((expr, alias) for alias, expr in a.field_map.items())
        if a.source_expr:
            exprs.append((a.source_expr, None))
        if a.key_expr:
            exprs.append((a.key_expr, None))
    for agg in aggregations:
        if agg.operand:
            exprs.append((agg.operand, agg.acc_var))
    prefix, bracket_prefix = f"{var}.", f"{var}["
    return [(expr, alias) for expr, alias in exprs if expr.startswith(prefix) or expr.startswith(bracket_prefix)]


def _resolve_used_columns(query: QueryCall, accessors: List[Tuple[str, Optional[str]]]) -> None:
    """Fill in which columns of `query` the code really uses.

      - `row.city` and `row['city']` name the column directly.
      - `row[2]` is looked up in the query's column list
        (`SELECT id, name, city` -> position 2 is "city").
      - With `SELECT *` we don't know the column list, so for `row[2]` we
        only store the name the code itself uses for that value, in
        `used_indexes`. That is a hint, not a fact."""
    is_select_star = query.columns == ["*"]
    for expr, alias in accessors:
        m = _ACCESSOR_RE.match(expr)
        if not m:
            continue
        if m.group("attr") or m.group("key"):
            name = m.group("attr") or m.group("key")
        elif m.group("idx") is not None:
            idx = int(m.group("idx"))
            if not is_select_star and 0 <= idx < len(query.columns):
                name = query.columns[idx].split(".")[-1].strip()
            else:
                if alias and idx not in query.used_indexes:
                    query.used_indexes[idx] = alias
                continue
        else:
            continue
        if name not in query.used_columns:
            query.used_columns.append(name)


def _flatten_loops(loops: List[LoopNode]) -> List[LoopNode]:
    """Every loop, including loops inside other loops, in one flat list."""
    out: List[LoopNode] = []
    for l in loops:
        out.append(l)
        out.extend(_flatten_loops(l.nested_loops))
    return out


def _apply_used_column_facts(procedure_ast: ProcedureAST) -> None:
    """Fill in `used_columns` for every query, once all loops are known.

    A query's rows can be read in two ways:
      - directly:          row = cursor.fetchone()   ...   row[0]
      - through a loop:    rows = cursor.fetchall()  ...   for r in rows: r.x
    Both are checked. Knowing which columns are really used lets us
    suggest `SELECT a, b` instead of `SELECT *`.
    """
    all_loops = _flatten_loops(procedure_ast.loops)
    loop_by_iter_source = {l.iter_source: l for l in all_loops}

    def resolve(query: QueryCall, own_appends: List[AppendOp], own_aggregations: List[AggregationOp]) -> None:
        if not query.result_var:
            return
        accessors = _row_accessors(query.result_var, own_appends, own_aggregations)
        consumer = loop_by_iter_source.get(query.result_var)
        if consumer is not None:
            accessors += _row_accessors(consumer.loop_var, consumer.appends, consumer.aggregations)
        _resolve_used_columns(query, accessors)

    for query in procedure_ast.top_queries:
        resolve(query, procedure_ast.top_appends, procedure_ast.top_aggregations)
    for loop in all_loops:
        for query in loop.queries:
            resolve(query, loop.appends, loop.aggregations)


def _has_toplevel_loop(stmts: List[ast.stmt]) -> bool:
    """True if the file has a `for` loop outside all functions and classes.

    That means the main work is done at the top level of the script, so
    that is the code we should analyze."""
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.For):
                return True
    return False


# Methods that put something into a list or set.
_ADD_METHODS = ("append", "extend", "add", "insert")


def _append_site_counts(body: List[ast.stmt]) -> Dict[str, int]:
    """Count, for each list or set, how many lines add to it.
    Returns {name: count}, e.g. {"results": 1}."""
    counts: Dict[str, int] = {}
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _ADD_METHODS and isinstance(node.func.value, ast.Name)
        ):
            counts[node.func.value.id] = counts.get(node.func.value.id, 0) + 1
    return counts


def _list_usage(body: List[ast.stmt], names: Set[str]) -> Dict[str, ListUsage]:
    """For each list in `names`, find every line that uses it, sorted into
    three groups: `len(x)`, `for ... in x`, and anything else.
    Adding to the list (`x.append(...)`) doesn't count as using it."""
    module = ast.Module(body=body, type_ignores=[])
    # AST nodes don't know their parent, so build a lookup: id(child) -> parent.
    parent: Dict[int, ast.AST] = {}
    for node in ast.walk(module):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    usage = {n: ListUsage() for n in names}
    for node in ast.walk(module):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in usage):
            continue
        p, u = parent.get(id(node)), usage[node.id]
        if isinstance(p, ast.Attribute) and p.attr in _ADD_METHODS and isinstance(parent.get(id(p)), ast.Call):
            continue
        if isinstance(p, ast.For) and p.iter is node:
            u.iter_lines.append(p.lineno)
        elif (
            isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id == "len"
            and p.args == [node]
        ):
            u.len_lines.append(node.lineno)
        else:
            u.other_lines.append(node.lineno)
    return usage


def build_procedure_ast(source: str, func_name: Optional[str] = None) -> ProcedureAST:
    """Read the Python code in `source` and return all Stage 1 facts about it.

    Which part of the code is analyzed:
    - If `func_name` is given, that function.
    - Else, if there is a `for` loop outside all functions, the top-level
      script code (a script may define helper functions, but its main work
      is at the top level).
    - Else, if the code defines functions, the first one.
    - Else, the top-level script code.
    """
    tree = ast.parse(source)

    if func_name is not None:
        func_def = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
            ),
            None,
        )
        if func_def is None:
            raise ValueError(f"No function named '{func_name}' found in source.")
        body, name = func_def.body, func_def.name
    elif _has_toplevel_loop(tree.body):
        body, name = tree.body, "<module>"
    else:
        first_func = next(
            (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None
        )
        body, name = (first_func.body, first_func.name) if first_func else (tree.body, "<module>")

    builder = _Builder()
    # Counted before the walk, so the walk already knows if a list is filled in more than one place.
    builder.list_append_sites = _append_site_counts(body)
    top_queries, top_appends, top_aggregations, loops = builder.walk_statements(body, aliases={})
    procedure_ast = ProcedureAST(
        name=name, source=source, top_queries=top_queries, top_appends=top_appends,
        top_aggregations=top_aggregations, loops=loops, fetched_tables=builder.fetched_tables,
        list_usage=_list_usage(body, set(builder.list_append_sites)),
    )
    _apply_used_column_facts(procedure_ast)
    # Finally, build faster SQL for any loop shapes rewrites.py fully understands.
    procedure_ast.rewrite_hints = build_rewrite_hints(procedure_ast)
    return procedure_ast
