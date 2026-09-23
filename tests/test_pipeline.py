import json

from scenery_brief_clips.eligibility import evaluate_metadata
from scenery_brief_clips.models import Constraint, RunLimits
from scenery_brief_clips.pipeline import run_dry
from scenery_brief_clips.prompt import parse_prompt
from scenery_brief_clips.store import MetadataCache, write_run


def _hd_info(video_id: str, title: str, duration: int = 180) -> dict:
    return {
        "id": video_id,
        "title": title,
        "duration": duration,
        "live_status": "not_live",
        "availability": "public",
        "formats": [
            {
                "format_id": "137",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "none",
            }
        ],
    }


class FakeYt:
    def __init__(self, search_hits: list[dict], metadata: dict[str, dict]):
        self.search_hits = search_hits
        self.metadata = metadata
        self.search_calls: list[tuple[str, int]] = []
        self.metadata_calls: list[str] = []

    def search(self, query: str, limit: int) -> list[dict]:
        self.search_calls.append((query, limit))
        return self.search_hits[:limit]

    def fetch_metadata(self, video_id: str) -> dict:
        self.metadata_calls.append(video_id)
        if video_id not in self.metadata:
            raise RuntimeError(f"missing {video_id}")
        return self.metadata[video_id]


def test_cache_roundtrip(tmp_path):
    cache = MetadataCache(tmp_path / "cache")
    payload = {"id": "-LA9aBfC6j8", "title": "dash"}
    assert cache.get("-LA9aBfC6j8") is None
    cache.put("-LA9aBfC6j8", payload)
    assert cache.get("-LA9aBfC6j8") == payload


def test_write_run_emits_json_files(tmp_path):
    constraint = parse_prompt("fjords 1080p 16:9 3 clips")
    info = _hd_info("abcdefghijk", "Fjord")
    kept = evaluate_metadata(info, constraint)
    run_dir = write_run(
        tmp_path / "runs",
        constraint=constraint,
        candidates=[kept],
        rejected=[],
        log_text="ok\n",
        run_id="testrun",
    )
    assert (run_dir / "constraint.json").is_file()
    assert (run_dir / "candidates.json").is_file()
    assert (run_dir / "rejected.json").is_file()
    assert (run_dir / "log.txt").read_text() == "ok\n"
    data = json.loads((run_dir / "candidates.json").read_text())
    assert data[0]["video_id"] == "abcdefghijk"
    assert data[0]["height"] == 1080


def test_dry_run_keeps_eligible_and_skips_second_metadata_on_cache(tmp_path):
    constraint = Constraint(
        theme_text="alps",
        n_clips=2,
        limits=RunLimits(max_search_results=5, max_metadata_fetches=10, sleep_s=0.0),
    )
    hits = [
        {"id": "id111111111", "title": "Good"},
        {"id": "id222222222", "title": "Also"},
    ]
    meta = {
        "id111111111": _hd_info("id111111111", "Good"),
        "id222222222": _hd_info("id222222222", "Also", duration=20 * 60),
    }
    yt = FakeYt(hits, meta)
    cache = MetadataCache(tmp_path / "cache")
    first = run_dry(constraint, yt=yt, cache=cache, sleep_fn=lambda _s: None)
    assert len(first.candidates) == 2
    assert yt.metadata_calls == ["id111111111", "id222222222"]

    yt2 = FakeYt(hits, meta)
    second = run_dry(constraint, yt=yt2, cache=cache, sleep_fn=lambda _s: None)
    assert len(second.candidates) == 2
    assert yt2.metadata_calls == []


def test_dry_run_records_rejects_and_respects_metadata_cap(tmp_path):
    constraint = Constraint(
        theme_text="alps",
        limits=RunLimits(max_search_results=10, max_metadata_fetches=1, sleep_s=0.0),
    )
    hits = [
        {"id": "id111111111", "title": "Live"},
        {"id": "id222222222", "title": "Later"},
    ]
    meta = {
        "id111111111": {
            **_hd_info("id111111111", "Live"),
            "live_status": "is_live",
        },
        "id222222222": _hd_info("id222222222", "Later"),
    }
    yt = FakeYt(hits, meta)
    result = run_dry(
        constraint,
        yt=yt,
        cache=MetadataCache(tmp_path / "cache"),
        sleep_fn=lambda _s: None,
    )
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "live"
    assert result.candidates == []
    assert result.stopped_reason == "max_metadata_fetches"
    assert yt.metadata_calls == ["id111111111"]


def test_discovery_uses_more_than_first_query_variant(tmp_path):
    class QueryYt(FakeYt):
        def search(self, query: str, limit: int) -> list[dict]:
            self.search_calls.append((query, limit))
            prefix = len(self.search_calls)
            return [
                {"id": f"q{prefix}{i:09d}", "title": f"Hit {prefix}-{i}"}
                for i in range(limit)
            ]

        def fetch_metadata(self, video_id: str) -> dict:
            self.metadata_calls.append(video_id)
            return _hd_info(video_id, video_id)

    constraint = Constraint(
        theme_text="alps",
        limits=RunLimits(max_search_results=8, max_metadata_fetches=8, sleep_s=0.0),
    )
    yt = QueryYt([], {})
    result = run_dry(
        constraint,
        yt=yt,
        cache=MetadataCache(tmp_path / "cache"),
        sleep_fn=lambda _s: None,
    )
    assert len(yt.search_calls) == 4
    assert len(result.candidates) == 8


