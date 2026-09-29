"""Loosened brief form (plan step 5): duration bands, 1080p cap, place names,
optional sources, and how the new fields flow into runner/CLI/vision."""

import copy
import json
import re
from pathlib import Path

import pytest

from scenery_brief_clips.brief import BriefValidationError, validate_brief
from scenery_brief_clips.cli import _brief_constraint_from_brief
from scenery_brief_clips.runner import _bindings, _config_with_brief_export_cap
from scenery_brief_clips.vision_wire import _theme_from_run

from test_brief import valid_brief

REPO = Path(__file__).resolve().parents[1]


def brief_with(**changes):
    brief = valid_brief()
    for key, value in changes.items():
        brief[key] = value
    return brief


def band(lo, target, hi):
    return {"min": lo, "target": target, "max": hi}


def rejects(brief):
    with pytest.raises(BriefValidationError):
        validate_brief(brief)


# 1. duration ----------------------------------------------------------------

@pytest.mark.parametrize("b", [
    (2, 2, 30), (2, 5, 9), (18, 20, 22), (4.5, 6, 12), (4, 6, 12), (2.0, 3.5, 30.0),
])
def test_duration_accepted(b):
    assert validate_brief(brief_with(clip_duration_s=band(*b)))["clip_duration_s"] == band(*b)


@pytest.mark.parametrize("b", [
    (1.9, 5, 9),      # min below floor
    (1, 5, 9),
    (0, 5, 9),
    (4, 6, 30.5),     # max above ceiling
    (4, 6, 31),
    (8, 6, 12),       # min > target
    (4, 13, 12),      # target > max
    (6, 6, 6),        # min == max
    (10, 10, 10),
    (4, 6, "12"),     # non numbers
    ("4", 6, 12),
    (4, None, 12),
    (4, [6], 12),
    (True, 6, 12),    # bool
    (4, True, 12),
    (2, 6, True),
])
def test_duration_rejected(b):
    rejects(brief_with(clip_duration_s=band(*b)))


def test_duration_extra_or_missing_key_rejected():
    rejects(brief_with(clip_duration_s={"min": 4, "target": 6}))
    rejects(brief_with(clip_duration_s={**band(4, 6, 12), "extra": 1}))


# 2. export_max_height -------------------------------------------------------

@pytest.mark.parametrize("h", [720, 1080])
def test_export_height_accepted(h):
    assert validate_brief(brief_with(export_max_height=h))["export_max_height"] == h


@pytest.mark.parametrize("h", [1440, 2160, 480, 0, -720, "1080", 1080.0, 720.0, True, None])
def test_export_height_rejected(h):
    rejects(brief_with(export_max_height=h))


# 3. geography ---------------------------------------------------------------

@pytest.mark.parametrize("g", [
    "Iceland", "Scottish Highlands", "Kyōto", "Côte d'Azur", "Sahara, Morocco",
    "european", None, "Côte d’Azur", "St. Moritz", "Aix-en-Provence", "A", "x" * 60,
])
def test_geography_accepted(g):
    assert validate_brief(brief_with(geography=g))["geography"] == g


@pytest.mark.parametrize("g", [
    "", " ", " Iceland", "Iceland ", "Iceland\n", "Tokyo 2020", "x" * 61, "a;b",
    ["Iceland"], 5, True, {"place": "Iceland"}, "-Iceland", ".Iceland", "'Iceland",
    "Ice/land", "Iceland!",
])
def test_geography_rejected(g):
    rejects(brief_with(geography=g))


# 4. sources -----------------------------------------------------------------

def minimal_sources():
    return {
        "scene.subjects": {"origin": "user_explicit", "quote": "alpacas"},
        "n_clips": {"origin": "user_clarification", "quote": "three clips"},
    }


def test_sources_with_only_required_keys_validate():
    assert validate_brief(brief_with(sources=minimal_sources()))


def test_sources_required_keys_alone_and_explicit_n_clips_quote():
    sources = minimal_sources()
    sources["n_clips"] = {"origin": "user_explicit", "quote": "CLIPS"}  # case-insensitive
    assert validate_brief(brief_with(sources=sources))


@pytest.mark.parametrize("drop", ["n_clips", "scene.subjects"])
def test_sources_missing_required_key_rejected(drop):
    sources = minimal_sources()
    del sources[drop]
    rejects(brief_with(sources=sources))


def test_sources_empty_rejected():
    rejects(brief_with(sources={}))


def test_sources_unknown_key_rejected():
    sources = minimal_sources()
    sources["scene.mood"] = {"origin": "project_default", "quote": None}
    rejects(brief_with(sources=sources))


