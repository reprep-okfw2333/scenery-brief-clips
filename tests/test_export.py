"""Part 5 export tests: plan-first, authorized acquisition with the copyts
absolute-timestamp mapping, coverage without clamping, one-encode half-open
trim at the 720p cap (largest rendition <= cap, never scaled), manifest +
pointer publication, honest failures, binding-based reuse, and the guards
(authorization, staleness, ownership, duplicates, symlinks, disk reserve).

Fast by design: synthetic 720p sources are generated once per module (they
stand in for acquisitions; the run's recorded source dims stay decoupled and
can be 1080p/4K); the production encode preset is monkeypatched to ultrafast
for integration tests (the default recipe arguments are asserted separately
with a fake runner).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scenery_brief_clips.analysis_cache import sha256_file
from scenery_brief_clips.export import (
    ENCODE_CRF,
    ENCODE_PRESET,
    EXPORT_SCHEMA_VERSION,
    MARGIN_S,
    ExportError,
    ExportStaleError,
    build_export_plan,
    clip_name,
    coverage_problem,
    expected_clip_frames,
    export_run,
    slug_theme,
)
from scenery_brief_clips.shortlist import SHORTLIST_SCHEMA_VERSION, frame_dhash
from scenery_brief_clips.yt import ExportMediaError

RUN_ID = "20260101T000000Z"
VIDEO = "aaaaaaaaaaa"


def _ffmpeg(*args: str) -> None:
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())


@pytest.fixture(scope="module")
def synth_source(tmp_path_factory) -> Path:
    """A 24s 1280x720 30fps acquisition stand-in with 1s keyframes (testsrc2)."""
    directory = tmp_path_factory.mktemp("export-src")
    src = directory / "source.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=24",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-crf", "30",
        "-pix_fmt", "yuv420p", str(src),
    )
    return src


@pytest.fixture(scope="module")
def alt_source(tmp_path_factory) -> Path:
    """A different 24s 1280x720 source (content differs)."""
    directory = tmp_path_factory.mktemp("export-alt")
    src = directory / "alt.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=24",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-crf", "30",
        "-pix_fmt", "yuv420p", str(src),
    )
    return src


@pytest.fixture(scope="module")
def synth_1080(tmp_path_factory) -> Path:
    """A 24s 1920x1080 acquisition stand-in (for cap-override tests)."""
    directory = tmp_path_factory.mktemp("export-1080")
    src = directory / "source1080.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=24",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-crf", "30",
        "-pix_fmt", "yuv420p", str(src),
    )
    return src


def _section(src: Path, start: float, end: float, dest: Path) -> Path:
    """A stream-copied section with absolute timestamps (copyts), like yt-dlp's."""
    _ffmpeg("-copyts", "-ss", str(start), "-i", str(src), "-to", str(end), "-c", "copy", str(dest))
    return dest


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload) + "\n")


def _formats_multi() -> list[dict]:
    """A 4K source's DASH listings: 4K + 1080p + 720p video-only renditions."""
    return [
        {"format_id": "401", "vcodec": "av01.0.12M.08", "acodec": "none", "width": 3840, "height": 2160, "fps": 30, "ext": "mp4"},
        {"format_id": "137", "vcodec": "avc1.640028", "acodec": "none", "width": 1920, "height": 1080, "fps": 30, "ext": "mp4"},
        {"format_id": "136", "vcodec": "avc1.4d401f", "acodec": "none", "width": 1280, "height": 720, "fps": 30, "ext": "mp4"},
    ]


def _formats_4k_only() -> list[dict]:
    return [
        {"format_id": "401", "vcodec": "av01.0.12M.08", "acodec": "none", "width": 3840, "height": 2160, "fps": 30, "ext": "mp4"},
        {"format_id": "313", "vcodec": "vp9", "acodec": "none", "width": 3840, "height": 2160, "fps": 30, "ext": "webm"},
    ]


def _formats_1080_only() -> list[dict]:
    return [
        {"format_id": "137", "vcodec": "avc1.640028", "acodec": "none", "width": 1920, "height": 1080, "fps": 30, "ext": "mp4"},
    ]


def _formats_480_only() -> list[dict]:
    return [
        {"format_id": "135", "vcodec": "avc1.4d401e", "acodec": "none", "width": 854, "height": 480, "fps": 30, "ext": "mp4"},
    ]



