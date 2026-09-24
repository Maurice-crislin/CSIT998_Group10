# Code walkthrough

This document explains every file in the project, **in the order the code
runs**. Start at the top and read down: each section is the next file the
program uses.

The `logs/` and `tests/` folders are not covered. `logs/` only holds output
files, and `tests/` holds example programs to analyze.

If you have never seen some of the words used here, check the
[glossary](#glossary) at the end first.

---

## The example we will follow

To make things concrete, we follow one small program through every file.
Imagine it is saved as `example.py`:

```python
cursor.execute("SELECT order_id FROM orders WHERE status = 'delivered'")
orders = cursor.fetchall()
totals = []
for order in orders:
    cursor.execute(f"SELECT price FROM items WHERE order_id = '{order[0]}'")
    items = cursor.fetchall()
    total = 0
    for item in items:
        total += item[0]
    totals.append({"order_id": order[0], "total": total})
```

It is slow: for **every** order it runs one more query (line 5) and adds up
the prices in Python (line 9). With 10,000 orders, that is 10,001 queries.
This shape is called an **N+1 query**: 1 query for the orders, plus N more,
one per order.

By the end, the tool will have turned it into **one** query:

```sql
SELECT o.order_id AS order_id, COALESCE(SUM(i.price), 0) AS total
FROM orders o
LEFT JOIN items i ON i.order_id = o.order_id
WHERE o.status = 'delivered'
GROUP BY o.order_id
```

---

## The order in which files are used

```
 1. cli.py                        you start here: python cli.py example.py
 2. n1_optimizer/__init__.py      runs when cli.py imports the package
 3. n1_optimizer/pipeline.py      optimize_source(): runs everything below in order
 4. n1_optimizer/ollama_client.py    is the AI server running?
 5. n1_optimizer/ast_builder.py      Stage 1: read the code and record facts
 6. n1_optimizer/ast_model.py           the classes that hold those facts
 7. n1_optimizer/sql_facts.py           helper: read SQL, turn `if` tests into SQL
 8. n1_optimizer/value_sql.py           helper: turn computed values into SQL
 9. n1_optimizer/rewrites.py            build faster SQL for known patterns
10. n1_optimizer/ast_log.py          save the Stage 1 facts to a log file
11. n1_optimizer/llm_optimizer.py    Stage 2: ask the AI model, clean up its answer
12. n1_optimizer/llm_log.py             save the question sent to the model
    back in cli.py                  print the findings

Other files:
13. run_bad_code_tests.py          runs cli.py on many files, writes a CSV
14. README.md, requirements.txt, algorithm_diagram.dot/.png
```

The picture `algorithm_diagram.png` shows the same flow as a diagram.

---

## 1. `cli.py` — the starting point

**What it is:** a command-line program. "CLI" stands for *command-line
interface*: you run it in a terminal, and it prints the results.

**How you run it:**

```bash
python cli.py example.py
```

**What it does, step by step (inside `main()`):**

1. **Reads the options** you typed, using Python's `argparse` module. For
   example `--model llama3:latest` picks the AI model and `--timeout 600`
   waits longer for its answer. `--no-ollama` skips the AI completely.
   Each option has a `help=` text, shown by `python cli.py --help`.
2. **Reads the file.** `open(args.file).read()` gives the whole program as
   one string. With no file name, it reads from standard input instead
   (so `cat example.py | python cli.py` also works).
3. **Calls `optimize_source(...)`** from `pipeline.py`, passing the code and
   the options. That one call does all the real work (sections 3–12).
4. **Prints the result.** Which message you see depends on what happened:

   | Situation | What is printed |
   |---|---|
   | Ollama isn't running (or `--no-ollama`) | the Stage 1 facts as JSON, and a note that no AI was used |
   | the AI call failed (e.g. too slow) | `LLM call failed: ...` — and the program exits with code 1 |
   | the AI's answer wasn't valid JSON | the raw answer text |
   | no problems found | `No issues detected.` |
   | problems found | one block per finding: type, line, explanation, suggested code, caveats |

**The exit code** is the number `main()` returns: `0` means success, `1`
means the AI call failed. The last line, `raise SystemExit(main())`, passes
it back to the terminal.

**Why `if __name__ == "__main__":`?** Python sets `__name__` to
`"__main__"` only for the file you run directly. So `main()` runs when you
type `python cli.py`, but not when another file imports `cli.py`.

---

## 2. `n1_optimizer/__init__.py` — the package front door

**What it is:** the file Python runs when you import the folder
`n1_optimizer` as a *package*. The line in `cli.py`

```python
from n1_optimizer import optimize_source
```

runs this file first.

**What it does:** it imports the most useful functions and classes from the
other files, so users can write `from n1_optimizer import optimize_source`
instead of `from n1_optimizer.pipeline import optimize_source`. The list
`__all__` names everything the package offers this way.

Its docstring is a short overview of the whole project. It has no logic of
its own.

---

## 3. `n1_optimizer/pipeline.py` — runs the two stages in order

**What it is:** one function, `optimize_source()`, and one class,
`OptimizationResult`. This is the "manager" that calls every other part.

**`optimize_source(source, ...)` does four things, in this order:**

1. **Checks for the AI server.** It creates an `OllamaClient` (section 4)
   and calls `available()`. If the server answers, `used_ollama` becomes
   `True`.
2. **Stage 1:** calls `build_procedure_ast(source)` (section 5). This
   returns all the facts about the code, in an object called a
   `ProcedureAST`.
3. **Saves the facts** with `log_procedure_ast(...)` (section 10), unless
   logging is turned off.
4. **Stage 2:** calls `request_optimizations(...)` (section 11), which asks
   the AI model for suggestions. If there is no AI server, this returns
   `None`.

**`OptimizationResult`** is a *dataclass*: a simple class that just holds
values. It bundles everything the run produced:

| Field | Holds |
|---|---|
| `procedure_ast` | the Stage 1 facts |
| `used_ollama` | `True` if the AI server was reachable |
| `response` | the Stage 2 answer (or `None`) |
| `ast_log_path` | where the facts were saved (or `None`) |

`cli.py` receives this object and prints from it.

---

## 4. `n1_optimizer/ollama_client.py` — talking to the AI server

**What it is:** a small class that sends questions to
[Ollama](https://ollama.com), a program that runs AI models on your own
computer. Ollama listens on a web address, by default
`http://localhost:11434`, and we talk to it over HTTP using Python's
built-in `urllib` — no extra packages needed.

**Called first by:** `pipeline.py`, to check the server is there.

**The methods:**

- **`available()`** — asks `http://localhost:11434/api/tags` and waits at
  most 3 seconds. Returns `True` if it gets an answer.
- **`generate(prompt, system, json_mode)`** — sends a question and returns
  the answer text. It builds a dictionary (`payload`) with the model name,
  the question, the *system prompt* (instructions for the model) and
  `temperature: 0`. Temperature controls randomness; 0 means "always give
  the most likely answer", so the same question gives nearly the same
  answer every time. `json_mode=True` asks Ollama to answer in JSON only.
- **`generate_json(...)`** — calls `generate()` and turns the answer into a
  Python dictionary with `json.loads()`. If the model added extra words
  around the JSON, it tries again using only the text between the first
  `{` and the last `}`.

**Errors:** if the server can't be reached or doesn't answer within
`timeout` seconds, the methods raise `OllamaUnavailable`, a custom
exception. The message suggests a larger `--timeout`, because the first
request after Ollama starts must also load the model into memory, which can
take minutes.

---

## 5. `n1_optimizer/ast_builder.py` — Stage 1: reading the code

This is the biggest and most important file. It reads the program **without
running it** and records facts about its queries and loops.

### How can code read code?

Python's built-in `ast` module turns source text into an **AST** (abstract
syntax tree): a tree of objects, one per piece of the program. For our
example, line 4 (`for order in orders:`) becomes an `ast.For` object with:

- `target` → the name `order`
- `iter` → the name `orders`
- `body` → a list of the statements inside the loop

Line 5 becomes an `ast.Expr` holding an `ast.Call` whose function is
`cursor.execute`. By checking the *types* of these objects
(`isinstance(stmt, ast.For)`), the code can recognise loops, calls and
assignments.

### The entry point: `build_procedure_ast(source)`

1. **`ast.parse(source)`** — turns the text into a tree.
2. **Chooses which code to analyze.** If you passed `--func name`, that
   function. Otherwise the top-level script code if it has a `for` loop
   (our example does), else the first function in the file.
3. **Pre-scan (`_append_site_counts`)** — counts how many places add to
   each list. (Later, some rewrites are only safe if a list is filled in
   exactly one place.)
4. **Walks the statements** with `_Builder.walk_statements()` (below).
5. **Builds the `ProcedureAST`** object with everything found, and records
   where each list is used (`_list_usage`: in `len()`, in a `for` loop, or
   elsewhere).
6. **Post-pass (`_apply_used_column_facts`)** — works out which columns the
   code really reads. In our example: `order_id` from the first query and
   `price` from the second.
7. **Calls `build_rewrite_hints()`** from `rewrites.py` (section 9) to build
   faster SQL.

### The walk: `_Builder.walk_statements(stmts, aliases, loop_vars)`

It goes through the statements one by one. First `_flatten()` opens up every
`if` and `try` block, so statements hidden inside them are not missed. Then
each statement is checked:

| The statement looks like | What is recorded |
|---|---|
| `cursor.execute(SQL)` + next line `x = cursor.fetchall()` | a `QueryCall` (see `_build_query_call`) |
| `x = db.execute(SQL)` | a `QueryCall` too |
| `for x in y:` | a `LoopNode` — see below |
| `x.append(...)`, `x.add(...)`, `d[k] = v` | an `AppendOp` |
| `name = row[0]` | an *alias*: `name` now means `row[0]` |
| `total = 0` | remembered as the start of a possible running total |

**Getting the SQL text (`_extract_sql_and_params`).** The SQL may be a plain
string, an f-string, `"..." % values` or `"...".format(values)`. Values
pasted into it are replaced by markers. For line 5 of our example:

```
SQL:    SELECT price FROM items WHERE order_id = '__PARAM_0__'
params: {"__PARAM_0__": "order[0]"}
```

So we know the query depends on `order[0]`, a value from the outer loop.
That is exactly what makes it an N+1 query.

**Aliases.** Programs often write `price = item[0]` and then use `price`.
The dictionary `aliases` remembers `{"price": "item[0]"}`, so later code can
see that `price` really is `item[0]`.

### What happens at a `for` loop

When the walk reaches a `for` loop, it:

1. **Walks the loop body first**, by calling `walk_statements()` again on
   it. A function calling itself like this is called *recursion*; it is how
   loops inside loops, to any depth, are handled.
2. **Then looks at the loop as a whole:**
   - **Filters** (`_filter_tests`): `if X: continue` means "skip rows where
     X is true", so the rows kept are "not X". An `if` that wraps all of the
     loop's work is also a filter. An if/else that only computes a value is
     *not* a filter.
   - **Running totals, counters and flags** (`_extract_loop_aggregation`):
     `total += item[0]` after `total = 0` is a manual SUM. `count += 1` is a
     manual COUNT. `found = True` inside an `if` is a manual EXISTS check.
   - **Guards on appends** (`_attach_append_guards`): an `if` directly
     around an append, like `if total > 100: results.append(...)`.
   - **Special shapes:** a query per row that is only checked for "any
     rows?" (`_exists_check`), or a `COUNT(*)` per row that is then compared
     with a number (`_result_guard`).
   - **Values as SQL** (`_record_field_sql`, using `value_sql.py`): which
     dictionary values in an append can be written as SQL.
3. **Stores it all** in a new `LoopNode`.

**Our example after Stage 1** (shortened; this is real output):

```
top_queries:  SELECT order_id FROM orders WHERE status = 'delivered'   → variable "orders"
loop (line 4): for order in orders
    query (line 5): SELECT price FROM items WHERE order_id = '__PARAM_0__'   (__PARAM_0__ = order[0])
    append (line 10): totals.append({"order_id": order[0], "total": total})
    nested loop (line 8): for item in items
        aggregation (line 9): total += item[0]   (started as total = 0 on line 7)
```

---

## 6. `n1_optimizer/ast_model.py` — the classes that hold the facts

**What it is:** only *dataclasses* — classes that store values and have no
logic. `ast_builder.py` creates these objects; every later file reads them.

| Class | Describes | In our example |
|---|---|---|
| `QueryCall` | one SQL query: its text, table, columns, WHERE parts, pasted-in values, the variable it's stored in | the two `SELECT`s |
| `LoopNode` | one `for` loop: what it goes over, its filters, queries, appends, totals, and loops inside it | the loops on lines 4 and 8 |
| `AppendOp` | one "add to a collection" | `totals.append(...)` |
| `AggregationOp` | one running total / counter / flag | `total += item[0]` |
| `Condition` | one `if` test, as Python text and (if possible) as SQL | `status = 'delivered'` |
| `ExistsCheck` | a loop that only checks "did the query find anything?" | — |
| `ListUsage` | where a list is used: `len()`, `for`, or other | `totals` (not used later) |
| `FetchedTable` | "this variable holds rows of that table" | `orders` → `orders` |
| `RewriteHint` | a faster version built by Stage 1 | the JOIN query at the top |
| `ProcedureAST` | the whole collection of facts for one program | — |

`dataclasses.asdict()` can turn any of these into a plain dictionary, which
is how they become JSON for the log file and for the AI model.

A detail you will see often: `field(default_factory=list)`. It gives each
new object its own empty list. (Writing `= []` directly would make all
objects share one list — a classic Python mistake.)

---

## 7. `n1_optimizer/sql_facts.py` — reading SQL, turning `if` tests into SQL

**Called by:** `ast_builder.py`, while it walks the code.

**`parse_select(sql)`** splits a simple query into its parts using
*regular expressions* (a small pattern language for searching text):

```
"SELECT order_id FROM orders WHERE status = 'delivered'"
  → table "orders",  columns ["order_id"],  where ["status = 'delivered'"]
```

It only understands `SELECT ... FROM one_table [WHERE ...] [LIMIT n]`. For
anything more complex it returns no table, but the SQL text is kept.

**`has_limit(sql)`** — `True` if the SQL has a `LIMIT`.

**`resolve_row_field(node, loop_vars, aliases, columns)`** — answers "which
column does this expression read?" It understands `row.price`,
`row["price"]` and `row[0]`. For `row[0]` it needs the query's column list:
in `SELECT order_id FROM orders`, position 0 is `order_id`. It also follows
aliases, so after `price = item[0]`, the name `price` resolves to the same
column.

**`condition_to_sql(node, ...)`** — turns a Python `if` test into SQL:

| Python | SQL |
|---|---|
| `row["price"] > 10` | `price > 10` |
| `row["city"] == name` | `city = ?` (with `name` as a parameter) |
| `row["date"] is None` | `date IS NULL` |
| `a["id"] == b["a_id"]` | `a.id = b.a_id` (matches rows of two loops — a hand-made JOIN) |
| `a > 1 and b < 2` | `(a > 1) AND (b < 2)` |

It returns `None` if the test can't be translated. Each result is a
`Condition` holding the Python text, the SQL, and a `qualified_sql` version
where every column has its loop variable in front (`item.price`), so it is
clear which table it belongs to.

---

## 8. `n1_optimizer/value_sql.py` — turning computed values into SQL

**Called by:** `ast_builder.py` (in `_record_field_sql`).

Loops often compute a value before using it:

```python
if row[2] is not None:
    finished_late = end > start
else:
    finished_late = False
results.append({"finished_late": finished_late})
```

To move this work into the database, `finished_late` must be written as SQL.

**`track_loop_values(body, translator, on_append)`** reads the loop body from
top to bottom and keeps a dictionary called `env`: *variable name → its value
as SQL*. Each assignment updates `env`. An if/else becomes a SQL
`CASE WHEN ... THEN ... ELSE ... END`. At every `.append(...)`, it calls
`on_append` with the current `env`, so the builder can record each value's
SQL.

**`ValueTranslator.expr(node, env)`** translates one expression. It
understands constants, comparisons, `and`/`or`, `is None`, and
`datetime.strptime(...)` dates. For anything else — arithmetic, method
calls, `+=` — it returns `None` ("unknown") instead of guessing.

Each SQL value also has a **kind**: `"value"` (a normal value), `"datetime"`
(a date read with `strptime`), or `"bool"` (true/false). The kind stops
mistakes like comparing a date with a number.

In our example it records `order_id` as SQL for the `"order_id"` key. The
`"total"` key is a running total, so it is handled by the aggregation facts
instead.

---

## 9. `n1_optimizer/rewrites.py` — building faster SQL

**Called by:** `build_procedure_ast()`, as its last step.

**Why it exists:** some fixes need several SQL ideas at once — our example
needs a JOIN, `SUM()` and `GROUP BY`. AI models often get that wrong. But
Stage 1 already has every piece: the tables, the columns, the value that
links the rows (`order[0]`), and what is added up (`item[0]` → `price`). So
for known patterns the SQL is built here, in code.

**`build_rewrite_hints(procedure_ast)`** tries each pattern on each
outermost loop. The first one that matches wins:

| Function | Pattern in the Python code | Becomes |
|---|---|---|
| `_chain_aggregate` | nested loops with a running total | `JOIN` + `SUM`/`COUNT` + `GROUP BY` (+ `HAVING`) |
| `_chain_rows` | nested loops appending each matching pair | one `JOIN` query |
| `_scalar_having` | `SELECT COUNT(*)` per row, then `if n > N:` | `GROUP BY ... HAVING COUNT(*) > N` |
| `_semi_join` | a query per row, then only `if not rows:` / `if rows:` | `WHERE NOT EXISTS (...)` / `WHERE EXISTS (...)` |
| `_list_lookup` | a loop over a Python list of ids, one query per id | `WHERE id IN (...)` |
| `_count_only` | a list that is only used with `len()` | `SELECT COUNT(*) ... WHERE ...` |

If the code differs even slightly from a pattern, the function returns
`None` and the problem is left to the AI. It never guesses.

**Our example matches `_chain_aggregate`.** The steps:

1. `_loop_chain` lists the nested loops: `[loop on line 4, loop on line 8]`.
2. `_join_chain` turns each inner loop into a JOIN. The inner query's
   `WHERE order_id = '{order[0]}'` becomes `ON i.order_id = o.order_id`.
   `_new_alias` makes short table names: `orders` → `o`, `items` → `i`.
3. The running total `total += item[0]` becomes `SUM(i.price)`.
4. The appended dictionary `{"order_id": ..., "total": ...}` becomes the
   `SELECT` list, and `GROUP BY o.order_id` gives one group per order.
5. `LEFT JOIN` and `COALESCE(..., 0)` keep orders that have no items, with a
   total of 0 — exactly what the Python loop does. (`COALESCE(x, 0)` means
   "x, or 0 if x is empty".)

The result is a `RewriteHint` with the SQL, ready-to-use Python code, an
explanation, **caveats** (differences to be aware of, for example that row
order isn't guaranteed), and the source lines it replaces (4, 5, 7, 8, 9, 10).

---

## 10. `n1_optimizer/ast_log.py` — saving the facts

**Called by:** `pipeline.py`, right after Stage 1 (unless you used
`--no-ast-log`).

`log_procedure_ast()` adds one line to `logs/procedure_ast.jsonl`. The file
format is **JSONL**: one complete JSON object per line, so new runs can
simply be added to the end. Each line holds the time (in UTC), the function
name and all the facts. This lets you look at old results without running
the tool again.

---

## 11. `n1_optimizer/llm_optimizer.py` — Stage 2: asking the AI model

**Called by:** `pipeline.py`, after the facts are saved.

**`request_optimizations(procedure_ast, ollama_client)`:**

1. **Builds the question (prompt).** It contains the original source code
   and the Stage 1 facts as JSON (`_procedure_ast_to_json`).
2. **Saves the question** with `log_llm_request()` (section 12) *before*
   sending it, so it is kept even if the model never answers.
3. **Sends it** with `generate_json()`, together with `_SYSTEM_PROMPT`. The
   system prompt tells the model which problems to look for (N+1 queries,
   manual joins, totals in Python, ...), ties each problem to a fact in the
   JSON, and describes the exact JSON format to answer in.
4. **Handles problems:** if the model can't be reached, it returns a
   response with an `error`. If the answer isn't valid JSON, it asks again
   for plain text and returns that as `raw_text`.
5. **Reads the answer** (`_parse_findings`) into `OptimizationFinding`
   objects, skipping empty entries and filling in safe defaults.
6. **Removes findings the facts contradict** (`_drop_unsupported`). For
   example, an "N+1" finding is dropped if Stage 1 found no query inside any
   loop. Stage 1 reads the code exactly, so its facts win.
7. **Adds Stage 1's own fixes** (`_merge_rewrite_hints`). Each rewrite hint
   becomes a finding, and any AI finding about the same lines is removed.

**The result** is an `LLMOptimizationResponse` with a list of `findings`
(or `error` / `raw_text` if something went wrong).

In our example, the AI may find the N+1 problem too, but because Stage 1
built a rewrite for lines 4–10, **the Stage 1 version is what you see**.

---

## 12. `n1_optimizer/llm_log.py` — saving the question sent to the model

**Called by:** `llm_optimizer.py`, just before contacting the model (unless
you used `--no-llm-log`).

`log_llm_request()` adds one line to `logs/llm_requests.jsonl` with the
time, the model name, the server address, the system prompt and the full
question. When an answer looks strange, this shows exactly what the model
was asked.

### Back in `cli.py`

`optimize_source()` now returns, and `cli.py` prints the findings. For our
example:

```
--- Finding 1: N_PLUS_ONE (line 5, severity: high) ---
Walks orders -> items with a query per row and sums the innermost rows in Python
(line 9), once per orders row. A single query with JOINs and GROUP BY returns the
same rows.

Suggested code:
cursor.execute("""
    SELECT o.order_id AS order_id, COALESCE(SUM(i.price), 0) AS total
    FROM orders o
    LEFT JOIN items i ON i.order_id = o.order_id
    WHERE o.status = 'delivered'
    GROUP BY o.order_id
""")
totals = [{"order_id": r[0], "total": r[1]} for r in cursor.fetchall()]

Caveats:
- Assumes o.order_id identifies one row of orders's result (true if it includes
  the primary key); otherwise GROUP BY merges rows the loop kept separate.
- LEFT JOIN + COALESCE keep outer rows with no matches, with 0, exactly as the
  loop does.
- Row order is unspecified in both versions; add ORDER BY if the order matters.
```

That is the whole journey of one run.

---

## 13. `run_bad_code_tests.py` — testing many files at once

**What it is:** a second starting point, used instead of `cli.py` when you
want to check all the example programs in `tests/bad_code/` in one go.

```bash
python run_bad_code_tests.py
```

**What it does:**

1. **Finds the files** with `Path(...).glob("*.py")` and sorts them
   "naturally" (`_natural_key`), so `bad_code_2.py` comes before
   `bad_code_10.py`.
2. **Runs `cli.py` once per file** as a separate program, using the
   `subprocess` module (`run_one`). It captures everything `cli.py` prints.
3. **Splits the printed text into columns** (`_parse_findings`), using a
   regular expression that matches each finding's heading line
   (`--- Finding 1: N_PLUS_ONE (line 5, severity: high) ---`).
4. **Decides a status** for files without findings: `no_issues`,
   `llm_failed`, `no_llm`, `raw_text` or `error`.
5. **Writes a CSV file** (`bad_code_results.csv`) with `csv.DictWriter`, one
   row per finding. It uses the `utf-8-sig` encoding so Excel shows accented
   letters correctly.

Options include `--out` (another CSV name), `--model`, `--timeout`,
`--pattern` (which files) and `--keep-logs`.

---

## 14. The other files

| File | What it is |
|---|---|
| `README.md` | the project overview: what the tool does, how to install and run it |
| `requirements.txt` | normally lists packages to install with `pip`; here it only says that none are needed |
| `algorithm_diagram.png` | a picture of the whole flow described in this document |
| `algorithm_diagram.dot` | the source of that picture, written for Graphviz. Change it, then run `dot -Tpng -Gdpi=150 algorithm_diagram.dot -o algorithm_diagram.png` |
---

## Glossary

| Word | Meaning |
|---|---|
| **AST** | abstract syntax tree: Python's tree-of-objects form of source code, made by `ast.parse()` |
| **alias** | here: a short name for a row value, like `price = item[0]`. In SQL: a short table name, like `orders o` |
| **CLI** | command-line interface: a program you run by typing in a terminal |
| **dataclass** | a class that mainly stores values; Python writes `__init__` for you (`@dataclass`) |
| **fetchall / fetchone** | read all rows / one row of a query's result |
| **GROUP BY** | SQL: put rows into groups (e.g. one per order) so SUM/COUNT work per group |
| **HAVING** | SQL: a filter applied to groups, after SUM/COUNT are calculated |
| **JOIN / LEFT JOIN** | SQL: combine matching rows of two tables. LEFT JOIN also keeps rows with no match |
| **EXISTS / NOT EXISTS** | SQL: true if a subquery finds at least one row / finds none |
| **JSON / JSONL** | a text format for data (`{"key": "value"}`) / one JSON object per line |
| **LLM** | large language model: the AI model that writes the Stage 2 suggestions |
| **N+1 query** | 1 query for a list of rows, then N more queries, one per row |
| **Ollama** | a program that runs LLMs on your own computer |
| **prompt / system prompt** | the question sent to the model / the standing instructions on how to answer |
| **recursion** | a function calling itself, e.g. to handle loops inside loops |
| **regular expression (regex)** | a pattern for finding text, e.g. `LIMIT\s+\d+` |
| **rewrite hint** | a faster version of the code built by Stage 1 itself |
| **Stage 1 / Stage 2** | reading the code without AI / asking the AI model |
