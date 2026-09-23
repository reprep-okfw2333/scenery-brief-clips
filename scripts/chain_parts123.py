#!/usr/bin/env python3
"""Run parts 1–3 in order, then verify. Live YouTube.

Does not apply vision scores. After this script, rank tiles still need
labels + apply-scores, then analyze should be re-run. See docs/STATUS.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT = "Beautiful natural european scenery, 1920p or higher, 16:9, 20 individual clips"
MAX_RESULTS = 5
SLEEP = 2.0
MAX_TILES = 8
MAX_ANALYSIS_S = 120.0


def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.rfind("\n{")
        if start == -1:
            start = text.rfind("{")
        if start == -1:
            raise
        return json.loads(text[start:])


def _cli(args: list[str]) -> dict:
    bin_path = ROOT / ".venv" / "bin" / "scenery-brief-clips"
    cmd = [str(bin_path), *args]
    print("+", " ".join(cmd), flush=True)
    env = {**os.environ, "TMPDIR": str(ROOT / "tmp")}
    proc = subprocess.run(cmd, cwd=ROOT, check=False, capture_output=True, text=True, env=env)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    print(proc.stdout)
    payload = _parse_json(proc.stdout) if proc.stdout.strip() else {}
    if proc.returncode != 0 and args[0] != "verify":
        raise SystemExit(proc.returncode)
    payload["_exit"] = proc.returncode
    return payload


def main() -> int:
    doctor = _cli(["doctor"])
    if not doctor.get("ok"):
        print("doctor failed", json.dumps(doctor, indent=2))
        return 1
    explained = _cli(["explain-prompt", PROMPT])
    if explained.get("min_width") != 1920 or explained.get("min_height") != 1080:
        print("prompt did not compile to 1920x1080")
        return 1
    run = _cli(
        [
            "run",
            "--dry-run",
            "--prompt",
            PROMPT,
            "--max-results",
            str(MAX_RESULTS),
            "--max-metadata",
            str(MAX_RESULTS),
            "--sleep",
            str(SLEEP),
        ]
    )
    run_dir = run["run_dir"]
    n_cand = int(run["candidates"])
    if n_cand < 1:
        print("no candidates")
        return 1
    _cli(["rank", "--run-dir", run_dir, "--max-videos", str(n_cand), "--max-tiles", str(MAX_TILES)])
    _cli(
        [
            "analyze",
            "--run-dir",
            run_dir,
            "--max-videos",
            str(n_cand),
            "--max-analysis-s",
            str(MAX_ANALYSIS_S),
        ]
    )
    report = _cli(["verify", "--run-dir", run_dir])
    print(json.dumps({"chain": "ok", "run_dir": run_dir, "verify": report}, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
