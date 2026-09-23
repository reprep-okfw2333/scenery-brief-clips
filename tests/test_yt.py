import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scenery_brief_clips.analysis_cache import (
    ANALYSIS_CACHE_POLICY,
    ANALYSIS_MARKER_SCHEMA_VERSION,
    sha256_file,
)
from scenery_brief_clips.yt import (
    EXPORT_MAX_FILESIZE_BYTES,
    DownloadForbidden,
    ExportMediaError,
    ExportSpec,
    YtDlp,
    analysis_marker_path,
    export_marker_path,
    validate_export_media,
)


def _ok(payload: dict):
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(payload) + "\n", stderr=""
        )

    return runner


def test_fetch_metadata_always_skips_download_and_uses_watch_url(tmp_path):
    calls: list[dict] = []

    def runner(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env")})
        return subprocess.CompletedProcess(cmd, 0, stdout='{"id":"abc123abc12"}\n', stderr="")

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    info = client.fetch_metadata("abc123abc12")
    assert info["id"] == "abc123abc12"
    cmd = calls[0]["cmd"]
    assert "-j" in cmd
    assert "--skip-download" in cmd
    assert cmd[-1] == "https://www.youtube.com/watch?v=abc123abc12"
    assert not any(arg in {"-f", "--format"} for arg in cmd)


def test_dash_prefixed_id_is_not_passed_as_bare_argv(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"id":"-LA9aBfC6j8"}\n', stderr="")

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    client.fetch_metadata("-LA9aBfC6j8")
    cmd = calls[0]
    assert "-LA9aBfC6j8" not in cmd
    assert "https://www.youtube.com/watch?v=-LA9aBfC6j8" in cmd


def test_subprocess_tmpdir_is_project_tmp(tmp_path):
    seen = {}

    def runner(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="{}\n", stderr="")

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    client.fetch_metadata("abcdefghijk")
    assert seen["env"]["TMPDIR"] == str(tmp_path)


def _write_media(path, seconds: float) -> None:
    import subprocess as _subprocess

    _subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=teal:s=320x180:d={seconds}",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def test_fetch_analysis_rejects_media_shorter_than_span(tmp_path):
    import subprocess

    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        out = Path(cmd[cmd.index("-o") + 1])
        _write_media(out, 4.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    dest = tmp_path / "clip.mp4"

    with pytest.raises(RuntimeError):
        client.fetch_analysis("abcdefghijk", dest, (0.0, 14.0))

    assert not dest.is_file()
    assert not analysis_marker_path(dest).is_file()
    assert len(calls) == 1


def test_fetch_analysis_heals_short_cached_media(tmp_path):
    import subprocess

    dest = tmp_path / "clip.mp4"
    _write_media(dest, 4.0)
    marker = {
        "schema_version": ANALYSIS_MARKER_SCHEMA_VERSION,
        "cache_policy": ANALYSIS_CACHE_POLICY,
        "video_id": "abcdefghijk",
        "span_ms": [0, 14000],
        "size_bytes": dest.stat().st_size,
        "sha256": sha256_file(dest),
    }
    analysis_marker_path(dest).write_text(json.dumps(marker), encoding="utf-8")

    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        out = Path(cmd[cmd.index("-o") + 1])
        _write_media(out, 14.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    result = client.fetch_analysis("abcdefghijk", dest, (0.0, 14.0))

    assert result == dest
    assert len(calls) == 1  # the short cache entry was rejected and re-downloaded
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(dest)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert abs(float(probe.stdout.strip()) - 14.0) <= 1.0


def test_fetch_analysis_sweeps_old_abandoned_staging_files(tmp_path):
    import os
    import subprocess

    stale = tmp_path / "other_10000-20000_v3-video-only-720.mp4.staging-deadbeef.mp4"
    stale.write_bytes(b"partial")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = tmp_path / "other2_10000-20000_v3-video-only-720.mp4.staging-cafebabe.mp4"
    fresh.write_bytes(b"live")

    def runner(cmd, **kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_bytes(b"fresh complete file")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = YtDlp(
        tmp_dir=tmp_path,
        allow_download=False,
        runner=runner,
        analysis_validator=lambda _path, _span: None,
    )
    client.fetch_analysis("abcdefghijk", tmp_path / "clip.mp4", (10.0, 20.0))

    assert not stale.exists()
    assert fresh.exists()


def test_all_yt_dlp_invocations_ignore_external_config(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"video")
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"id":"abcdefghijk"}\n', stderr=""
        )

    client = YtDlp(
        tmp_dir=tmp_path,
        allow_download=False,
        runner=runner,
        analysis_validator=lambda _path, _span: None,
    )
    client.fetch_metadata("abcdefghijk")
    client.search("alps", limit=1)
    client.fetch_analysis("abcdefghijk", tmp_path / "clip.mp4", (0.0, 5.0))

    assert len(calls) == 3
    assert all(cmd[1] == "--ignore-config" for cmd in calls)
    assert all("--js-runtimes" in cmd and "node" in cmd for cmd in calls)


def test_download_is_forbidden_in_part1(tmp_path):
    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=_ok({}))
    with pytest.raises(DownloadForbidden):
        client.download("abcdefghijk")


def test_search_uses_ytsearch_and_skip_download(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        line = json.dumps({"id": "abcdefghijk", "title": "Alps", "duration": 120})
        return subprocess.CompletedProcess(cmd, 0, stdout=line + "\n", stderr="")

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    rows = client.search("european scenery 4k", limit=8)
    assert rows[0]["id"] == "abcdefghijk"
    cmd = calls[0]
    assert "--skip-download" in cmd
    assert "--flat-playlist" in cmd
    assert "ytsearch8:european scenery 4k" in cmd


def test_nonzero_exit_raises(tmp_path):
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    with pytest.raises(RuntimeError, match="boom"):
        client.fetch_metadata("abcdefghijk")


def test_analysis_args_are_720p_video_only_with_section(tmp_path):
    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=_ok({}))
    dest = tmp_path / "clip.mp4"
    cmd = client.analysis_args("abc123abc12", dest, (10.0, 22.5))
    assert "-f" in cmd
    fmt = cmd[cmd.index("-f") + 1]
    selectors = fmt.split("/")
    assert selectors
    assert all("[height<=720]" in selector for selector in selectors)
    assert all(selector.startswith(("bv[", "wv[")) for selector in selectors)
    assert all("bv*" not in selector and "wv*" not in selector for selector in selectors)
    assert "avc" in fmt
    assert "+ba" not in fmt
    assert "bestaudio" not in fmt
    assert "--skip-download" not in cmd
    assert "--download-sections" in cmd
    assert "--force-keyframes-at-cuts" in cmd
    assert "*00:00:10.000-00:00:22.500" in cmd
    assert cmd[-1] == "https://www.youtube.com/watch?v=abc123abc12"
    assert str(dest) in cmd


def test_analysis_args_dash_id_uses_watch_url(tmp_path):
    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=_ok({}))
    cmd = client.analysis_args("-LA9aBfC6j8", tmp_path / "a.mp4", (0.0, 5.0))
    assert "-LA9aBfC6j8" not in cmd
    assert "https://www.youtube.com/watch?v=-LA9aBfC6j8" in cmd


