"""Planner retry behaviour (v2 instruction), the live-planner runner path and the CLI wiring.

Every model call here is a fake. No network, no key, no media tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scenery_brief_clips import cli
from scenery_brief_clips.brief import PLANNER_INSTRUCTION, canonical_json_hash, render_planner
from scenery_brief_clips.planner import (
    PLANNER_INSTRUCTION_VERSION,
    PLANNER_RETRIES,
    PlannerError,
    PlannerWire,
    plan_queries,
)
from scenery_brief_clips.runner import Ports, advance
from test_brief import valid_brief, valid_plan
from test_runner_contract import FakeYt, Guard, _brief_plan, _ports, _root

WIRE = PlannerWire(backend="openai-api", model="fake-planner", base_url="https://example.invalid/v1", api_key_env="X")


def bad_plan(brief):
    """Fails validation with subject_mismatch: no query holds the noun 'alpaca'."""
    plan = valid_plan(brief)
    plan["queries"] = [
        {"query": "llama herd in a field", "strategy": "exact"},
        {"query": "camelid pasture footage", "strategy": "context"},
    ]
    return plan


class SeqCaller:
    """Returns the queued replies in order; an Exception item is raised."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, wire, instruction, search_view_json):
        self.calls.append((wire, instruction, search_view_json))
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


# 1-8: plan_queries ---------------------------------------------------------


def test_retry_after_subject_mismatch_returns_second_plan():
    brief = valid_brief()
    first, good = bad_plan(brief), valid_plan(brief)
    caller = SeqCaller(first, good)
    plan, prov = plan_queries(brief, WIRE, caller=caller)

    assert plan == good
    assert len(caller.calls) == 2
    assert prov["model_calls"] == 2
    assert len(prov["rejected_attempts"]) == 1
    rejected = prov["rejected_attempts"][0]
    assert rejected["attempt"] == 1
    assert rejected["code"] == "subject_mismatch"
    assert prov["plan_sha256"] == canonical_json_hash(good)
    first_instruction, second_instruction = caller.calls[0][1], caller.calls[1][1]
    assert first_instruction == PLANNER_INSTRUCTION
    assert second_instruction.startswith(PLANNER_INSTRUCTION)
    assert "subject_mismatch" in second_instruction
    assert rejected["error"][:60] in second_instruction
    assert json.dumps(first) in second_instruction  # previous reply text
    # the user message is unchanged between attempts
    assert caller.calls[0][2] == caller.calls[1][2]


def test_retry_after_non_json_reply():
    brief = valid_brief()
    good = valid_plan(brief)
    caller = SeqCaller("Sure! here are some searches", good)
    plan, prov = plan_queries(brief, WIRE, caller=caller)
    assert plan == good
    assert prov["model_calls"] == 2
    assert [r["code"] for r in prov["rejected_attempts"]] == ["not_json"]
    assert "not_json" in caller.calls[1][1]
    assert "Sure! here are some searches" in caller.calls[1][1]


def test_both_replies_invalid_raises_after_two_attempts():
    brief = valid_brief()
    caller = SeqCaller(bad_plan(brief), "still not json")
    with pytest.raises(PlannerError) as raised:
        plan_queries(brief, WIRE, caller=caller)
    assert "after 2 attempts" in str(raised.value)
    assert "not_json" in str(raised.value)
    assert raised.value.model_calls == 2
    assert len(caller.calls) == 2


def test_both_replies_invalid_plan_reports_last_code():
    brief = valid_brief()
    caller = SeqCaller(bad_plan(brief))
    with pytest.raises(PlannerError) as raised:
        plan_queries(brief, WIRE, caller=caller)
    assert "subject_mismatch" in str(raised.value)
    assert "after 2 attempts" in str(raised.value)
    assert raised.value.model_calls == 2 and len(caller.calls) == 2


def test_default_retries_is_one():
    assert PLANNER_RETRIES == 1


def test_retries_zero_makes_exactly_one_call():
    brief = valid_brief()
    caller = SeqCaller(bad_plan(brief), valid_plan(brief))
    with pytest.raises(PlannerError) as raised:
        plan_queries(brief, WIRE, caller=caller, retries=0)
    assert len(caller.calls) == 1
    assert raised.value.model_calls == 1
    assert "after 1 attempts" in str(raised.value)


