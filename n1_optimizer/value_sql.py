"""
Turns values computed inside a loop into SQL expressions.

Loops often compute new values from a row before using them, for example:

    for row in rows:
        start = datetime.strptime(row[1], "%Y-%m-%d")
        end = datetime.strptime(row[2], "%Y-%m-%d")
        if row[2] is not None:
            finished_late = end > start
        else:
            finished_late = False
        results.append({"id": row[0], "finished_late": finished_late})

To move this work into the database, we need `finished_late` written as SQL
using the table's columns. track_loop_values() reads the loop body from top
to bottom (without running it) and keeps a dictionary called `env`:
"variable name -> its value as SQL". Each assignment updates `env`, and an
if/else becomes a SQL `CASE WHEN ... THEN ... ELSE ... END`.

Only simple code is understood: comparisons, `and`/`or`, `is None`,
constants and datetime.strptime(). Anything else (arithmetic, method calls,
loops, `+=`, ...) makes the variable "unknown" instead of guessing.

Every SQL expression also has a "kind":
  - "value":    a normal column value or constant
  - "datetime": a text column read with datetime.strptime(). These are only
                used in comparisons. Dates written like 2024-01-31 12:00:00
                sort the same way as text and as dates, so SQL can compare
                the text directly.
  - "bool":     a true/false result. In SQL, a missing value (NULL) counts
                as false inside WHERE and CASE WHEN, the same as Python's
                False here. `!=` and `not` are never produced, because
                with NULL they would give a different answer than Python.
"""
from __future__ import annotations

import ast
from typing import Callable, Dict, List, Optional, Set, Tuple

SqlExpr = Tuple[str, str]  # (sql text, kind)
FieldResolver = Callable[[ast.AST], Optional[SqlExpr]]  # turns `row["x"]` into its SQL, or None

# Date formats where comparing the text gives the same order as comparing dates.
_SORTABLE_DATE_FORMATS = {
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
}
DATE_TEXT_ASSUMPTION = (
    "Compares timestamps as text instead of parsed datetimes: identical as long as every stored "
    "value is zero-padded ISO text (e.g. 2017-10-02 10:56:33), which datetime.strptime would "
    "otherwise also accept unpadded."
)
_CMP = {ast.Eq: "=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}


class ValueTranslator:
    """Translates single Python expressions into SQL.

    `resolve_field` is a function that knows how to turn a row access such
    as `row["price"]` into SQL. `assumptions` collects notes about anything
    the translation relies on (for example, how dates are stored)."""

    def __init__(self, resolve_field: FieldResolver):
        self.resolve_field = resolve_field
        self.assumptions: Set[str] = set()

    def expr(self, node: ast.AST, env: Dict[str, SqlExpr]) -> Optional[SqlExpr]:
        """Return (sql, kind) for the expression `node`, or None if it can't
        be translated. `env` holds the SQL of variables assigned earlier."""
        field = self.resolve_field(node)
        if field is not None:
            return field
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return ("1" if v else "0"), "bool"
            if v is None:
                return "NULL", "value"
            if isinstance(v, (int, float)):
                return repr(v), "value"
            if isinstance(v, str):
                return "'" + v.replace("'", "''") + "'", "value"
            return None
        if isinstance(node, ast.Call):
            return self._strptime(node, env)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            return self._compare(node, env)
        if isinstance(node, ast.BoolOp):
            parts = [self.expr(v, env) for v in node.values]
            if any(p is None or p[1] != "bool" for p in parts):
                return None
            joiner = " AND " if isinstance(node.op, ast.And) else " OR "
            return "(" + joiner.join(p[0] for p in parts) + ")", "bool"
        return None

    def _strptime(self, node: ast.Call, env: Dict[str, SqlExpr]) -> Optional[SqlExpr]:
        """`datetime.strptime(text, format)` -> the text column itself, marked "datetime"."""
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "strptime" and len(node.args) == 2):
            return None
        fmt = node.args[1]
        if not (isinstance(fmt, ast.Constant) and fmt.value in _SORTABLE_DATE_FORMATS):
            return None
        inner = self.expr(node.args[0], env)
        if inner is None or inner[1] != "value":
            return None
        self.assumptions.add(DATE_TEXT_ASSUMPTION)
        return inner[0], "datetime"

    def _compare(self, node: ast.Compare, env: Dict[str, SqlExpr]) -> Optional[SqlExpr]:
        """A single comparison: `a < b`, `a == b`, `x is None`, `x is not None`."""
        op_type, left, right = type(node.ops[0]), node.left, node.comparators[0]
        if op_type in (ast.Is, ast.IsNot):
            if isinstance(left, ast.Constant) and left.value is None:
                left, right = right, left
            if not (isinstance(right, ast.Constant) and right.value is None):
                return None
            subject = self.expr(left, env)
            if subject is None or subject[1] == "bool":
                return None
            return f"{subject[0]} {'IS NULL' if op_type is ast.Is else 'IS NOT NULL'}", "bool"
        if op_type not in _CMP:
            return None
        a, b = self.expr(left, env), self.expr(right, env)
        if a is None or b is None or "bool" in (a[1], b[1]):
            return None
        if (a[1] == "datetime") != (b[1] == "datetime"):
            return None  # comparing a date with a non-date would be an error in Python
        return f"({a[0]} {_CMP[op_type]} {b[0]})", "bool"


