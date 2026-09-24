# n1-optimizer

Finds slow database code in Python programs and suggests faster versions.

Many programs fetch rows with SQL and then do the rest of the work in Python
loops. That is often much slower than letting the database do it. This tool
reads such a program (it never runs it) and points out problems like:

| Problem | What the code does | Faster version |
|---|---|---|
| **N+1 queries** | runs one query for every row of another query | one query with a `JOIN` |
| **Manual join** | matches rows of two tables with nested `for` loops | one query with a `JOIN` |
| **Totals in Python** | adds up or counts rows in a loop | `SUM()` / `COUNT()` with `GROUP BY` |
| **Filtering in Python** | fetches every row, then skips most with an `if` | a `WHERE` clause |
| **Counting a list** | builds a list only to call `len()` on it | `SELECT COUNT(*)` |
| **Existence check per row** | runs a query per row only to see if it finds anything | `WHERE [NOT] EXISTS (...)` |

## How it works

The tool works in two stages:

```
your Python file
      │
      ▼
Stage 1: ast_builder.py   reads the code and writes down facts (no AI)
      │                     - which SQL queries run, and on which tables
      │                     - which loops go over which rows, and how they nest
      │                     - what each loop does: filter, add up, count, append
      │                   and, for patterns it fully understands, builds the
      │                   faster SQL itself (rewrites.py)
      ▼
Stage 2: llm_optimizer.py sends the code + the facts to a local AI model
                          (through Ollama) and collects its suggestions
      │
      ▼
findings: problem type, line, explanation, suggested code, caveats
```

