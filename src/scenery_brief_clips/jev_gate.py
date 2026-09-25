"""Optional Jev metadata gate (experimental, off by default).

Runs after discovery and before rank/analyze. For each metadata-eligible candidate it asks
TypeSafe's Jev decision model (OpenRouter Decisions API) a fixed set of typed questions about
the title/description/tags/channel and the run's theme, and drops candidates whose
P(keep) is at or below ``jev_reject_below``. Survivors are kept (optionally re-ordered by
P(keep) so rank/analyze caps spend on the most promising first).

Safety properties:
- Disabled unless the project config sets ``jev_gate: true``.
- The API key is read only from the ``OPENROUTER_API_KEY`` environment variable. It is never
  logged or written to any artifact.
- Any missing key, HTTP error, timeout, malformed answer, or exhausted per-run budget falls back
  to the rule-based gate for that candidate (the candidate is kept, as the metadata rules
  already accepted it) and the fallback source is recorded.
- Decisions are cached under data/cache/jev keyed by model + question version + state hash.
- The gate never downloads video. It only reads the metadata cache.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from scenery_brief_clips.run_lock import exclusive_run_lock
from scenery_brief_clips.store import write_json_atomic

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"
QUESTIONS_VERSION = "jev_gate_q_v1"
API_KEY_ENV = "OPENROUTER_API_KEY"
SCHEMA_VERSION = "jev_gate_v1"
DESCRIPTION_MAX_CHARS = 1200
TAGS_MAX = 25

QUESTIONS: dict = {
    "live_action": {
        "type": "noul",
        "instructions": "Is this real live-action camera footage of real trains?",
        "criteria": {
            "true": "Filmed with a real camera (handheld, tripod, or drone) showing real railways and real trains.",
            "false": "AI-generated, CGI, 3D render, video game, animation, painted/AI art film, or a scene from a movie or TV show.",
        },
    },
    "shot_type": {
        "type": "choice",
        "instructions": "What kind of footage does this video mostly contain?",
        "criteria": {
            "exterior": "Trains filmed from outside: trackside, lineside, from a bridge, or aerial/drone views of the train in the landscape.",
            "cab_or_onboard": "Filmed from the driver's cab, the front of the train, or out of a passenger window while riding.",
            "ambience_loop": "Long relaxing, sleep, white-noise or ASMR ambience video.",
            "model_or_toy": "Model railway, toy train, or miniature layout.",
            "vlog_or_documentary": "Presenter vlog, travel show, documentary, history explainer, or urban exploration.",
            "other": "Something else: light show, crash video, non-train content, gameplay, movie clip.",
        },
    },
    "on_brief": {
        "type": "noul",
        "instructions": "Is the brief's subject and setting likely to be shown prominently in this video?",
        "criteria": {
            "true": "The video most likely shows the brief's subject and action in the brief's setting for a meaningful share of its runtime.",
            "false": "Mostly a different subject (for example modern diesel or electric trains when the brief asks for steam), a different setting, or the subject is only incidental.",
        },
    },
    "branding": {
        "type": "noul",
        "instructions": "Is the footage likely to carry a watermark, channel logo, burned-in captions, subtitles or text overlays?",
        "criteria": {
            "true": "Likely has an on-screen watermark, logo, captions, subtitles, titles or social handles over the picture.",
            "false": "Likely clean picture with no overlays.",
        },
    },
    "compilation": {
        "type": "noul",
        "instructions": "Is this a compilation or re-upload of other people's footage?",
        "criteria": {
            "true": "Aggregated or re-uploaded footage: 'no copyright' or stock channels, top-ten lists of other creators, clips from movies or games.",
            "false": "Original footage shot by the uploader (a creator's own highlight reel of their own footage counts as original).",
        },
    },
    "cinematic": {
        "type": "score",
        "instructions": "How cinematic and usable is this footage likely to be for an epic music edit?",
        "criteria": [
            "Unusable: very low quality, tiny, or not really the subject",
            "Amateur: shaky phone-grade or poorly framed",
            "Decent enthusiast footage",
            "High-quality enthusiast footage: 4K or 1080p, steady, well framed",
            "Cinematic, professional-grade footage",
        ],
    },
    "keep": {
        "type": "noul",
        "instructions": "Should the pipeline spend bandwidth downloading sections of this video to look for 4-12 second clips for an epic cinematic music edit matching the brief?",
        "criteria": {
            "true": "Real live-action exterior footage of the brief's subject in the brief's setting, likely usable; a small corner watermark is acceptable.",
            "false": "AI or CGI, cab or onboard view, ambience loop, model or toy, vlog or documentary, off-brief subject or setting, heavy burned-in text, low quality, or a re-upload.",
        },
    },
}

PostFn = Callable[[str, bytes, dict, float], dict]


class JevError(RuntimeError):
    pass


@dataclass(frozen=True)
class JevGateSettings:
    enabled: bool = False
    reject_below: float = 0.40
    keep_above: float = 0.85
    timeout_s: float = 20.0
    max_usd_per_run: float = 0.25
    order_by_p: bool = True

    @classmethod
    def from_config(cls, config: Mapping) -> "JevGateSettings":
        return cls(
            enabled=bool(config.get("jev_gate", False)),
            reject_below=float(config.get("jev_reject_below", 0.40)),
            keep_above=float(config.get("jev_keep_above", 0.85)),
            timeout_s=float(config.get("jev_timeout_s", 20.0)),
            max_usd_per_run=float(config.get("jev_max_usd_per_run", 0.25)),
            order_by_p=bool(config.get("jev_order_by_p", True)),
        )


def default_post(url: str, body: bytes, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Never include request headers in the message.
        raise JevError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JevError(f"transport error: {type(exc).__name__}") from None
    except json.JSONDecodeError:
        raise JevError("invalid JSON response") from None


def build_state(theme: dict, info: Mapping) -> dict:
    description = str(info.get("description") or "").strip()
    if len(description) > DESCRIPTION_MAX_CHARS:
        description = description[:DESCRIPTION_MAX_CHARS] + " …"
    formats = [
        f for f in (info.get("formats") or [])
        if isinstance(f, dict) and (f.get("vcodec") or "none") != "none" and isinstance(f.get("height"), (int, float))
    ]
    best = max(formats, key=lambda f: (f.get("height") or 0, f.get("width") or 0), default=None)
    return {
        "task": "Pre-download triage of a YouTube search result for an epic cinematic music edit. "
                "Only metadata is available; nothing has been watched yet.",
        "brief": theme,
        "video": {
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "channel_followers": info.get("channel_follower_count"),
            "duration_s": info.get("duration"),
            "views": info.get("view_count"),
            "likes": info.get("like_count"),
            "upload_date": info.get("upload_date"),
            "best_resolution": f"{best.get('width')}x{best.get('height')}" if best else None,
            "fps": best.get("fps") if best else None,
            "tags": list(info.get("tags") or [])[:TAGS_MAX],
            "description": description,
        },
    }


def theme_from_constraint(constraint: Mapping) -> dict:
    negatives = [str(x) for x in (constraint.get("visual_negatives") or [])]
    return {"theme": str(constraint.get("theme_text") or ""), "must_avoid": negatives + ["cab views"]}


def cache_key(state: dict) -> str:
    payload = json.dumps({"model": JEV_MODEL, "q": QUESTIONS_VERSION, "questions": QUESTIONS, "state": state},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _p_keep(answers: Mapping) -> float:
    value = (answers.get("keep") or {}).get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0.0 <= float(value) <= 1.0):
        raise JevError("missing or invalid keep probability")
    return float(value)


def _compact(answers: Mapping) -> dict:
    out: dict = {}
    for name, ans in answers.items():
        if not isinstance(ans, dict):
            continue
        kind = ans.get("type")
        if kind == "noul":
            out[name] = ans.get("noul")
        elif kind == "choice":
            out[name] = {"choice": ans.get("choice"), "confidence": ans.get("confidence"),
                         "probabilities": ans.get("probabilities")}
        elif kind == "score":
            out[name] = {"score": ans.get("score"), "confidence": ans.get("confidence"),
                         "probabilities": ans.get("probabilities")}
    return out


def ask_jev(state: dict, api_key: str, timeout_s: float, post: PostFn) -> tuple[dict, dict]:
    body = json.dumps({"model": JEV_MODEL, "state": state, "questions": QUESTIONS}).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = post(JEV_URL, body, headers, timeout_s)
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise JevError("response has no answers")
    _p_keep(data["answers"])
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return data["answers"], {"cost": float(usage.get("cost") or 0.0), "input_tokens": usage.get("input_tokens"),
                             "model": data.get("model")}


def gate_run(
    run_dir: Path,
    metadata_cache: Path,
    jev_cache: Path,
    settings: JevGateSettings,
    env: Mapping[str, str] | None = None,
    post: PostFn | None = None,
) -> dict:
    run_dir = Path(run_dir)
    with exclusive_run_lock(run_dir):
        return _gate_run_locked(run_dir, Path(metadata_cache), Path(jev_cache), settings,
                                os.environ if env is None else env, post or default_post)


def _gate_run_locked(run_dir, metadata_cache, jev_cache, settings, env, post) -> dict:
    pre_path = run_dir / "candidates_pre_jev.json"
    cand_path = run_dir / "candidates.json"
    if not settings.enabled:
        return {"run_dir": str(run_dir), "enabled": False, "changed": False}
    source_path = pre_path if pre_path.is_file() else cand_path
    candidates = json.loads(source_path.read_text(encoding="utf-8"))
    constraint = json.loads((run_dir / "constraint.json").read_text(encoding="utf-8"))
    theme = theme_from_constraint(constraint)
    api_key = str(env.get(API_KEY_ENV) or "")
    jev_cache.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    spent = 0.0
    for index, cand in enumerate(candidates):
        video_id = str(cand.get("video_id") or "")
        rec: dict = {"video_id": video_id, "title": cand.get("title"), "discovery_index": index,
                     "p_keep": None, "answers": None, "decision": "keep_fallback", "source": None,
                     "cache_hit": False, "latency_s": None, "cost_usd": 0.0}
        info_path = metadata_cache / f"{video_id.replace('/', '_')}.json"
        if not info_path.is_file():
            rec["source"] = "rules_fallback_no_metadata"
            records.append(rec)
            continue
        state = build_state(theme, json.loads(info_path.read_text(encoding="utf-8")))
        key = cache_key(state)
        cached_path = jev_cache / f"{key}.json"
        answers = None
        if cached_path.is_file():
            try:
                cached = json.loads(cached_path.read_text(encoding="utf-8"))
                answers = cached["answers"]
                _p_keep(answers)
                rec.update(cache_hit=True, source="jev_cache")
            except (OSError, ValueError, KeyError, JevError):
                answers = None
        if answers is None:
            if not api_key:
                rec["source"] = "rules_fallback_no_key"
            elif spent >= settings.max_usd_per_run:
                rec["source"] = "rules_fallback_budget"
            else:
                t0 = time.monotonic()
                try:
                    answers, usage = ask_jev(state, api_key, settings.timeout_s, post)
                except Exception as exc:  # noqa: BLE001 - any failure falls back to rules
                    rec["source"] = "rules_fallback_error"
                    rec["error"] = str(exc)[:200] if isinstance(exc, JevError) else type(exc).__name__
                    answers = None
                else:
                    rec["latency_s"] = round(time.monotonic() - t0, 4)
                    rec["cost_usd"] = usage["cost"]
                    spent += usage["cost"]
                    rec["source"] = "jev"
                    write_json_atomic(cached_path, {"model": JEV_MODEL, "questions_version": QUESTIONS_VERSION,
                                                    "served_by": usage.get("model"), "answers": answers,
                                                    "usage": {"cost": usage["cost"], "input_tokens": usage["input_tokens"]}})
        if answers is not None:
            p = _p_keep(answers)
            rec["p_keep"] = p
            rec["answers"] = _compact(answers)
            if p <= settings.reject_below:
                rec["decision"] = "reject"
            elif p >= settings.keep_above:
                rec["decision"] = "keep_high"
            else:
                rec["decision"] = "keep_review"
        records.append(rec)

    by_id = {r["video_id"]: r for r in records}
    survivors = [c for c in candidates if by_id[str(c.get("video_id") or "")]["decision"] != "reject"]
    if settings.order_by_p:
        # Scored survivors first by P(keep) desc; fallbacks keep discovery order after them.
        survivors.sort(key=lambda c: (by_id[str(c.get("video_id") or "")]["p_keep"] is None,
                                      -(by_id[str(c.get("video_id") or "")]["p_keep"] or 0.0)))
    counts: dict[str, int] = {}
    for r in records:
        counts[r["decision"]] = counts.get(r["decision"], 0) + 1
    sources: dict[str, int] = {}
    for r in records:
        sources[str(r["source"])] = sources.get(str(r["source"]), 0) + 1
    report = {
        "schema_version": SCHEMA_VERSION,
        "model": JEV_MODEL,
        "questions_version": QUESTIONS_VERSION,
        "settings": {"reject_below": settings.reject_below, "keep_above": settings.keep_above,
                     "timeout_s": settings.timeout_s, "max_usd_per_run": settings.max_usd_per_run,
                     "order_by_p": settings.order_by_p},
        "theme": theme,
        "counts": {"input": len(candidates), "kept": len(survivors), "by_decision": counts, "by_source": sources},
        "cost_usd": round(spent, 8),
        "candidates": records,
    }
    if not pre_path.is_file():
        write_json_atomic(pre_path, candidates)
    write_json_atomic(run_dir / "jev_gate.json", report)
    write_json_atomic(cand_path, survivors)
    return {"run_dir": str(run_dir), "enabled": True, "changed": True, "input": len(candidates),
            "kept": len(survivors), "by_decision": counts, "by_source": sources, "cost_usd": round(spent, 8),
            "jev_gate_path": str(run_dir / "jev_gate.json")}
