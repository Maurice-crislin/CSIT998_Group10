"""
n1_optimizer
============

Finds slow database code in Python programs and suggests faster versions.

The programs we look at run SQL with calls like `cursor.execute("SELECT ...")`
and then process the rows with Python loops. A common problem is doing work
in Python that the database could do much faster, for example:

  - running one query per row inside a loop (called an "N+1 query"),
  - matching rows from two tables with nested loops instead of a SQL JOIN,
  - adding up or counting rows in Python instead of using SUM() or COUNT().

The work happens in two steps ("stages"):

1. Stage 1 -- ast_builder.py
   Reads the Python code (without running it) and writes down plain facts:
   which queries run, which loops go over which rows, and what each loop
   does. For shapes it fully understands, it also builds the faster SQL
   itself (see rewrites.py). No AI is used here, so the facts are exact.

2. Stage 2 -- llm_optimizer.py
   Sends the code and the Stage 1 facts to a local AI model (an LLM, run by
   Ollama) and asks it to point out problems and suggest fixes. AI answers
   can be wrong, so always review a suggestion before using it.
"""

from .ast_builder import build_procedure_ast
from .ast_log import log_procedure_ast
from .ast_model import ProcedureAST
from .llm_log import log_llm_request
from .llm_optimizer import LLMOptimizationResponse, OptimizationFinding, request_optimizations
from .pipeline import OptimizationResult, optimize_source

__all__ = [
    "optimize_source",
    "OptimizationResult",
    "build_procedure_ast",
    "ProcedureAST",
    "request_optimizations",
    "LLMOptimizationResponse",
    "OptimizationFinding",
    "log_procedure_ast",
    "log_llm_request",
]
