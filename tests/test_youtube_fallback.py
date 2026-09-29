"""YouTube bot-check handling: circuit breaker, opt-in cookies/proxy fallback.

No network and no real yt-dlp: YtDlp takes a ``runner=`` callable that these
tests replace with fakes. Cookie content is dummy text only.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from scenery_brief_clips import cli
from scenery_brief_clips.cli import main
from scenery_brief_clips.config import ConfigError, load_project_config
from scenery_brief_clips.export import ExportError, export_run
from scenery_brief_clips.runner import Ports, _blocked, _note_fallback, _result
from scenery_brief_clips.yt import (
    YOUTUBE_BLOCKED_EARLIER,
    YoutubeFallback,
    YtDlp,
    is_youtube_block,
    youtube_fallback_from_config,
)

BLOCK = (
    "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication."
)
COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tDUMMY\tx\n"
PROXY = "http://user:secret@proxy.invalid:8080"


def _done(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def _cookie_arg(cmd):
    return cmd[cmd.index("--cookies") + 1] if "--cookies" in cmd else None


class Script:
    """Fake runner: replies from a queue (or a callable), records every command."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if callable(reply):
            return reply(cmd)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _block_reply(cmd=None):
    return _done(cmd or [], 1, "", BLOCK)


def _ok_reply(stdout="fine"):
    return lambda cmd: _done(cmd, 0, stdout, "")


def _block(cmd):
    return _done(cmd, 1, "", BLOCK)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("scenery_brief_clips.yt.time.sleep", lambda _s: None)


@pytest.fixture
def owner_cookies(tmp_path):
    """A cookies file outside the project root (root is tmp_path/proj)."""
    root = tmp_path / "proj"
    root.mkdir()
    path = tmp_path / "owner-cookies.txt"
    path.write_text(COOKIES, encoding="utf-8")
    return root, path


def _yt(tmp_path, runner, fallback=None):
    return YtDlp(tmp_dir=tmp_path / "ytmp", runner=runner, fallback=fallback)


# 1. config -----------------------------------------------------------------


def _load(tmp_path, text):
    (tmp_path / "config.yaml").write_text(text)
    return load_project_config(tmp_path)


@pytest.mark.parametrize("line", ['youtube_cookies_file: ""\n', 'youtube_cookies_file: "   "\n', "youtube_cookies_file: 5\n", "youtube_cookies_file: true\n"])
def test_config_rejects_bad_cookies_file(tmp_path, line):
    with pytest.raises(ConfigError, match="youtube_cookies_file"):
        _load(tmp_path, line)


@pytest.mark.parametrize(
    "value",
    ["proxy.invalid:8080", "ftp://user:secret@proxy.invalid", "socks4://user:secret@h:1", "7", "true"],
)
def test_config_rejects_bad_proxy_without_echoing_it(tmp_path, value):
    with pytest.raises(ConfigError, match="youtube_proxy") as excinfo:
        _load(tmp_path, f"youtube_proxy: {value}\n")
    assert value not in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


@pytest.mark.parametrize(
    "proxy",
    ["http://user:secret@proxy.invalid:8080", "https://p.invalid:3128", "socks5://p.invalid:1080", "socks5h://p.invalid:1080"],
)
def test_config_accepts_valid_settings(tmp_path, proxy):
    loaded = _load(tmp_path, f"youtube_cookies_file: /somewhere/cookies.txt\nyoutube_proxy: {proxy}\n")
    assert loaded["youtube_cookies_file"] == "/somewhere/cookies.txt"
    assert loaded["youtube_proxy"] == proxy


# 2. youtube_fallback_from_config -------------------------------------------


def test_fallback_from_empty_config_is_not_configured(tmp_path):
    for config in (None, {}, {"youtube_cookies_file": "", "youtube_proxy": ""}):
        fallback = youtube_fallback_from_config(tmp_path, config)
        assert fallback.configured is False
        assert fallback.cookies_file is None and fallback.proxy is None


def test_fallback_refuses_cookies_inside_project(tmp_path):
    inside = tmp_path / "cookies.txt"
    inside.write_text(COOKIES)
    with pytest.raises(ValueError, match="outside the project"):
        youtube_fallback_from_config(tmp_path, {"youtube_cookies_file": str(inside)})