@pytest.mark.parametrize("field", ["scene.subjects", "n_clips"])
def test_required_source_cannot_be_project_default(field):
    sources = minimal_sources()
    sources[field] = {"origin": "project_default", "quote": None}
    rejects(brief_with(sources=sources))


def test_user_explicit_quote_must_be_in_request_text():
    sources = minimal_sources()
    sources["scene.subjects"] = {"origin": "user_explicit", "quote": "llamas"}
    rejects(brief_with(sources=sources))


def test_user_explicit_quote_is_case_insensitive_substring():
    sources = minimal_sources()
    sources["scene.subjects"] = {"origin": "user_explicit", "quote": "ALPACAS in a"}
    assert validate_brief(brief_with(sources=sources))


def test_project_default_quote_must_be_null():
    sources = minimal_sources()
    sources["scene.setting"] = {"origin": "project_default", "quote": "in a field"}
    rejects(brief_with(sources=sources))


def test_included_optional_key_still_checked():
    sources = minimal_sources()
    sources["geography"] = {"origin": "project_default", "quote": "Iceland"}
    rejects(brief_with(sources=sources))
    sources["geography"] = {"origin": "user_explicit", "quote": "in Iceland"}  # not in request_text
    rejects(brief_with(sources=sources))
    sources["geography"] = {"origin": "project_default", "quote": None}
    assert validate_brief(brief_with(sources=sources))


def test_user_source_needs_a_quote_and_bad_origin_rejected():
    sources = minimal_sources()
    sources["scene.setting"] = {"origin": "user_explicit", "quote": None}
    rejects(brief_with(sources=sources))
    sources["scene.setting"] = {"origin": "guess", "quote": None}
    rejects(brief_with(sources=sources))


# 5. unchanged safeguards ----------------------------------------------------

def test_min_height_480_still_rejected():
    brief = valid_brief()
    brief["source_geometry"]["min_height"] = 480
    rejects(brief)


def test_min_width_below_1280_still_rejected():
    brief = valid_brief()
    brief["source_geometry"]["min_width"] = 1000
    rejects(brief)


@pytest.mark.parametrize("lo,hi", [(1.5, 1.86), (1.70, 2.0), (1.6, 1.9), (1.80, 1.80)])
def test_aspect_band_outside_still_rejected(lo, hi):
    brief = valid_brief()
    brief["source_geometry"]["aspect_min"] = lo
    brief["source_geometry"]["aspect_max"] = hi
    rejects(brief)


def test_max_search_results_21_still_rejected():
    brief = valid_brief()
    brief["search_limits"]["max_search_results"] = 21
    rejects(brief)


def test_may_download_video_true_still_rejected():
    brief = valid_brief()
    brief["permissions"]["may_download_video"] = True
    rejects(brief)


def test_1080p_geometry_still_accepted():
    brief = valid_brief()
    brief["source_geometry"].update(min_width=1920, min_height=1080)
    assert validate_brief(brief)


# 6. _config_with_brief_export_cap -------------------------------------------

def test_cap_brief_720_empty_config_is_same_object():
    config = {}
    assert _config_with_brief_export_cap(config, brief_with(export_max_height=720)) is config


def test_cap_brief_1080_empty_config_copies():
    config = {}
    out = _config_with_brief_export_cap(config, brief_with(export_max_height=1080))
    assert out is not config
    assert out["export_max_height"] == 1080
    assert config == {}


def test_cap_brief_1080_copy_keeps_other_keys_and_original_untouched():
    config = {"run_deadline_s": 5, "continuity_detector": "legacy"}
    snapshot = copy.deepcopy(config)
    out = _config_with_brief_export_cap(config, brief_with(export_max_height=1080))
    assert out == {**snapshot, "export_max_height": 1080}
    assert config == snapshot


def test_cap_config_can_lower_brief_1080():
    config = {"export_max_height": 720}
    out = _config_with_brief_export_cap(config, brief_with(export_max_height=1080))
    assert out["export_max_height"] == 720
    assert config == {"export_max_height": 720}


def test_cap_config_cannot_raise_brief_720():
    config = {"export_max_height": 1080}
    out = _config_with_brief_export_cap(config, brief_with(export_max_height=720))
    assert out["export_max_height"] == 720
    assert config == {"export_max_height": 1080}  # not mutated


def test_cap_brief_1080_config_1080_same_object():
    config = {"export_max_height": 1080}
    assert _config_with_brief_export_cap(config, brief_with(export_max_height=1080)) is config


def test_cap_brief_720_config_720_same_object():
    config = {"export_max_height": 720}
    assert _config_with_brief_export_cap(config, brief_with(export_max_height=720)) is config


# 7. _bindings export hash ---------------------------------------------------

def export_hashes(config, brief):
    b = _bindings(brief, None, config, "model-x")
    return b["agree_export"], b["export"], b["verify_export"]


