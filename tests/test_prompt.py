from scenery_brief_clips.prompt import parse_prompt


def test_example_prompt_sets_hd_16x9_clip_count_and_theme():
    c = parse_prompt(
        "Beautiful natural european scenery, 1920p or higher, 16:9, 20 individual clips"
    )
    assert c.min_width == 1920
    assert c.min_height == 1080
    assert c.n_clips == 20
    assert c.aspect_min == 1.70
    assert c.aspect_max == 1.86
    assert c.geo_requirement == "european"
    assert c.allow_download is False
    assert "european" in c.theme_text.lower()
    assert "scenery" in c.theme_text.lower()
    assert "1920" not in c.theme_text
    assert "16:9" not in c.theme_text
    assert "clips" not in c.theme_text.lower()


def test_4k_maps_to_uhd_dimensions():
    c = parse_prompt("alps 4k 16:9 5 clips")
    assert c.min_height == 2160
    assert c.min_width == 3840
    assert c.n_clips == 5


def test_1080p_with_widescreen_requires_1920x1080():
    c = parse_prompt("coast 1080p 16:9")
    assert c.min_width == 1920
    assert c.min_height == 1080


def test_defaults_when_only_theme_given():
    c = parse_prompt("norwegian fjords")
    assert c.n_clips == 20
    assert c.min_width == 1280
    assert c.min_height == 720
    assert c.target_duration_s == 6.0
    assert c.duration_min_s == 4.0
    assert c.duration_max_s == 12.0
    assert c.theme_text.lower() == "norwegian fjords"
    assert c.geo_requirement == "none"
    assert c.allow_download is False


def test_1440p_maps_to_2560x1440():
    c = parse_prompt("forest 1440p")
    assert c.min_width == 2560
    assert c.min_height == 1440


def test_720p_maps_to_1280x720():
    c = parse_prompt("coast 720p 16:9 3 clips")
    assert c.min_width == 1280
    assert c.min_height == 720
    assert c.n_clips == 3
    assert "720" not in c.theme_text


def test_short_clips_prompt_preserves_requested_count_and_activity_theme():
    c = parse_prompt("4 short clips of trees being cut down")
    assert c.n_clips == 4
    assert c.theme_text == "trees being cut down"


def test_720p_literal_dimensions_parse():
    c = parse_prompt("ridge 1280x720 16:9")
    assert c.min_width == 1280
    assert c.min_height == 720
    assert "1280" not in c.theme_text
