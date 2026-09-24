"""
Stage 2: ask the AI model (LLM) for suggestions.

Steps:
  1. Turn the Stage 1 facts (a ProcedureAST) into JSON text.
  2. Send the original code plus that JSON to the model, together with a
     "system prompt" (_SYSTEM_PROMPT below) that explains what to look for
     and what format to answer in.
  3. Read the model's JSON answer into OptimizationFinding objects.
  4. Clean up the answer:
       - drop findings that the Stage 1 facts show can't be right,
       - add the faster versions Stage 1 already built (rewrite hints),
         which replace what the model said about the same lines.

The model's own suggestions are not checked by any code, so review them
before using them.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from .ast_model import LoopNode, ProcedureAST
from .llm_log import DEFAULT_LOG_PATH as DEFAULT_REQUEST_LOG_PATH
from .llm_log import log_llm_request
from .ollama_client import OllamaClient, OllamaUnavailable

# The instructions sent to the model with every request. They list the
# problems to look for, tie each one to a fact in the Stage 1 JSON, and
# describe the exact JSON format the answer must use.
_SYSTEM_PROMPT = (
    "You are a database performance expert reviewing Python code that talks to a SQL "
    "database. You get the source code plus JSON facts extracted from it. Your goal: move "
    "as much work as possible from Python into SQL, so the database returns only the rows "
    "and values the program actually needs.\n\n"
    "Report every one of these that occurs (a procedure can have several). Each pattern "
    "is tied to a fact in the JSON -- only report it when that fact is present:\n"
    "1. PYTHON_FILTER -- requires a loop with non-empty `conditions`: a loop over a query "
    "result with an `if` that keeps only some rows. "
    "The loop's `conditions[].sql` is the exact SQL equivalent of that `if`. Fix: add it to "
    "the WHERE clause of the query the loop iterates (`loop.iter_source` == the query's "
    "`result_var`), and remove the Python `if`.\n"
    "2. COUNT_IN_PYTHON / SUM_IN_PYTHON -- a list built only to call len() on it, or a loop "
    "with non-empty `aggregations` (op \"add\" = manual SUM/COUNT, op \"set_true\" = manual "
    "existence check). Fix: SELECT COUNT(*) / SUM(col) / EXISTS ... WHERE ... and "
    "fetchone()[0].\n"
    "3. N_PLUS_ONE -- requires a loop with non-empty `queries`: a query executed inside a "
    "loop, once per row. Fix: one query with JOIN or IN, keeping every column the loop used.\n"
    "4. MANUAL_JOIN -- requires non-empty `nested_loops`: nested loops matching rows of two "
    "fetched tables. A condition whose `sql` compares two loop variables' fields (e.g. "
    "`order.customer_id = customer.customer_id`) is that join's ON clause, not a WHERE "
    "filter. Fix: SQL JOIN.\n"
    "5. EXECUTEMANY -- row-by-row INSERT/UPDATE/DELETE in a loop. Fix: executemany().\n"
    "6. OVER_SELECT -- a query selecting columns it never uses. Each query's `used_columns` "
    "lists the columns confirmed to be read from its result; for `SELECT *`, `used_indexes` "
    "gives the name the source itself uses for a positional `row[i]` (a strong hint, not a "
    "verified column name). Fix: select exactly the needed columns by name.\n"
    "7. UNBOUNDED_SELECT -- SELECT * with no WHERE/LIMIT feeding a large in-memory loop.\n"
    "`rewrite_hints` in the JSON are fixes already assembled mechanically from these facts. "
    "When one is present, reuse its optimized_code verbatim for that finding and spend your "
    "effort on anything else in the code.\n"
    "\n"
    "Rules for optimized_code:\n"
    "- It must be complete, runnable Python that replaces the original query AND loop, "
    "producing the same final output.\n"
    "- Use only tables and columns that appear in the source's SQL. Python variables "
    "(lists, dicts, locals) are not tables.\n"
    "- Do not keep a Python `if` that the new WHERE clause already handles.\n"
    "- Write real SQL with real table and column names -- never `...` placeholders.\n\n"
    "Return strict JSON of this exact shape:\n"
    '{"findings": [{"pattern": "<one of the names above>", "lineno": <int>, '
    '"severity": "high"|"medium"|"low", "explanation": "<why this is inefficient>", '
    '"optimized_code": "<replacement code>", '
    '"caveats": ["<semantic differences or assumptions>"]}]}\n'
    'If nothing looks inefficient, return exactly {"findings": []}. Only output JSON, nothing else.'
)


@dataclass
class OptimizationFinding:
    """One problem found in the code, with a suggested fix."""
    pattern: str                  # the kind of problem, e.g. "N_PLUS_ONE"
    lineno: int                   # the line it's on (0 if unknown)
    severity: str                 # "high", "medium" or "low"
    explanation: str              # why it's slow
    optimized_code: str           # the suggested replacement code
    caveats: List[str] = field(default_factory=list)  # things to be aware of before using the fix


@dataclass
class LLMOptimizationResponse:
    """The result of Stage 2. Normally only `findings` is filled in."""
    findings: List[OptimizationFinding] = field(default_factory=list)
    raw_text: Optional[str] = None          # the model's answer, when it wasn't valid JSON
    request_log_path: Optional[Path] = None  # where the question sent to the model was saved
    error: Optional[str] = None             # an error message, if the model couldn't be reached


def _procedure_ast_to_json(procedure_ast: ProcedureAST) -> str:
    """The Stage 1 facts as JSON text (without the source code)."""
    data = dataclasses.asdict(procedure_ast)
    data.pop("source", None)  # the source code is sent separately, so leave it out here
    return json.dumps(data, indent=2, default=str)


def _parse_findings(data: dict) -> List[OptimizationFinding]:
    """Turn the model's JSON answer into OptimizationFinding objects.

    Missing or badly formed values get safe defaults instead of crashing."""
    findings = []
    for f in data.get("findings", []) or []:
        if not isinstance(f, dict):
            continue
        # Skip empty entries like `{}` that contain no real finding.
        if not (f.get("explanation") or f.get("optimized_code")):
            continue
        try:
            lineno = int(f.get("lineno") or 0)
        except (TypeError, ValueError):
            lineno = 0
        findings.append(
            OptimizationFinding(
                pattern=str(f.get("pattern", "UNKNOWN")),
                lineno=lineno,
                severity=str(f.get("severity", "medium")),
                explanation=str(f.get("explanation", "")),
                optimized_code=str(f.get("optimized_code", "")),
                caveats=[str(c) for c in (f.get("caveats") or []) if c],
            )
        )
    return findings


def _all_loops(loops: List[LoopNode]) -> List[LoopNode]:
    """Every loop, including loops inside other loops, in one flat list."""
    out: List[LoopNode] = []
    for loop in loops:
        out.append(loop)
        out.extend(_all_loops(loop.nested_loops))
    return out


_JOIN_CONDITION_RE = re.compile(r"^\s*\w+\.\w+\s*(?:=|!=|<>|<=|>=|<|>)\s*\w+\.\w+\s*$")


def _is_join_condition(sql: Optional[str]) -> bool:
    """True for a test that compares rows of two loops, like `a.id = b.a_id`.
    Such a test matches rows together (a join); it doesn't filter them."""
    return bool(sql and _JOIN_CONDITION_RE.match(sql))


