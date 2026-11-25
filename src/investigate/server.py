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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from investigate import history
from investigate.attachments import AttachmentError, AttachmentSource, Upload, check_attachments
from investigate.capture import capture_csp, capture_page
from investigate.config import Config, ConfigError, Depth, load_config, validate_raw
from investigate.factcheck import KIND as FACT_CHECK
from investigate.followup import FollowupError, FollowupManager
from investigate.inbox import Notices
from investigate.runner import Runner
from investigate.runqueue import QueueClosed, RunQueue
from investigate.service import KIND, execute_run, fail_run, reserve_run
from investigate.settings import (
    SaveResult,
    read_raw,
    save_settings,
    state_from_form,
    state_from_raw,
)
from investigate.settingspage import settings_csp, settings_page
from investigate.webpages import Filters, apply_filters, history_page

DEFAULT_PORT = 8765
MAX_FILES = 10  # per submission
SETTINGS_BODY_CAP = 256 * 1024  # the Settings form is a few KB of text
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
        on_config_changed: Callable[[Config], None] | None = None,
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
        self._promote_lock = threading.Lock()
        self._interrupted = threading.Event()
        self.shutdown_grace = 10.0  # seconds a running Run gets when `serve_forever` ends
        self._active: set[str] = set()  # queued or running here: cannot be deleted
        # The parallel limit comes from config.toml unless given; it is read once at start-up.
        limit = max_parallel if max_parallel is not None else load_config(home).parallel_runs
        self.queue = RunQueue(limit)
        self.on_finished = on_finished
        # Called with the new Config after Settings saved it. The queue is already resized.
        self.on_config_changed = on_config_changed
        self._limit_pinned = max_parallel is not None
        self._settings_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._ops = threading.RLock()  # delete and Follow-up start check and act as one step
        self.followups = FollowupManager(home, runner, queue=self.queue, ops=self._ops)

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
            if self.followups.cancel(
                run_id, "Cancelled: the app was quit before this Follow-up started."
            ):
                continue
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

    def save_settings(self, form: dict[str, list[str]]) -> SaveResult:
        """Validate and write the Settings form. What applies without a restart is applied here:
        the parallel limit resizes the live queue, and `on_config_changed` tells the tray (hotkey,
        start at login). Everything else is read from the file when it is next needed."""
        with self._settings_lock:
            result = save_settings(self.home, form)
            if result.config is None:
                return result
            if not self._limit_pinned:
                self.queue.set_limit(result.config.parallel_runs)
            if self.on_config_changed is not None:
                try:
                    self.on_config_changed(result.config)
                except Exception:  # a broken listener must not turn a saved file into an error
                    pass
            return result

    def active_ids(self) -> set[str]:
        with self._lock:
            return set(self._active) | self.followups.active_ids()

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
        return self.enqueue_run(item, chosen, uploads)

    def submit_fact_check(
        self, item: str, depth: Depth, uploads: Sequence[Upload], notes: Sequence[str] = ()
    ) -> str:
        """Queue a Fact-check of `item` (the Inbox calls this) and return its id. It waits in
        the same queue as every other Run, so `parallel_runs` applies."""
        return self.enqueue_run(item, depth, uploads, kind=FACT_CHECK, notes=notes)

    def promote(self, run_id: str, depth: str = "") -> str:
        """Start an Investigation on the Item of a finished Fact-check, linked to it. Asking
        again returns the Investigation already made (unless that one failed)."""
        folder = self.run_folder(run_id)
        with self._promote_lock:  # two clicks at once must not start two Investigations
            entries = history.load_entries(self.home)
            source = next((e for e in entries if e.id == run_id), None)
            if folder is None or source is None or not folder.is_dir():
                raise SubmitError("no such Run", HTTPStatus.NOT_FOUND)
            if source.kind != FACT_CHECK:
                raise SubmitError("only a Fact-check can be promoted")
            if source.status in ("queued", "running"):
                raise SubmitError("the Fact-check is still running", HTTPStatus.CONFLICT)
            existing = next(
                (e for e in entries if e.promoted_from == run_id and e.status != "failed"), None
            )
            if existing is not None:
                return existing.id
            try:
                chosen = Depth(depth) if depth else Depth(source.depth)
            except ValueError:
                raise SubmitError(f"unknown depth {depth!r}") from None
            item = (folder / "item.md").read_text(encoding="utf-8", errors="replace")
            attached = folder / ATTACHMENTS_DIR
            files = (
                sorted(p for p in attached.glob("*") if p.is_file()) if attached.is_dir() else []
            )
            return self.enqueue_run(item, chosen, files, promoted_from=run_id)

    def enqueue_run(
        self,
        item: str,
        chosen: Depth,
        uploads: Sequence[AttachmentSource],
        *,
        kind: str = KIND,
        notes: Sequence[str] = (),
        promoted_from: str | None = None,
    ) -> str:
        """List the Run as `queued` and put it in the queue. Returns its id."""
        now = datetime.now(UTC)
        try:
            folder = reserve_run(
                item,
                chosen,
                home=self.home,
                attachments=uploads,
                now=now,
                kind=kind,
                promoted_from=promoted_from,
                notes=notes,
            )
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
                    kind=kind,
                    notes=notes,
                    promoted_from=promoted_from,
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

    def ask_followup(self, run_id: str, question: str) -> None:
        """Start a Follow-up on a finished Run in the background. Raises `FollowupError`."""
        folder = self.run_folder(run_id)
        if folder is None:
            raise FollowupError("no such Run", HTTPStatus.NOT_FOUND)
        with self._ops:
            with self._lock:
                busy = run_id in self._active
            self.followups.start(run_id, question, folder=folder, busy_run=busy)

    def delete(self, run_id: str) -> str:
        """'deleted', 'busy' (still running here) or 'missing'."""
        folder = self.run_folder(run_id)
        if folder is None:
            return "missing"
        with self._ops:  # a Follow-up cannot start between the check and the removal
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

    def _read_body(self, cap: int | None = None) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > (cap if cap is not None else self._body_cap()):
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
            notices = [f"{k}: {v}" for k, v in Notices(self.app.home).all().items()]
            page = history_page(
                apply_filters(entries, filters),
                filters,
                self.app.active_ids(),
                len(entries),
                notices,
            )
            self._send(
                HTTPStatus.OK,
                page.encode(),
                "text/html; charset=utf-8",
                {"Content-Security-Policy": "frame-ancestors 'none'"},
            )
            return
        if url.path == "/settings":
            self._settings_get(parse_qs(url.query))
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
            run_id = unquote(raw_id)
            busy = run_id in self.app.active_ids()  # includes a running fallback Run
            data = self.app.followups.decorate(
                run_id, data.decode("utf-8", "replace"), busy_run=busy
            ).encode()
            # No scripts at all: progress is a meta refresh, the ask box a plain form post.
            policy = (
                "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
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
        body = self._read_body(SETTINGS_BODY_CAP if path == "/settings" else None)
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
        if path == "/settings":
            self._settings_post(body)
            return
        match = re.fullmatch(r"/runs/([^/]+)/followup", path)
        if match:
            self._followup(unquote(match.group(1)), body)
            return
        match = re.fullmatch(r"/runs/([^/]+)/delete", path)
        if match:
            self._delete(unquote(match.group(1)), redirect=True)
            return
        match = re.fullmatch(r"/runs/([^/]+)/promote", path)
        if match:
            self._promote(unquote(match.group(1)), body)
        else:
            self._text(HTTPStatus.NOT_FOUND, "not found")

    def _promote(self, run_id: str, body: bytes) -> None:
        form = parse_qs(body.decode("utf-8", "replace"))
        depth = (form.get("depth") or [""])[0].strip()
        try:
            self.app.promote(run_id, depth)
        except SubmitError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (RuntimeError, OSError) as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        else:
            self._redirect("/")

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

    def _followup(self, run_id: str, body: bytes) -> None:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        form = ctype == "application/x-www-form-urlencoded"
        try:
            if form:
                question = (parse_qs(body.decode("utf-8", "replace")).get("question") or [""])[0]
            elif ctype == "application/json":
                data = json.loads(body)
                question = data.get("question") if isinstance(data, dict) else None
                if not isinstance(question, str):
                    raise SubmitError("send a JSON object with a string 'question'")
            else:
                raise SubmitError("send urlencoded form data or JSON")
            self.app.ask_followup(run_id, question)
        except (SubmitError, FollowupError) as exc:
            self._json(exc.status, {"error": str(exc)})
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
        else:
            if form:
                self._redirect(f"/runs/{run_id}/")
            else:
                self._json(HTTPStatus.ACCEPTED, {"id": run_id})

    def _settings_html(self, status: int, page: str) -> None:
        self._send(
            status,
            page.encode(),
            "text/html; charset=utf-8",
            {"Content-Security-Policy": settings_csp()},
        )

    def _settings_get(self, query: dict) -> None:
        try:
            raw = read_raw(self.app.home)
        except ConfigError as exc:
            self._settings_html(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                settings_page(state_from_raw({}), {}, problem=f"Settings cannot open: {exc}"),
            )
            return
        errors = validate_raw(raw, self.app.home)[1]
        self._settings_html(
            HTTPStatus.OK,
            settings_page(state_from_raw(raw), errors, saved="saved" in query),
        )

    def _settings_post(self, body: bytes) -> None:
        form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        try:
            result = self.app.save_settings(form)
        except (ConfigError, OSError) as exc:
            self._settings_html(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                settings_page(state_from_form(form), {}, problem=f"Nothing was saved: {exc}"),
            )
            return
        if result.config is None:
            self._settings_html(
                HTTPStatus.UNPROCESSABLE_ENTITY, settings_page(result.state, result.errors)
            )
        else:
            self._redirect("/settings?saved=1")

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
