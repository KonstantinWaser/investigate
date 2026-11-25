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
    _seed(home, "older topic", now=datetime(2026, 1, 1, 12, tzinfo=UTC))
    _seed(home, "newer topic", Depth.DEEP, now=datetime(2026, 2, 1, 12, tzinfo=UTC))
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
    rid, kind="investigation", created="2026-03-01T12:00:00+00:00", title="t", verdicts=None
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
            "2026-02-10T12:00:00+00:00",
            "Claim video",
            {"wrong": 2, "confirmed": 1},
        ),
    )
    history.upsert(
        home,
        _entry("fc2", "fact-check", "2026-01-05T12:00:00+00:00", "Old claim", {"confirmed": 3}),
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
        assert c.get(f"/runs/{run_id}/attachments/evil%20name.pdf")[2] == b"PDFDATA"
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


# review round 2


def test_aliased_ids_cannot_delete_a_running_run(home):
    runner = BlockingRunner()
    srv = HistoryServer(home, runner, port=0)
    srv.start()
    try:
        c = Client(srv)
        run_id = json.loads(
            c.post("/api/runs", b'{"text": "x"}', {"Content-Type": "application/json"})[2]
        )["id"]
        assert runner.started.wait(10)
        for alias in (f"{run_id}.", run_id.upper(), f"{run_id}.."):
            status, _, _ = c.post(f"/runs/{alias}/delete")
            assert status in (404, 409)
        assert (home / "runs" / run_id).exists()
        assert [e.id for e in history.load_entries(home)] == [run_id]
    finally:
        runner.release.set()
        srv.wait_for_runs()
        srv.stop()


def test_delete_requires_an_index_entry(home, client):
    result = _seed(home)
    history.remove(home, result.id)
    assert client.post(f"/runs/{result.id}/delete")[0] == 404
    assert result.folder.exists()


def test_a_crash_inside_execute_run_marks_the_run_failed(home, server, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr("investigate.service.render_report", boom)
    client = Client(server)
    status, _, resp = client.post(
        "/api/runs", b'{"text": "x"}', {"Content-Type": "application/json"}
    )
    assert status == 202
    server.wait_for_runs()
    (entry,) = history.load_entries(home)
    assert entry.id == json.loads(resp)["id"] and entry.status == "failed"
    assert "render exploded" in (home / "runs" / entry.id / "agent.log").read_text(encoding="utf-8")


def test_keep_alive_connection_dies_with_stop(home):
    srv = HistoryServer(home, FakeRunner(), port=0)
    srv.start()
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("GET", "/")
    first = conn.getresponse()
    first.read()
    assert first.status == 200
    srv.stop()
    with pytest.raises((OSError, http.client.HTTPException)):
        conn.request("GET", "/")
        assert conn.getresponse().status != 200
        raise OSError("answered")
    conn.close()


def test_concurrent_submits_are_all_tracked(home):
    runner = BlockingRunner()
    srv = HistoryServer(home, runner, port=0)
    count = 12
    ids = []

    def submit(i):
        ids.append(srv.submit(f"topic {i}", [], "quick", []))

    try:
        threads = [threading.Thread(target=submit, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert len(ids) == count
        assert len(srv._workers) == count
    finally:
        runner.release.set()
        assert srv.wait_for_runs()
        assert not srv.active_ids()


def test_page_survives_malformed_entries(home, client):
    home.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    path = home / "index.json"
    base = {
        "id": "ok",
        "kind": "investigation",
        "depth": "quick",
        "status": "completed",
        "title": "fine",
        "created": "2026-03-01T12:00:00+00:00",
        "session_id": None,
    }
    runs = [
        base,
        {**base, "id": "b1", "verdicts": "lots"},
        {**base, "id": "b2", "title": 5},
        {**base, "id": "b3", "created": None},
        {**base, "id": "b4", "verdicts": {"wrong": "x", "confirmed": 1}},
        {**base, "id": "b5", "kind": ["x"], "status": {"a": 1}, "depth": 3},
    ]
    path.write_text(json.dumps({"runs": runs}), encoding="utf-8")
    for query in ("/", "/?q=5", "/?verdict=wrong", "/?since=2026-01-01&kind=investigation"):
        assert client.get(query)[0] == 200


def test_local_day_uses_the_given_timezone():
    from datetime import timedelta, timezone

    from investigate.webpages import local_day

    tz = timezone(timedelta(hours=2))
    assert local_day("2026-03-01T23:30:00+00:00", tz) == "2026-03-02"
    assert local_day("2026-03-01T23:30:00", tz) == "2026-03-02"  # naive means UTC
    assert local_day("garbage", tz) == ""


def test_other_loopback_address_is_served(home):
    srv = HistoryServer(home, FakeRunner(), host="127.0.0.2", port=0)
    try:
        srv.start()
    except OSError:
        pytest.skip("127.0.0.2 not bindable here")
    try:
        conn = http.client.HTTPConnection("127.0.0.2", srv.port, timeout=5)
        conn.request("GET", "/")
        assert conn.getresponse().status == 200
    finally:
        srv.stop()


# attachments through the shared module


PNG = bytes.fromhex("89504e470d0a1a0a") + b"rest"


def test_reserved_and_hostile_upload_names_are_made_safe(home, server):
    body, headers = _multipart(
        {"text": "x"},
        [
            ("attachments", "NUL", b"1"),
            ("attachments", "nul.txt", b"2"),
            ("attachments", "..\\..\\evil.txt", b"3"),
            ("attachments", "a:b*c?.txt", b"4"),
        ],
    )
    status, _, resp = Client(server).post("/api/runs", body, headers)
    assert status == 202
    server.wait_for_runs()
    run_id = json.loads(resp)["id"]
    names = sorted(f.name for f in (home / "runs" / run_id / "attachments").iterdir())
    assert "NUL" not in names and "nul.txt" not in names
    assert "attachment-NUL" in names and "attachment-nul.txt" in names and "evil.txt" in names
    assert all(not set(n) & set(":*?" + chr(92) + "/") for n in names)
    assert not (home / "runs" / "evil.txt").exists() and not (home / "evil.txt").exists()


def test_oversize_attachment_is_413_with_the_message_and_starts_no_run(home, server):
    (home).mkdir(exist_ok=True)
    (home / "config.toml").write_text("max_attachment_mb = 0.001" + chr(10), encoding="utf-8")
    body, headers = _multipart({"text": "x"}, [("attachments", "big.bin", b"x" * 5000)])
    status, _, resp = Client(server).post("/api/runs", body, headers)
    assert status == 413
    assert "big.bin" in json.loads(resp)["error"] and "limit" in json.loads(resp)["error"]
    assert history.load_entries(home) == []
    assert not (home / "runs").exists() or not list((home / "runs").iterdir())


def test_request_body_over_the_cap_is_refused_before_it_is_read(home, server):
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text("max_attachment_mb = 0.001" + chr(10), encoding="utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("POST", "/api/runs")
    conn.putheader("Origin", f"http://127.0.0.1:{server.port}")
    conn.putheader("Content-Type", "multipart/form-data; boundary=b")
    conn.putheader("Content-Length", str(50 * 1024 * 1024))
    conn.endheaders()  # no body is ever sent: the server must answer without waiting for it
    resp = conn.getresponse()
    assert resp.status == 413
    conn.close()


def test_too_many_files_is_refused(home, server):
    files = [("attachments", f"f{i}.txt", b"x") for i in range(11)]
    body, headers = _multipart({"text": "x"}, files)
    assert Client(server).post("/api/runs", body, headers)[0] == 413


def test_images_display_inline_in_the_report_and_other_files_download(home, server):
    body, headers = _multipart(
        {"text": "x"},
        [("attachments", "shot.png", PNG), ("attachments", "notes.pdf", b"%PDF")],
    )
    client = Client(server)
    run_id = json.loads(client.post("/api/runs", body, headers)[2])["id"]
    server.wait_for_runs()
    status, report_headers, report = client.get(f"/runs/{run_id}/")
    assert status == 200 and b'src="attachments/shot.png"' in report
    assert "img-src 'self'" in report_headers["Content-Security-Policy"]
    assert "script-src" not in report_headers["Content-Security-Policy"]
    _, img_headers, data = client.get(f"/runs/{run_id}/attachments/shot.png")
    assert data == PNG and img_headers["Content-Type"] == "image/png"
    assert img_headers["Content-Disposition"] == "inline"
    assert img_headers["Content-Security-Policy"] == "sandbox"
    _, pdf_headers, _ = client.get(f"/runs/{run_id}/attachments/notes.pdf")
    assert pdf_headers["Content-Disposition"] == "attachment"


def _smuggled_submit(port: int) -> bytes:
    """A complete submit request with no Origin, which the guard lets through on its own."""
    body = b'{"text": "SMUGGLED run"}'
    return (
        b"POST /api/runs HTTP/1.1\r\n"
        + f"Host: 127.0.0.1:{port}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


def _raw_exchange(port: int, payload: bytes) -> bytes:
    """Send raw bytes on one connection and read until the server closes it."""
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        chunks = []
        try:
            while data := sock.recv(65536):
                chunks.append(data)
        except TimeoutError:
            pass
    return b"".join(chunks)


@pytest.mark.parametrize(
    "outer",
    [
        # refused by the Origin check, body left unread
        "POST /api/runs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://evil.example\r\n"
        "Content-Type: text/plain\r\nContent-Length: {length}\r\n\r\n",
        # refused by the Host check
        "POST /api/runs HTTP/1.1\r\nHost: evil.example\r\nContent-Length: {length}\r\n\r\n",
        # a GET carrying a body, which GET never reads
        "GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {length}\r\n\r\n",
        # a chunked body, which the server does not parse
        "POST /api/runs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n"
        "Transfer-Encoding: chunked\r\n\r\n{length:x}\r\n",
    ],
    ids=["foreign-origin", "foreign-host", "get-with-body", "chunked"],
)
def test_an_unread_body_is_never_run_as_a_second_request(home, server, outer):
    inner = _smuggled_submit(server.port)
    head = outer.format(port=server.port, length=len(inner)).encode()
    reply = _raw_exchange(server.port, head + inner)
    assert reply.count(b"HTTP/1.1 ") == 1, reply
    assert b"202" not in reply.split(b"\r\n", 1)[0]
    runs = home / "runs"
    assert not runs.exists() or not any(runs.iterdir())


def test_chunked_bodies_are_refused_and_the_connection_closed(home, server):
    head = (
        f"POST /api/runs HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
        f"Origin: http://127.0.0.1:{server.port}\r\nTransfer-Encoding: chunked\r\n\r\n"
    ).encode()
    reply = _raw_exchange(server.port, head + b"0\r\n\r\n" + _smuggled_submit(server.port))
    assert reply.startswith(b"HTTP/1.1 501 ")
    assert reply.count(b"HTTP/1.1 ") == 1
