"""
Saves a copy of every question (prompt) sent to the AI model.

Each request is added as one line to a JSONL file ("one JSON object per
line"), with the time, the model name, the server address and the full
prompt. This lets you see exactly what the model was asked, which helps
when an answer looks strange. ast_log.py does the same for Stage 1.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Union

DEFAULT_LOG_PATH = Path("logs") / "llm_requests.jsonl"


def log_llm_request(
    procedure_name: str,
    model: str,
    host: str,
    system_prompt: str,
    prompt: str,
    log_path: Union[str, Path] = DEFAULT_LOG_PATH,
) -> Path:
    """Add one AI request to the end of the log file as a JSON line, together
    with the current time (in UTC).

    The file and its folder are created if they don't exist yet.
    Returns the path of the log file."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    record = {
        "timestamp": timestamp.replace("+00:00", "Z"),
        "procedure_name": procedure_name,
        "model": model,
        "host": host,
        "system_prompt": system_prompt,
        "prompt": prompt,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record))
        f.write("\n")
    return path
