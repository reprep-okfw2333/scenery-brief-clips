import json

import pytest

from scenery_brief_clips.brief import (
    BriefValidationError,
    PLANNER_INSTRUCTION,
    QueryPlanValidationError,
    canonical_json_hash,
    render_planner,
    validate_brief,
    validate_query_plan,
)


def valid_brief():
    return {
        "schema_version": "search_brief_v1",
        "request_text": "clips of alpacas in a field",
        "scene": {
            "subjects": [{"noun": "alpaca", "min_visible": 1}],
            "setting": "outdoor field or pasture",
            "action": None,
            "required_other": [],
            "excluded": [],
        },
        "theme_text": "alpacas in an outdoor field",
        "geography": None,
        "n_clips": 3,
        "clip_duration_s": {"min": 4, "target": 6, "max": 12},
        "source_geometry": {
            "min_width": 1280,
            "min_height": 720,
            "aspect_min": 1.70,
            "aspect_max": 1.86,
        },
        "export_max_height": 720,
        "search_limits": {
            "max_search_results": 20,
            "max_metadata_fetches": 30,
            "sleep_s": 2.0,
        },
        "delivery": "files",
        "permissions": {"may_search": True, "may_download_video": False},
        "sources": {
            "scene.subjects": {"origin": "user_explicit", "quote": "alpacas"},
            "scene.setting": {"origin": "user_explicit", "quote": "in a field"},
            "scene.action": {"origin": "project_default", "quote": None},
            "scene.required_other": {"origin": "project_default", "quote": None},
            "scene.excluded": {"origin": "project_default", "quote": None},
            "geography": {"origin": "project_default", "quote": None},
            "n_clips": {"origin": "user_clarification", "quote": "three clips"},
            "clip_duration_s": {"origin": "project_default", "quote": None},
            "source_geometry": {"origin": "project_default", "quote": None},
            "export_max_height": {"origin": "project_default", "quote": None},
            "delivery": {"origin": "project_default", "quote": None},
            "search_limits": {"origin": "project_default", "quote": None},
        },
    }


def valid_plan(brief=None):
    brief = brief or valid_brief()
    return {
        "version": "search_queries_v1",
        "brief_sha256": canonical_json_hash(brief),
        "queries": [
            {"query": "alpacas in a field", "strategy": "exact"},
            {"query": "alpaca pasture footage", "strategy": "context"},
        ],
    }


class CanonicalHashTests:
    def test_hash_uses_sorted_compact_utf8_json(self):
        import hashlib

        left = {"name": "alpaca", "nested": {"z": 1, "a": 2}}
        right = {"nested": {"a": 2, "z": 1}, "name": "alpaca"}
        encoded = json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert canonical_json_hash(left) == hashlib.sha256(encoded).hexdigest()
        assert canonical_json_hash(left) == canonical_json_hash(right)


class BriefValidationTests:
    def test_accepts_complete_alpaca_brief_without_rewriting_it(self):
        brief = valid_brief()
        assert validate_brief(brief) == brief

    def test_rejects_float_export_cap_even_when_numerically_equal(self):
        brief = valid_brief()
        brief["export_max_height"] = 720.0
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_non_string_delivery_fails_with_validation_error(self):
        brief = valid_brief()
        brief["delivery"] = []
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_clip_count_cannot_be_a_project_default(self):
        brief = valid_brief()
        brief["sources"]["n_clips"] = {"origin": "project_default", "quote": None}
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_boolean_is_not_accepted_as_clip_count_integer(self):
        brief = valid_brief()
        brief["n_clips"] = True
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_missing_clip_count_asks_for_clarification_code(self):
        brief = valid_brief()
        del brief["n_clips"]
        with pytest.raises(BriefValidationError) as raised:
            validate_brief(brief)
        assert raised.value.code == "clarification_required"

    def test_non_string_provenance_origin_is_a_validation_error(self):
        brief = valid_brief()
        brief["sources"]["delivery"]["origin"] = []
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_unknown_top_level_field_is_rejected(self):
        brief = valid_brief()
        brief["visual_enforced"] = True
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_required_setting_cannot_also_be_excluded(self):
        brief = valid_brief()
        brief["scene"]["excluded"] = ["outdoor field or pasture"]
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_subject_cannot_be_excluded(self):
        brief = valid_brief()
        brief["scene"]["excluded"] = ["alpaca"]
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_explicit_provenance_quote_must_come_from_original_request_casefolded(self):
        brief = valid_brief()
        brief["sources"]["scene.setting"]["quote"] = "IN A FIELD"
        assert validate_brief(brief) == brief

        brief["sources"]["scene.setting"]["quote"] = "open grassland"
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_clarification_quote_may_come_from_a_later_reply(self):
        brief = valid_brief()
        brief["sources"]["n_clips"]["quote"] = "three clips, please"
        assert validate_brief(brief) == brief

    def test_unsupported_geography_is_rejected(self):
        brief = valid_brief()
        brief["geography"] = "tropical"
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_only_the_supported_duration_band_is_accepted(self):
        brief = valid_brief()
        brief["clip_duration_s"] = {"min": 2, "target": 5, "max": 9}
        with pytest.raises(BriefValidationError):
            validate_brief(brief)

    def test_geometry_looser_than_1280x720_is_rejected(self):
        brief = valid_brief()
        brief["source_geometry"]["min_height"] = 480
        with pytest.raises(BriefValidationError):
            validate_brief(brief)


