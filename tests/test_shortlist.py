import hashlib
import json

import pytest
from PIL import Image, ImageDraw

from scenery_brief_clips.shortlist import (
    ShortlistInputError,
    ShortlistStaleError,
    build_shortlist,
    frame_dhash,
    moments_are_duplicates,
    signature_for_frames,
    validate_label_payload,
)


def _write_bars(path, tone=120):
    img = Image.new("L", (64, 64), 0)
    draw = ImageDraw.Draw(img)
    for i in range(0, 64, 8):
        draw.rectangle([i, 0, i + 3, 63], fill=tone + (i * 3 % 100))
    img.save(path, "JPEG", quality=90)


def _write_checker(path):
    img = Image.new("L", (64, 64), 30)
    draw = ImageDraw.Draw(img)
    for y in range(0, 64, 8):
        for x in range(0, 64, 8):
            if (x // 8 + y // 8) % 2 == 0:
                draw.rectangle([x, y, x + 7, y + 7], fill=220)
    img.save(path, "JPEG", quality=90)


def test_frame_signature_matches_identical_and_separates_different(tmp_path):
    bars = tmp_path / "bars.jpg"
    bars_again = tmp_path / "bars-again.jpg"
    checker = tmp_path / "checker.jpg"
    _write_bars(bars)
    _write_bars(bars_again)
    _write_checker(checker)

    assert frame_dhash(bars) == frame_dhash(bars_again)
    assert moments_are_duplicates([frame_dhash(bars)], [frame_dhash(bars_again)])
    assert not moments_are_duplicates([frame_dhash(bars)], [frame_dhash(checker)])


def test_signature_survives_reencode_but_flags_near_duplicates(tmp_path):
    original = tmp_path / "original.jpg"
    reencoded = tmp_path / "reencoded.jpg"
    _write_bars(original)
    with Image.open(original) as img:
        img.convert("RGB").save(reencoded, "JPEG", quality=55)

    assert moments_are_duplicates([frame_dhash(original)], [frame_dhash(reencoded)])


def test_validate_label_payload_accepts_valid_shape():
    payload = {
        "abcdefghijk": [
            {
                "excerpt_index": 0,
                "match": "keep",
                "geo": "supported",
                "scene_type": "mountains",
                "note": "alps",
            }
        ]
    }
    assert validate_label_payload(payload) == payload


@pytest.mark.parametrize(
    "entry",
    [
        {"excerpt_index": 0, "match": "keeper", "geo": "supported", "scene_type": "x"},
        {"excerpt_index": 0, "match": "keep", "geo": "maybe", "scene_type": "x"},
        {"excerpt_index": "0", "match": "keep", "geo": "supported", "scene_type": "x"},
        {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": ""},
        {"excerpt_index": 0, "match": "keep", "geo": "supported"},
        "not-an-object",
    ],
)
def test_validate_label_payload_rejects_bad_entries(entry):
    with pytest.raises(ShortlistInputError):
        validate_label_payload({"abcdefghijk": [entry]})


def test_validate_label_payload_rejects_bad_shapes():
    with pytest.raises(ShortlistInputError):
        validate_label_payload([])
    with pytest.raises(ShortlistInputError):
        validate_label_payload({"abcdefghijk": {"excerpt_index": 0}})


def _excerpt(start, end, key):
    return {
        "start_s": start,
        "end_s": end,
        "source_scene": [start, end],
        "analysis_span": [start, end],
        "analysis_cache_key": key,
    }


def _rows_fixture():
    return [
        {
            "video_id": "vid00000001",
            "title": "One",
            "priority": "promising",
            "status": "complete",
            "excerpts": [
                _excerpt(10.0, 16.0, "vid00000001_10000-16000_v3.mp4"),
                _excerpt(20.0, 26.0, "vid00000001_20000-26000_v3.mp4"),
                _excerpt(30.0, 36.0, "vid00000001_30000-36000_v3.mp4"),
            ],
        },
        {
            "video_id": "vid00000002",
            "title": "Two",
            "priority": "uncertain",
            "status": "complete",
            "excerpts": [
                _excerpt(5.0, 11.0, "vid00000002_5000-11000_v3.mp4"),
                _excerpt(15.0, 21.0, "vid00000002_15000-21000_v3.mp4"),
                _excerpt(25.0, 31.0, "vid00000002_25000-31000_v3.mp4"),
            ],
        },
        {
            "video_id": "vid00000003",
            "title": "Skipped source",
            "priority": "low",
            "status": "skipped",
            "excerpts": [],
        },
    ]


def _review_fixture(rows, hashes_by_key):
    moments = []
    for row in rows:
        for index, excerpt in enumerate(row["excerpts"]):
            key = (row["video_id"], index)
            moments.append(
                {
                    "video_id": row["video_id"],
                    "excerpt_index": index,
                    "start_s": excerpt["start_s"],
                    "end_s": excerpt["end_s"],
                    "analysis_cache_key": excerpt["analysis_cache_key"],
                    "frames": [],
                    "frame_hashes": hashes_by_key.get(key, [f"{index + 1:016x}"]),
                }
            )
    return {
        "schema_version": 1,
        "excerpts_sha256": "e" * 64,
        "generation_id": "g" * 64,
        "frames_per_moment": 1,
        "moments": moments,
        "counts": {"moments": len(moments)},
    }


def _bindings():
    return {
        "excerpts_sha256": "e" * 64,
        "generation_id": "g" * 64,
        "review_sha256": "r" * 64,
        "labels_sha256": "l" * 64,
        "labels_path": "/tmp/labels.json",
    }


def _sig(*parts: str) -> list[str]:
    return [hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]]


def _unique_hashes(rows) -> dict:
    out = {}
    for row in rows:
        for index in range(len(row.get("excerpts") or [])):
            out[(row["video_id"], index)] = _sig(row["video_id"], str(index))
    return out


@pytest.mark.parametrize(
    "bad_hashes",
    [
        "0123456789abcdef",
        ["zzzzzzzzzzzzzzzz"],
        ["0123"],
        [1.5],
        [True],
        [None],
    ],
)
def test_build_shortlist_rejects_malformed_frame_hashes(bad_hashes):
    rows = _rows_fixture()
    hashes = _unique_hashes(rows)
    hashes[("vid00000001", 0)] = bad_hashes
    review = _review_fixture(rows, hashes)
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "a"}
        ]
    }

    with pytest.raises(ShortlistStaleError):
        build_shortlist(rows, review, labels, 20, _bindings())


