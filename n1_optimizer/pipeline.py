"""
Runs the whole process on one piece of code: Stage 1, then Stage 2.
See the package overview in __init__.py for what each stage does.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from .ast_builder import build_procedure_ast
from .ast_log import DEFAULT_LOG_PATH, log_procedure_ast
from .ast_model import ProcedureAST
from .llm_log import DEFAULT_LOG_PATH as DEFAULT_LLM_LOG_PATH
from .llm_optimizer import LLMOptimizationResponse, request_optimizations
from .ollama_client import OllamaClient


@dataclass
class OptimizationResult:
    """Everything optimize_source() produced."""
    procedure_ast: ProcedureAST                         # the Stage 1 facts
    used_ollama: bool                                   # True if the AI model was reachable
    response: Optional[LLMOptimizationResponse] = None  # the Stage 2 answer; None if there was no AI model
    ast_log_path: Optional[Path] = None                 # where the Stage 1 facts were saved, if they were


def optimize_source(
    source: str,
    func_name: Optional[str] = None,
    ollama_model: str = "llama3:latest",
    ollama_host: str = "http://localhost:11434",
    ollama_timeout: int = 300,
    use_ollama: bool = True,
    log_ast: bool = True,
    ast_log_path: Union[str, Path] = DEFAULT_LOG_PATH,
    log_llm_request: bool = True,
    llm_log_path: Union[str, Path] = DEFAULT_LLM_LOG_PATH,
) -> OptimizationResult:
    """Analyze the Python code in `source` and suggest faster database code.

    Steps:
      1. Check whether the Ollama server is running (only if `use_ollama`).
      2. Stage 1: build_procedure_ast() reads the code and records facts
         about its queries and loops (a ProcedureAST). If `log_ast` is True,
         those facts are also saved to `ast_log_path`.
      3. Stage 2: request_optimizations() sends the code and the facts to
         the AI model and collects its suggestions. If `log_llm_request` is
         True, the exact question sent is saved to `llm_log_path`.

    `func_name` limits the analysis to one function in the code.
    """
    client: Optional[OllamaClient] = None
    used_ollama = False
    if use_ollama:
        candidate = OllamaClient(model=ollama_model, host=ollama_host, timeout=ollama_timeout)
        if candidate.available():
            client = candidate
            used_ollama = True

    procedure_ast = build_procedure_ast(source, func_name=func_name)

    logged_path = log_procedure_ast(procedure_ast, ast_log_path) if log_ast else None

    response = request_optimizations(
        procedure_ast, ollama_client=client, log_request=log_llm_request, request_log_path=llm_log_path
    )

    return OptimizationResult(
        procedure_ast=procedure_ast, used_ollama=used_ollama, response=response, ast_log_path=logged_path
    )
