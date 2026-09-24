"""
Saves a copy of every Stage 1 result to a log file.

Each time Stage 1 describes a program (a ProcedureAST), one line is added to
a JSONL file. JSONL simply means "one JSON object per line", which makes the
file easy to read back later, line by line.

This is useful for looking at what Stage 1 produced in the past without
running it again. Logging can be switched off with the `log_ast` option of
pipeline.optimize_source().
"""
from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path
from typing import Union

from .ast_model import ProcedureAST

DEFAULT_LOG_PATH = Path("logs") / "procedure_ast.jsonl"


def log_procedure_ast(procedure_ast: ProcedureAST, log_path: Union[str, Path] = DEFAULT_LOG_PATH) -> Path:
    """Add `procedure_ast` to the end of the log file as one JSON line,
    together with the current time (in UTC).

    The file and its folder are created if they don't exist yet.
    Returns the path of the log file."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    record = {
        "timestamp": timestamp.replace("+00:00", "Z"),
        "procedure_name": procedure_ast.name,
        "procedure_ast": dataclasses.asdict(procedure_ast),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str))
        f.write("\n")
    return path