**Stage 1** uses Python's built-in `ast` module, which turns source code into
a tree of objects (an "abstract syntax tree"). Because no AI is involved, its
facts are exact. For six common loop patterns it also builds the fixed SQL
itself (see [Built-in rewrites](#built-in-rewrites)).

**Stage 2** asks an AI model (an LLM) to find problems and suggest fixes.
The Stage 1 facts are used to clean up the answer:

- a finding that the facts show can't be right is removed (for example "N+1"
  when there is no query inside any loop);
- a fix built by Stage 1 replaces whatever the model said about the same lines.

> Suggestions from the AI model are not checked by any code and can be
> wrong. Always review a suggestion before using it.

## Requirements

- **Python 3.9 or newer.** Only the standard library is used; there is
  nothing to `pip install`.
- **[Ollama](https://ollama.com)** (optional) to run the AI model for
  Stage 2. The default model is `llama3:latest`:

  ```bash
  ollama pull llama3
  ```

  Without Ollama, the tool still runs Stage 1 and prints its facts.

The tool only reads code; it never connects to a database. The example
programs in `tests/bad_code/` use the
[Olist e-commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
stored in `tests/bad_code/olist.db`, but that file is only needed if you want
to *run* those examples yourself. They open it as `olist.db`, so run them from
inside that folder:

```bash
cd tests/bad_code
python bad_code_4.py
```

## Usage

Analyze one file:

```bash
python cli.py tests/bad_code/bad_code_4.py
```

Useful options:

```bash
python cli.py FILE --func my_function     # only analyze this function
python cli.py FILE --no-ollama            # Stage 1 only (no AI)
python cli.py FILE --show-ast             # also print the Stage 1 facts
python cli.py FILE --model llama3.2:3b    # use a different Ollama model
python cli.py FILE --timeout 600          # wait up to 600 s for the model (default 300)
python cli.py FILE --host http://localhost:11434
cat FILE | python cli.py                  # read the code from standard input
```

The first request after Ollama starts can be slow, because the model has to
be loaded into memory. If you see `LLM call failed: ... did not answer within
300s`, try a larger `--timeout`.

### Test all example files at once

`run_bad_code_tests.py` runs `cli.py` on every file in `tests/bad_code/` and
saves the results in a CSV file (one row per finding) that opens in Excel:

```bash
python run_bad_code_tests.py                          # writes bad_code_results.csv
python run_bad_code_tests.py --out results/run1.csv
python run_bad_code_tests.py --model llama3.2:3b --timeout 600
python run_bad_code_tests.py --pattern "bad_code_1*.py"
```

The `status` column says what happened for each file: `findings`,
`no_issues`, `llm_failed` (the model didn't answer), `no_llm` (Ollama not
running), `raw_text` (the model's answer wasn't valid JSON) or `error`.

## Example

Input (`tests/bad_code/bad_code_4.py`) -- one extra query for *every*
delivered order, then adding up the prices in Python:

```python
cursor.execute("""
    SELECT order_id
    FROM olist_orders_dataset
    WHERE order_status = 'delivered'
""")
orders = cursor.fetchall()
order_totals = []

for order in orders:
    order_id = order[0]
    cursor.execute(f"""
        SELECT price
        FROM olist_order_items_dataset
        WHERE order_id = '{order_id}'
    """)
    items = cursor.fetchall()
    total = 0
    for item in items:
        price = item[0]
        total = total + price
    order_totals.append({"order_id": order_id, "total": total})
```

Output:

```
--- Finding 1: N_PLUS_ONE (line 18, severity: high) ---
Walks olist_orders_dataset -> olist_order_items_dataset with a query per row and sums
the innermost rows in Python (line 29), once per olist_orders_dataset row. A single
query with JOINs and GROUP BY returns the same rows.

Suggested code:
cursor.execute("""
    SELECT o.order_id AS order_id, COALESCE(SUM(oi.price), 0) AS total
    FROM olist_orders_dataset o
    LEFT JOIN olist_order_items_dataset oi ON oi.order_id = o.order_id
    WHERE o.order_status = 'delivered'
    GROUP BY o.order_id
""")
order_totals = [{"order_id": r[0], "total": r[1]} for r in cursor.fetchall()]

Caveats:
- Assumes o.order_id identifies one row of olist_orders_dataset's result (true if it
  includes the primary key); otherwise GROUP BY merges rows the loop kept separate.
- LEFT JOIN + COALESCE keep outer rows with no matches, with 0, exactly as the loop does.
- Row order is unspecified in both versions; add ORDER BY if the order matters.
```

This fix was built by Stage 1, so it is the same every time. Findings that
come from the AI model are worded differently from run to run.

## Built-in rewrites

Stage 1 builds the fix itself when the code matches one of these patterns
exactly (`n1_optimizer/rewrites.py`). If the code differs even slightly, it
leaves the problem to the AI model instead of guessing.

| Pattern in the Python code | Becomes |
|---|---|
| Nested loops (a query per row, or `if a[0] != b[0]: continue` matching) with a running total or counter | `JOIN` + `SUM`/`COUNT` + `GROUP BY` (+ `HAVING` when the total is filtered afterwards) |
| Nested loops that append one result per matching pair of rows | one `JOIN` query |
| A `SELECT COUNT(*)` per row, followed by `if count > N:` | `GROUP BY ... HAVING COUNT(*) > N` |
| A query per row, followed only by `if not rows:` / `if rows:` | `WHERE NOT EXISTS (...)` / `WHERE EXISTS (...)` |
| A list built only to call `len()` on it (also through several lists) | `SELECT COUNT(*) ... WHERE <all the filters>` |
| A loop over a Python list of ids, one `fetchone()` query per id | one query with `WHERE id IN (...)` |

Each of these was checked by running the original code and the rewritten
code on the same test data and comparing the results. Any real difference is
listed in the finding's caveats (for example, when row order or duplicate
rows could differ).

## What Stage 1 understands

- **Scripts and functions.** Top-level script code and function bodies both
  work. Use `--func name` to pick a function; otherwise the top-level code is
  used if it has a `for` loop, else the first function in the file.
- **Two ways of running a query:**
  `rows = db.execute(SQL)`, or `cursor.execute(SQL)` followed by
  `rows = cursor.fetchall()`, `cursor.fetchone()` or `cursor.fetchone()[0]`.
- **Three ways of reading a row value:** `row.name`, `row["name"]` and
  `row[0]` -- also through a short name such as `name = row[1]`.
- **Four ways of building the SQL text:** a normal string, an f-string,
  `"..." % values` and `"...".format(values)`.
- **Loops inside loops**, to any depth. For each loop it records the queries
  inside it, which rows it keeps (`if ...: continue`, or an `if` around all
  its work), running totals, counters and flags (also inside nested `if`s),
  and what it appends to lists, sets or dicts.
- **Values computed in a loop** from simple comparisons, `and`/`or`,
  `is None`, and `datetime.strptime()` dates can be turned into SQL
  (`n1_optimizer/value_sql.py`).
- **SQL text** is fully understood only for single-table
  `SELECT ... FROM table WHERE ... [LIMIT n]`. Other SQL (JOINs, subqueries,
  `OR` inside the SQL) is kept as text and still sent to the AI model.

## Logs

Both stages save a copy of each run in the `logs/` folder, as JSONL files
(one JSON object per line):

| File | What it contains | Turn off | Other location |
|---|---|---|---|
| `logs/procedure_ast.jsonl` | the Stage 1 facts for each run | `--no-ast-log` | `--ast-log-path` |
| `logs/llm_requests.jsonl` | the exact question sent to the model (saved *before* sending, so it is kept even if the model never answers) | `--no-llm-log` | `--llm-log-path` |

## Using it from Python

```python
from n1_optimizer import optimize_source

result = optimize_source(open("tests/bad_code/bad_code_4.py").read())

print(result.procedure_ast)            # the Stage 1 facts
if result.response:                    # None when Ollama isn't running
    for finding in result.response.findings:
        print(finding.pattern, finding.lineno)
        print(finding.explanation)
        print(finding.optimized_code)
```

Or run the two stages yourself:

```python
from n1_optimizer import build_procedure_ast, request_optimizations
from n1_optimizer.ollama_client import OllamaClient

facts = build_procedure_ast(open("tests/bad_code/bad_code_4.py").read())
for hint in facts.rewrite_hints:       # fixes Stage 1 built by itself
    print(hint.optimized_code)

response = request_optimizations(facts, OllamaClient(model="llama3:latest"))
```

## Project layout

```
cli.py                   command-line tool
run_bad_code_tests.py    runs cli.py on every file in tests/bad_code, writes a CSV
n1_optimizer/
    pipeline.py          runs Stage 1 then Stage 2 (optimize_source)
    ast_builder.py       Stage 1: reads the code and records the facts
    ast_model.py         the classes that hold the facts
    sql_facts.py         reads SQL text; turns Python `if` tests into SQL
    value_sql.py         turns values computed in a loop into SQL
    rewrites.py          builds the fixes for the patterns listed above
    llm_optimizer.py     Stage 2: asks the AI model and cleans up its answer
    ollama_client.py     talks to the Ollama server
    ast_log.py           saves the Stage 1 facts to logs/
    llm_log.py           saves the questions sent to the model to logs/
tests/
    bad_code/            example programs with slow database code (Olist data)
    query_optimizer_patterns/, *.py   more small example inputs
```
