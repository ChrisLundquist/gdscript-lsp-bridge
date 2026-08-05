"""Command-line entry point: serve stdio by default, manage engines on request.

With no arguments the process IS the language server -- Claude Code spawns it,
speaks LSP over its stdio, and never passes a flag. Every other mode exists
because the engines this tool leaves running are otherwise invisible: ``status``
shows what is warm, ``stop`` and ``stop-all`` end it, ``doctor`` answers "why is
there no code intelligence" without needing the protocol at all.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from . import __version__, engine, paths, project, yielding
from .registry import Registry
from .session import BridgeSession, Logger


def build_parser() -> argparse.ArgumentParser:
    """Returns the argument parser for the bridge executable."""
    parser = argparse.ArgumentParser(
        prog="gdscript-lsp-bridge",
        description=(
            "Bridge a stdio LSP client to Godot's built-in GDScript language "
            "server. With no arguments, serves LSP on stdin/stdout."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--godot",
        default="",
        help="Godot binary to use (overrides GDSCRIPT_LSP_GODOT and PATH).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("GDSCRIPT_LSP_LOG_LEVEL", "info"),
        choices=("debug", "info", "warning", "error", "quiet"),
        help="Diagnostic verbosity on stderr. Never touches stdout.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="List recorded engines and their liveness.")

    stop = subparsers.add_parser("stop", help="Stop the engine serving a project.")
    stop.add_argument("root", nargs="?", default=".", help="Project root (default: .)")

    subparsers.add_parser("stop-all", help="Stop every recorded engine.")
    subparsers.add_parser("reap", help="Drop registry entries whose engine is gone.")

    doctor = subparsers.add_parser(
        "doctor", help="Diagnose the setup for a project root."
    )
    doctor.add_argument("root", nargs="?", default=".", help="Project root (default: .)")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the bridge or a management command. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    registry = Registry()

    if args.command == "status":
        entries = sorted(registry.load().items(), key=lambda item: item[1].root)
        print(engine.describe(entries))
        return 0

    if args.command == "stop":
        entry = engine.stop_root(args.root, registry)
        if entry is None:
            print(f"no engine recorded for {paths.physical_root(args.root)}")
            return 1
        print(f"stopped pid={entry.pid} port={entry.port} root={entry.root}")
        return 0

    if args.command == "stop-all":
        stopped = engine.stop_all(registry)
        for entry in stopped:
            print(f"stopped pid={entry.pid} port={entry.port} root={entry.root}")
        if not stopped:
            print("no engines recorded")
        return 0

    if args.command == "reap":
        removed = engine.reap_stale(registry)
        print(f"removed {len(removed)} stale entr{'y' if len(removed) == 1 else 'ies'}")
        return 0

    if args.command == "doctor":
        return _doctor(args.root, args.godot, registry)

    logger = Logger(args.log_level)
    session = BridgeSession(registry=registry, godot=args.godot, logger=logger)
    return session.run()


def _doctor(root: str, godot: str, registry: Registry) -> int:
    """Prints everything that determines whether this project can be served."""
    print(f"bridge version:   {__version__}")
    print(f"python:           {sys.version.split()[0]} ({sys.executable})")

    try:
        binary = engine.find_godot(godot)
        print(f"godot binary:     {binary}")
    except engine.EngineError as error:
        print(f"godot binary:     NOT FOUND ({error})")
        return 1

    workspace = os.path.abspath(root)
    print(f"workspace:        {workspace}")
    discovered = project.find_project_root(workspace)
    if not discovered:
        print(f"project root:     NOT FOUND (no {project.PROJECT_MARKER} at, above,")
        print("                  or just below the workspace)")
        return 1
    print(f"project root:     {discovered}")

    key = paths.project_key(discovered)
    print(f"project key:      {key}")
    print(f"registry file:    {registry.path}")
    print(f"engine log:       {paths.engine_log_path(key)}")

    entry = registry.get(key)
    if entry is None:
        print("recorded engine:  none (a session would spawn one)")
    else:
        live = engine.verify(entry, entry.root)
        print(f"recorded engine:  pid={entry.pid} port={entry.port} live={live}")
        if not live:
            print("                  (stale; a session would reap and respawn it)")

    pattern = yielding.configured_glob()
    if not pattern:
        print("lock-yield:       off")
    else:
        held = yielding.held_paths(pattern)
        print(f"lock-yield:       {pattern} (mode={yielding.configured_mode()})")
        print(f"                  held now: {held or 'no'}")

    print()
    print("invocation this bridge uses:")
    print("  " + " ".join(engine.build_argv(binary, discovered, 6005)))
    return 0