def test_fetch_analysis_reuses_only_validated_completed_cache(tmp_path):
    calls = []
    dest = tmp_path / "clip.mp4"
    dest.write_bytes(b"stale interrupted file")
    dest.with_suffix(dest.suffix + ".part").write_bytes(b"partial")
    orphan = tmp_path / "clip.staging-abandoned.mp4.part"
    orphan.write_bytes(b"abandoned")

    def runner(cmd, **kwargs):
        calls.append(cmd)
        output = Path(cmd[cmd.index("-o") + 1])
        assert output != dest
        assert output.parent == dest.parent
        assert not output.exists()
        output.write_bytes(b"fresh complete file")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = YtDlp(
        tmp_dir=tmp_path,
        allow_download=False,
        runner=runner,
        analysis_validator=lambda _path, _span: None,
    )
    first = client.fetch_analysis("abcdefghijk", dest, (10.0, 20.0))
    assert first.read_bytes() == b"fresh complete file"
    assert analysis_marker_path(dest).is_file()
    marker = json.loads(analysis_marker_path(dest).read_text())
    assert marker["video_id"] == "abcdefghijk"
    assert marker["span_ms"] == [10000, 20000]
    assert marker["size_bytes"] == dest.stat().st_size
    assert len(marker["sha256"]) == 64
    assert marker["cache_policy"]

    second = client.fetch_analysis("abcdefghijk", dest, (10.0001, 20.0001))
    assert second == dest
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.staging-*.mp4*"))


