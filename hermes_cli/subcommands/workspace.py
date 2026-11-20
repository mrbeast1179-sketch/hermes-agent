"""``hermes workspace``: run the local-first browser workspace."""

from __future__ import annotations

from typing import Callable


def build_workspace_parser(subparsers, *, cmd_workspace: Callable) -> None:
    parser = subparsers.add_parser(
        "workspace",
        help="Run the local-first browser workspace",
        description=(
            "Run a loopback-only browser workspace with durable notes, safe Markdown "
            "preview, an inbox, recent sessions, and a bounded read-only terminal."
        ),
    )
    parser.add_argument("--port", type=int, default=8787, help="loopback port (default: 8787)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=["127.0.0.1", "localhost"],
        help="loopback address to bind (default: 127.0.0.1)",
    )
    parser.add_argument("--root", default=None, help="workspace data directory")
    parser.add_argument(
        "--token",
        default=None,
        help="bearer token (generated automatically when omitted)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the URL instead of opening it in a browser",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the workspace (database, schema, renderer, loopback) and exit",
    )
    parser.set_defaults(func=cmd_workspace)
