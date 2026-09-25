import json
from pathlib import Path
import pytest
from scenery_brief_clips.cli import main
from test_runner_contract import (Guard, FakeYt, _root, _brief_plan, _ports,
                                  _patch_media, _tile_text, _strip_text)


@pytest.mark.parametrize('live,agree,expected', [
    (False, True, 'label_tiles'), (True, False, 'agree_vision'),
    (True, True, 'verify_export')])
def test_cli_live_vision_is_opt_in_and_approval_gated(tmp_path, monkeypatch, capsys,
                                                   live, agree, expected):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / 'acq'
    yt.base.mkdir()
    _patch_media(monkeypatch, yt)
    ports = _ports(yt)
    yt.fetch_analysis = lambda vid, dest, span, timeout: ports.fetch_span(vid, dest, span)
    yt.invalidate_analysis = lambda *a, **k: None
    monkeypatch.setattr('scenery_brief_clips.yt.YtDlp', lambda **kw: yt)
    monkeypatch.setattr('scenery_brief_clips.fetch.cached_fetcher', lambda *a: ports.rank_fetcher)
    monkeypatch.setattr('scenery_brief_clips.detect.detect_scenes', ports.detect_fn)
    # Default arguments bind these at import, so replace only external probe seams.
    import scenery_brief_clips.verify as verify
    import scenery_brief_clips.runner as runner
    original = runner.verify_run
    def real_verify(*args, **kw):
        kw.update(probe_fn=ports.verify_probe, decode_fn=ports.verify_decode,
                  export_probe_fn=ports.export_probe)
        return original(*args, **kw)
    monkeypatch.setattr(runner, 'verify_run', real_verify)
    calls = []
    def vision(wire, path, prompt):
        calls.append(str(path))
        return (_strip_text if 'strip' in prompt else _tile_text)(wire, path, prompt)
    monkeypatch.setattr('scenery_brief_clips.vision_wire.call_wired_vision', vision)
    args = ['run-pipeline', '--root', str(root), '--brief', str(brief),
            '--plan', str(plan), '--allow-export', '--theme', 'cli-smoke']
    if live:
        args.append('--live-vision')
    if agree:
        args.append('--vision-agree')
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['stage'] == expected, result
    if live and agree:
        assert result['status'] == 'completed', result
        assert len(calls) == 2
        assert yt.exports == 1
    else:
        assert not calls
        assert yt.exports == 0
