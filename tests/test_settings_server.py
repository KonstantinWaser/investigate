"""The Settings page over real HTTP, the live queue resize and the tray reacting to a save."""

from __future__ import annotations

import re
import tomllib
from urllib.parse import urlencode

import pytest

from investigate.config import Config, config_path, load_config, parse_hotkey
from investigate.hotkeys import HotkeyBackend, HotkeyError
from investigate.runqueue import RunQueue
from investigate.server import HistoryServer
from investigate.settings import FIELDS, read_raw, state_from_raw
from investigate.settingspage import settings_csp, settings_page
from tests.fakes import FakeRunner
from tests.test_queue_server import CountingRunner, _wait_for
from tests.test_server import Client
from tests.test_tray import RecordingNotifier, _app

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _fields(home, **changes) -> list[tuple[str, str]]:
    state = state_from_raw(read_raw(home))
    pairs: dict[str, str] = {}
    for f in FIELDS:
        value = state.values[f.key]
        if f.kind == "bool":
            if value:
                pairs[f.key] = "on"
        else:
            pairs[f.key] = str(value)
    for i, (name, path) in enumerate(state.contexts):
        pairs[f"ctx_name_{i}"], pairs[f"ctx_path_{i}"] = name, path
    for key, value in changes.items():
        key = key.replace("__", ".")
        if value is False:
            pairs.pop(key, None)
        else:
            pairs[key] = "on" if value is True else value
    return list(pairs.items())


def _post(client, home, **changes):
    return client.post("/settings", urlencode(_fields(home, **changes)).encode(), FORM)


@pytest.fixture
def server(home):
    srv = HistoryServer(home, FakeRunner(), port=0)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(server):
    return Client(server)


# the page


def test_the_page_shows_every_setting_with_its_current_value(client, home):
    status, headers, body = client.get("/settings")
    html = body.decode()
    assert status == 200
    assert 'name="parallel_runs"' in html and 'value="2"' in html
    assert 'name="hotkey"' in html and 'value="ctrl+alt+i"' in html
    for f in FIELDS:
        assert f'name="{f.key}"' in html, f.key
    assert "ctx_name_0" in html  # blank Context rows to fill in
    assert "<script" not in html


def test_the_page_has_a_script_free_csp_that_forbids_framing(client):
    _, headers, body = client.get("/settings")
    csp = headers["Content-Security-Policy"]
    assert "script-src" not in csp and "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp and "form-action 'self'" in csp
    assert csp == settings_csp()
    style = re.search(r"<style>(.*?)</style>", body.decode(), re.S).group(1)
    import base64
    import hashlib

    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in csp  # the inline style is exactly the hashed one


def test_values_from_the_file_are_escaped(client, home):
    config_path(home).write_text(
        'language = "<script>alert(1)</script>\\"onfocus=\\"x"\n'
        'context_files = [{name = "<b>n</b>", path = "a\\"><img src=x>"}]\n',
        encoding="utf-8",
    )
    html = client.get("/settings")[2].decode()
    assert "<script>alert" not in html and "<img src=x>" not in html and "<b>n</b>" not in html
    assert "&lt;script&gt;" in html


def test_submitted_values_and_errors_are_escaped_when_the_form_comes_back(client, home):
    status, _, body = _post(client, home, language="  ", parallel_runs='"><script>x</script>')
    html = body.decode()
    assert status == 422
    assert "<script>x" not in html
    assert "&lt;script&gt;x" in html


def test_a_hand_edited_problem_in_the_file_is_shown_next_to_its_field(client, home):
    config_path(home).write_text("parallel_runs = 9\n", encoding="utf-8")
    html = client.get("/settings")[2].decode()
    assert "between 1 and 3" in html


def test_an_unreadable_config_file_shows_a_message_not_a_crash(client, home):
    config_path(home).write_text("= = =", encoding="utf-8")
    status, _, body = client.get("/settings")
    assert status == 500 and b"cannot read" in body


# saving


def test_a_valid_save_writes_the_file_and_redirects_with_a_confirmation(client, home):
    status, headers, _ = _post(client, home, language="German", parallel_runs="3")
    assert status == 303 and headers["Location"] == "/settings?saved=1"
    raw = tomllib.loads(config_path(home).read_text(encoding="utf-8"))
    assert raw["language"] == "German" and raw["parallel_runs"] == 3
    assert b"Saved." in client.get("/settings?saved=1")[2]


