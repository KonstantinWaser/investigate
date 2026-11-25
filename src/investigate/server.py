"""The local web server: History page, Report view, delete and the submit endpoint.

Built on the standard library (`http.server`), so it adds no dependency and can be started and
stopped from code (`HistoryServer.start()` / `.stop()`), for example by a tray app. It serves
only loopback unless explicitly told otherwise, checks the Host header (DNS rebinding) on every
request and the Origin header on every state-changing one (cross-site requests from any web
page).
"""

from __future__ import annotations

import contextlib
import dataclasses
import email.policy
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import stat
import threading
import uuid
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from investigate import history
from investigate.config import Depth
from investigate.runner import Runner, RunnerOutcome, RunRequest
from investigate.service import execute_run
from investigate.webpages import Filters, apply_filters, history_page

DEFAULT_PORT = 8765
MAX_BODY = 200 * 1024 * 1024
ATTACHMENTS_DIR = "attachments"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SubmitError(ValueError):
    """The submitted form is unusable (becomes a 400)."""


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def safe_filename(name: str, taken: set[str]) -> str:
    """A plain file name: no folders, no odd characters, unique among `taken`."""
    base = re.split(r"[\\/]", name)[-1]
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .") or "file"
    base = base[:100]
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    candidate, n = base, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
    taken.add(candidate.lower())
    return candidate


def save_attachments(folder: Path, files: list[tuple[str, bytes]]) -> list[str]:
    """Write uploaded files into `folder` under safe names and return the names.

    Deliberately minimal: the attachments module of issue #3 can replace this one function.
    """
    folder.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    names = []
    for original, data in files:
        name = safe_filename(original, taken)
        (folder / name).write_bytes(data)
        names.append(name)
    return names


def parse_submission(content_type: str, body: bytes) -> tuple[str, list[str], str, list]:
    """(text, urls, depth, files) from a multipart, urlencoded or JSON body."""
    ctype = content_type.split(";")[0].strip().lower()
    text, urls, depth, files = "", [], "", []
    if ctype == "multipart/form-data":
        raw = f"Content-Type: {content_type}\r\n\r\n".encode() + body
        msg = BytesParser(policy=email.policy.HTTP).parsebytes(raw)
        if not msg.is_multipart():
            raise SubmitError("malformed multipart body")
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                if payload:
                    files.append((filename, payload))
            elif name == "text":
                text = payload.decode("utf-8", "replace")
            elif name == "urls":
                urls += payload.decode("utf-8", "replace").split()
            elif name == "depth":
                depth = payload.decode("utf-8", "replace").strip()
    elif ctype == "application/x-www-form-urlencoded":
        form = parse_qs(body.decode("utf-8", "replace"))
        text = (form.get("text") or [""])[0]
        urls = [u for v in form.get("urls", []) for u in v.split()]
        depth = (form.get("depth") or [""])[0].strip()
    elif ctype == "application/json":
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise SubmitError("invalid JSON") from exc
        if not isinstance(data, dict):
            raise SubmitError("JSON body must be an object")
        text = data.get("text") or ""
        raw_urls = data.get("urls") or []
        depth = data.get("depth") or ""
        if not (isinstance(text, str) and isinstance(raw_urls, list) and isinstance(depth, str)):
            raise SubmitError("text, urls and depth have the wrong types")
        urls = [u for u in raw_urls if isinstance(u, str)]
    else:
        raise SubmitError("send multipart/form-data, urlencoded form data or JSON")
    return text, urls, depth, files


def request_allowed(headers, *, state_changing: bool, allow_remote: bool) -> bool:
    """Host must be a loopback name unless remote is allowed. For a state-changing request a
    browser's Origin must equal the Host; without an Origin, a browser-sent Sec-Fetch-Site must be
    same-origin or none. Clients that send neither (curl, tests) are not a cross-site risk."""
    host = headers.get("Host", "")
    if not allow_remote:
        name = host if host.endswith("]") else host.rsplit(":", 1)[0]
        if not is_loopback(name.lower()):
            return False
    if not state_changing:
        return True
    origin = headers.get("Origin")
    if origin is not None:
        parts = urlsplit(origin)
        return parts.scheme == "http" and parts.netloc == host
    site = headers.get("Sec-Fetch-Site")
    return site is None or site in ("same-origin", "none")


class _StagedRunner:
    """Wraps the real runner: reports the run folder to the submit call as soon as the Run
    exists, and moves the staged attachments into it before the agent starts."""

    def __init__(self, inner: Runner, staged: Path | None, announce) -> None:
        self.inner, self.staged, self.announce = inner, staged, announce
        self.name = inner.name

    def run(self, request: RunRequest) -> RunnerOutcome:
        self.announce(request.workdir.name)
        if self.staged is not None:
            staged, self.staged = self.staged, None
            # A failure here propagates into `execute_run`, which records a failed Run.
            shutil.move(str(staged), str(request.workdir / ATTACHMENTS_DIR))
        return self.inner.run(request)


