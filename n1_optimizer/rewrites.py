"""
Builds faster SQL for common slow loop patterns, using only Stage 1 facts.

Some fixes need several SQL ideas at once, for example a JOIN together with
SUM() and GROUP BY. The AI model often gets those wrong. But all the pieces
(table names, columns, the conditions that link rows, what is added up) are
already in the Stage 1 facts. So for the patterns below we build the SQL
here, in code, instead of relying on the AI.

Each function checks for one exact pattern. If the code doesn't match
exactly, the function returns None and the AI handles that code instead.

SQL words used below:
  JOIN        combine rows of two tables that match, e.g. orders with their customer
  LEFT JOIN   like JOIN, but also keeps rows of the first table that have no match
  GROUP BY    put rows into groups, one per value (e.g. one group per customer)
  SUM/COUNT   add up / count the rows in each group
  HAVING      a filter applied to groups, after SUM/COUNT are calculated
  EXISTS      true if a subquery returns at least one row
  alias       a short name for a table in one query, e.g. `orders o`

The patterns:
  - _chain_aggregate: a loop over query rows, with loops inside it that
    find matching rows in other tables, and a running total in the
    innermost loop. Becomes one query with JOINs, SUM/COUNT and GROUP BY.
  - _chain_rows: the same nested loops, but the innermost loop appends one
    result for every matching combination (no total). Becomes one JOIN query.
  - _scalar_having: for each row, run `SELECT COUNT(*) ...` and keep the row
    only if the count is large enough. Becomes GROUP BY ... HAVING COUNT(*) > N.
  - _semi_join: for each row, run a query only to see if it finds anything
    (`if not rows:` / `if rows:`). Becomes WHERE NOT EXISTS (...) / WHERE
    EXISTS (...). Keeping rows with *no* match is called an "anti-join";
    keeping rows *with* a match is called a "semi-join".
  - _membership_join: a loop that keeps rows depending on `row in other_rows`
    / `row not in other_rows` (another query's rows). Becomes EXISTS (for
    `in`) or an anti-join (for `not in`), instead of searching the other
    list once per row.
  - _count_only: a list that is only used with len(). Becomes
    SELECT COUNT(*) with all the loop's filters in the WHERE clause.
  - _list_lookup: a loop over a Python list of ids that runs one query per
    id. Becomes one query with `WHERE id IN (...)`.
  - _not_in_null: not a loop, but a query that uses `NOT IN (SELECT ...)`
    to find rows with no match. NOT IN returns nothing at all if the
    subquery contains a NULL, so it becomes the NULL-safe
    WHERE NOT EXISTS (...) anti-join.
"""
from __future__ import annotations

import re
import textwrap
from typing import Dict, List, Optional, Set, Tuple

from .ast_model import AppendOp, Condition, LoopNode, ProcedureAST, QueryCall, RewriteHint

# Regular expressions (text patterns) used below:
# a row access as text: "row.city", "row['city']" or "row[0]"
_ACCESSOR_RE = re.compile(r"^(?P<var>\w+)(?:\.(?P<attr>\w+)|\['(?P<key>\w+)'\]|\[(?P<idx>\d+)\])$")
# a WHERE part comparing a column with one pasted-in Python value: "customer_id = '__PARAM_0__'"
_PARAM_EQ_RE = re.compile(r"^\s*(?P<col>[A-Za-z_]\w*)\s*=\s*'?(?P<token>__PARAM_\d+__)'?\s*$")
# a simple test that starts with a column name: "status = 'open'", "price IS NOT NULL"
_SIMPLE_COND_RE = re.compile(r"^\s*(?P<col>[A-Za-z_]\w*)(?P<rest>\s*(?:=|!=|<>|>=|<=|>|<|\bLIKE\b|\bIS\b).*)$", re.I)
# a test matching rows of two loops: "order.customer_id = customer.id"
_JOIN_COND_RE = re.compile(r"^\s*(?P<lv>\w+)\.(?P<lc>\w+)\s*=\s*(?P<rv>\w+)\.(?P<rc>\w+)\s*$")
# words left out when making short table aliases (see _new_alias)
_ALIAS_SKIP = {"olist", "dataset", "table", "tbl"}
# short SQL keywords a generated alias must never be
_SQL_KEYWORDS = {
    "AS", "BY", "IF", "IN", "IS", "NO", "OF", "ON", "OR", "TO", "DO",
    "ADD", "ALL", "AND", "ASC", "END", "FOR", "KEY", "NOT", "SET", "ROW",
}
# a test of a variable against a number: "total > 100"
_VAR_CMP_RE = re.compile(r"^\s*(?P<var>\w+)\s*(?P<op>>=|<=|!=|>|<|=)\s*(?P<const>-?\d+(?:\.\d+)?)\s*$")
# SQL comparison sign -> a function doing the same comparison in Python
_PY_CMP = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b, "=": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def _all_loops(loops: List[LoopNode]) -> List[LoopNode]:
    """Every loop, including loops inside other loops, in one flat list."""
    out: List[LoopNode] = []
    for loop in loops:
        out.append(loop)
        out.extend(_all_loops(loop.nested_loops))
    return out


def _column_for(expr: str, row_var: str, query: QueryCall) -> Optional[str]:
    """The column name that `expr` reads, if `expr` reads from `row_var`.

    For example "row.price" or "row['price']" gives "price", and "row[0]"
    gives the first column of `query`. Returns None otherwise."""
    m = _ACCESSOR_RE.match(expr)
    if not m or m.group("var") != row_var:
        return None
    if m.group("attr") or m.group("key"):
        return m.group("attr") or m.group("key")
    idx = int(m.group("idx"))
    if query.columns == ["*"] or not 0 <= idx < len(query.columns):
        return None
    col = re.split(r"\s+AS\s+", query.columns[idx], flags=re.I)[-1]
    return col.split(".")[-1].strip() or None


def _qualify(sql: str, alias: str) -> Optional[str]:
    """Put a table alias in front of the column: `status = 'open'` with
    alias "o" becomes `o.status = 'open'`. None if the test isn't simple."""
    m = _SIMPLE_COND_RE.match(sql)
    return f"{alias}.{m.group('col')}{m.group('rest')}" if m else None


# A text value inside SQL, like 'abc' (two quotes '' inside mean one quote).
_SQL_STRING_RE = re.compile(r"'(?:[^']|'')*'")


def _alias_sql(qualified_sql: str, aliases: Dict[str, str]) -> Optional[str]:
    """Replace loop-variable names with table aliases.

    `aliases` maps loop variable -> table alias, e.g. {"item": "i"}.
    Then `item.price IS NOT NULL` becomes `i.price IS NOT NULL`.
    Text inside quotes is left unchanged. Returns None if a name isn't
    in `aliases`."""
    out, pos = [], 0
    for m in list(_SQL_STRING_RE.finditer(qualified_sql)) + [None]:
        end = m.start() if m else len(qualified_sql)
        chunk = qualified_sql[pos:end]
        for var in set(re.findall(r"\b([A-Za-z_]\w*)\.[A-Za-z_]\w*", chunk)):
            if var not in aliases:
                return None
        out.append(re.sub(r"\b([A-Za-z_]\w*)\.(?=[A-Za-z_])", lambda v: aliases[v.group(1)] + ".", chunk))
        if m:
            out.append(m.group(0))
            pos = m.end()
    return "".join(out)


def _split_and(sql: str) -> List[str]:
    """Split `(a) AND (b)` into ["a", "b"]. Anything else is returned as one piece."""
    parts = re.split(r"\)\s+AND\s+\(", sql.strip())
    if len(parts) > 1 and sql.strip().startswith("(") and sql.strip().endswith(")"):
        parts[0], parts[-1] = parts[0][1:], parts[-1][:-1]
        if all(p.count("(") == p.count(")") for p in parts):
            return parts
    return [sql]


