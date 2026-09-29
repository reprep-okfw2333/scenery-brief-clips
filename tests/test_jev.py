"""Optional Jev support: jev.py units, run_dry judge hooks, shortlist note_violation, config, runner.

No network: every Jev call goes through a fake ``post``.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scenery_brief_clips import jev
from scenery_brief_clips.config import ConfigError, load_project_config
from scenery_brief_clips.jev import (
    DESCRIPTION_MAX_CHARS,
    JEV_MODEL,
    JEV_URL,
    NEUTRAL_SCORE,
    NOTE_QUESTIONS,
    SNIPPET_MAX_CHARS,
    SOURCE_QUESTIONS,
    DiscoveryJudge,
    JevClient,
    JevError,
    JevSettings,
    _check_answers,
    apply_note_check,
    brief_context,
    compact,
    default_post,
    hit_state,
    metadata_state,
    note_state,
    source_score,
)
from scenery_brief_clips.models import Constraint, RunLimits
from scenery_brief_clips.pipeline import run_dry
from scenery_brief_clips.shortlist import (
    REASON_NOTE_VIOLATION,
    ShortlistInputError,
    build_shortlist,
    validate_label_payload,
)
from scenery_brief_clips.store import MetadataCache
from test_brief import valid_brief
from test_pipeline import FakeYt, _hd_info
from test_shortlist import _bindings, _review_fixture, _rows_fixture, _unique_hashes

KEY = "sk-or-test-DO-NOT-LEAK-987"


# --------------------------------------------------------------------------- fake Jev


def _noul(p: float) -> dict:
    return {"type": "noul", "noul": p}


def _choice(label: str, options) -> dict:
    probs = {name: (0.9 if name == label else 0.1 / (len(options) - 1)) for name in options}
    return {"type": "choice", "choice": label, "probabilities": probs, "confidence": 0.9}


def answers_for(questions: dict, p_source: float = 0.6, p_violation: float = 0.1) -> dict:
    out = {}
    for name, spec in questions.items():
        if spec["type"] == "noul":
            out[name] = _noul(p_violation if name == "violation" else p_source)
        elif spec["type"] == "choice":
            label = "matches" if name == "place" else "exterior_scene" if name == "kind" else next(iter(spec["criteria"]))
            out[name] = _choice(label, spec["criteria"])
    return out


class FakeJev:
    """post(url, body, headers, timeout) -> dict. Scores come from the state.

    Source questions: ``hit_p`` / ``meta_p`` map a title fragment to a probability (the metadata
    state is recognised by its ``best_resolution`` key). Note question: a note containing a key of
    ``note_p`` gets that P(violation), else 0.1.
    """

    def __init__(self, hit_p=None, meta_p=None, note_p=None, default=0.6, cost=0.0001, fail=None, delay=None):
        self.hit_p = hit_p or {}
        self.meta_p = meta_p or {}
        self.note_p = note_p or {}
        self.default = default
        self.cost = cost
        self.fail = fail
        self.delay = delay
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    @staticmethod
    def _lookup(table, text, default):
        for fragment, p in table.items():
            if fragment in (text or ""):
                return p
        return default

    def __call__(self, url, body, headers, timeout):
        parsed = json.loads(body)
        with self._lock:
            self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout, "body": parsed})
        if self.fail is not None:
            raise self.fail
        if self.delay is not None:
            time.sleep(self.delay(parsed))
        state, questions = parsed["state"], parsed["questions"]
        if "violation" in questions:
            p = self._lookup(self.note_p, state["vision"]["note"], 0.1)
            answers = answers_for(questions, p_violation=p)
        else:
            video = state["video"]
            table = self.meta_p if "best_resolution" in video else self.hit_p
            answers = answers_for(questions, p_source=self._lookup(table, video["title"], self.default))
        return {"answers": answers, "usage": {"cost": self.cost, "input_tokens": 1000},
                "model": "typesafe/jev-1.13-test"}

    @property
    def count(self) -> int:
        return len(self.calls)


def _client(tmp_path, post, settings=None, key=KEY):
    return JevClient(api_key=key, cache_dir=tmp_path / "jevcache", settings=settings or JevSettings(), post=post)


def _all_files_text(root: Path) -> list[tuple[Path, str]]:
    out = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".txt", ".yaml", ".tsv"}:
            out.append((path, path.read_text(encoding="utf-8", errors="replace")))
    return out


STATE = {"video": {"title": "a"}}


# --------------------------------------------------------------------------- settings


class TestSettings:
    def test_defaults(self):
        s = JevSettings.from_config({})
        assert (s.rank, s.note_check) == (False, False)
        assert s.reject_below == 0.35 and s.note_reject_at == 0.70
        assert s.timeout_s == 20.0 and s.max_usd_per_run == 0.25 and s.workers == 4

    def test_from_config_reads_keys(self):
        s = JevSettings.from_config({"jev_rank": True, "jev_note_check": True, "jev_reject_below": 0.2,
                                     "jev_note_reject_at": 0.9, "jev_timeout_s": 5, "jev_max_usd_per_run": 0.5})
        assert s.rank and s.note_check
        assert (s.reject_below, s.note_reject_at, s.timeout_s, s.max_usd_per_run) == (0.2, 0.9, 5.0, 0.5)

    def test_jev_gate_alone_does_not_enable_rank_or_notes(self):
        s = JevSettings.from_config({"jev_gate": True})
        assert s.rank is False and s.note_check is False

    def test_bindings_none_when_off_and_stable_when_on(self):
        assert JevSettings().rank_binding() is None
        assert JevSettings().note_binding() is None
        rank = JevSettings(rank=True, reject_below=0.3).rank_binding()
        assert rank == {"model": JEV_MODEL, "questions": jev.SOURCE_QUESTIONS_VERSION, "reject_below": 0.3}
        note = JevSettings(note_check=True, note_reject_at=0.8).note_binding()
        assert note == {"model": JEV_MODEL, "questions": jev.NOTE_QUESTIONS_VERSION, "reject_at": 0.8}
        assert JevSettings(rank=True).rank_binding() != JevSettings(rank=True, reject_below=0.2).rank_binding()


# --------------------------------------------------------------------------- default_post


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDefaultPost:
    def test_success_returns_parsed_json_and_sends_headers(self, monkeypatch):
        seen = {}

        def fake_urlopen(req, timeout):
            seen.update(url=req.full_url, data=req.data, headers=dict(req.header_items()),
                        method=req.get_method(), timeout=timeout)
            return _Resp(b'{"answers": {}}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        out = default_post(JEV_URL, b'{"a":1}', {"Authorization": f"Bearer {KEY}"}, 3.0)
        assert out == {"answers": {}}
        assert seen["url"] == JEV_URL and seen["method"] == "POST" and seen["timeout"] == 3.0
        assert seen["data"] == b'{"a":1}'

    @pytest.mark.parametrize("exc, text", [
        (urllib.error.HTTPError(JEV_URL, 401, "unauthorized", {}, None), "HTTP 401"),
        (urllib.error.URLError("dns failure"), "transport error"),
        (TimeoutError("slow"), "transport error"),
    ])
    def test_errors_become_jev_error_without_the_key(self, monkeypatch, exc, text):
        def fake_urlopen(req, timeout):
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(JevError) as err:
            default_post(JEV_URL, b"{}", {"Authorization": f"Bearer {KEY}"}, 1.0)
        assert text in str(err.value) and KEY not in str(err.value)

    def test_invalid_json_response(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _Resp(b"<html>"))
        with pytest.raises(JevError, match="invalid JSON"):
            default_post(JEV_URL, b"{}", {}, 1.0)


# --------------------------------------------------------------------------- _check_answers


QS = {"n": {"type": "noul"}, "c": {"type": "choice", "criteria": {"x": "", "y": ""}}}


def _valid():
    return {"n": _noul(0.5), "c": {"type": "choice", "choice": "x"}}


class TestCheckAnswers:
    def test_valid_returned_unchanged(self):
        answers = _valid()
        assert _check_answers(answers, QS) is answers

    @pytest.mark.parametrize("value", [0, 1, 0.0, 1.0])
    def test_noul_bounds_inclusive(self, value):
        answers = _valid()
        answers["n"] = _noul(value)
        assert _check_answers(answers, QS) == answers

    @pytest.mark.parametrize("value", [True, False, -0.01, 1.01, "0.5", None, float("nan")])
    def test_bad_noul_is_malformed(self, value):
        answers = _valid()
        answers["n"] = {"type": "noul", "noul": value}
        with pytest.raises(JevError, match="invalid noul"):
            _check_answers(answers, QS)

    @pytest.mark.parametrize("choice", ["z", None, 3, ""])
    def test_invalid_choice_label_is_malformed(self, choice):
        answers = _valid()
        answers["c"] = {"type": "choice", "choice": choice}
        with pytest.raises(JevError, match="invalid choice"):
            _check_answers(answers, QS)

    def test_missing_question_is_malformed(self):
        answers = _valid()
        del answers["c"]
        with pytest.raises(JevError, match="missing answer c"):
            _check_answers(answers, QS)

    @pytest.mark.parametrize("answers", [None, [], "x", 3])
    def test_non_dict_answers(self, answers):
        with pytest.raises(JevError, match="no answers"):
            _check_answers(answers, QS)

    def test_answer_that_is_not_a_dict(self):
        with pytest.raises(JevError, match="missing answer n"):
            _check_answers({"n": 0.5, "c": {"choice": "x"}}, QS)

    def test_extra_answers_are_tolerated(self):
        answers = {**_valid(), "extra": _noul(0.1)}
        assert _check_answers(answers, QS) == answers


# --------------------------------------------------------------------------- client


class TestClient:
    def test_fresh_call_shape_cost_and_cache_file(self, tmp_path):
        post = FakeJev()
        client = _client(tmp_path, post)
        got = client.ask(STATE, SOURCE_QUESTIONS)
        assert got["source"] == "jev" and got["cost_usd"] == 0.0001 and got["latency_s"] is not None
        assert set(got["answers"]) == set(SOURCE_QUESTIONS)
        assert post.count == 1
        call = post.calls[0]
        assert call["url"] == JEV_URL
        assert call["body"]["model"] == "typesafe/jev-1.13"
        assert call["body"]["state"] == STATE and call["body"]["questions"] == SOURCE_QUESTIONS
        assert call["headers"]["Authorization"] == f"Bearer {KEY}"
        assert call["timeout"] == JevSettings().timeout_s
        assert client.calls == 1 and client.spent_usd == pytest.approx(0.0001)
        files = list((tmp_path / "jevcache").glob("*.json"))
        assert [f.stem for f in files] == [client.cache_key(STATE, SOURCE_QUESTIONS)]
        cached = json.loads(files[0].read_text())
        assert cached["model"] == "typesafe/jev-1.13" and cached["served_by"] == "typesafe/jev-1.13-test"
        assert cached["usage"] == {"cost": 0.0001, "input_tokens": 1000}
        assert KEY not in files[0].read_text()

    def test_cache_hit_makes_no_call_and_costs_nothing(self, tmp_path):
        post = FakeJev()
        first = _client(tmp_path, post).ask(STATE, SOURCE_QUESTIONS)
        # A new client, no key, no port: the cache still answers.
        second_client = JevClient(api_key="", cache_dir=tmp_path / "jevcache", settings=JevSettings(), post=None)
        second = second_client.ask(STATE, SOURCE_QUESTIONS)
        assert post.count == 1
        assert second["source"] == "jev_cache" and second["cost_usd"] == 0.0 and second["latency_s"] is None
        assert second["answers"] == first["answers"]
        assert second_client.spent_usd == 0.0 and second_client.calls == 0

    def test_cache_key_depends_on_model_state_and_questions(self, tmp_path):
        client = _client(tmp_path, FakeJev())
        base = client.cache_key(STATE, SOURCE_QUESTIONS)
        assert base == client.cache_key(json.loads(json.dumps(STATE)), dict(SOURCE_QUESTIONS))
        assert base != client.cache_key({"video": {"title": "b"}}, SOURCE_QUESTIONS)
        assert base != client.cache_key(STATE, NOTE_QUESTIONS)
        edited = {**SOURCE_QUESTIONS, "subject": {**SOURCE_QUESTIONS["subject"], "instructions": "changed"}}
        assert base != client.cache_key(STATE, edited)
        assert len(base) == 64

    def test_different_state_is_a_cache_miss(self, tmp_path):
        post = FakeJev()
        client = _client(tmp_path, post)
        client.ask({"video": {"title": "a"}}, SOURCE_QUESTIONS)
        client.ask({"video": {"title": "b"}}, SOURCE_QUESTIONS)
        assert post.count == 2

    def test_corrupt_or_invalid_cache_entry_is_ignored_and_rewritten(self, tmp_path):
        post = FakeJev()
        client = _client(tmp_path, post)
        path = tmp_path / "jevcache" / f"{client.cache_key(STATE, SOURCE_QUESTIONS)}.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert client.ask(STATE, SOURCE_QUESTIONS)["source"] == "jev"
        path.write_text(json.dumps({"answers": {"subject": _noul(7)}}))
        assert client.ask(STATE, SOURCE_QUESTIONS)["source"] == "jev"
        assert client.ask(STATE, SOURCE_QUESTIONS)["source"] == "jev_cache"
        assert post.count == 2

    def test_post_none_is_fallback_no_port(self, tmp_path):
        got = _client(tmp_path, None).ask(STATE, SOURCE_QUESTIONS)
        assert got == {"answers": None, "source": "fallback_no_port", "cost_usd": 0.0, "latency_s": None}
        assert not (tmp_path / "jevcache").exists()

    def test_no_key_is_fallback_no_key_without_calls(self, tmp_path):
        post = FakeJev()
        got = _client(tmp_path, post, key="").ask(STATE, SOURCE_QUESTIONS)
        assert got["answers"] is None and got["source"] == "fallback_no_key"
        assert post.count == 0

    def test_no_port_wins_over_no_key(self, tmp_path):
        assert _client(tmp_path, None, key="").ask(STATE, SOURCE_QUESTIONS)["source"] == "fallback_no_port"

    @pytest.mark.parametrize("failure", [JevError("HTTP 500"), TimeoutError("slow"), ValueError("weird"), OSError("x")])
    def test_exceptions_are_fallback_error(self, tmp_path, failure):
        client = _client(tmp_path, FakeJev(fail=failure))
        got = client.ask(STATE, SOURCE_QUESTIONS)
        assert got["answers"] is None and got["source"] == "fallback_error" and got["cost_usd"] == 0.0
        if isinstance(failure, JevError):
            assert got["error"] == "HTTP 500"
        else:
            assert got["error"] == type(failure).__name__  # never str(): could hold secrets
        assert client.calls == 0 and client.spent_usd == 0.0
        assert not list((tmp_path / "jevcache").glob("*.json")) if (tmp_path / "jevcache").exists() else True

    @pytest.mark.parametrize("response", [
        None,
        [],
        {"answers": None},
        {"answers": {"subject": _noul(0.5)}},  # missing questions
        {"answers": {**answers_for(SOURCE_QUESTIONS), "usable": _noul(True)}},  # bool noul
        {"answers": {**answers_for(SOURCE_QUESTIONS), "usable": _noul(1.5)}},
        {"answers": {**answers_for(SOURCE_QUESTIONS), "kind": {"type": "choice", "choice": "nonsense"}}},
    ])
    def test_malformed_responses_are_fallback_error_and_not_cached(self, tmp_path, response):
        client = _client(tmp_path, lambda url, body, headers, timeout: response)
        got = client.ask(STATE, SOURCE_QUESTIONS)
        assert got["answers"] is None and got["source"] == "fallback_error" and got["error"]
        assert client.calls == 0
        assert not list((tmp_path / "jevcache").glob("*.json")) if (tmp_path / "jevcache").exists() else True

    def test_error_text_never_contains_key(self, tmp_path):
        client = _client(tmp_path, FakeJev(fail=ValueError(f"Bearer {KEY}")))
        got = client.ask(STATE, SOURCE_QUESTIONS)
        assert KEY not in json.dumps(got)

    def test_budget_stops_further_calls(self, tmp_path):
        post = FakeJev(cost=0.0001)
        client = _client(tmp_path, post, JevSettings(max_usd_per_run=0.0002))
        sources = [client.ask({"video": {"title": t}}, SOURCE_QUESTIONS)["source"] for t in "abcd"]
        assert sources == ["jev", "jev", "fallback_budget", "fallback_budget"]
        assert post.count == 2
        assert client.spent_usd == pytest.approx(0.0002)

    def test_budget_exhausted_still_serves_the_cache(self, tmp_path):
        post = FakeJev(cost=0.0002)
        client = _client(tmp_path, post, JevSettings(max_usd_per_run=0.0002))
        assert client.ask({"video": {"title": "a"}}, SOURCE_QUESTIONS)["source"] == "jev"
        assert client.ask({"video": {"title": "b"}}, SOURCE_QUESTIONS)["source"] == "fallback_budget"
        again = client.ask({"video": {"title": "a"}}, SOURCE_QUESTIONS)
        assert again["source"] == "jev_cache" and again["cost_usd"] == 0.0
        assert post.count == 1

    def test_missing_usage_costs_zero(self, tmp_path):
        client = _client(tmp_path, lambda url, body, headers, timeout: {"answers": answers_for(NOTE_QUESTIONS)})
        got = client.ask(STATE, NOTE_QUESTIONS)
        assert got["source"] == "jev" and got["cost_usd"] == 0.0 and client.spent_usd == 0.0 and client.calls == 1

    def test_ask_many_preserves_order_under_concurrency(self, tmp_path):
        titles = [f"t{i}" for i in range(8)]
        # Earlier jobs sleep longer so completion order is the reverse of submission order.
        post = FakeJev(delay=lambda body: 0.02 * (8 - titles.index(body["state"]["video"]["title"])),
                       hit_p={t: 0.1 * (i + 1) for i, t in enumerate(titles)})
        client = _client(tmp_path, post, JevSettings(workers=4))
        got = client.ask_many([({"video": {"title": t}}, SOURCE_QUESTIONS) for t in titles])
        assert [source_score(g["answers"]) for g in got] == [pytest.approx(0.1 * (i + 1), abs=1e-3) for i in range(8)]
        assert post.count == 8

    def test_ask_many_empty(self, tmp_path):
        assert _client(tmp_path, FakeJev()).ask_many([]) == []

    def test_key_is_not_written_to_any_file(self, tmp_path):
        client = _client(tmp_path, FakeJev())
        client.ask(STATE, SOURCE_QUESTIONS)
        client.ask(STATE, NOTE_QUESTIONS)
        texts = _all_files_text(tmp_path)
        assert texts and all(KEY not in text for _, text in texts)


# --------------------------------------------------------------------------- states


def _brief():
    return valid_brief()


class TestStates:
    def test_brief_context(self):
        brief = _brief()
        brief["geography"] = "Peru"
        brief["scene"]["action"] = "grazing"
        brief["scene"]["excluded"] = ["people"]
        assert brief_context(brief) == {
            "request": "clips of alpacas in a field", "subjects": ["alpaca"], "action": "grazing",
            "setting": "outdoor field or pasture", "place": "Peru", "must_not_show": ["people"],
        }

    def test_brief_context_minimal(self):
        assert brief_context({}) == {"request": None, "subjects": [], "action": None, "setting": None,
                                     "place": None, "must_not_show": []}
        assert brief_context({"request_text": "x", "scene": {"subjects": ["not-a-dict"], "excluded": None}})["subjects"] == []

    def test_hit_state_snippet_is_capped_and_channel_falls_back_to_uploader(self):
        hit = {"title": "T", "uploader": "U", "duration": 90, "view_count": 5, "description": "  " + "x" * 400 + "  "}
        state = hit_state(_brief(), hit)
        assert state["video"] == {"title": "T", "channel": "U", "duration_s": 90, "views": 5,
                                  "description": "x" * SNIPPET_MAX_CHARS}
        assert SNIPPET_MAX_CHARS == 300
        assert state["brief"] == brief_context(_brief())
        assert hit_state(_brief(), {"title": "T", "channel": "C", "uploader": "U"})["video"]["channel"] == "C"
        assert hit_state(_brief(), {"title": "T"})["video"]["description"] == ""
        assert len(hit_state(_brief(), {"description": "y" * 300})["video"]["description"]) == 300

    def test_metadata_state_description_truncation(self):
        exact = metadata_state(_brief(), {"description": "d" * DESCRIPTION_MAX_CHARS})["video"]["description"]
        assert exact == "d" * 1200
        long = metadata_state(_brief(), {"description": "d" * 1201})["video"]["description"]
        assert long == "d" * 1200 + " …"
        assert metadata_state(_brief(), {})["video"]["description"] == ""

    def test_metadata_state_caps_tags_and_chapters(self):
        info = {"tags": [f"t{i}" for i in range(40)],
                "chapters": [{"title": f"c{i}"} for i in range(60)] + ["junk"]}
        video = metadata_state(_brief(), info)["video"]
        assert video["tags"] == [f"t{i}" for i in range(25)]
        assert video["chapters"] == [f"c{i}" for i in range(40)]
        assert metadata_state(_brief(), {"chapters": ["junk"]})["video"]["chapters"] == []

    def test_metadata_state_best_resolution(self):
        info = {"title": "T", "channel": "C", "duration": 60, "view_count": 7, "upload_date": "20260101",
                "categories": ["Travel"], "formats": [
                    {"vcodec": "avc1", "width": 1280, "height": 720},
                    {"vcodec": "vp9", "width": 1920, "height": 1080},
                    {"vcodec": "none", "width": 3840, "height": 2160},  # audio row: ignored
                    {"width": 3840, "height": 2160},  # no vcodec key: treated as none
                    {"vcodec": "avc1", "width": 100, "height": None},
                    "junk",
                ]}
        video = metadata_state(_brief(), info)["video"]
        assert video["best_resolution"] == "1920x1080"
        assert (video["title"], video["channel"], video["duration_s"], video["views"]) == ("T", "C", 60, 7)
        assert video["upload_date"] == "20260101" and video["categories"] == ["Travel"]
        assert metadata_state(_brief(), {"formats": []})["video"]["best_resolution"] is None
        assert metadata_state(_brief(), {})["video"]["best_resolution"] is None
        tie = metadata_state(_brief(), {"formats": [{"vcodec": "a", "width": 1000, "height": 720},
                                                    {"vcodec": "a", "width": 1280, "height": 720}]})
        assert tie["video"]["best_resolution"] == "1280x720"

    def test_states_differ_between_hit_and_metadata(self):
        assert "best_resolution" not in hit_state(_brief(), {"title": "T"})["video"]
        assert "best_resolution" in metadata_state(_brief(), {"title": "T"})["video"]

    def test_note_state(self):
        entry = {"scene_type": "field", "geo": "uncertain", "note": "alpacas", "match": "keep", "excerpt_index": 0}
        state = note_state(_brief(), entry)
        assert state["vision"] == {"scene_type": "field", "geo": "uncertain", "note": "alpacas"}
        assert state["brief"] == brief_context(_brief())


# --------------------------------------------------------------------------- score / compact


class TestScoreAndCompact:
    def test_source_score_is_mean_of_usable_subject_conditions(self):
        answers = answers_for(SOURCE_QUESTIONS)
        answers["usable"], answers["subject"], answers["conditions"] = _noul(0.9), _noul(0.6), _noul(0.3)
        answers["place"] = _choice("different", SOURCE_QUESTIONS["place"]["criteria"])  # not part of the score
        assert source_score(answers) == 0.6
        assert jev.SCORE_QUESTIONS == ("usable", "subject", "conditions")

    def test_source_score_rounds_to_four_places(self):
        answers = answers_for(SOURCE_QUESTIONS)
        answers["usable"], answers["subject"], answers["conditions"] = _noul(1.0), _noul(0.0), _noul(0.0)
        assert source_score(answers) == 0.3333

    def test_compact(self):
        answers = {"n": _noul(0.4), "c": _choice("x", ["x", "y"]), "s": {"type": "score", "score": 3.0, "confidence": 0.5,
                                                                        "probabilities": {"3": 1.0}},
                   "bad": "not-a-dict"}
        out = compact(answers)
        assert out["n"] == 0.4
        assert out["c"] == {"choice": "x", "confidence": 0.9, "probabilities": {"x": 0.9, "y": pytest.approx(0.1)}}
        assert out["s"] == {"score": 3.0, "confidence": 0.5, "probabilities": {"3": 1.0}}
        assert "bad" not in out


# --------------------------------------------------------------------------- DiscoveryJudge


def _judge(tmp_path, post, **settings):
    s = JevSettings(rank=True, **settings)
    client = JevClient(api_key=KEY, cache_dir=tmp_path / "jevcache", settings=s, post=post)
    return DiscoveryJudge(_brief(), client, s)


class TestDiscoveryJudge:
    def test_order_hits_by_score_desc_with_stable_ties(self, tmp_path):
        post = FakeJev(hit_p={"low": 0.2, "high": 0.9, "mid": 0.5})
        judge = _judge(tmp_path, post)
        hits = {"a": {"title": "low a"}, "b": {"title": "high b"}, "c": {"title": "mid c"},
                "d": {"title": "high d"}, "e": {"title": "mid e"}}
        assert judge.order_hits(list("abcde"), hits) == ["b", "d", "c", "e", "a"]
        assert [h["video_id"] for h in judge.hits] == list("abcde")  # recorded in search order
        assert [h["search_index"] for h in judge.hits] == [0, 1, 2, 3, 4]
        assert judge.hits[1]["score"] == 0.9 and judge.hits[1]["source"] == "jev"

    def test_unanswered_hits_are_neutral_and_keep_search_order(self, tmp_path):
        judge = _judge(tmp_path, None)
        hits = {v: {"title": v} for v in "abc"}
        assert judge.order_hits(["c", "a", "b"], hits) == ["c", "a", "b"]
        assert all(h["score"] is None and h["source"] == "fallback_no_port" and h["answers"] is None for h in judge.hits)

    def test_unanswered_hit_ranks_between_high_and_low(self, tmp_path):
        # "u" hits the budget cap after two answered calls, so it is scored NEUTRAL_SCORE = 0.5.
        post = FakeJev(hit_p={"hi": 0.9, "lo": 0.1}, cost=0.0001)
        judge = _judge(tmp_path, post, max_usd_per_run=0.0002, workers=1)
        hits = {"x": {"title": "lo"}, "y": {"title": "hi"}, "u": {"title": "unanswered"}}
        assert judge.order_hits(["x", "y", "u"], hits) == ["y", "u", "x"]
        assert judge.hits[2]["source"] == "fallback_budget" and judge.hits[2]["score"] is None
        assert NEUTRAL_SCORE == 0.5

    def test_order_hits_handles_ids_missing_from_hits_by_id(self, tmp_path):
        post = FakeJev()
        judge = _judge(tmp_path, post)
        assert judge.order_hits(["zz"], {}) == ["zz"]
        assert post.calls[0]["body"]["state"]["video"]["title"] is None

    def test_order_hits_sends_hit_state_and_source_questions(self, tmp_path):
        post = FakeJev()
        judge = _judge(tmp_path, post)
        judge.order_hits(["a"], {"a": {"title": "A", "description": "d" * 500}})
        body = post.calls[0]["body"]
        assert body["questions"] == SOURCE_QUESTIONS
        assert len(body["state"]["video"]["description"]) == 300

    @pytest.mark.parametrize("p, keep", [(0.34, False), (0.35, False), (0.36, True), (0.0, False), (1.0, True)])
    def test_judge_metadata_boundary_score_at_reject_below_rejects(self, tmp_path, p, keep):
        judge = _judge(tmp_path, FakeJev(meta_p={"T": p}))
        info = {"title": "T", "formats": []}
        assert judge.judge_metadata("v", info) is keep
        rec = judge.candidates["v"]
        assert rec["decision"] == ("keep" if keep else "reject") and rec["score"] == p and rec["source"] == "jev"

    def test_judge_metadata_uses_custom_reject_below(self, tmp_path):
        judge = _judge(tmp_path, FakeJev(meta_p={"T": 0.5}), reject_below=0.5)
        assert judge.judge_metadata("v", {"title": "T"}) is False

    def test_judge_metadata_keeps_when_unanswered(self, tmp_path):
        judge = _judge(tmp_path, FakeJev(fail=TimeoutError("x")))
        assert judge.judge_metadata("v", {"title": "T"}) is True
        rec = judge.candidates["v"]
        assert rec["score"] is None and rec["decision"] == "keep" and rec["source"] == "fallback_error"
        assert rec["error"] == "TimeoutError"

    def test_order_candidates(self, tmp_path):
        class Cand:
            def __init__(self, video_id):
                self.video_id = video_id

        judge = _judge(tmp_path, FakeJev(meta_p={"lo": 0.4, "hi": 0.95}))
        for vid, title in [("a", "lo"), ("b", "hi"), ("c", "other")]:
            judge.judge_metadata(vid, {"title": title})
        # c scored the 0.6 default; d was never judged (no record) -> neutral 0.5; e likewise.
        ordered = judge.order_candidates([Cand(v) for v in "adbec"])
        assert [c.video_id for c in ordered] == ["b", "c", "d", "e", "a"]

    def test_report(self, tmp_path):
        post = FakeJev(hit_p={"bad": 0.1}, meta_p={"bad": 0.1}, cost=0.0001)
        judge = _judge(tmp_path, post)
        judge.order_hits(["a", "b"], {"a": {"title": "ok"}, "b": {"title": "bad"}})
        judge.judge_metadata("a", {"title": "ok"})
        judge.judge_metadata("b", {"title": "bad"})
        report = judge.report()
        assert report["schema_version"] == "jev_rank_v1"
        assert report["model"] == JEV_MODEL and report["questions_version"] == jev.SOURCE_QUESTIONS_VERSION
        assert report["reject_below"] == 0.35
        assert report["counts"] == {"hits": 2, "candidates_judged": 2, "rejected": 1, "by_source": {"jev": 4}}
        assert report["calls"] == 4 and report["cost_usd"] == pytest.approx(0.0004)
        assert [h["video_id"] for h in report["hits"]] == ["a", "b"]
        assert [c["decision"] for c in report["candidates"]] == ["keep", "reject"]
        json.dumps(report)  # serialisable

    def test_report_counts_fallback_sources(self, tmp_path):
        judge = _judge(tmp_path, None)
        judge.order_hits(["a"], {"a": {"title": "x"}})
        judge.judge_metadata("a", {"title": "x"})
        assert judge.report()["counts"]["by_source"] == {"fallback_no_port": 2}
        assert judge.report()["counts"]["rejected"] == 0


# --------------------------------------------------------------------------- apply_note_check


def _scores():
    return {
        "excerpts_sha256": "e" * 64,
        "vid00000001": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "field", "note": "alpacas grazing"},
            {"excerpt_index": 1, "match": "keep", "geo": "supported", "scene_type": "town", "note": "VIOLATION town"},
            {"excerpt_index": 2, "match": "reject", "geo": "supported", "scene_type": "town", "note": "VIOLATION x"},
            {"excerpt_index": 3, "match": "uncertain", "geo": "uncertain", "scene_type": "x", "note": "VIOLATION y"},
        ],
        "vid00000002": [
            {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "field", "note": "BORDER note"},
        ],
    }


def _note_client(tmp_path, post, **settings):
    s = JevSettings(note_check=True, **settings)
    return JevClient(api_key=KEY, cache_dir=tmp_path / "jevcache", settings=s, post=post), s


class TestApplyNoteCheck:
    def test_only_keep_entries_are_checked_and_flagged(self, tmp_path):
        post = FakeJev(note_p={"VIOLATION": 0.95})
        client, settings = _note_client(tmp_path, post)
        scores, report = apply_note_check(_brief(), _scores(), client, settings)
        assert post.count == 3  # the three match == keep entries only
        e0, e1, e2, e3 = scores["vid00000001"]
        assert e0["note_check"] == {"p_violation": 0.1, "source": "jev"} and e0["note_violation"] is False
        assert e1["note_check"] == {"p_violation": 0.95, "source": "jev"} and e1["note_violation"] is True
        assert "note_check" not in e2 and "note_violation" not in e2
        assert "note_check" not in e3 and "note_violation" not in e3
        assert scores["excerpts_sha256"] == "e" * 64  # non-list values untouched
        assert all(c["body"]["questions"] == NOTE_QUESTIONS for c in post.calls)

    def test_report(self, tmp_path):
        post = FakeJev(note_p={"VIOLATION": 0.95})
        client, settings = _note_client(tmp_path, post)
        _, report = apply_note_check(_brief(), _scores(), client, settings)
        assert report["schema_version"] == "jev_notes_v1" and report["model"] == JEV_MODEL
        assert report["questions_version"] == jev.NOTE_QUESTIONS_VERSION and report["reject_at"] == 0.70
        assert report["counts"] == {"checked": 3, "violations": 1, "unanswered": 0}
        assert report["calls"] == 3 and report["cost_usd"] == pytest.approx(0.0003)
        assert [(r["video_id"], r["excerpt_index"], r["violation"]) for r in report["entries"]] == [
            ("vid00000001", 0, False), ("vid00000001", 1, True), ("vid00000002", 0, False)]

    @pytest.mark.parametrize("p, flagged", [(0.69, False), (0.70, True), (0.71, True)])
    def test_boundary_p_equal_to_note_reject_at_flags(self, tmp_path, p, flagged):
        client, settings = _note_client(tmp_path, FakeJev(note_p={"BORDER": p}))
        scores, _ = apply_note_check(_brief(), _scores(), client, settings)
        entry = scores["vid00000002"][0]
        assert entry["note_check"]["p_violation"] == p
        assert entry["note_violation"] is flagged

    def test_custom_note_reject_at(self, tmp_path):
        client, settings = _note_client(tmp_path, FakeJev(note_p={"BORDER": 0.5}), note_reject_at=0.5)
        scores, _ = apply_note_check(_brief(), _scores(), client, settings)
        assert scores["vid00000002"][0]["note_violation"] is True

    def test_unanswered_entries_get_no_note_violation_key(self, tmp_path):
        client, settings = _note_client(tmp_path, None)
        scores, report = apply_note_check(_brief(), _scores(), client, settings)
        for entry in (scores["vid00000001"][0], scores["vid00000001"][1], scores["vid00000002"][0]):
            assert entry["note_check"] == {"p_violation": None, "source": "fallback_no_port"}
            assert "note_violation" not in entry
        assert report["counts"] == {"checked": 3, "violations": 0, "unanswered": 3}
        assert all(r["violation"] is None for r in report["entries"])

    def test_error_is_recorded_in_report(self, tmp_path):
        client, settings = _note_client(tmp_path, FakeJev(fail=ValueError("x")))
        _, report = apply_note_check(_brief(), _scores(), client, settings)
        assert all(r["source"] == "fallback_error" and r["error"] == "ValueError" for r in report["entries"])

    def test_nothing_to_check(self, tmp_path):
        post = FakeJev()
        client, settings = _note_client(tmp_path, post)
        scores, report = apply_note_check(_brief(), {"excerpts_sha256": "e" * 64, "v": []}, client, settings)
        assert post.count == 0 and report["counts"] == {"checked": 0, "violations": 0, "unanswered": 0}
        assert scores == {"excerpts_sha256": "e" * 64, "v": []}

    def test_report_is_json_and_has_no_key(self, tmp_path):
        client, settings = _note_client(tmp_path, FakeJev())
        _, report = apply_note_check(_brief(), _scores(), client, settings)
        assert KEY not in json.dumps(report)


# --------------------------------------------------------------------------- run_dry with a judge


def _ids(n):
    return [f"id{i:09d}" for i in range(1, n + 1)]


def _dry(tmp_path, titles, *, judge=None, max_candidates=None, live=(), max_fetches=20, name="cache"):
    ids = _ids(len(titles))
    hits = [{"id": vid, "title": title} for vid, title in zip(ids, titles)]
    meta = {vid: _hd_info(vid, title) for vid, title in zip(ids, titles)}
    for vid in live:
        meta[vid] = {**meta[vid], "live_status": "is_live"}
    yt = FakeYt(hits, meta)
    constraint = Constraint(theme_text="alpacas", limits=RunLimits(max_search_results=20,
                                                                     max_metadata_fetches=max_fetches, sleep_s=0.0))
    result = run_dry(constraint, yt=yt, cache=MetadataCache(tmp_path / name), sleep_fn=lambda _s: None,
                     queries=["alpacas"], max_candidates=max_candidates, judge=judge)
    return result, yt, ids


class TestRunDryJudge:
    def test_fetch_order_follows_the_judge(self, tmp_path):
        judge = _judge(tmp_path, FakeJev(hit_p={"first": 0.2, "second": 0.9, "third": 0.6}, default=0.9))
        result, yt, ids = _dry(tmp_path, ["first", "second", "third"], judge=judge)
        assert yt.metadata_calls == [ids[1], ids[2], ids[0]]
        assert f"jev order: {ids[1]} {ids[2]} {ids[0]}" in result.log_lines
        # the log line comes before the first metadata decision
        assert result.log_lines.index(f"jev order: {ids[1]} {ids[2]} {ids[0]}") < next(
            i for i, line in enumerate(result.log_lines) if line.startswith("keep "))

    def test_order_line_is_logged_once_and_only_with_a_judge(self, tmp_path):
        plain, yt, ids = _dry(tmp_path, ["a", "b"], name="plain")
        assert not any(line.startswith("jev order") for line in plain.log_lines)
        assert yt.metadata_calls == ids

    def test_jev_reject_is_recorded_and_does_not_count_toward_max_candidates(self, tmp_path):
        # Hits look equally good (search order kept); after metadata the first one is rejected.
        judge = _judge(tmp_path, FakeJev(hit_p={"A": 0.9, "B": 0.8, "C": 0.7, "D": 0.6},
                                         meta_p={"A": 0.1, "B": 0.8, "C": 0.7, "D": 0.6}))
        result, yt, ids = _dry(tmp_path, ["A", "B", "C", "D"], judge=judge, max_candidates=2)
        assert yt.metadata_calls == ids[:3]  # A rejected, B and C kept, D never fetched
        assert [(r.video_id, r.reason) for r in result.rejected] == [(ids[0], "jev_reject")]
        assert result.rejected[0].title == "A"
        assert [c.video_id for c in result.candidates] == [ids[1], ids[2]]
        assert result.stopped_reason == "enough_candidates"
        assert "stop: 2 candidates kept" in result.log_lines
        assert any(line.startswith(f"reject {ids[0]}") and "jev_reject" in line for line in result.log_lines)
        assert judge.candidates[ids[0]]["decision"] == "reject"

    def test_score_exactly_at_reject_below_is_rejected(self, tmp_path):
        judge = _judge(tmp_path, FakeJev(meta_p={"edge": 0.35, "above": 0.36}))
        result, _, ids = _dry(tmp_path, ["edge", "above"], judge=judge)
        assert [r.video_id for r in result.rejected] == [ids[0]]
        assert [c.video_id for c in result.candidates] == [ids[1]]

    def test_final_candidates_are_ordered_by_score(self, tmp_path):
        judge = _judge(tmp_path, FakeJev(meta_p={"A": 0.5, "B": 0.9, "C": 0.7, "D": 0.7}))  # hits all 0.6 default
        result, yt, ids = _dry(tmp_path, ["A", "B", "C", "D"], judge=judge)
        assert yt.metadata_calls == ids  # equal hit scores keep search order
        assert [c.video_id for c in result.candidates] == [ids[1], ids[2], ids[3], ids[0]]  # stable tie C, D

    def test_metadata_rule_rejects_never_reach_the_judge(self, tmp_path):
        post = FakeJev()
        judge = _judge(tmp_path, post)
        ids = _ids(2)
        result, _, _ = _dry(tmp_path, ["live one", "fine"], judge=judge, live=[ids[0]])
        assert [(r.video_id, r.reason) for r in result.rejected] == [(ids[0], "live")]
        assert list(judge.candidates) == [ids[1]]
        # 2 hit calls + 1 metadata call (the live video is not judged)
        assert post.count == 3

    def test_unanswered_judge_changes_nothing(self, tmp_path):
        plain, plain_yt, _ = _dry(tmp_path, ["A", "B", "C"], name="plain")
        judge = _judge(tmp_path, None)
        judged, judged_yt, _ = _dry(tmp_path, ["A", "B", "C"], judge=judge, name="judged")
        assert judged_yt.metadata_calls == plain_yt.metadata_calls
        assert [c.video_id for c in judged.candidates] == [c.video_id for c in plain.candidates]
        assert judged.rejected == plain.rejected

    def test_judge_none_is_unchanged_and_max_candidates_none_is_unbounded(self, tmp_path):
        result, yt, ids = _dry(tmp_path, ["A", "B", "C"])
        assert [c.video_id for c in result.candidates] == ids and yt.metadata_calls == ids
        assert result.stopped_reason == "complete"
        capped, _, _ = _dry(tmp_path, ["A", "B", "C"], max_candidates=1, name="capped")
        assert [c.video_id for c in capped.candidates] == ids[:1]
        assert capped.stopped_reason == "enough_candidates"

    def test_judge_not_asked_when_search_failed(self, tmp_path):
        post = FakeJev()
        judge = _judge(tmp_path, post)

        class BrokenYt(FakeYt):
            def search(self, query, limit):
                raise RuntimeError("blocked")

        constraint = Constraint(theme_text="alpacas", limits=RunLimits(max_search_results=5, sleep_s=0.0))
        result = run_dry(constraint, yt=BrokenYt([], {}), cache=MetadataCache(tmp_path / "c"),
                         sleep_fn=lambda _s: None, queries=["alpacas"], judge=judge)
        assert result.stopped_reason == "search_error" and post.count == 0

    def test_cached_metadata_is_still_judged(self, tmp_path):
        post = FakeJev(meta_p={"A": 0.1})
        judge = _judge(tmp_path, post)
        _dry(tmp_path, ["A"], judge=judge, name="shared")
        judge2 = _judge(tmp_path, FakeJev(meta_p={"A": 0.1}), max_usd_per_run=0.25)
        result, yt, ids = _dry(tmp_path, ["A"], judge=judge2, name="shared")
        assert yt.metadata_calls == []  # metadata cache hit
        assert [r.reason for r in result.rejected] == ["jev_reject"]


# --------------------------------------------------------------------------- shortlist


def _shortlist_case(entries_0, extra=None):
    rows = _rows_fixture()
    review = _review_fixture(rows, _unique_hashes(rows))
    labels = {"vid00000001": entries_0, **(extra or {})}
    return rows, review, labels


def _keep(index, **extra):
    return {"excerpt_index": index, "match": "keep", "geo": "supported", "scene_type": "mountains", **extra}


class TestShortlistNoteViolation:
    def test_flagged_keep_is_excluded_with_reason(self):
        rows, review, labels = _shortlist_case([_keep(0, note_violation=True), _keep(1, note_violation=False),
                                                _keep(2)])
        doc = build_shortlist(rows, review, labels, 20, _bindings())
        assert REASON_NOTE_VIOLATION == "jev note violation"
        selected = {(s["video_id"], s["excerpt_index"]) for s in doc["selected"]}
        assert selected == {("vid00000001", 1), ("vid00000001", 2)}
        excluded = {(e["video_id"], e["excerpt_index"]): e for e in doc["excluded"]}
        assert excluded[("vid00000001", 0)]["reasons"] == ["jev note violation"]
        assert doc["counts"]["n_excluded_by_reason"]["jev note violation"] == 1

    def test_extra_note_check_dict_is_accepted(self):
        rows, review, labels = _shortlist_case([_keep(0, note_violation=False,
                                                      note_check={"p_violation": 0.1, "source": "jev"})])
        doc = build_shortlist(rows, review, labels, 20, _bindings())
        assert doc["counts"]["n_selected"] == 1

    def test_reason_precedence_match_and_geo_come_first(self):
        rows, review, labels = _shortlist_case([
            {"excerpt_index": 0, "match": "reject", "geo": "supported", "scene_type": "x", "note_violation": True},
            {"excerpt_index": 1, "match": "uncertain", "geo": "supported", "scene_type": "x", "note_violation": True},
            {"excerpt_index": 2, "match": "keep", "geo": "conflicting", "scene_type": "x", "note_violation": True},
        ])
        doc = build_shortlist(rows, review, labels, 20, _bindings())
        excluded = {e["excerpt_index"]: e["reasons"] for e in doc["excluded"] if e["video_id"] == "vid00000001"}
        assert excluded[0] == ["visual match rejected"]
        assert excluded[1] == ["visual match uncertain"]
        assert excluded[2] == ["geographic evidence conflicting"]
        assert doc["counts"]["n_selected"] == 0

    def test_geo_uncertain_flag_unaffected_by_false_note_violation(self):
        rows, review, labels = _shortlist_case([
            {"excerpt_index": 0, "match": "keep", "geo": "uncertain", "scene_type": "x", "note_violation": False}])
        doc = build_shortlist(rows, review, labels, 20, _bindings())
        assert doc["selected"][0]["flags"] == ["geo_uncertain"]

    @pytest.mark.parametrize("value", ["yes", "true", 1, 0, None, [], {}])
    def test_note_violation_must_be_bool(self, value):
        entry = {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "x", "note_violation": value}
        with pytest.raises(ShortlistInputError, match="note_violation"):
            validate_label_payload({"abcdefghijk": [entry]})

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_note_violation_validates(self, value):
        payload = {"abcdefghijk": [{"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "x",
                                    "note_violation": value}]}
        assert validate_label_payload(payload) == payload


# --------------------------------------------------------------------------- config


class TestConfigKeys:
    def _load(self, tmp_path, text):
        (tmp_path / "config.yaml").write_text(text)
        return load_project_config(tmp_path)

    def test_new_keys_load(self, tmp_path):
        cfg = self._load(tmp_path, "jev_rank: true\njev_note_check: true\njev_note_reject_at: 0.8\n")
        assert cfg["jev_rank"] is True and cfg["jev_note_check"] is True and cfg["jev_note_reject_at"] == 0.8
        s = JevSettings.from_config(cfg)
        assert s.rank and s.note_check and s.note_reject_at == 0.8

    @pytest.mark.parametrize("key", ["jev_rank", "jev_note_check"])
    @pytest.mark.parametrize("bad", ["yes", "1", "0"])
    def test_bool_keys_reject_non_booleans(self, tmp_path, key, bad):
        with pytest.raises(ConfigError, match="boolean"):
            self._load(tmp_path, f"{key}: {bad}\n")

    @pytest.mark.parametrize("value", ["-0.1", "1.5", "true", "nan"])
    def test_note_reject_at_must_be_a_probability(self, tmp_path, value):
        with pytest.raises(ConfigError):
            self._load(tmp_path, f"jev_note_reject_at: {value}\n")

    @pytest.mark.parametrize("value, ok", [("0", True), ("1", True), ("0.7", True)])
    def test_note_reject_at_bounds_inclusive(self, tmp_path, value, ok):
        assert self._load(tmp_path, f"jev_note_reject_at: {value}\n")["jev_note_reject_at"] == float(value)

    def test_reject_below_default_is_035_and_must_stay_below_keep_above(self, tmp_path):
        assert JevSettings.from_config({}).reject_below == 0.35
        with pytest.raises(ConfigError, match="below"):
            self._load(tmp_path, "jev_keep_above: 0.35\n")
        with pytest.raises(ConfigError, match="below"):
            self._load(tmp_path, "jev_keep_above: 0.3\n")
        assert self._load(tmp_path, "jev_keep_above: 0.36\n")["jev_keep_above"] == 0.36
        # The old default 0.40 would now be a valid explicit value above 0.35 keep_above? No: 0.40 >= 0.36.
        with pytest.raises(ConfigError, match="below"):
            self._load(tmp_path, "jev_reject_below: 0.4\njev_keep_above: 0.36\n")
        assert self._load(tmp_path, "jev_reject_below: 0.4\n")["jev_reject_below"] == 0.4


# --------------------------------------------------------------------------- runner integration

from scenery_brief_clips.runner import _bindings as runner_bindings  # noqa: E402
from scenery_brief_clips.runner import _jev_status, advance  # noqa: E402
from test_brief import valid_plan  # noqa: E402
from test_runner_contract import (  # noqa: E402
    Guard,
    _brief_plan,
    _metadata,
    _patch_media,
    _ports,
    _root,
    _strip_text,
    _tile_text,
)

V_PLAIN, V_GREAT, V_DARK, V_OKAY = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"
TITLES = {V_PLAIN: "alpacas plain", V_GREAT: "great alpacas", V_DARK: "dark alpacas", V_OKAY: "okay alpacas"}
NO_JEV_ORDER = [V_PLAIN, V_GREAT, V_DARK]  # max_rank_videos: 3 in _jroot


class MultiYt:
    """Four search hits; metadata for each; export/media hooks like test_runner_contract.FakeYt."""

    def __init__(self):
        self.searches = 0
        self.exports = 0
        self.fetched: list[str] = []

    def search(self, query, limit):
        self.searches += 1
        return [{"id": vid, "title": title} for vid, title in TITLES.items()][:limit]

    def fetch_metadata(self, video_id):
        self.fetched.append(video_id)
        return {**_metadata(), "id": video_id, "title": TITLES[video_id]}


def _jroot(tmp_path, extra: str = "", ranks: int = 3) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = _root(tmp_path)
    config = root / "config.yaml"
    text = config.read_text(encoding="utf-8").replace("max_rank_videos: 1", f"max_rank_videos: {ranks}")
    config.write_text(text + extra, encoding="utf-8")
    return root


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _discover_only(root, yt, jev_post=None, monkeypatch=None):
    brief_path, plan_path = _brief_plan(root)
    ports = _ports(yt)
    ports.jev_post = jev_post
    result = advance(root, brief=brief_path, plan=plan_path, ports=ports)
    return result, Path(result["run_dir"])


@pytest.fixture()
def guarded(monkeypatch):
    guard = Guard()
    guard.install(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    return guard


class TestRunnerDiscoveryRank:
    def test_rank_orders_by_score_rejects_low_and_records_report(self, tmp_path, guarded):
        root = _jroot(tmp_path, "jev_rank: true\n", ranks=4)
        yt = MultiYt()
        post = FakeJev(hit_p={"dark": 0.2, "great": 0.9}, meta_p={"dark": 0.1, "great": 0.9})
        result, run_dir = _discover_only(root, yt, post)
        assert result["status"] == "paused" and result["stage"] == "agree_vision", result
        assert result["jev"] == {"enabled": True, "rank": True, "note_check": False}
        assert guarded.tripped == []

        # Fetch order follows the hit scores; the dark video is fetched last and rejected.
        assert yt.fetched == [V_GREAT, V_PLAIN, V_OKAY, V_DARK]
        candidates = _json(run_dir / "candidates.json")
        assert [c["video_id"] for c in candidates] == [V_GREAT, V_PLAIN, V_OKAY]
        rejected = _json(run_dir / "rejected.json")
        assert [(r["video_id"], r["reason"]) for r in rejected] == [(V_DARK, "jev_reject")]

        discovery = _json(run_dir / "discovery.json")
        rank = discovery["jev_rank"]
        assert rank["schema_version"] == "jev_rank_v1" and rank["model"] == "typesafe/jev-1.13"
        assert rank["counts"]["hits"] == 4 and rank["counts"]["candidates_judged"] == 4
        assert rank["counts"]["rejected"] == 1 and rank["counts"]["by_source"] == {"jev": 8}
        assert rank["calls"] == 8 and post.count == 8
        assert [c["video_id"] for c in discovery["candidates"]] == [V_GREAT, V_PLAIN, V_OKAY]
        by_id = {c["video_id"]: c for c in rank["candidates"]}
        assert by_id[V_DARK]["decision"] == "reject" and by_id[V_GREAT]["score"] == 0.9
        assert discovery["max_candidates"] == 4

        assert all(call["url"] == jev.JEV_URL and call["body"]["model"] == "typesafe/jev-1.13"
                   and call["headers"]["Authorization"] == f"Bearer {KEY}" for call in post.calls)
        assert any("jev order:" in line for line in (run_dir / "log.txt").read_text().splitlines())

    def test_rank_at_max_candidates_never_fetches_the_worst_hit(self, tmp_path, guarded):
        root = _jroot(tmp_path, "jev_rank: true\n")  # max_rank_videos: 3
        yt = MultiYt()
        post = FakeJev(hit_p={"dark": 0.2, "great": 0.9}, meta_p={"dark": 0.1, "great": 0.9})
        _, run_dir = _discover_only(root, yt, post)
        assert yt.fetched == [V_GREAT, V_PLAIN, V_OKAY]  # the dark hit sorts last and is never fetched
        assert [c["video_id"] for c in _json(run_dir / "candidates.json")] == [V_GREAT, V_PLAIN, V_OKAY]
        assert _json(run_dir / "rejected.json") == []
        assert _json(run_dir / "discovery.json")["stopped_reason"] == "enough_candidates"

    def test_key_never_written_anywhere(self, tmp_path, guarded):
        root = _jroot(tmp_path, "jev_rank: true\n")
        _discover_only(root, MultiYt(), FakeJev())
        texts = _all_files_text(tmp_path)
        assert any(path.name == "discovery.json" for path, _ in texts)
        assert any("jevcache" in str(path) or "/jev/" in str(path) for path, _ in texts)
        assert all(KEY not in text for _, text in texts)

    def test_rerun_uses_jev_cache_for_free(self, tmp_path, guarded):
        root = _jroot(tmp_path, "jev_rank: true\n", ranks=4)
        post = FakeJev(hit_p={"dark": 0.2, "great": 0.9}, meta_p={"dark": 0.1, "great": 0.9})
        _discover_only(root, MultiYt(), post)
        first = post.count
        assert first == 8
        assert len(list((root / "data" / "cache" / "jev").glob("*.json"))) == 8
        # A second run dir in the same project: metadata cache and jev cache both hit.
        post2 = FakeJev(fail=AssertionError("cache must answer"))
        brief_path, plan_path = _brief_plan(root)
        ports = _ports(MultiYt())
        ports.jev_post = post2
        result = advance(root, brief=brief_path, plan=plan_path, ports=ports, run_dir=root / "data" / "runs" / "second")
        assert post2.count == 0
        rank = _json(Path(result["run_dir"]) / "discovery.json")["jev_rank"]
        assert rank["counts"]["by_source"] == {"jev_cache": 8} and rank["cost_usd"] == 0.0

    def test_no_port_falls_back_to_the_no_jev_order(self, tmp_path, guarded):
        root_plain = _jroot(tmp_path / "plain")
        _, plain_dir = _discover_only(root_plain, MultiYt())
        root = _jroot(tmp_path / "jev", "jev_rank: true\n")
        result, run_dir = _discover_only(root, MultiYt(), jev_post=None)
        assert result["status"] == "paused"
        rank = _json(run_dir / "discovery.json")["jev_rank"]
        assert set(rec["source"] for rec in rank["hits"] + rank["candidates"]) == {"fallback_no_port"}
        assert rank["counts"]["by_source"] == {"fallback_no_port": 4 + len(rank["candidates"])}
        assert all(rec["score"] is None for rec in rank["hits"] + rank["candidates"])
        assert rank["calls"] == 0 and rank["cost_usd"] == 0.0 and rank["counts"]["rejected"] == 0
        order = [c["video_id"] for c in _json(run_dir / "candidates.json")]
        assert order == [c["video_id"] for c in _json(plain_dir / "candidates.json")] == NO_JEV_ORDER
        assert _json(run_dir / "rejected.json") == _json(plain_dir / "rejected.json") == []

    def test_no_key_falls_back_even_with_a_port(self, tmp_path, monkeypatch):
        Guard().install(monkeypatch)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        root = _jroot(tmp_path, "jev_rank: true\n")
        post = FakeJev()
        _, run_dir = _discover_only(root, MultiYt(), post)
        assert post.count == 0
        rank = _json(run_dir / "discovery.json")["jev_rank"]
        assert set(rec["source"] for rec in rank["hits"]) == {"fallback_no_key"}
        assert [c["video_id"] for c in _json(run_dir / "candidates.json")] == NO_JEV_ORDER

    def test_without_jev_keys_discovery_has_no_jev_record(self, tmp_path, guarded):
        root = _jroot(tmp_path)
        post = FakeJev()
        result, run_dir = _discover_only(root, MultiYt(), post)
        assert "jev_rank" not in _json(run_dir / "discovery.json")
        assert result["jev"]["enabled"] is False
        assert post.count == 0
        assert not (root / "data" / "cache" / "jev").exists()
        assert "jev order" not in (run_dir / "log.txt").read_text()

    def test_jev_gate_alone_does_not_touch_the_runner(self, tmp_path, guarded):
        # _root() already sets jev_gate: true
        root = _jroot(tmp_path)
        assert "jev_gate: true" in (root / "config.yaml").read_text()
        post = FakeJev()
        result, run_dir = _discover_only(root, MultiYt(), post)
        assert result["jev"] == {"enabled": False, "reason": "config_off"}
        assert post.count == 0 and "jev_rank" not in _json(run_dir / "discovery.json")


# --------------------------------------------------------------------------- runner bindings


def _b(config: dict) -> dict:
    brief = valid_brief()
    return runner_bindings(brief, valid_plan(brief), config, "gpt-6-sol")


class TestRunnerBindings:
    BASE = {"max_rank_videos": 3, "sleep_s": 0.0}

    def test_status_helper(self):
        assert _jev_status({}) == {"enabled": False, "reason": "config_off"}
        assert _jev_status({"jev_gate": True}) == {"enabled": False, "reason": "config_off"}
        assert _jev_status({"jev_rank": True}) == {"enabled": True, "rank": True, "note_check": False}
        assert _jev_status({"jev_note_check": True}) == {"enabled": True, "rank": False, "note_check": True}
        assert _jev_status({"jev_rank": True, "jev_note_check": True})["note_check"] is True

    def test_manual_gate_does_not_change_bindings(self):
        assert _b(self.BASE) == _b({**self.BASE, "jev_gate": True})
        assert _b(self.BASE) == _b({**self.BASE, "jev_gate": True, "jev_order_by_p": False})
        assert _b(self.BASE) == _b({**self.BASE, "jev_rank": False, "jev_note_check": False})

    def test_rank_changes_discover_and_rank_only(self):
        before, after = _b(self.BASE), _b({**self.BASE, "jev_rank": True})
        assert before["discover"] != after["discover"]
        assert before["rank"] != after["rank"]
        assert before["label_tiles"] == after["label_tiles"]
        # analyze does not bind on the discover hash, so it is unchanged too
        assert before["analyze"] == after["analyze"]
        assert before["label_strips"] == after["label_strips"]

    def test_rank_threshold_changes_the_binding(self):
        a = _b({**self.BASE, "jev_rank": True})
        b = _b({**self.BASE, "jev_rank": True, "jev_reject_below": 0.2})
        assert a["discover"] != b["discover"]
        # a threshold change with rank off is irrelevant
        assert _b(self.BASE) == _b({**self.BASE, "jev_reject_below": 0.2})

    def test_note_check_changes_strip_bindings_only(self):
        before, after = _b(self.BASE), _b({**self.BASE, "jev_note_check": True})
        assert before["label_strips"] != after["label_strips"]
        assert before["shortlist_apply"] != after["shortlist_apply"]
        for stage in ("discover", "rank", "label_tiles", "analyze", "verify_review", "shortlist_review", "export"):
            assert before[stage] == after[stage], stage
        assert after["label_strips"] == after["shortlist_apply"]
        other = _b({**self.BASE, "jev_note_check": True, "jev_note_reject_at": 0.9})
        assert other["label_strips"] != after["label_strips"]


# --------------------------------------------------------------------------- runner note check

from test_runner_contract import FakeYt as ContractYt  # noqa: E402


def _note_setup(tmp_path, monkeypatch, extra="jev_note_check: true\n"):
    Guard().install(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    root = _jroot(tmp_path, extra, ranks=1)
    brief_path, plan_path = _brief_plan(root)
    yt = ContractYt()
    yt.base = root / "acq"
    yt.base.mkdir()
    calls = _patch_media(monkeypatch, yt)
    return root, brief_path, plan_path, yt, calls


def _note_strip(note):
    def caller(wire, path, prompt):
        return json.dumps({"match": "keep", "geo": "uncertain", "scene_type": "field", "note": note,
                           "continuity_ok": True})
    return caller


def _run_full(root, brief_path, plan_path, yt, post, strip, **kw):
    ports = _ports(yt, tile_caller=_tile_text, strip_caller=strip)
    ports.jev_post = post
    return advance(root, brief=brief_path, plan=plan_path, vision_agree=True, allow_export=True,
                   theme="contract-theme", ports=ports, **kw)


class TestRunnerNoteCheck:
    def test_flagged_note_is_excluded_and_verify_reproduces_the_shortlist(self, tmp_path, monkeypatch):
        root, brief_path, plan_path, yt, calls = _note_setup(tmp_path, monkeypatch)
        post = FakeJev(note_p={"WRONGSPECIES": 0.95})
        result = _run_full(root, brief_path, plan_path, yt, post, _note_strip("WRONGSPECIES goats in a field"))
        assert result["jev"] == {"enabled": True, "rank": False, "note_check": True}
        run_dir = Path(result["run_dir"])
        assert post.count == 1
        assert all("violation" in c["body"]["questions"] and c["headers"]["Authorization"] == f"Bearer {KEY}"
                   and c["url"] == jev.JEV_URL for c in post.calls)

        scores = _json(run_dir / "shortlist_scores.json")
        entries = [e for k, v in scores.items() if isinstance(v, list) for e in v]
        assert len(entries) == 1
        assert entries[0]["note_check"] == {"p_violation": 0.95, "source": "jev"}
        assert entries[0]["note_violation"] is True
        assert isinstance(scores["excerpts_sha256"], str)  # non-list value kept

        notes = _json(run_dir / "jev_notes.json")
        assert notes["schema_version"] == "jev_notes_v1"
        assert notes["counts"] == {"checked": 1, "violations": 1, "unanswered": 0}

        shortlist = _json(run_dir / "shortlist.json")
        assert shortlist["counts"]["n_selected"] == 0
        assert [e["reasons"] for e in shortlist["excluded"]] == [["jev note violation"]]
        assert calls["encode"] == 0 and yt.exports == 0

        # verify's own shortlist reproduction accepts the annotated labels
        assert result["status"] == "completed", result
        report = _json(run_dir / "verify_export.json")
        assert report["ok"] is True, report
        assert all(KEY not in text for _, text in _all_files_text(tmp_path))

    def test_unflagged_note_is_exported_and_verify_is_ok(self, tmp_path, monkeypatch):
        root, brief_path, plan_path, yt, calls = _note_setup(tmp_path, monkeypatch)
        post = FakeJev(note_p={"WRONGSPECIES": 0.95})
        result = _run_full(root, brief_path, plan_path, yt, post, _note_strip("alpacas grazing"))
        assert result["status"] == "completed", result
        run_dir = Path(result["run_dir"])
        entry = next(e for k, v in _json(run_dir / "shortlist_scores.json").items() if isinstance(v, list) for e in v)
        assert entry["note_violation"] is False and entry["note_check"]["p_violation"] == 0.1
        assert _json(run_dir / "shortlist.json")["counts"]["n_selected"] == 1
        assert calls["encode"] == 1
        assert _json(run_dir / "verify_export.json")["ok"] is True

    def test_shortlist_scores_file_rebuilds_the_shortlist(self, tmp_path, monkeypatch):
        """Reproduce shortlist.json with build_shortlist from the files the runner wrote."""
        root, brief_path, plan_path, yt, _ = _note_setup(tmp_path, monkeypatch)
        post = FakeJev(note_p={"WRONGSPECIES": 0.95})
        result = _run_full(root, brief_path, plan_path, yt, post, _note_strip("WRONGSPECIES"))
        run_dir = Path(result["run_dir"])
        shortlist = _json(run_dir / "shortlist.json")
        labels = _json(run_dir / "shortlist_scores.json")
        validate_label_payload(labels)
        rows = _json(run_dir / "excerpts.json")
        review = _json(run_dir / "review.json")
        rebuilt = build_shortlist(rows, review, labels, shortlist["n_clips_requested"], _bindings_from(shortlist))
        assert [(e["video_id"], e["excerpt_index"], e["reasons"]) for e in rebuilt["excluded"]] == [
            (e["video_id"], e["excerpt_index"], e["reasons"]) for e in shortlist["excluded"]]
        assert rebuilt["selected"] == shortlist["selected"]

    def test_captured_strip_judgments_skip_the_note_check(self, tmp_path, monkeypatch):
        root, brief_path, plan_path, yt, _ = _note_setup(tmp_path, monkeypatch)
        post = FakeJev(default=0.9, note_p={"": 0.95})  # would flag everything if it ran
        ports = _ports(yt, tile_caller=_tile_text)
        ports.jev_post = post
        first = advance(root, brief=brief_path, plan=plan_path, vision_agree=True, ports=ports)
        assert first["status"] == "paused" and first["stage"] == "label_strips", first
        run_dir = Path(first["run_dir"])
        excerpts = _json(run_dir / "excerpts.json")
        video_id = excerpts[0]["video_id"]
        strip_scores = {"excerpts_sha256": _json(run_dir / "review.json")["excerpts_sha256"],
                        video_id: [{"excerpt_index": 0, "match": "keep", "geo": "uncertain", "scene_type": "field",
                                    "note": "captured note", "continuity_ok": True}]}
        second = advance(root, brief=brief_path, plan=plan_path, run_dir=run_dir, vision_agree=True,
                         judgments={"strip_scores": strip_scores}, ports=ports)
        assert second["stage"] != "label_strips" or second["status"] != "paused", second
        assert post.count == 0
        assert not (run_dir / "jev_notes.json").exists()
        entry = _json(run_dir / "shortlist_scores.json")[video_id][0]
        assert "note_check" not in entry and "note_violation" not in entry

    def test_note_check_off_by_default_even_with_a_port(self, tmp_path, monkeypatch):
        root, brief_path, plan_path, yt, _ = _note_setup(tmp_path, monkeypatch, extra="")
        post = FakeJev(note_p={"": 0.95})
        result = _run_full(root, brief_path, plan_path, yt, post, _note_strip("alpacas"))
        assert post.count == 0
        assert not (Path(result["run_dir"]) / "jev_notes.json").exists()
        assert result["jev"]["enabled"] is False

    def test_no_port_leaves_labels_unchanged_and_records_fallback(self, tmp_path, monkeypatch):
        root, brief_path, plan_path, yt, calls = _note_setup(tmp_path, monkeypatch)
        result = _run_full(root, brief_path, plan_path, yt, None, _note_strip("WRONGSPECIES"))
        run_dir = Path(result["run_dir"])
        entry = next(e for k, v in _json(run_dir / "shortlist_scores.json").items() if isinstance(v, list) for e in v)
        assert entry["note_check"] == {"p_violation": None, "source": "fallback_no_port"}
        assert "note_violation" not in entry
        assert _json(run_dir / "shortlist.json")["counts"]["n_selected"] == 1
        assert _json(run_dir / "jev_notes.json")["counts"]["unanswered"] == 1
        assert result["status"] == "completed", result


def _bindings_from(shortlist: dict) -> dict:
    b = shortlist.get("bindings")
    if not isinstance(b, dict):
        pytest.skip("shortlist.json carries no bindings block")
    return b