def _make_run(
    tmp_path: Path,
    *,
    selected: list[dict] | None = None,
    excluded: list[dict] | None = None,
    formats=None,
    recorded: tuple[int, int] = (1920, 1080),
    duration_s: float = 24.0,
    shortlist_generation: str = "gen1",
    manifest_generation: str = "gen1",
    n_clips: int = 20,
    theme_text: str = "Beautiful natural european scenery",
) -> tuple[Path, Path]:
    root = tmp_path
    run_dir = root / "data" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    meta_dir = root / "data" / "cache" / "metadata"
    meta_dir.mkdir(parents=True)

    _write(
        run_dir / "constraint.json",
        {
            "theme_text": theme_text,
            "min_width": 1920,
            "min_height": 1080,
            "aspect_min": 1.7,
            "aspect_max": 1.86,
            "n_clips": n_clips,
            "duration_min_s": 4.0,
            "duration_max_s": 12.0,
        },
    )
    _write(
        run_dir / "ranked.json",
        [
            {
                "video_id": VIDEO,
                "priority": "promising",
                "width": recorded[0],
                "height": recorded[1],
                "duration_s": duration_s,
            }
        ],
    )
    _write(
        meta_dir / f"{VIDEO}.json",
        {"id": VIDEO, "duration": duration_s, "formats": formats or _formats_multi()},
    )
    _write(run_dir / "review.json", {"schema_version": 1, "moments": []})
    _write(run_dir / "shortlist_scores.json", {VIDEO: []})
    if selected is None:
        selected = [
            {
                "video_id": VIDEO,
                "excerpt_index": 0,
                "start_s": 7.0,
                "end_s": 13.0,
                "scene_type": "fields",
                "geo": "supported",
                "flags": [],
                "reason": "selected",
            }
        ]
    if excluded is None:
        excluded = []
    _write(
        run_dir / "shortlist.json",
        {
            "schema_version": SHORTLIST_SCHEMA_VERSION,
            "bindings": {
                "excerpts_sha256": "e" * 64,
                "generation_id": shortlist_generation,
                "review_sha256": sha256_file(run_dir / "review.json"),
                "labels_sha256": sha256_file(run_dir / "shortlist_scores.json"),
                "labels_path": "shortlist_scores.json",
            },
            "selected": selected,
            "excluded": excluded,
            "counts": {"n_selected": len(selected), "n_excluded": len(excluded)},
        },
    )
    _write(
        run_dir / "analysis_manifest.json",
        {
            "schema_version": 3,
            "generation_id": manifest_generation,
            "excerpts_sha256": "e" * 64,
            "ranked_sha256": sha256_file(run_dir / "ranked.json"),
            "constraint_sha256": sha256_file(run_dir / "constraint.json"),
            "settings": {"cache_policy": "v3-video-only-720"},
        },
    )
    return root, run_dir


class FakeYt:
    """Stands in for the authorized acquisition client: copies a prepared
    section (with its real copyts timestamps) and returns its path."""

    def __init__(self, sections: dict, out_dir: Path) -> None:
        self.sections = sections
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.calls = []
        self.paths: list[Path] = []

    def fetch_export(self, spec):
        self.calls.append(spec)
        key = (spec.video_id, float(spec.span[0]), float(spec.span[1]))
        section = self.sections.get(key)
        if section is None:
            raise RuntimeError(f"no prepared section for {key}")
        dest = self.out_dir / f"acq_{spec.video_id}_{int(spec.span[0] * 1000)}-{int(spec.span[1] * 1000)}.mp4"
        shutil.copy(section, dest)
        self.paths.append(dest)
        return dest


class FailingYt:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def fetch_export(self, spec):
        raise self.exc


def _fast_encode(monkeypatch) -> None:
    monkeypatch.setattr("scenery_brief_clips.export.ENCODE_PRESET", "ultrafast")
    monkeypatch.setattr("scenery_brief_clips.export.ENCODE_CRF", 30)


def _pointer_of(run_dir: Path) -> dict:
    return json.loads((run_dir / "export.json").read_text())


def _published(root: Path, theme: str) -> dict:
    return json.loads((root / "out" / theme / "manifest.json").read_text())


def test_slug_theme_is_deterministic():
    assert slug_theme("Beautiful natural european scenery") == "beautiful-natural-european-scenery"
    assert slug_theme("  Alps!! 4K — lakes ") == "alps-4k-lakes"
    assert len(slug_theme("x" * 100)) <= 60


def test_expected_clip_frames_is_half_open():
    assert expected_clip_frames(1.0, 3.0, 30, 1) == 60
    assert expected_clip_frames(1.01, 2.99, 30, 1) == 59
    assert expected_clip_frames(0.0, 6.0, 30, 1) == 180
    assert expected_clip_frames(0.0, 6.0, 30000, 1001) == 180
    assert expected_clip_frames(-0.0004, 6.0, 30, 1) == 180


def test_coverage_problem_is_millisecond_precise_and_never_clamps():
    assert coverage_problem(7000, 13000, 5000, 15000) is None
    assert coverage_problem(7000, 13000, 7000, 13000) is None
    assert coverage_problem(7000, 13000, 7001, 15000) is None  # within 1ms precision
    assert coverage_problem(7000, 13000, 7002, 15000) is not None  # late start beyond precision
    assert coverage_problem(7000, 13000, 7000, 12999) is None
    assert coverage_problem(7000, 13000, 7000, 12998) is not None  # short end


def test_clip_name_format():
    assert clip_name(VIDEO, 0, 7000, 13000) == "aaaaaaaaaaa_e0_7000-13000.mp4"


def test_export_requires_authorization(tmp_path):
    root, run_dir = _make_run(tmp_path)

    with pytest.raises(ExportError) as excinfo:
        export_run(run_dir, root, allow_export=False)

    assert "allow" in str(excinfo.value).lower()
    assert not (root / "out").exists()


def test_export_refuses_missing_shortlist(tmp_path):
    root, run_dir = _make_run(tmp_path)
    (run_dir / "shortlist.json").unlink()

    with pytest.raises(ExportError) as excinfo:
        export_run(run_dir, root, allow_export=True)

    assert "shortlist" in str(excinfo.value)


