"""
The data classes that hold Stage 1's results.

Stage 1 (ast_builder.py) reads a Python program and fills in these classes.
Together they describe what the program does with its database:

  - which SQL queries it runs              (QueryCall)
  - which loops it has, and their nesting  (LoopNode)
  - what each loop does with every row:
      * adds something to a list or dict   (AppendOp)
      * keeps a running total or counter   (AggregationOp)
      * only checks whether rows exist     (ExistsCheck)
  - ready-made faster SQL, when Stage 1
    could build it by itself              (RewriteHint)

These classes only *describe* the code. They don't decide whether the code
is slow -- that happens later.

A note on words used below:
  - "loop variable": the name after `for`, e.g. `row` in `for row in rows:`.
  - "column": a named field of a database table, e.g. `price`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Condition:
    """One Python `if` test, plus the same test written as SQL when possible.

    Example: the Python test `row["price"] > 10` becomes the SQL `price > 10`.
    """
    raw: str                                       # the Python test, as written in the code
    sql: Optional[str] = None                      # the SQL version, or None if it couldn't be translated
    bound_params: List[str] = field(default_factory=list)  # Python values that fill each `?` in `sql`
    assumptions: List[str] = field(default_factory=list)   # things that must be true for `sql` to match the Python
    # The SQL again, but with every column prefixed by the loop variable it
    # comes from, e.g. `item.price IS NOT NULL`. This matters inside nested
    # loops, where columns can come from different tables.
    qualified_sql: Optional[str] = None


@dataclass
class QueryCall:
    """One SQL query the program runs."""
    lineno: int                                    # line number in the source file
    cursor: str                                    # the object used to run it, e.g. "cursor" or "self.db"
    raw_sql: str                                   # the SQL text; Python values are shown as __PARAM_0__, __PARAM_1__, ...
    bound_params: Dict[str, str]                   # which Python value each __PARAM_n__ stands for
    table: Optional[str] = None                    # the table after FROM, if it could be found
    columns: List[str] = field(default_factory=list)       # the selected columns (["*"] for SELECT *)
    where: List[Condition] = field(default_factory=list)   # the parts of the WHERE clause
    has_limit: bool = False                        # True if the SQL has a LIMIT
    result_var: Optional[str] = None               # the variable that receives the rows, if any
    fetch_kind: Optional[str] = None               # how rows are read: "fetchall", "fetchone",
                                                   # "fetchone_scalar" (fetchone()[0]) or "direct"
    # Filled in after the whole program has been read:
    # the columns whose values are actually used somewhere in the code.
    used_columns: List[str] = field(default_factory=list)
    # For `SELECT *` queries only: when the code reads `row[i]`, the name the
    # code itself gives that value (e.g. the dict key it is stored under).
    # This is only a hint -- we can't know the real column name for `*`.
    used_indexes: Dict[int, str] = field(default_factory=dict)


@dataclass
class AppendOp:
    """Something a loop adds to a collection, e.g. `results.append({...})`,
    `names.add(x)`, `items.extend(x)` or `by_id[key] = value`."""
    lineno: int                                    # line number in the source file
    kind: str                                      # "append", "extend", "add" or "dict_assign"
    target: str                                    # the name of the list / set / dict being filled
    source_expr: Optional[str] = None              # what is added, when it isn't a {...} literal
    field_map: Dict[str, str] = field(default_factory=dict)  # for `append({"a": x, ...})`: key -> value
    key_expr: Optional[str] = None                 # for `by_id[key] = value`: the key
    # The `if` directly around this append (when the `if` contains nothing
    # else). For example `if total > 100: results.append(...)` stores the
    # test `total > 100`.
    guard: Optional["Condition"] = None
    # True when the append is hidden inside some other if/else or try block,
    # so it may not run exactly once for every row the loop keeps.
    in_branch: bool = False
    # Values in the {...} that could be written as SQL using the columns of
    # the rows being looped over (see value_sql.py). Key -> SQL expression.
    field_sql: Dict[str, str] = field(default_factory=dict)


@dataclass
class AggregationOp:
    """A running total, counter or flag that a loop builds up by hand.

    Examples:
        total = 0                       found = False
        for row in rows:                for row in rows:
            total += row["price"]           if row["x"] == 1:
                                                found = True
    In SQL these would be SUM(...), COUNT(...) or EXISTS(...).
    """
    lineno: int                                    # line where the value is added / set
    acc_var: str                                   # the variable being built up ("accumulator")
    init_value: str                                # its starting value, e.g. "0" or "False"
    op: str                                        # "add" (a sum or count) or "set_true" (a flag)
    operand: Optional[str] = None                  # what is added each time ("1" for a counter)
    condition: Optional[Condition] = None          # the `if` around it, if there is one
    init_lineno: Optional[int] = None              # line of the starting assignment, e.g. `total = 0`


@dataclass
class ExistsCheck:
    """A loop that runs a query for each row and only checks whether it
    found anything.

    Example:
        for c in customers:
            cursor.execute("SELECT ... FROM orders WHERE customer_id = ...")
            orders = cursor.fetchall()
            if not orders:          # "no rows"  -> negated=True
                ...
    `if not orders:`, `if orders is None:` and `if len(orders) == 0:` all
    mean "no rows" (negated=True). `if orders:` and similar mean "some
    rows" (negated=False).
    """
    query_var: str                                 # the variable holding the query result
    negated: bool                                  # True for "no rows", False for "some rows"
    raw: str                                       # the Python test, as written


@dataclass
class MembershipCheck:
    """A loop that keeps rows depending on whether they appear in another
    list of rows, using Python's `in` / `not in`.

    Example:
        for order in orders:
            if order not in orders_with_reviews:   # negated=True
                results.append(order)
    `x in some_list` compares x with every item of the list, so doing it
    for every row is very slow. In SQL this is a semi-join (`in`) or an
    anti-join (`not in`).
    """
    item: str                                      # what is looked for, e.g. "order" (the whole row)
    collection: str                                # the list it is looked for in, e.g. "orders_with_reviews"
    negated: bool                                  # True for `not in` (keep rows *without* a match)
    raw: str                                       # the Python test, as written


@dataclass
class LoopNode:
    """One `for` loop, with everything that happens inside it."""
    lineno: int                                    # line number of the `for`
    loop_var: str                                  # the name after `for`
    iter_source: str                               # what the loop goes over (after `in`)
    iterates_query_result: bool = False            # True if that is the result of a query
    # Tests that decide which rows the loop keeps ("filters"):
    #   - `if X: continue` skips rows where X is true, so it keeps "not X";
    #   - an `if X:` (no else) that contains all the loop's work.
    # An if/else that just computes a value for every row is not a filter.
    conditions: List[Condition] = field(default_factory=list)
    opaque_filters: List[str] = field(default_factory=list)  # filters that couldn't be written as SQL
    # Set when everything in the loop is inside one `if` with no else,
    # e.g. `if order["customer_id"] == customer["id"]:`.
    guard: Optional[Condition] = None
    # Set when the loop runs one COUNT/SUM-style query per row, reads the
    # number with `x = cursor.fetchone()[0]`, and then only continues
    # `if x > 2:` (or similar). Stored as SQL, e.g. `COUNT(*) > 2`.
    result_guard: Optional[Condition] = None
    exists_check: Optional[ExistsCheck] = None     # see ExistsCheck above
    membership: Optional[MembershipCheck] = None   # see MembershipCheck above
    queries: List[QueryCall] = field(default_factory=list)          # queries run inside this loop
    appends: List[AppendOp] = field(default_factory=list)           # things added to collections
    aggregations: List[AggregationOp] = field(default_factory=list)  # running totals / counters / flags
    nested_loops: List["LoopNode"] = field(default_factory=list)    # loops inside this loop


@dataclass
class FetchedTable:
    """Records that a variable holds rows from a certain table."""
    var_name: str
    table_name: str
    raw_sql: str
    lineno: int


@dataclass
class RewriteHint:
    """A faster replacement that Stage 1 built by itself (see rewrites.py).

    These are only created for code shapes that Stage 1 understands
    completely, so the SQL comes straight from the facts, not from the AI.
    """
    pattern: str                                   # the kind of problem, e.g. "N_PLUS_ONE"
    lineno: int                                    # the main line of the problem
    explanation: str                               # why the original code is slow
    sql: str                                       # the new SQL
    optimized_code: str                            # Python code to use instead
    caveats: List[str] = field(default_factory=list)  # small differences or assumptions to be aware of
    lines: List[int] = field(default_factory=list)    # the source lines this replaces


@dataclass
class ListUsage:
    """Where a list is used after it has been filled.

    For example, if a list is only ever used as `len(my_list)`, the code
    really only needs a count, and SQL COUNT(*) can give that directly.
    """
    len_lines: List[int] = field(default_factory=list)     # lines with len(x)
    iter_lines: List[int] = field(default_factory=list)    # lines with `for ... in x`
    other_lines: List[int] = field(default_factory=list)   # any other use (x[0], x.sort(), print(x), ...)


@dataclass
class ProcedureAST:
    """All of Stage 1's facts about one function (or one script)."""
    name: str                                      # the function name, or "<module>" for a script
    source: str                                    # the original source code
    top_queries: List[QueryCall] = field(default_factory=list)          # queries outside any loop
    top_appends: List[AppendOp] = field(default_factory=list)           # appends outside any loop
    top_aggregations: List[AggregationOp] = field(default_factory=list)  # totals outside any loop
    loops: List[LoopNode] = field(default_factory=list)                 # the outermost loops
    fetched_tables: Dict[str, FetchedTable] = field(default_factory=dict)  # variable -> table it holds rows of
    list_usage: Dict[str, ListUsage] = field(default_factory=dict)       # list name -> where it is used
    rewrite_hints: List[RewriteHint] = field(default_factory=list)       # faster versions built by Stage 1