def _make_writable_and_retry(func, path, exc) -> None:
    """Windows will not delete read-only files; clear the flag and try once more."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


class HistoryServer:
    """The History and Report server. `start()` and `stop()` can be called from any code."""

    def __init__(
        self,
        home: Path,
        runner: Runner,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        allow_remote: bool = False,
    ) -> None:
        if not allow_remote and not is_loopback(host):
            raise ValueError(
                f"refusing to bind {host!r}: only localhost is allowed unless remote access is "
                "explicitly enabled"
            )
        self.home, self.runner, self.allow_remote = home, runner, allow_remote
        self.host, self.port = host, port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._workers: list[threading.Thread] = []
        self._connections: set[socket.socket] = set()

    # lifecycle

    def start(self) -> None:
        if self._httpd is not None:
            return
        handler = type("Handler", (_Handler,), {"app": self})
        server_cls = ThreadingHTTPServer
        if ":" in self.host:
            server_cls = type(
                "V6Server", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6}
            )
        self._httpd = server_cls((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self._httpd.handle_error = lambda request, client_address: None  # resets are routine
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop serving. Runs already started keep going in the background."""
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        with self._lock:
            open_connections, self._connections = list(self._connections), set()
        for conn in open_connections:  # keep-alive clients must not outlive the server
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
        if self._thread:
            self._thread.join(timeout=5)
        self._httpd = self._thread = None

    def serve_forever(self) -> None:
        """Start and block until interrupted (the `investigate serve` command)."""
        self.start()
        try:
            while self._thread and self._thread.is_alive():
                self._thread.join(timeout=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/"

    def wait_for_runs(self, timeout: float = 30) -> bool:
        """Wait until every background Run has finished."""
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout)
        return not any(w.is_alive() for w in workers)

    # actions

    def active_ids(self) -> set[str]:
        with self._lock:
            return set(self._active)

    def submit(self, text: str, urls: list[str], depth: str, files: list[tuple[str, bytes]]) -> str:
        """Start a Run in the background and return its id."""
        try:
            chosen = Depth(depth) if depth else Depth.STANDARD
        except ValueError:
            raise SubmitError(f"unknown depth {depth!r}") from None
        if not (text.strip() or urls or files):
            raise SubmitError("nothing to investigate: send text, URLs or an attachment")
        staged = None
        names: list[str] = []
        if files:
            staged = self.home / "uploads" / uuid.uuid4().hex
            names = save_attachments(staged, files)
        parts = [text.strip(), *urls]
        if names:
            listing = "\n".join(f"- {ATTACHMENTS_DIR}/{n}" for n in names)
            parts.append(f"Attached files (in this folder):\n{listing}")
        item = "\n".join(p for p in parts if p)

        announced = threading.Event()
        box: dict[str, str] = {}

        def announce(run_id: str) -> None:
            box["id"] = run_id
            with self._lock:
                self._active.add(run_id)
            announced.set()

        wrapper = _StagedRunner(self.runner, staged, announce)

        def work() -> None:
            try:
                execute_run(item, chosen, home=self.home, runner=wrapper)
            except Exception as exc:  # reported to the submitter if it is still waiting
                box.setdefault("error", f"{type(exc).__name__}: {exc}")
                self._mark_failed(box.get("id"), box["error"])
            finally:
                with self._lock:
                    self._active.discard(box.get("id", ""))
                if staged is not None:
                    shutil.rmtree(staged, ignore_errors=True)
                announced.set()

        worker = threading.Thread(target=work, daemon=True)
        with self._lock:
            # Started before it is listed: an unstarted thread reads as not alive and would be
            # pruned by a concurrent submit.
            worker.start()
            self._workers = [w for w in self._workers if w.is_alive()] + [worker]
        announced.wait(timeout=60)
        if "id" not in box:
            raise RuntimeError(box.get("error", "the Run did not start"))
        return box["id"]

    def _mark_failed(self, run_id: str | None, error: str) -> None:
        """A Run that crashed after it was listed as running must not stay running forever."""
        if not run_id:
            return
        try:
            entry = next(e for e in history.load_entries(self.home) if e.id == run_id)
            history.upsert(self.home, dataclasses.replace(entry, status="failed"))
            folder = self.run_folder(run_id)
            if folder is not None:
                with open(folder / "agent.log", "a", encoding="utf-8") as log:
                    log.write(f"\nERROR: {error}\n")
        except (StopIteration, OSError):
            pass

    def run_folder(self, run_id: str) -> Path | None:
        """The folder of a Run, or None for an id that is not a plain child of `runs/`."""
        if not _RUN_ID.match(run_id) or ".." in run_id:
            return None
        runs = (self.home / "runs").resolve()
        folder = (runs / run_id).resolve()
        # Windows maps "id." and other spellings onto the same folder; only the exact name counts.
        return folder if folder.parent == runs and folder.name == run_id else None

    def delete(self, run_id: str) -> str:
        """'deleted', 'busy' (still running here) or 'missing'."""
        folder = self.run_folder(run_id)
        if folder is None:
            return "missing"
        if run_id in self.active_ids():
            return "busy"
        if not any(e.id == run_id for e in history.load_entries(self.home)):
            return "missing"
        existed = folder.is_dir()
        if existed:
            shutil.rmtree(folder, onexc=_make_writable_and_retry)
        removed = history.remove(self.home, run_id)
        return "deleted" if existed or removed else "missing"

    def served_file(self, run_id: str, rel: str) -> Path | None:
        """The Report, or a file under the Run's `attachments/` folder. Anything else is None."""
        folder = self.run_folder(run_id)
        if folder is None:
            return None
        if rel in ("", "report.html"):
            target, allowed = folder / "report.html", folder
        else:
            parts = unquote(rel).split("/")
            if any(p in ("", ".", "..") or "\\" in p or ":" in p for p in parts):
                return None
            if parts[0] != ATTACHMENTS_DIR or len(parts) < 2:
                return None
            target, allowed = folder.joinpath(*parts), folder / ATTACHMENTS_DIR
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            return None
        if not resolved.is_file() or not resolved.is_relative_to(allowed.resolve()):
            return None
        return resolved