def test_export_refuses_when_project_disk_is_below_floor(tmp_path, monkeypatch):
    root, run_dir = _make_run(tmp_path)
    usage = shutil.disk_usage(root)
    monkeypatch.setattr(
        "scenery_brief_clips.export.shutil.disk_usage",
        lambda _path: usage._replace(free=1),
    )

    with pytest.raises(ExportError, match="insufficient free disk"):
        export_run(run_dir, root, allow_export=True)

    assert not (root / "out").exists()


def test_export_refuses_stale_shortlist(tmp_path):
    root, run_dir = _make_run(tmp_path, shortlist_generation="gen2", manifest_generation="gen1")

    with pytest.raises(ExportStaleError) as excinfo:
        export_run(run_dir, root, allow_export=True)

    assert "stale" in str(excinfo.value).lower() or "generation" in str(excinfo.value).lower()
    assert not (root / "out").exists()


def test_export_refuses_inputs_changed_after_analysis(tmp_path):
    root, run_dir = _make_run(tmp_path)
    ranked = json.loads((run_dir / "ranked.json").read_text())
    ranked[0]["width"] = 1280  # a weakened downgrade guard must never pass
    _write(run_dir / "ranked.json", ranked)

    with pytest.raises(ExportStaleError) as excinfo:
        export_run(run_dir, root, allow_export=True)

    assert "ranked.json changed" in str(excinfo.value)


def test_export_refuses_changed_review_or_labels(tmp_path):
    root, run_dir = _make_run(tmp_path)
    _write(run_dir / "review.json", {"schema_version": 1, "moments": [{"tampered": True}]})

    with pytest.raises(ExportStaleError) as excinfo:
        export_run(run_dir, root, allow_export=True)

    assert "review" in str(excinfo.value)


def test_plan_rejects_duplicate_selected_and_overlap(tmp_path):
    dup = [
        {"video_id": VIDEO, "excerpt_index": 0, "start_s": 7.0, "end_s": 13.0, "scene_type": "x", "geo": "supported", "flags": [], "reason": "selected"},
        {"video_id": VIDEO, "excerpt_index": 0, "start_s": 7.0, "end_s": 13.0, "scene_type": "x", "geo": "supported", "flags": [], "reason": "selected"},
    ]
    root, run_dir = _make_run(tmp_path / "dup", selected=dup)
    with pytest.raises(ExportStaleError) as excinfo:
        build_export_plan(run_dir, root)
    assert "more than once" in str(excinfo.value)

    both = [
        {"video_id": VIDEO, "excerpt_index": 0, "start_s": 7.0, "end_s": 13.0, "scene_type": "x", "geo": "supported", "flags": [], "reason": "selected"},
    ]
    excluded = [{"video_id": VIDEO, "excerpt_index": 0, "start_s": 7.0, "end_s": 13.0, "reasons": ["visual match rejected"]}]
    root2, run_dir2 = _make_run(tmp_path / "overlap", selected=both, excluded=excluded)
    with pytest.raises(ExportStaleError) as excinfo2:
        build_export_plan(run_dir2, root2)
    assert "excludes it" in str(excinfo2.value)


def test_export_refuses_theme_owned_by_another_run(tmp_path):
    root, run_dir = _make_run(tmp_path)
    theme_dir = root / "out" / "beautiful-natural-european-scenery"
    theme_dir.mkdir(parents=True)
    _write(theme_dir / "manifest.json", {"run_id": "19990101T000000Z", "schema_version": EXPORT_SCHEMA_VERSION})

    with pytest.raises(ExportError) as excinfo:
        export_run(run_dir, root, allow_export=True)

    assert "owned" in str(excinfo.value).lower() or "another run" in str(excinfo.value).lower()


def test_export_refuses_corrupt_prior_manifest(tmp_path):
    root, run_dir = _make_run(tmp_path)
    theme_dir = root / "out" / "beautiful-natural-european-scenery"
    theme_dir.mkdir(parents=True)
    (theme_dir / "manifest.json").write_text("{ not json")

    with pytest.raises(ExportError) as excinfo:
        export_run(run_dir, root, allow_export=True)

    assert "corrupt" in str(excinfo.value).lower()


def test_export_refuses_symlinked_output_dir(tmp_path):
    root, run_dir = _make_run(tmp_path)
    outside = tmp_path / "outside-out"
    outside.mkdir()
    (root / "out").symlink_to(outside)

    with pytest.raises(ExportError) as excinfo:
        export_run(run_dir, root, allow_export=True)

    assert "symlink" in str(excinfo.value).lower()


def test_export_requires_free_disk(tmp_path, monkeypatch):
    from collections import namedtuple

    root, run_dir = _make_run(tmp_path)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "scenery_brief_clips.export.shutil.disk_usage",
        lambda _path: usage(total=10, used=9, free=1),
    )

    with pytest.raises(ExportError) as excinfo:
        export_run(run_dir, root, allow_export=True)

    assert "disk" in str(excinfo.value).lower()
    assert not (root / "out").exists()


