"""Summarize a batch of bench.sh runs.

Usage:
    python benchmark/batch_summary.py <index.tsv> [--out PATH.json]
    python benchmark/batch_summary.py <out_dir> [<out_dir> ...] [--out PATH.json]

An index is the TSV written by benchmark/batch.sh (<ID>\\t<exit_code>\\t<out_dir>).
Out dirs are benchmark/runs/<label>-<stamp>/ directories holding the
summary.json that summarize.py wrote. Missing or unreadable files never crash
the script; the row's "problems" list says what was missing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _inputs_txt(out: Path) -> dict[str, str]:
    kv: dict[str, str] = {}
    text = _read(out / "inputs.txt") or ""
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def _find_brief(out: Path, problems: list[str]):
    """Prefer the brief the run actually used (tmp/bench root), else the input set."""
    cands = [PROJ / "tmp" / "bench" / out.name / "brief.json"]
    bench_inputs = _inputs_txt(out).get("BENCH_INPUTS")
    if bench_inputs:
        cands.append(PROJ / "benchmark" / bench_inputs / "brief.json")
    for p in cands:
        b = _load(p)
        if isinstance(b, dict):
            return b
    problems.append("brief")
    return None


def _stage_sum(out: Path, summary: dict | None) -> tuple[float | None, bool]:
    """Sum every timing entry of result.json (summary.json keys stages by name,
    so a resumed run's 'reused' zeros overwrite the executed times).
    Returns (sum, resumed_flag)."""
    result = _load(out / "result.json")
    stages = ((result or {}).get("timing") or {}).get("stages")
    if isinstance(stages, list) and stages:
        names = [s.get("stage") for s in stages]
        total = sum(float(s.get("elapsed_s") or 0) for s in stages)
        return round(total, 2), len(names) != len(set(names))
    if summary and summary.get("stage_sum_s") is not None:
        return summary["stage_sum_s"], False
    return None, False


def _row(run_id: str | None, exit_code: str | None, out_dir: str | None) -> dict:
    problems: list[str] = []
    row: dict = {"id": run_id, "out_dir": out_dir}
    out = Path(out_dir) if out_dir and out_dir != "missing" else None
    if out is not None and not out.is_absolute():
        out = PROJ / out
    if out is None or not out.is_dir():
        problems.append("out_dir")
        out = None

    summary = _load(out / "summary.json") if out else None
    if out and not isinstance(summary, dict):
        problems.append("summary.json")
        summary = None
    summary = summary or {}
    y = summary.get("yield") or {}

    if run_id is None and out is not None:
        m = re.match(r"^(?:.+-)?(R\d+)-\d{8}T\d{6}Z$", out.name)
        run_id = m.group(1) if m else out.name
        row["id"] = run_id

    brief = _find_brief(out, problems) if out else None
    if brief is None and out is None and run_id:
        pass  # index caller may fill it in via _fallback_brief
    row["request_text"] = (brief or {}).get("request_text")
    row["n_requested"] = (brief or {}).get("n_clips")

    if exit_code is None and out is not None:
        exit_code = _read(out / "exit_code.txt")
    row["exit_code"] = exit_code
    row["status"] = summary.get("status")
    row["stage"] = summary.get("stage")
    row["error"] = summary.get("error")
    row["wall_clock"] = summary.get("wall_clock")
    stage_sum, resumed = _stage_sum(out, summary) if out else (None, False)
    row["stage_sum_s"] = stage_sum
    row["resumed"] = resumed
    row["model_calls"] = summary.get("model_calls")
    row["candidates"] = y.get("candidates")
    row["analyzed_sources"] = y.get("analyzed_sources")
    row["excerpts"] = y.get("excerpts")
    row["continuity_rejected"] = y.get("continuity_rejected")
    counts = y.get("shortlist_counts") or {}
    row["shortlist_excluded_by_reason"] = counts.get("n_excluded_by_reason")
    row["clips_delivered"] = y.get("clips_delivered")
    row["verify_export_ok"] = y.get("verify_export_ok")
    row["request_fulfilled"] = y.get("request_fulfilled")
    row["unattended"] = str(exit_code) == "0" and summary.get("status") == "completed"
    row["problems"] = problems
    return row


def _fallback_brief(row: dict, batch: str | None) -> None:
    if row.get("request_text") is None and batch and row.get("id"):
        b = _load(PROJ / "benchmark" / batch / row["id"] / "brief.json")
        if isinstance(b, dict):
            row["request_text"] = b.get("request_text")
            row["n_requested"] = b.get("n_clips")
            if "brief" in row["problems"]:
                row["problems"].remove("brief")


def _num(v) -> float:
    return v if isinstance(v, (int, float)) else 0


def _cell(v) -> str:
    if v is None:
        return "?"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="an index.tsv, or one or more out dirs")
    ap.add_argument("--out", help="write rows + totals as JSON")
    args = ap.parse_args(argv)

    rows: list[dict] = []
    for a in args.paths:
        p = Path(a)
        if p.is_dir():
            rows.append(_row(None, None, str(p.resolve())))
            continue
        text = _read(p)
        if text is None:
            print(f"cannot read {a}", file=sys.stderr)
            rows.append({"id": None, "out_dir": a, "problems": ["index"], "unattended": False})
            continue
        m = re.match(r"^(.+)-index-\d{8}T\d{6}Z\.tsv$", p.name)
        batch = m.group(1) if m else None
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not parts[0]:
                continue
            row = _row(parts[0], parts[1], parts[2] if len(parts) > 2 else None)
            _fallback_brief(row, batch)
            rows.append(row)
    # Out dirs passed directly: brief lookup already fell back to BENCH_INPUTS.

    totals = {
        "runs": len(rows),
        "unattended_completions": sum(1 for r in rows if r.get("unattended")),
        "clips_requested": sum(_num(r.get("n_requested")) for r in rows),
        "clips_delivered": sum(_num(r.get("clips_delivered")) for r in rows),
        "stage_seconds": round(sum(_num(r.get("stage_sum_s")) for r in rows), 2),
        "model_calls": sum(_num(r.get("model_calls")) for r in rows),
    }

    cols = [
        ("id", "id"), ("requested", "n_requested"), ("delivered", "clips_delivered"),
        ("verify", "verify_export_ok"), ("unattended", "unattended"), ("stopped at", "stage"),
        ("wall", "wall_clock"), ("stage sum s", "stage_sum_s"), ("model calls", "model_calls"),
        ("excerpts", "excerpts"), ("note", None),
    ]
    print("| " + " | ".join(c[0] for c in cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        cells = []
        for name, key in cols:
            if key is None:
                notes = []
                if r.get("problems"):
                    notes.append("missing: " + ",".join(r["problems"]))
                if r.get("resumed"):
                    notes.append("resumed")
                if r.get("error"):
                    notes.append("error: " + str(r["error"]).replace("|", "/").replace("\n", " ")[:80])
                cells.append("; ".join(notes))
            else:
                cells.append(_cell(r.get(key)))
        print("| " + " | ".join(cells) + " |")
    print()
    print(f"runs: {totals['runs']}")
    print(f"unattended completions: {totals['unattended_completions']}/{totals['runs']}")
    print(f"clips requested/delivered: {totals['clips_requested']}/{totals['clips_delivered']}")
    print(f"total stage seconds: {totals['stage_seconds']}")
    print(f"total model calls: {totals['model_calls']}")

    if args.out:
        Path(args.out).write_text(json.dumps({"rows": rows, "totals": totals}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