def test_retries_zero_success_is_single_call():
    brief = valid_brief()
    caller = SeqCaller(valid_plan(brief))
    _, prov = plan_queries(brief, WIRE, caller=caller, retries=0)
    assert len(caller.calls) == 1 and prov["model_calls"] == 1


@pytest.mark.parametrize("exc", [RuntimeError("boom"), OSError("connection reset")])
def test_caller_exception_is_not_retried(exc):
    brief = valid_brief()
    caller = SeqCaller(exc, valid_plan(brief))
    with pytest.raises(PlannerError) as raised:
        plan_queries(brief, WIRE, caller=caller)
    assert len(caller.calls) == 1
    assert raised.value.model_calls == 1
    assert "planner call failed" in str(raised.value)


def test_caller_urlerror_is_not_retried():
    from urllib.error import URLError

    brief = valid_brief()
    caller = SeqCaller(URLError("no route"), valid_plan(brief))
    with pytest.raises(PlannerError) as raised:
        plan_queries(brief, WIRE, caller=caller)
    assert len(caller.calls) == 1 and raised.value.model_calls == 1


def test_caller_planner_error_is_not_retried_and_counts_call():
    brief = valid_brief()
    caller = SeqCaller(PlannerError("environment variable X is empty"), valid_plan(brief))
    with pytest.raises(PlannerError) as raised:
        plan_queries(brief, WIRE, caller=caller)
    assert len(caller.calls) == 1
    assert raised.value.model_calls == 1
    assert "environment variable X is empty" in str(raised.value)


def test_call_error_on_the_retry_attempt_counts_both_calls():
    brief = valid_brief()
    caller = SeqCaller(bad_plan(brief), RuntimeError("timeout"))
    with pytest.raises(PlannerError) as raised:
        plan_queries(brief, WIRE, caller=caller)
    assert len(caller.calls) == 2
    assert raised.value.model_calls == 2


def test_valid_empty_plan_is_not_retried():
    brief = valid_brief()
    empty = {"version": "search_queries_v1", "brief_sha256": canonical_json_hash(brief), "queries": []}
    caller = SeqCaller(empty)
    plan, prov = plan_queries(brief, WIRE, caller=caller)
    assert plan == empty
    assert len(caller.calls) == 1
    assert prov["model_calls"] == 1
    assert prov["rejected_attempts"] == []


def test_wire_none_records_no_backend_or_model_and_no_secrets():
    brief = valid_brief()
    caller = SeqCaller(valid_plan(brief))
    plan, prov = plan_queries(brief, None, caller=caller)
    assert plan == valid_plan(brief)
    assert caller.calls[0][0] is None
    assert prov["backend"] is None and prov["model"] is None
    text = json.dumps(prov)
    assert "sk-" not in text and "Bearer" not in text


def test_retry_provenance_has_no_secrets_and_rejection_error_is_bounded():
    brief = valid_brief()
    caller = SeqCaller(bad_plan(brief), valid_plan(brief))
    _, prov = plan_queries(brief, WIRE, caller=caller)
    text = json.dumps(prov)
    assert "sk-" not in text and "Bearer" not in text
    assert "base_url" not in prov and "example.invalid" not in text
    assert all(len(item["error"]) <= 300 for item in prov["rejected_attempts"])
    assert prov["instruction_version"] == PLANNER_INSTRUCTION_VERSION == "search_query_planner_v2"


def test_previous_reply_in_retry_note_is_truncated():
    brief = valid_brief()
    long_reply = "x" * 5000
    caller = SeqCaller(long_reply, valid_plan(brief))
    plan_queries(brief, WIRE, caller=caller)
    second = caller.calls[1][1]
    assert "x" * 2000 in second
    assert "x" * 2001 not in second


def test_instruction_states_the_exact_noun_rule():
    text = PLANNER_INSTRUCTION
    assert "exactly as written" in text
    assert "mice" in text and "mouse" in text
    assert "close names" not in text
    assert "close names" not in text.lower()
    assert render_planner(valid_brief())["instruction"] is PLANNER_INSTRUCTION


# 9-10: runner --------------------------------------------------------------


def _state(result):
    return json.loads((Path(result["run_dir"]) / "runner_state.json").read_text(encoding="utf-8"))