def test_plan_uses_only_selected_moments_and_is_deterministic(tmp_path):
    selected = [
        {"video_id": VIDEO, "excerpt_index": 1, "start_s": 7.0, "end_s": 13.0, "scene_type": "x", "geo": "supported", "flags": [], "reason": "selected"},
        {"video_id": VIDEO, "excerpt_index": 0, "start_s": 0.5, "end_s": 6.5, "scene_type": "y", "geo": "supported", "flags": [], "reason": "selected"},
    ]
    excluded = [
        {"video_id": VIDEO, "excerpt_index": 2, "start_s": 14.0, "end_s": 20.0, "reasons": ["visual match rejected"]}
    ]
    root, run_dir = _make_run(tmp_path, selected=selected, excluded=excluded)

    plan = build_export_plan(run_dir, root)
    plan_bytes = (root / "out" / plan["theme"] / "plan.json").read_bytes()
    plan2 = build_export_plan(run_dir, root)
    plan_bytes2 = (root / "out" / plan["theme"] / "plan.json").read_bytes()

    assert plan == plan2
    assert plan_bytes == plan_bytes2
    assert plan["schema_version"] == 2
    assert plan["max_height"] == 720
    assert [m["excerpt_index"] for m in plan["moments"]] == [0, 1]
    assert all(m["status"] == "ready" for m in plan["moments"])


def test_plan_picks_the_largest_rendition_under_the_720p_cap(tmp_path):
    root, run_dir = _make_run(tmp_path, recorded=(3840, 2160), formats=_formats_multi())
    plan = build_export_plan(run_dir, root)
    spec = plan["moments"][0]["spec"]
    assert (spec["format_id"], spec["width"], spec["height"]) == ("136", 1280, 720)
    assert plan["max_height"] == 720
    assert plan["schema_version"] == EXPORT_SCHEMA_VERSION


def test_plan_honors_a_higher_cap_from_config(tmp_path):
    root, run_dir = _make_run(tmp_path, recorded=(3840, 2160), formats=_formats_multi())
    plan = build_export_plan(run_dir, root, config={"export_max_height": 1080})
    spec = plan["moments"][0]["spec"]
    assert (spec["format_id"], spec["width"], spec["height"]) == ("137", 1920, 1080)
    assert plan["max_height"] == 1080


def test_plan_rejects_invalid_caps(tmp_path):
    root, run_dir = _make_run(tmp_path)
    for bad in (480, True, "720", 720.0, 0, 3000, None):
        with pytest.raises(ExportError):
            build_export_plan(run_dir, root, config={"export_max_height": bad})


def test_plan_fails_closed_without_an_under_cap_rendition(tmp_path):
    root, run_dir = _make_run(tmp_path / "only4k", recorded=(3840, 2160), formats=_formats_4k_only())
    plan = build_export_plan(run_dir, root)
    moment = plan["moments"][0]
    assert moment["status"] == "unplannable"
    assert moment["reason"] == "no_rendition_at_target"

    root2, run_dir2 = _make_run(tmp_path / "only1080", recorded=(3840, 2160), formats=_formats_1080_only())
    plan2 = build_export_plan(run_dir2, root2)
    assert plan2["moments"][0]["reason"] == "no_rendition_at_target"

    root3, run_dir3 = _make_run(tmp_path / "only480", recorded=(3840, 2160), formats=_formats_480_only())
    plan3 = build_export_plan(run_dir3, root3)
    assert plan3["moments"][0]["reason"] == "below_720_floor"


def test_plan_selection_is_deterministic_under_shuffled_metadata(tmp_path):
    shuffled = list(reversed(_formats_multi())) + [
        {"format_id": "298", "vcodec": "avc1.4d4020", "acodec": "none", "width": 1280, "height": 720, "fps": 60, "ext": "mp4"},
    ]
    root, run_dir = _make_run(tmp_path, recorded=(3840, 2160), formats=shuffled)
    plan = build_export_plan(run_dir, root)
    # Same codec preference: 60fps wins over 30fps; format id breaks remaining ties.
    assert plan["moments"][0]["spec"]["format_id"] == "298"
    plan_bytes = (root / "out" / plan["theme"] / "plan.json").read_bytes()

    root2, run_dir2 = _make_run(tmp_path / "again", recorded=(3840, 2160), formats=list(reversed(shuffled)))
    plan2 = build_export_plan(run_dir2, root2)
    assert plan2["moments"][0]["spec"]["format_id"] == "298"
    assert (root2 / "out" / plan2["theme"] / "plan.json").read_bytes() == plan_bytes


def test_plan_skips_ineligible_renditions_within_the_top_tier(tmp_path):
    portrait = {"format_id": "portrait", "vcodec": "avc1", "acodec": "none", "width": 405, "height": 720, "fps": 30, "ext": "mp4"}
    landscape = {"format_id": "land", "vcodec": "avc1", "acodec": "none", "width": 1280, "height": 720, "fps": 30, "ext": "mp4"}
    root, run_dir = _make_run(tmp_path, recorded=(1920, 1080), formats=[portrait, landscape])
    plan = build_export_plan(run_dir, root)
    assert plan["moments"][0]["spec"]["format_id"] == "land"

    # When the top 720p tier cannot satisfy policy, the moment fails -- it is
    # never silently delivered from a lower-height rendition.
    low = {"format_id": "low", "vcodec": "avc1", "acodec": "none", "width": 640, "height": 360, "fps": 30, "ext": "mp4"}
    root2, run_dir2 = _make_run(tmp_path / "nofallback", recorded=(1920, 1080), formats=[portrait, low])
    plan2 = build_export_plan(run_dir2, root2)
    moment = plan2["moments"][0]
    assert moment["status"] == "unplannable"
    assert moment["reason"] == "aspect_unsupported"


