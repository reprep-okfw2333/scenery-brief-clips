from __future__ import annotations

from scenery_brief_clips.models import Candidate, Constraint, Reject

WATCH_URL = "https://www.youtube.com/watch?v={id}"
SHORT_MAX_S = 4 * 60
LONG_MIN_S = 90 * 60


def watch_url(video_id: str) -> str:
    return WATCH_URL.format(id=video_id)


def duration_hint(duration_s: float | None) -> str:
    if duration_s is None:
        return "unknown"
    if duration_s < SHORT_MAX_S:
        return "short"
    if duration_s > LONG_MIN_S:
        return "long"
    return "medium"


def _is_video_format(fmt: dict) -> bool:
    vcodec = fmt.get("vcodec") or "none"
    if vcodec == "none":
        return False
    height = fmt.get("height")
    width = fmt.get("width")
    return isinstance(height, (int, float)) and isinstance(width, (int, float)) and height > 0 and width > 0


def _height_formats(formats: list[dict], constraint: Constraint) -> list[dict]:
    eligible = []
    for fmt in formats:
        if not _is_video_format(fmt):
            continue
        if int(fmt["height"]) < constraint.min_height:
            continue
        eligible.append(fmt)
    return eligible


def _aspect_formats(formats: list[dict], constraint: Constraint) -> list[dict]:
    eligible = []
    for fmt in formats:
        width = int(fmt["width"])
        height = int(fmt["height"])
        aspect = width / height
        if constraint.aspect_min <= aspect <= constraint.aspect_max:
            eligible.append(fmt)
    return eligible


def _best_format(formats: list[dict], constraint: Constraint) -> dict | None:
    eligible = [fmt for fmt in formats if int(fmt["width"]) >= constraint.min_width]
    if not eligible:
        return None
    eligible.sort(key=lambda f: (int(f["height"]), int(f["width"])), reverse=True)
    return eligible[0]


def evaluate_metadata(info: dict, constraint: Constraint) -> Candidate | Reject:
    video_id = str(info.get("id") or "")
    title = info.get("title")
    if not video_id:
        return Reject(video_id="", title=title, reason="no_video_id")

    live_status = info.get("live_status") or "not_live"
    if live_status == "is_live":
        return Reject(video_id=video_id, title=title, reason="live")
    if live_status == "is_upcoming":
        return Reject(video_id=video_id, title=title, reason="upcoming")

    availability = info.get("availability") or "public"
    if availability in {"private", "premium_only", "subscriber_only", "needs_auth", "unavailable"}:
        return Reject(video_id=video_id, title=title, reason=availability if availability != "unavailable" else "unavailable")

    formats = info.get("formats") or []
    height_formats = _height_formats(formats, constraint)
    if not height_formats:
        return Reject(video_id=video_id, title=title, reason="no_min_resolution")
    aspect_formats = _aspect_formats(height_formats, constraint)
    if not aspect_formats:
        return Reject(video_id=video_id, title=title, reason="aspect")
    chosen = _best_format(aspect_formats, constraint)
    if chosen is None:
        return Reject(video_id=video_id, title=title, reason="no_min_resolution")

    width = int(chosen["width"])
    height = int(chosen["height"])
    aspect = width / height

    duration = info.get("duration")
    duration_s = float(duration) if isinstance(duration, (int, float)) else None
    fps_raw = chosen.get("fps")
    fps = float(fps_raw) if isinstance(fps_raw, (int, float)) else None

    return Candidate(
        video_id=video_id,
        title=str(title or ""),
        duration_s=duration_s,
        width=width,
        height=height,
        fps=fps,
        aspect=aspect,
        format_id=str(chosen.get("format_id") or ""),
        watch_url=watch_url(video_id),
        reason_kept=f"{width}x{height}",
        duration_hint=duration_hint(duration_s),
    )
