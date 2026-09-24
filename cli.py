#!/usr/bin/env python3
"""
Command-line tool: analyze one Python file and print suggestions for
faster database code.

It runs Stage 1 (read the code and record facts) and Stage 2 (ask the AI
model for suggestions), then prints each finding with its suggested code.
If the AI model (Ollama) isn't running, only the Stage 1 facts are printed.

Usage:
    python cli.py path/to/code.py
    cat code.py | python cli.py                           # read the code from standard input
    python cli.py path/to/code.py --func my_function      # only analyze this function
    python cli.py path/to/code.py --model llama3.1 --host http://localhost:11434
    python cli.py path/to/code.py --timeout 600           # wait longer for the model to answer
    python cli.py path/to/code.py --show-ast              # also print the Stage 1 facts sent to the model
    python cli.py path/to/code.py --no-ast-log            # don't save the Stage 1 facts to the log
    python cli.py path/to/code.py --ast-log-path logs/custom.jsonl
    python cli.py path/to/code.py --no-llm-log            # don't save the question sent to the model
    python cli.py path/to/code.py --llm-log-path logs/custom_requests.jsonl
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from n1_optimizer import optimize_source
from n1_optimizer.ast_log import DEFAULT_LOG_PATH as DEFAULT_AST_LOG_PATH
from n1_optimizer.llm_log import DEFAULT_LOG_PATH as DEFAULT_LLM_LOG_PATH


def main() -> int:
    """Read the command-line options, analyze the code and print the result.
    Returns the exit code: 0 for success, 1 if the AI model call failed."""
    parser = argparse.ArgumentParser(description="Parse Python+SQL to an AST and ask an LLM to optimize it.")
    parser.add_argument("file", nargs="?", help="Path to a .py file. Reads stdin if omitted.")
    parser.add_argument("--func", default=None, help="Only analyze this function name.")
    parser.add_argument("--model", default="llama3:latest", help="Ollama model to use.")
    parser.add_argument("--host", default="http://localhost:11434", help="Ollama server URL.")
    parser.add_argument(
        "--timeout", type=int, default=300,
        help="Seconds to wait for the LLM reply (a cold model load can take a while).",
    )
    parser.add_argument("--no-ollama", action="store_true", help="Skip the LLM call; only build and print the AST.")
    parser.add_argument("--show-ast", action="store_true", help="Also print the structured facts sent to the LLM.")
    parser.add_argument(
        "--no-ast-log", action="store_true", help="Skip appending Stage 1's ProcedureAST to the AST log file."
    )
    parser.add_argument(
        "--ast-log-path", default=str(DEFAULT_AST_LOG_PATH),
        help=f"Where to append Stage 1's timestamped ProcedureAST (default: {DEFAULT_AST_LOG_PATH}).",
    )
    parser.add_argument(
        "--no-llm-log", action="store_true", help="Skip logging the request sent to the LLM."
    )
    parser.add_argument(
        "--llm-log-path", default=str(DEFAULT_LLM_LOG_PATH),
        help=f"Where to append each timestamped LLM request (default: {DEFAULT_LLM_LOG_PATH}).",
    )
    args = parser.parse_args()

    source = open(args.file).read() if args.file else sys.stdin.read()

    result = optimize_source(
        source,
        func_name=args.func,
        ollama_model=args.model,
        ollama_host=args.host,
        ollama_timeout=args.timeout,
        use_ollama=not args.no_ollama,
        log_ast=not args.no_ast_log,
        ast_log_path=args.ast_log_path,
        log_llm_request=not args.no_llm_log,
        llm_log_path=args.llm_log_path,
    )

    if result.ast_log_path is not None:
        print(f"[Stage 1 ProcedureAST logged to {result.ast_log_path}]\n")

    if args.show_ast or not result.used_ollama:
        ast_json = json.dumps(dataclasses.asdict(result.procedure_ast), indent=2, default=str)
        print("=== Stage 1: ProcedureAST ===\n")
        print(ast_json)
        print()

    if not result.used_ollama:
        print("[Ollama not available at the given host -- ran Stage 1 (AST) only; no optimization response]")
        return 0

    print(f"[using Ollama model '{args.model}' at {args.host} for the optimization pass]\n")

    response = result.response
    if response is None:
        print("No response from the LLM.")
        return 0

    if response.request_log_path is not None:
        print(f"[LLM request logged to {response.request_log_path}]\n")

    if response.error is not None:
        print(f"LLM call failed: {response.error}")
        return 1

    if response.raw_text is not None:
        print("=== Stage 2: LLM response (not valid JSON, showing raw text) ===\n")
        print(response.raw_text)
        return 0

    if not response.findings:
        print("No issues detected.")
        return 0

    print("=== Stage 2: LLM-suggested optimizations ===\n")
    for i, finding in enumerate(response.findings, start=1):
        print(f"--- Finding {i}: {finding.pattern} (line {finding.lineno}, severity: {finding.severity}) ---")
        print(finding.explanation)
        print()
        print("Suggested code:")
        print(finding.optimized_code)
        if finding.caveats:
            print()
            print("Caveats:")
            for c in finding.caveats:
                print(f"- {c}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
