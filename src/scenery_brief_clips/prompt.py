from __future__ import annotations

import re

from scenery_brief_clips.models import Constraint

_ASPECT_RE = re.compile(r"\b16\s*[:/]\s*9\b", re.I)
_CLIPS_RE = re.compile(r"\b(\d+)\s+(?:(?:individual|short)\s+)*clips\b", re.I)
_OR_HIGHER_RE = re.compile(r"\bor\s+higher\b", re.I)

# First match wins. 1920p is treated as 1920x1080, not 1920-tall.
_RESOLUTION_RULES: tuple[tuple[str, int, int], ...] = (
    (r"\b1920\s*[x×]\s*1080\b", 1920, 1080),
    (r"\b1280\s*[x×]\s*720\b", 1280, 720),
    (r"\b3840\s*[x×]\s*2160\b", 3840, 2160),
    (r"\b2560\s*[x×]\s*1440\b", 2560, 1440),
    (r"\b2160p\b", 3840, 2160),
    (r"\b1440p\b", 2560, 1440),
    (r"\b1080p\b", 1920, 1080),
    (r"\b1920p\b", 1920, 1080),
    (r"\b720p\b", 1280, 720),
    (r"\b4k\b", 3840, 2160),
    (r"\buhd\b", 3840, 2160),
)

_STRIP_RES = [re.compile(pat, re.I) for pat, _, _ in _RESOLUTION_RULES]


def parse_prompt(text: str) -> Constraint:
    raw = text.strip()
    min_width, min_height = 1280, 720
    for pattern, width, height in _RESOLUTION_RULES:
        if re.search(pattern, raw, re.I):
            min_width, min_height = width, height
            break

    n_clips = 20
    clips_match = _CLIPS_RE.search(raw)
    if clips_match:
        n_clips = int(clips_match.group(1))

    theme = raw
    for compiled in _STRIP_RES:
        theme = compiled.sub(" ", theme)
    theme = _ASPECT_RE.sub(" ", theme)
    theme = _CLIPS_RE.sub(" ", theme)
    theme = _OR_HIGHER_RE.sub(" ", theme)
    theme = re.sub(r"[,:;|/]+", " ", theme)
    theme = re.sub(r"\s+", " ", theme).strip(" -")
    if clips_match and clips_match.start() == 0:
        theme = re.sub(r"^of\s+", "", theme, flags=re.I)

    geo = "none"
    if re.search(r"\beuropean\b|\beurope\b", theme, re.I):
        geo = "european"

    return Constraint(
        theme_text=theme,
        min_width=min_width,
        min_height=min_height,
        n_clips=n_clips,
        geo_requirement=geo,
        allow_download=False,
    )