def _new_alias(table: str, used: Set[str]) -> str:
    """Make a short alias from a table name, using the first letter of each
    word: "order_items" -> "oi". If that alias is already `used` in this
    query, a number is added ("oi2")."""
    words = [w for w in table.lower().split("_") if w and w not in _ALIAS_SKIP] or [table.lower()]
    base = "".join(w[0] for w in words)
    alias, n = base, 2
    # An alias must not be an SQL keyword ("order_reviews" would give "or").
    while alias in used or alias.upper() in _SQL_KEYWORDS:
        alias, n = f"{base}{n}", n + 1
    used.add(alias)
    return alias


def _loop_chain(top: LoopNode) -> Optional[List[LoopNode]]:
    """Return [top, loop inside top, loop inside that, ...].

    Each loop may contain at most one other loop. Returns None if a loop
    has two or more loops inside it, or if there is no inner loop at all."""
    chain = [top]
    while chain[-1].nested_loops:
        if len(chain[-1].nested_loops) != 1:
            return None
        chain.append(chain[-1].nested_loops[0])
    return chain if len(chain) >= 2 else None


# What _join_chain() returns (see its docstring).
Joined = Tuple[Dict[str, Tuple[str, QueryCall]], List[Tuple[str, str, List[str]]], Set[int], bool]


def _join_chain(
    outer: QueryCall, chain: List[LoopNode], queries_by_var: Dict[str, QueryCall],
    top_level: Set[int], last_condition: Optional[Condition] = None,
) -> Optional[Joined]:
    """Turn each inner loop of `chain` into a SQL JOIN.

    An inner loop finds rows that match the outer row, in one of two ways:
      a) it runs a query per outer row:
             cursor.execute(f"SELECT ... FROM items WHERE order_id = '{order[0]}'")
      b) it goes over a table fetched earlier and compares ids by hand:
             for item in all_items:
                 if item[0] != order[0]:
                     continue
    Both become `JOIN items i ON i.order_id = o.order_id`.

    `last_condition` is an extra test for the innermost rows (for example
    the `if` around a running total); it is added to that JOIN's ON part.

    Returns a tuple of four things, or None if a step doesn't fit:
      - loop variable -> (table alias, its query)
      - a list of (table, alias, [ON conditions]), one per JOIN
      - the source line numbers involved
      - True if any step ran a query per row (case a)"""
    top, last = chain[0], chain[-1]
    used: Set[str] = set()
    rows: Dict[str, Tuple[str, QueryCall]] = {top.loop_var: (_new_alias(outer.table, used), outer)}
    joins: List[Tuple[str, str, List[str]]] = []
    lines: Set[int] = {top.lineno}
    has_per_row_query = False

    for parent, child in zip(chain, chain[1:]):
        if parent is not top and (parent.appends or parent.aggregations):
            return None
        allowed_conditions: Set[Optional[str]] = set()
        per_row = [q for q in parent.queries if q.result_var == child.iter_source]
        if per_row:
            # Case a: the outer loop runs `SELECT ... WHERE col = '{row[i]}'` for every row.
            if len(parent.queries) != 1:
                return None
            q = per_row[0]
            if not q.table or not q.columns or q.has_limit or len(q.where) != 1:
                return None
            m = _PARAM_EQ_RE.match(q.where[0].sql or "")
            if not m or m.group("token") not in q.bound_params:
                return None
            ref = _ACCESSOR_RE.match(q.bound_params[m.group("token")])
            if not ref or ref.group("var") not in rows:
                return None
            src_alias, src_q = rows[ref.group("var")]
            src_col = _column_for(q.bound_params[m.group("token")], ref.group("var"), src_q)
            if not src_col:
                return None
            alias = _new_alias(q.table, used)
            on = [f"{alias}.{m.group('col')} = {src_alias}.{src_col}"]
            has_per_row_query = True
            lines.add(q.lineno)
        else:
            # Case b: the inner loop goes over rows fetched earlier and matches them by hand.
            if parent.queries:
                return None
            q = queries_by_var.get(child.iter_source)
            if q is None or id(q) not in top_level or not q.table or not q.columns or q.has_limit:
                return None
            # Find the test that matches the rows. It is written either as
            # `if a.id == b.id:` around the whole loop body, or as
            # `if a.id != b.id: continue` (which Stage 1 stores as `=`).
            candidates = ([child.guard] if child.guard else []) + list(child.conditions)
            join = None
            for c in candidates:
                jm = _JOIN_COND_RE.match(c.sql or "")
                if not jm:
                    continue
                if jm.group("lv") == child.loop_var and jm.group("rv") in rows:
                    join = c, jm.group("lc"), jm.group("rv"), jm.group("rc")
                elif jm.group("rv") == child.loop_var and jm.group("lv") in rows:
                    join = c, jm.group("rc"), jm.group("lv"), jm.group("lc")
                if join:
                    break
            if join is None:
                return None
            join_cond, own_col, other_var, other_col = join
            alias = _new_alias(q.table, used)
            on = [f"{alias}.{own_col} = {rows[other_var][0]}.{other_col}"]
            for cond in q.where:
                qualified = _qualify(cond.sql or "", alias)
                if not qualified or "__PARAM_" in qualified:
                    return None
                on.append(qualified)
            allowed_conditions.add(join_cond.sql)
            lines.add(q.lineno)

        # Any other filter in this loop is added to the JOIN's ON part. A row
        # that fails it then doesn't join. With LEFT JOIN, the outer row is
        # still kept (with a total of 0) -- the same as the Python loop,
        # which just skips that inner row.
        aliases = {var: a for var, (a, _) in rows.items()}
        aliases[child.loop_var] = alias
        extra = [c for c in child.conditions if c.sql not in allowed_conditions]
        if child is last and last_condition is not None:
            extra.append(last_condition)
        for cond in extra:
            if cond.bound_params:
                return None
            qualified = _alias_sql(cond.qualified_sql, aliases) if cond.qualified_sql else (
                _qualify(cond.sql or "", alias)
            )
            if not qualified:
                return None
            for part in _split_and(qualified):
                if part not in on:
                    on.append(part)
        rows[child.loop_var] = (alias, q)
        joins.append((q.table, alias, on))
        lines.add(child.lineno)

    return rows, joins, lines, has_per_row_query


def _outer_where(outer: QueryCall, alias: str) -> Optional[List[str]]:
    """The outer query's WHERE parts with the table alias added, or None if
    one of them can't be rewritten."""
    where: List[str] = []
    for cond in outer.where:
        qualified = _qualify(cond.sql or "", alias)
        if not qualified or "__PARAM_" in qualified:
            return None
        where.append(qualified)
    return where


def _chain_rows(
    outer: QueryCall, top: LoopNode, queries_by_var: Dict[str, QueryCall], top_level: Set[int]
) -> Optional[RewriteHint]:
    """Nested loops that append one result for every matching pair of rows.

    Example:
        for customer in customers:
            cursor.execute(f"SELECT id, status FROM orders WHERE customer_id = '{customer[0]}'")
            for order in cursor.fetchall():
                results.append({"name": customer[1], "order": order[0]})
    becomes one query:
        SELECT c.name, o.id FROM customers c JOIN orders o ON o.customer_id = c.id

    A plain JOIN is used, not LEFT JOIN: a customer with no orders never
    reaches the append in the loop, so it shouldn't appear here either."""
    if outer.has_limit or not outer.table or not outer.columns:
        return None
    if top.conditions or top.aggregations or top.appends:
        return None
    chain = _loop_chain(top)
    if chain is None:
        return None
    last = chain[-1]
    if last.queries or last.nested_loops or last.aggregations or len(last.appends) != 1:
        return None
    append = last.appends[0]
    if append.kind != "append" or not append.field_map:
        return None

    joined = _join_chain(outer, chain, queries_by_var, top_level)
    if joined is None:
        return None
    rows, joins, lines, has_per_row_query = joined
    lines.add(append.lineno)

    select: List[Tuple[str, str]] = []
    for name, expr in append.field_map.items():
        m = _ACCESSOR_RE.match(expr)
        if not m or m.group("var") not in rows:
            return None
        alias, q = rows[m.group("var")]
        col = _column_for(expr, m.group("var"), q)
        if not col:
            return None
        select.append((f"{alias}.{col}", name))

    top_alias = rows[top.loop_var][0]
    where = _outer_where(outer, top_alias)
    if where is None:
        return None
    sql_lines = [
        "SELECT " + ", ".join(f"{expr} AS {name}" for expr, name in select),
        f"FROM {outer.table} {top_alias}",
    ]
    sql_lines += [f"JOIN {table} {alias} ON " + " AND ".join(on) for table, alias, on in joins]
    if where:
        sql_lines.append("WHERE " + " AND ".join(where))

    row_dict = ", ".join(f'"{name}": r[{i}]' for i, (_, name) in enumerate(select))
    indented = "\n".join("    " + l for l in sql_lines)
    code = (
        f'{outer.cursor}.execute("""\n{indented}\n""")\n'
        f"{append.target} = [{{{row_dict}}} for r in {outer.cursor}.fetchall()]"
    )
    tables = " -> ".join([outer.table] + [t for t, _, _ in joins])
    how = "a query per row" if has_per_row_query else "nested Python loops"
    return RewriteHint(
        pattern="N_PLUS_ONE" if has_per_row_query else "MANUAL_JOIN",
        lineno=min(l for l in lines if l > top.lineno),
        explanation=(
            f"Walks {tables} with {how}, appending one result per matching row pair "
            f"(line {append.lineno}). A single JOIN query returns the same rows in one round trip."
        ),
        sql="\n".join(sql_lines),
        optimized_code=code,
        caveats=[
            "Inner JOIN on purpose: outer rows with no match produced no output in the loop either.",
            "Row order is unspecified in both versions; add ORDER BY if the order matters.",
        ],
        lines=sorted(lines),
    )


