import json
from dataclasses import replace
from pathlib import Path
import pytest
from PIL import Image
from scenery_brief_clips.vision_wire import (
    VisionWire, VisionWireError, label_ranked_tiles, label_review_strips)

WIRE = VisionWire('codex-login', 'test-model')


def setup_tiles(root):
    tiles = []
    for i in range(2):
        p = root / f'{i}.jpg'
        Image.new('RGB', (8, 8), (20 + i * 50, 80, 40)).save(p)
        tiles.append({'path': str(p), 't_s': i})
    (root / 'ranked.json').write_text(json.dumps([{'video_id': 'abc', 'tiles': tiles}]))


def test_checkpoint_hit_rejects_image_changed_during_lookup(tmp_path, monkeypatch):
    setup_tiles(tmp_path)
    def caller(wire, path, prompt):
        return '{"label":"keep","look":"europe_like","note":"coast"}'
    label_ranked_tiles(tmp_path, WIRE, caller)
    original = Path.read_text
    def changing_read(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if path.parent.name == 'vision_labels':
            Image.new('RGB', (8, 8), (222, 0, 0)).save(tmp_path / '0.jpg')
        return text
    monkeypatch.setattr(Path, 'read_text', changing_read)
    result = label_ranked_tiles(tmp_path, WIRE, caller)
    assert any('image changed' in f['error'] for f in result['failures'])


def test_successful_tile_judgments_survive_a_failed_batch(tmp_path):
    setup_tiles(tmp_path)
    calls = []
    def first(wire, path, prompt):
        calls.append(Path(path).name)
        if Path(path).name == '1.jpg':
            raise VisionWireError('temporary failure')
        return '{"label":"keep","look":"europe_like","note":"coast"}'
    result = label_ranked_tiles(tmp_path, WIRE, first)
    assert len(result['failures']) == 1
    calls.clear()
    def recovered(wire, path, prompt):
        calls.append(Path(path).name)
        return '{"label":"keep","look":"europe_like","note":"coast"}'
    result = label_ranked_tiles(tmp_path, WIRE, recovered)
    assert not result['failures']
    assert calls == ['1.jpg']
    calls.clear()
    assert label_ranked_tiles(tmp_path, WIRE, recovered) == result
    assert calls == []


@pytest.mark.parametrize('change', ['model', 'endpoint', 'image', 'prompt', 'corrupt'])
def test_checkpoint_binding_invalidates_only_affected_work(tmp_path, change):
    setup_tiles(tmp_path)
    calls = []
    def caller(wire, path, prompt):
        calls.append(str(path))
        return '{"label":"keep","look":"europe_like","note":"coast"}'
    label_ranked_tiles(tmp_path, WIRE, caller)
    calls.clear()
    wire = WIRE
    expected = 2
    if change == 'model':
        wire = replace(WIRE, model='different')
    elif change == 'endpoint':
        wire = replace(WIRE, base_url='https://different.test')
    elif change == 'image':
        Image.new('RGB', (8, 8), (200, 80, 40)).save(tmp_path / '0.jpg')
        expected = 1
    elif change == 'prompt':
        (tmp_path / 'constraint.json').write_text('{"theme_text":"forest"}')
    else:
        for p in (tmp_path / 'vision_labels').glob('*.json'):
            p.write_text('{broken')
    result = label_ranked_tiles(tmp_path, wire, caller)
    assert not result['failures']
    assert len(calls) == expected


def test_strip_checkpoint_retains_generation_and_current_suspect_flag(tmp_path):
    setup_tiles(tmp_path)
    review = {'excerpts_sha256': 'a', 'moments': [
        {'video_id': 'abc', 'excerpt_index': 0, 'strip': '0.jpg'}]}
    (tmp_path / 'review.json').write_text(json.dumps(review))
    calls = []
    def caller(wire, path, prompt):
        calls.append(path)
        return '{"match":"keep","geo":"uncertain","scene_type":"coast","note":"waves","continuity_ok":false}'
    first = label_review_strips(tmp_path, WIRE, caller)
    review['excerpts_sha256'] = 'b'
    review['moments'][0]['continuity_suspect'] = True
    (tmp_path / 'review.json').write_text(json.dumps(review))
    second = label_review_strips(tmp_path, WIRE, caller)
    assert len(calls) == 1
    assert second['scores']['excerpts_sha256'] == 'b'
    assert second['scores']['abc'][0]['note'].startswith('continuity not cleared:')
    assert first['scores']['abc'][0]['note'] == 'waves'
