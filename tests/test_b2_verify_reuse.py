"""verify_run trusted_decodes / decoded_media / n_decode_reused and runner._review_decodes.

Every media, probe and decode call is a fake. No network.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scenery_brief_clips import runner
from scenery_brief_clips.analysis_cache import analysis_cache_path, analysis_marker_path
from scenery_brief_clips.pipeline_analyze import sha256_file
from scenery_brief_clips.runner import advance
from scenery_brief_clips.verify import verify_run
from test_runner_contract import (
    FakeYt,
    Guard,
    _brief_plan,
    _patch_media,
    _ports,
    _root,
    _strip_text,
    _tile_text,
)
from test_verify import _probe, _refresh_manifest, _valid_run, _write


class CountingDecode:
    def __init__(self, fail_on: set[str] | None = None):
        self.calls: list[str] = []
        self.fail_on = fail_on or set()

    def __call__(self, path):
        self.calls.append(str(path))
        if str(path) in self.fail_on:
            raise RuntimeError("corrupt stream")

    def count(self, path) -> int:
        return self.calls.count(str(path))


def _two_media_run(tmp_path: Path):
    """_valid_run plus a second analyzed source (as in test_verify)."""
    run_dir, media = _valid_run(tmp_path)
    other_id = "bbbbbbbbbbb"
    other = analysis_cache_path(tmp_path / "analysis", other_id, (10.0, 20.0))
    shutil.copyfile(media, other)
    marker = json.loads(analysis_marker_path(media).read_text())
    marker["video_id"] = other_id
    _write(analysis_marker_path(other), marker)
    candidates = json.loads((run_dir / "candidates.json").read_text())
    candidates.append({**candidates[0], "video_id": other_id})
    _write(run_dir / "candidates.json", candidates)
    ranked = json.loads((run_dir / "ranked.json").read_text())
    ranked.append({**ranked[0], "video_id": other_id})
    _write(run_dir / "ranked.json", ranked)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    empty = json.loads(json.dumps(rows[0]))
    empty["video_id"] = other_id
    empty["ready_for_shortlist"] = False
    empty["excerpts"] = []
    for copy in empty["copies"] + empty["ranges"]:
        copy["path"] = str(other)
        copy["cache_key"] = other.name
    rows.append(empty)
    _write(run_dir / "excerpts.json", rows)
    manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    manifest["settings"]["max_videos"] = 2
    _write(run_dir / "analysis_manifest.json", manifest)
    _refresh_manifest(run_dir)
    return run_dir, media, other


def _verify(run_dir, decode, trusted=None):
    return verify_run(run_dir, probe_fn=_probe, decode_fn=decode, trusted_decodes=trusted)


class TestVerifyReuse:
    def test_no_trust_decodes_every_media_once(self, tmp_path):
        run_dir, media, other = _two_media_run(tmp_path)
        decode = CountingDecode()
        report = _verify(run_dir, decode)
        assert report["ok"] is True, report["errors"]
        assert report["n_media"] == 2
        assert report["n_decode_reused"] == 0
        assert report["decoded_media"] == {
            str(media): sha256_file(media),
            str(other): sha256_file(other),
        }
        assert all(Path(p).is_absolute() for p in report["decoded_media"])
        assert decode.count(media) == 1 and decode.count(other) == 1
        assert len(decode.calls) == 2

    def test_trusted_hashes_skip_all_decodes(self, tmp_path):
        run_dir, media, other = _two_media_run(tmp_path)
        first = _verify(run_dir, CountingDecode())
        decode = CountingDecode()
        report = _verify(run_dir, decode, first["decoded_media"])
        assert decode.calls == []
        assert report["n_decode_reused"] == 2
        assert report["ok"] is first["ok"] is True
        assert report["errors"] == first["errors"]
        assert report["decoded_media"] == first["decoded_media"]

    def test_wrong_trusted_hash_redecodes_only_that_path(self, tmp_path):
        run_dir, media, other = _two_media_run(tmp_path)
        trusted = _verify(run_dir, CountingDecode())["decoded_media"]
        trusted[str(other)] = "0" * 64
        decode = CountingDecode()
        report = _verify(run_dir, decode, trusted)
        assert decode.calls == [str(other)]
        assert report["n_decode_reused"] == 1
        assert report["ok"] is True
        assert report["decoded_media"] == {
            str(media): sha256_file(media),
            str(other): sha256_file(other),
        }

    def test_trusted_path_not_in_run_is_ignored(self, tmp_path):
        run_dir, media = _valid_run(tmp_path)
        decode = CountingDecode()
        report = _verify(run_dir, decode, {str(tmp_path / "nope.mp4"): "a" * 64})
        assert decode.calls == [str(media)]
        assert report["n_decode_reused"] == 0

    def test_changed_bytes_are_flagged_and_not_recorded(self, tmp_path):
        run_dir, media = _valid_run(tmp_path)
        trusted = _verify(run_dir, CountingDecode())["decoded_media"]
        assert trusted == {str(media): sha256_file(media)}
        with media.open("ab") as handle:
            handle.write(b"x")
        decode = CountingDecode()
        report = _verify(run_dir, decode, trusted)
        assert report["ok"] is False
        assert any("media hash changed" in e for e in report["errors"])
        assert any("media size changed" in e for e in report["errors"])
        assert str(media) not in report["decoded_media"]
        assert report["decoded_media"] == {}
        assert report["n_decode_reused"] == 0

    def test_changed_bytes_without_trust_are_not_recorded_either(self, tmp_path):
        run_dir, media = _valid_run(tmp_path)
        with media.open("ab") as handle:
            handle.write(b"x")
        report = _verify(run_dir, CountingDecode())
        assert report["ok"] is False
        assert report["decoded_media"] == {}

    def test_trusted_path_is_not_decoded_even_if_decode_would_fail(self, tmp_path):
        run_dir, media = _valid_run(tmp_path)
        decode = CountingDecode(fail_on={str(media)})
        report = _verify(run_dir, decode, {str(media): sha256_file(media)})
        assert decode.calls == []
        assert report["ok"] is True
        assert report["n_decode_reused"] == 1

    def test_hash_mismatch_decodes_and_reports_failure(self, tmp_path):
        run_dir, media = _valid_run(tmp_path)
        decode = CountingDecode(fail_on={str(media)})
        report = _verify(run_dir, decode, {str(media): "f" * 64})
        assert decode.calls == [str(media)]
        assert report["ok"] is False
        assert any(f"decode failed for {media}" in e for e in report["errors"])
        assert report["decoded_media"] == {}
        assert report["n_decode_reused"] == 0

    def test_failed_decode_is_not_recorded_without_trust(self, tmp_path):
        run_dir, media, other = _two_media_run(tmp_path)
        report = _verify(run_dir, CountingDecode(fail_on={str(other)}))
        assert report["ok"] is False
        assert report["decoded_media"] == {str(media): sha256_file(media)}


class TestReviewDecodes:
    def _write_review(self, run_dir: Path, payload) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "verify_review.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_file(self, tmp_path):
        assert runner._review_decodes(tmp_path) == {}

    def test_unreadable_file(self, tmp_path):
        (tmp_path / "verify_review.json").write_text("{not json", encoding="utf-8")
        assert runner._review_decodes(tmp_path) == {}

    def test_non_utf8_file(self, tmp_path):
        (tmp_path / "verify_review.json").write_bytes(b"\xff\xfe\x00")
        assert runner._review_decodes(tmp_path) == {}

    def test_verify_review_is_a_directory(self, tmp_path):
        (tmp_path / "verify_review.json").mkdir()
        assert runner._review_decodes(tmp_path) == {}

    def test_not_ok(self, tmp_path):
        self._write_review(tmp_path, {"ok": False, "decoded_media": {"/a": "a" * 64}})
        assert runner._review_decodes(tmp_path) == {}

    def test_ok_missing(self, tmp_path):
        self._write_review(tmp_path, {"decoded_media": {"/a": "a" * 64}})
        assert runner._review_decodes(tmp_path) == {}

    def test_lacks_decoded_media(self, tmp_path):
        self._write_review(tmp_path, {"ok": True})
        assert runner._review_decodes(tmp_path) == {}

    @pytest.mark.parametrize("value", [["/a"], "x", 3, None])
    def test_decoded_media_not_a_dict(self, tmp_path, value):
        self._write_review(tmp_path, {"ok": True, "decoded_media": value})
        assert runner._review_decodes(tmp_path) == {}

    def test_top_level_not_a_dict(self, tmp_path):
        self._write_review(tmp_path, [1, 2])
        assert runner._review_decodes(tmp_path) == {}

    def test_filters_bad_hash_values(self, tmp_path):
        good = "a" * 64
        self._write_review(
            tmp_path,
            {
                "ok": True,
                "decoded_media": {
                    "/good": good,
                    "/short": "abc",
                    "/long": "b" * 65,
                    "/int": 5,
                    "/none": None,
                    "/list": ["c" * 64],
                },
            },
        )
        assert runner._review_decodes(tmp_path) == {"/good": good}

    def test_returns_dict_unchanged(self, tmp_path):
        payload = {"/x/a.mp4": "a" * 64, "/x/b.mp4": "b" * 64}
        self._write_review(tmp_path, {"ok": True, "decoded_media": payload})
        assert runner._review_decodes(tmp_path) == payload


def test_advance_decodes_each_analysis_media_once_across_review_and_export(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / "acq"
    yt.base.mkdir()
    _patch_media(monkeypatch, yt)
    ports = _ports(yt, tile_caller=_tile_text, strip_caller=_strip_text)
    decode = CountingDecode()
    ports.verify_decode = decode

    result = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        vision_agree=True,
        allow_export=True,
        theme="reuse-theme",
        ports=ports,
    )
    assert result["status"] == "completed", result
    run_dir = Path(result["run_dir"])
    review = json.loads((run_dir / "verify_review.json").read_text(encoding="utf-8"))
    export = json.loads((run_dir / "verify_export.json").read_text(encoding="utf-8"))
    assert review["ok"] is True and export["ok"] is True

    media = list(review["decoded_media"])
    assert media, review
    assert review["n_decode_reused"] == 0
    for path in media:
        assert decode.count(path) == 1, (path, decode.calls)
    assert export["n_decode_reused"] == len(media) == export["n_media"]
    assert export["decoded_media"] == review["decoded_media"]
    # Export clips are decoded separately (not analysis media).
    assert set(decode.calls) - set(media)