def test_plan_requires_strict_integers_and_explicit_video_only(tmp_path):
    base = {"format_id": "f", "vcodec": "avc1", "acodec": "none", "width": 1280, "height": 720, "fps": 30, "ext": "mp4"}
    cases = [
        {**base, "width": 1280.0},  # float dimensions
        {**base, "height": "720"},  # string dimensions
        {**base, "width": True},  # bool dimensions
        {key: value for key, value in base.items() if key != "acodec"},  # missing audio metadata
        {**base, "acodec": "mp4a.40.2"},  # muxed rendition
    ]
    for index, fmt in enumerate(cases):
        root, run_dir = _make_run(tmp_path / f"strict{index}", recorded=(1920, 1080), formats=[fmt])
        plan = build_export_plan(run_dir, root)
        assert plan["moments"][0]["reason"] == "no_rendition_at_target"

    root, run_dir = _make_run(tmp_path / "exceeds", recorded=(640, 360), formats=[base])
    plan = build_export_plan(run_dir, root)
    assert plan["moments"][0]["reason"] == "rendition_exceeds_recorded"


def test_plan_flags_bad_format_ids_and_aspects(tmp_path):
    bad = [{"format_id": "bad id/../x", "vcodec": "av01", "acodec": "none", "width": 1280, "height": 720, "fps": 30}]
    root, run_dir = _make_run(tmp_path / "badid", recorded=(1920, 1080), formats=bad)
    plan = build_export_plan(run_dir, root)
    assert plan["moments"][0]["reason"] == "bad_format_id"

    odd = [{"format_id": "1", "vcodec": "av01", "acodec": "none", "width": 1500, "height": 720, "fps": 30}]
    root2, run_dir2 = _make_run(tmp_path / "aspect", recorded=(1920, 1080), formats=odd)
    plan2 = build_export_plan(run_dir2, root2)
    assert plan2["moments"][0]["reason"] == "aspect_unsupported"

    root3, run_dir3 = _make_run(tmp_path / "badmeta", recorded=(1920, 1080), formats={"not": "a list"})
    plan3 = build_export_plan(run_dir3, root3)
    assert plan3["moments"][0]["reason"] == "metadata_invalid"


def test_plan_clamps_margin_to_zero_and_to_source_duration(tmp_path):
    selected = [
        {"video_id": VIDEO, "excerpt_index": 0, "start_s": 0.3, "end_s": 6.3, "scene_type": "x", "geo": "supported", "flags": [], "reason": "selected"},
        {"video_id": VIDEO, "excerpt_index": 3, "start_s": 17.5, "end_s": 23.5, "scene_type": "y", "geo": "supported", "flags": [], "reason": "selected"},
    ]
    root, run_dir = _make_run(tmp_path, selected=selected, duration_s=24.0)

    plan = build_export_plan(run_dir, root)
    first, second = plan["moments"]
    assert first["acq_start_ms"] == 0
    assert first["acq_end_ms"] == 8300
    assert second["acq_end_ms"] == 24000  # clamped to source duration
    assert second["acq_start_ms"] == 15500


def test_export_downloads_only_planned_spans_and_publishes(tmp_path, synth_source, monkeypatch):
    _fast_encode(monkeypatch)
    section = _section(synth_source, 5.0, 15.0, tmp_path / "sec.mp4")
    root, run_dir = _make_run(tmp_path, formats=_formats_multi())
    fake = FakeYt({(VIDEO, 5.0, 15.0): section}, tmp_path / "acq")

    manifest = export_run(run_dir, root, allow_export=True, yt=fake)

    assert len(fake.calls) == 1
    assert fake.calls[0].span == (5.0, 15.0)
    assert fake.calls[0].format_id == "136"

    theme = slug_theme("Beautiful natural european scenery")
    clips = sorted((root / "out" / theme / "clips").glob("*.mp4"))
    assert [c.name for c in clips] == ["aaaaaaaaaaa_e0_7000-13000.mp4"]
    entry = manifest["clips"][0]
    assert entry["file"] == "clips/aaaaaaaaaaa_e0_7000-13000.mp4"
    assert (entry["width"], entry["height"]) == (1280, 720)
    assert entry["n_frames"] == 180
    assert manifest["schema_version"] == EXPORT_SCHEMA_VERSION == 2
    assert manifest["max_height"] == 720
    assert entry["acq_sha256"] == sha256_file(fake.paths[0])
    assert manifest["counts"] == {
        "requested": 20,
        "selected": 1,
        "exported": 1,
        "failed": 0,
        "export_complete": True,
        "request_fulfilled": False,
    }
    pointer = _pointer_of(run_dir)
    assert pointer["schema_version"] == 2
    assert pointer["theme"] == theme
    assert pointer["manifest_sha256"] == sha256_file(root / "out" / theme / "manifest.json")
    assert pointer["manifest_path"] == f"out/{theme}/manifest.json"