def test_build_shortlist_rejects_non_integer_n_clips():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "a"}
        ]
    }

    for value in (2.9, 2.0, True, "20"):
        with pytest.raises(ShortlistInputError):
            build_shortlist(rows, review, labels, value, _bindings())

    doc = build_shortlist(rows, review, labels, 20, _bindings())
    assert doc["n_clips_requested"] == 20


def test_build_shortlist_requires_non_empty_frame_hashes():
    rows = _rows_fixture()
    hashes = _unique_hashes(rows)
    hashes[("vid00000001", 0)] = []
    review = _review_fixture(rows, hashes)
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "a"}
        ]
    }

    with pytest.raises(ShortlistStaleError):
        build_shortlist(rows, review, labels, 20, _bindings())


def test_build_shortlist_accepts_labels_with_generation_binding():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    labels = {
        "excerpts_sha256": "e" * 64,
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "a"}
        ],
    }

    doc = build_shortlist(rows, review, labels, 20, _bindings())

    assert doc["counts"]["n_selected"] == 1
    assert doc["selected"][0]["video_id"] == "vid00000001"


def test_build_shortlist_applies_decision_rules():
    rows = _rows_fixture()
    hashes = {
        ("vid00000001", 0): _sig("v1", "0"),
        ("vid00000001", 1): _sig("v1", "1"),
        ("vid00000001", 2): _sig("v1", "2"),
        ("vid00000002", 0): _sig("v2", "0"),
        ("vid00000002", 1): _sig("v2", "1"),
        ("vid00000002", 2): _sig("v2", "2"),
    }
    review = _review_fixture(rows, hashes)
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 1, "match": "keep", "geo": "uncertain", "scene_type": "coast"},
            {"excerpt_index": 2, "match": "keep", "geo": "conflicting", "scene_type": "mountains"},
        ],
        "vid00000002": [
            {"excerpt_index": 0, "match": "reject", "geo": "supported", "scene_type": "water"},
            {"excerpt_index": 1, "match": "uncertain", "geo": "uncertain", "scene_type": "forest"},
        ],
    }
    doc = build_shortlist(rows, review, labels, n_clips=20, bindings=_bindings())

    selected = {(item["video_id"], item["excerpt_index"]): item for item in doc["selected"]}
    assert set(selected) == {("vid00000001", 0), ("vid00000001", 1)}
    assert selected[("vid00000001", 1)]["flags"] == ["geo_uncertain"]
    assert selected[("vid00000001", 0)]["flags"] == []

    excluded = {(item["video_id"], item["excerpt_index"]): item for item in doc["excluded"]}
    assert excluded[("vid00000001", 2)]["reasons"] == ["geographic evidence conflicting"]
    assert excluded[("vid00000002", 0)]["reasons"] == ["visual match rejected"]
    assert excluded[("vid00000002", 1)]["reasons"] == ["visual match uncertain"]
    assert excluded[("vid00000002", 2)]["reasons"] == ["no label"]

    counts = doc["counts"]
    assert counts["n_candidates"] == 6
    assert counts["n_selected"] == 2
    assert counts["n_sources_without_moments"] == 1
    assert counts["n_excluded_by_reason"]["no label"] == 1


