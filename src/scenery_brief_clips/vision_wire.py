"""Swappable picture labeler.

vision.yaml names the backend and model. The program does not look at
pictures until a person confirms that choice. Secrets never live in the file.
"""

from __future__ import annotations

import base64
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from scenery_brief_clips.config import _parse_scalar

CODEX_LOGIN = "codex-login"
OPENAI_API = "openai-api"
BACKENDS = {CODEX_LOGIN, OPENAI_API}
CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
TILE_LABELS = {"keep", "reject", "uncertain"}
TILE_LOOKS = {"europe_like", "other_landscape", "not_nature"}
STRIP_MATCH = {"keep", "reject", "uncertain"}
STRIP_GEO = {"supported", "uncertain", "conflicting"}

TILE_PROMPT = (
    "Look at this picture. Reply with one JSON object only, no markdown. "
    "Fields: label (keep, reject, or uncertain), look (europe_like, "
    "other_landscape, or not_nature), note (one short sentence). "
    "keep means the requested outdoor scene or activity is visible and central; "
    "for a scenery brief, mountains, water, forest, coast, or fields are matches. "
    "By default reject people as the subject, maps, title cards, towns or cities "
    "as the subject, castles as the subject, or indoor scenes. But when the "
    "brief asks for people or machines doing something, their visible requested "
    "activity is a match, not a reason to reject. A small corner watermark "
    "is not enough to reject."
)
STRIP_PROMPT = (
    "This strip is frames left to right over a few seconds. Reply with one "
    "JSON object only, no markdown. Fields: match (keep, reject, or uncertain), "
    "geo (supported, uncertain, or conflicting), scene_type (one short word), "
    "note (one short sentence), continuity_ok (true or false). "
    "Reject when a cut, dissolve, title, or town fills the moment, even if "
    "most frames match. geo supported only when the place is recognizable. "
    "Set continuity_ok true only when the strip is one continuous scene."
)


class VisionWireError(ValueError):
    pass


@dataclass(frozen=True)
class VisionWire:
    backend: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    path: str = ""

    def as_public_dict(self) -> dict:
        payload = {
            "backend": self.backend,
            "model": self.model,
            "plain": plain_description(self),
            "file": self.path,
        }
        if self.backend == OPENAI_API:
            payload["base_url"] = self.base_url
            payload["api_key_env"] = self.api_key_env
        return payload


def vision_yaml_path(root: str | Path) -> Path:
    return Path(root).resolve() / "vision.yaml"


def load_vision_wire(root: str | Path) -> VisionWire:
    path = vision_yaml_path(root)
    if not path.is_file():
        raise VisionWireError(
            f"vision.yaml is missing at {path}. That file is the switch for which model looks at pictures."
        )
    parsed: dict = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise VisionWireError(f"invalid vision.yaml at line {line_number}")
        key, value = line.split(":", 1)
        key = key.strip()
        if key in parsed:
            raise VisionWireError(f"duplicate vision.yaml key: {key}")
        if key in {"api_key", "token", "password", "secret"}:
            raise VisionWireError("do not put a secret in vision.yaml; set api_key_env to the variable name")
        parsed[key] = _parse_scalar(value)
    return _validate_wire(parsed, path)


def _validate_wire(parsed: dict, path: Path) -> VisionWire:
    allowed = {"backend", "model", "base_url", "api_key_env"}
    unknown = sorted(set(parsed) - allowed)
    if unknown:
        raise VisionWireError(f"unknown vision.yaml key: {unknown[0]}")
    backend = parsed.get("backend")
    model = parsed.get("model")
    if backend not in BACKENDS:
        raise VisionWireError(
            f"vision backend must be {CODEX_LOGIN} or {OPENAI_API}, not {backend!r}"
        )
    if not isinstance(model, str) or not model.strip():
        raise VisionWireError("vision model must be a non-empty name")
    if _looks_like_secret(model):
        raise VisionWireError("vision model looks like a secret; put a model name there")
    base_url = str(parsed.get("base_url") or "")
    api_key_env = str(parsed.get("api_key_env") or "")
    if backend == OPENAI_API:
        if not base_url.startswith("https://"):
            raise VisionWireError("openai-api base_url must start with https://")
        if not api_key_env or not api_key_env.replace("_", "").isalnum():
            raise VisionWireError("openai-api api_key_env must be an environment variable name")
        if _looks_like_secret(api_key_env):
            raise VisionWireError("api_key_env must be the variable name, not the key")
    elif base_url or api_key_env:
        raise VisionWireError("base_url and api_key_env are only used when backend is openai-api")
    return VisionWire(
        backend=backend,
        model=model.strip(),
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        path=str(path),
    )