class PlannerRenderTests:
    def test_instruction_is_invariant_while_compact_view_is_brief_bound(self):
        first = render_planner(valid_brief())
        other = valid_brief()
        other["request_text"] = "clips of llamas in a field"
        other["scene"]["subjects"][0]["noun"] = "llama"
        other["sources"]["scene.subjects"]["quote"] = "llamas"
        other["theme_text"] = "llamas in a field"
        second = render_planner(other)

        assert first["instruction"] == second["instruction"] == PLANNER_INSTRUCTION
        first_view = json.loads(first["search_view_json"])
        second_view = json.loads(second["search_view_json"])
        assert first_view != second_view
        assert first_view["brief_sha256"] == canonical_json_hash(valid_brief())
        assert set(first_view) == {
            "brief_sha256", "subjects", "setting", "action", "required_other",
            "excluded_context", "geography",
        }
        assert "request_text" not in first_view

    def test_render_refuses_an_invalid_brief(self):
        brief = valid_brief()
        brief["n_clips"] = 0
        with pytest.raises(BriefValidationError):
            render_planner(brief)


class QueryPlanValidationTests:
    def test_accepts_brief_bound_bounded_query_plan(self):
        brief = valid_brief()
        plan = valid_plan(brief)
        assert validate_query_plan(plan, brief) == plan

    def test_broad_subject_queries_need_not_repeat_setting_or_action(self):
        brief = valid_brief()
        plan = {
            "version": "search_queries_v1", "brief_sha256": canonical_json_hash(brief),
            "queries": [
                {"query": "alpaca", "strategy": "exact"},
                {"query": "alpaca animals", "strategy": "synonym"},
            ],
        }
        assert validate_query_plan(plan, brief) == plan

    def test_wrong_strategy_type_is_rejected_as_a_plan_error(self):
        brief = valid_brief()
        plan = valid_plan(brief)
        plan["queries"][0]["strategy"] = []
        with pytest.raises(QueryPlanValidationError):
            validate_query_plan(plan, brief)

    def test_wrong_brief_hash_is_rejected(self):
        brief = valid_brief()
        plan = valid_plan(brief)
        plan["brief_sha256"] = "0" * 64
        with pytest.raises(QueryPlanValidationError) as raised:
            validate_query_plan(plan, brief)
        assert raised.value.code == "brief_hash_mismatch"

    def test_duplicate_queries_are_rejected_case_insensitively(self):
        brief = valid_brief()
        plan = {
            "version": "search_queries_v1", "brief_sha256": canonical_json_hash(brief),
            "queries": [
                {"query": "alpacas in a field", "strategy": "exact"},
                {"query": "  ALPACAS in a field ", "strategy": "context"},
            ],
        }
        with pytest.raises(QueryPlanValidationError) as raised:
            validate_query_plan(plan, brief)
        assert raised.value.code == "duplicate_query"

    def test_single_query_is_outside_the_allowed_count_bounds(self):
        brief = valid_brief()
        plan = {
            "version": "search_queries_v1", "brief_sha256": canonical_json_hash(brief),
            "queries": [{"query": "alpacas in a field", "strategy": "exact"}],
        }
        with pytest.raises(QueryPlanValidationError) as raised:
            validate_query_plan(plan, brief)
        assert raised.value.code == "invalid_query_count"

    def test_empty_plan_is_the_explicit_no_guess_response(self):
        brief = valid_brief()
        plan = {"version": "search_queries_v1", "brief_sha256": canonical_json_hash(brief), "queries": []}
        assert validate_query_plan(plan, brief) == plan

    def test_query_over_character_bound_is_rejected(self):
        brief = valid_brief()
        plan = {
            "version": "search_queries_v1", "brief_sha256": canonical_json_hash(brief),
            "queries": [
                {"query": "alpaca " + "x" * 114, "strategy": "exact"},
                {"query": "alpaca pasture", "strategy": "context"},
            ],
        }
        with pytest.raises(QueryPlanValidationError) as raised:
            validate_query_plan(plan, brief)
        assert raised.value.code == "query_too_long"

    def test_more_than_four_queries_is_rejected(self):
        brief = valid_brief()
        queries = [{"query": f"alpaca field {index}", "strategy": "context"} for index in range(5)]
        plan = {"version": "search_queries_v1", "brief_sha256": canonical_json_hash(brief), "queries": queries}
        with pytest.raises(QueryPlanValidationError) as raised:
            validate_query_plan(plan, brief)
        assert raised.value.code == "invalid_query_count"

    def test_query_with_a_url_is_rejected(self):
        brief = valid_brief()
        plan = {
            "version": "search_queries_v1", "brief_sha256": canonical_json_hash(brief),
            "queries": [
                {"query": "alpacas https://example.com", "strategy": "exact"},
                {"query": "alpaca pasture", "strategy": "context"},
            ],
        }
        with pytest.raises(QueryPlanValidationError) as raised:
            validate_query_plan(plan, brief)
        assert raised.value.code == "invalid_query"

    def test_plan_replacing_the_subject_is_rejected(self):
        brief = valid_brief()
        plan = {
            "version": "search_queries_v1", "brief_sha256": canonical_json_hash(brief),
            "queries": [
                {"query": "llamas in a field", "strategy": "exact"},
                {"query": "llama pasture footage", "strategy": "context"},
            ],
        }
        with pytest.raises(QueryPlanValidationError) as raised:
            validate_query_plan(plan, brief)
        assert raised.value.code == "subject_mismatch"