def _chain_aggregate(
    outer: QueryCall, top: LoopNode, queries_by_var: Dict[str, QueryCall], top_level: Set[int]
) -> Optional[RewriteHint]:
    """Nested loops that add up (or count) matching rows for every outer row.

    Example:
        for order in orders:
            total = 0
            cursor.execute(f"SELECT price FROM items WHERE order_id = '{order[0]}'")
            for item in cursor.fetchall():
                total += item[0]
            if total > 100:                         # optional filter on the total
                results.append({"id": order[0], "total": total})
    becomes one query:
        SELECT o.id, SUM(i.price) FROM orders o
        JOIN items i ON i.order_id = o.id
        GROUP BY o.id
        HAVING SUM(i.price) > 100

    Without an `if` filter, LEFT JOIN and COALESCE(SUM(...), 0) are used, so
    orders without items still appear with a total of 0, just like in the
    loop. (COALESCE(x, 0) means "x, or 0 if x is empty".)"""
    if outer.has_limit or not outer.table or not outer.columns:
        return None
    if top.conditions or top.aggregations or len(top.appends) != 1 or not top.appends[0].field_map:
        return None
    append = top.appends[0]

    chain = _loop_chain(top)
    if chain is None:
        return None
    last = chain[-1]
    if last.queries or last.appends or last.nested_loops or not last.aggregations:
        return None
    aggs = last.aggregations
    # Each total must be set back to 0 for every outer row, so the `total = 0`
    # line must be inside the outer loop, before the inner loop starts.
    # All totals must also share the same `if` (it goes into the JOIN's ON part).
    for agg in aggs:
        if agg.op != "add" or not agg.operand or agg.init_lineno is None:
            return None
        if not top.lineno < agg.init_lineno < chain[1].lineno:
            return None
    if len({a.condition.sql if a.condition else None for a in aggs}) != 1:
        return None
    if len({a.acc_var for a in aggs}) != len(aggs):
        return None

    # `if total > 100: results.append(...)` becomes `HAVING SUM(...) > 100`.
    # If that filter would reject a total of 0, outer rows with no matching
    # rows are dropped anyway, so a plain JOIN gives the same result as
    # LEFT JOIN and is simpler.
    having = None
    if append.guard is not None:
        hm = _VAR_CMP_RE.match(append.guard.sql or "")
        if not hm or hm.group("var") not in {a.acc_var for a in aggs}:
            return None
        having = (hm.group("var"), hm.group("op"), hm.group("const"))
    inner_join = having is not None and not _PY_CMP[having[1]](0, float(having[2]))

    joined = _join_chain(outer, chain, queries_by_var, top_level, aggs[0].condition)
    if joined is None:
        return None
    rows, joins, lines, has_per_row_query = joined
    first_step = min(l for l in lines if l > top.lineno)
    lines |= {append.lineno} | {a.lineno for a in aggs} | {a.init_lineno for a in aggs}

    agg_sql: Dict[str, str] = {}
    for agg in aggs:
        if agg.operand == "1":
            agg_sql[agg.acc_var] = f"COUNT({joins[-1][2][0].split('=')[0].strip()})"
        else:
            # The value being added may come from any of the joined tables,
            # not only the innermost one. That's fine: the JOIN produces one
            # row per match, just as the loop adds once per match.
            ref = _ACCESSOR_RE.match(agg.operand)
            if not ref or ref.group("var") not in rows:
                return None
            src_alias, src_q = rows[ref.group("var")]
            col = _column_for(agg.operand, ref.group("var"), src_q)
            if not col:
                return None
            total = f"SUM({src_alias}.{col})"
            agg_sql[agg.acc_var] = total if inner_join else f"COALESCE({total}, 0)"
    verb = " and ".join(sorted({"counts" if a.operand == "1" else "sums" for a in aggs}))
    agg_lines = ", ".join(str(a.lineno) for a in aggs)

    top_alias = rows[top.loop_var][0]
    select: List[Tuple[str, str]] = []
    group_by: List[str] = []
    for name, expr in append.field_map.items():
        if expr in agg_sql:
            select.append((agg_sql[expr], name))
            continue
        col = _column_for(expr, top.loop_var, outer)
        if not col:
            return None
        select.append((f"{top_alias}.{col}", name))
        if f"{top_alias}.{col}" not in group_by:
            group_by.append(f"{top_alias}.{col}")
    # Also group by the outer columns used in the JOINs (usually the id),
    # so that each group is exactly one outer row.
    for _, _, on in joins:
        for part in on:
            for side in part.split("="):
                side = side.strip()
                if side.startswith(f"{top_alias}.") and re.fullmatch(r"\w+\.\w+", side) and side not in group_by:
                    group_by.insert(0, side)

    where = _outer_where(outer, top_alias)
    if where is None:
        return None

    sql_lines = [
        "SELECT " + ", ".join(f"{expr} AS {name}" for expr, name in select),
        f"FROM {outer.table} {top_alias}",
    ]
    join_kw = "JOIN" if inner_join else "LEFT JOIN"
    sql_lines += [f"{join_kw} {table} {alias} ON " + " AND ".join(on) for table, alias, on in joins]
    if where:
        sql_lines.append("WHERE " + " AND ".join(where))
    sql_lines.append("GROUP BY " + ", ".join(group_by))
    if having is not None:
        sql_lines.append(f"HAVING {agg_sql[having[0]]} {having[1]} {having[2]}")

    row_dict = ", ".join(f'"{name}": r[{i}]' for i, (_, name) in enumerate(select))
    indented = "\n".join("    " + l for l in sql_lines)
    code = (
        f'{outer.cursor}.execute("""\n{indented}\n""")\n'
        f"{append.target} = [{{{row_dict}}} for r in {outer.cursor}.fetchall()]"
    )
    tables = " -> ".join([outer.table] + [t for t, _, _ in joins])
    how = "a query per row" if has_per_row_query else "nested Python loops"
    return RewriteHint(
        pattern="N_PLUS_ONE" if has_per_row_query else "MANUAL_JOIN",
        lineno=first_step,
        explanation=(
            f"Walks {tables} with {how} and {verb} the innermost rows in Python "
            f"(line {agg_lines}), once per {outer.table} row"
            + (f", then keeps only rows where `{append.guard.raw}`" if having else "")
            + ". A single query with JOINs and GROUP BY"
            + (" ... HAVING" if having else "")
            + " returns the same rows."
        ),
        sql="\n".join(sql_lines),
        optimized_code=code,
        caveats=[
            f"Assumes {', '.join(group_by)} identifies one row of {outer.table}'s result (true if it "
            "includes the primary key); otherwise GROUP BY merges rows the loop kept separate.",
            (
                "Plain JOIN: an outer row with no matches has all totals 0, which the HAVING "
                "filter rejects anyway, just like the loop's `if`."
                if inner_join else
                "LEFT JOIN + COALESCE keep outer rows with no matches, with 0, exactly as the loop does."
            ),
            "Row order is unspecified in both versions; add ORDER BY if the order matters.",
        ],
        lines=sorted(lines),
    )