def test_export_clip_trim_maps_absolute_source_times_and_matches_content(
    tmp_path, synth_source, monkeypatch
):
    _fast_encode(monkeypatch)
    section = _section(synth_source, 5.0, 15.0, tmp_path / "sec.mp4")
    root, run_dir = _make_run(tmp_path, formats=_formats_multi())
    fake = FakeYt({(VIDEO, 5.0, 15.0): section}, tmp_path / "acq")

    manifest = export_run(run_dir, root, allow_export=True, yt=fake)

    entry = manifest["clips"][0]
    theme = slug_theme("Beautiful natural european scenery")
    clip = root / "out" / theme / entry["file"]
    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-of", "json", str(clip)],
            capture_output=True, text=True, check=True,
        ).stdout
    )
    duration = float(probe["format"]["duration"])
    assert entry["mapping_k_ms"] == 5000
    assert entry["start_ms"] == 7000 and entry["end_ms"] == 13000
    assert entry["acq_span_ms"] == [5000, 15000]
    assert abs(duration - 6.0) < 0.05
    assert entry["duration_ms"] == 6000
    assert entry["n_frames"] == 180

    # Content correspondence: a clip frame must be the same picture as the
    # acquisition frame at the mapped source time (clip local 1.0 == source 8.0;
    # acquisition local 3.0). dHash distance must be tiny.
    clip_frame = tmp_path / "clip_frame.jpg"
    acq_frame = tmp_path / "acq_frame.jpg"
    _ffmpeg("-ss", "1.0", "-i", str(clip), "-frames:v", "1", "-q:v", "2", str(clip_frame))
    _ffmpeg("-ss", "3.0", "-i", str(fake.paths[0]), "-frames:v", "1", "-q:v", "2", str(acq_frame))
    assert (frame_dhash(clip_frame) ^ frame_dhash(acq_frame)).bit_count() <= 6


def test_export_records_failure_and_continues(tmp_path, synth_source, monkeypatch):
    _fast_encode(monkeypatch)
    selected = [
        {"video_id": VIDEO, "excerpt_index": 0, "start_s": 7.0, "end_s": 13.0, "scene_type": "x", "geo": "supported", "flags": [], "reason": "selected"},
        {"video_id": VIDEO, "excerpt_index": 4, "start_s": 0.3, "end_s": 6.3, "scene_type": "y", "geo": "supported", "flags": [], "reason": "selected"},
    ]
    section = _section(synth_source, 5.0, 15.0, tmp_path / "sec.mp4")
    root, run_dir = _make_run(tmp_path, selected=selected, formats=_formats_multi())
    fake = FakeYt({(VIDEO, 5.0, 15.0): section}, tmp_path / "acq")  # only idx0 prepared

    manifest = export_run(run_dir, root, allow_export=True, yt=fake)

    assert manifest["counts"]["exported"] == 1
    assert manifest["counts"]["failed"] == 1
    assert manifest["counts"]["export_complete"] is False
    (failed,) = manifest["failed"]
    assert failed["excerpt_index"] == 4
    assert failed["reason"] == "download_failed"
    assert "detail" in failed
    assert failed.get("attempts") == 2


def test_export_preserves_specific_media_code_across_retries(tmp_path, monkeypatch):
    _fast_encode(monkeypatch)
    root, run_dir = _make_run(tmp_path, formats=_formats_multi())
    failing = FailingYt(ExportMediaError("dimension_mismatch", "acquisition is 1280x720"))

    manifest = export_run(run_dir, root, allow_export=True, yt=failing)

    (failed,) = manifest["failed"]
    assert failed["reason"] == "dimension_mismatch"
    assert failed["attempts"] == 2
    assert len(failed["attempt_errors"]) == 2


def test_export_flags_coverage_missing_without_downgrade(tmp_path, synth_source, monkeypatch):
    _fast_encode(monkeypatch)
    section = _section(synth_source, 5.0, 12.0, tmp_path / "sec.mp4")
    root, run_dir = _make_run(tmp_path, formats=_formats_multi())
    fake = FakeYt({(VIDEO, 5.0, 15.0): section}, tmp_path / "acq")

    manifest = export_run(run_dir, root, allow_export=True, yt=fake)

    assert manifest["counts"]["exported"] == 0
    (failed,) = manifest["failed"]
    assert failed["reason"] == "coverage_missing"
    assert manifest["clips"] == []


def test_export_fails_late_start_instead_of_clamping(tmp_path, synth_source, monkeypatch):
    _fast_encode(monkeypatch)
    # The acquisition starts at source 7.0 but the clip needs 6.9: the missing
    # beginning must fail the moment, never be clamped away.
    section = _section(synth_source, 7.0, 15.0, tmp_path / "sec.mp4")
    selected = [
        {"video_id": VIDEO, "excerpt_index": 0, "start_s": 6.9, "end_s": 12.0, "scene_type": "x", "geo": "supported", "flags": [], "reason": "selected"},
    ]
    root, run_dir = _make_run(tmp_path, selected=selected, formats=_formats_multi())
    fake = FakeYt({(VIDEO, 4.9, 14.0): section}, tmp_path / "acq")

    manifest = export_run(run_dir, root, allow_export=True, yt=fake)

    assert manifest["counts"]["exported"] == 0
    (failed,) = manifest["failed"]
    assert failed["reason"] == "coverage_missing"
    assert "beginning would be missing" in failed["detail"]