def test_fetch_analysis_refreshes_malformed_marker_and_same_size_corruption(tmp_path):
    calls = []
    dest = tmp_path / "clip.mp4"
    marker = analysis_marker_path(dest)
    dest.write_bytes(b"bad1")
    marker.write_text("[]\n")

    def runner(cmd, **kwargs):
        calls.append(cmd)
        output = Path(cmd[cmd.index("-o") + 1])
        output.write_bytes(b"good")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = YtDlp(
        tmp_dir=tmp_path,
        allow_download=False,
        runner=runner,
        analysis_validator=lambda _path, _span: None,
    )
    client.fetch_analysis("abcdefghijk", dest, (10.0, 20.0))
    assert dest.read_bytes() == b"good"
    assert len(calls) == 1

    dest.write_bytes(b"evil")  # same size as the validated bytes
    client.fetch_analysis("abcdefghijk", dest, (10.0, 20.0))
    assert dest.read_bytes() == b"good"
    assert len(calls) == 2


def test_concurrent_same_span_fetches_publish_once(tmp_path):
    calls = 0
    dest = tmp_path / "clip.mp4"

    def runner(cmd, **kwargs):
        nonlocal calls
        calls += 1
        output = Path(cmd[cmd.index("-o") + 1])
        time.sleep(0.05)
        output.write_bytes(b"complete")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = YtDlp(
        tmp_dir=tmp_path,
        allow_download=False,
        runner=runner,
        analysis_validator=lambda _path, _span: None,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: client.fetch_analysis("abcdefghijk", dest, (10.0, 20.0)),
                range(2),
            )
        )

    assert results == [dest, dest]
    assert calls == 1
    assert dest.read_bytes() == b"complete"


def test_failed_staged_download_does_not_publish_or_leave_staging_files(tmp_path):
    dest = tmp_path / "clip.mp4"

    def runner(cmd, **kwargs):
        output = Path(cmd[cmd.index("-o") + 1])
        output.write_bytes(b"partial")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network down")

    client = YtDlp(
        tmp_dir=tmp_path,
        allow_download=False,
        runner=runner,
        analysis_validator=lambda _path, _span: None,
    )
    with pytest.raises(RuntimeError, match="network down"):
        client.fetch_analysis("abcdefghijk", dest, (10.0, 20.0))

    assert not dest.exists()
    assert not analysis_marker_path(dest).exists()
    assert not list(tmp_path.glob("*.staging-*.mp4"))


def test_download_generic_still_forbidden_during_analysis_stage(tmp_path):
    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=_ok({}))
    with pytest.raises(DownloadForbidden):
        client.download("abcdefghijk")


def _copyts_source(tmp_path: Path) -> Path:
    src = tmp_path / "_export_src.mp4"
    if not src.is_file():
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=24",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-crf", "30",
                "-pix_fmt", "yuv420p", str(src),
            ],
            check=True,
        )
    return src


def _holed_source(tmp_path: Path) -> Path:
    """The copyts source re-encoded with EXACTLY ONE frame dropped at t=8.0s
    (a 66.7ms hole), preserving absolute timestamps."""
    src = tmp_path / "_holed_src.mp4"
    if not src.is_file():
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-copyts",
                "-i", str(_copyts_source(tmp_path)),
                "-vf", "select='not(between(t,7.999,8.001))'",
                "-fps_mode", "passthrough",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-crf", "30",
                "-pix_fmt", "yuv420p", str(src),
            ],
            check=True,
        )
    return src


def _copyts_section(tmp_path: Path, dest: Path, start: float, end: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-copyts", "-ss", str(start), "-i", str(_copyts_source(tmp_path)),
            "-to", str(end), "-c", "copy", str(dest),
        ],
        check=True,
    )


def _export_spec(**overrides) -> ExportSpec:
    values = {
        "video_id": "abcdefghijk",
        "format_id": "136",
        "width": 1280,
        "height": 720,
        "codec": "avc1.4d401f",
        "span": (5.0, 15.0),
        "clip_start_ms": 7000,
        "clip_end_ms": 13000,
    }
    values.update(overrides)
    return ExportSpec(**values)


def _authorized_client(tmp_path: Path, runner) -> YtDlp:
    return YtDlp(
        tmp_dir=tmp_path,
        allow_download=False,
        runner=runner,
        allow_export=True,
        export_cache_dir=tmp_path / "export-cache",
    )


def test_fetch_export_requires_authorization(tmp_path):
    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=_ok({}))

    with pytest.raises(DownloadForbidden):
        client.fetch_export(_export_spec())