def test_fallback_refuses_cookies_inside_project_via_dotdot(owner_cookies):
    root, path = owner_cookies
    inside = root / "sub" / ".." / "cookies.txt"
    (root / "cookies.txt").write_text(COOKIES)
    with pytest.raises(ValueError, match="outside the project"):
        youtube_fallback_from_config(root, {"youtube_cookies_file": str(inside)})


def test_fallback_refuses_missing_or_non_file(owner_cookies, tmp_path):
    root, _path = owner_cookies
    with pytest.raises(ValueError, match="does not exist"):
        youtube_fallback_from_config(root, {"youtube_cookies_file": str(tmp_path / "nope.txt")})
    with pytest.raises(ValueError, match="does not exist"):
        youtube_fallback_from_config(root, {"youtube_cookies_file": str(tmp_path)})


def test_fallback_accepts_file_outside_root_and_proxy(owner_cookies):
    root, path = owner_cookies
    fallback = youtube_fallback_from_config(root, {"youtube_cookies_file": str(path), "youtube_proxy": PROXY})
    assert fallback.cookies_file == path.resolve()
    assert fallback.proxy == PROXY
    assert fallback.configured is True
    only_proxy = youtube_fallback_from_config(root, {"youtube_proxy": PROXY})
    assert only_proxy.cookies_file is None and only_proxy.configured is True


def test_fallback_expands_tilde(owner_cookies, tmp_path, monkeypatch):
    root, path = owner_cookies
    home = tmp_path / "home"
    home.mkdir()
    (home / "yt-cookies.txt").write_text(COOKIES)
    monkeypatch.setenv("HOME", str(home))
    fallback = youtube_fallback_from_config(root, {"youtube_cookies_file": "~/yt-cookies.txt"})
    assert fallback.cookies_file == (home / "yt-cookies.txt").resolve()


# 3. no fallback: circuit breaker -------------------------------------------


def test_block_without_fallback_trips_breaker(tmp_path):
    runner = Script(_block)
    yt = _yt(tmp_path, runner)
    with pytest.raises(RuntimeError) as first:
        yt._run(["--dump-json", "abc"])
    assert "confirm you’re not a bot" in str(first.value)
    assert len(runner.calls) == 1  # "sign in to confirm" is fatal: no in-loop retry
    assert yt.fallback_used is False

    with pytest.raises(RuntimeError) as second:
        yt._run(["--version"])
    assert str(second.value) == YOUTUBE_BLOCKED_EARLIER
    assert is_youtube_block(str(second.value)) is True
    assert len(runner.calls) == 1  # breaker: runner not called again

    with pytest.raises(RuntimeError, match="refused this host"):
        yt._run(["--version"])
    assert len(runner.calls) == 1


def test_non_block_error_does_not_trip_breaker(tmp_path):
    runner = Script(
        lambda cmd: _done(cmd, 1, "", "ERROR: Video unavailable"),
        _ok_reply("ok"),
    )
    yt = _yt(tmp_path, runner)
    with pytest.raises(RuntimeError, match="Video unavailable"):
        yt._run(["--dump-json", "abc"])
    assert yt._run(["--version"]) == "ok"
    assert len(runner.calls) == 2


def test_is_youtube_block_markers():
    assert is_youtube_block(BLOCK)
    assert is_youtube_block("Sign in to confirm you're not a bot".replace("Sign", "sign").replace("sign", "Sign"))
    assert not is_youtube_block("ERROR: Video unavailable")
    assert not is_youtube_block(None)


# 4. cookies fallback --------------------------------------------------------


def test_cookies_fallback_uses_private_copy_and_persists(owner_cookies, tmp_path):
    root, owner = owner_cookies
    seen = {}

    def fallback_run(cmd):
        path = Path(_cookie_arg(cmd))
        seen["path"] = path
        seen["exists"] = path.exists()
        seen["mode"] = path.stat().st_mode & 0o777
        seen["bytes"] = path.read_bytes()
        with path.open("ab") as handle:  # yt-dlp writes the jar back
            handle.write(b"# appended by yt-dlp\n")
        return _done(cmd, 0, "retry-out", "")

    runner = Script(_block, fallback_run, _ok_reply("later"))
    fb = YoutubeFallback(cookies_file=owner)
    yt = _yt(tmp_path, runner, fb)

    assert yt._run(["--dump-json", "abc"]) == "retry-out"

    plain_cmd, retry_cmd = runner.calls
    assert "--cookies" not in plain_cmd
    assert seen["exists"] is True
    assert seen["mode"] == 0o600
    assert seen["bytes"] == owner.read_bytes()
    assert seen["path"].parent == yt.tmp_dir
    assert str(owner) not in retry_cmd
    assert not seen["path"].exists()  # copy removed after the call
    assert list(yt.tmp_dir.glob(".cookies-*")) == []
    assert yt.fallback_used is True
    assert owner.read_text(encoding="utf-8") == COOKIES  # owner's file untouched

    # A later call goes straight through the fallback, no plain attempt.
    assert yt._run(["--version"]) == "later"
    assert "--cookies" in runner.calls[2]
    assert str(owner) not in runner.calls[2]
    assert owner.read_text(encoding="utf-8") == COOKIES
    assert list(yt.tmp_dir.glob(".cookies-*")) == []


