"""Jev typed decisions for discovery ranking and vision-note checks (optional, off by default).

Jev (TypeSafe ``typesafe/jev-1.13``, OpenRouter Decisions API) takes a text ``state`` plus
typed ``questions`` and returns calibrated answers: ``noul`` (P(true)), ``choice`` (one of N
with probabilities) or ``score``. It has no image input and writes no text.

Two uses, each behind its own config switch (docs/JEV.md):

- ``jev_rank``: discovery asks the source questions about every search hit (title, channel,
  duration, views, description snippet) and fetches metadata in score order; after each
  metadata fetch it asks again with the full metadata and rejects the candidate when the
  score is at or below ``jev_reject_below``. Candidates are ordered by that score.
- ``jev_note_check``: after the vision model labels review strips, Jev reads each ``keep``
  note against the brief; P(violation) >= ``jev_note_reject_at`` marks the entry
  ``note_violation: true`` and the shortlist excludes it.

Safety: the key comes only from ``OPENROUTER_API_KEY``; it is never logged or written. Any
missing key, HTTP error, timeout, malformed answer or spent budget falls back (search order,
candidate kept, entry unchanged) and the fallback source is recorded. Answers are cached
under data/cache/jev keyed by model + questions + state, so a rerun is free and repeatable.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from scenery_brief_clips.store import write_json_atomic

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"
API_KEY_ENV = "OPENROUTER_API_KEY"
SNIPPET_MAX_CHARS = 300
DESCRIPTION_MAX_CHARS = 1200
TAGS_MAX = 25
CHAPTERS_MAX = 40
NEUTRAL_SCORE = 0.5  # rank position for a hit or candidate Jev did not answer

PostFn = Callable[[str, bytes, dict, float], dict]

# Question set v2 (benchmark/jev_eval, 2026-09-29): written from the v1 misses, not fitted.
# The ranking score is the mean of the usable, subject and conditions probabilities
# (source_score); the kind and place answers are recorded for inspection only.
SOURCE_QUESTIONS_VERSION = "jev_source_q_v2"
SOURCE_QUESTIONS: dict = {
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
            "relaxation_film": "Long relaxation, sleep, or ambience film that shows real moving footage of the scene.",
            "vehicle_pov": "Filmed from a moving vehicle: dashcam, driving POV, train or plane window.",
            "walking_pov": "First-person walking tour through streets or trails.",
            "vlog_or_documentary": "Presenter vlog, travel show, documentary, explainer, or itinerary.",
            "dark_or_still_screen": "Audio-only, black or dark screen, or a single still image with sound.",
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
            "true": "Real moving camera footage in which the requested subject is what the camera shows, in the requested place and conditions. Long relaxation or ambience films of the real scene are fine.",
            "false": "Audio-only, dark or still screen, a different subject or species, a clearly different place, a presenter vlog or explainer where the subject is incidental, AI or CGI, or unlikely to contain clean usable shots.",
        },
    },
}
SCORE_QUESTIONS = ("usable", "subject", "conditions")

NOTE_QUESTIONS_VERSION = "jev_note_q_v1"
NOTE_QUESTIONS: dict = {
    "violation": {
        "type": "noul",
        "instructions": "Does the vision model's note report that this moment fails part of the request?",
        "criteria": {
            "true": "The note says the moment shows a wrong subject or species, a wrong action, the wrong time of day or weather for what was asked, a place that contradicts the requested place, mostly people or foreground objects instead of the subject, a view through a vehicle window or windshield, prominent text or a watermark, or a cut or title.",
            "false": "The note describes the requested scene; saying only that the place is not clearly recognizable or generic is not a failure.",
        },
    },
}


class JevError(RuntimeError):
    pass


@dataclass(frozen=True)
class JevSettings:
    rank: bool = False
    note_check: bool = False
    reject_below: float = 0.35
    note_reject_at: float = 0.70
    timeout_s: float = 20.0
    max_usd_per_run: float = 0.25
    workers: int = 4

    @classmethod
    def from_config(cls, config: Mapping) -> "JevSettings":
        return cls(
            rank=bool(config.get("jev_rank", False)),
            note_check=bool(config.get("jev_note_check", False)),
            reject_below=float(config.get("jev_reject_below", 0.35)),
            note_reject_at=float(config.get("jev_note_reject_at", 0.70)),
            timeout_s=float(config.get("jev_timeout_s", 20.0)),
            max_usd_per_run=float(config.get("jev_max_usd_per_run", 0.25)),
        )

    def rank_binding(self) -> dict | None:
        """What discovery results depend on; None when ranking is off (binding unchanged)."""
        if not self.rank:
            return None
        return {"model": JEV_MODEL, "questions": SOURCE_QUESTIONS_VERSION, "reject_below": self.reject_below}

    def note_binding(self) -> dict | None:
        if not self.note_check:
            return None
        return {"model": JEV_MODEL, "questions": NOTE_QUESTIONS_VERSION, "reject_at": self.note_reject_at}


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


def _check_answers(answers, questions: dict) -> dict:
    if not isinstance(answers, dict):
        raise JevError("response has no answers")
    for name, spec in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise JevError(f"missing answer {name}")
        if spec["type"] == "noul":
            value = answer.get("noul")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise JevError(f"invalid noul for {name}")
        elif spec["type"] == "choice" and answer.get("choice") not in spec["criteria"]:
            raise JevError(f"invalid choice for {name}")
    return answers


class JevClient:
    """Cached, budgeted, concurrent Jev requests. ``post=None`` means no network."""

    def __init__(self, api_key: str, cache_dir: Path, settings: JevSettings, post: PostFn | None) -> None:
        self._api_key = api_key
        self._cache_dir = Path(cache_dir)
        self._settings = settings
        self._post = post
        self._lock = threading.Lock()
        self.spent_usd = 0.0
        self.calls = 0

    def cache_key(self, state: dict, questions: dict) -> str:
        payload = json.dumps({"model": JEV_MODEL, "questions": questions, "state": state},
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def ask(self, state: dict, questions: dict) -> dict:
        """{"answers": dict|None, "source": str, "cost_usd", "latency_s"[, "error"]}."""
        key = self.cache_key(state, questions)
        path = self._cache_dir / f"{key}.json"
        if path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                return {"answers": _check_answers(cached["answers"], questions), "source": "jev_cache",
                        "cost_usd": 0.0, "latency_s": None}
            except (OSError, ValueError, KeyError, JevError):
                pass
        if self._post is None:
            return {"answers": None, "source": "fallback_no_port", "cost_usd": 0.0, "latency_s": None}
        if not self._api_key:
            return {"answers": None, "source": "fallback_no_key", "cost_usd": 0.0, "latency_s": None}
        with self._lock:
            if self.spent_usd >= self._settings.max_usd_per_run:
                return {"answers": None, "source": "fallback_budget", "cost_usd": 0.0, "latency_s": None}
        body = json.dumps({"model": JEV_MODEL, "state": state, "questions": questions}).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        started = time.monotonic()
        try:
            data = self._post(JEV_URL, body, headers, self._settings.timeout_s)
            if not isinstance(data, dict):
                raise JevError("response is not an object")
            answers = _check_answers(data.get("answers"), questions)
        except Exception as exc:  # noqa: BLE001 - any failure falls back
            error = str(exc)[:200] if isinstance(exc, JevError) else type(exc).__name__
            return {"answers": None, "source": "fallback_error", "cost_usd": 0.0, "latency_s": None, "error": error}
        latency = round(time.monotonic() - started, 4)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = float(usage.get("cost") or 0.0)
        with self._lock:
            self.spent_usd += cost
            self.calls += 1
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"model": JEV_MODEL, "served_by": data.get("model"), "answers": answers,
                                 "usage": {"cost": cost, "input_tokens": usage.get("input_tokens")}})
        return {"answers": answers, "source": "jev", "cost_usd": cost, "latency_s": latency}

    def ask_many(self, jobs: list[tuple[dict, dict]]) -> list[dict]:
        if not jobs:
            return []
        with ThreadPoolExecutor(max(1, self._settings.workers)) as pool:
            return list(pool.map(lambda job: self.ask(*job), jobs))


def brief_context(brief: Mapping) -> dict:
    scene = brief.get("scene") or {}
    return {
        "request": brief.get("request_text"),
        "subjects": [s.get("noun") for s in scene.get("subjects") or [] if isinstance(s, dict)],
        "action": scene.get("action"),
        "setting": scene.get("setting"),
        "place": brief.get("geography"),
        "must_not_show": list(scene.get("excluded") or []),
    }


def _source_task() -> str:
    return ("Pre-download triage of a YouTube search result for a request for short footage clips. "
            "Only metadata is available; nothing has been watched yet.")


def hit_state(brief: Mapping, hit: Mapping) -> dict:
    """State for a flat search hit (what --flat-playlist returns, before any metadata fetch)."""
    snippet = str(hit.get("description") or "").strip()[:SNIPPET_MAX_CHARS]
    return {
        "task": _source_task(),
        "brief": brief_context(brief),
        "video": {
            "title": hit.get("title"),
            "channel": hit.get("channel") or hit.get("uploader"),
            "duration_s": hit.get("duration"),
            "views": hit.get("view_count"),
            "description": snippet,
        },
    }


def metadata_state(brief: Mapping, info: Mapping) -> dict:
    """State for a fetched metadata record (full description, tags, chapters)."""
    description = str(info.get("description") or "").strip()
    if len(description) > DESCRIPTION_MAX_CHARS:
        description = description[:DESCRIPTION_MAX_CHARS] + " …"
    formats = [f for f in (info.get("formats") or [])
               if isinstance(f, dict) and (f.get("vcodec") or "none") != "none" and isinstance(f.get("height"), (int, float))]
    best = max(formats, key=lambda f: (f.get("height") or 0, f.get("width") or 0), default=None)
    return {
        "task": _source_task(),
        "brief": brief_context(brief),
        "video": {
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration_s": info.get("duration"),
            "views": info.get("view_count"),
            "upload_date": info.get("upload_date"),
            "best_resolution": f"{best.get('width')}x{best.get('height')}" if best else None,
            "tags": list(info.get("tags") or [])[:TAGS_MAX],
            "categories": info.get("categories"),
            "chapters": [c.get("title") for c in info.get("chapters") or [] if isinstance(c, dict)][:CHAPTERS_MAX],
            "description": description,
        },
    }


def source_score(answers: Mapping) -> float:
    return round(sum(float(answers[q]["noul"]) for q in SCORE_QUESTIONS) / len(SCORE_QUESTIONS), 4)


def compact(answers: Mapping) -> dict:
    out: dict = {}
    for name, ans in answers.items():
        if not isinstance(ans, dict):
            continue
        if ans.get("type") == "noul":
            out[name] = ans.get("noul")
        elif ans.get("type") in {"choice", "score"}:
            out[name] = {k: ans.get(k) for k in (ans.get("type"), "confidence", "probabilities")}
    return out


class DiscoveryJudge:
    """Hooks run_dry calls when jev_rank is on; also builds the discovery.json record."""

    def __init__(self, brief: Mapping, client: JevClient, settings: JevSettings) -> None:
        self.brief = brief
        self.client = client
        self.settings = settings
        self.hits: list[dict] = []
        self.candidates: dict[str, dict] = {}

    def order_hits(self, ordered_ids: list[str], hits_by_id: Mapping[str, Mapping]) -> list[str]:
        jobs = [(hit_state(self.brief, hits_by_id.get(vid) or {"title": None}), SOURCE_QUESTIONS) for vid in ordered_ids]
        answered = self.client.ask_many(jobs)
        scores: dict[str, float] = {}
        for index, (vid, got) in enumerate(zip(ordered_ids, answered)):
            score = source_score(got["answers"]) if got["answers"] else None
            scores[vid] = NEUTRAL_SCORE if score is None else score
            self.hits.append({"video_id": vid, "title": (hits_by_id.get(vid) or {}).get("title"),
                              "search_index": index, "score": score, "source": got["source"],
                              "answers": compact(got["answers"]) if got["answers"] else None,
                              **({"error": got["error"]} if got.get("error") else {})})
        return sorted(ordered_ids, key=lambda vid: -scores[vid])  # stable: ties keep search order

    def judge_metadata(self, video_id: str, info: Mapping) -> bool:
        """True keeps the candidate; False rejects it (score <= reject_below)."""
        got = self.client.ask(metadata_state(self.brief, info), SOURCE_QUESTIONS)
        score = source_score(got["answers"]) if got["answers"] else None
        keep = score is None or score > self.settings.reject_below
        self.candidates[video_id] = {"video_id": video_id, "title": info.get("title"), "score": score,
                                     "source": got["source"], "decision": "keep" if keep else "reject",
                                     "answers": compact(got["answers"]) if got["answers"] else None,
                                     **({"error": got["error"]} if got.get("error") else {})}
        return keep

    def order_candidates(self, candidates: list) -> list:
        def key(candidate):
            score = (self.candidates.get(candidate.video_id) or {}).get("score")
            return -(NEUTRAL_SCORE if score is None else score)
        return sorted(candidates, key=key)

    def report(self) -> dict:
        sources: dict[str, int] = {}
        for rec in [*self.hits, *self.candidates.values()]:
            sources[rec["source"]] = sources.get(rec["source"], 0) + 1
        return {
            "schema_version": "jev_rank_v1",
            "model": JEV_MODEL,
            "questions_version": SOURCE_QUESTIONS_VERSION,
            "score": "mean of P(usable), P(subject), P(conditions)",
            "reject_below": self.settings.reject_below,
            "counts": {"hits": len(self.hits), "candidates_judged": len(self.candidates),
                       "rejected": sum(1 for r in self.candidates.values() if r["decision"] == "reject"),
                       "by_source": sources},
            "calls": self.client.calls,
            "cost_usd": round(self.client.spent_usd, 8),
            "hits": self.hits,
            "candidates": list(self.candidates.values()),
        }


def note_state(brief: Mapping, entry: Mapping) -> dict:
    return {
        "task": "A vision model looked at frames from a few seconds of a downloaded video and wrote a note. "
                "Judge the note against the request.",
        "brief": brief_context(brief),
        "vision": {"scene_type": entry.get("scene_type"), "geo": entry.get("geo"), "note": entry.get("note")},
    }


def apply_note_check(brief: Mapping, scores: dict, client: JevClient, settings: JevSettings) -> tuple[dict, dict]:
    """Annotate strip labels (shortlist_scores payload) whose match is keep.

    Each checked entry gets ``note_check`` ({p_violation, source}); ``note_violation: true``
    is set only when Jev answered and P(violation) >= note_reject_at. Unanswered entries
    are left without ``note_violation`` (fallback: the vision label stands).
    """
    targets = [(vid, entry) for vid, entries in scores.items() if isinstance(entries, list)
               for entry in entries if isinstance(entry, dict) and entry.get("match") == "keep"]
    answered = client.ask_many([(note_state(brief, entry), NOTE_QUESTIONS) for _, entry in targets])
    records = []
    for (vid, entry), got in zip(targets, answered):
        p = float(got["answers"]["violation"]["noul"]) if got["answers"] else None
        entry["note_check"] = {"p_violation": p, "source": got["source"]}
        if p is not None:
            entry["note_violation"] = p >= settings.note_reject_at
        records.append({"video_id": vid, "excerpt_index": entry.get("excerpt_index"), "p_violation": p,
                        "violation": entry.get("note_violation"), "source": got["source"],
                        **({"error": got["error"]} if got.get("error") else {})})
    report = {
        "schema_version": "jev_notes_v1",
        "model": JEV_MODEL,
        "questions_version": NOTE_QUESTIONS_VERSION,
        "reject_at": settings.note_reject_at,
        "counts": {"checked": len(records), "violations": sum(1 for r in records if r["violation"]),
                   "unanswered": sum(1 for r in records if r["p_violation"] is None)},
        "calls": client.calls,
        "cost_usd": round(client.spent_usd, 8),
        "entries": records,
    }
    return scores, report