def test_runner_live_planner_records_plan_and_provenance(tmp_path, monkeypatch):
    guard = Guard()
    guard.install(monkeypatch)
    root = _root(tmp_path)
    brief_path, _plan_path = _brief_plan(root)
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    good = valid_plan(brief)
    seen_wires = []

    def planner(wire, instruction, search_view_json):
        seen_wires.append(wire)
        return json.dumps(bad_plan(brief) if len(seen_wires) == 1 else good)

    yt = FakeYt()
    result = advance(root, brief=brief_path, plan=None, ports=_live_ports(yt, planner))
    assert result["status"] == "paused", result
    assert result["stage"] == "agree_vision"
    assert seen_wires and all(w is WIRE for w in seen_wires)
    assert len(seen_wires) == 2  # one rejected reply, one retry
    assert "discover" in _state(result)["completed"]
    discovery = json.loads((Path(result["run_dir"]) / "discovery.json").read_text(encoding="utf-8"))
    assert discovery["query_plan"] == good
    prov = discovery["plan_provenance"]
    assert prov["model"] == "fake-planner" and prov["backend"] == "openai-api"
    assert prov["model_calls"] == 2
    assert prov["rejected_attempts"][0]["code"] == "subject_mismatch"
    assert yt.searches >= 1
    assert guard.tripped == []


def _live_ports(yt, planner, wire=WIRE):
    ports = _ports(yt, planner_caller=planner)
    ports.planner_wire = wire
    return ports


