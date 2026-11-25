"""The History and Report server, driven over real HTTP with the fake runner."""

from __future__ import annotations

import http.client
import json
import threading
from datetime import UTC, datetime

import pytest

from investigate import history
from investigate.cli import main
from investigate.config import Depth
from investigate.server import HistoryServer
from investigate.service import execute_run
from tests.fakes import FakeRunner

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class Client:
    def __init__(self, server: HistoryServer) -> None:
        self.server = server

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def get(self, path, headers=None):
        return self.request("GET", path, headers=headers)

    def own_origin(self):
        return {"Origin": f"http://127.0.0.1:{self.server.port}"}

    def post(self, path, body=b"", headers=None):
        return self.request("POST", path, body, {**self.own_origin(), **(headers or {})})


@pytest.fixture
def server(home):
    srv = HistoryServer(home, FakeRunner(), port=0)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(server):
    return Client(server)


def _seed(home, item="topic", depth=Depth.QUICK, now=NOW, **runner_kwargs):
    return execute_run(item, depth, home=home, runner=FakeRunner(**runner_kwargs), now=now)


def _multipart(fields, files=()):
    boundary = "XBOUNDARYX"
    out = b""
    for name, value in fields.items():
        out += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    for name, filename, data in files:
        out += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + data
            + b"\r\n"
        )
    out += f"--{boundary}--\r\n".encode()
    return out, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


# listing


def test_empty_history_renders(client):
    status, _, body = client.get("/")
    assert status == 200
    assert b"No Runs yet" in body


def test_lists_runs_newest_first_with_columns(home, client):
    _seed(home, "older topic", now=datetime(2026, 1, 1, tzinfo=UTC))
    _seed(home, "newer topic", Depth.DEEP, now=datetime(2026, 2, 1, tzinfo=UTC))
    _, _, body = client.get("/")
    text = body.decode()
    assert text.index("Found a thing") < text.index(
        "Found a thing", text.index("Found a thing") + 1
    )
    assert "Investigation" in text and "deep" in text and "completed" in text
    assert "2026-02-01" in text
    assert text.index("2026-02-01") < text.index("2026-01-01")


def test_titles_are_escaped(home, client):
    history.upsert(
        home,
        history.HistoryEntry(
            "x", "investigation", "quick", "completed", "<script>1</script>", "c", None
        ),
    )
    _, _, body = client.get("/")
    assert b"<script>1</script>" not in body
    assert b"&lt;script&gt;" in body


def _entry(
    rid, kind="investigation", created="2026-03-01T00:00:00+00:00", title="t", verdicts=None
):
    return history.HistoryEntry(rid, kind, "quick", "completed", title, created, None, verdicts)


def _ids_listed(body: bytes) -> list[str]:
    import re

    return re.findall(r'href="/runs/([^/]+)/"', body.decode())


def test_filters_by_kind_verdict_date_and_search(home, client):
    history.upsert(home, _entry("inv", title="Solar panels"))
    history.upsert(
        home,
        _entry(
            "fc",
            "fact-check",
            "2026-02-10T00:00:00+00:00",
            "Claim video",
            {"wrong": 2, "confirmed": 1},
        ),
    )
    history.upsert(
        home,
        _entry("fc2", "fact-check", "2026-01-05T00:00:00+00:00", "Old claim", {"confirmed": 3}),
    )
    assert _ids_listed(client.get("/")[2]) == ["inv", "fc", "fc2"]
    assert _ids_listed(client.get("/?kind=fact-check")[2]) == ["fc", "fc2"]
    assert _ids_listed(client.get("/?verdict=wrong")[2]) == ["fc"]
    assert _ids_listed(client.get("/?since=2026-02-01")[2]) == ["inv", "fc"]
    assert _ids_listed(client.get("/?until=2026-02-10")[2]) == ["fc", "fc2"]
    assert _ids_listed(client.get("/?q=SOLAR")[2]) == ["inv"]
    assert b"No Run matches" in client.get("/?q=zzz")[2]


def test_verdict_counts_show_only_when_present(home, client):
    history.upsert(home, _entry("fc", "fact-check", title="C", verdicts={"partly_true": 2}))
    history.upsert(home, _entry("inv"))
    text = client.get("/")[2].decode()
    assert "2 Partly true" in text


def test_query_values_are_escaped(client):
    _, _, body = client.get('/?q="><script>x</script>')
    assert b"<script>x</script>" not in body


# report and attachments