def test_discovery_keeps_earlier_hits_when_later_query_fails(tmp_path):
    class PartialSearchYt(FakeYt):
        def search(self, query: str, limit: int) -> list[dict]:
            self.search_calls.append((query, limit))
            if len(self.search_calls) == 2:
                raise RuntimeError("search down")
            return [
                {"id": "id111111111", "title": "First"},
                {"id": "id222222222", "title": "Second"},
            ][:limit]

    metadata = {
        "id111111111": _hd_info("id111111111", "First"),
        "id222222222": _hd_info("id222222222", "Second"),
    }
    constraint = Constraint(
        theme_text="alps",
        limits=RunLimits(max_search_results=8, max_metadata_fetches=8, sleep_s=0.0),
    )
    yt = PartialSearchYt([], metadata)
    result = run_dry(
        constraint,
        yt=yt,
        cache=MetadataCache(tmp_path / "cache"),
        sleep_fn=lambda _s: None,
    )

    assert result.stopped_reason == "search_error"
    assert {candidate.video_id for candidate in result.candidates} == {
        "id111111111",
        "id222222222",
    }
    assert yt.metadata_calls == ["id111111111", "id222222222"]


def test_later_search_failure_keeps_priority_when_metadata_budget_is_exhausted(tmp_path):
    class PartialSearchYt(FakeYt):
        def search(self, query: str, limit: int) -> list[dict]:
            self.search_calls.append((query, limit))
            if len(self.search_calls) == 2:
                raise RuntimeError("search down")
            return [
                {"id": "id111111111", "title": "First"},
                {"id": "id222222222", "title": "Second"},
            ][:limit]

    constraint = Constraint(
        theme_text="alps",
        limits=RunLimits(max_search_results=8, max_metadata_fetches=1, sleep_s=0.0),
    )
    metadata = {
        "id111111111": _hd_info("id111111111", "First"),
        "id222222222": _hd_info("id222222222", "Second"),
    }
    yt = PartialSearchYt([], metadata)
    result = run_dry(
        constraint,
        yt=yt,
        cache=MetadataCache(tmp_path / "cache"),
        sleep_fn=lambda _s: None,
    )

    assert result.stopped_reason == "search_error"
    assert "search failed: search down" in result.log_lines
    assert "skip id222222222: max_metadata_fetches" in result.log_lines
    assert {candidate.video_id for candidate in result.candidates} == {"id111111111"}
    assert yt.metadata_calls == ["id111111111"]


def test_cached_metadata_is_processed_after_network_budget_is_exhausted(tmp_path):
    constraint = Constraint(
        theme_text="alps",
        limits=RunLimits(max_search_results=2, max_metadata_fetches=1, sleep_s=0.0),
    )
    hits = [
        {"id": "id111111111", "title": "Fresh"},
        {"id": "id222222222", "title": "Cached"},
    ]
    metadata = {
        "id111111111": _hd_info("id111111111", "Fresh"),
        "id222222222": _hd_info("id222222222", "Cached"),
    }
    class QueueYt(FakeYt):
        def search(self, query: str, limit: int) -> list[dict]:
            self.search_calls.append((query, limit))
            index = len(self.search_calls) - 1
            return self.search_hits[index : index + 1]

    cache = MetadataCache(tmp_path / "cache")
    cache.put("id222222222", metadata["id222222222"])
    yt = QueueYt(hits, metadata)
    result = run_dry(constraint, yt=yt, cache=cache, sleep_fn=lambda _s: None)
    assert {candidate.video_id for candidate in result.candidates} == {
        "id111111111",
        "id222222222",
    }
    assert yt.metadata_calls == ["id111111111"]


def test_cached_metadata_after_uncached_budget_skip_is_still_processed(tmp_path):
    constraint = Constraint(
        theme_text="alps",
        limits=RunLimits(max_search_results=3, max_metadata_fetches=1, sleep_s=0.0),
    )
    hits = [
        {"id": "id111111111", "title": "Fresh"},
        {"id": "id222222222", "title": "Skipped"},
        {"id": "id333333333", "title": "Cached"},
    ]
    metadata = {
        "id111111111": _hd_info("id111111111", "Fresh"),
        "id222222222": _hd_info("id222222222", "Skipped"),
        "id333333333": _hd_info("id333333333", "Cached"),
    }

    class QueueYt(FakeYt):
        def search(self, query: str, limit: int) -> list[dict]:
            self.search_calls.append((query, limit))
            index = len(self.search_calls) - 1
            return self.search_hits[index : index + 1]

    cache = MetadataCache(tmp_path / "cache")
    cache.put("id333333333", metadata["id333333333"])
    yt = QueueYt(hits, metadata)
    result = run_dry(constraint, yt=yt, cache=cache, sleep_fn=lambda _s: None)

    assert {candidate.video_id for candidate in result.candidates} == {
        "id111111111",
        "id333333333",
    }
    assert yt.metadata_calls == ["id111111111"]
    assert result.stopped_reason == "max_metadata_fetches"
    assert "skip id222222222: max_metadata_fetches" in result.log_lines


def test_dry_run_reports_total_metadata_failure(tmp_path):
    constraint = Constraint(
        theme_text="alps",
        n_clips=2,
        limits=RunLimits(max_search_results=5, max_metadata_fetches=10, sleep_s=0.0),
    )
    hits = [
        {"id": "id111111111", "title": "A"},
        {"id": "id222222222", "title": "B"},
    ]
    yt = FakeYt(hits, metadata={})  # every metadata fetch raises

    result = run_dry(
        constraint,
        yt=yt,
        cache=MetadataCache(tmp_path / "cache"),
        sleep_fn=lambda _s: None,
    )

    assert result.candidates == []
    assert result.stopped_reason == "metadata_errors"
