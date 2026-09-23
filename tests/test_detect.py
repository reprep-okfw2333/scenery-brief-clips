from pathlib import Path

from scenery_brief_clips.detect import detect_scenes


def _color_cut_video(path: Path) -> None:
    import subprocess

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=320x180:d=3",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x180:d=3",
        "-f",
        "lavfi",
        "-i",
        "color=c=green:s=320x180:d=4",
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True)


def test_detect_scenes_finds_hard_cuts_on_color_changes(tmp_path):
    video = tmp_path / "cuts.mp4"
    _color_cut_video(video)
    scenes = detect_scenes(video, min_scene_len_s=0.5)
    assert len(scenes) >= 3
    starts = [s for s, _ in scenes]
    # cuts near 3s and 6s
    assert any(abs(t - 0.0) < 0.4 for t in starts)
    assert any(2.5 <= t <= 3.5 for t in starts)
    assert any(5.5 <= t <= 6.5 for t in starts)


def test_detect_scenes_no_cut_is_one_scene(tmp_path):
    import subprocess

    video = tmp_path / "one.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x180:d=4",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    scenes = detect_scenes(video, min_scene_len_s=0.5)
    assert len(scenes) == 1
    start, end = scenes[0]
    assert start == 0.0
    assert end >= 3.5