def test_report_is_served(home, client):
    result = _seed(home)
    status, headers, body = client.get(f"/runs/{result.id}/")
    assert status == 200
    assert b"Found a thing" in body
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert client.get(f"/runs/{result.id}")[0] == 303


def test_attachment_is_served_sandboxed(home, client):
    result = _seed(home)
    (result.folder / "attachments").mkdir()
    (result.folder / "attachments" / "note.html").write_text("<script>1</script>")
    status, headers, body = client.get(f"/runs/{result.id}/attachments/note.html")
    assert status == 200 and body == b"<script>1</script>"
    assert headers["Content-Security-Policy"] == "sandbox"
    assert headers["Content-Disposition"] == "attachment"


@pytest.mark.parametrize(
    "path",
    [
        "/runs/{id}/attachments/../agent.log",
        "/runs/{id}/attachments/%2e%2e/agent.log",
        "/runs/{id}/attachments/..%2fagent.log",
        "/runs/{id}/attachments/..%5cagent.log",
        "/runs/{id}/attachments/%2e%2e%2f%2e%2e%2findex.json",
        "/runs/{id}/agent.log",
        "/runs/{id}/item.md",
        "/runs/{id}/run.json",
        "/runs/{id}/..%2f..%2findex.json",
        "/runs/%2e%2e/{id}/report.html",
        "/runs/..%2f..%2fconfig.toml/report.html",
        "/runs/..%5c..%5cconfig.toml/report.html",
        "/runs/{id}/C:/Windows/win.ini",
        "/runs/{id}/attachments//etc/passwd",
        "/runs/{id}/attachments/C:%5cWindows%5cwin.ini",
    ],
)
def test_path_traversal_is_not_possible(home, client, path):
    result = _seed(home)
    (result.folder / "attachments").mkdir()
    (result.folder / "attachments" / "ok.txt").write_text("ok")
    secret = home / "secret.txt"
    secret.write_text("SECRET")
    status, _, body = client.get(path.format(id=result.id))
    assert status in (403, 404) or status == 303
    assert b"SECRET" not in body and b"fake log" not in body and b"[runs]" not in body
    assert not status == 200


def test_symlink_out_of_attachments_is_refused(home, client):
    result = _seed(home)
    secret = home / "secret.txt"
    secret.write_text("SECRET")
    (result.folder / "attachments").mkdir()
    try:
        (result.folder / "attachments" / "link.txt").symlink_to(secret)
    except OSError:
        pytest.skip("cannot create symlinks here")
    status, _, body = client.get(f"/runs/{result.id}/attachments/link.txt")
    assert status == 404 and b"SECRET" not in body


# delete


def test_delete_removes_folder_and_entry(home, client):
    keep, drop = _seed(home, "keep"), _seed(home, "drop")
    status, headers, _ = client.post(f"/runs/{drop.id}/delete")
    assert status == 303 and headers["Location"] == "/"
    assert not drop.folder.exists() and keep.folder.exists()
    assert [e.id for e in history.load_entries(home)] == [keep.id]
    assert client.post(f"/runs/{drop.id}/delete")[0] == 404


def test_http_delete_method_works(home, client):
    result = _seed(home)
    status, _, body = client.request("DELETE", f"/runs/{result.id}", headers=client.own_origin())
    assert status == 200 and json.loads(body) == {"deleted": result.id}
    assert not result.folder.exists()


def test_get_cannot_delete(home, client):
    result = _seed(home)
    assert client.get(f"/runs/{result.id}/delete")[0] == 404
    assert result.folder.exists()


def test_cross_origin_delete_is_refused(home, client):
    result = _seed(home)
    for headers in (
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        status, _, _ = client.request("POST", f"/runs/{result.id}/delete", headers=headers)
        assert status == 403
        status, _, _ = client.request("DELETE", f"/runs/{result.id}", headers=headers)
        assert status == 403
    assert result.folder.exists()


def test_foreign_host_header_is_refused(home, client):
    result = _seed(home)
    status, _, _ = client.get("/", headers={"Host": "evil.example"})
    assert status == 403
    status, _, _ = client.request(
        "POST",
        f"/runs/{result.id}/delete",
        headers={"Host": "evil.example", "Origin": "http://evil.example"},
    )
    assert status == 403
    assert result.folder.exists()


def test_delete_cannot_escape_the_runs_folder(home, client):
    _seed(home)
    victim = home / "victim"
    victim.mkdir()
    for rid in ("..%2fvictim", "..", "%2e%2e%5cvictim"):
        client.post(f"/runs/{rid}/delete")
    assert victim.exists()


# submit and running


class BlockingRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, request):
        self.started.set()
        assert self.release.wait(10)
        return super().run(request)