def test_build_shortlist_shortfall_explains_and_never_pads():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
        ],
    }
    doc = build_shortlist(rows, review, labels, n_clips=20, bindings=_bindings())

    assert len(doc["selected"]) == 1
    assert doc["shortfall"]["count"] == 19
    explanation = doc["shortfall"]["explanation"]
    assert "20" in explanation and "1" in explanation and "19" in explanation
    assert "no label" in explanation
    assert doc["counts"]["n_selected"] + doc["counts"]["n_excluded"] == doc["counts"]["n_candidates"]


def test_dedup_collapses_duplicate_and_keeps_distinct():
    rows = _rows_fixture()
    shared = _sig("shared-lake")
    hashes = _unique_hashes(rows)
    hashes[("vid00000001", 0)] = shared
    hashes[("vid00000002", 0)] = shared
    review = _review_fixture(rows, hashes)
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 1, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 2, "match": "keep", "geo": "supported", "scene_type": "mountains"},
        ],
        "vid00000002": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 1, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 2, "match": "keep", "geo": "supported", "scene_type": "mountains"},
        ],
    }
    doc = build_shortlist(rows, review, labels, n_clips=20, bindings=_bindings())

    selected = {(item["video_id"], item["excerpt_index"]) for item in doc["selected"]}
    assert selected == {
        ("vid00000001", 0),
        ("vid00000001", 1),
        ("vid00000001", 2),
        ("vid00000002", 1),
        ("vid00000002", 2),
    }
    excluded = {(item["video_id"], item["excerpt_index"]): item for item in doc["excluded"]}
    assert excluded[("vid00000002", 0)]["reasons"] == ["duplicate of vid00000001:0"]


def test_diversity_alternates_scene_types_and_prefers_least_used_source():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 1, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 2, "match": "keep", "geo": "supported", "scene_type": "mountains"},
        ],
        "vid00000002": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 1, "match": "keep", "geo": "supported", "scene_type": "coast"},
            {"excerpt_index": 2, "match": "keep", "geo": "supported", "scene_type": "coast"},
        ],
    }
    doc = build_shortlist(rows, review, labels, n_clips=3, bindings=_bindings())

    picked = [(item["video_id"], item["excerpt_index"]) for item in doc["selected"]]
    assert picked == [
        ("vid00000001", 0),
        ("vid00000002", 1),
        ("vid00000001", 1),
    ]
    excluded = {(item["video_id"], item["excerpt_index"]): item for item in doc["excluded"]}
    assert excluded[("vid00000002", 2)]["reasons"] == ["beyond n_clips"]
    assert excluded[("vid00000001", 2)]["reasons"] == ["beyond n_clips"]

    # Least-used source beats fingerprint order inside a scene_type bucket:
    mountain_labels = {
        video_id: [
            {"excerpt_index": index, "match": "keep", "geo": "supported", "scene_type": "mountains"}
            for index in range(len([row for row in rows if row["video_id"] == video_id][0]["excerpts"]))
        ]
        for video_id in ("vid00000001", "vid00000002")
    }
    doc = build_shortlist(rows, review, mountain_labels, n_clips=2, bindings=_bindings())
    picked = [(item["video_id"], item["excerpt_index"]) for item in doc["selected"]]
    assert picked == [("vid00000001", 0), ("vid00000002", 0)]


def test_build_shortlist_is_deterministic():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 1, "match": "keep", "geo": "uncertain", "scene_type": "coast"},
        ],
    }
    first = build_shortlist(rows, review, labels, n_clips=5, bindings=_bindings())
    second = build_shortlist(rows, review, labels, n_clips=5, bindings=_bindings())
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_build_shortlist_requires_exact_review_coverage():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    review["moments"] = review["moments"][:-1]
    with pytest.raises(ShortlistStaleError):
        build_shortlist(rows, review, {}, n_clips=5, bindings=_bindings())

    review = _review_fixture(rows, _unique_hashes(rows))
    review["moments"][0]["start_s"] = 999.0
    with pytest.raises(ShortlistStaleError):
        build_shortlist(rows, review, {}, n_clips=5, bindings=_bindings())