def test_runner_live_planner_first_reply_valid_single_call(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, _ = _brief_plan(root)
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    calls = []

    def planner(wire, instruction, search_view_json):
        calls.append(wire)
        return json.dumps(valid_plan(brief))

    result = advance(root, brief=brief_path, plan=None, ports=_live_ports(FakeYt(), planner))
    assert result["stage"] == "agree_vision"
    assert len(calls) == 1
    discovery = json.loads((Path(result["run_dir"]) / "discovery.json").read_text(encoding="utf-8"))
    assert discovery["plan_provenance"]["model_calls"] == 1
    assert discovery["plan_provenance"]["rejected_attempts"] == []
    assert discovery["export_max_height"] == brief["export_max_height"]


def test_runner_live_planner_rejected_twice_fails_discover(tmp_path, monkeypatch):
    guard = Guard()
    guard.install(monkeypatch)
    root = _root(tmp_path)
    brief_path, _ = _brief_plan(root)
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    calls = []

    def planner(wire, instruction, search_view_json):
        calls.append(wire)
        return json.dumps(bad_plan(brief))

    yt = FakeYt()
    result = advance(root, brief=brief_path, plan=None, ports=_live_ports(yt, planner))
    assert result["status"] == "failed", result
    assert result["stage"] == "discover"
    assert "planner plan rejected" in result["error"]
    assert "after 2 attempts" in result["error"]
    assert len(calls) == 2
    assert yt.searches == 0
    state = _state(result)
    assert "discover" not in state["completed"]
    assert state.get("failed_stage") == "discover"
    assert not (Path(result["run_dir"]) / "discovery.json").exists()
    assert guard.tripped == []


def test_runner_planner_call_error_fails_discover_without_retry(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, _ = _brief_plan(root)
    calls = []

    def planner(wire, instruction, search_view_json):
        calls.append(1)
        raise RuntimeError("upstream 503")

    result = advance(root, brief=brief_path, plan=None, ports=_live_ports(FakeYt(), planner))
    assert result["status"] == "failed" and result["stage"] == "discover"
    assert "upstream 503" in result["error"]
    assert len(calls) == 1
    assert "discover" not in _state(result)["completed"]


def test_runner_counts_model_calls_of_a_failed_planner_stage(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, _ = _brief_plan(root)
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    result = advance(
        root,
        brief=brief_path,
        plan=None,
        ports=_live_ports(FakeYt(), lambda w, i, s: json.dumps(bad_plan(brief))),
    )
    records = [r for r in _state(result)["timing"]["stages"] if r["stage"] == "discover"]
    assert [(r["status"], r["model_calls"]) for r in records] == [("failed", 2)]


def test_runner_without_plan_or_planner_pauses_and_names_live_planner(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, _ = _brief_plan(root)
    result = advance(root, brief=brief_path, plan=None, ports=_ports(FakeYt()))
    assert result["status"] == "paused"
    assert result["stage"] == "discover"
    assert "--live-planner" in result["how_to_supply"]
    assert "--plan" in result["how_to_supply"]
    assert "discover" not in _state(result)["completed"]


# 11: CLI -------------------------------------------------------------------


class _NoNetYt:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def fetch_analysis(self, *a, **k):
        raise AssertionError("not used")

    prefetch_analysis = invalidate_analysis = fetch_analysis


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    captured = {}

    def fake_advance(root_arg, **kwargs):
        captured["root"] = root_arg
        captured["kwargs"] = kwargs
        return {"status": "paused", "stage": "discover"}

    monkeypatch.setattr("scenery_brief_clips.runner.advance", fake_advance)
    monkeypatch.setattr("scenery_brief_clips.yt.YtDlp", _NoNetYt)
    monkeypatch.setattr("scenery_brief_clips.fetch.cached_fetcher", lambda *a, **k: (lambda url: b""))
    return root, brief_path, plan_path, captured


def _pipeline_args(root, brief, *extra):
    return ["run-pipeline", "--root", str(root), "--brief", str(brief), *extra]


def test_cli_parser_accepts_live_planner_flags(cli_env, monkeypatch, tmp_path):
    root, brief, _plan, captured = cli_env
    monkeypatch.setattr(cli, "load_planner_wire", lambda r, c: WIRE)
    cfg = tmp_path / "other-planner.yaml"
    assert cli.main(_pipeline_args(root, brief, "--live-planner", "--planner-config", str(cfg))) == 0
    assert captured["kwargs"]["ports"].planner_wire is WIRE


def test_cli_live_planner_wires_caller_and_wire(cli_env, monkeypatch, tmp_path, capsys):
    root, brief, _plan, captured = cli_env
    loads = []

    def fake_load(root_arg, config_arg):
        loads.append((Path(root_arg), config_arg))
        return WIRE

    monkeypatch.setattr(cli, "load_planner_wire", fake_load)
    cfg = tmp_path / "alt.yaml"
    assert cli.main(_pipeline_args(root, brief, "--live-planner", "--planner-config", str(cfg))) == 0
    ports = captured["kwargs"]["ports"]
    assert isinstance(ports, Ports)
    assert ports.planner_caller is cli.call_wired_planner
    assert ports.planner_wire is WIRE
    assert captured["kwargs"]["plan"] is None
    assert loads == [(root, cfg)]
    assert json.loads(capsys.readouterr().out)["status"] == "paused"


def test_cli_live_planner_default_config_is_none(cli_env, monkeypatch):
    root, brief, _plan, _captured = cli_env
    loads = []
    monkeypatch.setattr(cli, "load_planner_wire", lambda r, c: loads.append(c) or WIRE)
    cli.main(_pipeline_args(root, brief, "--live-planner"))
    assert loads == [None]


def test_cli_plan_given_does_not_wire_planner(cli_env, monkeypatch):
    root, brief, plan, captured = cli_env

    def no_load(*a, **k):
        raise AssertionError("planner wire must not be loaded when --plan is given")

    monkeypatch.setattr(cli, "load_planner_wire", no_load)
    assert cli.main(_pipeline_args(root, brief, "--plan", str(plan), "--live-planner")) == 0
    ports = captured["kwargs"]["ports"]
    assert ports.planner_caller is None
    assert ports.planner_wire is None
    assert captured["kwargs"]["plan"] == plan


def test_cli_neither_flag_leaves_planner_unwired(cli_env, monkeypatch):
    root, brief, _plan, captured = cli_env
    monkeypatch.setattr(cli, "load_planner_wire", lambda *a, **k: pytest.fail("must not load"))
    assert cli.main(_pipeline_args(root, brief)) == 0
    ports = captured["kwargs"]["ports"]
    assert ports.planner_caller is None and ports.planner_wire is None


def test_cli_planner_wire_error_exits_2_without_advance(cli_env, monkeypatch, capsys):
    root, brief, _plan, captured = cli_env

    def boom(root_arg, config_arg):
        raise PlannerError("planner.yaml is missing")

    monkeypatch.setattr(cli, "load_planner_wire", boom)
    assert cli.main(_pipeline_args(root, brief, "--live-planner")) == 2
    assert captured == {}
    err = capsys.readouterr().err
    assert "planner.yaml is missing" in err
