from __future__ import annotations

import fcntl
import json
import math
import os
import re
import subprocess
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
AnalysisValidator = Callable[[Path, tuple[float, float]], None]

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


def validate_analysis_media(path: Path, span: tuple[float, float] | None = None) -> None:
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        raise RuntimeError((probe.stderr or probe.stdout or "ffprobe failed").strip())
    try:
        payload = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON: {exc}") from exc
    streams = payload.get("streams") or []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not videos:
        raise RuntimeError("analysis media has no video stream")
    if audios:
        raise RuntimeError("analysis media must be video-only")
    width = max((int(stream.get("width") or 0) for stream in videos), default=0)
    height = max((int(stream.get("height") or 0) for stream in videos), default=0)
    if width <= 0 or height <= 0:
        raise RuntimeError("analysis media has invalid dimensions")
    if height > 720:
        raise RuntimeError(f"analysis media height {height} exceeds 720")
    duration_raw = (payload.get("format") or {}).get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("analysis media has no finite positive duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("analysis media has no finite positive duration")
    if span is not None:
        expected = float(span[1]) - float(span[0])
        if not math.isfinite(expected) or expected <= 0:
            raise RuntimeError("analysis span is invalid")
        if abs(duration - expected) > MEDIA_DURATION_TOLERANCE_S:
            raise RuntimeError(
                f"analysis media duration {duration:.3f}s does not match span {expected:.3f}s"
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
    "timed out",
    "timeout",
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
    ) -> None:
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.allow_download = allow_download
        self.binary = binary
        self.runner = runner or subprocess.run
        self.timeout = timeout
        self.analysis_validator = analysis_validator
        self.allow_export = allow_export
        self.export_cache_dir = Path(export_cache_dir) if export_cache_dir is not None else None

    def download(self, video_id: str) -> None:
        raise DownloadForbidden(
            "Full-quality download is disabled. "
            f"allow_download={self.allow_download}"
        )

    def analysis_args(self, video_id: str, dest: str | Path, span: tuple[float, float]) -> list[str]:
        start, end = canonical_span_seconds(span)
        return [
            "-f",
            ANALYSIS_FORMAT,
            "--download-sections",
            f"*{format_ts(start)}-{format_ts(end)}",
            "--force-keyframes-at-cuts",
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
        timeout: int = 300,
    ) -> Path:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        marker = analysis_marker_path(dest_path)
        with _exclusive_cache_lock(dest_path):
            _cleanup_abandoned_staging(dest_path)
            if _analysis_cache_valid(dest_path, video_id, span):
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
                self._run(self.analysis_args(video_id, stage, span), timeout=timeout)
                if not stage.is_file() or stage.stat().st_size <= 0:
                    raise RuntimeError(f"yt-dlp produced no analysis file at {stage}")
                self.analysis_validator(stage, span)
                start_ms, end_ms = canonical_span_ms(span)
                payload = {
                    "schema_version": ANALYSIS_MARKER_SCHEMA_VERSION,
                    "cache_policy": ANALYSIS_CACHE_POLICY,
                    "video_id": video_id,
                    "span_ms": [start_ms, end_ms],
                    "size_bytes": stage.stat().st_size,
                    "sha256": sha256_file(stage),
                }
                _write_json_fsync(marker_tmp, payload)
                os.replace(stage, dest_path)
                os.replace(marker_tmp, marker)
                stale_part = dest_path.with_suffix(dest_path.suffix + ".part")
                if stale_part.is_file():
                    stale_part.unlink()
                return dest_path
            finally:
                _cleanup_staging(stage)
                if marker_tmp.is_file():
                    marker_tmp.unlink()

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
            "--no-warnings",
            "--no-playlist",
            "-o",
            str(dest),
            watch_url(video_id),
        ]

    def fetch_export(self, spec, timeout: int = 900) -> Path:
        """Acquire one planned full-resolution section (stream copy with copyts).

        Only authorized clients (allow_export=True) may call this, and only
        with a spec built from an export plan; this method never falls back to
        another format or a wider span.
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
        env = os.environ.copy()
        env["TMPDIR"] = str(self.tmp_dir)
        cmd = [self.binary, "--ignore-config", "--js-runtimes", "node", *args]
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
            except Exception as exc:
                # Preserve runner timeouts / OS errors with their message.
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
