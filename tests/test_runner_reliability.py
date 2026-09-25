"""Runner regressions use real stages and only external fixture ports."""
import json
from pathlib import Path

from scenery_brief_clips.runner import advance
from test_runner_contract import Guard, FakeYt, _root, _brief_plan, _ports


def test_prompt_change_invalidates_completed_labels(tmp_path, monkeypatch):
    from test_runner_contract import _patch_media, _tile_text
    import scenery_brief_clips.vision_wire as vision
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()
    _patch_media(monkeypatch, yt)
    calls = []
    def caller(*args):
        calls.append(1)
        return _tile_text(*args)
    ports = _ports(yt, tile_caller=caller)
    first = advance(root, brief=brief, plan=plan, ports=ports, vision_agree=True)
    assert first['stage'] == 'label_strips', first
    count = len(calls)
    monkeypatch.setattr(vision, 'TILE_PROMPT', vision.TILE_PROMPT + ' changed rubric')
    advance(root, brief=brief, plan=plan, ports=ports, run_dir=first['run_dir'])
    assert len(calls) > count


def test_discovery_honors_tighter_config_limits(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    config = root / 'config.yaml'
    config.write_text(config.read_text() + 'max_search_results: 1\nmax_metadata_fetches: 1\nmin_height: 1080\n')
    yt = FakeYt()
    result = advance(root, brief=brief, plan=plan, ports=_ports(yt))
    constraint = json.loads((Path(result['run_dir']) / 'constraint.json').read_text())
    assert constraint['limits']['max_search_results'] == 1
    assert constraint['limits']['max_metadata_fetches'] == 1
    assert constraint['min_height'] == 1080


def test_completed_resume_rechecks_export_bytes(tmp_path, monkeypatch):
    from test_runner_contract import _patch_media, _tile_text, _strip_text
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / 'acq'
    yt.base.mkdir()
    _patch_media(monkeypatch, yt)
    ports = _ports(yt, tile_caller=_tile_text, strip_caller=_strip_text)
    first = advance(root, brief=brief, plan=plan, ports=ports,
                    vision_agree=True, allow_export=True, theme='integrity')
    assert first['status'] == 'completed', first
    clip = next((root / 'out' / 'integrity' / 'clips').glob('*.mp4'))
    clip.write_bytes(b'corrupted exported clip')
    resumed = advance(root, brief=brief, plan=plan, run_dir=first['run_dir'],
                      ports=ports, theme='integrity')
    assert resumed['status'] == 'failed', resumed
    assert resumed['stage'] == 'verify_export'


def test_resume_rejects_changed_discovery_bytes(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()
    first = advance(root, brief=brief, plan=plan, ports=_ports(yt))
    run = Path(first['run_dir'])
    (run / 'candidates.json').write_text('[]')
    searches = yt.searches
    resumed = advance(root, brief=brief, plan=plan, run_dir=run, ports=_ports(yt))
    assert resumed['status'] == 'recovery', resumed
    assert resumed['stage'] == 'discover'
    assert yt.searches == searches


def test_interrupted_discovery_does_not_accept_old_nonempty_files(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()
    first = advance(root, brief=brief, plan=plan, ports=_ports(yt))
    run = Path(first['run_dir'])
    state = json.loads((run / 'runner_state.json').read_text())
    (run / 'runner_inflight.json').write_text(json.dumps({
        'stage': 'discover', 'binding': state['completed']['discover']['binding']}))
    searches = yt.searches
    resumed = advance(root, brief=brief, plan=plan, run_dir=run, ports=_ports(yt))
    assert resumed['status'] == 'recovery', resumed
    assert resumed['stage'] == 'discover'
    assert yt.searches == searches