# "SELECT COUNT(...) FROM <tables> WHERE <key> = <one pasted-in value>"
_AGG_QUERY_RE = re.compile(
    r"""^\s*SELECT\s+(?P<agg>COUNT\s*\([^)]*\))\s+FROM\s+(?P<from>.+?)
        \s+WHERE\s+(?P<key>(?:\w+\.)?\w+)\s*=\s*'?(?P<token>__PARAM_\d+__)'?\s*;?\s*$""",
    re.I | re.S | re.X,
)
# Each table (and its alias, if any) in a FROM ... JOIN ... list.
_FROM_ITEM_RE = re.compile(r"(?:^|\bJOIN\s+)(\w+)(?:\s+(?:AS\s+)?(?!(?:ON|JOIN|LEFT|INNER|CROSS)\b)(\w+))?", re.I)
# "COUNT(*) > 2" or "COUNT(*) >= 3"
_HAVING_RE = re.compile(r"^(?P<agg>.+?)\s*(?P<op>>=|>)\s*(?P<const>-?\d+(?:\.\d+)?)$")


def _scalar_having(outer: QueryCall, loop: LoopNode) -> Optional[RewriteHint]:
    """A loop that counts something for every row and keeps the big counts.

    Example:
        for customer in customers:
            cursor.execute(f"SELECT COUNT(*) FROM orders WHERE customer_id = '{customer[0]}'")
            n = cursor.fetchone()[0]
            if n > 2:
                results.append({"id": customer[0], "orders": n})
    becomes one query:
        SELECT customer_id, COUNT(*) FROM orders
        GROUP BY customer_id
        HAVING COUNT(*) > 2"""
    if outer.has_limit or outer.where or not outer.table or not outer.columns:
        return None
    if loop.conditions or loop.nested_loops or loop.aggregations or loop.result_guard is None:
        return None
    if len(loop.queries) != 1 or len(loop.appends) != 1 or not loop.appends[0].field_map:
        return None
    q, append = loop.queries[0], loop.appends[0]
    if q.fetch_kind != "fetchone_scalar" or len(q.bound_params) != 1:
        return None
    m = _AGG_QUERY_RE.match(q.raw_sql)
    if not m or m.group("token") not in q.bound_params:
        return None
    from_sql = re.sub(r"\s+", " ", m.group("from")).strip()
    if re.search(r"\b(?:WHERE|GROUP|HAVING|LIMIT|ORDER|UNION)\b", from_sql, re.I):
        return None

    # GROUP BY only makes groups for values that have at least one row, so a
    # count of 0 never appears. That's only correct if the `if` would have
    # rejected a count of 0 anyway (e.g. `> 2`, but not `>= 0`).
    hm = _HAVING_RE.match(loop.result_guard.sql or "")
    if not hm:
        return None
    const = float(hm.group("const"))
    if not ((hm.group("op") == ">" and const >= 0) or (hm.group("op") == ">=" and const >= 1)):
        return None

    # The column in the count query's WHERE must be the same column, of the
    # same table, that the outer loop takes its value from.
    outer_col = _column_for(q.bound_params[m.group("token")], loop.loop_var, outer)
    key = m.group("key")
    key_alias, _, key_col = key.rpartition(".")
    tables = {(alias or table): table for table, alias in _FROM_ITEM_RE.findall(from_sql)}
    key_table = tables.get(key_alias) if key_alias else (outer.table if len(tables) == 1 else None)
    if not outer_col or key_col != outer_col or key_table != outer.table:
        return None

    agg = re.sub(r"\s+", "", m.group("agg"))
    select: List[Tuple[str, str]] = []
    for name, expr in append.field_map.items():
        if expr == q.result_var:
            select.append((agg, name))
        elif _column_for(expr, loop.loop_var, outer) == outer_col:
            select.append((key, name))
        else:
            return None

    from_lines = re.sub(r"\s+((?:LEFT |INNER |CROSS )?JOIN)\b", r"\n\1", from_sql, flags=re.I).split("\n")
    sql_lines = (
        ["SELECT " + ", ".join(f"{expr} AS {name}" for expr, name in select), f"FROM {from_lines[0]}"]
        + from_lines[1:]
        + [f"GROUP BY {key}", f"HAVING {loop.result_guard.sql}"]
    )
    row_dict = ", ".join(f'"{name}": r[{i}]' for i, (_, name) in enumerate(select))
    indented = "\n".join("    " + l for l in sql_lines)
    code = (
        f'{q.cursor}.execute("""\n{indented}\n""")\n'
        f"{append.target} = [{{{row_dict}}} for r in {q.cursor}.fetchall()]"
    )
    return RewriteHint(
        pattern="N_PLUS_ONE",
        lineno=q.lineno,
        explanation=(
            f"Runs one {agg} query per row of {outer.table} (line {q.lineno}) and filters the "
            f"result in Python (`{loop.result_guard.raw}`). GROUP BY {key} with "
            f"HAVING {loop.result_guard.sql} computes every count and applies the filter in one query."
        ),
        sql="\n".join(sql_lines),
        optimized_code=code,
        caveats=[
            f"Returns each {key_col} once. The loop ran once per row of {outer.table}'s result, so a "
            f"{key_col} that appears on several rows was appended once per row (duplicates); if those "
            "duplicates were intended, join this result back to the outer query.",
            "Row order is unspecified in both versions; add ORDER BY if the order matters.",
        ],
        lines=sorted({loop.lineno, *range(q.lineno, append.lineno + 1)}),
    )


