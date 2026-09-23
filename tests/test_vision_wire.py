import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from scenery_brief_clips.cli import main
from scenery_brief_clips.vision_wire import (
    VisionWireError,
    brief_prompt,
    label_ranked_tiles,
    load_vision_wire,
    parse_model_json,
    plain_description,
)


def _jpeg(path: Path) -> None:
    Image.new("RGB", (8, 8), (20, 80, 40)).save(path, format="JPEG")


def _write_wire(root: Path, text: str) -> None:
    (root / "vision.yaml").write_text(text, encoding="utf-8")


def test_action_brief_prompt_has_no_generic_people_rejection():
    for kind in ("tile", "strip"):
        prompt = brief_prompt(kind, "Trees being cut down")
        assert "Trees being cut down" in prompt
        assert "reject means people as the subject" not in prompt
        assert "people or machines actively doing the requested action" in prompt
        assert "incidental people" in prompt


def test_scenery_brief_still_rejects_people_as_subject():
    prompt = brief_prompt("tile", "Beautiful natural european scenery")
    assert "people as the subject" in prompt
    assert "towns or cities as the subject" in prompt


def test_shipped_vision_yaml_is_the_codex_login():
    wire = load_vision_wire(Path(__file__).resolve().parents[1])
    assert wire.backend == "codex-login"
    assert wire.model == "gpt-6-sol"
    assert "ChatGPT/Codex sign-in" in plain_description(wire)
    assert "gpt-6-sol" in plain_description(wire)


def test_openai_api_switch_is_only_a_file_change(tmp_path):
    _write_wire(
        tmp_path,
        "backend: openai-api\nmodel: gpt-4.1\nbase_url: https://api.example.test/v1\napi_key_env: OPENAI_API_KEY\n",
    )
    wire = load_vision_wire(tmp_path)
    text = plain_description(wire)
    assert wire.backend == "openai-api"
    assert wire.model == "gpt-4.1"
    assert "api.example.test" in text
    assert "OPENAI_API_KEY" in text
    assert "sk-" not in text


def test_vision_yaml_rejects_a_pasted_secret(tmp_path):
    _write_wire(tmp_path, "backend: codex-login\nmodel: sk-live-secret\n")
    with pytest.raises(VisionWireError, match="secret"):
        load_vision_wire(tmp_path)


def test_parse_model_json_strips_a_fence():
    payload = parse_model_json('```json\n{"label": "reject"}\n```')
    assert payload["label"] == "reject"


def test_label_tiles_uses_the_injected_caller_and_skips_dark(tmp_path):
    _write_wire(tmp_path, "backend: codex-login\nmodel: gpt-6-astra\n")
    run = tmp_path / "data" / "runs" / "demo"
    run.mkdir(parents=True)
    picture = tmp_path / "tile.jpg"
    _jpeg(picture)
    (run / "ranked.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "abc",
                    "tiles": [
                        {"path": str(picture), "t_s": 1.5, "dark": False},
                        {"path": str(tmp_path / "dark.jpg"), "t_s": 0.0, "dark": True},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    seen = []

    def caller(wire, image_path, prompt):
        seen.append((wire.model, str(image_path), "watermark" in prompt))
        return '{"label":"keep","look":"europe_like","note":"a lake and mountains"}'

    result = label_ranked_tiles(run, load_vision_wire(tmp_path), caller)
    assert result["failures"] == []
    assert seen == [("gpt-6-astra", str(picture), True)]
    rows = result["scores"]["abc"]
    assert rows[0]["label"] == "keep"
    assert rows[1]["label"] == "reject"
    assert "dark" in rows[1]["note"]


def test_tile_labels_overlap_at_most_two_calls_and_keep_order(tmp_path):
    _write_wire(tmp_path, "backend: codex-login\nmodel: gpt-6-sol\n")
    run = tmp_path / "run"
    run.mkdir()
    rows = [
        {"video_id": str(i), "tiles": [{"path": f"tile-{i}-{j}.jpg", "t_s": j} for j in range(2)]}
        for i in range(2)
    ]
    (run / "ranked.json").write_text(json.dumps(rows), encoding="utf-8")
    lock = threading.Lock()
    active = 0
    peak = 0

    def caller(wire, path, prompt):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return '{"label":"keep","look":"europe_like","note":"matched"}'

    result = label_ranked_tiles(run, load_vision_wire(tmp_path), caller)
    assert result["failures"] == []
    assert peak == 2
    assert [[entry["t_s"] for entry in result["scores"][str(i)]] for i in range(2)] == [[0, 1], [0, 1]]


def test_tile_label_failure_is_reported_even_if_ranked_row_has_no_id(tmp_path):
    _write_wire(tmp_path, "backend: codex-login\nmodel: gpt-6-sol\n")
    run = tmp_path / "run"
    run.mkdir()
    (run / "ranked.json").write_text(
        json.dumps([{"tiles": [{"path": "broken.jpg", "t_s": 1, "dark": False}]}]),
        encoding="utf-8",
    )

    def caller(wire, path, prompt):
        raise VisionWireError("model unavailable")

    result = label_ranked_tiles(run, load_vision_wire(tmp_path), caller)
    assert result["scores"] == {}
    assert result["failures"] == [{"path": "broken.jpg", "error": "model unavailable"}]


def test_label_tiles_refuses_without_confirmation(tmp_path, capsys):
    _write_wire(tmp_path, "backend: codex-login\nmodel: other-model\n")
    run = tmp_path / "run"
    run.mkdir()
    code = main(["label-tiles", "--run-dir", str(run), "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "other-model" in err
    assert "--confirm-vision" in err
    assert not (run / "vision_scores.json").exists()


def test_vision_show_prints_the_wired_model(tmp_path, capsys):
    _write_wire(tmp_path, "backend: codex-login\nmodel: gpt-6-astra\n")
    code = main(["vision-show", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "gpt-6-astra" in out
    assert "ChatGPT/Codex sign-in" in out
