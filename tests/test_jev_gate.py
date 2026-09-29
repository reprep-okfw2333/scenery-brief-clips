import json
from pathlib import Path

import pytest

from scenery_brief_clips import jev, jev_gate
from scenery_brief_clips.cli import main
from scenery_brief_clips.config import ConfigError, load_project_config
from scenery_brief_clips.jev_gate import JevError, JevGateSettings, gate_run
from test_brief import valid_brief

FAKE_KEY = "sk-or-test-DO-NOT-LEAK-123"


def _answers(score: float) -> dict:
    """All five source questions; the gate score is the mean of usable, subject and conditions."""
    return {
        "subject": {"type": "noul", "noul": score},
        "place": {"type": "choice", "choice": "matches", "confidence": 0.9,
                  "probabilities": {"matches": 0.9, "different": 0.05, "unknown": 0.05}},
        "kind": {"type": "choice", "choice": "exterior_scene", "confidence": 0.9,
                 "probabilities": {"exterior_scene": 0.95, "other": 0.05}},
        "conditions": {"type": "noul", "noul": score},
        "usable": {"type": "noul", "noul": score},
    }


def _make_run(tmp_path: Path, videos: dict[str, str]) -> tuple[Path, Path, Path]:
    run = tmp_path / "run"
    run.mkdir(parents=True)
    meta = tmp_path / "meta"
    meta.mkdir(parents=True)
    cands = []
    for vid, title in videos.items():
        cands.append({"video_id": vid, "title": title, "duration_s": 100.0, "width": 1920, "height": 1080})
        (meta / f"{vid}.json").write_text(json.dumps({
            "id": vid, "title": title, "channel": "chan", "description": f"desc {title}", "duration": 100,
            "tags": ["train"], "view_count": 10,
            "formats": [{"vcodec": "avc1", "width": 1920, "height": 1080, "fps": 30}],
        }))
    (run / "candidates.json").write_text(json.dumps(cands))
    (run / "constraint.json").write_text(json.dumps({"theme_text": "steam train in snow", "visual_negatives": ["text"]}))
    return run, meta, tmp_path / "jevcache"


class FakePost:
    def __init__(self, p_by_title: dict[str, float], fail: Exception | None = None):
        self.p_by_title = p_by_title
        self.fail = fail
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout, "body": json.loads(body)})
        if self.fail is not None:
            raise self.fail
        title = json.loads(body)["state"]["video"]["title"]
        return {"answers": _answers(self.p_by_title[title]), "usage": {"cost": 0.0001, "input_tokens": 1500},
                "model": "typesafe/jev-1.13-20260917"}


ON = JevGateSettings(enabled=True, reject_below=0.35, keep_above=0.85)


