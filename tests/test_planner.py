import json
from pathlib import Path

import pytest

from scenery_brief_clips.brief import canonical_json_hash
from scenery_brief_clips.planner import (
    PLANNER_INSTRUCTION_VERSION,
    PlannerError,
    PlannerWire,
    load_planner_wire,
    plan_queries,
)
from scenery_brief_clips.vision_wire import parse_model_json

from test_brief import valid_brief, valid_plan


class FakeCaller:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, wire, instruction, search_view_json):
        self.calls.append((wire, instruction, search_view_json))
        if isinstance(self.payload, Exception):
            raise self.payload
        return json.dumps(self.payload)


def test_plan_queries_valid_plan_round_trip():
    brief = valid_brief()
    plan = valid_plan(brief)
    caller = FakeCaller(plan)
    wire = PlannerWire(backend="codex-login", model="gpt-test")
    returned_plan, provenance = plan_queries(brief, wire, caller=caller)

    assert returned_plan == plan
    assert len(caller.calls) == 1
    wire_seen, instruction, search_view_json = caller.calls[0]
    assert wire_seen is wire
    view = json.loads(search_view_json)
    assert view["brief_sha256"] == canonical_json_hash(brief)
    assert "search_view_json" not in instruction  # the user message is the JSON only
    assert provenance["schema_version"] == "brief_plan_provenance_v1"
    assert provenance["instruction_version"] == "search_query_planner_v1"
    assert provenance["instruction_version"] == PLANNER_INSTRUCTION_VERSION
    assert provenance["backend"] == "codex-login"
    assert provenance["model"] == "gpt-test"
    assert provenance["plan_sha256"] == canonical_json_hash(plan)
    assert isinstance(provenance["elapsed_s"], float)
    assert provenance["elapsed_s"] >= 0.0


def test_plan_queries_provenance_has_no_secrets():
    brief = valid_brief()
    plan = valid_plan(brief)
    caller = FakeCaller(plan)
    wire = PlannerWire(
        backend="openai-api",
        model="gpt-test",
        base_url="https://api.example.com/v1",
        api_key_env="PLANNER_API_KEY",
    )
    _, provenance = plan_queries(brief, wire, caller=caller)
    text = json.dumps(provenance)
    assert "sk-" not in text
    assert "Bearer" not in text
    assert "token" not in text.lower()
    # Only a plain backend/model identity is recorded; never a URL or key.
    assert "base_url" not in provenance
    assert set(provenance) == {
        "schema_version", "instruction_version", "backend", "model",
        "elapsed_s", "plan_sha256",
    }


def test_plan_queries_wrong_hash_is_a_planner_error():
    brief = valid_brief()
    plan = valid_plan(brief)
    plan["brief_sha256"] = "1" * 64
    caller = FakeCaller(plan)
    with pytest.raises(PlannerError):
        plan_queries(brief, PlannerWire(backend="codex-login", model="gpt-test"), caller=caller)


def test_plan_queries_subject_mismatch_is_a_planner_error():
    brief = valid_brief()
    plan = valid_plan(brief)
    # The lab's "sea waves" case: the model renamed the required subject.
    plan["queries"] = [
        {"query": "sea waves crashing", "strategy": "exact"},
        {"query": "ocean wave footage", "strategy": "context"},
    ]
    caller = FakeCaller(plan)
    with pytest.raises(PlannerError) as raised:
        plan_queries(brief, PlannerWire(backend="codex-login", model="gpt-test"), caller=caller)
    assert "subject_mismatch" in str(raised.value)


def test_plan_queries_empty_queries_is_a_valid_refusal_shaped_plan():
    brief = valid_brief()
    plan = {
        "version": "search_queries_v1",
        "brief_sha256": canonical_json_hash(brief),
        "queries": [],
    }
    caller = FakeCaller(plan)
    returned, provenance = plan_queries(
        brief, PlannerWire(backend="codex-login", model="gpt-test"), caller=caller
    )
    assert returned == plan
    assert returned["queries"] == []
    assert provenance["plan_sha256"] == canonical_json_hash(plan)


def test_plan_queries_non_json_answer_is_a_planner_error():
    caller = FakeCaller(RuntimeError("boom"))
    with pytest.raises(PlannerError):
        plan_queries(valid_brief(), PlannerWire(backend="codex-login", model="gpt-test"), caller=caller)


def test_plan_queries_wraps_non_planner_caller_exceptions():
    brief = valid_brief()

    class BadJson:
        def __call__(self, wire, instruction, view):
            return "not json at all"

    with pytest.raises(PlannerError):
        plan_queries(brief, PlannerWire(backend="codex-login", model="gpt-test"), caller=BadJson())


def test_parse_model_json_still_rejects_non_objects():
    with pytest.raises(Exception):
        parse_model_json("[1, 2, 3]")


def test_load_planner_wire_missing_file(tmp_path: Path):
    with pytest.raises(PlannerError):
        load_planner_wire(tmp_path)


def test_load_planner_wire_codex(tmp_path: Path):
    (tmp_path / "planner.yaml").write_text("backend: codex-login\nmodel: gpt-6-sol\n", encoding="utf-8")
    wire = load_planner_wire(tmp_path)
    assert wire.backend == "codex-login"
    assert wire.model == "gpt-6-sol"
    assert wire.as_public_dict()["model"] == "gpt-6-sol"


def test_load_planner_wire_rejects_secrets(tmp_path: Path):
    (tmp_path / "planner.yaml").write_text("backend: codex-login\nmodel: sk-abc123\n", encoding="utf-8")
    with pytest.raises(PlannerError):
        load_planner_wire(tmp_path)
    (tmp_path / "planner.yaml").write_text("api_key: hunter2\nbackend: codex-login\nmodel: m\n", encoding="utf-8")
    with pytest.raises(PlannerError):
        load_planner_wire(tmp_path)


def test_load_planner_wire_openai_api(tmp_path: Path):
    (tmp_path / "planner.yaml").write_text(
        "backend: openai-api\nmodel: gpt-test\n"
        "base_url: https://api.example.com/v1\napi_key_env: PLANNER_API_KEY\n",
        encoding="utf-8",
    )
    wire = load_planner_wire(tmp_path)
    assert wire.backend == "openai-api"
    public = wire.as_public_dict()
    assert public["base_url"] == "https://api.example.com/v1"
    assert public["api_key_env"] == "PLANNER_API_KEY"


def test_load_planner_wire_unknown_key(tmp_path: Path):
    (tmp_path / "planner.yaml").write_text("backend: codex-login\nmodel: m\ntemperature: 0\n", encoding="utf-8")
    with pytest.raises(PlannerError):
        load_planner_wire(tmp_path)