def test_export_binding_unchanged_for_720_brief():
    brief = brief_with(export_max_height=720)
    effective = _config_with_brief_export_cap({}, brief)
    assert export_hashes(effective, brief) == export_hashes({}, brief)


def test_export_binding_differs_for_1080_brief():
    brief = brief_with(export_max_height=1080)
    effective = _config_with_brief_export_cap({}, brief)
    assert export_hashes(effective, brief) != export_hashes({}, brief)
    # and the non-export stages are unaffected by the cap
    base, new = _bindings(brief, None, {}, "m"), _bindings(brief, None, effective, "m")
    for stage in ("discover", "rank", "analyze", "label_tiles"):
        assert base[stage] == new[stage]


# 8. CLI constraint ----------------------------------------------------------

@pytest.mark.parametrize("geo,expected", [
    ("Iceland", "Iceland"), ("european", "european"), (None, "none"),
    ("Scottish Highlands", "Scottish Highlands"),
])
def test_constraint_geo_requirement(geo, expected):
    constraint, _ = _brief_constraint_from_brief(brief_with(geography=geo), {})
    assert constraint.geo_requirement == expected


def test_constraint_durations_and_geometry_flow_through():
    brief = brief_with(clip_duration_s=band(8, 10, 12), n_clips=4)
    brief["source_geometry"].update(min_width=1920, min_height=1080, aspect_min=1.75, aspect_max=1.80)
    constraint, limits = _brief_constraint_from_brief(brief, {})
    assert (constraint.duration_min_s, constraint.target_duration_s, constraint.duration_max_s) == (8.0, 10.0, 12.0)
    assert (constraint.min_width, constraint.min_height) == (1920, 1080)
    assert (constraint.aspect_min, constraint.aspect_max) == (1.75, 1.80)
    assert constraint.n_clips == 4
    assert constraint.theme_text == brief["theme_text"]
    assert constraint.allow_download is False
    assert limits.max_search_results == 20


def test_constraint_default_band_still_4_6_12():
    constraint, _ = _brief_constraint_from_brief(valid_brief(), {})
    assert (constraint.duration_min_s, constraint.target_duration_s, constraint.duration_max_s) == (4.0, 6.0, 12.0)


# 9. vision_wire._theme_from_run ---------------------------------------------

def write_constraint(tmp_path, payload):
    (tmp_path / "constraint.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_theme_names_requested_place(tmp_path):
    run = write_constraint(tmp_path, {"theme_text": "  horses in a meadow \n", "geo_requirement": "Iceland"})
    theme = _theme_from_run(run)
    assert theme.startswith("horses in a meadow")
    assert "Requested place: Iceland" in theme
    assert "conflicting" in theme


def test_theme_place_is_stripped(tmp_path):
    run = write_constraint(tmp_path, {"theme_text": "horses", "geo_requirement": " Iceland "})
    assert "Requested place: Iceland." in _theme_from_run(run)


@pytest.mark.parametrize("geo", ["european", "none"])
def test_theme_unchanged_for_european_or_none(tmp_path, geo):
    run = write_constraint(tmp_path, {"theme_text": "  horses in a meadow \n", "geo_requirement": geo})
    assert _theme_from_run(run) == "horses in a meadow"


def test_theme_unchanged_when_geo_missing_or_not_a_string(tmp_path):
    run = write_constraint(tmp_path, {"theme_text": "horses"})
    assert _theme_from_run(run) == "horses"
    run = write_constraint(tmp_path, {"theme_text": "horses", "geo_requirement": None})
    assert _theme_from_run(run) == "horses"
    run = write_constraint(tmp_path, {"theme_text": "horses", "geo_requirement": "  "})
    assert _theme_from_run(run) == "horses"


@pytest.mark.parametrize("theme", ["", "   ", None, 5])
def test_theme_blank_is_none_even_with_place(tmp_path, theme):
    run = write_constraint(tmp_path, {"theme_text": theme, "geo_requirement": "Iceland"})
    assert _theme_from_run(run) is None


def test_theme_none_when_no_constraint_file(tmp_path):
    assert _theme_from_run(tmp_path) is None


# 10. doc example ------------------------------------------------------------

def test_doc_example_validates():
    text = (REPO / "docs" / "SEARCH_BRIEF.md").read_text(encoding="utf-8")
    marker = text.index('Example: "three cinematic clips')
    match = re.search(r"```json\n(.*?)\n```", text[marker:], re.DOTALL)
    assert match, "no json block after the example marker"
    brief = json.loads(match.group(1))
    validated = validate_brief(brief)
    assert validated["geography"] == "Iceland"
    assert validated["export_max_height"] == 1080