def _looks_like_secret(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith("sk-") or lowered.startswith("sk_") or "bearer " in lowered


def plain_description(wire: VisionWire) -> str:
    if wire.backend == CODEX_LOGIN:
        return (
            f"Vision is set to the ChatGPT/Codex sign-in, model {wire.model}. "
            "That is a login, not a paid API key. "
            "To use a different model or a real API, edit vision.yaml."
        )
    return (
        f"Vision is set to an API at {wire.base_url}, model {wire.model}. "
        f"The key is read from the environment variable {wire.api_key_env}, not from vision.yaml. "
        "To change the model or the service, edit vision.yaml."
    )


def parse_model_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise VisionWireError("the model did not return a JSON object")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise VisionWireError("the model returned text that is not JSON") from exc
    if not isinstance(payload, dict):
        raise VisionWireError("the model JSON must be an object")
    return payload


def normalize_tile_label(payload: dict) -> dict:
    label = payload.get("label")
    look = payload.get("look")
    note = payload.get("note")
    if label not in TILE_LABELS:
        raise VisionWireError(f"tile label must be keep, reject, or uncertain, not {label!r}")
    if look not in TILE_LOOKS:
        raise VisionWireError(f"tile look must be europe_like, other_landscape, or not_nature, not {look!r}")
    if not isinstance(note, str) or not note.strip():
        raise VisionWireError("tile note must be a short sentence")
    return {"label": label, "look": look, "note": note.strip()}


def normalize_strip_label(payload: dict) -> dict:
    match = payload.get("match")
    geo = payload.get("geo")
    scene_type = payload.get("scene_type")
    note = payload.get("note")
    continuity_ok = payload.get("continuity_ok", False)
    if match not in STRIP_MATCH:
        raise VisionWireError(f"strip match must be keep, reject, or uncertain, not {match!r}")
    if geo not in STRIP_GEO:
        raise VisionWireError(f"strip geo must be supported, uncertain, or conflicting, not {geo!r}")
    if not isinstance(scene_type, str) or not scene_type.strip():
        raise VisionWireError("strip scene_type must be non-empty text")
    if not isinstance(note, str) or not note.strip():
        raise VisionWireError("strip note must be a short sentence")
    if not isinstance(continuity_ok, bool):
        raise VisionWireError("strip continuity_ok must be true or false")
    result = {
        "match": match,
        "geo": geo,
        "scene_type": scene_type.strip(),
        "note": note.strip(),
    }
    if continuity_ok:
        result["continuity_ok"] = True
        if not result["note"].lower().startswith("continuity_ok:"):
            result["note"] = f"continuity_ok: {result['note']}"
    return result


def data_url_for_image(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
    elif raw.startswith(b"\x89PNG"):
        mime = "image/png"
    else:
        raise VisionWireError(f"unsupported image type: {path}")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def call_wired_vision(wire: VisionWire, image_path: str | Path, prompt: str) -> str:
    if wire.backend == CODEX_LOGIN:
        return _call_codex_login(wire, image_path, prompt)
    if wire.backend == OPENAI_API:
        return _call_openai_api(wire, image_path, prompt)
    raise VisionWireError(f"unknown vision backend: {wire.backend}")


def _call_codex_login(wire: VisionWire, image_path: str | Path, prompt: str) -> str:
    token = _read_codex_token()
    if not token:
        raise VisionWireError(
            "ChatGPT/Codex sign-in was not found. Run hermes auth, or switch vision.yaml to openai-api."
        )
    body = {
        "model": wire.model,
        "store": False,
        "instructions": "You label scenery pictures. Reply with one JSON object only.",
        "stream": True,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url_for_image(image_path)},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "scenery-brief-clips/0.1",
        "originator": "hermes-agent",
    }
    account = _chatgpt_account_id(token)
    if account:
        headers["ChatGPT-Account-ID"] = account
    return _post_json(CODEX_URL, headers, body)


def _call_openai_api(wire: VisionWire, image_path: str | Path, prompt: str) -> str:
    key = os.environ.get(wire.api_key_env, "").strip()
    if not key:
        raise VisionWireError(
            f"environment variable {wire.api_key_env} is empty. "
            "Set that variable, or change vision.yaml."
        )
    body = {
        "model": wire.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url_for_image(image_path)}},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    return _post_json(f"{wire.base_url}/chat/completions", headers, body)


