"""The `investigate` command line."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from investigate import browser, history
from investigate.attachments import AttachmentError
from investigate.config import ConfigError, Depth, data_home, load_config
from investigate.runner import ClaudeCodeRunner, Runner
from investigate.server import DEFAULT_PORT, HistoryServer
from investigate.service import execute_run
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
    sub.add_parser("history", help="List past Runs.")
    sub.add_parser("login", help="Open the browser profile to log in to YouTube, Instagram, X.")
    tray = sub.add_parser("tray", help="Run the tray app: icon, quick capture and the server.")
    tray.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port (default 8765).")
    serve = sub.add_parser("serve", help="Serve the History and Reports on localhost.")
    serve.add_argument("--host", default="127.0.0.1", help="Address to bind (default 127.0.0.1).")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port (default 8765).")
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow binding a non-local address. Anyone who can reach it can read and delete.",
    )
    return parser


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    args = _parser().parse_args(argv)
    home = data_home()
    if args.command == "history":
        for e in sorted(history.load_entries(home), key=lambda e: e.created, reverse=True):
            print(f"{e.id}  {e.kind}  {e.depth}  {e.status}  {e.title}")
        return 0

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
        return run_tray(home, runner or ClaudeCodeRunner(), tray_gui.run, port=args.port)
    if args.command == "serve":
        try:
            server = HistoryServer(
                home,
                runner or ClaudeCodeRunner(),
                host=args.host,
                port=args.port,
                allow_remote=args.allow_remote,
            )
            server.start()
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"History at {server.url} (Ctrl+C to stop)", flush=True)
        server.serve_forever()
        return 0

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


if __name__ == "__main__":
    sys.exit(main())