def test_cookie_copy_removed_when_fallback_fails_non_block(owner_cookies, tmp_path):
    _root, owner = owner_cookies
    runner = Script(_block, lambda cmd: _done(cmd, 1, "", "ERROR: Video unavailable"))
    yt = _yt(tmp_path, runner, YoutubeFallback(cookies_file=owner))
    with pytest.raises(RuntimeError, match="Video unavailable"):
        yt._run(["x"])
    assert list(yt.tmp_dir.glob(".cookies-*")) == []
    assert yt.fallback_used is False
    # Not a block: breaker is not tripped, fallback stays active.
    with pytest.raises(RuntimeError, match="Video unavailable"):
        yt._run(["y"])
    assert len(runner.calls) == 3


# 5. fallback refused too ----------------------------------------------------


def test_fallback_refused_too_short_circuits(owner_cookies, tmp_path):
    _root, owner = owner_cookies
    runner = Script(_block)
    yt = _yt(tmp_path, runner, YoutubeFallback(cookies_file=owner))
    with pytest.raises(RuntimeError) as excinfo:
        yt._run(["x"])
    assert is_youtube_block(str(excinfo.value))
    assert len(runner.calls) == 2  # plain, then fallback
    assert yt.fallback_used is False
    assert list(yt.tmp_dir.glob(".cookies-*")) == []

    for _ in range(2):
        with pytest.raises(RuntimeError) as later:
            yt._run(["--version"])
        assert str(later.value) == YOUTUBE_BLOCKED_EARLIER
    assert len(runner.calls) == 2


def test_fallback_refused_after_it_worked_trips_breaker(owner_cookies, tmp_path):
    _root, owner = owner_cookies
    runner = Script(_block, _ok_reply("good"), _block)
    yt = _yt(tmp_path, runner, YoutubeFallback(cookies_file=owner))
    assert yt._run(["a"]) == "good"
    with pytest.raises(RuntimeError):
        yt._run(["b"])  # fallback now refused: breaker trips
    calls = len(runner.calls)
    with pytest.raises(RuntimeError) as later:
        yt._run(["c"])
    assert str(later.value) == YOUTUBE_BLOCKED_EARLIER
    assert len(runner.calls) == calls


# 6. proxy fallback and redaction --------------------------------------------


def test_proxy_fallback_passes_proxy_url(tmp_path):
    runner = Script(_block, _ok_reply("via-proxy"))
    yt = _yt(tmp_path, runner, YoutubeFallback(proxy=PROXY))
    assert yt._run(["x"]) == "via-proxy"
    retry = runner.calls[1]
    assert retry[retry.index("--proxy") + 1] == PROXY
    assert "--proxy" not in runner.calls[0]
    assert "--cookies" not in retry
    assert yt.fallback_used is True


def test_timeout_error_redacts_proxy_and_cookie_path(owner_cookies, tmp_path):
    _root, owner = owner_cookies
    captured = {}

    def timeout(cmd):
        captured["copy"] = _cookie_arg(cmd)
        raise subprocess.TimeoutExpired(cmd, 5)

    runner = Script(_block, timeout)
    yt = _yt(tmp_path, runner, YoutubeFallback(cookies_file=owner, proxy=PROXY))
    with pytest.raises(RuntimeError) as excinfo:
        yt._run(["x"], timeout=5)
    text = str(excinfo.value)
    assert "<redacted>" in text
    assert PROXY not in text and "secret" not in text
    assert captured["copy"] and captured["copy"] not in text
    assert str(owner) not in text
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert list(yt.tmp_dir.glob(".cookies-*")) == []
    assert yt.fallback_used is False