class _Handler(BaseHTTPRequestHandler):
    app: HistoryServer
    protocol_version = "HTTP/1.1"
    timeout = 30  # an idle keep-alive connection gives its thread back

    def setup(self) -> None:
        super().setup()
        with self.app._lock:
            self.app._connections.add(self.connection)

    def finish(self) -> None:
        with self.app._lock:
            self.app._connections.discard(self.connection)
        super().finish()

    def log_message(self, format, *args) -> None:
        pass

    # responses

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _text(self, status: int, message: str) -> None:
        self._send(status, message.encode(), "text/plain; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": location})

    def _guard(self, *, state_changing: bool) -> bool:
        ok = request_allowed(
            self.headers, state_changing=state_changing, allow_remote=self.app.allow_remote
        )
        if not ok:
            self._text(HTTPStatus.FORBIDDEN, "forbidden: cross-origin or non-local request")
        return ok

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large or length invalid")
            return None
        return self.rfile.read(length)

    # GET

    def do_GET(self) -> None:
        if not self._guard(state_changing=False):
            return
        url = urlsplit(self.path)
        if url.path == "/":
            q = {k: v[0] for k, v in parse_qs(url.query).items() if v}
            filters = Filters(
                q.get("q", ""),
                q.get("kind", ""),
                q.get("verdict", ""),
                q.get("since", ""),
                q.get("until", ""),
            )
            entries = history.load_entries(self.app.home)
            page = history_page(
                apply_filters(entries, filters), filters, self.app.active_ids(), len(entries)
            )
            self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
            return
        match = re.fullmatch(r"/runs/([^/]+)(?:/(.*))?", url.path)
        if not match:
            self._text(HTTPStatus.NOT_FOUND, "not found")
            return
        raw_id, rel = match.group(1), match.group(2)
        if rel is None:
            self._redirect(f"/runs/{raw_id}/")
            return
        file = self.app.served_file(unquote(raw_id), rel)
        if file is None:
            self._text(HTTPStatus.NOT_FOUND, "not found")
            return
        data = file.read_bytes()
        if rel in ("", "report.html"):
            policy = "default-src 'none'; style-src 'unsafe-inline'"
            self._send(
                HTTPStatus.OK,
                data,
                "text/html; charset=utf-8",
                {"Content-Security-Policy": policy},
            )
        else:
            # Uploaded files are untrusted: never let them run as part of this origin.
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            self._send(
                HTTPStatus.OK,
                data,
                ctype,
                {"Content-Security-Policy": "sandbox", "Content-Disposition": "attachment"},
            )

    # POST and DELETE

    def do_POST(self) -> None:
        if not self._guard(state_changing=True):
            return
        path = urlsplit(self.path).path
        body = self._read_body()
        if body is None:
            return
        if path == "/api/runs":
            try:
                text, urls, depth, files = parse_submission(
                    self.headers.get("Content-Type", ""), body
                )
                run_id = self.app.submit(text, urls, depth, files)
            except SubmitError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except RuntimeError as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            else:
                self._json(HTTPStatus.ACCEPTED, {"id": run_id})
            return
        match = re.fullmatch(r"/runs/([^/]+)/delete", path)
        if match:
            self._delete(unquote(match.group(1)), redirect=True)
        else:
            self._text(HTTPStatus.NOT_FOUND, "not found")

    def do_DELETE(self) -> None:
        if not self._guard(state_changing=True):
            return
        if self._read_body() is None:
            return
        match = re.fullmatch(r"/runs/([^/]+)", urlsplit(self.path).path)
        if match:
            self._delete(unquote(match.group(1)), redirect=False)
        else:
            self._text(HTTPStatus.NOT_FOUND, "not found")

    def _delete(self, run_id: str, *, redirect: bool) -> None:
        result = self.app.delete(run_id)
        if result == "busy":
            self._json(HTTPStatus.CONFLICT, {"error": "the Run is still running"})
        elif result == "missing":
            self._json(HTTPStatus.NOT_FOUND, {"error": "no such Run"})
        elif redirect:
            self._redirect("/")
        else:
            self._json(HTTPStatus.OK, {"deleted": run_id})
