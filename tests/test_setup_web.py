"""The browser setup page: the steps' logic on fakes, then the page over real HTTP.

Nothing here runs the real `claude`, winget, Chrome or the registry: every outside effect is a
`SetupActions` field replaced by a fake.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import tomllib
from base64 import b64encode
from pathlib import Path
from urllib.parse import urlencode

import pytest

from investigate import setupweb
from investigate.config import config_path, load_config
from investigate.inbox import load_inbox_config
from investigate.server import HistoryServer
from investigate.setup import _add_context
from investigate.setupweb import (
    MAX_CONTEXT_BYTES,
    SetupActions,
    SetupError,
    add_contexts,
    gather_status,
    set_browser,
    set_inbox,
    set_start_at_login,
    start_browser_login,
    start_claude_login,
    start_ffmpeg_install,
)
from tests.fakes import FakeRunner
from tests.test_server import Client, _multipart
from tests.test_setup import FakeIO

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class FakeProc:
    def __init__(self) -> None:
        self.alive = True

    def poll(self) -> int | None:
        return None if self.alive else 0


class FakeActions:
    def __init__(self, io: FakeIO | None = None, *, login_running: bool = False) -> None:
        self.io = io or FakeIO(tools={"ffmpeg", "winget"})
        self.windows: list[list[str]] = []
        self.procs: list[FakeProc] = []
        self.start_error: OSError | None = None
        self.logins: list[Path] = []
        self.login_running = login_running

    def build(self) -> SetupActions:
        def start_login(home: Path) -> bool:
            self.logins.append(home)
            return not self.login_running

        def open_window(argv: list[str]) -> FakeProc:
            if self.start_error is not None:
                raise self.start_error
            self.windows.append(argv)
            proc = FakeProc()
            self.procs.append(proc)
            return proc

        return SetupActions(
            io=self.io.build(),
            open_window=open_window,
            start_browser_login=start_login,
            browser_login_running=lambda home: self.login_running,
        )


def _actions(**kwargs) -> tuple[FakeActions, SetupActions]:
    fake = FakeActions(**kwargs)
    return fake, fake.build()


def _text(home: Path) -> str:
    return config_path(home).read_text(encoding="utf-8")


# status


def test_status_on_a_fresh_folder_reports_every_step(home):
    _, actions = _actions()
    status = gather_status(home, actions)
    assert status.agent.installed and status.agent.logged_in is True
    assert status.contexts == []
    assert status.chrome and not status.browser_logged_in
    assert status.playlist == "" and status.collection == ""
    assert status.ffmpeg and status.winget
    assert not status.start_at_login and status.problem == ""
    assert config_path(home).is_file()  # created with the defaults


def test_status_with_a_broken_config_says_so_instead_of_raising(home):
    home.mkdir(parents=True)
    config_path(home).write_text("this is = = not toml", encoding="utf-8")
    _, actions = _actions()
    assert "cannot read" in gather_status(home, actions).problem


def test_status_marks_a_login_window_that_is_open(home):
    _, actions = _actions(login_running=True)
    assert gather_status(home, actions).browser_login_running


# programs started in their own window


def test_claude_login_starts_the_official_command_in_a_window():
    fake, actions = _actions()
    start_claude_login(actions)
    assert fake.windows == [["/bin/claude", "auth", "login"]]


def test_claude_login_without_the_cli_starts_nothing():
    fake, actions = _actions(io=FakeIO(claude=None))
    with pytest.raises(SetupError, match="not installed"):
        start_claude_login(actions)
    assert fake.windows == []


def test_ffmpeg_install_uses_the_winget_id():
    fake, actions = _actions(io=FakeIO(tools={"winget"}))
    start_ffmpeg_install(actions)
    assert fake.windows[0][:4] == ["/bin/winget", "install", "--id", "Gyan.FFmpeg"]


def test_ffmpeg_install_without_winget_or_when_present_starts_nothing():
    fake, actions = _actions(io=FakeIO(tools=set()))
    with pytest.raises(SetupError, match="winget"):
        start_ffmpeg_install(actions)
    fake2, actions2 = _actions(io=FakeIO(tools={"ffmpeg", "winget"}))
    with pytest.raises(SetupError, match="already"):
        start_ffmpeg_install(actions2)
    assert fake.windows == [] and fake2.windows == []


def test_browser_login_needs_chrome_and_runs_once_at_a_time(home):
    _, actions = _actions(io=FakeIO(chrome=False))
    with pytest.raises(SetupError, match="Chrome"):
        start_browser_login(home, actions)
    _, actions = _actions(login_running=True)
    with pytest.raises(SetupError, match="profile is in use"):
        start_browser_login(home, actions)
    fake, actions = _actions()
    start_browser_login(home, actions)
    assert fake.logins == [home]


# Context uploads


def _contexts_dir(home: Path) -> Path:
    return home / "contexts"


def test_upload_copies_into_the_data_folder_and_lists_it(home):
    load_config(home)
    out = add_contexts(home, [("work notes.md", b"# Work\n")], replace=False)
    assert [o.status for o in out] == ["added"]
    stored = _contexts_dir(home) / "work notes.md"
    assert stored.read_bytes() == b"# Work\n"
    names = [(c.name, c.path) for c in load_config(home).contexts]
    assert names == [("work notes", str(stored.resolve()))]


def test_upload_keeps_the_comments_of_the_config(home):
    load_config(home)
    add_contexts(home, [("a.md", b"a")], replace=False)
    assert "# Your Context files" in _text(home)


def test_same_name_is_kept_unless_the_user_confirms(home):
    load_config(home)
    add_contexts(home, [("a.md", b"first")], replace=False)
    out = add_contexts(home, [("a.md", b"second")], replace=False)
    assert [o.status for o in out] == ["kept"]
    assert (_contexts_dir(home) / "a.md").read_bytes() == b"first"
    out = add_contexts(home, [("a.md", b"second")], replace=True)
    assert [o.status for o in out] == ["replaced"]
    assert (_contexts_dir(home) / "a.md").read_bytes() == b"second"
    assert len(load_config(home).contexts) == 1  # never listed twice


def test_a_file_name_cannot_leave_the_contexts_folder(home):
    load_config(home)
    for name in ("../../evil.md", "..\\..\\evil.md", "/etc/passwd", "C:\\x\\y.md", ".."):
        add_contexts(home, [(name, b"x")], replace=False)
    for path in home.rglob("*"):
        assert path == home or home in path.parents
    assert not (home.parent / "evil.md").exists()
    folder = str(_contexts_dir(home).resolve())
    assert all(c.path.startswith(folder) for c in load_config(home).contexts)


def test_big_and_binary_uploads_are_rejected_and_nothing_is_written(home):
    load_config(home)
    before = _text(home)
    out = add_contexts(
        home,
        [("big.md", b"x" * (MAX_CONTEXT_BYTES + 1)), ("bin.md", b"\xff\xfe\x00bad"), ("e.md", b"")],
        replace=False,
    )
    assert [o.status for o in out] == ["rejected"] * 3
    assert _text(home) == before
    assert not _contexts_dir(home).exists() or not list(_contexts_dir(home).iterdir())


def test_a_name_already_listed_for_another_file_is_refused(home, tmp_path):
    load_config(home)
    mine = tmp_path / "mine.md"
    mine.write_text("mine", encoding="utf-8")
    _add_context(home, "notes", mine)
    out = add_contexts(home, [("notes.md", b"other")], replace=False)
    assert [o.status for o in out] == ["rejected"]
    assert mine.read_text(encoding="utf-8") == "mine"
    assert not (_contexts_dir(home) / "notes.md").exists()


def test_upload_with_no_files_is_an_error(home):
    with pytest.raises(SetupError, match="Choose"):
        add_contexts(home, [], replace=False)


# Inbox


def test_playlist_and_collection_are_saved_with_comments_kept(home):
    load_config(home)
    set_inbox(home, "https://www.youtube.com/playlist?list=PLabc123", "Fact-check")
    cfg = load_inbox_config(home)
    assert cfg.playlist == "https://www.youtube.com/playlist?list=PLabc123"
    assert cfg.instagram_collection == "Fact-check"
    assert "# investigate config" in _text(home)


def test_an_invalid_playlist_is_refused_with_the_inbox_message_and_nothing_changes(home):
    load_config(home)
    before = config_path(home).read_bytes()
    with pytest.raises(SetupError, match="playlist"):
        set_inbox(home, "my favourites", "")
    assert config_path(home).read_bytes() == before


def test_empty_values_remove_the_inbox_entries(home):
    load_config(home)
    set_inbox(home, "PLabc123", "Saved")
    set_inbox(home, "", "")
    cfg = load_inbox_config(home)
    assert cfg.playlist is None and cfg.instagram_collection is None


def test_saving_one_inbox_value_keeps_other_inbox_settings(home):
    load_config(home)
    extra = '\n[inbox]\nmax_new_per_check = 3\n\n[inbox.youtube]\nsubtitle_languages = ["fr"]\n'
    config_path(home).write_text(_text(home) + extra, encoding="utf-8")
    set_inbox(home, "PLabc123", "")
    raw = tomllib.loads(_text(home))
    assert raw["inbox"]["max_new_per_check"] == 3
    assert raw["inbox"]["youtube"]["subtitle_languages"] == ["fr"]


# toggles


def test_start_at_login_and_browser_flag_are_written(home):
    load_config(home)
    assert set_start_at_login(home, True).start_at_login is True
    assert load_config(home).start_at_login is True
    assert set_start_at_login(home, False).start_at_login is False
    assert set_browser(home, True).browser is True
    assert "# Start the tray app" in _text(home)


# the page over HTTP


@pytest.fixture
def fake():
    return FakeActions()


@pytest.fixture
def changed():
    return []


@pytest.fixture
def server(home, fake, changed):
    srv = HistoryServer(
        home, FakeRunner(), port=0, setup=fake.build(), on_config_changed=changed.append
    )
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(server):
    return Client(server)


def test_page_renders_every_step_without_script_and_with_a_hash_csp(client):
    status, headers, body = client.get("/setup")
    assert status == 200
    page = body.decode()
    for title in (
        "Claude Code",
        "Context files",
        "Browser profile",
        "YouTube Inbox",
        "Instagram Inbox",
        "Transcription",
        "Start at login",
    ):
        assert title in page
    assert "<script" not in page.lower()
    csp = headers["Content-Security-Policy"]
    assert "script-src" not in csp and "default-src 'none'" in csp
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    digest = b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in csp


def test_page_escapes_everything_it_shows(home, client):
    load_config(home)
    set_inbox(home, "", '"><img src=x onerror=alert(1)>')
    _add_context(home, "<b>x</b>", home / "a<i>.md")
    page = client.get("/setup")[2].decode()
    assert "<img src=x" not in page and "<b>x</b>" not in page and "<i>" not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page


def test_post_checks_origin_and_host_like_settings(client, fake):
    body = urlencode({"x": "1"}).encode()
    status, _, _ = client.request(
        "POST", "/setup/claude-login", body, {**FORM, "Origin": "http://evil.example"}
    )
    assert status == 403
    status, _, _ = client.request(
        "POST", "/setup/claude-login", body, {**FORM, "Host": "evil.example", **client.own_origin()}
    )
    assert status == 403
    assert fake.windows == []


def test_claude_login_button_starts_the_command_and_reloads_the_page(client, fake):
    status, headers, _ = client.post("/setup/claude-login", b"", FORM)
    assert status == 303 and headers["Location"].startswith("/setup?did=claude-login")
    assert fake.windows == [["/bin/claude", "auth", "login"]]


def test_a_start_that_cannot_happen_is_shown_as_an_error(client, fake):
    fake.io.claude = None
    status, _, body = client.post("/setup/claude-login", b"", FORM)
    assert status == 422 and b"not installed" in body
    assert fake.windows == []


def test_ffmpeg_and_browser_buttons(client, fake):
    fake.io.tools = {"winget"}
    assert client.post("/setup/ffmpeg", b"", FORM)[0] == 303
    assert fake.windows and "Gyan.FFmpeg" in fake.windows[0]
    assert client.post("/setup/browser-login", b"", FORM)[0] == 303
    assert len(fake.logins) == 1


def test_inbox_form_saves_and_reports_a_bad_url_in_place(home, client):
    good = urlencode({"playlist": "PLabc123", "collection": "Saved"}).encode()
    assert client.post("/setup/inbox", good, FORM)[0] == 303
    assert load_inbox_config(home).instagram_collection == "Saved"
    bad = urlencode({"playlist": "<nope>", "collection": "Saved"}).encode()
    status, _, body = client.post("/setup/inbox", bad, FORM)
    assert status == 422 and b"&lt;nope&gt;" in body and b"<nope>" not in body
    assert load_inbox_config(home).playlist is not None  # unchanged


def test_start_at_login_form_writes_config_and_tells_the_tray(home, client, changed):
    on = urlencode({"start_at_login": "on"}).encode()
    assert client.post("/setup/autostart", on, FORM)[0] == 303
    assert load_config(home).start_at_login is True
    assert changed and changed[-1].start_at_login is True
    assert client.post("/setup/autostart", b"", FORM)[0] == 303
    assert load_config(home).start_at_login is False


def test_browser_flag_form(home, client):
    on = urlencode({"browser": "on"}).encode()
    assert client.post("/setup/browser-flag", on, FORM)[0] == 303
    assert load_config(home).browser is True


def test_context_upload_over_http_and_confirm_flag(home, client):
    body, headers = _multipart({}, [("files", "../n.md", b"v1"), ("files", "m.md", b"m")])
    status, _, _ = client.post("/setup/contexts", body, headers)
    assert status == 200  # the result is shown in place
    assert (home / "contexts" / "n.md").read_bytes() == b"v1"
    body, headers = _multipart({}, [("files", "n.md", b"v2")])
    status, _, page = client.post("/setup/contexts", body, headers)
    assert b"already exists" in page
    assert (home / "contexts" / "n.md").read_bytes() == b"v1"
    body, headers = _multipart({"replace": "on"}, [("files", "n.md", b"v2")])
    client.post("/setup/contexts", body, headers)
    assert (home / "contexts" / "n.md").read_bytes() == b"v2"


def test_context_upload_with_no_file_chosen_and_oversized_body(client, server):
    body, headers = _multipart({}, [("files", "", b"")])
    status, _, page = client.post("/setup/contexts", body, headers)
    assert status == 422 and b"Choose" in page
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("POST", "/setup/contexts")
    conn.putheader("Origin", f"http://127.0.0.1:{server.port}")
    conn.putheader("Content-Type", "multipart/form-data; boundary=b")
    conn.putheader("Content-Length", str(setupweb.MAX_CONTEXT_FILES * MAX_CONTEXT_BYTES * 2))
    conn.endheaders()  # no body is sent: the server must refuse without waiting for it
    assert conn.getresponse().status == 413
    conn.close()
    assert client.post("/setup/inbox", b"a=" + b"x" * 70_000, FORM)[0] == 413


def test_unknown_setup_action_is_404(client):
    assert client.post("/setup/nope", b"", FORM)[0] == 404


def test_page_refreshes_itself_only_while_waiting_after_a_start(client, fake):
    fake.io.status = (0, '{"loggedIn": false}')
    page = client.get("/setup?did=claude-login&t=1")[2].decode()
    assert 'http-equiv="refresh"' in page and "t=2" in page
    assert 'http-equiv="refresh"' not in client.get("/setup")[2].decode()
    assert 'http-equiv="refresh"' not in client.get("/setup?did=claude-login&t=999")[2].decode()


# the real seams, with the operating system calls replaced


def test_open_in_window_starts_a_new_console_on_windows_and_does_not_wait(monkeypatch):
    import subprocess

    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    monkeypatch.setattr(setupweb.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    setupweb.open_in_window(["claude", "auth", "login"])
    assert calls == [(["claude", "auth", "login"], {"creationflags": 16})]
    monkeypatch.setattr(setupweb.sys, "platform", "linux")
    setupweb.open_in_window(["x"])
    assert calls[1][1]["stdin"] is subprocess.DEVNULL and calls[1][1]["start_new_session"]


def test_background_login_never_blocks_and_allows_one_at_a_time(home, monkeypatch):
    import threading

    release, started = threading.Event(), threading.Event()

    def slow_login(h):
        started.set()
        release.wait(5)
        return 0

    monkeypatch.setattr(setupweb.browser, "login", slow_login)
    assert setupweb.background_login(home) is True  # returned while the login is still open
    assert started.wait(5) and setupweb.login_running(home)
    assert setupweb.background_login(home) is False
    release.set()
    setupweb._login_thread.join(5)
    assert not setupweb.login_running(home)


def test_tray_setup_item_opens_the_setup_page(home):
    from tests.test_tray import _app

    opened = []
    app = _app(home, open_url=opened.append)
    app.start()
    try:
        app.on_setup()
        assert opened == [app.server.url + "setup"]
    finally:
        app.shutdown()


# review round: finding 1, a login that cannot start is not reported as started


def test_background_login_refuses_while_the_profile_lock_is_held(home):
    from investigate.profile_lock import profile_lock

    assert setupweb.login_running(home) is False
    with profile_lock(home):  # a Run, or the tray's own login window
        assert setupweb.login_running(home) is True
        assert setupweb.background_login(home) is False
    assert setupweb.login_running(home) is False


def test_page_does_not_offer_the_login_button_while_one_is_open(client, fake):
    fake.login_running = True
    page = client.get("/setup")[2].decode()
    assert "/setup/browser-login" not in page
    status, _, body = client.post("/setup/browser-login", b"", FORM)
    assert status == 422 and b"profile is in use" in body
    assert b"Chrome opened" not in body


# finding 2: a program is not started again while its window is still open


def test_claude_login_and_ffmpeg_are_not_started_twice_while_running(client, fake):
    fake.io.tools = {"winget"}
    for action in ("claude-login", "ffmpeg"):
        assert client.post(f"/setup/{action}", b"", FORM)[0] == 303
        status, _, body = client.post(f"/setup/{action}", b"", FORM)
        assert status == 422 and b"already running" in body
    assert len(fake.windows) == 2
    for proc in fake.procs:
        proc.alive = False
    assert client.post("/setup/claude-login", b"", FORM)[0] == 303
    assert len(fake.windows) == 3


# finding 3: names that differ only in case are one Context on Windows


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive file names")
def test_replacing_with_a_name_that_differs_in_case_keeps_one_entry(home):
    load_config(home)
    add_contexts(home, [("work.md", b"one")], replace=False)
    out = add_contexts(home, [("Work.md", b"two")], replace=True)
    assert [o.status for o in out] == ["replaced"]
    assert len(load_config(home).contexts) == 1


def test_context_names_compare_through_normcase(home, monkeypatch):
    load_config(home)
    add_contexts(home, [("a.md", b"one")], replace=False)
    seen = []
    real = os.path.normcase
    monkeypatch.setattr(setupweb.os.path, "normcase", lambda p: seen.append(p) or real(p))
    add_contexts(home, [("a.md", b"two")], replace=True)
    assert seen  # names and paths are compared after normcase


# finding 4: the upload goes through the locked path and tells the tray


def test_context_upload_tells_the_tray_and_holds_the_settings_lock(
    client, server, changed, monkeypatch
):
    held = []
    real = setupweb.add_contexts

    def spy(*args, **kwargs):
        held.append(server._settings_lock.locked())
        return real(*args, **kwargs)

    import investigate.server as srv_module

    monkeypatch.setattr(srv_module, "add_contexts", spy)
    body, headers = _multipart({}, [("files", "n.md", b"v1")])
    assert client.post("/setup/contexts", body, headers)[0] == 200
    assert held == [True]
    assert changed and changed[-1].contexts


# finding 5: a program that cannot be started is an error on the page


def test_oserror_from_starting_a_program_is_shown_not_raised(client, fake):
    fake.start_error = FileNotFoundError("claude.exe is gone")
    status, _, body = client.post("/setup/claude-login", b"", FORM)
    assert status == 422 and b"claude.exe is gone" in body


# finding 6: the login status is cached and checked by one caller at a time


def _counting(fake):
    calls = []
    inner = fake.io.build().run

    def run(argv):
        calls.append(argv)
        return inner(argv)

    return calls, run


def test_agent_status_is_cached_for_a_short_time_and_refreshed_on_demand(home):
    from dataclasses import replace

    from investigate.setupweb import AgentStatusCache

    fake = FakeActions()
    calls, run = _counting(fake)
    now = [100.0]
    base = fake.build()
    actions = replace(
        base,
        io=replace(base.io, run=run),
        agent_cache=AgentStatusCache(ttl=30, clock=lambda: now[0]),
    )
    gather_status(home, actions)
    gather_status(home, actions)
    assert len(calls) == 1
    gather_status(home, actions, refresh=True)
    assert len(calls) == 2
    now[0] += 31
    gather_status(home, actions)
    assert len(calls) == 3


def test_concurrent_status_requests_run_one_check(home):
    import threading
    import time
    from dataclasses import replace

    fake = FakeActions()
    calls = []

    def slow(argv):
        calls.append(argv)
        time.sleep(0.2)
        return 0, '{"loggedIn": true}'

    base = fake.build()
    actions = replace(base, io=replace(base.io, run=slow))
    threads = [threading.Thread(target=gather_status, args=(home, actions)) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert len(calls) == 1


def test_a_claude_login_action_bypasses_the_cache(client, fake):
    fake.io.status = (0, '{"loggedIn": false}')
    assert b"Not logged in" in client.get("/setup")[2]
    fake.io.status = (0, '{"loggedIn": true}')
    assert b"Not logged in" in client.get("/setup")[2]  # cached
    assert b"Logged in" in client.get("/setup?did=claude-login&t=1")[2]