def _stored_names(stmts: List[ast.stmt]) -> Set[str]:
    """All variable names that are assigned somewhere in `stmts`."""
    return {
        n.id for s in stmts for n in ast.walk(s)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }


def _is_skip_if(s: ast.stmt) -> bool:
    """True for `if X: continue` (with no else)."""
    return isinstance(s, ast.If) and not s.orelse and len(s.body) == 1 and isinstance(s.body[0], ast.Continue)


def track_loop_values(
    body: List[ast.stmt],
    translator: ValueTranslator,
    on_append: Callable[[ast.Call, Dict[str, SqlExpr]], None],
    env: Optional[Dict[str, SqlExpr]] = None,
) -> Dict[str, SqlExpr]:
    """Read a loop body from top to bottom and track each variable as SQL.

    Every time the code calls `something.append(...)`, `on_append(call, env)`
    is called with the SQL known at that moment. Returns the final `env`.

    How if/else is handled:
      - `if C: a = X  else: a = Y` becomes `CASE WHEN C THEN X ELSE Y END`.
      - A variable assigned in only one branch keeps that branch's value.
        This is fine when the code only reads the variable under the same
        condition (for example, a date parsed only when it isn't None).
        If the code read it somewhere else, Python would use the value from
        the previous loop round -- that case is not handled here."""
    env = dict(env or {})
    for s in body:
        if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
            value = translator.expr(s.value, env)
            if value is None:
                env.pop(s.targets[0].id, None)
            else:
                env[s.targets[0].id] = value
        elif isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
            func = s.value.func
            if isinstance(func, ast.Attribute) and func.attr == "append":
                on_append(s.value, env)
        elif _is_skip_if(s):
            continue  # a filter: skipped rows never reach the code below it
        elif isinstance(s, ast.If):
            cond = translator.expr(s.test, env)
            stored_then, stored_else = _stored_names(s.body), _stored_names(s.orelse)
            then_env = track_loop_values(s.body, translator, on_append, env)
            else_env = track_loop_values(s.orelse, translator, on_append, env)
            for name in stored_then | stored_else:
                a = then_env.get(name) if name in stored_then else env.get(name)
                b = else_env.get(name) if name in stored_else else env.get(name)
                if cond is None or cond[1] != "bool" or (name in stored_then and a is None) or (
                    name in stored_else and b is None
                ):
                    env.pop(name, None)  # the condition or a branch value is unknown
                elif a is not None and b is not None:
                    if a[1] != b[1]:
                        env.pop(name, None)  # the branches give different kinds of value
                    elif a[1] == "bool" and b[0] == "0":
                        # `X if C else False` is simply `C AND X`
                        env[name] = f"({cond[0]} AND {a[0]})", "bool"
                    else:
                        env[name] = f"CASE WHEN {cond[0]} THEN {a[0]} ELSE {b[0]} END", a[1]
                else:
                    env[name] = a if a is not None else b  # assigned in one branch only
        else:
            # Any other statement: forget variables it changes, since we
            # can't follow what it does.
            for name in _stored_names([s]):
                env.pop(name, None)
    return env
