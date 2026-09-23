from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from scenery_brief_clips.discover import build_queries
from scenery_brief_clips.eligibility import evaluate_metadata
from scenery_brief_clips.models import Candidate, Constraint, Reject
from scenery_brief_clips.store import MetadataCache
from scenery_brief_clips.yt import YtDlp


@dataclass
class DryRunResult:
    queries: list[str]
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Reject] = field(default_factory=list)
    stopped_reason: str | None = None
    log_lines: list[str] = field(default_factory=list)


def run_dry(
    constraint: Constraint,
    yt: YtDlp,
    cache: MetadataCache,
    sleep_fn: Callable[[float], None],
    queries: list[str] | None = None,
) -> DryRunResult:
    if constraint.allow_download:
        raise RuntimeError("dry-run refuses allow_download=True")

    if queries is None:
        queries = build_queries(constraint)
    else:
        # A validated plan supplies queries verbatim; no re-sort or dedupe.
        queries = list(queries)
    result = DryRunResult(queries=queries)
    ordered_ids: list[str] = []
    titles: dict[str, str] = {}
    remaining = constraint.limits.max_search_results

    for query_index, query in enumerate(queries):
        if remaining <= 0:
            break
        queries_left = len(queries) - query_index
        query_limit = max(1, (remaining + queries_left - 1) // queries_left)
        result.log_lines.append(f"search {query_limit}: {query}")
        try:
            hits = yt.search(query, query_limit)
        except Exception as exc:
            result.log_lines.append(f"search failed: {exc}")
            result.stopped_reason = "search_error"
            break
        for hit in hits:
            video_id = str(hit.get("id") or "")
            if not video_id or video_id in titles:
                continue
            titles[video_id] = str(hit.get("title") or "")
            ordered_ids.append(video_id)
            remaining = constraint.limits.max_search_results - len(ordered_ids)
            if remaining <= 0:
                break

    fetches = 0
    metadata_failures = 0
    for video_id in ordered_ids:
        info = cache.get(video_id)
        if info is None and fetches >= constraint.limits.max_metadata_fetches:
            if result.stopped_reason is None:
                result.stopped_reason = "max_metadata_fetches"
            result.log_lines.append(f"skip {video_id}: max_metadata_fetches")
            continue
        if info is None:
            try:
                info = yt.fetch_metadata(video_id)
            except Exception as exc:
                result.rejected.append(
                    Reject(video_id=video_id, title=titles.get(video_id), reason="metadata_error")
                )
                result.log_lines.append(f"metadata error {video_id}: {exc}")
                fetches += 1
                metadata_failures += 1
                if constraint.limits.sleep_s:
                    sleep_fn(constraint.limits.sleep_s)
                continue
            cache.put(video_id, info)
            fetches += 1
            if constraint.limits.sleep_s:
                sleep_fn(constraint.limits.sleep_s)
        judged = evaluate_metadata(info, constraint)
        if isinstance(judged, Candidate):
            result.candidates.append(judged)
            result.log_lines.append(f"keep {video_id} {judged.reason_kept}")
        else:
            result.rejected.append(judged)
            result.log_lines.append(f"reject {video_id} {judged.reason}")
    else:
        if result.stopped_reason is None:
            if metadata_failures and not result.candidates:
                result.stopped_reason = "metadata_errors"
            else:
                result.stopped_reason = "complete"

    return result
