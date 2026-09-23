from scenery_brief_clips.eligibility import evaluate_metadata
from scenery_brief_clips.models import Candidate, Constraint, Reject


def _info(**overrides):
    base = {
        "id": "dQw4w9WgXcQ",
        "title": "Nature 4K",
        "duration": 180,
        "live_status": "not_live",
        "availability": "public",
        "formats": [
            {
                "format_id": "22",
                "width": 1280,
                "height": 720,
                "fps": 30,
                "vcodec": "avc1.64001F",
                "acodec": "mp4a.40.2",
            },
            {
                "format_id": "137",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "vcodec": "avc1.640028",
                "acodec": "none",
            },
        ],
    }
    base.update(overrides)
    return base


def _constraint(**overrides) -> Constraint:
    data = dict(
        theme_text="nature",
        min_width=1920,
        min_height=1080,
        aspect_min=1.70,
        aspect_max=1.86,
        allow_download=False,
    )
    data.update(overrides)
    return Constraint(**data)


def test_keeps_720p_source_under_720p_floor_constraint():
    info = _info(
        title="Nature 720",
        formats=[
            {
                "format_id": "22",
                "width": 1280,
                "height": 720,
                "fps": 30,
                "vcodec": "avc1.64001F",
                "acodec": "mp4a.40.2",
            }
        ],
    )
    result = evaluate_metadata(info, _constraint(min_width=1280, min_height=720))
    assert isinstance(result, Candidate)
    assert result.width == 1280
    assert result.height == 720
    assert result.format_id == "22"


def test_keeps_public_1080p_widescreen():
    result = evaluate_metadata(_info(), _constraint())
    assert isinstance(result, Candidate)
    assert result.video_id == "dQw4w9WgXcQ"
    assert result.width == 1920
    assert result.height == 1080
    assert abs(result.aspect - 16 / 9) < 0.01
    assert result.format_id == "137"
    assert result.watch_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert result.duration_hint == "short"


def test_rejects_720p_even_when_title_says_4k():
    info = _info(
        title="Ultra 4K Europe",
        formats=[
            {
                "format_id": "22",
                "width": 1280,
                "height": 720,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "mp4a.40.2",
            }
        ],
    )
    result = evaluate_metadata(info, _constraint())
    assert isinstance(result, Reject)
    assert result.reason == "no_min_resolution"


def test_rejects_vertical_1080p():
    info = _info(
        formats=[
            {
                "format_id": "v",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "none",
            }
        ]
    )
    result = evaluate_metadata(info, _constraint())
    assert isinstance(result, Reject)
    assert result.reason == "aspect"


def test_rejects_1080_high_format_narrower_than_minimum_width():
    info = _info(
        formats=[
            {
                "format_id": "narrow",
                "width": 1840,
                "height": 1080,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "none",
            }
        ]
    )
    result = evaluate_metadata(info, _constraint())
    assert isinstance(result, Reject)
    assert result.reason == "no_min_resolution"


def test_chooses_valid_landscape_instead_of_taller_portrait():
    info = _info(
        formats=[
            {
                "format_id": "landscape",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "none",
            },
            {
                "format_id": "portrait",
                "width": 1440,
                "height": 2560,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "none",
            },
        ]
    )
    result = evaluate_metadata(info, _constraint())
    assert isinstance(result, Candidate)
    assert result.format_id == "landscape"


def test_rejects_live():
    result = evaluate_metadata(_info(live_status="is_live"), _constraint())
    assert isinstance(result, Reject)
    assert result.reason == "live"


def test_rejects_upcoming():
    result = evaluate_metadata(_info(live_status="is_upcoming"), _constraint())
    assert isinstance(result, Reject)
    assert result.reason == "upcoming"


def test_rejects_needs_auth():
    result = evaluate_metadata(_info(availability="needs_auth"), _constraint())
    assert isinstance(result, Reject)
    assert result.reason == "needs_auth"


def test_prefers_tallest_format_that_meets_minimum():
    info = _info(
        formats=[
            {
                "format_id": "137",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "none",
            },
            {
                "format_id": "313",
                "width": 3840,
                "height": 2160,
                "fps": 30,
                "vcodec": "vp9",
                "acodec": "none",
            },
            {
                "format_id": "140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
            },
        ]
    )
    result = evaluate_metadata(info, _constraint())
    assert isinstance(result, Candidate)
    assert result.format_id == "313"
    assert result.height == 2160


def test_long_duration_is_kept_with_long_hint():
    result = evaluate_metadata(_info(duration=8 * 3600), _constraint())
    assert isinstance(result, Candidate)
    assert result.duration_hint == "long"


def test_medium_duration_hint():
    result = evaluate_metadata(_info(duration=20 * 60), _constraint())
    assert isinstance(result, Candidate)
    assert result.duration_hint == "medium"


def test_dash_prefixed_id_stays_in_watch_url():
    result = evaluate_metadata(_info(id="-LA9aBfC6j8"), _constraint())
    assert isinstance(result, Candidate)
    assert result.video_id == "-LA9aBfC6j8"
    assert result.watch_url == "https://www.youtube.com/watch?v=-LA9aBfC6j8"
