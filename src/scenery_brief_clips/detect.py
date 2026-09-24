from __future__ import annotations

import os
from pathlib import Path


def _detect_frame_skip() -> int:
    raw = os.environ.get("SCENERY_DETECT_FRAME_SKIP", "1").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(0, value)


def detect_scenes(video_path: str | Path, min_scene_len_s: float = 0.5) -> list[tuple[float, float]]:
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import AdaptiveDetector, ThresholdDetector

    path = Path(video_path)
    video = open_video(str(path))
    manager = SceneManager()
    manager.add_detector(AdaptiveDetector(min_scene_len=min_scene_len_s))
    manager.add_detector(ThresholdDetector(min_scene_len=min_scene_len_s))
    manager.detect_scenes(video, frame_skip=_detect_frame_skip())
    scenes = manager.get_scene_list(start_in_scene=True)
    out: list[tuple[float, float]] = []
    for start, end in scenes:
        out.append((float(start.seconds), float(end.seconds)))
    if not out:
        raise RuntimeError(f"no scenes decoded from {path}")
    return out