def test_submit_returns_id_and_run_goes_running_then_done(home):
    runner = BlockingRunner()
    srv = HistoryServer(home, runner, port=0)
    srv.start()
    try:
        c = Client(srv)
        body, headers = _multipart(
            {
                "text": "Look into solar",
                "urls": "https://example.com/a https://example.com/b",
                "depth": "deep",
            },
            [("attachments", "../../evil name.pdf", b"PDFDATA")],
        )
        status, _, resp = c.post("/api/runs", body, headers)
        assert status == 202
        run_id = json.loads(resp)["id"]
        assert runner.started.wait(10)

        (entry,) = history.load_entries(home)
        assert entry.id == run_id and entry.status == "running" and entry.depth == "deep"
        assert b"running" in c.get("/")[2]
        assert c.post(f"/runs/{run_id}/delete")[0] == 409

        runner.release.set()
        assert srv.wait_for_runs()
        (entry,) = history.load_entries(home)
        assert entry.status == "completed"
        folder = home / "runs" / run_id
        item = (folder / "item.md").read_text(encoding="utf-8")
        assert "Look into solar" in item and "https://example.com/b" in item
        saved = list((folder / "attachments").iterdir())
        assert [f.name for f in saved] == ["evil name.pdf"]
        assert saved[0].read_bytes() == b"PDFDATA"
        assert f"attachments/{saved[0].name}" in item
        assert c.get(f"/runs/{run_id}/attachments/evil%20name.pdf")[2] == b"PDFDATA"
        assert not list((home / "uploads").iterdir())
    finally:
        runner.release.set()
        srv.stop()


def test_submit_json_and_form(home, client, server):
    status, _, resp = client.post(
        "/api/runs",
        json.dumps({"text": "a topic", "depth": "quick"}).encode(),
        {"Content-Type": "application/json"},
    )
    assert status == 202
    status, _, resp2 = client.post(
        "/api/runs",
        b"urls=https%3A%2F%2Fexample.com",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 202
    assert server.wait_for_runs()
    assert {e.id for e in history.load_entries(server.home)} == {
        json.loads(resp)["id"],
        json.loads(resp2)["id"],
    }


@pytest.mark.parametrize(
    "body,ctype",
    [
        (b"{}", "application/json"),
        (b'{"text": "x", "depth": "bogus"}', "application/json"),
        (b"not json", "application/json"),
        (b"[]", "application/json"),
        (b"text=x", "text/plain"),
        (b"", "multipart/form-data; boundary=zz"),
    ],
)
def test_bad_submissions_are_400(home, client, body, ctype):
    status, _, _ = client.post("/api/runs", body, {"Content-Type": ctype})
    assert status == 400
    assert history.load_entries(home) == []


def test_cross_origin_submit_is_refused(home, client):
    status, _, _ = client.request(
        "POST",
        "/api/runs",
        b'{"text": "x"}',
        {"Content-Type": "application/json", "Origin": "http://evil.example"},
    )
    assert status == 403
    assert history.load_entries(home) == []


def test_failed_runner_still_ends_up_in_history(home, server):
    server.runner = FakeRunner(ok=False, error="boom")
    client = Client(server)
    status, _, _ = client.post("/api/runs", b'{"text": "x"}', {"Content-Type": "application/json"})
    assert status == 202
    server.wait_for_runs()
    assert history.load_entries(server.home)[0].status == "failed"


# bind rules and lifecycle


def test_non_local_bind_is_refused_unless_allowed(home):
    for host in ("0.0.0.0", "192.168.1.5", "example.com", "::"):
        with pytest.raises(ValueError):
            HistoryServer(home, FakeRunner(), host=host)
    HistoryServer(home, FakeRunner(), host="0.0.0.0", allow_remote=True)
    for host in ("127.0.0.1", "localhost", "::1"):
        HistoryServer(home, FakeRunner(), host=host)


def test_cli_serve_refuses_non_local_bind(capsys):
    assert main(["serve", "--host", "0.0.0.0", "--port", "0"], runner=FakeRunner()) == 2
    assert "refusing" in capsys.readouterr().err


def test_server_starts_and_stops_from_code(home):
    srv = HistoryServer(home, FakeRunner(), port=0)
    srv.start()
    srv.start()
    assert srv.port != 0 and Client(srv).get("/")[0] == 200
    port = srv.port
    srv.stop()
    srv.stop()
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", port, timeout=2).request("GET", "/")
    srv.start()
    srv.stop()
