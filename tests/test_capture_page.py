"""The capture page: its script is run in node against a fake DOM, and its CSP is checked."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from investigate.capture import _JS, capture_page
from investigate.server import HistoryServer
from tests.fakes import FakeRunner
from tests.test_server import Client

HARNESS = str(Path(__file__).with_name("capture_harness.js"))
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(scenario: str, tmp_path) -> dict:
    script = tmp_path / "capture.js"
    script.write_text(_JS, encoding="utf-8")
    done = subprocess.run(
        [NODE, HARNESS, str(script), scenario], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_a_dropped_file_is_attached_once(tmp_path):
    out = _run("drop-file-once", tmp_path)
    assert out["drop"] is True
    assert out["attachments"] == ["a.png"]


@needs_node
def test_a_dragged_link_goes_into_the_box(tmp_path):
    out = _run("drop-link", tmp_path)
    assert out["prevented"] is True
    assert out["value"] == "https://x.test/a"


@needs_node
def test_a_drop_with_nothing_usable_is_left_to_the_browser(tmp_path):
    assert _run("drop-nothing", tmp_path)["prevented"] is False


@needs_node
def test_ctrl_enter_during_a_submit_does_not_submit_twice(tmp_path):
    assert _run("double-submit", tmp_path)["fetches"] == 1


def test_there_is_one_drop_registration():
    # Structural guard that also runs without node: the drop handler is on the window only.
    assert len(re.findall(r"addEventListener\('drop'", _JS)) == 1
    assert "window.addEventListener('drop'" in _JS


def test_capture_csp_allows_exactly_its_script_and_blocks_framing(home):
    srv = HistoryServer(home, FakeRunner(), port=0)
    srv.start()
    try:
        status, headers, body = Client(srv).get("/capture")
    finally:
        srv.stop()
    assert status == 200
    policy = headers["Content-Security-Policy"]
    script = re.search(r"<script>(.*?)</script>", body.decode(), re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in policy
    for part in (
        "default-src 'none'",
        "connect-src 'self'",
        "img-src blob:",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ):
        assert part in policy
    assert "unsafe-inline" not in policy
    style = re.search(r"<style>(.*?)</style>", body.decode(), re.S).group(1)
    style_digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"style-src 'sha256-{style_digest}'" in policy
    assert capture_page().count("<script") == 1


def test_history_and_report_cannot_be_framed(home):
    from investigate.config import Depth
    from investigate.service import execute_run

    result = execute_run("t", Depth.QUICK, home=home, runner=FakeRunner())
    srv = HistoryServer(home, FakeRunner(), port=0)
    srv.start()
    try:
        c = Client(srv)
        assert "frame-ancestors 'none'" in c.get("/")[1]["Content-Security-Policy"]
        report = c.get(f"/runs/{result.id}/")[1]["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in report
    finally:
        srv.stop()