def test_timeout_error_context_does_not_carry_secrets(owner_cookies, tmp_path):
    """The redacted error is raised outside the except block: no __context__
    that still quotes the proxy and cookie path (found by this test, fixed)."""
    _root, owner = owner_cookies

    def timeout(cmd):
        raise subprocess.TimeoutExpired(cmd, 5)

    yt = _yt(tmp_path, Script(_block, timeout), YoutubeFallback(cookies_file=owner, proxy=PROXY))
    with pytest.raises(RuntimeError) as excinfo:
        yt._run(["x"], timeout=5)
    link = excinfo.value.__context__
    while link is not None:
        assert PROXY not in str(link), f"proxy URL reachable through __context__: {type(link).__name__}"
        link = link.__context__


def test_runner_exception_message_redacts_proxy(tmp_path):
    def boom(cmd):
        raise OSError(f"cannot reach {PROXY}")

    yt = _yt(tmp_path, Script(_block, boom), YoutubeFallback(proxy=PROXY))
    with pytest.raises(RuntimeError) as excinfo:
        yt._run(["x"])
    assert PROXY not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


def test_yt_dlp_stderr_echoing_proxy_is_redacted(tmp_path):
    runner = Script(_block, lambda cmd: _done(cmd, 1, "", f"ERROR: proxy {PROXY} refused"))
    yt = _yt(tmp_path, runner, YoutubeFallback(proxy=PROXY))
    with pytest.raises(RuntimeError) as excinfo:
        yt._run(["x"])
    assert PROXY not in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


# 7. concurrency --------------------------------------------------------------