def _semi_join(outer: QueryCall, loop: LoopNode) -> Optional[RewriteHint]:
    """A loop that runs a query per row only to see whether it finds anything.

    Example ("customers without orders"):
        for customer in customers:
            cursor.execute(f"SELECT * FROM orders WHERE customer_id = {customer['id']}")
            if not cursor.fetchall():
                results.append(customer)
    becomes one query:
        SELECT c.* FROM customers c
        WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)

    The inner SELECT is a "subquery". Because it refers to the outer row
    (c.id), it is called a "correlated" subquery. Keeping rows with no match
    is an "anti-join"; `if rows:` (keeping rows *with* a match) becomes
    WHERE EXISTS and is called a "semi-join"."""
    check = loop.exists_check
    if check is None or outer.has_limit or not outer.table or not outer.columns:
        return None
    if loop.conditions or loop.opaque_filters or loop.nested_loops or loop.aggregations:
        return None
    if len(loop.queries) != 1 or len(loop.appends) != 1:
        return None
    q, append = loop.queries[0], loop.appends[0]
    if q.result_var != check.query_var or not q.table or not q.where:
        return None
    if append.guard is None or append.guard.raw != check.raw or append.kind != "append":
        return None

    used: Set[str] = set()
    oa, ia = _new_alias(outer.table, used), _new_alias(q.table, used)

    # The inner query must use exactly one value from the outer row
    # (e.g. `customer_id = {customer['id']}`). Other WHERE parts, like
    # `status = 'shipped'`, are kept inside the subquery.
    if len(q.bound_params) != 1:
        return None
    correlate: List[str] = []
    for cond in q.where:
        m = _PARAM_EQ_RE.match(cond.sql or "")
        if m and m.group("token") in q.bound_params:
            outer_col = _column_for(q.bound_params[m.group("token")], loop.loop_var, outer)
            if not outer_col:
                return None
            correlate.insert(0, f"{ia}.{m.group('col')} = {oa}.{outer_col}")
        else:
            qualified = _qualify(cond.sql or "", ia)
            if not qualified or "__PARAM_" in qualified:
                return None
            correlate.append(qualified)
    if not any(f"= {oa}." in c for c in correlate):
        return None

    # What the loop keeps: either the whole outer row, or a dict made from its values.
    if append.source_expr == loop.loop_var:
        select = f"{oa}.*" if outer.columns == ["*"] else ", ".join(f"{oa}.{c.strip()}" for c in outer.columns)
        build = f"{append.target} = {outer.cursor}.fetchall()"
    elif append.field_map:
        parts = []
        for name, expr in append.field_map.items():
            col = _column_for(expr, loop.loop_var, outer)
            if not col:
                return None
            parts.append((f"{oa}.{col}", name))
        select = ", ".join(f"{e} AS {n}" for e, n in parts)
        row_dict = ", ".join(f'"{n}": r[{i}]' for i, (_, n) in enumerate(parts))
        build = f"{append.target} = [{{{row_dict}}} for r in {outer.cursor}.fetchall()]"
    else:
        return None

    where = _outer_where(outer, oa)
    if where is None:
        return None
    key_column = correlate[0].split("=")[0].strip().split(".")[-1]
    if check.negated:
        # "Keep rows with no match": offer both anti-join forms.
        ne_sql, lj_sql = _anti_join_forms(select, outer.table, oa, q.table, ia, correlate[0], correlate[1:], where + [None])
        sql = ne_sql + "\n\n-- or --\n\n" + lj_sql
        code = _anti_join_code(outer.cursor, ne_sql, lj_sql, build, (q.table, key_column))
        how = "NOT EXISTS with a correlated subquery, or LEFT JOIN + IS NULL, answers it"
    else:
        # "Keep rows with a match": EXISTS (a JOIN would repeat rows with several matches).
        exists = "EXISTS (\n    SELECT 1\n" f"    FROM {q.table} {ia}\n    WHERE " + "\n      AND ".join(correlate) + "\n)"
        sql = "\n".join([f"SELECT {select}", f"FROM {outer.table} {oa}", _where_block(where + [exists])])
        code = f'{outer.cursor}.execute("""\n{textwrap.indent(sql, "    ")}\n""")\n{build}'
        how = "WHERE EXISTS with a correlated subquery answers it"
    kind = "anti-join" if check.negated else "semi-join"
    return RewriteHint(
        pattern="N_PLUS_ONE",
        lineno=q.lineno,
        explanation=(
            f"Runs one query on {q.table} per row of {outer.table} (line {q.lineno}) only to check "
            f"whether it returns anything (`{check.raw}`). That is an {kind}: {how} for every row "
            "in one query."
        ),
        sql=sql,
        optimized_code=code,
        caveats=[
            "NOT EXISTS / LEFT JOIN + IS NULL rather than NOT IN: NOT IN returns no rows at all if "
            "the subquery yields a NULL."
            if check.negated else
            "EXISTS rather than a JOIN: a JOIN would repeat the outer row once per match.",
            f"An index on {q.table}({key_column}) lets each lookup be quick instead of a scan.",
            "Row order is unspecified in both versions; add ORDER BY if the order matters.",
            "The key is compared column-to-column instead of being pasted into the SQL text, which also "
            "removes the SQL-injection risk of the f-string.",
        ],
        lines=sorted({loop.lineno, q.lineno, append.lineno}),
    )


def _where_block(parts: List[str]) -> str:
    """`WHERE a\n  AND b ...`. Multi-line parts (a NOT EXISTS block) are
    indented so they line up under their `AND`."""
    lines = []
    for i, part in enumerate(parts):
        keyword = "WHERE " if i == 0 else "  AND "
        pad = "" if i == 0 else "  "
        first, *rest = part.split("\n")
        lines.append(keyword + first)
        lines.extend(pad + line for line in rest)
    return "\n".join(lines)


def _anti_join_forms(
    select: str, table: str, alias: str, inner_table: str, inner_alias: str,
    match: str, inner_filters: List[str], where_parts: List[Optional[str]],
) -> Tuple[str, str]:
    """Write the same anti-join ("rows with no match") in two ways.

    `match` links the two tables, e.g. "r.order_id = o.order_id".
    `inner_filters` are extra tests on the other table's rows.
    `where_parts` are the outer query's other WHERE tests; the `None` in
    the list marks where the anti-join test goes.

    Returns (NOT EXISTS version, LEFT JOIN version):

      1. NOT EXISTS -- for each row, check that no matching row exists:
             WHERE NOT EXISTS (SELECT 1 FROM reviews r WHERE r.order_id = o.order_id)

      2. LEFT JOIN + IS NULL -- join the other table, keeping every row
         even without a partner (that's what LEFT JOIN does), then keep only
         the rows where the partner's column came back empty (NULL):
             LEFT JOIN reviews r ON r.order_id = o.order_id
             WHERE r.order_id IS NULL
         The column checked is the one used to match, so it can only be
         NULL when no partner was found."""
    inner_key = match.split("=")[0].strip()
    not_exists = (
        "NOT EXISTS (\n    SELECT 1\n"
        f"    FROM {inner_table} {inner_alias}\n"
        "    WHERE " + "\n      AND ".join([match] + inner_filters) + "\n)"
    )
    head = [f"SELECT {select}", f"FROM {table} {alias}"]
    ne_sql = "\n".join(head + [_where_block([not_exists if p is None else p for p in where_parts])])
    lj_sql = "\n".join(
        head
        + [f"LEFT JOIN {inner_table} {inner_alias} ON " + " AND ".join([match] + inner_filters)]
        + [_where_block([f"{inner_key} IS NULL" if p is None else p for p in where_parts])]
    )
    return ne_sql, lj_sql


def _anti_join_code(cursor: str, ne_sql: str, lj_sql: str, after: str, index: Tuple[str, str]) -> str:
    """Python code offering both anti-join versions, plus the index they need."""
    def execute(sql: str) -> str:
        return f'{cursor}.execute("""\n{textwrap.indent(sql, "    ")}\n""")' + (f"\n{after}" if after else "")
    table, column = index
    return (
        "# Option 1 -- NOT EXISTS: for each row, check that no matching row exists.\n"
        f"{execute(ne_sql)}\n\n"
        "# Option 2 -- LEFT JOIN + IS NULL: join the other table, keep the rows that found no partner.\n"
        f"{execute(lj_sql)}\n\n"
        "# Both are fastest with an index on the column they look up. Option 1 especially needs it:\n"
        "# SQLite builds a temporary index for a JOIN by itself, but not for NOT EXISTS.\n"
        "# Create it once if missing:\n"
        f"# CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table}({column});"
    )


# `outer_col NOT IN (SELECT inner_col FROM inner_table [alias] [WHERE ...])`
# The subquery must be simple: no brackets inside it (so no nested subqueries).
_NOT_IN_RE = re.compile(
    r"""(?P<outer>(?:(?P<oprefix>[A-Za-z_]\w*)\.)?(?P<ocol>[A-Za-z_]\w*))
        \s+NOT\s+IN\s*\(\s*
        SELECT\s+(?:DISTINCT\s+)?(?:(?P<iprefix>[A-Za-z_]\w*)\.)?(?P<icol>[A-Za-z_]\w*)
        \s+FROM\s+(?P<itable>[A-Za-z_]\w*)
        (?:\s+(?:AS\s+)?(?!WHERE\b)(?P<ialias>[A-Za-z_]\w*))?
        (?:\s+WHERE\s+(?P<iwhere>[^()]*?))?
        \s*\)""",
    re.I | re.S | re.X,
)
# A whole simple query: SELECT <list> FROM <table> [alias] WHERE <conditions>
_SIMPLE_SELECT_RE = re.compile(
    r"""^\s*SELECT\s+(?P<select>.+?)\s+FROM\s+(?P<table>[A-Za-z_]\w*)
        (?:\s+(?:AS\s+)?(?!WHERE\b)(?P<alias>[A-Za-z_]\w*))?
        \s+WHERE\s+(?P<where>.+?)\s*;?\s*$""",
    re.I | re.S | re.X,
)
_BARE_COL_RE = re.compile(r"^[A-Za-z_]\w*$")
_PREFIXED_RE = re.compile(r"^(?P<prefix>[A-Za-z_]\w*)\.(?P<col>[A-Za-z_]\w*|\*)$")


