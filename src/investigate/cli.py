"""The `investigate` command line."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from investigate import browser, history
from investigate.attachments import AttachmentError
from investigate.autostart import default_autostart, entry_suffix
from investigate.config import ConfigError, Depth, data_home, load_config
from investigate.firstrun import run_first_run, run_stop_tray, run_uninstall
from investigate.hotkeys import default_hotkeys
from investigate.inbox import (
    InboxConfig,
    InboxSource,
    check_inbox,
    default_sources,
    load_inbox_config,
)
from investigate.instance import InstanceLock, recover_interrupted, running_url
from investigate.runner import ClaudeCodeRunner, Runner
from investigate.server import DEFAULT_PORT, HistoryServer
from investigate.service import execute_run
from investigate.setup import run_setup
from investigate.tray import run_tray


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="investigate", description="A personal research desk.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Investigate text and/or URLs and save a Report.")
    run.add_argument("item", nargs="*", help="Text and/or URLs to look into.")
    run.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="FILE",
        type=Path,
        help="A file or image to include in the Item. Copied, never moved. Repeatable.",
    )
    run.add_argument(
        "--depth",
        type=Depth,
        choices=list(Depth),
        default=Depth.STANDARD,
        help="quick, standard (default) or deep.",
    )
    sub.add_parser(
        "inbox",
        help="Fact-check the videos newly saved in your YouTube playlist (see config.toml).",
    )
    sub.add_parser("history", help="List past Runs.")
    sub.add_parser("setup", help="Guided first-time setup. Safe to run again.")
    first = sub.add_parser(
        "first-run",
        help="After install: Start menu shortcuts, start at login, start the tray, open setup.",
    )
    uninstall = sub.add_parser(
        "uninstall", help="Remove the shortcuts, start at login and the program. Keeps your data."
    )
    uninstall.add_argument("--yes", action="store_true", help="Skip the first question.")
    uninstall.add_argument(
        "--pause", action="store_true", help="Wait for Enter at the end (the shortcut uses it)."
    )
    stop = sub.add_parser("stop-tray", help="Quit a running tray (used before an update).")
    for command in (first, uninstall, stop):
        command.add_argument("--home", type=Path, help="Data folder (default: the usual one).")
    sub.add_parser("login", help="Open the browser profile to log in to YouTube, Instagram, X.")
    tray = sub.add_parser("tray", help="Run the tray app: icon, quick capture and the server.")
    tray.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port (default 8765).")
    tray.add_argument(
        "--home",
        type=Path,
        help="Data folder to use instead of INVESTIGATE_HOME or ~/.investigate. Start at login "
        "adds it when the folder is not the default one.",
    )
    serve = sub.add_parser("serve", help="Serve the History and Reports on localhost.")
    serve.add_argument("--host", default="127.0.0.1", help="Address to bind (default 127.0.0.1).")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port (default 8765).")
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow binding a non-local address. Anyone who can reach it can read and delete.",
    )
    return parser


def _serve(home: Path, args: argparse.Namespace, runner: Runner) -> int:
    """`serve` is a host like the tray: one per data folder, and it repairs Runs a hard kill
    left behind before it takes new ones."""
    lock = InstanceLock(home)
    if not lock.acquire():
        url = running_url(home)
        where = f" at {url}" if url else ""
        print(f"error: investigate is already running{where}", file=sys.stderr)
        return 1
    try:
        try:
            server = HistoryServer(
                home,
                runner,
                host=args.host,
                port=args.port,
                allow_remote=args.allow_remote,
            )
            recover_interrupted(home)  # before the server listens: see run_tray
            server.start()
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        lock.publish(server.url)
        print(f"History at {server.url} (Ctrl+C to stop)", flush=True)
        server.serve_forever()
        return 0
    finally:
        lock.release()


def _inbox(
    home: Path, runner: Runner, make_sources: Callable[[InboxConfig], list[InboxSource]]
) -> int:
    """One inbox check. The Fact-checks go through the same Run queue as the server's, so
    `parallel_runs` applies; this waits until they have all finished."""
    try:
        inbox = load_inbox_config(home)
        load_config(home)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not inbox.configured:
        print(
            "error: no inbox set. In config.toml add an [inbox.youtube] table with "
            'playlist = "PLxxxxxxxxxxxx" (a playlist id or URL), or an [inbox.instagram] '
            'table with collection = "Name" (a saved Collection).',
            file=sys.stderr,
        )
        return 2
    server = HistoryServer(home, runner)  # not started: only its Run queue is used
    result = check_inbox(
        home,
        make_sources(inbox),
        lambda item: server.submit_fact_check(item.text, inbox.depth, item.attachments, item.notes),
    )
    if result.busy:
        print("Another inbox check is running. Nothing done.")
        return 1
    for created in result.created:
        print(f"queued Fact-check: {created.title}")
    for error in result.errors + result.login_required:
        print(f"error: {error}", file=sys.stderr)
    for skipped in result.source_busy:
        print(f"skipped, will be retried: {skipped}")
    print(
        f"{len(result.created)} new, {result.already_processed} already checked."
        + (" Waiting for the Fact-checks to finish." if result.created else "")
    )
    try:
        server.queue.wait_idle()
    except KeyboardInterrupt:
        server.shutdown(0)
        return 130
    done = {e.id: e for e in history.load_entries(home)}
    for created in result.created:
        entry = done.get(created.run_id)
        print(f"{entry.status if entry else 'unknown'}: {created.title}")
        print(f"Report: {home / 'runs' / created.run_id / 'report.html'}")
    return 1 if result.errors or result.login_required else 0


def main(
    argv: Sequence[str] | None = None,
    runner: Runner | None = None,
    inbox_sources: Callable[[InboxConfig], list[InboxSource]] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    home = data_home()
    if args.command == "history":
        for e in sorted(history.load_entries(home), key=lambda e: e.created, reverse=True):
            print(f"{e.id}  {e.kind}  {e.depth}  {e.status}  {e.title}")
        return 0

    if args.command == "inbox":
        return _inbox(
            home,
            runner or ClaudeCodeRunner(),
            inbox_sources or (lambda config: default_sources(config, home)),
        )
    if args.command == "setup":
        return run_setup(home)
    if args.command in ("first-run", "uninstall", "stop-tray"):
        if args.home:
            home = args.home.expanduser().resolve()
        if args.command == "first-run":
            return run_first_run(home)
        if args.command == "stop-tray":
            return run_stop_tray(home)
        return run_uninstall(home, assume_yes=args.yes, pause=args.pause and sys.stdin.isatty())
    if args.command == "login":
        return browser.login(home)
    if args.command == "tray":
        try:
            from investigate import tray_gui
        except ImportError as exc:
            print(
                f"error: the tray needs its extra ({exc}). Install it with: uv sync --extra tray",
                file=sys.stderr,
            )
            return 2
        if args.home:
            home = args.home.expanduser().resolve()
        return run_tray(
            home,
            runner or ClaudeCodeRunner(),
            tray_gui.run,
            port=args.port,
            hotkeys=default_hotkeys(),
            autostart=default_autostart(suffix=entry_suffix(home)),
        )
    if args.command == "serve":
        return _serve(home, args, runner or ClaudeCodeRunner())

    try:
        config = load_config(home)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.item and not args.attach:
        print("error: give some text or URLs, or at least one --attach FILE", file=sys.stderr)
        return 2
    minutes = config.depths[args.depth].timeout_minutes
    print(f"Investigating at depth {args.depth} (limit {minutes:g} min). This can take a while.")
    try:
        result = execute_run(
            " ".join(args.item),
            args.depth,
            home=home,
            runner=runner or ClaudeCodeRunner(),
            config=config,
            attachments=args.attach,
        )
    except AttachmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{result.status}: {result.title}")
    print(f"Report: {result.report_path}")
    return 1 if result.status == "failed" else 0


def tray_main() -> int:
    """The `investigate-tray` gui-script: runs under pythonw, so no console window opens.
    Takes the options of `investigate tray` (`--port`, `--home`)."""
    return main(["tray", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
