"""One bounded model call that turns a frozen brief into a query plan.

planner.yaml names the backend and model, exactly like vision.yaml does for
picture labeling. The planner is text-only: instruction in, one JSON plan
out. Ordinary code validates the plan; the model never rewrites the brief.
Secrets never live in the file or in the recorded provenance.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from scenery_brief_clips.brief import (
    canonical_json_hash,
    render_planner,
    validate_query_plan,
)
from scenery_brief_clips.config import _parse_scalar
from scenery_brief_clips.vision_wire import (
    CODEX_LOGIN,
    OPENAI_API,
    CODEX_URL,
    _post_json,
    _read_codex_token,
    parse_model_json,
)

PLANNER_INSTRUCTION_VERSION = "search_query_planner_v1"


class PlannerError(ValueError):
    pass


@dataclass(frozen=True)
class PlannerWire:
    backend: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    path: str = ""

    def as_public_dict(self) -> dict:
        payload = {
            "backend": self.backend,
            "model": self.model,
            "file": self.path,
        }
        if self.backend == OPENAI_API:
            payload["base_url"] = self.base_url
            payload["api_key_env"] = self.api_key_env
        return payload


def planner_yaml_path(root: str | Path) -> Path:
    return Path(root).resolve() / "planner.yaml"


def load_planner_wire(root: str | Path, config_path: str | Path | None = None) -> PlannerWire:
    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise PlannerError(f"planner config file not found: {path}")
    else:
        path = planner_yaml_path(root)
    if not path.is_file():
        raise PlannerError(
            f"planner.yaml is missing at {path}. That file is the switch for which model plans queries."
        )
    parsed: dict = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise PlannerError(f"invalid planner.yaml at line {line_number}")
        key, value = line.split(":", 1)
        key = key.strip()
        if key in parsed:
            raise PlannerError(f"duplicate planner.yaml key: {key}")
        if key in {"api_key", "token", "password", "secret"}:
            raise PlannerError("do not put a secret in planner.yaml; set api_key_env to the variable name")
        parsed[key] = _parse_scalar(value)
    return _validate_planner_wire(parsed, path)


def _looks_like_secret(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith("sk-") or lowered.startswith("sk_") or "bearer " in lowered


def _validate_planner_wire(parsed: dict, path: Path) -> PlannerWire:
    allowed = {"backend", "model", "base_url", "api_key_env"}
    unknown = sorted(set(parsed) - allowed)
    if unknown:
        raise PlannerError(f"unknown planner.yaml key: {unknown[0]}")
    backend = parsed.get("backend")
    model = parsed.get("model")
    if backend not in {CODEX_LOGIN, OPENAI_API}:
        raise PlannerError(
            f"planner backend must be {CODEX_LOGIN} or {OPENAI_API}, not {backend!r}"
        )
    if not isinstance(model, str) or not model.strip():
        raise PlannerError("planner model must be a non-empty name")
    if _looks_like_secret(model):
        raise PlannerError("planner model looks like a secret; put a model name there")
    base_url = str(parsed.get("base_url") or "")
    api_key_env = str(parsed.get("api_key_env") or "")
    if backend == OPENAI_API:
        if not base_url.startswith("https://"):
            raise PlannerError("openai-api base_url must start with https://")
        if not api_key_env or not api_key_env.replace("_", "").isalnum():
            raise PlannerError("openai-api api_key_env must be an environment variable name")
        if _looks_like_secret(api_key_env):
            raise PlannerError("api_key_env must be the variable name, not the key")
    elif base_url or api_key_env:
        raise PlannerError("base_url and api_key_env are only used when backend is openai-api")
    return PlannerWire(
        backend=backend,
        model=model.strip(),
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        path=str(path),
    )


def _call_codex_login(wire: PlannerWire, instruction: str, search_view_json: str) -> str:
    token = _read_codex_token()
    if not token:
        raise PlannerError(
            "ChatGPT/Codex sign-in was not found. Run hermes auth, or switch planner.yaml to openai-api."
        )
    body = {
        "model": wire.model,
        "store": False,
        "instructions": instruction,
        "stream": False,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": search_view_json}],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "scenery-brief-clips/0.1",
        "originator": "hermes-agent",
    }
    return _post_json(CODEX_URL, headers, body)


def _call_openai_api(wire: PlannerWire, instruction: str, search_view_json: str) -> str:
    key = os.environ.get(wire.api_key_env, "").strip()
    if not key:
        raise PlannerError(
            f"environment variable {wire.api_key_env} is empty. "
            "Set that variable, or change planner.yaml."
        )
    body = {
        "model": wire.model,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": search_view_json},
        ],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    return _post_json(f"{wire.base_url}/chat/completions", headers, body)


def call_wired_planner(wire: PlannerWire, instruction: str, search_view_json: str) -> str:
    if wire.backend == CODEX_LOGIN:
        return _call_codex_login(wire, instruction, search_view_json)
    if wire.backend == OPENAI_API:
        return _call_openai_api(wire, instruction, search_view_json)
    raise PlannerError(f"unknown planner backend: {wire.backend}")


def plan_queries(brief: dict, wire, caller=None) -> tuple[dict, dict]:
    """Make the one bounded planner call for a frozen brief.

    Returns (plan, provenance). The plan is validated against the brief; an
    empty queries list is a valid refusal-shaped plan and is returned as-is.
    Every failure raises PlannerError.
    """
    caller = caller or call_wired_planner
    rendered = render_planner(brief)
    instruction = rendered["instruction"]
    search_view_json = rendered["search_view_json"]
    started = time.monotonic()
    try:
        text = caller(wire, instruction, search_view_json)
    except PlannerError:
        raise
    except Exception as exc:
        raise PlannerError(f"planner call failed: {exc}") from exc
    elapsed = time.monotonic() - started
    try:
        payload = parse_model_json(text)
    except Exception as exc:
        raise PlannerError(f"the planner model did not return a JSON object: {exc}") from exc
    try:
        plan = validate_query_plan(payload, brief)
    except Exception as exc:
        code = getattr(exc, "code", "invalid_plan")
        raise PlannerError(f"planner plan rejected ({code}): {exc}") from exc
    provenance = {
        "schema_version": "brief_plan_provenance_v1",
        "instruction_version": PLANNER_INSTRUCTION_VERSION,
        "backend": wire.backend,
        "model": wire.model,
        "elapsed_s": round(elapsed, 3),
        "plan_sha256": canonical_json_hash(plan),
    }
    return plan, provenance
