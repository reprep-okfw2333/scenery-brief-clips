from __future__ import annotations

from pathlib import Path


def detect_scenes(video_path: str | Path, min_scene_len_s: float = 0.5) -> list[tuple[float, float]]:
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import AdaptiveDetector, ThresholdDetector

    path = Path(video_path)
    video = open_video(str(path))
    manager = SceneManager()
    manager.add_detector(AdaptiveDetector(min_scene_len=min_scene_len_s))
    manager.add_detector(ThresholdDetector(min_scene_len=min_scene_len_s))
    manager.detect_scenes(video)
    scenes = manager.get_scene_list(start_in_scene=True)
    out: list[tuple[float, float]] = []
    for start, end in scenes:
        out.append((float(start.seconds), float(end.seconds)))
    if not out:
        raise RuntimeError(f"no scenes decoded from {path}")
    return out
