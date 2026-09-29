"""Manual Jev metadata gate for the step-by-step CLI path (`jev-gate`, off by default).

Runs after discovery and before rank. For each candidate it asks the generic source
questions (jev.SOURCE_QUESTIONS, the same ones run-pipeline's jev_rank uses) about the
cached metadata and the brief, and drops candidates whose score (mean of P(usable),
P(subject), P(conditions)) is at or below ``jev_reject_below``. Survivors are kept and,
with ``jev_order_by_p``, ordered by score. run-pipeline does not call this module; it
ranks inside discovery (docs/JEV.md).

Safety: disabled unless the config sets ``jev_gate: true``; the key comes only from
OPENROUTER_API_KEY and is never written; any missing key, error, malformed answer or
spent budget keeps the candidate (the rule gate already accepted it) and records why.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from scenery_brief_clips.jev import (
    API_KEY_ENV,
    JEV_MODEL,
    JEV_URL,  # noqa: F401 - re-exported for callers and tests
    SOURCE_QUESTIONS,
    SOURCE_QUESTIONS_VERSION,
    JevClient,
    JevError,  # noqa: F401 - re-exported
    JevSettings,
    PostFn,
    compact,
    default_post,
    metadata_state,
    source_score,
)
from scenery_brief_clips.run_lock import exclusive_run_lock
from scenery_brief_clips.store import write_json_atomic

SCHEMA_VERSION = "jev_gate_v2"


@dataclass(frozen=True)
class JevGateSettings:
    enabled: bool = False
    reject_below: float = 0.35
    keep_above: float = 0.85
    timeout_s: float = 20.0
    max_usd_per_run: float = 0.25
    order_by_p: bool = True

    @classmethod
    def from_config(cls, config: Mapping) -> "JevGateSettings":
        return cls(
            enabled=bool(config.get("jev_gate", False)),
            reject_below=float(config.get("jev_reject_below", 0.35)),
            keep_above=float(config.get("jev_keep_above", 0.85)),
            timeout_s=float(config.get("jev_timeout_s", 20.0)),
            max_usd_per_run=float(config.get("jev_max_usd_per_run", 0.25)),
            order_by_p=bool(config.get("jev_order_by_p", True)),
        )


def brief_from_constraint(constraint: Mapping) -> dict:
    """A minimal brief for runs made without one (legacy `run --prompt`)."""
    return {"request_text": str(constraint.get("theme_text") or ""),
            "scene": {"excluded": [str(x) for x in (constraint.get("visual_negatives") or [])]}}


def gate_run(
    run_dir: Path,
    metadata_cache: Path,
    jev_cache: Path,
    settings: JevGateSettings,
    env: Mapping[str, str] | None = None,
    post: PostFn | None = None,
    brief: Mapping | None = None,
) -> dict:
    run_dir = Path(run_dir)
    with exclusive_run_lock(run_dir):
        return _gate_run_locked(run_dir, Path(metadata_cache), Path(jev_cache), settings,
                                os.environ if env is None else env, post or default_post, brief)


def _gate_run_locked(run_dir, metadata_cache, jev_cache, settings, env, post, brief) -> dict:
    pre_path = run_dir / "candidates_pre_jev.json"
    cand_path = run_dir / "candidates.json"
    if not settings.enabled:
        return {"run_dir": str(run_dir), "enabled": False, "changed": False}
    source_path = pre_path if pre_path.is_file() else cand_path
    candidates = json.loads(source_path.read_text(encoding="utf-8"))
    if brief is None:
        brief = brief_from_constraint(json.loads((run_dir / "constraint.json").read_text(encoding="utf-8")))
    client = JevClient(api_key=str(env.get(API_KEY_ENV) or ""), cache_dir=jev_cache,
                       settings=replace(JevSettings(), timeout_s=settings.timeout_s,
                                        max_usd_per_run=settings.max_usd_per_run, workers=1),
                       post=post)

    records: list[dict] = []
    for index, cand in enumerate(candidates):
        video_id = str(cand.get("video_id") or "")
        rec: dict = {"video_id": video_id, "title": cand.get("title"), "discovery_index": index,
                     "score": None, "answers": None, "decision": "keep_fallback", "source": None,
                     "cache_hit": False, "latency_s": None, "cost_usd": 0.0}
        info_path = metadata_cache / f"{video_id.replace('/', '_')}.json"
        if not info_path.is_file():
            rec["source"] = "fallback_no_metadata"
            records.append(rec)
            continue
        got = client.ask(metadata_state(brief, json.loads(info_path.read_text(encoding="utf-8"))), SOURCE_QUESTIONS)
        rec.update(source=got["source"], cache_hit=got["source"] == "jev_cache", latency_s=got["latency_s"],
                   cost_usd=got["cost_usd"])
        if got.get("error"):
            rec["error"] = got["error"]
        if got["answers"] is not None:
            score = source_score(got["answers"])
            rec["score"] = score
            rec["answers"] = compact(got["answers"])
            if score <= settings.reject_below:
                rec["decision"] = "reject"
            elif score >= settings.keep_above:
                rec["decision"] = "keep_high"
            else:
                rec["decision"] = "keep_review"
        records.append(rec)

    by_id = {r["video_id"]: r for r in records}
    survivors = [c for c in candidates if by_id[str(c.get("video_id") or "")]["decision"] != "reject"]
    if settings.order_by_p:
        # Scored survivors first by score desc; fallbacks keep discovery order after them.
        survivors.sort(key=lambda c: (by_id[str(c.get("video_id") or "")]["score"] is None,
                                      -(by_id[str(c.get("video_id") or "")]["score"] or 0.0)))
    counts: dict[str, int] = {}
    sources: dict[str, int] = {}
    for r in records:
        counts[r["decision"]] = counts.get(r["decision"], 0) + 1
        sources[str(r["source"])] = sources.get(str(r["source"]), 0) + 1
    report = {
        "schema_version": SCHEMA_VERSION,
        "model": JEV_MODEL,
        "questions_version": SOURCE_QUESTIONS_VERSION,
        "score": "mean of P(usable), P(subject), P(conditions)",
        "settings": {"reject_below": settings.reject_below, "keep_above": settings.keep_above,
                     "timeout_s": settings.timeout_s, "max_usd_per_run": settings.max_usd_per_run,
                     "order_by_p": settings.order_by_p},
        "brief": {"request_text": brief.get("request_text")},
        "counts": {"input": len(candidates), "kept": len(survivors), "by_decision": counts, "by_source": sources},
        "cost_usd": round(client.spent_usd, 8),
        "candidates": records,
    }
    if not pre_path.is_file():
        write_json_atomic(pre_path, candidates)
    write_json_atomic(run_dir / "jev_gate.json", report)
    write_json_atomic(cand_path, survivors)
    return {"run_dir": str(run_dir), "enabled": True, "changed": True, "input": len(candidates),
            "kept": len(survivors), "by_decision": counts, "by_source": sources,
            "cost_usd": round(client.spent_usd, 8), "jev_gate_path": str(run_dir / "jev_gate.json")}