def test_concurrent_calls_all_recover_through_fallback(owner_cookies, tmp_path):
    _root, owner = owner_cookies
    lock = threading.Lock()
    plain = []
    fallback_cmds = []

    def runner(cmd, **kwargs):
        if "--cookies" in cmd:
            with lock:
                fallback_cmds.append(cmd)
            assert Path(_cookie_arg(cmd)).read_bytes() == owner.read_bytes()
            return _done(cmd, 0, "ok", "")
        with lock:
            plain.append(cmd)
        time.sleep(0.02)
        return _done(cmd, 1, "", BLOCK)

    yt = _yt(tmp_path, runner, YoutubeFallback(cookies_file=owner))
    results: list[str] = []
    errors: list[BaseException] = []

    def work(i):
        try:
            results.append(yt._run([f"item{i}"]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert results == ["ok"] * 8
    assert len(plain) <= 8
    assert yt._fallback_active is True
    assert yt._blocked is False
    assert yt.fallback_used is True
    # Each fallback call used its own private copy.
    assert len({_cookie_arg(c) for c in fallback_cmds}) == len(fallback_cmds)
    assert list(yt.tmp_dir.glob(".cookies-*")) == []
    before = len(plain)
    assert yt._run(["later"]) == "ok"
    assert len(plain) == before


# 8. runner helpers -----------------------------------------------------------


class _FakeYt:
    def __init__(self, used=False, fallback=None):
        self.fallback_used = used
        self.fallback = fallback if fallback is not None else YoutubeFallback()


def test_result_includes_youtube_fallback_used_only_when_set(tmp_path):
    with_flag = _result({"youtube_fallback_used": True}, tmp_path, status="done", stage="x")
    assert with_flag["youtube_fallback_used"] is True
    assert "youtube_fallback_used" not in _result({}, tmp_path, status="done", stage="x")
    assert "youtube_fallback_used" not in _result({"youtube_fallback_used": False}, tmp_path, status="done")


def test_note_fallback_copies_state_from_ports():
    state: dict = {}
    _note_fallback(state, Ports(yt=_FakeYt(used=False)))
    assert state == {}
    _note_fallback(state, Ports(yt=None))
    assert state == {}
    _note_fallback(state, Ports(yt=_FakeYt(used=True)))
    assert state == {"youtube_fallback_used": True}
    # Once recorded it is never cleared by a later call without fallback use.
    _note_fallback(state, Ports(yt=_FakeYt(used=False)))
    assert state == {"youtube_fallback_used": True}


def test_blocked_advice_depends_on_fallback_configuration(tmp_path, owner_cookies):
    _root, owner = owner_cookies
    configured = _blocked({}, tmp_path, "analyze", BLOCK, Ports(yt=_FakeYt(fallback=YoutubeFallback(cookies_file=owner))))
    assert configured["status"] == "blocked"
    assert "was refused too" in configured["how_to_supply"]
    assert "may opt in" not in configured["how_to_supply"]

    plain = _blocked({}, tmp_path, "analyze", BLOCK, Ports(yt=_FakeYt()))
    assert plain["status"] == "blocked"
    assert "may opt in" in plain["how_to_supply"]
    assert "was refused too" not in plain["how_to_supply"]

    no_ports = _blocked({}, tmp_path, "analyze", BLOCK)
    assert "may opt in" in no_ports["how_to_supply"]
    for payload in (configured, plain, no_ports):
        assert str(owner) not in str(payload)


# 9. CLI -----------------------------------------------------------------------


def _analyze_root(tmp_path, config_text):
    root = tmp_path / "proj"
    run_dir = root / "data" / "runs" / "R1"
    run_dir.mkdir(parents=True)
    (run_dir / "ranked.json").write_text("[]")
    (root / "config.yaml").write_text(config_text)
    return root, run_dir


def test_cli_rejects_cookies_inside_project(tmp_path, capsys):
    root, run_dir = _analyze_root(tmp_path, "")
    (root / "cookies.txt").write_text(COOKIES)
    (root / "config.yaml").write_text(f"youtube_cookies_file: {root / 'cookies.txt'}\n")
    assert main(["analyze", "--run-dir", str(run_dir), "--root", str(root)]) == 2
    err = capsys.readouterr().err
    assert "invalid YouTube fallback settings" in err
    assert "outside the project" in err


def test_cli_rejects_missing_cookies_file(tmp_path, capsys):
    root, run_dir = _analyze_root(tmp_path, f"youtube_cookies_file: {tmp_path / 'nope.txt'}\n")
    assert main(["analyze", "--run-dir", str(run_dir), "--root", str(root)]) == 2
    assert "invalid YouTube fallback settings" in capsys.readouterr().err


def test_cli_passes_valid_fallback_to_ytdlp(tmp_path, monkeypatch):
    cookies = tmp_path / "owner-cookies.txt"
    cookies.write_text(COOKIES)
    root, run_dir = _analyze_root(
        tmp_path, f"youtube_cookies_file: {cookies}\nyoutube_proxy: {PROXY}\n"
    )
    made = {}

    class Stop(Exception):
        pass

    class RecordingYt:
        def __init__(self, *args, **kwargs):
            made["kwargs"] = kwargs
            raise Stop

    monkeypatch.setattr(cli, "YtDlp", RecordingYt)
    with pytest.raises(Stop):
        main(["analyze", "--run-dir", str(run_dir), "--root", str(root)])
    fallback = made["kwargs"]["fallback"]
    assert fallback.cookies_file == cookies.resolve()
    assert fallback.proxy == PROXY


def test_cli_without_settings_gets_unconfigured_fallback(tmp_path, monkeypatch):
    root, run_dir = _analyze_root(tmp_path, "max_analyze_videos: 1\n")
    made = {}

    class Stop(Exception):
        pass

    class RecordingYt:
        def __init__(self, *args, **kwargs):
            made["kwargs"] = kwargs
            raise Stop

    monkeypatch.setattr(cli, "YtDlp", RecordingYt)
    with pytest.raises(Stop):
        main(["analyze", "--run-dir", str(run_dir), "--root", str(root)])
    assert made["kwargs"]["fallback"].configured is False


# 10. export_run ---------------------------------------------------------------


def test_export_run_rejects_bad_fallback_config(tmp_path):
    from test_export import _make_run  # reuse the minimal shortlisted-run fixture

    root, run_dir = _make_run(tmp_path)
    inside = root / "cookies.txt"
    inside.write_text(COOKIES)
    with pytest.raises(ExportError, match="YouTube fallback") as excinfo:
        export_run(run_dir, root, allow_export=True, config={"youtube_cookies_file": str(inside)})
    assert "outside the project" in str(excinfo.value)
    assert not (root / "out" / "clips").exists()


def test_relative_cookies_path_is_relative_to_root_and_refused(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "cookies.txt").write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with pytest.raises(ValueError, match="outside the project"):
        youtube_fallback_from_config(root, {"youtube_cookies_file": "cookies.txt"})

