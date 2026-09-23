from __future__ import annotations

from scenery_brief_clips.models import Constraint

_EXTRAS = ("4k", "compilation", "drone")


def build_queries(constraint: Constraint) -> list[str]:
    base = constraint.theme_text.strip()
    if not base:
        return []
    queries = [base]
    lowered = base.lower()
    for extra in _EXTRAS:
        if extra not in lowered:
            queries.append(f"{base} {extra}")
    return queries