def test_fetch_export_uses_copyts_pinned_format_and_writes_marker(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        out = Path(cmd[cmd.index("-o") + 1])
        _copyts_section(tmp_path, out, 5.0, 15.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = _authorized_client(tmp_path, runner)
    acquired = client.fetch_export(_export_spec())

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[1] == "--ignore-config"
    assert cmd[cmd.index("-f") + 1] == "136"
    assert cmd[cmd.index("--download-sections") + 1] == "*00:00:05.000-00:00:15.000"
    assert cmd[cmd.index("--downloader-args") + 1] == "ffmpeg:-copyts"
    assert cmd[cmd.index("--max-filesize") + 1] == str(EXPORT_MAX_FILESIZE_BYTES)
    assert acquired.is_file()
    marker = json.loads(export_marker_path(acquired).read_text())
    assert marker["format_id"] == "136"
    assert marker["first_pts_ms"] == 5000
    assert marker["start_time_ms"] == 5000
    assert marker["cache_policy"].startswith("v2-export-cap-copyts.")
    assert acquired.name.endswith("v2-export-cap-copyts.136.mp4")


def test_fetch_export_cache_hit_does_not_redownload(tmp_path):
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        out = Path(cmd[cmd.index("-o") + 1])
        _copyts_section(tmp_path, out, 5.0, 15.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = _authorized_client(tmp_path, runner)
    first = client.fetch_export(_export_spec())
    second = client.fetch_export(_export_spec())

    assert first == second
    assert len(calls) == 1


def test_fetch_export_rejects_unsafe_format_id(tmp_path):
    def runner(cmd, **kwargs):
        raise AssertionError("no download should be attempted for an invalid spec")

    client = _authorized_client(tmp_path, runner)

    with pytest.raises(ExportMediaError) as excinfo:
        client.fetch_export(_export_spec(format_id="bad id/../x"))

    assert excinfo.value.code == "spec_invalid"


def test_fetch_export_rejects_sub_floor_or_invalid_geometry(tmp_path):
    def runner(cmd, **kwargs):
        raise AssertionError("no download should be attempted for an invalid spec")

    client = _authorized_client(tmp_path, runner)

    cases = [
        {"width": 640, "height": 480},  # below the 720p floor
        {"width": 720, "height": 1280},  # not 16:9
        {"width": 1280.0, "height": 720},  # non-integer dimensions
        {"width": True, "height": 720},  # bool dimensions
    ]
    for overrides in cases:
        with pytest.raises(ExportMediaError) as excinfo:
            client.fetch_export(_export_spec(**overrides))
        assert excinfo.value.code == "spec_invalid"


def test_fetch_export_enforces_size_bound(tmp_path, monkeypatch):
    monkeypatch.setattr("scenery_brief_clips.yt.EXPORT_MAX_FILESIZE_BYTES", 10)

    def runner(cmd, **kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        _copyts_section(tmp_path, out, 5.0, 15.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    client = _authorized_client(tmp_path, runner)

    with pytest.raises(ExportMediaError) as excinfo:
        client.fetch_export(_export_spec())

    assert excinfo.value.code == "size_exceeded"
    assert not list((tmp_path / "export-cache").glob("*.mp4"))


def test_probe_export_coverage_counts_packet_before_end_without_millisecond_rounding(monkeypatch):
    """The live 29.97fps frame at 33.032833s is before end=33.033s."""
    from scenery_brief_clips.yt import probe_export_coverage

    first = 28.6618
    frame_s = 1001 / 30000
    packets = [
        {"pts_time": f"{first + index * frame_s:.6f}", "duration_time": f"{frame_s:.6f}"}
        for index in range(-1, 133)
    ]
    streams = {
        "streams": [{"codec_type": "video", "r_frame_rate": "30000/1001", "width": 1280, "height": 720}],
        "format": {"start_time": packets[0]["pts_time"]},
    }

    def fake_run(cmd, **kwargs):
        payload = streams if "-show_streams" in cmd else {"packets": packets}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr("scenery_brief_clips.yt.subprocess.run", fake_run)
    info = probe_export_coverage(Path("synthetic-packets.mp4"), window_ms=(28654, 33033))
    assert info["window_n_frames"] == 132
    assert info["window_max_gap_s"] <= frame_s + 0.001


def test_validate_export_media_rejects_hdr_bitdepth_and_mapping_mismatch(tmp_path, monkeypatch):
    probe = {
        "width": 1280, "height": 720, "codec": "h264", "pix_fmt": "yuv420p", "sar": None,
        "color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709",
        "rotation": 0, "fps_num": 30, "fps_den": 1, "start_time_s": 5.0,
        "first_pts_s": 5.0, "last_end_s": 15.0, "n_frames": 300, "max_gap_s": 0.0333,
        "window_n_frames": 180, "window_max_gap_s": 0.0333,
    }
    media = tmp_path / "media.mp4"
    media.write_bytes(b"x")
    spec = _export_spec()

    monkeypatch.setattr("scenery_brief_clips.yt.probe_export_coverage", lambda _p, window_ms=None, **kw: dict(probe, color_transfer="smpte2084"))
    with pytest.raises(ExportMediaError) as hdr:
        validate_export_media(media, spec, full_decode=False)
    assert hdr.value.code == "hdr_unsupported"

    monkeypatch.setattr("scenery_brief_clips.yt.probe_export_coverage", lambda _p, window_ms=None, **kw: dict(probe, pix_fmt="yuv420p10le"))
    with pytest.raises(ExportMediaError) as depth:
        validate_export_media(media, spec, full_decode=False)
    assert depth.value.code == "bitdepth_unsupported"

    monkeypatch.setattr("scenery_brief_clips.yt.probe_export_coverage", lambda _p, window_ms=None, **kw: dict(probe, start_time_s=0.0))
    with pytest.raises(ExportMediaError) as mapping:
        validate_export_media(media, spec, full_decode=False)
    assert mapping.value.code == "timestamps_invalid"

    monkeypatch.setattr("scenery_brief_clips.yt.probe_export_coverage", lambda _p, window_ms=None, **kw: dict(probe, max_gap_s=0.5, window_max_gap_s=0.5))
    with pytest.raises(ExportMediaError) as gap:
        validate_export_media(media, spec, full_decode=False)
    assert gap.value.code == "timestamps_invalid"


def test_validate_export_media_tolerates_holes_outside_the_clip_window(tmp_path):
    holed = _holed_source(tmp_path)
    spec = _export_spec(clip_start_ms=1000, clip_end_ms=5000)  # the hole at 8.0s is in the margin

    info = validate_export_media(holed, spec, full_decode=False)

    assert info["window_n_frames"] == 120  # 4s at 30fps
    assert info["max_gap_s"] > 0.05  # the hole is still visible file-wide


def test_validate_export_media_rejects_holes_inside_the_clip_window(tmp_path):
    holed = _holed_source(tmp_path)
    spec = _export_spec()  # the clip window (7.0..13.0s) contains the hole at 8.0s

    with pytest.raises(ExportMediaError) as excinfo:
        validate_export_media(holed, spec, full_decode=False)

    assert excinfo.value.code == "timestamps_invalid"





def test_ytdlp_error_classification_transient_vs_fatal():
    from scenery_brief_clips.yt import ytdlp_error_is_transient

    assert ytdlp_error_is_transient("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests")
    assert ytdlp_error_is_transient("HTTPSConnectionPool: Read timed out.")
    assert not ytdlp_error_is_transient("ERROR: Private video. Sign in if you've been granted access")
    assert not ytdlp_error_is_transient("ERROR: Video unavailable")
    assert not ytdlp_error_is_transient("HTTP Error 404: Not Found")


def test_ytdlp_retries_transient_errors(tmp_path, monkeypatch):
    import subprocess

    import scenery_brief_clips.yt as ytmod
    from scenery_brief_clips.yt import YtDlp

    monkeypatch.setattr(ytmod, "_YTDLP_RETRY_SLEEP_S", (0, 0))
    calls = {"n": 0}

    def runner(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP Error 503: Service Unavailable")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"id":"abc123abc12"}\n', stderr="")

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    payload = client._run(["-J", "https://www.youtube.com/watch?v=abc123abc12"])
    assert calls["n"] == 3
    assert "abc123abc12" in payload


def test_ytdlp_does_not_retry_fatal_errors(tmp_path, monkeypatch):
    import subprocess

    import scenery_brief_clips.yt as ytmod
    from scenery_brief_clips.yt import YtDlp

    monkeypatch.setattr(ytmod, "_YTDLP_RETRY_SLEEP_S", (0, 0))
    calls = {"n": 0}

    def runner(cmd, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="ERROR: Private video. Sign in if you've been granted access"
        )

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    with pytest.raises(RuntimeError, match="Private video"):
        client._run(["-J", "https://www.youtube.com/watch?v=abc123abc12"])
    assert calls["n"] == 1


def test_ytdlp_retry_budget_is_small(tmp_path, monkeypatch):
    import subprocess

    import scenery_brief_clips.yt as ytmod
    from scenery_brief_clips.yt import YtDlp

    monkeypatch.setattr(ytmod, "_YTDLP_RETRY_SLEEP_S", (0, 0))
    calls = {"n": 0}

    def runner(cmd, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP Error 429: Too Many Requests")

    client = YtDlp(tmp_dir=tmp_path, allow_download=False, runner=runner)
    with pytest.raises(RuntimeError, match="429"):
        client._run(["-J", "https://www.youtube.com/watch?v=abc123abc12"])
    assert calls["n"] == ytmod._YTDLP_MAX_ATTEMPTS
