"""Strip label budget: label_review_strips(budget=True) stops at n_clips distinct keeps.

Every model call is a fake caller injected into label_review_strips. No network, no key.

Fixture vocabulary: a "moment" is a dict from _m(video, index, ...); _make_run() writes
review.json (moments with distinct strip images strip-<position>.png, position = review
order), constraint.json, and returns the run dir plus the matching excerpts rows.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from scenery_brief_clips import runner
from scenery_brief_clips.cli import main
from scenery_brief_clips.config import ConfigError, load_project_config
from scenery_brief_clips.runner import _bindings, strip_label_budget_on
from scenery_brief_clips.shortlist import (
    DEDUP_HAMMING_MAX,
    REASON_UNLABELED_DUPLICATE_PREFIX,
    REASON_UNLABELED_ENOUGH,
    UNLABELED_KEY,
    ShortlistInputError,
    build_shortlist,
    label_keeps,
    unlabeled_reasons,
    validate_label_payload,
)
from scenery_brief_clips.vision_wire import (
    VisionWire,
    _interleaved_by_source,
    _run_n_clips,
    _signature,
    label_review_strips,
)
from test_shortlist import _bindings as shortlist_bindings
from test_shortlist import _review_fixture, _rows_fixture, _unique_hashes

WIRE = VisionWire("codex-login", "test-model")
KEEP = {"match": "keep", "geo": "supported", "scene_type": "field", "note": "a field", "continuity_ok": False}
ENOUGH = "not labeled: enough clips kept"


# ---------------------------------------------------------------- fixtures


def _h(tag: str) -> str:
    """A 16-char hex frame hash, effectively random per tag (far apart in Hamming distance)."""
    return hashlib.sha256(tag.encode()).hexdigest()[:16]


def _flip(hash_hex: str, bits: int) -> str:
    """The same hash with its lowest `bits` bits flipped: Hamming distance exactly `bits`."""
    return f"{int(hash_hex, 16) ^ ((1 << bits) - 1):016x}"


def _m(video: str, index: int, hashes=None, **extra) -> dict:
    """One review moment spec; frame hashes default to a unique hash per (video, index)."""
    if hashes is None:
        hashes = [_h(f"{video}:{index}")]
    return {"video_id": video, "excerpt_index": index, "frame_hashes": hashes, **extra}


def _make_run(tmp_path: Path, moments: list[dict], n_clips=None, raw_constraint: str | None = None,
              name: str = "run"):
    run_dir = tmp_path / name
    run_dir.mkdir()
    review_moments = []
    rows: dict[str, dict] = {}
    for position, spec in enumerate(moments):
        strip = run_dir / f"strip-{position}.png"
        Image.new("RGB", (8, 8), (position % 256, position // 256, 77)).save(strip)
        index = spec["excerpt_index"]
        video = spec["video_id"]
        start = 10.0 * index
        key = f"{video}_{index}"
        review_moments.append({
            **spec,
            "start_s": start, "end_s": start + 6.0, "analysis_cache_key": key,
            "strip": strip.name,
        })
        row = rows.setdefault(video, {"video_id": video, "title": video, "priority": "promising",
                                      "status": "complete", "excerpts": {}})
        row["excerpts"][index] = {"start_s": start, "end_s": start + 6.0, "analysis_cache_key": key}
    (run_dir / "review.json").write_text(
        json.dumps({"schema_version": 1, "excerpts_sha256": "e" * 64, "moments": review_moments}),
        encoding="utf-8",
    )
    if raw_constraint is not None:
        (run_dir / "constraint.json").write_text(raw_constraint, encoding="utf-8")
    elif n_clips is not None:
        (run_dir / "constraint.json").write_text(json.dumps({"theme_text": "alpine lakes", "n_clips": n_clips}),
                                                 encoding="utf-8")
    shortlist_rows = []
    for row in rows.values():
        excerpts = [row["excerpts"][i] for i in sorted(row["excerpts"])]
        assert [sorted(row["excerpts"])[i] for i in range(len(excerpts))] == list(range(len(excerpts)))
        shortlist_rows.append({**row, "excerpts": excerpts})
    return run_dir, shortlist_rows


def _name(position: int) -> str:
    return f"strip-{position}.png"


class Caller:
    """Fake vision caller. replies: strip position -> label field overrides. bad: positions that
    answer with text that is not JSON (both attempts). delay: strip name -> seconds to sleep."""

    def __init__(self, replies=None, bad=(), delay=None):
        self.replies = {_name(p): r for p, r in (replies or {}).items()}
        self.bad = {_name(p) for p in bad}
        self.delay = delay
        self.lock = threading.Lock()
        self.calls: list[str] = []

    def __call__(self, wire, path, prompt):
        name = Path(path).name
        with self.lock:
            self.calls.append(name)
        if self.delay is not None:
            time.sleep(self.delay(name))
        if name in self.bad:
            return "sorry, no idea"
        return json.dumps({**KEEP, **self.replies.get(name, {})})


def _videos(scores: dict) -> dict:
    return {k: v for k, v in scores.items() if k not in ("excerpts_sha256", UNLABELED_KEY)}


def _labeled(result: dict) -> set[tuple[str, int]]:
    return {(video, e["excerpt_index"]) for video, entries in _videos(result["scores"]).items() for e in entries}


def _unlabeled(result: dict) -> dict[tuple[str, int], str]:
    return {(u["video_id"], u["excerpt_index"]): u["reason"] for u in result["scores"].get(UNLABELED_KEY, [])}


def _call_positions(caller: Caller) -> list[int]:
    return [int(n[len("strip-"):-len(".png")]) for n in caller.calls]


@pytest.fixture(autouse=True)
def _workers_default(monkeypatch):
    monkeypatch.setenv("SCENERY_VISION_WORKERS", "1")


def _workers(monkeypatch, n: int) -> None:
    monkeypatch.setenv("SCENERY_VISION_WORKERS", str(n))


# ------------------------------------------------------ 1. budget=False regression


def test_budget_false_labels_every_moment_and_has_no_unlabeled_key(tmp_path):
    moments = [_m("vidA", 0), _m("vidA", 1), _m("vidB", 0), _m("vidC", 0), _m("vidC", 1), _m("vidB", 1)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=False)
    assert result["failures"] == []
    assert UNLABELED_KEY not in result["scores"]
    assert len(caller.calls) == len(moments)
    assert _labeled(result) == {(m["video_id"], m["excerpt_index"]) for m in moments}
    assert result["scores"]["excerpts_sha256"] == "e" * 64


def test_budget_defaults_to_false(tmp_path):
    run_dir, _rows = _make_run(tmp_path, [_m("vidA", 0), _m("vidB", 0), _m("vidC", 0)], n_clips=1)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller)
    assert len(caller.calls) == 3
    assert UNLABELED_KEY not in result["scores"]


def test_budget_false_does_not_label_fewer_even_with_duplicates(tmp_path):
    shared = _h("same")
    moments = [_m("vidA", 0, [shared]), _m("vidB", 0, [shared]), _m("vidC", 0, [shared])]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=False)
    assert len(caller.calls) == 3
    assert UNLABELED_KEY not in result["scores"]


# ------------------------------------------------------ 2. stop at n_clips keeps


def test_budget_stops_at_n_clips_keeps_and_lists_the_rest_sorted_by_review_position(tmp_path):
    # review order A0 A1 B0 B1 C0 C1 (positions 0..5); interleaved: A0 B0 C0 A1 B1 C1
    moments = [_m("vidA", 0), _m("vidA", 1), _m("vidB", 0), _m("vidB", 1), _m("vidC", 0), _m("vidC", 1)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=2)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert result["failures"] == []
    assert _call_positions(caller) == [0, 2]  # A0 then B0, then enough
    assert _labeled(result) == {("vidA", 0), ("vidB", 0)}
    assert result["scores"][UNLABELED_KEY] == [
        {"video_id": "vidA", "excerpt_index": 1, "reason": ENOUGH},
        {"video_id": "vidB", "excerpt_index": 1, "reason": ENOUGH},
        {"video_id": "vidC", "excerpt_index": 0, "reason": ENOUGH},
        {"video_id": "vidC", "excerpt_index": 1, "reason": ENOUGH},
    ]
    assert REASON_UNLABELED_ENOUGH == ENOUGH
    assert result["scores"]["excerpts_sha256"] == "e" * 64
    # Every moment is labeled or listed, never both, never neither.
    assert _labeled(result).isdisjoint(_unlabeled(result))
    assert len(_labeled(result)) + len(_unlabeled(result)) == len(moments)


def test_budget_with_more_keeps_needed_than_moments_labels_everything_without_unlabeled_key(tmp_path):
    moments = [_m("vidA", 0), _m("vidB", 0), _m("vidA", 1)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=20)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert len(caller.calls) == 3
    assert UNLABELED_KEY not in result["scores"]


def test_budget_output_matches_unbudgeted_output_when_nothing_is_skipped(tmp_path):
    # Scrambled review order across three sources: entries must be grouped per video in review order.
    moments = [_m("vidB", 0), _m("vidA", 0), _m("vidB", 1), _m("vidC", 0), _m("vidA", 1), _m("vidB", 2)]
    run_a, _ = _make_run(tmp_path, moments, n_clips=99, name="a")
    run_b, _ = _make_run(tmp_path, moments, n_clips=99, name="b")
    budgeted = label_review_strips(run_a, WIRE, Caller(), budget=True)
    plain = label_review_strips(run_b, WIRE, Caller(), budget=False)
    assert budgeted == plain
    assert list(_videos(budgeted["scores"])) == ["vidB", "vidA", "vidC"]
    assert [e["excerpt_index"] for e in budgeted["scores"]["vidB"]] == [0, 1, 2]


def test_budget_labeled_entries_are_grouped_per_video_in_review_order(tmp_path):
    moments = [_m("vidB", 0), _m("vidA", 0), _m("vidB", 1), _m("vidA", 1), _m("vidB", 2), _m("vidA", 2)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=4)
    result = label_review_strips(run_dir, WIRE, Caller(), budget=True)
    # interleaved B0 A0 B1 A1 -> 4 keeps; B2 and A2 unlabeled.
    assert [e["excerpt_index"] for e in result["scores"]["vidB"]] == [0, 1]
    assert [e["excerpt_index"] for e in result["scores"]["vidA"]] == [0, 1]
    assert _unlabeled(result) == {("vidB", 2): ENOUGH, ("vidA", 2): ENOUGH}
    assert [u["video_id"] for u in result["scores"][UNLABELED_KEY]] == ["vidB", "vidA"]  # review positions 4, 5


# ------------------------------------------------------ 3. interleaving


def test_workers_1_sees_strips_in_round_robin_source_order(tmp_path):
    # review: B0 B1 B2 A0 A1 C0 -> positions 0 1 2 3 4 5; sources in first-appearance order B, A, C.
    moments = [_m("vidB", 0), _m("vidB", 1), _m("vidB", 2), _m("vidA", 0), _m("vidA", 1), _m("vidC", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=99)
    caller = Caller()
    label_review_strips(run_dir, WIRE, caller, budget=True)
    # round 1: B0 A0 C0, round 2: B1 A1, round 3: B2
    assert _call_positions(caller) == [0, 3, 5, 1, 4, 2]


def test_interleave_helper_keeps_each_source_in_review_order():
    moments = [({}, "b", None), ({}, "a", None), ({}, "b", None), ({}, "b", None), ({}, "a", None)]
    assert _interleaved_by_source(moments) == [0, 1, 2, 4, 3]
    assert _interleaved_by_source([]) == []


# ------------------------------------------------------ 4. wave size


def test_wave_is_always_completed_workers_4_n_clips_1(tmp_path, monkeypatch):
    _workers(monkeypatch, 4)
    moments = [_m(f"vid{c}", i) for c in "ABC" for i in range(3)]  # 9 moments
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert len(caller.calls) == 4
    # first wave = first four in interleaved order: A0 B0 C0 A1 -> review positions 0, 3, 6, 1
    assert sorted(_call_positions(caller)) == [0, 1, 3, 6]
    assert _labeled(result) == {("vidA", 0), ("vidB", 0), ("vidC", 0), ("vidA", 1)}
    assert len(_unlabeled(result)) == 5
    assert set(_unlabeled(result).values()) == {ENOUGH}


@pytest.mark.parametrize(
    "workers,n_clips,expected_calls",
    [(1, 3, 3), (2, 3, 4), (3, 4, 6), (4, 1, 4), (4, 5, 8), (8, 2, 8), (2, 10, 10), (4, 20, 10)],
)
def test_call_count_is_a_whole_number_of_waves_capped_at_total(tmp_path, monkeypatch, workers, n_clips, expected_calls):
    _workers(monkeypatch, workers)
    moments = [_m(f"vid{c}", i) for c in "ABCDE" for i in range(2)]  # 10 distinct moments
    assert expected_calls == min(len(moments), math.ceil(n_clips / workers) * workers)
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=n_clips)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert len(caller.calls) == expected_calls
    # The labeled moments are the first `expected_calls` of the interleaved order (review positions
    # A0 A1 B0 B1 ... -> interleaved 0 2 4 6 8 1 3 5 7 9).
    interleaved = [0, 2, 4, 6, 8, 1, 3, 5, 7, 9]
    assert sorted(_call_positions(caller)) == sorted(interleaved[:expected_calls])
    assert len(_unlabeled(result)) == len(moments) - expected_calls
    assert (UNLABELED_KEY in result["scores"]) == (expected_calls < len(moments))


# ------------------------------------------------------ 5. duplicate skip


def test_duplicate_of_a_kept_moment_is_never_sent_and_reports_the_kept_moment(tmp_path):
    # rounds: A0 B0 C0 | A1 C1   (C1 duplicates A1, which has a non-zero excerpt index)
    a1 = _h("kept-a1")
    moments = [_m("vidA", 0), _m("vidA", 1, [a1]), _m("vidB", 0), _m("vidC", 0), _m("vidC", 1, [a1])]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=10)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert _name(4) not in caller.calls
    assert len(caller.calls) == 4
    assert _unlabeled(result) == {("vidC", 1): "not labeled: duplicate of vidA:1"}
    assert result["scores"][UNLABELED_KEY][0]["reason"] == REASON_UNLABELED_DUPLICATE_PREFIX + "vidA:1"


@pytest.mark.parametrize(
    "bits,is_duplicate",
    [(0, True), (3, True), (DEDUP_HAMMING_MAX, True), (DEDUP_HAMMING_MAX + 1, False), (32, False)],
)
def test_duplicate_threshold_follows_the_shortlist_hamming_limit(tmp_path, bits, is_duplicate):
    base = _h("base")
    moments = [_m("vidA", 0, [base]), _m("vidB", 0, [_flip(base, bits)])]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=10)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    if is_duplicate:
        assert caller.calls == [_name(0)]
        assert _unlabeled(result) == {("vidB", 0): "not labeled: duplicate of vidA:0"}
    else:
        assert sorted(caller.calls) == [_name(0), _name(1)]
        assert UNLABELED_KEY not in result["scores"]


def test_duplicate_match_uses_any_frame_hash_of_the_moment(tmp_path):
    base = _h("base")
    moments = [_m("vidA", 0, [_h("x1"), base]), _m("vidB", 0, [_h("y1"), _flip(base, 2), _h("y2")])]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=10)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert caller.calls == [_name(0)]
    assert _unlabeled(result) == {("vidB", 0): "not labeled: duplicate of vidA:0"}


def test_duplicate_of_a_rejected_moment_is_still_labeled(tmp_path):
    shared = _h("shared")
    moments = [_m("vidA", 0, [shared]), _m("vidB", 0, [shared]), _m("vidC", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller(replies={0: {"match": "reject"}})
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert caller.calls == [_name(0), _name(1)]  # B0 sent although it looks like rejected A0
    assert _labeled(result) == {("vidA", 0), ("vidB", 0)}
    assert _unlabeled(result) == {("vidC", 0): ENOUGH}


def test_duplicate_skip_does_not_use_up_a_wave_slot(tmp_path, monkeypatch):
    _workers(monkeypatch, 2)
    shared = _h("shared")
    moments = [_m("vidA", 0, [shared]), _m("vidB", 0), _m("vidC", 0, [shared]),
               _m("vidD", 0), _m("vidE", 0), _m("vidF", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=4)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    # wave 1: A0 B0 (2 keeps). wave 2: C0 skipped as a duplicate of A0, so D0 and E0 fill it (4 keeps).
    assert sorted(_call_positions(caller)) == [0, 1, 3, 4]
    assert _unlabeled(result) == {("vidC", 0): "not labeled: duplicate of vidA:0", ("vidF", 0): ENOUGH}


def test_moments_without_valid_frame_hashes_never_count_as_duplicates(tmp_path):
    moments = [
        {"video_id": "vidA", "excerpt_index": 0},  # no frame_hashes at all
        {"video_id": "vidB", "excerpt_index": 0, "frame_hashes": []},
        {"video_id": "vidC", "excerpt_index": 0, "frame_hashes": ["nothex"]},
        _m("vidD", 0),
    ]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=10)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert len(caller.calls) == 4
    assert UNLABELED_KEY not in result["scores"]


def test_signature_helper():
    assert _signature({"frame_hashes": ["0000000000000001"]}) == (1,)
    assert _signature({"frame_hashes": [5]}) == (5,)
    assert _signature({}) == ()
    assert _signature({"frame_hashes": ["zz"]}) == ()
    assert _signature({"frame_hashes": "0000000000000001"}) == ()


# ------------------------------------------------------ 6. two duplicates in one wave


def test_two_duplicates_kept_in_the_same_wave_count_as_one_keep(tmp_path, monkeypatch):
    _workers(monkeypatch, 2)
    shared = _h("twin")
    moments = [_m("vidA", 0, [shared]), _m("vidB", 0, [shared]), _m("vidC", 0), _m("vidD", 0),
               _m("vidE", 0), _m("vidF", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=2)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    # Wave 1 (A0 B0) is two keeps by label but one distinct clip: labeling must continue. Wave 2 (C0 D0)
    # brings the distinct count to 3 >= 2. If the twins counted twice it would stop after two calls.
    assert sorted(_call_positions(caller)) == [0, 1, 2, 3]
    assert _labeled(result) == {("vidA", 0), ("vidB", 0), ("vidC", 0), ("vidD", 0)}
    assert _unlabeled(result) == {("vidE", 0): ENOUGH, ("vidF", 0): ENOUGH}


def test_twin_duplicate_in_a_wave_still_reaches_the_shortlist_as_one_clip(tmp_path, monkeypatch):
    _workers(monkeypatch, 2)
    shared = _h("twin")
    moments = [_m("vidA", 0, [shared]), _m("vidB", 0, [shared]), _m("vidC", 0), _m("vidD", 0), _m("vidE", 0)]
    run_dir, rows = _make_run(tmp_path, moments, n_clips=2)
    result = label_review_strips(run_dir, WIRE, Caller(), budget=True)
    review = json.loads((run_dir / "review.json").read_text())
    doc = build_shortlist(rows, review, validate_label_payload(result["scores"]), 2, shortlist_bindings())
    assert doc["counts"]["n_selected"] == 2
    excluded = {(e["video_id"], e["excerpt_index"]): e["reasons"] for e in doc["excluded"]}
    assert excluded[("vidB", 0)] == ["duplicate of vidA:0"]


# ------------------------------------------------------ 7. non-keeps do not count


@pytest.mark.parametrize(
    "reply",
    [{"match": "reject"}, {"match": "uncertain"}, {"geo": "conflicting"}],
    ids=["reject", "uncertain", "geo-conflicting"],
)
def test_non_keep_labels_do_not_count_toward_n_clips(tmp_path, reply):
    moments = [_m("vidA", 0), _m("vidB", 0), _m("vidC", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller(replies={0: reply})
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert caller.calls == [_name(0), _name(1)]
    assert _labeled(result) == {("vidA", 0), ("vidB", 0)}
    assert _unlabeled(result) == {("vidC", 0): ENOUGH}


def test_geo_uncertain_keep_counts_as_a_keep(tmp_path):
    moments = [_m("vidA", 0), _m("vidB", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller(replies={0: {"geo": "uncertain"}})
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert caller.calls == [_name(0)]
    assert _unlabeled(result) == {("vidB", 0): ENOUGH}


def test_continuity_suspect_moment_without_clearance_is_not_a_keep(tmp_path):
    moments = [_m("vidA", 0, continuity_suspect=True), _m("vidB", 0), _m("vidC", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller()  # continuity_ok false for everything
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert caller.calls == [_name(0), _name(1)]
    assert result["scores"]["vidA"][0]["note"].startswith("continuity not cleared:")
    assert _unlabeled(result) == {("vidC", 0): ENOUGH}


def test_continuity_suspect_moment_cleared_by_the_label_is_a_keep(tmp_path):
    moments = [_m("vidA", 0, continuity_suspect=True), _m("vidB", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller(replies={0: {"continuity_ok": True}})
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert caller.calls == [_name(0)]
    assert result["scores"]["vidA"][0]["continuity_ok"] is True
    assert _unlabeled(result) == {("vidB", 0): ENOUGH}


def test_continuity_ok_on_a_moment_that_is_not_suspect_is_irrelevant(tmp_path):
    moments = [_m("vidA", 0), _m("vidB", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller(replies={0: {"continuity_ok": False}})
    label_review_strips(run_dir, WIRE, caller, budget=True)
    assert caller.calls == [_name(0)]


def test_label_keeps_agrees_with_the_shortlist_exclusion_rules():
    ok = {"match": "keep", "geo": "supported", "scene_type": "x", "note": "n"}
    assert label_keeps(ok, {}) is True
    assert label_keeps({**ok, "match": "reject"}, {}) is False
    assert label_keeps({**ok, "match": "uncertain"}, {}) is False
    assert label_keeps({**ok, "geo": "conflicting"}, {}) is False
    assert label_keeps({**ok, "note_violation": True}, {}) is False
    assert label_keeps(ok, {"continuity_suspect": True}) is False
    assert label_keeps({**ok, "continuity_ok": True}, {"continuity_suspect": True}) is True
    assert label_keeps({**ok, "note": "continuity_ok: fine"}, {"continuity_suspect": True}) is True


# ------------------------------------------------------ 8. missing or invalid n_clips


@pytest.mark.parametrize(
    "raw",
    [
        None,  # no constraint.json at all
        "{}",
        json.dumps({"n_clips": 0}),
        json.dumps({"n_clips": -3}),
        json.dumps({"n_clips": True}),
        json.dumps({"n_clips": False}),
        json.dumps({"n_clips": "3"}),
        json.dumps({"n_clips": 2.0}),
        json.dumps({"n_clips": None}),
        "not json at all",
        "[]",
        '"text"',
    ],
    ids=["no-file", "absent", "zero", "negative", "true", "false", "string", "float", "null",
         "invalid-json", "list", "string-json"],
)
def test_missing_or_invalid_n_clips_labels_everything(tmp_path, raw):
    moments = [_m("vidA", 0), _m("vidB", 0), _m("vidC", 0), _m("vidA", 1)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=None, raw_constraint=raw)
    caller = Caller()
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert len(caller.calls) == 4
    assert UNLABELED_KEY not in result["scores"]
    assert result["failures"] == []


def test_invalid_n_clips_still_labels_duplicates(tmp_path):
    shared = _h("dup")
    moments = [_m("vidA", 0, [shared]), _m("vidB", 0, [shared])]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=None, raw_constraint="{}")
    caller = Caller()
    label_review_strips(run_dir, WIRE, caller, budget=True)
    assert len(caller.calls) == 2  # budget off in effect: no skipping of any kind


def test_run_n_clips_helper(tmp_path):
    def write(text):
        (tmp_path / "constraint.json").write_text(text, encoding="utf-8")

    assert _run_n_clips(tmp_path) is None  # missing file
    for text, expected in [('{"n_clips": 7}', 7), ('{"n_clips": 1}', 1), ('{"n_clips": 0}', None),
                           ('{"n_clips": true}', None), ('{"n_clips": "7"}', None), ("nope", None),
                           ("[1]", None)]:
        write(text)
        assert _run_n_clips(tmp_path) == expected, text


# ------------------------------------------------------ 9. caller failure


def test_caller_failure_is_reported_and_does_not_count_as_a_keep(tmp_path):
    moments = [_m("vidA", 0), _m("vidB", 0), _m("vidC", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller(bad={0})
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert len(result["failures"]) == 1
    assert result["failures"][0]["path"].endswith(_name(0))
    assert result["failures"][0]["error"]
    assert caller.calls.count(_name(0)) == 2  # label_image tries twice
    assert caller.calls.count(_name(1)) == 1
    assert _name(2) not in caller.calls
    assert _labeled(result) == {("vidB", 0)}
    # The failed moment is neither labeled nor claimed as skipped on purpose.
    assert _unlabeled(result) == {("vidC", 0): ENOUGH}


def test_caller_failure_in_a_wave_does_not_stop_the_other_members(tmp_path, monkeypatch):
    _workers(monkeypatch, 3)
    moments = [_m("vidA", 0), _m("vidB", 0), _m("vidC", 0), _m("vidD", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller(bad={0, 1})
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert len(result["failures"]) == 2
    assert _labeled(result) == {("vidC", 0)}
    # C0 kept in wave 1, so D0 is not labeled.
    assert _unlabeled(result) == {("vidD", 0): ENOUGH}


def test_failures_never_count_so_every_moment_is_attempted_when_nothing_is_kept(tmp_path):
    # All but the last fail: nothing is ever kept, so every moment is attempted.
    moments = [_m("vidA", 0), _m("vidB", 0), _m("vidC", 0)]
    run_dir, _rows = _make_run(tmp_path, moments, n_clips=1)
    caller = Caller(bad={0, 1})
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert len(result["failures"]) == 2
    assert _labeled(result) == {("vidC", 0)}
    assert UNLABELED_KEY not in result["scores"]


# ------------------------------------------------------ 10. determinism


def _mixed_layout():
    shared1, shared2 = _h("s1"), _h("s2")
    return [
        _m("vidA", 0), _m("vidA", 1, [shared1]), _m("vidA", 2),
        _m("vidB", 0, [shared1]), _m("vidB", 1), _m("vidB", 2, [shared2]),
        _m("vidC", 0), _m("vidC", 1, [_flip(shared2, 4)]), _m("vidC", 2),
        _m("vidD", 0, continuity_suspect=True), _m("vidD", 1), _m("vidD", 2),
    ]


@pytest.mark.parametrize("workers", [1, 4])
def test_result_does_not_depend_on_completion_order(tmp_path, monkeypatch, workers):
    _workers(monkeypatch, workers)
    outcomes = []
    for seed in range(4):
        run_dir, _rows = _make_run(tmp_path, _mixed_layout(), n_clips=4, name=f"run-{seed}")

        def delay(name, seed=seed):
            return random.Random(f"{seed}/{name}").uniform(0.0, 0.03)

        caller = Caller(replies={2: {"match": "reject"}, 9: {"geo": "conflicting"}}, delay=delay)
        result = label_review_strips(run_dir, WIRE, caller, budget=True)
        assert result["failures"] == []
        outcomes.append((result, sorted(caller.calls)))
    first = outcomes[0]
    for other in outcomes[1:]:
        assert other == first


def test_workers_change_the_wave_size_but_keep_the_shortlist_feed_valid(tmp_path, monkeypatch):
    _workers(monkeypatch, 1)
    run_1, _ = _make_run(tmp_path, _mixed_layout(), n_clips=4, name="w1")
    serial_caller = Caller()
    serial = label_review_strips(run_1, WIRE, serial_caller, budget=True)
    _workers(monkeypatch, 4)
    run_4, _ = _make_run(tmp_path, _mixed_layout(), n_clips=4, name="w4")
    wide_caller = Caller()
    wide = label_review_strips(run_4, WIRE, wide_caller, budget=True)
    # A wider wave may label more, never fewer, than the serial run; the serial run is a prefix of it.
    assert len(wide_caller.calls) >= len(serial_caller.calls)
    assert _labeled(serial) <= _labeled(wide)
    for result in (serial, wide):
        validate_label_payload(result["scores"])


# ------------------------------------------------------ 11. shortlist: unlabeled list


def _unlabeled_labels(**overrides) -> dict:
    labels = {
        "excerpts_sha256": "e" * 64,
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 1, "match": "keep", "geo": "supported", "scene_type": "coast"},
        ],
        UNLABELED_KEY: [
            {"video_id": "vid00000001", "excerpt_index": 2, "reason": ENOUGH},
            {"video_id": "vid00000002", "excerpt_index": 0,
             "reason": REASON_UNLABELED_DUPLICATE_PREFIX + "vid00000001:0"},
            {"video_id": "vid00000002", "excerpt_index": 1, "reason": ENOUGH},
            {"video_id": "vid00000002", "excerpt_index": 2, "reason": ENOUGH},
        ],
    }
    labels.update(overrides)
    return labels


def _shortlist_inputs():
    rows = _rows_fixture()
    return rows, _review_fixture(rows, _unique_hashes(rows))


def test_build_shortlist_uses_the_recorded_reason_for_unlabeled_moments():
    rows, review = _shortlist_inputs()
    labels = validate_label_payload(_unlabeled_labels())
    doc = build_shortlist(rows, review, labels, 2, shortlist_bindings())
    excluded = {(e["video_id"], e["excerpt_index"]): e["reasons"] for e in doc["excluded"]}
    assert excluded == {
        ("vid00000001", 2): [ENOUGH],
        ("vid00000002", 0): ["not labeled: duplicate of vid00000001:0"],
        ("vid00000002", 1): [ENOUGH],
        ("vid00000002", 2): [ENOUGH],
    }
    assert {(s["video_id"], s["excerpt_index"]) for s in doc["selected"]} == {("vid00000001", 0), ("vid00000001", 1)}
    counts = doc["counts"]
    assert counts["n_candidates"] == 6
    assert counts["n_selected"] == 2
    assert counts["n_excluded"] == 4
    assert counts["n_selected"] + counts["n_excluded"] == counts["n_candidates"]
    assert counts["n_excluded_by_reason"] == {
        ENOUGH: 3, "not labeled: duplicate of vid00000001:0": 1,
    }
    assert "no label" not in counts["n_excluded_by_reason"]
    assert counts["request_fulfilled"] is True


def test_moments_neither_labeled_nor_listed_still_say_no_label():
    rows, review = _shortlist_inputs()
    labels = _unlabeled_labels()
    labels[UNLABELED_KEY] = labels[UNLABELED_KEY][:1]  # only vid1:2 is a recorded skip
    doc = build_shortlist(rows, review, validate_label_payload(labels), 2, shortlist_bindings())
    excluded = {(e["video_id"], e["excerpt_index"]): e["reasons"] for e in doc["excluded"]}
    assert excluded[("vid00000001", 2)] == [ENOUGH]
    assert excluded[("vid00000002", 0)] == ["no label"]
    assert doc["counts"]["n_excluded_by_reason"] == {ENOUGH: 1, "no label": 3}


def test_unlabeled_key_is_not_treated_as_a_video_and_none_or_empty_is_fine():
    rows, review = _shortlist_inputs()
    base = {"excerpts_sha256": "e" * 64,
            "vid00000001": [{"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "a"}]}
    for value in (None, []):
        labels = validate_label_payload({**base, UNLABELED_KEY: value})
        doc = build_shortlist(rows, review, labels, 5, shortlist_bindings())
        assert doc["counts"]["n_selected"] == 1
        assert doc["counts"]["n_excluded_by_reason"] == {"no label": 5}
    assert unlabeled_reasons({}) == {}
    assert unlabeled_reasons({UNLABELED_KEY: [{"video_id": "v", "excerpt_index": 3, "reason": ENOUGH}]}) == {
        ("v", 3): ENOUGH}


@pytest.mark.parametrize(
    "item",
    [
        {"video_id": "vid00000001", "excerpt_index": 2, "reason": "because"},
        {"video_id": "vid00000001", "excerpt_index": 2, "reason": "not labeled: something else"},
        {"video_id": "vid00000001", "excerpt_index": 2, "reason": "Not labeled: enough clips kept"},
        {"video_id": "vid00000001", "excerpt_index": 2, "reason": ""},
        {"video_id": "vid00000001", "excerpt_index": 2, "reason": 5},
        {"video_id": "vid00000001", "excerpt_index": 2, "reason": None},
        {"video_id": "vid00000001", "excerpt_index": 2},
        {"video_id": "vid00000001", "excerpt_index": -1, "reason": ENOUGH},
        {"video_id": "vid00000001", "excerpt_index": True, "reason": ENOUGH},
        {"video_id": "vid00000001", "excerpt_index": "2", "reason": ENOUGH},
        {"video_id": "vid00000001", "excerpt_index": 1.0, "reason": ENOUGH},
        {"video_id": "vid00000001", "excerpt_index": None, "reason": ENOUGH},
        {"video_id": "vid00000001", "reason": ENOUGH},
        {"video_id": 7, "excerpt_index": 2, "reason": ENOUGH},
        {"video_id": None, "excerpt_index": 2, "reason": ENOUGH},
        {"excerpt_index": 2, "reason": ENOUGH},
        "vid00000001:2",
        ["vid00000001", 2, ENOUGH],
        None,
    ],
    ids=lambda item: json.dumps(item)[:50],
)
def test_validate_rejects_bad_unlabeled_entries(item):
    with pytest.raises(ShortlistInputError):
        validate_label_payload({UNLABELED_KEY: [item]})


@pytest.mark.parametrize("value", [{}, {"video_id": "v"}, "text", 5, True, ("a",)])
def test_validate_rejects_unlabeled_that_is_not_a_list(value):
    with pytest.raises(ShortlistInputError, match="must be a list"):
        validate_label_payload({UNLABELED_KEY: value})


def test_validate_accepts_both_recognised_reasons():
    payload = {UNLABELED_KEY: [
        {"video_id": "v", "excerpt_index": 0, "reason": ENOUGH},
        {"video_id": "v", "excerpt_index": 1, "reason": "not labeled: duplicate of w:4"},
    ]}
    assert validate_label_payload(payload) == payload


@pytest.mark.parametrize(
    "unlabeled,fragment",
    [
        ([{"video_id": "vid_unknown", "excerpt_index": 0, "reason": ENOUGH}], "not an analyzed moment"),
        ([{"video_id": "vid00000001", "excerpt_index": 3, "reason": ENOUGH}], "not an analyzed moment"),
        ([{"video_id": "vid00000001", "excerpt_index": 99, "reason": ENOUGH}], "not an analyzed moment"),
        ([{"video_id": "vid00000003", "excerpt_index": 0, "reason": ENOUGH}], "not an analyzed moment"),
        ([{"video_id": "vid00000001", "excerpt_index": 0, "reason": ENOUGH}], "both labeled and listed"),
        ([{"video_id": "vid00000001", "excerpt_index": 2, "reason": ENOUGH},
          {"video_id": "vid00000001", "excerpt_index": 2, "reason": ENOUGH}], "unlabeled twice"),
        ([{"video_id": "vid00000001", "excerpt_index": 2, "reason": ENOUGH},
          {"video_id": "vid00000001", "excerpt_index": 2,
           "reason": "not labeled: duplicate of vid00000002:0"}], "unlabeled twice"),
    ],
    ids=["unknown-video", "index-just-out-of-range", "index-far-out-of-range", "source-without-moments",
         "labeled-and-unlabeled", "listed-twice", "listed-twice-different-reason"],
)
def test_build_shortlist_rejects_inconsistent_unlabeled_lists(unlabeled, fragment):
    rows, review = _shortlist_inputs()
    labels = _unlabeled_labels(**{UNLABELED_KEY: unlabeled})
    validate_label_payload(labels)  # shape is fine; the inconsistency is against the run
    with pytest.raises(ShortlistInputError, match=fragment):
        build_shortlist(rows, review, labels, 2, shortlist_bindings())


# ------------------------------------------------------ 12. end to end


def test_budgeted_labels_validate_and_the_shortlist_selects_n_clips(tmp_path, monkeypatch):
    _workers(monkeypatch, 2)
    shared = _h("shared")
    moments = [
        _m("vidA", 0, [shared]), _m("vidA", 1), _m("vidA", 2),
        _m("vidB", 0, [_flip(shared, 3)]), _m("vidB", 1), _m("vidB", 2),
        _m("vidC", 0), _m("vidC", 1), _m("vidC", 2),
        _m("vidD", 0), _m("vidD", 1), _m("vidD", 2),
    ]
    run_dir, rows = _make_run(tmp_path, moments, n_clips=3)
    caller = Caller(replies={6: {"match": "reject"}, 4: {"geo": "conflicting"}})
    result = label_review_strips(run_dir, WIRE, caller, budget=True)
    assert result["failures"] == []
    scores = json.loads(json.dumps(result["scores"]))  # what shortlist_scores.json would hold
    validate_label_payload(scores)
    assert len(caller.calls) < len(moments)
    assert scores[UNLABELED_KEY]

    review = json.loads((run_dir / "review.json").read_text())
    doc = build_shortlist(rows, review, scores, 3, shortlist_bindings())
    assert doc["counts"]["n_selected"] == 3
    assert doc["counts"]["request_fulfilled"] is True
    assert doc["counts"]["n_selected"] + doc["counts"]["n_excluded"] == len(moments)
    selected = {(s["video_id"], s["excerpt_index"]) for s in doc["selected"]}
    assert selected.isdisjoint(_unlabeled(result))
    excluded = {(e["video_id"], e["excerpt_index"]): e["reasons"] for e in doc["excluded"]}
    for key, reason in _unlabeled(result).items():
        assert excluded[key] == [reason]
    assert "no label" not in doc["counts"]["n_excluded_by_reason"]


def test_budget_on_and_off_select_the_same_number_of_clips(tmp_path):
    moments = [_m(f"vid{c}", i) for c in "ABCD" for i in range(3)]
    run_a, rows = _make_run(tmp_path, moments, n_clips=4, name="on")
    run_b, _ = _make_run(tmp_path, moments, n_clips=4, name="off")
    on = label_review_strips(run_a, WIRE, Caller(), budget=True)
    off = label_review_strips(run_b, WIRE, Caller(), budget=False)
    review = json.loads((run_a / "review.json").read_text())
    doc_on = build_shortlist(rows, review, on["scores"], 4, shortlist_bindings())
    doc_off = build_shortlist(rows, review, off["scores"], 4, shortlist_bindings())
    assert doc_on["counts"]["n_selected"] == doc_off["counts"]["n_selected"] == 4
    assert len(_labeled(on)) < len(_labeled(off))


def test_runner_label_writes_unlabeled_only_when_budget_is_passed(tmp_path):
    moments = [_m("vidA", 0), _m("vidB", 0), _m("vidC", 0)]
    on_dir, _ = _make_run(tmp_path, moments, n_clips=1, name="on")
    off_dir, _ = _make_run(tmp_path, moments, n_clips=1, name="off")
    assert runner._label(on_dir, WIRE, Caller(), runner._Counters(), kind="strip", judgments=None,
                         budget=True) == {}
    assert runner._label(off_dir, WIRE, Caller(), runner._Counters(), kind="strip", judgments=None) == {}
    on = json.loads((on_dir / "shortlist_scores.json").read_text())
    off = json.loads((off_dir / "shortlist_scores.json").read_text())
    assert [u["reason"] for u in on[UNLABELED_KEY]] == [ENOUGH, ENOUGH]
    assert UNLABELED_KEY not in off


# ------------------------------------------------------ 13. runner switch, bindings, config


@pytest.mark.parametrize(
    "config,expected",
    [
        ({}, True),
        ({"strip_label_budget": True}, True),
        ({"strip_label_budget": False}, False),
        ({"jev_note_check": True}, False),
        ({"jev_note_check": True, "strip_label_budget": True}, False),
        ({"jev_note_check": True, "strip_label_budget": False}, False),
        ({"jev_note_check": False}, True),
        ({"jev_rank": True}, True),
    ],
)
def test_strip_label_budget_on(config, expected):
    assert strip_label_budget_on(config) is expected


def test_bindings_differ_only_for_the_strip_stages_and_only_when_budget_is_on():
    on = _bindings(None, None, {}, "m")
    explicit_on = _bindings(None, None, {"strip_label_budget": True}, "m")
    off = _bindings(None, None, {"strip_label_budget": False}, "m")
    jev = _bindings(None, None, {"jev_note_check": True}, "m")
    jev_budget_true = _bindings(None, None, {"jev_note_check": True, "strip_label_budget": True}, "m")
    assert on == explicit_on
    strip_stages = {"label_strips", "shortlist_apply"}
    assert set(on) == set(off) == set(jev)
    for stage in set(on) - strip_stages:
        assert on[stage] == off[stage] == jev[stage], stage
    for stage in strip_stages:
        assert on[stage] != off[stage], stage
        assert on[stage] != jev[stage], stage
        assert off[stage] != jev[stage], stage  # the jev note check has its own binding
        # jev_note_check forces the budget off, so the config flag cannot change its binding
        assert jev[stage] == jev_budget_true[stage], stage


def test_label_strips_stage_passes_the_budget_switch(tmp_path, monkeypatch):
    seen = []

    def fake_label(run_dir, wire, caller, counters, *, kind, judgments, budget=False):
        seen.append((kind, budget))
        return {}

    monkeypatch.setattr(runner, "_label", fake_label)

    class Ports:
        strip_caller = None

    for config in ({}, {"strip_label_budget": False}, {"jev_note_check": True}):
        kw = {"judgments": None, "config": config, "brief_doc": None}
        if config.get("jev_note_check"):
            kw["judgments"] = {"strip_scores": {"x": []}}  # captured: no note check call, just the wiring
        try:
            runner._run_stage("label_strips", tmp_path, tmp_path, kw, Ports, None, None)
        except Exception:  # only the budget wiring matters here
            pass
    assert seen == [("strip", True), ("strip", False), ("strip", False)]


def test_config_accepts_boolean_strip_label_budget(tmp_path):
    for text, expected in (("true", True), ("false", False)):
        (tmp_path / "config.yaml").write_text(f"strip_label_budget: {text}\n", encoding="utf-8")
        assert load_project_config(tmp_path)["strip_label_budget"] is expected


@pytest.mark.parametrize("text", ["1", "0", "yes", '"true"', "null", "2.5", "[]", "maybe"])
def test_config_rejects_non_boolean_strip_label_budget(tmp_path, text):
    (tmp_path / "config.yaml").write_text(f"strip_label_budget: {text}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="strip_label_budget must be a boolean"):
        load_project_config(tmp_path)


# ------------------------------------------------------ 14. CLI


def _cli_run(tmp_path, monkeypatch, extra_args):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls = []

    def fake_label(run_dir_arg, wire, caller=None, *, budget=False):
        calls.append({"run_dir": Path(run_dir_arg), "budget": budget, "caller": caller})
        return {"scores": {"excerpts_sha256": "e" * 64}, "failures": []}

    monkeypatch.setattr("scenery_brief_clips.cli.load_vision_wire", lambda root: WIRE)
    monkeypatch.setattr("scenery_brief_clips.cli.label_review_strips", fake_label)
    code = main(["label-strips", "--run-dir", str(run_dir), "--confirm-vision", "--root", str(tmp_path),
                 *extra_args])
    return code, calls, run_dir


def test_cli_label_strips_defaults_to_budget(tmp_path, monkeypatch, capsys):
    code, calls, run_dir = _cli_run(tmp_path, monkeypatch, [])
    assert code == 0
    assert len(calls) == 1 and calls[0]["budget"] is True
    assert calls[0]["run_dir"] == run_dir
    assert json.loads((run_dir / "shortlist_scores.json").read_text()) == {"excerpts_sha256": "e" * 64}
    assert json.loads(capsys.readouterr().out)["scores"] == str(run_dir / "shortlist_scores.json")


def test_cli_label_all_turns_the_budget_off(tmp_path, monkeypatch):
    code, calls, _run_dir = _cli_run(tmp_path, monkeypatch, ["--label-all"])
    assert code == 0
    assert len(calls) == 1 and calls[0]["budget"] is False


def test_cli_label_strips_still_needs_confirm_vision(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls = []
    monkeypatch.setattr("scenery_brief_clips.cli.load_vision_wire", lambda root: WIRE)
    monkeypatch.setattr("scenery_brief_clips.cli.label_review_strips",
                        lambda *a, **k: calls.append(k) or {"scores": {}, "failures": []})
    code = main(["label-strips", "--run-dir", str(run_dir), "--root", str(tmp_path), "--label-all"])
    assert code == 2
    assert calls == []