def test_disabled_by_default_changes_nothing(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A", "b": "B"})
    before = (run / "candidates.json").read_text()
    post = FakePost({"A": 0.1, "B": 0.9})
    out = gate_run(run, meta, cache, JevGateSettings(), env={"OPENROUTER_API_KEY": FAKE_KEY}, post=post)
    assert out == {"run_dir": str(run), "enabled": False, "changed": False}
    assert post.calls == []
    assert (run / "candidates.json").read_text() == before
    assert not (run / "jev_gate.json").exists()
    assert JevGateSettings.from_config({}).enabled is False
    assert JevGateSettings.from_config({}).reject_below == 0.35


def test_rejects_low_orders_by_probability_and_records_artifacts(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A", "b": "B", "c": "C", "d": "D"})
    post = FakePost({"A": 0.2, "B": 0.6, "C": 0.9, "D": 0.35})
    out = gate_run(run, meta, cache, ON, env={"OPENROUTER_API_KEY": FAKE_KEY}, post=post)
    assert out["kept"] == 2 and out["by_decision"] == {"reject": 2, "keep_review": 1, "keep_high": 1}
    kept = [c["video_id"] for c in json.loads((run / "candidates.json").read_text())]
    assert kept == ["c", "b"]  # ordered by score desc; 0.35 is at the threshold -> reject
    pre = [c["video_id"] for c in json.loads((run / "candidates_pre_jev.json").read_text())]
    assert pre == ["a", "b", "c", "d"]
    report = json.loads((run / "jev_gate.json").read_text())
    recs = {r["video_id"]: r for r in report["candidates"]}
    assert recs["a"]["score"] == 0.2 and recs["a"]["decision"] == "reject"
    assert recs["c"]["answers"]["kind"]["choice"] == "exterior_scene"
    assert recs["c"]["answers"]["usable"] == 0.9
    assert recs["c"]["decision"] == "keep_high" and recs["b"]["decision"] == "keep_review"
    assert recs["d"]["decision"] == "reject" and recs["d"]["score"] == 0.35
    assert report["schema_version"] == "jev_gate_v2"
    assert report["settings"]["reject_below"] == 0.35
    assert report["counts"]["by_source"] == {"jev": 4}
    assert report["model"] == "typesafe/jev-1.13"
    assert report["cost_usd"] == pytest.approx(0.0004)
    call = post.calls[0]
    assert call["url"] == jev_gate.JEV_URL
    assert call["body"]["model"] == "typesafe/jev-1.13"
    assert set(call["body"]["questions"]) == {"subject", "place", "kind", "conditions", "usable"}
    assert call["body"]["questions"] == jev.SOURCE_QUESTIONS
    assert call["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    # No brief passed: the minimal brief comes from constraint.json.
    assert call["body"]["state"]["brief"]["request"] == "steam train in snow"
    assert call["body"]["state"]["brief"]["must_not_show"] == ["text"]
    assert report["brief"] == {"request_text": "steam train in snow"}


def test_no_key_falls_back_to_rules_without_calls(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A", "b": "B"})
    post = FakePost({"A": 0.1, "B": 0.9})
    out = gate_run(run, meta, cache, ON, env={}, post=post)
    assert post.calls == []
    assert out["kept"] == 2 and out["by_source"] == {"fallback_no_key": 2}
    assert [c["video_id"] for c in json.loads((run / "candidates.json").read_text())] == ["a", "b"]


@pytest.mark.parametrize("fail", [TimeoutError("slow"), JevError("HTTP 500"), ValueError("weird")])
def test_errors_and_timeouts_fall_back_to_rules(tmp_path, fail):
    run, meta, cache = _make_run(tmp_path, {"a": "A"})
    post = FakePost({"A": 0.1}, fail=fail)
    out = gate_run(run, meta, cache, ON, env={"OPENROUTER_API_KEY": FAKE_KEY}, post=post)
    assert out["kept"] == 1 and out["by_source"] == {"fallback_error": 1}
    rec = json.loads((run / "jev_gate.json").read_text())["candidates"][0]
    assert rec["decision"] == "keep_fallback" and rec["score"] is None


def test_malformed_answer_falls_back(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A"})

    def post(url, body, headers, timeout):
        return {"answers": {**_answers(0.5), "usable": {"type": "noul", "noul": 7}}}

    out = gate_run(run, meta, cache, ON, env={"OPENROUTER_API_KEY": FAKE_KEY}, post=post)
    assert out["by_source"] == {"fallback_error": 1}


def test_cache_hit_avoids_second_call_and_rerun_uses_pre_gate_candidates(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A", "b": "B"})
    gate_run(run, meta, cache, ON, env={"OPENROUTER_API_KEY": FAKE_KEY}, post=FakePost({"A": 0.1, "B": 0.7}))
    assert len(list(cache.glob("*.json"))) == 2
    boom = FakePost({}, fail=AssertionError("should not be called"))
    out = gate_run(run, meta, cache, ON, env={}, post=boom)
    assert boom.calls == []
    assert out["by_source"] == {"jev_cache": 2}
    # rerun re-gates from candidates_pre_jev.json, so a rejected candidate is still evaluated
    assert out["input"] == 2 and out["kept"] == 1


def test_budget_cap_stops_calls(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A", "b": "B", "c": "C"})
    post = FakePost({"A": 0.1, "B": 0.1, "C": 0.1})
    settings = JevGateSettings(enabled=True, max_usd_per_run=0.00015)  # spent 0.0001 < cap, 0.0002 >= cap
    out = gate_run(run, meta, cache, settings, env={"OPENROUTER_API_KEY": FAKE_KEY}, post=post)
    assert len(post.calls) == 2
    assert out["by_source"] == {"jev": 2, "fallback_budget": 1}
    assert out["kept"] == 1


def test_key_is_never_written_to_artifacts(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A", "b": "B"})
    gate_run(run, meta, cache, ON, env={"OPENROUTER_API_KEY": FAKE_KEY}, post=FakePost({"A": 0.1, "B": 0.9}))
    run2, meta2, cache2 = _make_run(tmp_path / "x", {"a": "A"})
    gate_run(run2, meta2, cache2, ON, env={"OPENROUTER_API_KEY": FAKE_KEY}, post=FakePost({}, fail=JevError("HTTP 401")))
    for path in list(tmp_path.rglob("*.json")):
        assert FAKE_KEY not in path.read_text()


def test_default_post_error_message_has_no_key(monkeypatch):
    import urllib.error
    import urllib.request

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(JevError) as exc:
        jev_gate.default_post(jev_gate.JEV_URL, b"{}", {"Authorization": f"Bearer {FAKE_KEY}"}, 1.0)
    assert FAKE_KEY not in str(exc.value) and "401" in str(exc.value)


def test_module_reads_key_from_env_only():
    src = Path(jev_gate.__file__).read_text()
    assert "box-secrets" not in src and "secrets.json" not in src
    assert jev_gate.API_KEY_ENV == "OPENROUTER_API_KEY"


def test_config_accepts_and_validates_jev_keys(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("jev_gate: true\njev_reject_below: 0.35\njev_keep_above: 0.9\njev_timeout_s: 5\njev_max_usd_per_run: 0.1\n")
    loaded = load_project_config(tmp_path)
    s = JevGateSettings.from_config(loaded)
    assert s.enabled and s.reject_below == 0.35 and s.keep_above == 0.9 and s.timeout_s == 5.0
    for bad, msg in [("jev_gate: yes\n", "boolean"), ("jev_reject_below: 1.5\n", "between 0 and 1"),
                     ("jev_reject_below: 0.9\njev_keep_above: 0.5\n", "below"), ("jev_timeout_s: 0\n", "greater than 0")]:
        cfg.write_text(bad)
        with pytest.raises(ConfigError, match=msg):
            load_project_config(tmp_path)


def test_cli_jev_gate_off_by_default(tmp_path, capsys):
    root = tmp_path
    (root / "data" / "cache" / "metadata").mkdir(parents=True)
    run, _, _ = _make_run(tmp_path / "data", {"a": "A"})
    rc = main(["jev-gate", "--run-dir", str(run), "--root", str(root)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is False
    assert not (run / "jev_gate.json").exists()


def test_cli_jev_gate_on_without_key_falls_back(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    root = tmp_path
    run, meta, _ = _make_run(tmp_path / "data", {"a": "A"})
    (root / "data" / "cache").mkdir(parents=True, exist_ok=True)
    meta.rename(root / "data" / "cache" / "metadata")
    (root / "cfg.yaml").write_text("jev_gate: true\n")
    rc = main(["jev-gate", "--run-dir", str(run), "--root", str(root), "--config", str(root / "cfg.yaml")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is True and out["by_source"] == {"fallback_no_key": 1}


def test_missing_metadata_is_kept_as_fallback_no_metadata(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A", "b": "B"})
    (meta / "a.json").unlink()
    post = FakePost({"B": 0.9})
    out = gate_run(run, meta, cache, ON, env={"OPENROUTER_API_KEY": FAKE_KEY}, post=post)
    assert len(post.calls) == 1
    assert out["by_source"] == {"fallback_no_metadata": 1, "jev": 1}
    assert out["kept"] == 2
    # Scored survivors first; the fallback keeps its place after them.
    assert [c["video_id"] for c in json.loads((run / "candidates.json").read_text())] == ["b", "a"]
    rec = {r["video_id"]: r for r in json.loads((run / "jev_gate.json").read_text())["candidates"]}
    assert rec["a"]["decision"] == "keep_fallback" and rec["a"]["score"] is None


def test_explicit_brief_is_used_for_state_and_recorded(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A"})
    post = FakePost({"A": 0.9})
    brief = valid_brief()
    gate_run(run, meta, cache, ON, env={"OPENROUTER_API_KEY": FAKE_KEY}, post=post, brief=brief)
    state_brief = post.calls[0]["body"]["state"]["brief"]
    assert state_brief["request"] == brief["request_text"]
    assert state_brief["subjects"] == ["alpaca"]
    assert json.loads((run / "jev_gate.json").read_text())["brief"] == {"request_text": brief["request_text"]}


def test_order_by_p_false_keeps_discovery_order(tmp_path):
    run, meta, cache = _make_run(tmp_path, {"a": "A", "b": "B", "c": "C"})
    settings = JevGateSettings(enabled=True, order_by_p=False)
    gate_run(run, meta, cache, settings, env={"OPENROUTER_API_KEY": FAKE_KEY},
             post=FakePost({"A": 0.5, "B": 0.9, "C": 0.6}))
    assert [c["video_id"] for c in json.loads((run / "candidates.json").read_text())] == ["a", "b", "c"]


def test_gate_settings_from_config_new_keys():
    s = JevGateSettings.from_config({"jev_gate": True, "jev_reject_below": 0.2, "jev_order_by_p": False})
    assert s.enabled and s.reject_below == 0.2 and s.order_by_p is False and s.keep_above == 0.85


def test_config_default_reject_below_must_stay_below_keep_above(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("jev_keep_above: 0.35\n")  # equals the new default reject_below (0.35)
    with pytest.raises(ConfigError, match="below"):
        load_project_config(tmp_path)
    cfg.write_text("jev_keep_above: 0.36\n")
    assert load_project_config(tmp_path)["jev_keep_above"] == 0.36


def _cli_setup(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    root = tmp_path
    run, meta, _ = _make_run(tmp_path / "data", {"a": "A"})
    (root / "data" / "cache").mkdir(parents=True, exist_ok=True)
    meta.rename(root / "data" / "cache" / "metadata")
    (root / "cfg.yaml").write_text("jev_gate: true\n")
    return root, run


def test_cli_jev_gate_brief_is_used(tmp_path, capsys, monkeypatch):
    root, run = _cli_setup(tmp_path, monkeypatch)
    brief = valid_brief()
    brief_path = root / "brief.json"
    brief_path.write_text(json.dumps(brief))
    rc = main(["jev-gate", "--run-dir", str(run), "--root", str(root), "--config", str(root / "cfg.yaml"),
               "--brief", str(brief_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is True and out["by_source"] == {"fallback_no_key": 1}
    report = json.loads((run / "jev_gate.json").read_text())
    assert report["brief"]["request_text"] == brief["request_text"] == "clips of alpacas in a field"
    assert report["brief"]["request_text"] != "steam train in snow"  # not the constraint.json theme


def test_cli_jev_gate_without_brief_uses_constraint_theme(tmp_path, capsys, monkeypatch):
    root, run = _cli_setup(tmp_path, monkeypatch)
    rc = main(["jev-gate", "--run-dir", str(run), "--root", str(root), "--config", str(root / "cfg.yaml")])
    assert rc == 0
    capsys.readouterr()
    assert json.loads((run / "jev_gate.json").read_text())["brief"]["request_text"] == "steam train in snow"


@pytest.mark.parametrize("content", [json.dumps({"request_text": "x"}), "not json", None])
def test_cli_jev_gate_invalid_brief_exits_2(tmp_path, capsys, monkeypatch, content):
    root, run = _cli_setup(tmp_path, monkeypatch)
    brief_path = root / "brief.json"
    if content is not None:  # None: the file does not exist
        brief_path.write_text(content)
    before = (run / "candidates.json").read_text()
    rc = main(["jev-gate", "--run-dir", str(run), "--root", str(root), "--config", str(root / "cfg.yaml"),
               "--brief", str(brief_path)])
    assert rc == 2
    assert "invalid brief" in capsys.readouterr().err
    assert not (run / "jev_gate.json").exists()
    assert (run / "candidates.json").read_text() == before