def test_an_invalid_save_returns_the_form_with_a_message_per_field_and_writes_nothing(client, home):
    load_config(home)
    before = config_path(home).read_bytes()
    status, _, body = _post(client, home, parallel_runs="7", hotkey="x", language="")
    html = body.decode()
    assert status == 422
    assert "between 1 and 3" in html and "hotkey needs at least one" in html
    assert "language must be a non-empty string" in html
    assert "Not saved" in html
    assert config_path(home).read_bytes() == before
    assert 'value="7"' in html  # what the user typed is kept


def test_saving_needs_the_same_origin(client, home):
    body = urlencode(_fields(home, language="Evil")).encode()
    status, _, _ = client.request(
        "POST", "/settings", body, {**FORM, "Origin": "http://evil.example"}
    )
    assert status == 403
    status, _, _ = client.request(
        "POST", "/settings", body, {**FORM, "Host": "evil.example", **client.own_origin()}
    )
    assert status == 403
    assert not config_path(home).exists() or "Evil" not in config_path(home).read_text()


def test_an_oversize_settings_body_is_refused(client):
    status, _, _ = client.post("/settings", b"a=" + b"x" * 300_000, FORM)
    assert status == 413


def test_a_save_that_cannot_be_written_says_so(client, home, monkeypatch):
    load_config(home)

    def broken(*args, **kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr("investigate.settings._write_atomically", broken)
    status, _, body = _post(client, home, language="German")
    assert status == 500 and b"disk is full" in body


# applying without a restart


def test_runqueue_limit_can_be_raised_and_lowered_while_running():
    import threading

    queue = RunQueue(1)
    gate = threading.Event()
    started = []

    def job(name):
        def work():
            started.append(name)
            assert gate.wait(10)

        return work

    for n in "abc":
        queue.submit(n, job(n))
    assert _wait_for(lambda: started == ["a"])
    queue.set_limit(3)
    assert _wait_for(lambda: sorted(started) == ["a", "b", "c"])
    queue.set_limit(1)  # lowering never kills a running job
    assert len(queue.running_ids()) == 3
    gate.set()
    assert queue.wait_idle(5)
    with pytest.raises(ValueError):
        queue.set_limit(0)


def test_saving_a_higher_parallel_limit_starts_waiting_runs_at_once(home, client, server):
    runner = CountingRunner()
    server.runner = runner
    server.queue.set_limit(1)
    for n in range(3):
        server.submit(f"item {n}", [], "quick", [])
    assert _wait_for(lambda: runner.started == 1)

    assert _post(client, home, parallel_runs="3")[0] == 303
    assert _wait_for(lambda: runner.started == 3)
    assert server.queue.limit == 3
    runner.release.set()
    assert server.wait_for_runs(10)


def test_a_limit_given_in_code_is_not_overridden_by_settings(home):
    srv = HistoryServer(home, FakeRunner(), port=0, max_parallel=1)
    srv.start()
    try:
        assert _post(Client(srv), home, parallel_runs="3")[0] == 303
        assert srv.queue.limit == 1
    finally:
        srv.stop()


def test_a_saved_config_reaches_the_listener_and_a_broken_listener_does_not_fail_the_save(home):
    seen = []
    srv = HistoryServer(home, FakeRunner(), port=0, on_config_changed=seen.append)
    srv.start()
    try:
        assert _post(Client(srv), home, hotkey="alt+shift+z")[0] == 303
        assert [c.hotkey for c in seen] == ["alt+shift+z"]
        srv.on_config_changed = lambda c: 1 / 0
        assert _post(Client(srv), home, language="Dutch")[0] == 303
    finally:
        srv.stop()


def test_settings_page_renders_for_a_bare_state():
    html = settings_page(state_from_raw({}), {"depths.x": "unknown depth 'x'"})
    assert "unknown depth" in html  # a problem on a key without a control is still shown


# the tray


class FakeHotkeys(HotkeyBackend):
    def __init__(self, fail: str | None = None) -> None:
        self.registered = None
        self.fail = fail
        self.calls = []

    def register(self, hotkey, callback):
        self.calls.append(("register", str(hotkey)))
        if self.fail:
            raise HotkeyError(self.fail)
        self.registered, self.callback = hotkey, callback

    def unregister(self):
        self.calls.append(("unregister",))
        self.registered = None


class FakeAutostart:
    def __init__(self, fail: Exception | None = None) -> None:
        self.command = None
        self.fail = fail

    def enabled(self):
        return self.command is not None

    def matches(self, command):
        return self.command == list(command)

    def enable(self, command):
        if self.fail:
            raise self.fail
        self.command = list(command)

    def disable(self):
        if self.fail:
            raise self.fail
        self.command = None


def _tray(home, **kwargs):
    kwargs.setdefault("get_startup_command", lambda: ["py", "-m", "investigate.cli", "tray"])
    return _app(home, **kwargs)


def test_the_settings_menu_item_opens_the_settings_page(home):
    opened = []
    app = _tray(home, open_url=opened.append)
    app.start()
    try:
        item = next(i for i in app.menu_items() if i.label == "Settings")
        assert item.enabled and item.action
        item.action()
        assert opened == [f"{app.server.url}settings"]
    finally:
        app.shutdown()


def test_the_hotkey_opens_the_capture_popup_and_activate_registers_the_configured_one(home):
    captured, hotkeys = [], FakeHotkeys()
    app = _tray(home, hotkeys=hotkeys, open_capture=captured.append)
    app.start()
    try:
        app.activate()
        assert str(hotkeys.registered) == "ctrl+alt+i"
        hotkeys.callback()
        assert captured == [f"{app.server.url}capture"]
    finally:
        app.shutdown()
    assert hotkeys.registered is None  # quitting releases it


def test_saving_a_new_hotkey_rebinds_it_at_once_and_an_empty_one_turns_it_off(home):
    hotkeys = FakeHotkeys()
    app = _tray(home, hotkeys=hotkeys)
    app.start()
    try:
        app.activate()
        client = Client(app.server)
        assert _post(client, home, hotkey="Ctrl+Shift+K")[0] == 303
        assert str(hotkeys.registered) == "ctrl+shift+k"
        assert _post(client, home, hotkey="")[0] == 303
        assert hotkeys.registered is None
    finally:
        app.shutdown()


def test_a_hotkey_that_cannot_register_shows_a_notice_that_links_to_settings(home):
    notifier = RecordingNotifier()
    app = _tray(
        home, hotkeys=FakeHotkeys(fail="another program already uses it"), notifier=notifier
    )
    app.start()
    try:
        app.activate()
        (toast,) = notifier.toasts
        assert toast.title == "Hotkey not available"
        assert "ctrl+alt+i" in toast.message and "already uses it" in toast.message
        assert toast.url == f"{app.server.url}settings"
    finally:
        app.shutdown()


def test_start_at_login_follows_the_setting_both_ways_and_is_repaired_at_start(home, monkeypatch):
    monkeypatch.setattr("investigate.tray.is_ephemeral", lambda path: False)
    autostart = FakeAutostart()
    app = _tray(home, autostart=autostart)
    app.start()
    try:
        client = Client(app.server)
        app.activate()
        assert autostart.command is None  # off by default
        assert _post(client, home, start_at_login=True)[0] == 303
        assert autostart.command == ["py", "-m", "investigate.cli", "tray"]
        autostart.command = ["old", "interpreter"]  # the install moved
        app.activate()
        assert autostart.command == ["py", "-m", "investigate.cli", "tray"]
        assert _post(client, home, start_at_login=False)[0] == 303
        assert autostart.command is None
    finally:
        app.shutdown()


def test_a_failing_startup_entry_shows_a_notice_and_the_save_still_succeeds(home):
    notifier = RecordingNotifier()
    app = _tray(home, autostart=FakeAutostart(fail=PermissionError("denied")), notifier=notifier)
    app.start()
    try:
        assert _post(Client(app.server), home, start_at_login=True)[0] == 303
        assert [t.title for t in notifier.toasts] == ["Start at login not changed"]
        assert "denied" in notifier.toasts[0].message
        assert load_config(home).start_at_login is True
    finally:
        app.shutdown()


def test_activate_with_a_broken_config_says_so_instead_of_raising(home):
    notifier = RecordingNotifier()
    app = _tray(home, hotkeys=FakeHotkeys(), notifier=notifier)
    app.start()
    try:
        config_path(home).write_text('hotkey = "nope"\n', encoding="utf-8")
        app.activate()
        assert [t.title for t in notifier.toasts] == ["Settings not applied"]
    finally:
        app.shutdown()


def test_apply_config_with_no_backends_is_a_no_op(home):
    app = _tray(home)
    app.apply_config(Config(hotkey="ctrl+alt+i", start_at_login=True))
    assert parse_hotkey("ctrl+alt+i") is not None