def _drop_unsupported(findings: List[OptimizationFinding], procedure_ast: ProcedureAST) -> List[OptimizationFinding]:
    """Remove findings that the Stage 1 facts show can't be right.

    Some problems need a certain fact to exist. For example, an N+1 problem
    needs a query inside a loop. If the model reports N_PLUS_ONE but Stage 1
    found no query inside any loop, the finding is dropped. Stage 1 reads
    the code exactly, so its facts win over the model."""
    loops = _all_loops(procedure_ast.loops)
    present = {
        "PYTHON_FILTER": any(not _is_join_condition(c.sql) for l in loops for c in l.conditions),
        "N_PLUS_ONE": any(l.queries for l in loops),
        "MANUAL_JOIN": any(l.nested_loops for l in loops),
    }
    return [f for f in findings if present.get(f.pattern.upper(), True)]


def _merge_rewrite_hints(
    findings: List[OptimizationFinding], procedure_ast: ProcedureAST
) -> List[OptimizationFinding]:
    """Add Stage 1's own fixes (rewrite hints) to the list of findings.

    Stage 1 builds these directly from the code, so they are more reliable
    than the model's answer. Each hint becomes a finding, and any model
    finding about the same source lines is removed."""
    hints = procedure_ast.rewrite_hints
    if not hints:
        return findings
    covered = {line for h in hints for line in h.lines}
    kept = [f for f in findings if f.lineno not in covered]
    from_hints = [
        OptimizationFinding(
            pattern=h.pattern, lineno=h.lineno, severity="high", explanation=h.explanation,
            optimized_code=h.optimized_code, caveats=list(h.caveats),
        )
        for h in hints
    ]
    return from_hints + kept


def request_optimizations(
    procedure_ast: ProcedureAST,
    ollama_client: Optional[OllamaClient],
    log_request: bool = True,
    request_log_path: Union[str, Path] = DEFAULT_REQUEST_LOG_PATH,
) -> Optional[LLMOptimizationResponse]:
    """Ask the AI model to look for slow database code.

    Returns None if there is no model to ask (`ollama_client` is None).

    If `log_request` is True, the full question is saved to
    `request_log_path` *before* it is sent, so it is kept even if the model
    never answers."""
    if ollama_client is None:
        return None

    prompt = (
        f"Source code:\n{procedure_ast.source}\n\n"
        f"Structured facts (JSON):\n{_procedure_ast_to_json(procedure_ast)}\n\n"
        "Return only the JSON described in the system prompt."
    )
    logged_path = (
        log_llm_request(
            procedure_name=procedure_ast.name, model=ollama_client.model, host=ollama_client.host,
            system_prompt=_SYSTEM_PROMPT, prompt=prompt, log_path=request_log_path,
        )
        if log_request
        else None
    )

    try:
        data = ollama_client.generate_json(prompt, system=_SYSTEM_PROMPT)
    except OllamaUnavailable as e:
        return LLMOptimizationResponse(error=str(e), request_log_path=logged_path)
    except json.JSONDecodeError:
        # The answer wasn't valid JSON. Ask again as plain text and return
        # that text, so the user can still read what the model said.
        try:
            raw = ollama_client.generate(prompt, system=_SYSTEM_PROMPT)
        except OllamaUnavailable as e:
            return LLMOptimizationResponse(error=str(e), request_log_path=logged_path)
        return LLMOptimizationResponse(raw_text=raw, request_log_path=logged_path)

    if not isinstance(data, dict):
        return LLMOptimizationResponse(raw_text=json.dumps(data), request_log_path=logged_path)
    findings = _merge_rewrite_hints(_drop_unsupported(_parse_findings(data), procedure_ast), procedure_ast)
    return LLMOptimizationResponse(findings=findings, request_log_path=logged_path)