def _qualify_parts(text: str, alias: str, allowed_prefixes: Set[str]) -> Optional[List[str]]:
    """Split `a = 1 AND b > 2` on AND and put `alias.` in front of each bare
    column. Parts that already have a prefix must use one of `allowed_prefixes`.
    None if any part is too complicated."""
    parts = []
    for part in re.split(r"\s+AND\s+", " ".join(text.split()), flags=re.I):
        m = re.match(r"^(?P<prefix>[A-Za-z_]\w*)\.[A-Za-z_]\w*\s", part + " ")
        if m:
            if m.group("prefix") not in allowed_prefixes:
                return None
            parts.append(part)
        else:
            qualified = _qualify(part, alias)
            if not qualified:
                return None
            parts.append(qualified)
    return parts


def _not_in_null(q: QueryCall) -> Optional[RewriteHint]:
    """A query that uses `NOT IN (subquery)` to find rows with no match.

    Example ("orders that have no review"):
        SELECT o.order_id FROM orders o
        WHERE o.order_id NOT IN (SELECT r.order_id FROM reviews r)

    This is an anti-join, but NOT IN has a trap: if the subquery returns
    even one NULL, `x NOT IN (..., NULL)` is never true, so the query
    silently returns no rows at all. Two anti-join forms don't have that
    problem, and both are suggested (see _anti_join_forms):
        1. WHERE NOT EXISTS (SELECT 1 FROM reviews r WHERE r.order_id = o.order_id)
        2. LEFT JOIN reviews r ON r.order_id = o.order_id  WHERE r.order_id IS NULL

    Only simple queries are rewritten: one table, one NOT IN, and other
    WHERE tests joined with AND. Anything else is left to the AI."""
    if q.bound_params:
        return None
    query = _SIMPLE_SELECT_RE.match(q.raw_sql)
    if not query:
        return None
    where = query.group("where")
    if re.search(r"\b(OR|JOIN|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION)\b", where, re.I):
        return None
    matches = list(_NOT_IN_RE.finditer(where))
    if len(matches) != 1:
        return None
    m = matches[0]
    table, alias = query.group("table"), query.group("alias")
    oa = alias or _new_alias(table, set())
    names = {table, oa}

    # The outer column: must belong to the outer table.
    if m.group("oprefix") and m.group("oprefix") not in names:
        return None
    outer_ref = f"{oa}.{m.group('ocol')}"

    # The inner table needs an alias that differs from the outer one.
    ia = m.group("ialias")
    if ia is None or ia in names:
        if ia is not None or m.group("iprefix"):
            return None  # renaming an alias used inside the subquery isn't worth the risk
        ia = _new_alias(m.group("itable"), set(names))
    if m.group("iprefix") and m.group("iprefix") not in (ia, m.group("itable")):
        return None
    inner_filters = []
    if m.group("iwhere"):
        inner_filters = _qualify_parts(m.group("iwhere"), ia, {ia, m.group("itable")})
        if inner_filters is None:
            return None
    match = f"{ia}.{m.group('icol')} = {outer_ref}"

    # The other WHERE tests, with a None marking where NOT IN was.
    before, after = where[:m.start()].strip(), where[m.end():].strip()
    before = re.sub(r"\s+AND$", "", before, flags=re.I).strip()
    after = re.sub(r"^AND\s+", "", after, flags=re.I).strip()
    before_parts = _qualify_parts(before, oa, names) if before else []
    after_parts = _qualify_parts(after, oa, names) if after else []
    if before_parts is None or after_parts is None:
        return None
    where_parts: List[Optional[str]] = before_parts + [None] + after_parts

    # The SELECT list, with every column given the outer table's prefix
    # (after the JOIN, a bare name like `order_id` could mean either table).
    select_items = []
    for item in [s.strip() for s in query.group("select").split(",")]:
        as_match = re.match(r"^(?P<expr>\S+)\s+AS\s+(?P<name>\w+)$", item, re.I)
        expr, name = (as_match.group("expr"), as_match.group("name")) if as_match else (item, None)
        if expr == "*":
            expr = f"{oa}.*"
        elif _BARE_COL_RE.match(expr):
            expr = f"{oa}.{expr}"
        else:
            p = _PREFIXED_RE.match(expr)
            if not p or p.group("prefix") not in names:
                return None
            expr = f"{oa}.{p.group('col')}"
        select_items.append(f"{expr} AS {name}" if name else expr)

    ne_sql, lj_sql = _anti_join_forms(
        ", ".join(select_items), table, oa, m.group("itable"), ia, match, inner_filters, where_parts
    )
    code = _anti_join_code(q.cursor, ne_sql, lj_sql, "", (m.group("itable"), m.group("icol")))
    return RewriteHint(
        pattern="NOT_IN_NULL",
        lineno=q.lineno,
        explanation=(
            f"`{outer_ref} NOT IN (SELECT {ia}.{m.group('icol')} ...)` is an anti-join (rows with no "
            "match) written with NOT IN. If the subquery ever returns a NULL, `x NOT IN (..., NULL)` "
            "is never true and the query silently returns no rows. Two NULL-safe forms give the "
            "intended answer: NOT EXISTS, or LEFT JOIN + IS NULL."
        ),
        sql=ne_sql + "\n\n-- or --\n\n" + lj_sql,
        optimized_code=code,
        caveats=[
            "The result is the same as before whenever the subquery returns no NULLs. If it does "
            "return NULLs, the old query returned no rows at all; the new ones return the rows "
            "that really have no match.",
            f"A row whose own {outer_ref} is NULL was left out by NOT IN but is kept by both new "
            f"forms (nothing can match it). Add `AND {outer_ref} IS NOT NULL` to leave such rows out.",
            "LEFT JOIN + IS NULL returns each unmatched row once; it checks the column used for "
            "matching, which can only be NULL when no partner was found.",
            "To keep NOT IN instead, filter the NULLs out inside the subquery: "
            "`... NOT IN (SELECT col FROM t WHERE col IS NOT NULL)`.",
        ],
        lines=[q.lineno],
    )


