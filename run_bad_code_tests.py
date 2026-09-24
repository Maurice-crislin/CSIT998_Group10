#!/usr/bin/env python3
"""
Run cli.py on every .py file in tests/bad_code and save all the results in
one CSV file (a table that opens in Excel).

cli.py is started once per file as a separate program (using the
`subprocess` module), and the text it prints is split into columns.

Usage:
    python run_bad_code_tests.py
    python run_bad_code_tests.py --out results/bad_code.csv
    python run_bad_code_tests.py --model llama3:latest --timeout 600
    python run_bad_code_tests.py --dir tests/bad_code --pattern "bad_code_1*.py"

One CSV row per finding. A file with no findings (or where the run failed)
still gets one row, with `status` saying what happened:
    findings    the LLM stage returned one or more findings
    no_issues   the LLM stage returned no findings
    raw_text    the LLM reply wasn't valid JSON (raw reply in `explanation`)
    llm_failed  the LLM call itself failed, e.g. timed out (message in `explanation`)
    no_llm      Ollama wasn't reachable, only Stage 1 ran
    error       cli.py crashed or produced output this script doesn't recognise
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent   # the folder this script is in
CLI = HERE / "cli.py"

# Matches the heading cli.py prints for each finding, e.g.
# "--- Finding 1: N_PLUS_ONE (line 10, severity: high) ---"
_FINDING_RE = re.compile(
    r"^--- Finding (?P<n>\d+): (?P<pattern>.*?) \(line (?P<line>-?\d+), severity: (?P<severity>[^)]*)\) ---$",
    re.M,
)
# The CSV columns, in order.
FIELDS = [
    "file", "status", "finding_no", "pattern", "line", "severity",
    "explanation", "suggested_code", "caveats", "elapsed_s", "exit_code", "raw_output",
]


def _natural_key(path: Path):
    """Sort key that compares the numbers inside file names as numbers, so
    file_2.py comes before file_10.py (normal text sorting puts 10 first)."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path.name)]


def _parse_findings(output: str) -> List[Dict[str, str]]:
    """Split cli.py's printed findings into one dict per finding.

    Each finding is printed as: a heading line, the explanation,
    "Suggested code:" followed by the code, and optionally "Caveats:"
    followed by lines starting with "- "."""
    headers = list(_FINDING_RE.finditer(output))
    findings = []
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(output)
        block = output[h.end():end].strip("\n")
        before, _, after = block.partition("\nSuggested code:\n")
        code, sep, caveat_text = after.rpartition("\nCaveats:\n")
        if not sep:  # a finding without caveats
            code, caveat_text = after, ""
        caveats = [l[2:] for l in caveat_text.splitlines() if l.startswith("- ")]
        findings.append({
            "finding_no": h.group("n"),
            "pattern": h.group("pattern"),
            "line": h.group("line"),
            "severity": h.group("severity"),
            "explanation": before.strip(),
            "suggested_code": code.strip("\n"),
            "caveats": "\n".join(caveats),
        })
    return findings


def run_one(path: Path, extra_args: List[str], timeout: int) -> List[Dict[str, str]]:
    """Run cli.py on one file and return its CSV rows (one per finding, or
    one row with a status if there were no findings)."""
    # Make the child program print UTF-8, so accented letters come through correctly.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, str(CLI), str(path), *extra_args]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=timeout,
        )
        output, code = proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else ""), proc.returncode
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or "") if isinstance(e.stdout, str) else ""
        output += f"\n[timed out after {timeout}s]"
        code = None
    elapsed = f"{time.monotonic() - start:.1f}"

    base = {"file": path.name, "elapsed_s": elapsed, "exit_code": "" if code is None else str(code),
            "raw_output": output}
    findings = _parse_findings(output)
    if findings:
        return [{**base, "status": "findings", **f} for f in findings]

    # No findings: work out why from the text cli.py printed.
    if code is None or (code != 0 and "LLM call failed:" not in output):
        status, detail = "error", ""
    elif "LLM call failed:" in output:
        status, detail = "llm_failed", output.split("LLM call failed:", 1)[1].strip()
    elif "No issues detected." in output:
        status, detail = "no_issues", ""
    elif "=== Stage 2: LLM response (not valid JSON" in output:
        status, detail = "raw_text", output.split("===\n", 1)[-1].strip()
    elif "[Ollama not available" in output:
        status, detail = "no_llm", ""
    else:
        status, detail = "error", ""
    return [{**base, "status": status, "explanation": detail}]


def main() -> int:
    """Run all the test files and write the CSV. Returns 0, or 1 if no files were found."""
    parser = argparse.ArgumentParser(description="Run cli.py on every test file and write the results to CSV.")
    parser.add_argument("--dir", default=str(HERE / "tests" / "bad_code"), help="Folder of .py files to test.")
    parser.add_argument("--pattern", default="*.py", help="Glob for files inside --dir (default: *.py).")
    parser.add_argument("--out", default=str(HERE / "bad_code_results.csv"), help="CSV file to write.")
    parser.add_argument("--model", default=None, help="Ollama model (default: cli.py's default).")
    parser.add_argument("--host", default=None, help="Ollama host (default: cli.py's default).")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds cli.py waits for the LLM.")
    parser.add_argument("--keep-logs", action="store_true",
                        help="Let cli.py append to its AST/LLM request logs (off by default).")
    args = parser.parse_args()

    files = sorted(Path(args.dir).glob(args.pattern), key=_natural_key)
    if not files:
        print(f"No files matching {args.pattern} in {args.dir}")
        return 1

    extra = ["--timeout", str(args.timeout)]
    if args.model:
        extra += ["--model", args.model]
    if args.host:
        extra += ["--host", args.host]
    if not args.keep_logs:
        extra += ["--no-ast-log", "--no-llm-log"]

    rows: List[Dict[str, str]] = []
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name} ...", end=" ", flush=True)
        # cli.py has its own --timeout for the AI model. This outer limit is
        # 2 minutes longer, and only stops cli.py if it hangs completely.
        file_rows = run_one(path, extra, timeout=args.timeout + 120)
        rows.extend(file_rows)
        summary = ", ".join(r["pattern"] for r in file_rows if r.get("pattern")) or file_rows[0]["status"]
        print(f"{file_rows[0]['elapsed_s']}s  {summary}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # "utf-8-sig" adds a small marker at the start of the file that tells
    # Excel the text is UTF-8, so accented letters display correctly.
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} rows for {len(files)} files to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
