"""The local web server: History page, Report view, delete and the submit endpoint.

Built on the standard library (`http.server`), so it adds no dependency and can be started and
stopped from code (`HistoryServer.start()` / `.stop()`), for example by a tray app. It serves
only loopback unless explicitly told otherwise, checks the Host header (DNS rebinding) on every
request and the Origin header on every state-changing one (cross-site requests from any web
page).
"""

from __future__ import annotations

import contextlib
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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from investigate import history
from investigate.attachments import AttachmentError, Upload, check_attachments
from investigate.capture import capture_csp, capture_page
from investigate.config import ConfigError, Depth, load_config
from investigate.runner import Runner
from investigate.runqueue import QueueClosed, RunQueue
from investigate.service import execute_run, fail_run, reserve_run
from investigate.webpages import Filters, apply_filters, history_page

DEFAULT_PORT = 8765
MAX_FILES = 10  # per submission
BODY_MARGIN = 1024 * 1024  # room for the text, URLs and multipart framing
ATTACHMENTS_DIR = "attachments"
_INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SubmitError(ValueError):
    """The submitted form is unusable (a 4xx, 400 unless stated)."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


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


@dataclass(frozen=True)
class FinishedRun:
    """What `on_finished` hears about a Run that has ended (success, partial or failed)."""

    id: str
    status: str
    title: str


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
        max_parallel: int | None = None,
        on_finished: Callable[[FinishedRun], None] | None = None,
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
        self._interrupted = threading.Event()
        self.shutdown_grace = 10.0  # seconds a running Run gets when `serve_forever` ends
        self._active: set[str] = set()  # queued or running here: cannot be deleted
        # The parallel limit comes from config.toml unless given; it is read once at start-up.
        limit = max_parallel if max_parallel is not None else load_config(home).parallel_runs
        self.queue = RunQueue(limit)
        self.on_finished = on_finished
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
        """Start and block until interrupted (the `investigate serve` command), then shut down
        like the tray's Quit: queued Runs are marked failed, running ones get the grace period."""
        self.start()
        try:
            while not self._interrupted.wait(0.5) and self._thread and self._thread.is_alive():
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown(self.shutdown_grace)

    def interrupt(self) -> None:
        """End `serve_forever` (what Ctrl+C does)."""
        self._interrupted.set()

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/"

    def wait_for_runs(self, timeout: float = 30) -> bool:
        """Wait until every queued and running Run has finished."""
        return self.queue.wait_idle(timeout)

    def shutdown(self, grace: float = 10.0) -> None:
        """Quit: stop serving and take no more Runs. Queued Runs that never started are marked
        failed. Running Runs get `grace` seconds, then the runner stops them and they end as
        `partial` with what they had. Returns once nothing is left running."""
        cancelled = self.queue.close()
        self.stop()
        for run_id in cancelled:
            fail_run(self.home, run_id, "Cancelled: the app was quit before this Run started.")
            with self._lock:
                self._active.discard(run_id)
        if self.queue.wait_idle(grace):
            return
        stop_runs = getattr(self.runner, "stop", None)
        if callable(stop_runs):
            stop_runs()
        self.queue.wait_idle(30)

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
        try:
            max_bytes = int(load_config(self.home).max_attachment_mb * 1_048_576)
        except ConfigError as exc:
            raise RuntimeError(str(exc)) from exc
        uploads = [Upload(name, data) for name, data in files]
        if len(uploads) > MAX_FILES:
            raise SubmitError(f"at most {MAX_FILES} attachments per submission", 413)
        try:
            check_attachments(uploads, max_bytes)
        except AttachmentError as exc:
            too_big = any(len(u.content) > max_bytes for u in uploads)
            raise SubmitError(str(exc), 413 if too_big else 400) from exc
        item = chr(10).join(p for p in [text.strip(), *urls] if p)

        now = datetime.now(UTC)
        try:
            folder = reserve_run(item, chosen, home=self.home, attachments=uploads, now=now)
        except OSError as exc:
            raise RuntimeError(f"could not create the run folder: {exc}") from exc
        run_id = folder.name
        with self._lock:
            self._active.add(run_id)

        def work() -> None:
            try:
                execute_run(
                    item,
                    chosen,
                    home=self.home,
                    runner=self.runner,
                    now=now,
                    attachments=uploads,
                    reserved=folder,
                )
            except Exception as exc:  # the entry must not stay queued or running
                self._mark_failed(run_id, f"{type(exc).__name__}: {exc}")
            finally:
                with self._lock:
                    self._active.discard(run_id)
                self._announce(run_id)

        try:
            self.queue.submit(run_id, work)
        except QueueClosed:
            self._mark_failed(run_id, "Cancelled: the app is shutting down.")
            with self._lock:
                self._active.discard(run_id)
            raise SubmitError("the app is shutting down", HTTPStatus.SERVICE_UNAVAILABLE) from None
        return run_id

    def _announce(self, run_id: str) -> None:
        if self.on_finished is None:
            return
        try:
            entry = next(e for e in history.load_entries(self.home) if e.id == run_id)
            self.on_finished(FinishedRun(run_id, entry.status, entry.title))
        except Exception:  # a broken listener must never break the queue
            pass

    def _mark_failed(self, run_id: str | None, error: str) -> None:
        """A Run that crashed after it was listed must not stay queued or running forever."""
        if run_id:
            fail_run(self.home, run_id, error)

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
        # Any body this handler does not read would stay on the keep-alive connection and be
        # parsed as the next request, with headers the sender chose (no Origin). So close the
        # connection whenever a body may be left unread: refused requests, chunked bodies and
        # bodies on GET.
        if "Transfer-Encoding" in self.headers:
            self.close_connection = True
            self._text(HTTPStatus.NOT_IMPLEMENTED, "chunked request bodies are not supported")
            return False
        ok = request_allowed(
            self.headers, state_changing=state_changing, allow_remote=self.app.allow_remote
        )
        if not ok:
            self.close_connection = True
            self._text(HTTPStatus.FORBIDDEN, "forbidden: cross-origin or non-local request")
            return False
        if not state_changing and self.headers.get("Content-Length", "0").strip() != "0":
            self.close_connection = True
        return True

    def _body_cap(self) -> int:
        """The largest body worth reading: every allowed file at the size limit, plus a margin."""
        try:
            max_bytes = int(load_config(self.app.home).max_attachment_mb * 1_048_576)
        except ConfigError:
            max_bytes = 25 * 1_048_576
        return max_bytes * MAX_FILES + BODY_MARGIN

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > self._body_cap():
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
            self._send(
                HTTPStatus.OK,
                page.encode(),
                "text/html; charset=utf-8",
                {"Content-Security-Policy": "frame-ancestors 'none'"},
            )
            return
        if url.path == "/capture":
            self._send(
                HTTPStatus.OK,
                capture_page().encode(),
                "text/html; charset=utf-8",
                {"Content-Security-Policy": capture_csp()},
            )
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
            policy = (
                "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                "frame-ancestors 'none'"
            )
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
                {
                    "Content-Security-Policy": "sandbox",
                    "Content-Disposition": "inline" if ctype in _INLINE_TYPES else "attachment",
                },
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
                self._json(exc.status, {"error": str(exc)})
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