def _membership_join(
    outer: QueryCall, loop: LoopNode, queries_by_var: Dict[str, QueryCall], top_level: Set[int]
) -> Optional[RewriteHint]:
    """A loop that keeps rows depending on whether they appear in another
    query's rows, using Python's `in` / `not in`.

    Example ("orders without a review"):
        cursor.execute("SELECT o.order_id FROM orders o")
        orders = cursor.fetchall()
        cursor.execute("SELECT r.order_id FROM reviews r")
        reviewed = cursor.fetchall()
        for order in orders:
            if order not in reviewed:
                results.append(order)

    `order not in reviewed` compares `order` with every item of `reviewed`,
    one by one. Doing that for every order means (orders x reviews)
    comparisons -- billions for large tables. In SQL, `not in` becomes an
    anti-join (both forms, see _anti_join_forms) and `in` becomes EXISTS.

    Both queries must select exactly one column: then each row is just
    that one value, and comparing rows means comparing those values."""
    mc = loop.membership
    if mc is None or mc.item != loop.loop_var:
        return None
    inner = queries_by_var.get(mc.collection)
    if inner is None or id(inner) not in top_level or inner.fetch_kind != "fetchall":
        return None
    for q in (outer, inner):
        if not q.table or len(q.columns) != 1 or q.columns == ["*"] or q.has_limit or q.bound_params:
            return None
    if loop.queries or loop.nested_loops or loop.aggregations or loop.conditions:
        return None
    if loop.opaque_filters != [mc.raw] and len(loop.opaque_filters) != 1:
        return None
    if len(loop.appends) != 1:
        return None
    append = loop.appends[0]
    if append.kind != "append" or append.in_branch or (append.guard is not None and append.guard.raw != mc.raw):
        return None

    # Reuse the aliases the original queries used (e.g. `FROM reviews r`), if they don't clash.
    oa = _own_alias(outer) or _new_alias(outer.table, set())
    ia = _own_alias(inner)
    if ia is None or ia == oa:
        ia = _new_alias(inner.table, {oa})
    outer_col = _column_at_name(outer.columns[0])
    inner_col = _column_at_name(inner.columns[0])
    if not outer_col or not inner_col:
        return None
    match = f"{ia}.{inner_col} = {oa}.{outer_col}"
    inner_filters = []
    for cond in inner.where:
        qualified = _qualify(cond.sql or "", ia)
        if not qualified:
            return None
        inner_filters.append(qualified)
    where = _outer_where(outer, oa)
    if where is None:
        return None

    # What the loop keeps: the whole row, or a dict made from it.
    if append.source_expr == loop.loop_var:
        select = f"{oa}.{outer_col}"
        build = f"{append.target} = {outer.cursor}.fetchall()"
    elif append.field_map and all(_column_for(e, loop.loop_var, outer) for e in append.field_map.values()):
        parts = [(f"{oa}.{_column_for(e, loop.loop_var, outer)}", n) for n, e in append.field_map.items()]
        select = ", ".join(f"{e} AS {n}" for e, n in parts)
        row_dict = ", ".join(f'"{n}": r[{i}]' for i, (_, n) in enumerate(parts))
        build = f"{append.target} = [{{{row_dict}}} for r in {outer.cursor}.fetchall()]"
    else:
        return None

    if mc.negated:
        ne_sql, lj_sql = _anti_join_forms(select, outer.table, oa, inner.table, ia, match, inner_filters, where + [None])
        sql = ne_sql + "\n\n-- or --\n\n" + lj_sql
        code = _anti_join_code(outer.cursor, ne_sql, lj_sql, build, (inner.table, inner_col))
        kind, pattern = "an anti-join (rows with no match)", "MANUAL_ANTI_JOIN"
    else:
        exists = (
            "EXISTS (\n    SELECT 1\n" f"    FROM {inner.table} {ia}\n    WHERE "
            + "\n      AND ".join([match] + inner_filters) + "\n)"
        )
        sql = "\n".join([f"SELECT {select}", f"FROM {outer.table} {oa}", _where_block(where + [exists])])
        code = f'{outer.cursor}.execute("""\n{textwrap.indent(sql, "    ")}\n""")\n{build}'
        kind, pattern = "a semi-join (rows with a match)", "MANUAL_SEMI_JOIN"

    return RewriteHint(
        pattern=pattern,
        lineno=loop.lineno,
        explanation=(
            f"`{mc.raw}` (line {append.lineno if append.guard else loop.lineno}) searches the whole "
            f"`{mc.collection}` list for every row of `{loop.iter_source}`, one item at a time, so the "
            f"work grows with ({outer.table} rows) x ({inner.table} rows). This is {kind} done by hand "
            "in Python; one SQL query does it with an index lookup per row."
        ),
        sql=sql,
        optimized_code=code,
        caveats=[
            f"The query that fills `{mc.collection}` (line {inner.lineno}) is no longer needed, unless "
            "its rows are used somewhere else.",
            "Python's `in` treats two missing values (None) as equal, but SQL never matches NULL with "
            "NULL. The results can only differ for rows whose value is NULL.",
            "Row order is unspecified in SQL; add ORDER BY if the order matters.",
        ],
        lines=sorted({outer.lineno, loop.lineno, append.lineno}),
    )


def _own_alias(q: QueryCall) -> Optional[str]:
    """The alias a simple query gives its table, e.g. "r" in `FROM reviews r`."""
    m = re.search(
        rf"\bFROM\s+{re.escape(q.table or '')}\s+(?:AS\s+)?(?!(?:WHERE|LIMIT|JOIN|LEFT|ORDER|GROUP)\b)([A-Za-z_]\w*)",
        q.raw_sql, re.I,
    )
    if not m or m.group(1).upper() in _SQL_KEYWORDS:
        return None
    return m.group(1)


def _column_at_name(column: str) -> Optional[str]:
    """`o.order_id` or `order_id AS id` -> the plain column name `order_id`
    (None for anything that isn't a simple column)."""
    name = re.split(r"\s+AS\s+", column.strip(), flags=re.I)[0].split(".")[-1].strip()
    return name if re.fullmatch(r"[A-Za-z_]\w*", name) else None


def _list_lookup(loop: LoopNode) -> Optional[RewriteHint]:
    """A loop over a Python list of ids that looks up each id with its own query.

    Example:
        for customer_id in customer_ids:
            cursor.execute(f"SELECT * FROM customers WHERE customer_id = '{customer_id}'")
            row = cursor.fetchone()
            if row:
                customers.append({"id": row[0], "city": row[3]})
    becomes one query, `SELECT ... WHERE customer_id IN (?, ?, ...)`, and
    then a normal loop matches the rows back to the list. That keeps the
    list's order, repeated ids and missing ids the same as before.

    Which columns are selected:
      - If the query lists its columns, only the ones the code uses are
        selected, and `row[i]` is renumbered to match.
      - With SELECT *, the query stays SELECT *: without the database we
        can't know which column is at position i. The code then finds the
        id column's position at run time from `cursor.description`."""
    if loop.iterates_query_result or loop.conditions or loop.nested_loops or loop.aggregations:
        return None
    if len(loop.queries) != 1 or len(loop.appends) != 1 or not re.fullmatch(r"\w+", loop.iter_source):
        return None
    q, append = loop.queries[0], loop.appends[0]
    if q.fetch_kind != "fetchone" or not q.result_var or not q.table or not q.columns or q.has_limit:
        return None
    if len(q.where) != 1 or len(q.bound_params) != 1 or not append.field_map:
        return None
    m = _PARAM_EQ_RE.match(q.where[0].sql or "")
    if not m or q.bound_params.get(m.group("token")) != loop.loop_var:
        return None
    for expr in append.field_map.values():
        am = _ACCESSOR_RE.match(expr)
        if not am or am.group("var") != q.result_var:
            return None

    key, row, items = m.group("col"), q.result_var, loop.iter_source
    select_star = q.columns == ["*"]
    caveats_extra: List[str] = []
    if select_star:
        # With SELECT * we can't know which position holds which column, so
        # the query keeps SELECT *, and the code asks the database at run
        # time where the id column is (cursor.description lists the names).
        select = "*"
        find_key = f"key = [d[0] for d in {q.cursor}.description].index({key!r})\n"
        key_ref = "r[key]"
        fields = ", ".join(f'"{name}": {expr}' for name, expr in append.field_map.items())
        if q.used_indexes:
            guessed = ", ".join(f"row[{i}] (called {name!r} in the code)" for i, name in sorted(q.used_indexes.items()))
            caveats_extra.append(
                f"Only some columns are used: {guessed}. Selecting just those by name instead of * would "
                "move less data, but with SELECT * the analyzer can't confirm which column is at which "
                "position -- check the table and name them yourself."
            )
    else:
        # The columns are listed, so select only the ones the code uses
        # (plus the id), and renumber row[i] to the new positions.
        positions: List[int] = []         # original positions used, in order of first use
        names: List[str] = []             # columns read by name (row["x"] / row.x)
        for expr in append.field_map.values():
            am = _ACCESSOR_RE.match(expr)
            if am.group("idx") is not None:
                i = int(am.group("idx"))
                if not 0 <= i < len(q.columns) or not _column_at_name(q.columns[i]):
                    return None
                if i not in positions:
                    positions.append(i)
            elif (am.group("key") or am.group("attr")) not in names:
                names.append(am.group("key") or am.group("attr"))
        select_items = [q.columns[i].strip() for i in positions]
        select_names = [_column_at_name(c) for c in select_items]
        for n in names + [key]:
            if n not in select_names:
                select_items.append(n)
                select_names.append(n)
        select = ", ".join(select_items)
        new_pos = {i: select_names.index(_column_at_name(q.columns[i])) for i in positions}
        find_key = ""
        key_ref = f"r[{select_names.index(key)}]"

        def renumber(expr: str) -> str:
            am = _ACCESSOR_RE.match(expr)
            return f"{row}[{new_pos[int(am.group('idx'))]}]" if am.group("idx") is not None else expr
        fields = ", ".join(f'"{name}": {renumber(expr)}' for name, expr in append.field_map.items())

    sql = f"SELECT {select}\nFROM {q.table}\nWHERE {key} IN (?, ?, ...)"
    code = (
        f'placeholders = ", ".join("?" * len({items}))\n'
        f"{q.cursor}.execute(\n"
        f'    f"SELECT {select} FROM {q.table} WHERE {key} IN ({{placeholders}})",\n'
        f"    list({items}),\n"
        f")\n"
        f"{find_key}"
        f"rows_by_{key} = {{{key_ref}: r for r in {q.cursor}.fetchall()}}\n"
        f"for {loop.loop_var} in {items}:\n"
        f"    {row} = rows_by_{key}.get({loop.loop_var})\n"
        f"    if {row}:\n"
        f"        {append.target}.append({{{fields}}})"
    )
    return RewriteHint(
        pattern="N_PLUS_ONE",
        lineno=q.lineno,
        explanation=(
            f"Runs one query on {q.table} per element of `{items}` (line {q.lineno}). A single "
            f"`WHERE {key} IN (...)` query fetches them all at once; the rows are then matched back "
            f"to `{items}` in Python, so output order, duplicates and missing ids behave as before."
        ),
        sql=sql,
        optimized_code=code,
        caveats=[
            f"Assumes {key} is unique in {q.table}: fetchone() took the first match, the dict keeps the last.",
            f"Values in `{items}` must have the same type as the {key} column for the dict lookup to match.",
            f"SQLite limits bound parameters (999 on old builds); chunk `{items}` if it can be larger.",
            "Values are now bound parameters instead of f-string interpolation, which also removes the SQL-injection risk.",
        ] + caveats_extra,
        lines=sorted({loop.lineno, q.lineno, append.lineno}),
    )