def test_export_normalizes_probe_and_encode_timeouts(tmp_path, synth_source, monkeypatch):
    _fast_encode(monkeypatch)
    section = _section(synth_source, 5.0, 15.0, tmp_path / "sec.mp4")
    root, run_dir = _make_run(tmp_path, formats=_formats_multi())
    fake = FakeYt({(VIDEO, 5.0, 15.0): section}, tmp_path / "acq")

    def slow_probe(_path):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)

    monkeypatch.setattr("scenery_brief_clips.export.probe_export_coverage", slow_probe)
    manifest = export_run(run_dir, root, allow_export=True, yt=fake)
    assert manifest["failed"][0]["reason"] == "probe_timeout"

    root2, run_dir2 = _make_run(tmp_path / "enc", formats=_formats_multi())
    fake2 = FakeYt({(VIDEO, 5.0, 15.0): section}, tmp_path / "acq2")
    monkeypatch.undo()
    _fast_encode(monkeypatch)

    def slow_encode(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    monkeypatch.setattr("scenery_brief_clips.export.encode_clip", slow_encode)
    manifest2 = export_run(run_dir2, root2, allow_export=True, yt=fake2)
    assert manifest2["failed"][0]["reason"] == "encode_timeout"


def test_export_reuses_clips_and_downloads_on_rerun(tmp_path, synth_source, monkeypatch):
    _fast_encode(monkeypatch)
    section = _section(synth_source, 5.0, 15.0, tmp_path / "sec.mp4")
    root, run_dir = _make_run(tmp_path, formats=_formats_multi())
    fake = FakeYt({(VIDEO, 5.0, 15.0): section}, tmp_path / "acq")

    first = export_run(run_dir, root, allow_export=True, yt=fake)
    theme = first["theme"]
    clips_dir = root / "out" / theme / "clips"
    clip = clips_dir / first["clips"][0]["file"].split("/")[-1]
    first_stat = clip.stat()
    first_manifest_bytes = (root / "out" / theme / "manifest.json").read_bytes()

    encode_calls = {"n": 0}
    real_encode = __import__("scenery_brief_clips.export", fromlist=["encode_clip"]).encode_clip

    def counting_encode(*args, **kwargs):
        encode_calls["n"] += 1
        return real_encode(*args, **kwargs)

    monkeypatch.setattr("scenery_brief_clips.export.encode_clip", counting_encode)

    second = export_run(run_dir, root, allow_export=True, yt=fake)

    assert encode_calls["n"] == 0  # clip reused, not re-encoded
    assert clip.stat().st_size == first_stat.st_size
    assert (root / "out" / theme / "manifest.json").read_bytes() == first_manifest_bytes
    assert second["clips"] == first["clips"]
    assert list(clips_dir.glob("*.staging-*")) == []


def test_export_reencodes_when_acquisition_content_changes(tmp_path, synth_source, alt_source, monkeypatch):
    _fast_encode(monkeypatch)
    section_a = _section(synth_source, 5.0, 15.0, tmp_path / "a.mp4")
    section_b = _section(alt_source, 5.0, 15.0, tmp_path / "b.mp4")
    root, run_dir = _make_run(tmp_path, formats=_formats_multi())

    first = export_run(run_dir, root, allow_export=True, yt=FakeYt({(VIDEO, 5.0, 15.0): section_a}, tmp_path / "acqA"))
    theme = first["theme"]
    clip_path = root / "out" / theme / first["clips"][0]["file"]
    first_sha = first["clips"][0]["sha256"]

    encode_calls = {"n": 0}
    real_encode = __import__("scenery_brief_clips.export", fromlist=["encode_clip"]).encode_clip

    def counting_encode(*args, **kwargs):
        encode_calls["n"] += 1
        return real_encode(*args, **kwargs)

    monkeypatch.setattr("scenery_brief_clips.export.encode_clip", counting_encode)

    second = export_run(run_dir, root, allow_export=True, yt=FakeYt({(VIDEO, 5.0, 15.0): section_b}, tmp_path / "acqB"))

    assert encode_calls["n"] == 1  # same span/K/format but different bytes -> no reuse
    assert second["clips"][0]["sha256"] != first_sha
    assert second["clips"][0]["acq_sha256"] != first["clips"][0]["acq_sha256"]


def test_export_removes_stale_clips_after_selection_shrinks(tmp_path, synth_source, monkeypatch):
    _fast_encode(monkeypatch)
    selected_two = [
        {"video_id": VIDEO, "excerpt_index": 0, "start_s": 7.0, "end_s": 13.0, "scene_type": "x", "geo": "supported", "flags": [], "reason": "selected"},
        {"video_id": VIDEO, "excerpt_index": 1, "start_s": 14.0, "end_s": 20.0, "scene_type": "y", "geo": "supported", "flags": [], "reason": "selected"},
    ]
    sections = {
        (VIDEO, 5.0, 15.0): _section(synth_source, 5.0, 15.0, tmp_path / "s1.mp4"),
        (VIDEO, 12.0, 22.0): _section(synth_source, 12.0, 22.0, tmp_path / "s2.mp4"),
    }
    root, run_dir = _make_run(tmp_path, selected=selected_two, formats=_formats_multi())
    fake = FakeYt(sections, tmp_path / "acq")

    first = export_run(run_dir, root, allow_export=True, yt=fake)
    assert first["counts"]["exported"] == 2
    theme = first["theme"]
    clips_dir = root / "out" / theme / "clips"
    assert len(list(clips_dir.glob("*.mp4"))) == 2

    # Shrink the selection to the first moment only.
    shortlist = json.loads((run_dir / "shortlist.json").read_text())
    shortlist["selected"] = [selected_two[0]]
    _write(run_dir / "shortlist.json", shortlist)

    second = export_run(run_dir, root, allow_export=True, yt=fake)

    assert second["counts"]["selected"] == 1
    remaining = sorted(p.name for p in clips_dir.glob("*.mp4"))
    assert remaining == ["aaaaaaaaaaa_e0_7000-13000.mp4"]
    assert second["clips"][0]["excerpt_index"] == 0


def test_export_manifest_has_no_volatile_timestamps(tmp_path, synth_source, monkeypatch):
    _fast_encode(monkeypatch)
    section = _section(synth_source, 5.0, 15.0, tmp_path / "sec.mp4")
    root, run_dir = _make_run(tmp_path, formats=_formats_multi())
    fake = FakeYt({(VIDEO, 5.0, 15.0): section}, tmp_path / "acq")

    manifest = export_run(run_dir, root, allow_export=True, yt=fake)
    raw = json.dumps(manifest)
    assert "now" not in raw and "timestamp" not in raw
    theme = manifest["theme"]
    published = json.loads((root / "out" / theme / "manifest.json").read_text())
    assert published == manifest


def test_export_cap_change_invalidates_reuse(tmp_path, synth_source, synth_1080, monkeypatch):
    _fast_encode(monkeypatch)
    sec_1080 = _section(synth_1080, 5.0, 15.0, tmp_path / "s1080.mp4")
    sec_720 = _section(synth_source, 5.0, 15.0, tmp_path / "s720.mp4")
    root, run_dir = _make_run(tmp_path, formats=_formats_multi(), recorded=(3840, 2160))

    first = export_run(
        run_dir,
        root,
        allow_export=True,
        yt=FakeYt({(VIDEO, 5.0, 15.0): sec_1080}, tmp_path / "acq1080"),
        config={"export_max_height": 1080},
    )
    assert (first["clips"][0]["width"], first["clips"][0]["height"]) == (1920, 1080)
    assert first["clips"][0]["format_id"] == "137"
    theme = first["theme"]

    encode_calls = {"n": 0}
    real_encode = __import__("scenery_brief_clips.export", fromlist=["encode_clip"]).encode_clip

    def counting_encode(*args, **kwargs):
        encode_calls["n"] += 1
        return real_encode(*args, **kwargs)

    monkeypatch.setattr("scenery_brief_clips.export.encode_clip", counting_encode)

    second = export_run(
        run_dir,
        root,
        allow_export=True,
        yt=FakeYt({(VIDEO, 5.0, 15.0): sec_720}, tmp_path / "acq720"),
    )

    assert encode_calls["n"] == 1  # the spec/cap changed: fresh encode, never a stale reuse
    assert (second["clips"][0]["width"], second["clips"][0]["height"]) == (1280, 720)
    assert second["clips"][0]["format_id"] == "136"
    assert second["max_height"] == 720
    clip = root / "out" / theme / second["clips"][0]["file"]
    assert clip.stat().st_size == second["clips"][0]["size_bytes"]


def test_encode_recipe_defaults_are_the_production_arguments(tmp_path, monkeypatch):
    # Asserts the REAL recipe argv (no preset monkeypatch): one x264 pass, half-open trim.
    acq = tmp_path / "acq.mp4"
    acq.write_bytes(b"unused")
    dest = tmp_path / "clip.mp4"
    captured: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        captured.append(list(cmd))
        dest.write_bytes(b"encoded")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    from scenery_brief_clips.export import encode_clip

    encode_clip(acq, 2.0, 8.0, dest, runner=fake_runner)

    (cmd,) = captured
    assert cmd[0] == "ffmpeg"
    assert "-xerror" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "trim=start=2.000000:end=8.000000,setpts=PTS-STARTPTS"
    assert "libx264" in cmd
    assert cmd.count("libx264") == 1
    assert cmd.count("-i") == 1  # a single encode pass, no second input
    assert "-r" not in cmd and "-s" not in cmd
    assert "scale" not in vf and "crop" not in vf and "pad" not in vf
    assert cmd[cmd.index("-preset") + 1] == ENCODE_PRESET == "medium"
    assert cmd[cmd.index("-crf") + 1] == str(ENCODE_CRF) == "17"
    assert "-an" in cmd
    assert cmd[cmd.index("-fps_mode") + 1] == "passthrough"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert cmd[cmd.index("-threads:v") + 1] == "1"


def test_export_plan_and_schema_versions(tmp_path):
    assert EXPORT_SCHEMA_VERSION == 2
    assert MARGIN_S == 2.0




def test_export_plan_preserves_shortlist_intervals(tmp_path):
    """Approved shortlist start/end must reach the plan unchanged (no continuity re-trim)."""
    from scenery_brief_clips.export import _validated_moment

    selected = [
        {
            "video_id": VIDEO,
            "excerpt_index": 0,
            "start_s": 12.345,
            "end_s": 18.901,
            "scene_type": "fjord",
            "geo": "supported",
            "flags": [],
            "reason": "selected",
        }
    ]
    video_id, index, start, end = _validated_moment(selected[0])
    assert (video_id, index, start, end) == (VIDEO, 0, 12.345, 18.901)

    root, run_dir = _make_run(tmp_path, selected=selected, excluded=[])
    plan = build_export_plan(run_dir, root)
    moments = plan["moments"]
    assert len(moments) == 1
    moment = moments[0]
    assert moment["start_ms"] == 12345
    assert moment["end_ms"] == 18901
    assert moment["video_id"] == VIDEO
    assert moment["excerpt_index"] == 0