def test_build_shortlist_rejects_unknown_label_video():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    labels = {
        "vid99999999": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
        ]
    }
    with pytest.raises(ShortlistInputError):
        build_shortlist(rows, review, labels, n_clips=5, bindings=_bindings())


def test_build_shortlist_rejects_out_of_range_excerpt_index():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    labels = {
        "vid00000001": [
            {"excerpt_index": 9, "match": "keep", "geo": "supported", "scene_type": "mountains"},
        ]
    }
    with pytest.raises(ShortlistInputError):
        build_shortlist(rows, review, labels, n_clips=5, bindings=_bindings())



def test_continuity_suspect_keep_without_clearance_is_excluded():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    for moment in review["moments"]:
        if moment["video_id"] == "vid00000001" and moment["excerpt_index"] == 0:
            moment["continuity_suspect"] = True
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 1, "match": "keep", "geo": "supported", "scene_type": "coast"},
        ],
    }
    doc = build_shortlist(rows, review, labels, n_clips=5, bindings=_bindings())
    selected = {(item["video_id"], item["excerpt_index"]) for item in doc["selected"]}
    assert ("vid00000001", 0) not in selected
    assert ("vid00000001", 1) in selected
    excluded = {(item["video_id"], item["excerpt_index"]): item for item in doc["excluded"]}
    assert excluded[("vid00000001", 0)]["reasons"] == ["continuity_suspect_uncleared"]


def test_continuity_suspect_cleared_by_note_prefix_or_flag_can_keep():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    for moment in review["moments"]:
        if (moment["video_id"], moment["excerpt_index"]) in {("vid00000001", 0), ("vid00000001", 1)}:
            moment["continuity_suspect"] = True
    labels = {
        "vid00000001": [
            {
                "excerpt_index": 0,
                "match": "keep",
                "geo": "supported",
                "scene_type": "mountains",
                "note": "continuity_ok: slow aerial pan, endpoints look continuous",
            },
            {
                "excerpt_index": 1,
                "match": "keep",
                "geo": "supported",
                "scene_type": "coast",
                "continuity_ok": True,
                "note": "cleared via flag",
            },
        ],
    }
    doc = build_shortlist(rows, review, labels, n_clips=5, bindings=_bindings())
    selected = {(item["video_id"], item["excerpt_index"]) for item in doc["selected"]}
    assert ("vid00000001", 0) in selected
    assert ("vid00000001", 1) in selected


def test_continuity_suspect_does_not_override_reject_or_uncertain():
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    for moment in review["moments"]:
        if moment["video_id"] == "vid00000001":
            moment["continuity_suspect"] = True
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "reject", "geo": "supported", "scene_type": "mountains"},
            {"excerpt_index": 1, "match": "uncertain", "geo": "supported", "scene_type": "coast"},
        ],
    }
    doc = build_shortlist(rows, review, labels, n_clips=5, bindings=_bindings())
    excluded = {(item["video_id"], item["excerpt_index"]): item for item in doc["excluded"]}
    assert excluded[("vid00000001", 0)]["reasons"] == ["visual match rejected"]
    assert excluded[("vid00000001", 1)]["reasons"] == ["visual match uncertain"]


def test_shortfall_reports_request_fulfilled_and_upstream_continuity_rejects():
    rows = _rows_fixture()
    rows[0]["continuity_rejected"] = [
        {"start_s": 1.0, "end_s": 7.0, "reason": "no stable continuity subspan"},
        {"start_s": 8.0, "end_s": 14.0, "reason": "no stable continuity subspan"},
    ]
    review = _review_fixture(rows, _unique_hashes(rows))
    labels = {
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
        ],
    }
    doc = build_shortlist(rows, review, labels, n_clips=20, bindings=_bindings())
    assert doc["counts"]["request_fulfilled"] is False
    assert doc["counts"]["n_continuity_rejected_upstream"] == 2
    assert doc["counts"]["n_continuity_rejected_upstream_by_reason"] == {
        "no stable continuity subspan": 2
    }
    explanation = doc["shortfall"]["explanation"]
    assert "request_fulfilled false" in explanation
    assert "upstream continuity rejects" in explanation
    assert "no stable continuity subspan" in explanation