def _post_json(url: str, headers: dict[str, str], body: dict) -> str:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8", errors="replace")
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise VisionWireError(f"vision call failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise VisionWireError(f"vision call could not connect: {exc.reason}") from exc
    text = _text_from_response(raw, content_type)
    if not text.strip():
        raise VisionWireError("vision call returned no text")
    return text


def _text_from_response(raw: str, content_type: str) -> str:
    if "text/event-stream" in content_type or raw.lstrip().startswith("data:") or "\ndata:" in raw:
        streamed = _text_from_sse(raw)
        if streamed.strip():
            return streamed
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _text_from_sse(raw)
    if isinstance(payload, dict):
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text
        choices = payload.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
    return _text_from_sse(raw)


def _text_from_sse(raw: str) -> str:
    chunks: list[str] = []
    for block in raw.split("\n\n"):
        data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            chunks.append(event["delta"])
        elif event_type == "response.output_text.done" and isinstance(event.get("text"), str) and not chunks:
            chunks.append(event["text"])
    return "".join(chunks)


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _read_codex_token() -> str:
    path = _hermes_home() / "auth.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    providers = data.get("providers") if isinstance(data, dict) else None
    codex = providers.get("openai-codex") if isinstance(providers, dict) else None
    tokens = codex.get("tokens") if isinstance(codex, dict) else None
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    return token.strip() if isinstance(token, str) else ""


def _chatgpt_account_id(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        account = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    except Exception:
        return ""
    return account if isinstance(account, str) else ""


def brief_prompt(kind: str, theme: str | None) -> str:
    base = TILE_PROMPT if kind == "tile" else STRIP_PROMPT
    brief = (theme or "").strip()
    if not brief:
        return base
    return (
        f"The user's brief is: {brief}. "
        "Judge the image against that brief; do not apply a generic scenery "
        "rule when the brief asks for an activity. Keep people or machines "
        "actively doing the requested action when that action is visible and "
        "central. Reject incidental people, conversation, or equipment that "
        "does not show the requested action. "
        + base
    )


def _theme_from_run(run_dir: Path) -> str | None:
    path = run_dir / "constraint.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    theme = payload.get("theme_text") if isinstance(payload, dict) else None
    return theme.strip() if isinstance(theme, str) and theme.strip() else None


def label_image(
    wire: VisionWire,
    image_path: str | Path,
    kind: str,
    caller=None,
    theme: str | None = None,
) -> dict:
    prompt = brief_prompt(kind, theme)
    caller = caller or call_wired_vision
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            text = caller(wire, image_path, prompt)
            payload = parse_model_json(text)
            break
        except VisionWireError as exc:
            last_error = exc
    else:
        raise last_error or VisionWireError("vision call failed")
    if kind == "tile":
        return normalize_tile_label(payload)
    if kind == "strip":
        return normalize_strip_label(payload)
    raise VisionWireError(f"unknown label kind: {kind}")


def label_ranked_tiles(run_dir: str | Path, wire: VisionWire, caller=None) -> dict:
    ranked_path = Path(run_dir) / "ranked.json"
    if not ranked_path.is_file():
        raise VisionWireError(f"missing ranked.json in {run_dir}")
    ranked = json.loads(ranked_path.read_text(encoding="utf-8"))
    if not isinstance(ranked, list):
        raise VisionWireError("ranked.json must be a list")
    theme = _theme_from_run(Path(run_dir))
    scores: dict[str, list] = {}
    failures: list[dict] = []
    pending_rows: list[tuple[str, list[tuple[dict, Future[dict] | None]]]] = []
    # Two independent I/O-bound model calls can overlap without unbounded bursts.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for row in ranked:
            if not isinstance(row, dict):
                continue
            video_id = str(row.get("video_id") or "")
            pending: list[tuple[dict, Future[dict] | None]] = []
            for tile in row.get("tiles") or []:
                if not isinstance(tile, dict):
                    continue
                path = tile.get("path")
                record = {"t_s": tile.get("t_s")}
                if path:
                    record["path"] = str(path)
                if tile.get("dark") is True:
                    record.update(
                        {
                            "label": "reject",
                            "look": "not_nature",
                            "note": "ranker marked this tile dark; not sent to the vision model",
                        }
                    )
                    pending.append((record, None))
                    continue
                if path:
                    pending.append((record, pool.submit(label_image, wire, path, "tile", caller, theme)))
            pending_rows.append((video_id, pending))
        for video_id, pending in pending_rows:
            entries: list[dict] = []
            for record, future in pending:
                if future is not None:
                    try:
                        record.update(future.result())
                    except VisionWireError as exc:
                        failures.append({"path": record["path"], "error": str(exc)})
                        continue
                entries.append(record)
            if video_id:
                scores[video_id] = entries
    return {"scores": scores, "failures": failures}


def label_review_strips(run_dir: str | Path, wire: VisionWire, caller=None) -> dict:
    run_dir = Path(run_dir)
    review_path = run_dir / "review.json"
    if not review_path.is_file():
        raise VisionWireError(f"missing review.json in {run_dir}")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if not isinstance(review, dict):
        raise VisionWireError("review.json must be an object")
    scores: dict[str, list] = {}
    failures: list[dict] = []
    binding = review.get("excerpts_sha256")
    theme = _theme_from_run(run_dir)
    for moment in review.get("moments") or []:
        if not isinstance(moment, dict):
            continue
        video_id = str(moment.get("video_id") or "")
        strip = moment.get("strip")
        if not video_id or not strip:
            continue
        strip_path = Path(str(strip))
        if not strip_path.is_absolute():
            strip_path = run_dir / strip_path
        prompt_kind = "strip"
        try:
            labeled = label_image(wire, strip_path, prompt_kind, caller, theme)
        except VisionWireError as exc:
            failures.append({"path": str(strip_path), "error": str(exc)})
            continue
        if moment.get("continuity_suspect") and not labeled.get("continuity_ok"):
            labeled["note"] = f"continuity not cleared: {labeled['note']}"
        entry = {"excerpt_index": int(moment.get("excerpt_index") or 0), **labeled}
        scores.setdefault(video_id, []).append(entry)
    payload = dict(scores)
    if isinstance(binding, str):
        payload = {"excerpts_sha256": binding, **payload}
    return {"scores": payload, "failures": failures}