def _row_source(
    loop: LoopNode, append: AppendOp, procedure_ast: ProcedureAST,
    queries_by_var: Dict[str, QueryCall], top_level: Set[int], seen: Set[int],
) -> Optional[Tuple[QueryCall, List[Condition], Set[int]]]:
    """Find which query the rows of `append` originally come from, and
    collect every filter the rows pass on the way.

    The rows may pass through several lists, for example:
        for row in rows:            # rows come from a query
            if ...: continue        # filter 1
            results.append(...)
        for r in results:
            if r["ok"]:             # filter 2
                good.append(r)
    For `good`, this follows `results` back to the query and returns
    (the query, [filter 1, filter 2], line numbers). Returns None if any
    step is more complicated than "filter, then append one item per row"."""
    if id(loop) in seen or loop not in procedure_ast.loops:
        return None
    seen.add(id(loop))
    if loop.queries or loop.nested_loops or loop.aggregations or loop.opaque_filters:
        return None
    if len(loop.appends) != 1 or loop.appends[0] is not append or append.in_branch:
        return None
    filters = list(loop.conditions)
    if append.guard is not None and append.guard.sql not in {c.sql for c in filters}:
        filters.append(append.guard)
    if any(c.sql is None or c.bound_params for c in filters):
        return None
    lines = {loop.lineno, append.lineno}

    q = queries_by_var.get(loop.iter_source)
    if q is not None:
        if id(q) not in top_level or not q.table or not q.columns or q.has_limit:
            return None
        if any(c.sql is None for c in q.where) or q.bound_params:
            return None
        return q, filters, lines | {q.lineno}

    # The loop goes over a list we filled ourselves. That list must be used
    # only by this loop, and be filled by exactly one append in another loop.
    usage = procedure_ast.list_usage.get(loop.iter_source)
    if usage is None or usage.len_lines or usage.other_lines or usage.iter_lines != [loop.lineno]:
        return None
    sites = [(l, a) for l in _all_loops(procedure_ast.loops) for a in l.appends if a.target == loop.iter_source]
    if len(sites) != 1 or any(a.target == loop.iter_source for a in procedure_ast.top_appends):
        return None
    upstream = _row_source(sites[0][0], sites[0][1], procedure_ast, queries_by_var, top_level, seen)
    if upstream is None:
        return None
    q, up_filters, up_lines = upstream
    return q, up_filters + filters, up_lines | lines


def _count_only(
    procedure_ast: ProcedureAST, queries_by_var: Dict[str, QueryCall], top_level: Set[int]
) -> List[RewriteHint]:
    """A list that is filled only to count it with len().

    Example:
        cursor.execute("SELECT id, status FROM orders")
        done = []
        for row in cursor.fetchall():
            if row[1] == "done":
                done.append(row)
        print(len(done))
    Only the number is needed, so this becomes:
        SELECT COUNT(*) FROM orders WHERE status = 'done'
    The rows can also pass through several lists first (see _row_source)."""
    hints: List[RewriteHint] = []
    # Look for lists that are only used with len() and filled in exactly one place.
    for name, usage in procedure_ast.list_usage.items():
        if not usage.len_lines or usage.iter_lines or usage.other_lines:
            continue
        sites = [(l, a) for l in _all_loops(procedure_ast.loops) for a in l.appends if a.target == name]
        if len(sites) != 1 or any(a.target == name for a in procedure_ast.top_appends):
            continue
        source = _row_source(sites[0][0], sites[0][1], procedure_ast, queries_by_var, top_level, set())
        if source is None:
            continue
        q, filters, lines = source

        where: List[str] = []
        for c in list(q.where) + filters:
            if c.sql not in where:
                where.append(c.sql)
        sql = f"SELECT COUNT(*) FROM {q.table}" + (
            "\nWHERE " + "\n  AND ".join(where) if where else ""
        )
        count_var = f"{name}_count"
        indented = "\n".join("    " + l for l in sql.split("\n"))
        len_lines = ", ".join(str(l) for l in usage.len_lines)
        code = (
            f'{q.cursor}.execute("""\n{indented}\n""")\n'
            f"{count_var} = {q.cursor}.fetchone()[0]\n"
            f"# then use {count_var} in place of len({name}) (line {len_lines})"
        )
        caveats = sorted({a for c in filters for a in c.assumptions})
        caveats.append(
            "Other per-row work in the dropped loop(s) doesn't affect the count and is skipped; if it "
            "could raise (e.g. parsing a malformed date), the original would have crashed instead."
        )
        hints.append(RewriteHint(
            pattern="COUNT_IN_PYTHON",
            lineno=min(lines - {q.lineno}),
            explanation=(
                f"Fetches every row of {q.table}, filters and reshapes them in Python, only to take "
                f"len({name}) (line {len_lines}). SELECT COUNT(*) with the same filters in WHERE "
                "returns that number directly, without transferring any rows."
            ),
            sql=sql,
            optimized_code=code,
            caveats=caveats,
            lines=sorted(lines | set(usage.len_lines)),
        ))
    return hints


def build_rewrite_hints(procedure_ast: ProcedureAST) -> List[RewriteHint]:
    """Try every pattern on the code and return the faster versions found.

    Each outermost loop is checked against the patterns in turn; the first
    one that matches is used. Then lists used only with len() are checked,
    unless their lines were already covered by another rewrite."""
    loops = _all_loops(procedure_ast.loops)
    queries_by_var: Dict[str, QueryCall] = {}
    for q in procedure_ast.top_queries + [q for l in loops for q in l.queries]:
        if q.result_var:
            queries_by_var[q.result_var] = q
    # Queries that run once, outside all loops (compared by object identity).
    top_level = {id(q) for q in procedure_ast.top_queries}

    hints: List[RewriteHint] = []
    for loop in procedure_ast.loops:
        outer = queries_by_var.get(loop.iter_source)
        hint = (
            (
                _chain_aggregate(outer, loop, queries_by_var, top_level)
                or _chain_rows(outer, loop, queries_by_var, top_level)
                or _scalar_having(outer, loop)
                or _semi_join(outer, loop)
                or _membership_join(outer, loop, queries_by_var, top_level)
            )
            if outer is not None else _list_lookup(loop)
        )
        if hint:
            hints.append(hint)
    covered = {line for h in hints for line in h.lines}
    hints += [h for h in _count_only(procedure_ast, queries_by_var, top_level) if not covered & set(h.lines)]
    # Finally, look inside every query's SQL for `NOT IN (subquery)`.
    covered = {line for h in hints for line in h.lines}
    for q in procedure_ast.top_queries + [q for l in loops for q in l.queries]:
        if q.lineno not in covered:
            hint = _not_in_null(q)
            if hint:
                hints.append(hint)
    return hints
