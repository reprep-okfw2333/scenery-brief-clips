"""Ask Jev (typesafe/jev-1.13, OpenRouter Decisions API) the eval questions.

Usage: .venv/bin/python benchmark/jev_eval/run_jev.py {sources-flat|sources-full|strips} [--limit N]

The key is read from OPENROUTER_API_KEY, else from ~/.hermes/.env, into this
process only; it is never printed or written. Every answer is cached under
benchmark/jev_eval/cache/ (keyed by model + questions + state), so reruns are
free and deterministic. Spend stops at MAX_USD per invocation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from scenery_brief_clips.jev_gate import JEV_URL, JevError, default_post  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = HERE / "cache"
MODEL = "typesafe/jev-1.13"
MAX_USD = 0.10
WORKERS = 4

SOURCE_Q_VERSION = "jev_eval_sources_v1"
SOURCE_QUESTIONS = {
    "subject": {
        "type": "noul",
        "instructions": "Does this video show the brief's subject (the same kind or species, not a look-alike or related kind) doing the brief's action for a meaningful share of its runtime?",
        "criteria": {
            "true": "The requested subject and action are likely on screen prominently.",
            "false": "A different subject or species, or the subject is only incidental, or not on screen at all.",
        },
    },
    "place": {
        "type": "choice",
        "instructions": "Where was this footage filmed, compared with the place the brief asks for? If the brief names no place, answer matches.",
        "criteria": {
            "matches": "Filmed in the brief's place, or the brief names no place.",
            "different": "Filmed in a clearly different place or region than the brief asks for.",
            "unknown": "The metadata does not say where it was filmed.",
        },
    },
    "kind": {
        "type": "choice",
        "instructions": "What kind of footage does this video mostly contain?",
        "criteria": {
            "exterior_scene": "Camera footage of the scene itself: tripod, handheld, or drone shots of the subject and landscape.",
            "vehicle_pov": "Filmed from a moving vehicle: dashcam, driving POV, train or plane window.",
            "walking_pov": "First-person walking tour through streets or trails.",
            "vlog_or_documentary": "Presenter vlog, travel show, documentary, explainer, or itinerary.",
            "ambience_or_dark_screen": "Sleep or ambience audio with a dark or black screen, or a still image.",
            "compilation_or_slideshow": "Compilation or slideshow of other people's clips or photos, top-N lists.",
            "ai_or_cgi": "AI-generated, CGI, animation, or video game.",
            "other": "Something else.",
        },
    },
    "conditions": {
        "type": "noul",
        "instructions": "Does the footage likely match the brief's requested setting, time of day, and weather (for example sunset, night, mist, storm)? If the brief requests none, answer true.",
        "criteria": {
            "true": "The requested conditions are likely shown, or none are requested.",
            "false": "The footage is likely in other conditions (for example daytime when sunset or night is asked).",
        },
    },
    "usable": {
        "type": "noul",
        "instructions": "Is this video worth downloading short sections of, to find clean 4 to 30 second clips that match the request?",
        "criteria": {
            "true": "Real camera footage with the requested subject clearly visible, in the requested place and conditions, mostly continuous shots without presenters, dark screens, or heavy text over the picture.",
            "false": "Dark or black screen, audio-only, a different subject or species, a clearly different place, a presenter vlog or explainer where the subject is incidental, AI or CGI, or unlikely to contain clean usable shots.",
        },
    },
}

# v2: written from the v1 misses (2026-09-29), not fitted: v1 lumped sleep/relaxation
# films that show the real scene with dark-screen audio videos (a "Sleep ... Ocean
# Sounds" video gave 2 good clips), and its usable criteria did not say that long
# relaxation films are fine.
SOURCE_QUESTIONS_V2 = json.loads(json.dumps(SOURCE_QUESTIONS))
SOURCE_QUESTIONS_V2["kind"]["criteria"] = {
    "exterior_scene": "Camera footage of the scene itself: tripod, handheld, or drone shots of the subject and landscape.",
    "relaxation_film": "Long relaxation, sleep, or ambience film that shows real moving footage of the scene.",
    "vehicle_pov": "Filmed from a moving vehicle: dashcam, driving POV, train or plane window.",
    "walking_pov": "First-person walking tour through streets or trails.",
    "vlog_or_documentary": "Presenter vlog, travel show, documentary, explainer, or itinerary.",
    "dark_or_still_screen": "Audio-only, black or dark screen, or a single still image with sound.",
    "compilation_or_slideshow": "Compilation or slideshow of other people's clips or photos, top-N lists.",
    "ai_or_cgi": "AI-generated, CGI, animation, or video game.",
    "other": "Something else.",
}
SOURCE_QUESTIONS_V2["usable"]["criteria"] = {
    "true": "Real moving camera footage in which the requested subject is what the camera shows, in the requested place and conditions. Long relaxation or ambience films of the real scene are fine.",
    "false": "Audio-only, dark or still screen, a different subject or species, a clearly different place, a presenter vlog or explainer where the subject is incidental, AI or CGI, or unlikely to contain clean usable shots.",
}
QUESTION_SETS = {"v1": (SOURCE_QUESTIONS, SOURCE_Q_VERSION), "v2": (SOURCE_QUESTIONS_V2, "jev_eval_sources_v2")}
SNIPPET_CHARS = 150  # flat search hits carry a description snippet of about this length

STRIP_Q_VERSION = "jev_eval_strips_v1"
STRIP_QUESTIONS = {
    "violation": {
        "type": "noul",
        "instructions": "Does the vision model's note report that this moment fails part of the request?",
        "criteria": {
            "true": "The note says the moment shows a wrong subject or species, a wrong action, the wrong time of day or weather for what was asked, a place that contradicts the requested place, mostly people or foreground objects instead of the subject, a view through a vehicle window or windshield, prominent text or a watermark, or a cut or title.",
            "false": "The note describes the requested scene; saying only that the place is not clearly recognizable or generic is not a failure.",
        },
    },
}


def load_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    env = Path.home() / ".hermes" / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.strip().partition("=")
            if name.strip().removeprefix("export ").strip() == "OPENROUTER_API_KEY":
                return value.strip().strip("'\"")
    return ""


def brief_state(brief: dict) -> dict:
    scene = brief.get("scene") or {}
    return {
        "request": brief.get("request_text"),
        "subjects": [s.get("noun") for s in scene.get("subjects") or []],
        "action": scene.get("action"),
        "setting": scene.get("setting"),
        "place": brief.get("geography"),
        "must_not_show": scene.get("excluded") or [],
    }


def source_state(brief: dict, meta: dict, variant: str) -> dict:
    video = {"title": meta["title"], "channel": meta["channel"], "duration_s": meta["duration_s"], "views": meta["views"]}
    if variant == "snippet":
        video["description"] = meta["description"][:SNIPPET_CHARS]
    if variant == "full":
        video.update({k: meta[k] for k in ("upload_date", "best_resolution", "tags", "categories", "chapters", "description")})
    return {
        "task": "Pre-download triage of a YouTube search result for a request for short footage clips. "
                "Only metadata is available; nothing has been watched yet.",
        "brief": brief_state(brief),
        "video": video,
    }


def strip_state(brief: dict, strip: dict) -> dict:
    return {
        "task": "A vision model looked at frames from a few seconds of a downloaded video and wrote a note. "
                "Judge the note against the request.",
        "brief": brief_state(brief),
        "vision": {"scene_type": strip["scene_type"], "geo": strip["geo"], "note": strip["note"]},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["sources-flat", "sources-snippet", "sources-full", "strips"])
    ap.add_argument("--qv", choices=sorted(QUESTION_SETS), default="v1")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    briefs = json.loads((DATA / "briefs.json").read_text())
    if args.task == "strips":
        items = json.loads((DATA / "strips.json").read_text())
        questions, qv = STRIP_QUESTIONS, STRIP_Q_VERSION
        jobs = [(f"{s['brief']}:{s['video_id']}:{s['excerpt_index']}:{i}", strip_state(briefs[s["brief"]], s))
                for i, s in enumerate(items)]
    else:
        variant = args.task.split("-")[1]
        items = json.loads((DATA / "sources.json").read_text())
        questions, qv = QUESTION_SETS[args.qv]
        qv = f"{qv}_{variant}"
        jobs = [(f"{s['brief']}:{s['video_id']}", source_state(briefs[s["brief"]], s["meta"], variant)) for s in items]
    if args.limit:
        jobs = jobs[: args.limit]
    key = load_key()
    CACHE.mkdir(exist_ok=True)
    spent = [0.0]
    lock = threading.Lock()

    def ask(job):
        item_id, state = job
        digest = hashlib.sha256(json.dumps({"m": MODEL, "q": questions, "s": state}, sort_keys=True).encode()).hexdigest()
        path = CACHE / f"{digest}.json"
        if path.is_file():
            return {"id": item_id, **json.loads(path.read_text()), "cache_hit": True}
        with lock:
            if spent[0] >= MAX_USD:
                return {"id": item_id, "error": "budget"}
        if not key:
            return {"id": item_id, "error": "no_key"}
        body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        t0 = time.monotonic()
        try:
            data = default_post(JEV_URL, body, headers, 30.0)
        except JevError as exc:
            return {"id": item_id, "error": str(exc)}
        latency = time.monotonic() - t0
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            return {"id": item_id, "error": "no answers"}
        usage = data.get("usage") or {}
        rec = {"answers": answers, "cost": float(usage.get("cost") or 0.0),
               "input_tokens": usage.get("input_tokens"), "latency_s": round(latency, 3), "served_by": data.get("model")}
        with lock:
            spent[0] += rec["cost"]
        path.write_text(json.dumps(rec))
        return {"id": item_id, **rec, "cache_hit": False}

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(ask, jobs))
    out = DATA / (f"jev_{args.task}.json" if args.task == "strips" or args.qv == "v1" else f"jev_{args.task}-{args.qv}.json")
    out.write_text(json.dumps({"model": MODEL, "questions_version": qv, "questions": questions, "results": results}, indent=1))
    errors = [r for r in results if r.get("error")]
    fresh = [r for r in results if not r.get("error") and not r.get("cache_hit")]
    lat = sorted(r["latency_s"] for r in fresh)
    print(json.dumps({
        "task": args.task, "n": len(results), "errors": len(errors),
        "error_kinds": sorted({r["error"] for r in errors}),
        "new_calls": len(fresh), "cost_usd_new": round(spent[0], 6),
        "latency_median_s": lat[len(lat) // 2] if lat else None,
        "latency_p95_s": lat[int(len(lat) * 0.95)] if lat else None,
        "tokens_median": sorted(r.get("input_tokens") or 0 for r in fresh)[len(fresh) // 2] if fresh else None,
        "out": str(out.relative_to(ROOT)),
    }))
    if fresh:
        print("sample answer:", json.dumps(fresh[0]["answers"])[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
