from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scenery_brief_clips.analysis_cache import (
    ANALYSIS_CACHE_POLICY,
    ANALYSIS_MARKER_SCHEMA_VERSION,
    EXPORT_CACHE_POLICY,
    EXPORT_MARKER_SCHEMA_VERSION,
    MEDIA_DURATION_TOLERANCE_S,
    analysis_lock_path,
    analysis_marker_path,
    canonical_span_ms,
    canonical_span_seconds,
    export_cache_path,
    export_marker_path,
    sha256_file,
)
from scenery_brief_clips.analysis_cache import (
    EXPORT_ASPECT_TOLERANCE,
    EXPORT_ASPECT_TARGET,
    EXPORT_FLOOR_HEIGHT,
)
from scenery_brief_clips.eligibility import watch_url

Runner = Callable[..., subprocess.CompletedProcess]
AnalysisValidator = Callable[[Path, tuple[float, float]], "dict | None"]


def group_kill_runner(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command as its own process group and kill the WHOLE group on timeout.

    The stock ``subprocess.run`` timeout kills only the direct child (yt-dlp),
    leaving its ffmpeg grandchildren downloading forever. Each abandoned
    ffmpeg then competes for bandwidth with the retry that replaced it, which
    is how a single hung span accumulates a pile of live downloaders on a
    small host. Starting the child in a fresh process group and killing that
    group (yt-dlp plus every descendant) on timeout keeps the retry budget
    honest: one attempt, one process tree, fully reaped.
    """
    timeout = kwargs.get("timeout")
    popen_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key not in {"timeout", "check", "capture_output"}
    }
    if kwargs.get("capture_output"):
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE
    popen_kwargs.setdefault("stdout", subprocess.PIPE)
    popen_kwargs.setdefault("stderr", subprocess.PIPE)
    popen_kwargs["start_new_session"] = True
    with subprocess.Popen(cmd, **popen_kwargs) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # SIGTERM the whole group, escalate to SIGKILL if it lingers.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()
            raise
        return subprocess.CompletedProcess(
            cmd,
            proc.returncode,
            stdout=out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out,
            stderr=err.decode("utf-8", errors="replace") if isinstance(err, bytes) else err,
        )

STAGING_SWEEP_AGE_S = 3600.0
EXPORT_MAX_FILESIZE_BYTES = 1_500_000_000
_EXPORT_FORMAT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class ExportSpec:
    """A run-authorized acquisition entry, built only from an export plan."""

    video_id: str
    format_id: str
    width: int
    height: int
    codec: str
    span: tuple[float, float]
    clip_start_ms: int
    clip_end_ms: int


ANALYSIS_FORMAT = "bv[height<=720][vcodec^=avc]/bv[height<=720][vcodec^=avc1]/bv[height<=720][ext=mp4]/bv[height<=720][vcodec!=none]/wv[height<=720][vcodec!=none]"


def format_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


class DownloadForbidden(RuntimeError):
    pass


class ExportMediaError(RuntimeError):
    """A full-resolution acquisition failed a policy check (carries a code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AnalysisSpanEmpty(RuntimeError):
    """The section download succeeded but holds no streams at all.

    Seen when a source's video track ends before its advertised duration
    (0I1hZCD7sT0: video stops at ~129 s of 193 s) and a span lies past it.
    The analyzer records such a span as unavailable instead of failing.
    """


# YouTube's anti-bot wall ("Sign in to confirm you're not a bot"). It refuses
# the host's IP for a while (hours); retrying at once only prolongs it.
_YOUTUBE_BLOCK_MARKERS = ("confirm you’re not a bot", "confirm you're not a bot")


def is_youtube_block(text: object) -> bool:
    """True when a yt-dlp error says YouTube is refusing this host (bot check)."""
    return any(marker in str(text) for marker in _YOUTUBE_BLOCK_MARKERS)


# Raised without contacting YouTube once it refused this process (the circuit
# breaker). Carries the block marker so callers classify it as a block.
YOUTUBE_BLOCKED_EARLIER = (
    "YouTube refused this host earlier in this process (confirm you're not a bot); "
    "not contacting it again until the next invocation"
)


@dataclass(frozen=True)
class YoutubeFallback:
    """Opt-in route used only after YouTube refuses the host.

    ``cookies_file``: a Netscape cookies.txt outside the project (ideally a
    throwaway account). ``proxy``: an http(s)/socks5 URL. Both come only from
    config (youtube_cookies_file / youtube_proxy); neither is ever printed.
    """

    cookies_file: Path | None = None
    proxy: str | None = None

    @property
    def configured(self) -> bool:
        return self.cookies_file is not None or self.proxy is not None


def youtube_fallback_from_config(root: str | Path, config: dict | None) -> YoutubeFallback:
    """Build the fallback from config; refuses a cookies file inside the project."""
    config = config or {}
    cookies = config.get("youtube_cookies_file")
    cookies_path = None
    if cookies:
        # A relative path is relative to the project root (where config.yaml
        # lives), so it is then refused below as inside the project.
        cookies_path = (Path(root) / os.path.expanduser(str(cookies))).resolve()
        if cookies_path.is_relative_to(Path(root).resolve()):
            raise ValueError(
                "youtube_cookies_file must live outside the project (it is a credential "
                "and must never be committed)"
            )
        if not cookies_path.is_file():
            raise ValueError("youtube_cookies_file does not exist or is not a file")
    return YoutubeFallback(cookies_file=cookies_path, proxy=config.get("youtube_proxy") or None)


def analysis_timeout_s(span: tuple[float, float]) -> int:
    """Hard timeout for one analysis span, scaled to its length.

    A healthy 12-14 s span took ~110-130 s on a 1-CPU host with two workers
    (re-encode at cuts included); a flat 300 s let a stalled 3 s span burn
    5 minutes per attempt. 90 s + 10 s per source second, capped at 300 s.
    """
    start, end = span
    return int(min(300, 90 + 10 * max(0.0, float(end) - float(start))))


def validate_analysis_media(path: Path, span: tuple[float, float] | None = None) -> dict:
    """Accept one stream-copied, copyts analysis section and return its mapping.

    The section starts at the keyframe at or before the span start, so local
    time 0 is the first packet PTS (K), not the span start. That mapping is
    only trusted when the container start_time equals K (the same proof export
    uses). The section must cover the span to within the duration tolerance;
    excerpts are later clamped to the span. One strict full decode follows.
    """
    try:
        info = probe_export_coverage(Path(path))
    except ExportMediaError as exc:
        if exc.code == "no_streams":
            raise AnalysisSpanEmpty(f"analysis media {exc.code}: {exc}") from exc
        raise RuntimeError(f"analysis media {exc.code}: {exc}") from exc
    width, height = int(info["width"]), int(info["height"])
    if width <= 0 or height <= 0:
        raise RuntimeError("analysis media has invalid dimensions")
    if height > 720:
        raise RuntimeError(f"analysis media height {height} exceeds 720")
    first_pts = float(info["first_pts_s"])
    last_end = float(info["last_end_s"])
    start_time = info.get("start_time_s")
    if start_time is None or abs(float(start_time) - first_pts) > 1e-3:
        raise RuntimeError(
            f"analysis media start_time {start_time} does not match first packet PTS {first_pts} "
            "(timeline mapping unproven)"
        )
    if not (math.isfinite(first_pts) and math.isfinite(last_end)) or last_end <= first_pts:
        raise RuntimeError("analysis media has no finite positive timeline")
    if span is not None:
        span_start, span_end = float(span[0]), float(span[1])
        if not (math.isfinite(span_start) and math.isfinite(span_end)) or span_end <= span_start:
            raise RuntimeError("analysis span is invalid")
        if first_pts > span_start + MEDIA_DURATION_TOLERANCE_S or last_end < span_end - MEDIA_DURATION_TOLERANCE_S:
            raise RuntimeError(
                f"analysis media covers {first_pts:.3f}-{last_end:.3f}s, "
                f"not span {span_start:.3f}-{span_end:.3f}s"
            )

    decode = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if decode.returncode != 0:
        raise RuntimeError((decode.stderr or decode.stdout or "ffmpeg decode failed").strip())
    return {
        "first_pts_ms": int(round(first_pts * 1000)),
        "last_end_ms": int(round(last_end * 1000)),
        "width": width,
        "height": height,
    }


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_fps(value) -> tuple[int, int]:
    text = str(value or "")
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            return int(num), int(den)
        except ValueError:
            return 0, 0
    try:
        return int(float(text) * 1000), 1000
    except (TypeError, ValueError):
        return 0, 0


def expected_frames(start_s: float, end_s: float, fps_num: int, fps_den: int) -> int:
    """Frames kept by a half-open [start, end) trim on an fps frame grid."""
    if end_s <= start_s or fps_num <= 0 or fps_den <= 0:
        return 0
    fps = fps_num / fps_den
    first_index = math.ceil(start_s * fps - 1e-6)
    last_index = math.ceil(end_s * fps - 1e-6) - 1
    return max(0, last_index - first_index + 1)


def probe_export_coverage(path: Path, window_ms: tuple[int, int] | None = None) -> dict:
    """Cheap structural + packet-timeline probe of an acquisition (no decode).

    The packet timeline is the mapping evidence: ``first_pts_s`` is the source
    time of the file's local 0 (the copyts contract), and it must equal the
    container's ``format.start_time`` -- validate_export_media enforces that.

    With ``window_ms`` (the clip's source interval), the probe also reports
    ``window_n_frames`` / ``window_max_gap_s`` for the DELIVERED interval, so
    the acceptance checks can ignore fragment-boundary holes in the margins.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        raise ExportMediaError("probe_failed", (probe.stderr or probe.stdout or "ffprobe failed").strip())
    try:
        payload = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise ExportMediaError("probe_failed", f"ffprobe returned invalid JSON: {exc}") from exc
    streams = payload.get("streams") or []
    if not streams:
        raise ExportMediaError("no_streams", "acquisition contains no streams (no media at this time in the source)")
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    others = [stream for stream in streams if stream.get("codec_type") != "video"]
    if len(videos) != 1 or others:
        raise ExportMediaError(
            "streams_invalid",
            f"acquisition must be exactly one video stream (video={len(videos)}, other={len(others)})",
        )
    video = videos[0]
    fps_num, fps_den = _parse_fps(video.get("r_frame_rate"))
    rotation = 0
    for side_data in video.get("side_data_list") or []:
        if "rotation" in side_data:
            rotation = abs(int(float(side_data["rotation"])))
    tags = video.get("tags") or {}
    if "rotate" in tags:
        try:
            rotation = rotation or abs(int(float(tags["rotate"])))
        except (TypeError, ValueError):
            pass
    start_time_raw = (payload.get("format") or {}).get("start_time")
    start_time_s = None
    if start_time_raw not in (None, "N/A"):
        try:
            start_time_s = float(start_time_raw)
        except (TypeError, ValueError):
            start_time_s = None

    packets = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if packets.returncode != 0:
        raise ExportMediaError("probe_failed", (packets.stderr or "ffprobe packets failed").strip())
    entries = (json.loads(packets.stdout) or {}).get("packets") or []
    timeline: list[tuple[float, float]] = []
    for entry in entries:
        raw = entry.get("pts_time")
        if raw in (None, "N/A"):
            continue
        try:
            pts = float(raw)
        except (TypeError, ValueError):
            continue
        duration = 0.0
        dur_raw = entry.get("duration_time")
        if dur_raw not in (None, "N/A"):
            try:
                duration = float(dur_raw)
            except (TypeError, ValueError):
                duration = 0.0
        timeline.append((pts, duration))
    if not timeline:
        raise ExportMediaError("timestamps_invalid", "acquisition has no packet timestamps")
    timeline.sort(key=lambda item: item[0])
    pts_list = [pts for pts, _ in timeline]
    if len(set(pts_list)) != len(pts_list):
        raise ExportMediaError("timestamps_invalid", "acquisition has duplicate packet timestamps")
    last_pts, last_duration = timeline[-1]
    if last_duration <= 0:
        last_duration = (fps_den / fps_num) if fps_num else 1.0 / 30.0
    gaps = [right - left for left, right in zip(pts_list, pts_list[1:])]
    info = {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "codec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "sar": video.get("sample_aspect_ratio"),
        "color_space": video.get("color_space"),
        "color_transfer": video.get("color_transfer"),
        "color_primaries": video.get("color_primaries"),
        "rotation": rotation,
        "fps_num": fps_num,
        "fps_den": fps_den,
        "start_time_s": start_time_s,
        "first_pts_s": pts_list[0],
        "last_end_s": last_pts + last_duration,
        "n_frames": len(pts_list),
        "max_gap_s": max(gaps) if gaps else 0.0,
    }
    if window_ms is not None:
        window_lo_s, window_hi_s = window_ms[0] / 1000.0, window_ms[1] / 1000.0
        # Packet PTS must retain its sub-millisecond precision here. Rounding
        # 33.032833s to 33.033s would drop a frame inside a window ending at 33.033s.
        in_window = [pts for pts in pts_list if window_lo_s <= pts < window_hi_s]
        window_gaps = [right - left for left, right in zip(in_window, in_window[1:])]
        info["window_n_frames"] = len(in_window)
        info["window_max_gap_s"] = max(window_gaps) if window_gaps else 0.0
    return info



def _spec_field(spec, name):
    if isinstance(spec, dict):
        return spec.get(name)
    return getattr(spec, name, None)


def validate_export_media(path: Path, spec, full_decode: bool = True) -> dict:
    """Acquisition acceptance: dimensions equality, SDR/8-bit, square pixels,
    no rotation, mapping proof (start_time == first PTS), sane frame timing,
    and a clean decode.

    Frame uniformity (count + max gap) is enforced on the CLIP WINDOW -- the
    delivered interval -- not the whole section: --download-sections can leave
    fragment-boundary holes in the acquisition margins, and those never reach
    the interior trim.
    """
    try:
        window_lo = int(str(_spec_field(spec, "clip_start_ms")))
        window_hi = int(str(_spec_field(spec, "clip_end_ms")))
    except (TypeError, ValueError) as exc:
        raise ExportMediaError("spec_invalid", "export spec has invalid clip interval") from exc
    info = probe_export_coverage(path, window_ms=(window_lo, window_hi))
    try:
        expected = (int(str(_spec_field(spec, "width"))), int(str(_spec_field(spec, "height"))))
    except (TypeError, ValueError) as exc:
        raise ExportMediaError("spec_invalid", "export spec has invalid dimensions") from exc
    actual = (info["width"], info["height"])
    if actual != expected:
        raise ExportMediaError(
            "dimension_mismatch", f"acquisition is {actual[0]}x{actual[1]}, planned {expected[0]}x{expected[1]}"
        )
    if info["sar"] not in (None, "", "N/A", "1:1"):
        raise ExportMediaError("non_square_pixels", f"sample aspect ratio is {info['sar']}")
    if info["rotation"]:
        raise ExportMediaError("rotation_unsupported", f"rotation {info['rotation']} present")
    if info["color_transfer"] in ("smpte2084", "arib-std-b67"):
        raise ExportMediaError("hdr_unsupported", f"HDR transfer {info['color_transfer']}")
    pix_fmt = str(info["pix_fmt"] or "")
    if pix_fmt not in ("yuv420p", "yuvj420p"):
        raise ExportMediaError(
            "bitdepth_unsupported", f"pixel format {pix_fmt!r} is not an accepted 8-bit format"
        )
    start_time = info.get("start_time_s")
    if start_time is None or abs(float(start_time) - float(info["first_pts_s"])) > 1e-3:
        raise ExportMediaError(
            "timestamps_invalid",
            f"format start_time {start_time} does not match first packet PTS {info['first_pts_s']} "
            "(timeline mapping unproven)",
        )
    if info["fps_num"] <= 0 or info["fps_den"] <= 0:
        raise ExportMediaError("timestamps_invalid", "acquisition has no usable frame rate")
    frame_s = info["fps_den"] / info["fps_num"]
    window_n = info.get("window_n_frames")
    window_gap = info.get("window_max_gap_s")
    if window_n is None or window_gap is None:
        raise ExportMediaError(
            "timestamps_invalid", "acquisition probe lacks clip-window metrics"
        )
    k_ms = int(round(float(info["first_pts_s"]) * 1000))
    expected = expected_frames(
        (window_lo - k_ms) / 1000.0,
        (window_hi - k_ms) / 1000.0,
        info["fps_num"],
        info["fps_den"],
    )
    if int(window_n) != expected:
        raise ExportMediaError(
            "timestamps_invalid",
            f"acquisition has {window_n} frames in the clip window, expected {expected}",
        )
    if float(window_gap) > frame_s + 1e-3:
        raise ExportMediaError(
            "timestamps_invalid",
            f"frame gap {float(window_gap):.3f}s inside the clip window exceeds "
            f"the frame interval {frame_s:.3f}s",
        )
    if full_decode:
        decode = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if decode.returncode != 0:
            raise ExportMediaError("decode_failed", (decode.stderr or decode.stdout or "decode failed").strip())
    return info


def _analysis_cache_valid(dest: Path, video_id: str, span: tuple[float, float]) -> bool:
    marker = analysis_marker_path(dest)
    if not dest.is_file() or not marker.is_file() or dest.stat().st_size <= 0:
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        expected_span_ms = list(canonical_span_ms(span))
        expected_size = int(payload.get("size_bytes"))
        expected_hash = str(payload.get("sha256") or "")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if (
        payload.get("schema_version") != ANALYSIS_MARKER_SCHEMA_VERSION
        or payload.get("cache_policy") != ANALYSIS_CACHE_POLICY
        or payload.get("video_id") != video_id
        or payload.get("span_ms") != expected_span_ms
        or expected_size != dest.stat().st_size
        or len(expected_hash) != 64
    ):
        return False
    try:
        return sha256_file(dest) == expected_hash
    except OSError:
        return False


def _marker_proves_validation(marker: Path) -> bool:
    """True when the marker was written by the v4 validator (it records the
    proven timeline mapping); hand-written or legacy markers are re-validated."""
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    value = payload.get("first_pts_ms") if isinstance(payload, dict) else None
    return isinstance(value, int) and not isinstance(value, bool)


def _export_cache_valid(dest: Path, video_id: str, span: tuple[float, float], spec, policy: str) -> bool:
    marker = export_marker_path(dest)
    if not dest.is_file() or not marker.is_file() or dest.stat().st_size <= 0:
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        expected_span_ms = list(canonical_span_ms(span))
        expected_size = int(payload.get("size_bytes"))
        expected_hash = str(payload.get("sha256") or "")
        expected_width = int(str(_spec_field(spec, "width")))
        expected_height = int(str(_spec_field(spec, "height")))
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False
    if (
        payload.get("schema_version") != EXPORT_MARKER_SCHEMA_VERSION
        or payload.get("cache_policy") != policy
        or payload.get("video_id") != video_id
        or payload.get("span_ms") != expected_span_ms
        or str(payload.get("format_id")) != str(_spec_field(spec, "format_id"))
        or int(payload.get("width") or 0) != expected_width
        or int(payload.get("height") or 0) != expected_height
        or expected_size != dest.stat().st_size
        or len(expected_hash) != 64
    ):
        return False
    try:
        return sha256_file(dest) == expected_hash
    except OSError:
        return False


@contextmanager
def _exclusive_cache_lock(dest: Path) -> Iterator[None]:
    lock_path = analysis_lock_path(dest)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _cleanup_staging(path: Path) -> None:
    for candidate in path.parent.glob(path.name + "*"):
        if candidate.is_file():
            candidate.unlink()


def _cleanup_abandoned_staging(dest: Path) -> None:
    patterns = (
        f"{dest.stem}.staging-*",
        f"{analysis_marker_path(dest).name}.staging-*",
    )
    for pattern in patterns:
        for candidate in dest.parent.glob(pattern):
            if candidate.is_file():
                candidate.unlink()
    cutoff = time.time() - STAGING_SWEEP_AGE_S
    for candidate in dest.parent.glob("*.staging-*"):
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            continue


def _remove_cached_analysis(dest_path: Path) -> None:
    for path in (dest_path, analysis_marker_path(dest_path)):
        if path.is_file():
            path.unlink()


def _write_json_fsync(path: Path, payload: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())



# Bounded yt-dlp retries: only obviously transient transport/rate-limit failures.
# Fatal extractor/auth/geo errors fail immediately. Total attempts stay small.
_YTDLP_MAX_ATTEMPTS = 3
_YTDLP_RETRY_SLEEP_S = (0.4, 1.0)

_YTDLP_FATAL_MARKERS = (
    "sign in to confirm",
    "confirm your age",
    "private video",
    "video unavailable",
    "this video is not available",
    "has been removed",
    "account associated with this video has been terminated",
    "not made this video available in your country",
    "requested format is not available",
    "no video formats",
    "unsupported url",
    "is not a valid url",
    "http error 404",
    "http error 403",
    "http error 401",
)

_YTDLP_TRANSIENT_MARKERS = (
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "temporarily unavailable",
    "temporary failure",
    "connection reset",
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "name resolution",
    "ssl: unexpected eof",
    "eof occurred in violation",
    "fragment not found",
    "unable to download api page",
    "unable to download webpage",
    "read error",
    "server returned 5",
)


def ytdlp_error_is_transient(message: str) -> bool:
    """Classify yt-dlp stderr for a small retry budget (no cookies required)."""
    text = (message or "").lower()
    if not text:
        return False
    if any(marker in text for marker in _YTDLP_FATAL_MARKERS):
        return False
    return any(marker in text for marker in _YTDLP_TRANSIENT_MARKERS)


class YtDlp:
    def __init__(
        self,
        tmp_dir: str | Path,
        allow_download: bool = False,
        binary: str = "yt-dlp",
        runner: Runner | None = None,
        timeout: int = 60,
        analysis_validator: AnalysisValidator = validate_analysis_media,
        allow_export: bool = False,
        export_cache_dir: str | Path | None = None,
        fallback: YoutubeFallback | None = None,
    ) -> None:
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.allow_download = allow_download
        self.binary = binary
        self.runner = runner or group_kill_runner
        self.timeout = timeout
        self.analysis_validator = analysis_validator
        self.allow_export = allow_export
        self.export_cache_dir = Path(export_cache_dir) if export_cache_dir is not None else None
        self.fallback = fallback or YoutubeFallback()
        # Block state is shared by the analysis worker threads.
        self._block_lock = threading.Lock()
        self._blocked = False
        self._fallback_active = False
        # True once any call went through the fallback (run provenance).
        self.fallback_used = False

    def download(self, video_id: str) -> None:
        raise DownloadForbidden(
            "Full-quality download is disabled. "
            f"allow_download={self.allow_download}"
        )

    def analysis_args(
        self,
        video_id: str,
        dest: str | Path,
        span: tuple[float, float] | list[tuple[float, float]],
        format_id: str | None = None,
    ) -> list[str]:
        """Stream-copy sections with source timestamps (-copyts), never re-encoded.

        ``span`` may be a list: one yt-dlp call (one player/JS extraction) then
        writes one file per section. ``format_id`` pins the export rendition so
        the same file can later serve export; otherwise the <=720p selector.
        """
        spans = span if isinstance(span, list) else [span]
        sections: list[str] = []
        for item in spans:
            start, end = canonical_span_seconds(item)
            sections += ["--download-sections", f"*{format_ts(start)}-{format_ts(end)}"]
        if format_id is not None and not _EXPORT_FORMAT_ID_RE.fullmatch(str(format_id)):
            raise ValueError(f"unsafe analysis format id: {format_id!r}")
        return [
            "-f",
            str(format_id) if format_id is not None else ANALYSIS_FORMAT,
            *sections,
            "--downloader-args",
            "ffmpeg:-copyts",
            # Bounded, fast-failing download: the outer retry budget (small,
            # transient-only) is the only retry loop. Without these flags
            # yt-dlp retries 10 times per attempt with a 30s socket timeout,
            # which multiplies one hung span into many.
            "--retries",
            "1",
            "--fragment-retries",
            "1",
            "--socket-timeout",
            "15",
            "--no-warnings",
            "--no-playlist",
            "-o",
            str(dest),
            watch_url(video_id),
        ]

    def fetch_analysis(
        self,
        video_id: str,
        dest: str | Path,
        span: tuple[float, float],
        timeout: int | None = None,
        format_id: str | None = None,
    ) -> Path:
        if timeout is None:
            timeout = analysis_timeout_s(span)
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        marker = analysis_marker_path(dest_path)
        with _exclusive_cache_lock(dest_path):
            _cleanup_abandoned_staging(dest_path)
            if _analysis_cache_valid(dest_path, video_id, span):
                if _marker_proves_validation(marker):
                    # These exact bytes (hash just re-checked) already passed
                    # the full validation, decode included, before the marker
                    # was written; verify decodes them again independently.
                    return dest_path
                try:
                    self.analysis_validator(dest_path, span)
                except Exception:
                    # A previously published entry can be stale or truncated
                    # (for example media shorter than the span). Drop it so this
                    # fetch re-downloads instead of reusing it forever.
                    _remove_cached_analysis(dest_path)
                else:
                    return dest_path

            token = uuid.uuid4().hex
            stage = dest_path.with_name(f"{dest_path.stem}.staging-{token}{dest_path.suffix}")
            marker_tmp = marker.with_name(f"{marker.name}.staging-{token}")
            try:
                self._run(self.analysis_args(video_id, stage, span, format_id), timeout=timeout)
                return self._publish_analysis(stage, dest_path, marker_tmp, video_id, span, format_id)
            finally:
                _cleanup_staging(stage)
                if marker_tmp.is_file():
                    marker_tmp.unlink()

    def _publish_analysis(
        self,
        stage: Path,
        dest_path: Path,
        marker_tmp: Path,
        video_id: str,
        span: tuple[float, float],
        format_id: str | None,
    ) -> Path:
        """Validate a staged section and publish it with its marker (caller holds the lock)."""
        if not stage.is_file() or stage.stat().st_size <= 0:
            raise RuntimeError(f"yt-dlp produced no analysis file at {stage}")
        info = self.analysis_validator(stage, span)
        start_ms, end_ms = canonical_span_ms(span)
        payload = {
            "schema_version": ANALYSIS_MARKER_SCHEMA_VERSION,
            "cache_policy": ANALYSIS_CACHE_POLICY,
            "video_id": video_id,
            "span_ms": [start_ms, end_ms],
            "size_bytes": stage.stat().st_size,
            "sha256": sha256_file(stage),
            "format_id": format_id,
        }
        if isinstance(info, dict):
            for key in ("first_pts_ms", "last_end_ms", "width", "height"):
                payload[key] = info.get(key)
        _write_json_fsync(marker_tmp, payload)
        os.replace(stage, dest_path)
        os.replace(marker_tmp, analysis_marker_path(dest_path))
        stale_part = dest_path.with_suffix(dest_path.suffix + ".part")
        if stale_part.is_file():
            stale_part.unlink()
        return dest_path

    def prefetch_analysis(
        self,
        video_id: str,
        items: list[tuple[str | Path, tuple[float, float]]],
        format_id: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, str]:
        """Acquire several spans of one video with ONE yt-dlp call.

        Each section is validated and published exactly as fetch_analysis
        would, under that span's own lock. Spans already cached are skipped.
        Returns {dest: error} for spans that were not published; callers then
        use fetch_analysis for those, so a batch failure never loses a span.
        """
        pending = [
            (Path(dest), canonical_span_seconds(span))
            for dest, span in items
            if not _analysis_cache_valid(Path(dest), video_id, span)
        ]
        if len(pending) < 2:
            return {}
        spans = [span for _dest, span in pending]
        if timeout is None:
            timeout = min(900, sum(analysis_timeout_s(span) for span in spans))
        batch_dir = pending[0][0].parent / f".batch.staging-{uuid.uuid4().hex}"
        batch_dir.mkdir(parents=True)
        errors: dict[str, str] = {}
        run_error = None
        try:
            try:
                self._run(
                    self.analysis_args(
                        video_id,
                        batch_dir / "%(section_start)s_%(section_end)s.mp4",
                        spans,
                        format_id,
                    ),
                    timeout=timeout,
                )
            except Exception as exc:  # sections finished before the failure are still usable
                run_error = str(exc)
            produced: dict[tuple[int, int], Path] = {}
            for path in batch_dir.glob("*.mp4"):
                try:
                    lo, hi = path.stem.split("_", 1)
                    produced[(round(float(lo) * 1000), round(float(hi) * 1000))] = path
                except ValueError:
                    continue
            for dest, span in pending:
                staged = produced.get(canonical_span_ms(span))
                if staged is None:
                    errors[str(dest)] = run_error or "batch produced no file for this span"
                    continue
                with _exclusive_cache_lock(dest):
                    _cleanup_abandoned_staging(dest)
                    if _analysis_cache_valid(dest, video_id, span):
                        continue
                    token = uuid.uuid4().hex
                    stage = dest.with_name(f"{dest.stem}.staging-{token}{dest.suffix}")
                    marker_tmp = analysis_marker_path(dest).with_name(
                        f"{analysis_marker_path(dest).name}.staging-{token}"
                    )
                    try:
                        os.replace(staged, stage)
                        self._publish_analysis(stage, dest, marker_tmp, video_id, span, format_id)
                    except Exception as exc:
                        errors[str(dest)] = str(exc)
                    finally:
                        stage.unlink(missing_ok=True)
                        marker_tmp.unlink(missing_ok=True)
        finally:
            shutil.rmtree(batch_dir, ignore_errors=True)
        return errors

    def invalidate_analysis(self, dest: str | Path) -> None:
        dest_path = Path(dest)
        with _exclusive_cache_lock(dest_path):
            _remove_cached_analysis(dest_path)

    def export_args(self, video_id: str, dest: str | Path, span: tuple[float, float], spec) -> list[str]:
        start, end = canonical_span_seconds(span)
        return [
            "-f",
            str(_spec_field(spec, "format_id")),
            "--download-sections",
            f"*{format_ts(start)}-{format_ts(end)}",
            "--downloader-args",
            "ffmpeg:-copyts",
            "--max-filesize",
            str(EXPORT_MAX_FILESIZE_BYTES),
            # Same bounded, fast-failing download policy as analysis: one
            # internal attempt, short socket timeout; the outer small
            # transient-only budget is the only retry loop.
            "--retries",
            "1",
            "--fragment-retries",
            "1",
            "--socket-timeout",
            "15",
            "--no-warnings",
            "--no-playlist",
            "-o",
            str(dest),
            watch_url(video_id),
        ]

    def fetch_export(self, spec, timeout: int = 900, source: str | Path | None = None) -> Path:
        """Acquire one planned full-resolution section (stream copy with copyts).

        Only authorized clients (allow_export=True) may call this, and only
        with a spec built from an export plan; this method never falls back to
        another format or a wider span.

        With ``source`` (an analysis copy the export plan chose to adopt: same
        rendition, same copyts stream copy), that file is hard-linked into
        staging instead of downloaded; it then passes exactly the same
        acceptance (full decode included) and marker as a download.
        """
        if not self.allow_export:
            raise DownloadForbidden(
                "Export acquisition is not authorized on this client (allow_export=False)."
            )
        video_id = str(_spec_field(spec, "video_id") or "")
        format_id = str(_spec_field(spec, "format_id") or "")
        if not video_id:
            raise ExportMediaError("spec_invalid", "export spec has no video_id")
        if not _EXPORT_FORMAT_ID_RE.fullmatch(format_id):
            raise ExportMediaError("spec_invalid", f"unsafe or empty format id: {format_id!r}")
        width = _spec_field(spec, "width")
        height = _spec_field(spec, "height")
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise ExportMediaError("spec_invalid", "export spec has invalid dimensions")
        if height < EXPORT_FLOOR_HEIGHT:
            raise ExportMediaError(
                "spec_invalid",
                f"export spec height {height} is below the {EXPORT_FLOOR_HEIGHT}p floor",
            )
        if abs(width / height - EXPORT_ASPECT_TARGET) > EXPORT_ASPECT_TOLERANCE:
            raise ExportMediaError(
                "spec_invalid", f"export spec aspect {width}x{height} is not 16:9"
            )
        try:
            span = canonical_span_seconds(_spec_field(spec, "span"))
        except (TypeError, ValueError) as exc:
            raise ExportMediaError("spec_invalid", f"invalid acquisition span: {exc}") from exc
        if self.export_cache_dir is None:
            raise ExportMediaError("spec_invalid", "export cache dir is not configured")
        policy = f"{EXPORT_CACHE_POLICY}.{format_id}"
        dest_path = export_cache_path(self.export_cache_dir, video_id, span, policy=policy)
        marker = export_marker_path(dest_path)
        with _exclusive_cache_lock(dest_path):
            _cleanup_abandoned_staging(dest_path)
            if _export_cache_valid(dest_path, video_id, span, spec, policy):
                try:
                    validate_export_media(dest_path, spec, full_decode=False)
                except (ExportMediaError, subprocess.TimeoutExpired):
                    validate_ok = False
                else:
                    validate_ok = True
                if validate_ok:
                    return dest_path
                _remove_cached_analysis(dest_path)

            token = uuid.uuid4().hex
            stage = dest_path.with_name(f"{dest_path.stem}.staging-{token}{dest_path.suffix}")
            marker_tmp = marker.with_name(f"{marker.name}.staging-{token}")
            try:
                if source is not None:
                    source_path = Path(source)
                    try:
                        os.link(source_path, stage)
                    except OSError:
                        shutil.copyfile(source_path, stage)
                else:
                    self._run(self.export_args(video_id, stage, span, spec), timeout=timeout)
                if not stage.is_file() or stage.stat().st_size <= 0:
                    raise RuntimeError(f"yt-dlp produced no export file at {stage}")
                if stage.stat().st_size > EXPORT_MAX_FILESIZE_BYTES:
                    raise ExportMediaError(
                        "size_exceeded",
                        f"acquisition is {stage.stat().st_size} bytes, over the {EXPORT_MAX_FILESIZE_BYTES}-byte bound",
                    )
                info = validate_export_media(stage, spec, full_decode=True)
                start_ms, end_ms = canonical_span_ms(span)
                payload = {
                    "schema_version": EXPORT_MARKER_SCHEMA_VERSION,
                    "cache_policy": policy,
                    "video_id": video_id,
                    "span_ms": [start_ms, end_ms],
                    "format_id": format_id,
                    "width": info["width"],
                    "height": info["height"],
                    "codec": info["codec"],
                    "pix_fmt": info["pix_fmt"],
                    "fps_num": info["fps_num"],
                    "fps_den": info["fps_den"],
                    "start_time_ms": round(float(info["start_time_s"]) * 1000),
                    "first_pts_ms": round(info["first_pts_s"] * 1000),
                    "last_end_ms": round(info["last_end_s"] * 1000),
                    "n_frames": info["n_frames"],
                    "size_bytes": stage.stat().st_size,
                    "sha256": sha256_file(stage),
                }
                if source is not None:
                    payload["adopted_from"] = {"analysis_copy": Path(source).name}
                _write_json_fsync(marker_tmp, payload)
                _fsync_file(stage)
                os.replace(stage, dest_path)
                _fsync_dir(dest_path.parent)
                os.replace(marker_tmp, marker)
                stale_part = dest_path.with_suffix(dest_path.suffix + ".part")
                if stale_part.is_file():
                    stale_part.unlink()
                return dest_path
            finally:
                _cleanup_staging(stage)
                if marker_tmp.is_file():
                    marker_tmp.unlink()

    def fetch_metadata(self, video_id: str) -> dict:
        stdout = self._run(
            [
                "-j",
                "--skip-download",
                "--no-warnings",
                "--no-playlist",
                watch_url(video_id),
            ]
        )
        rows = _parse_ndjson(stdout)
        if not rows:
            raise RuntimeError(f"yt-dlp returned no JSON for {video_id}")
        return rows[-1]

    def search(self, query: str, limit: int) -> list[dict]:
        if limit < 1:
            return []
        stdout = self._run(
            [
                "-j",
                "--flat-playlist",
                "--skip-download",
                "--no-warnings",
                f"ytsearch{limit}:{query}",
            ]
        )
        return _parse_ndjson(stdout)

    def _run(self, args: list[str], timeout: int | None = None) -> str:
        """Run yt-dlp with the YouTube block handling.

        After a bot-check refusal the call is retried once through the opt-in
        fallback (if configured) and later calls keep using it; without a
        fallback, or if the fallback is refused too, every later call in this
        process fails at once with YOUTUBE_BLOCKED_EARLIER (circuit breaker),
        because each refused request can prolong the block.
        """
        with self._block_lock:
            blocked, use_fallback = self._blocked, self._fallback_active
        if blocked:
            raise RuntimeError(YOUTUBE_BLOCKED_EARLIER)
        try:
            return self._run_attempts(args, timeout, use_fallback)
        except RuntimeError as exc:
            if not is_youtube_block(exc):
                raise
            with self._block_lock:
                retry = not use_fallback and self.fallback.configured and not self._blocked
                if retry:
                    self._fallback_active = True
                else:
                    self._blocked = True
            if not retry:
                raise
        try:
            return self._run_attempts(args, timeout, True)
        except RuntimeError as exc:
            if is_youtube_block(exc):
                with self._block_lock:
                    self._blocked = True
            raise

    def _run_attempts(self, args: list[str], timeout: int | None, use_fallback: bool) -> str:
        if not use_fallback:
            return self._run_plain(args, timeout, [])
        extra: list[str] = []
        if self.fallback.proxy:
            extra += ["--proxy", self.fallback.proxy]
        copy = None
        try:
            if self.fallback.cookies_file is not None:
                # yt-dlp writes the jar back to --cookies; concurrent workers
                # each get a private copy so the owner's file is never rewritten.
                copy = self.tmp_dir / f".cookies-{uuid.uuid4().hex}.txt"
                fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(self.fallback.cookies_file.read_bytes())
                extra += ["--cookies", str(copy)]
            failure = None
            try:
                result = self._run_plain(args, timeout, extra)
            except RuntimeError as exc:
                failure = str(exc)
            if failure is not None:
                # A timeout message quotes the command line; keep the proxy
                # (it may hold credentials) and the cookie path out of run
                # files. Raised outside the except block so the original
                # exception is not attached as __context__.
                for secret in (self.fallback.proxy, str(copy) if copy else None):
                    if secret:
                        failure = failure.replace(secret, "<redacted>")
                raise RuntimeError(failure)
        finally:
            if copy is not None:
                copy.unlink(missing_ok=True)
        with self._block_lock:
            self.fallback_used = True
        return result

    def _run_plain(self, args: list[str], timeout: int | None, extra: list[str]) -> str:
        env = os.environ.copy()
        env["TMPDIR"] = str(self.tmp_dir)
        cmd = [self.binary, "--ignore-config", "--js-runtimes", "node", *extra, *args]
        timeout_s = self.timeout if timeout is None else timeout
        errors: list[str] = []
        for attempt in range(1, _YTDLP_MAX_ATTEMPTS + 1):
            try:
                result = self.runner(
                    cmd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                # The default runner kills only the yt-dlp parent; its ffmpeg
                # children survive and pile up. Re-raise without retrying: a
                # timed-out media fetch is a failure, not a transient error.
                err = f"download timed out after {timeout_s}s: {exc}"
                raise RuntimeError(err) from exc
            except Exception as exc:
                # Preserve runner OS errors with their message.
                err = str(exc).strip() or exc.__class__.__name__
                errors.append(err)
                if attempt >= _YTDLP_MAX_ATTEMPTS or not ytdlp_error_is_transient(err):
                    raise RuntimeError(err) from exc
            else:
                if result.returncode == 0:
                    return result.stdout or ""
                err = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
                errors.append(err)
                if attempt >= _YTDLP_MAX_ATTEMPTS or not ytdlp_error_is_transient(err):
                    # Always surface stderr (or stdout fallback) to the caller.
                    raise RuntimeError(err)
            sleep_s = _YTDLP_RETRY_SLEEP_S[min(attempt - 1, len(_YTDLP_RETRY_SLEEP_S) - 1)]
            time.sleep(sleep_s)
        raise RuntimeError(errors[-1] if errors else "yt-dlp failed")


def _parse_ndjson(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows
