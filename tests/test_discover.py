from scenery_brief_clips.discover import build_queries
from scenery_brief_clips.prompt import parse_prompt


def test_query_variants_include_theme_4k_and_compilation():
    constraint = parse_prompt(
        "Beautiful natural european scenery, 1920p or higher, 16:9, 20 individual clips"
    )
    queries = build_queries(constraint)
    assert 1 <= len(queries) <= 4
    joined = " | ".join(queries).lower()
    assert "european" in joined
    assert "scenery" in joined
    assert "4k" in joined
    assert "compilation" in joined
    assert queries[0] == constraint.theme_text


def test_query_variants_are_unique():
    constraint = parse_prompt("alps 4k drone compilation 16:9 3 clips")
    queries = build_queries(constraint)
    assert len(queries) == len(set(queries))
