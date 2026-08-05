"""Godot LSP engine lifecycle: find it, spawn it, verify it, reuse it, stop it.

THE INVOCATION, AND WHY IT IS THE ONLY ONE. Godot's language server is an
EDITOR plugin. It is not a mode the runtime can be asked for, which rules out
every "just run the server" spelling that circulates:

===========================================================  ==================
invocation (Godot 4.7.1 on macOS)                            result
===========================================================  ==================
``--editor --headless --path ROOT --lsp-port N``             port opens, LSP OK
``--headless --main-loop GDScriptLSPMainLoop --lsp-port N``  port never opens
``--headless --path ROOT --lsp-port N``                      runs the *project*
``--editor --headless --lsp-port N``  (no ``--path``)        port never opens
===========================================================  ==================

Both ``--editor`` and ``--path`` are load-bearing. Without ``--editor`` the
binary runs the game (and refuses, for want of a main scene); without
``--path`` the editor opens the project manager, which loads no project and so
starts no language server. Measured, not assumed -- the table is what a probe
of all four reported.

THE PROCESS OUTLIVES THE BRIDGE, DELIBERATELY. A cold editor spends 10-20
seconds importing before its language server is useful, and paying that per
Claude Code session defeats the purpose. So the engine is spawned into its own
session (``setsid``), survives the bridge exiting, and is found again through
the registry. That is warm reuse; it is also, viewed unkindly, a background
process nobody asked about, which is why every spawn is recorded before it is
used, why stale records are reaped on sight, and why ``--stop``/``--stop-all``
exist. Set ``GDSCRIPT_LSP_PERSIST=0`` to opt out and have the engine die with
the bridge.

VERIFICATION IS NOT "IS THE PID ALIVE". A pid can be recycled onto an unrelated
program, and serving another process's port as if it were a language server
fails in confusing ways. :func:`verify` therefore checks the pid, checks that
the process's command line is still a Godot serving THIS root on THIS port, and
checks that the port accepts a connection. Anything less is a guess.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import time
from typing import Sequence

from . import paths
from .registry import Entry, Registry

#: Environment variable naming the Godot binary explicitly.
GODOT_ENV = "GDSCRIPT_LSP_GODOT"

#: Names looked up on PATH when the environment does not name a binary.
GODOT_CANDIDATES = ("godot", "godot4", "Godot", "godot-4")

#: macOS installs the binary inside an app bundle that is not on PATH.
MACOS_BUNDLE_PATHS = (
    "/Applications/Godot.app/Contents/MacOS/Godot",
    os.path.expanduser("~/Applications/Godot.app/Contents/MacOS/Godot"),
)

#: How long to wait for a freshly spawned engine's port to accept a connection.
#: Generous because this covers a cold import of an unfamiliar project.
DEFAULT_STARTUP_TIMEOUT = 120.0
STARTUP_POLL_SECONDS = 0.25

#: How long a liveness probe waits on the TCP connect itself.
PROBE_TIMEOUT_SECONDS = 2.0


class EngineError(Exception):
    """Raised when a Godot LSP engine cannot be located, started or reached."""


class EngineHandle:
    """A verified, reachable engine for one project root."""

    __slots__ = ("root", "key", "port", "pid", "godot", "log", "reused")

    def __init__(
        self,
        root: str,
        key: str,
        port: int,
        pid: int,
        godot: str,
        log: str,
        reused: bool,
    ) -> None:
        self.root = root
        self.key = key
        self.port = port
        self.pid = pid
        self.godot = godot
        self.log = log
        self.reused = reused

    def __repr__(self) -> str:
        state = "reused" if self.reused else "spawned"
        return f"EngineHandle({state} pid={self.pid} port={self.port} root={self.root!r})"


def find_godot(explicit: str = "") -> str:
    """Returns a usable Godot binary path.

    Order: the caller's explicit choice, then ``GDSCRIPT_LSP_GODOT``, then the
    usual names on PATH, then the macOS app bundle.
    """
    for candidate in (explicit, os.environ.get(GODOT_ENV, "")):
        if candidate:
            resolved = shutil.which(candidate) or candidate
            if os.path.isfile(resolved) and os.access(resolved, os.X_OK):
                return resolved
            raise EngineError(f"Godot binary not executable: {candidate}")
    for name in GODOT_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in MACOS_BUNDLE_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise EngineError(
        "no Godot binary found; put one on PATH or set " + GODOT_ENV
    )


def free_port() -> int:
    """Returns a port that was free at the moment of the call.

    Inherently racy -- the port is released before Godot binds it -- but the
    alternative, scanning a fixed range, races identically and picks fights
    with whatever else is listening. A collision surfaces as a startup timeout,
    which the caller retries.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists and is not a reaped-pending corpse.

    THE ZOMBIE TRAP. ``os.kill(pid, 0)`` succeeds for a zombie -- a process that
    has exited but whose parent has not collected its status -- so the obvious
    implementation reports a killed engine as alive forever. That matters here
    precisely because the bridge usually IS the parent: it spawns the engine,
    and when it later stops one (a lock yield, ``persist`` disabled), the corpse
    stays signalable until reaped. Measured, before this call learned to reap:
    every yield paid the full SIGTERM-then-SIGKILL timeout, about 20 seconds,
    waiting for a process that had already exited.

    So a non-blocking ``waitpid`` runs first. It collects our own dead children
    and reports them as gone; for a process that is not our child it raises and
    the signal probe below is accurate anyway, since that process's real parent
    (or init) reaps it promptly.
    """
    if pid <= 0:
        return False
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except (ChildProcessError, OSError, AttributeError):
        # Not our child, or a platform without waitpid. Fall through.
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but is owned by someone else -- which for our purposes means
        # it is NOT our engine, so treat it as absent.
        return False
    except OSError:
        return False
    return True


def process_command_line(pid: int) -> str:
    """Returns ``pid``'s command line, or "" when it cannot be read.

    Used to reject a recycled pid. Best effort by design: an empty result means
    "cannot tell", and callers fall back to the port probe rather than
    discarding a possibly-good engine.
    """
    if os.name == "nt":
        return ""
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def port_accepts(port: int, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """True when something accepts a TCP connection on ``port`` locally."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def connect(port: int, timeout: float = 10.0) -> socket.socket:
    """Opens a connection to the engine's LSP port.

    Nagle is disabled: LSP traffic is small request/response messages, and
    coalescing them adds latency to every single interaction.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(None)
    return sock


def build_argv(godot: str, root: str, port: int) -> list[str]:
    """Returns the one invocation that starts a headless Godot LSP server."""
    return [godot, "--editor", "--headless", "--path", root, "--lsp-port", str(port)]


def verify(entry: Entry, root: str, port: int | None = None) -> bool:
    """True when ``entry`` still names a live engine serving ``root``.

    Three checks, each closing a hole the previous one leaves open: the pid
    exists, the pid is still a Godot serving this root and port (pid reuse),
    and the port answers (the process may be alive but wedged or shutting
    down).
    """
    expected_port = entry.port if port is None else port
    if not pid_alive(entry.pid):
        return False
    command = process_command_line(entry.pid)
    if command:
        if "--lsp-port" not in command or str(expected_port) not in command:
            return False
        if root and root not in command:
            return False
    return port_accepts(expected_port)


def spawn(godot: str, root: str, port: int, log_path: str) -> int:
    """Starts a detached headless Godot LSP server and returns its pid.

    Detached on purpose: ``start_new_session`` puts the engine in its own
    process group so that killing the bridge -- or the terminal the bridge was
    launched from -- does not take the warm engine with it.
    """
    argv = build_argv(godot, root, port)
    creation_flags = 0
    start_new_session = True
    if os.name == "nt":
        start_new_session = False
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    log = open(log_path, "ab", buffering=0)
    try:
        log.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} spawn: "
            f"{' '.join(argv)}\n".encode("utf-8")
        )
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=root,
            start_new_session=start_new_session,
            creationflags=creation_flags,
        )
    except OSError as error:
        raise EngineError(f"cannot start {godot}: {error}") from error
    finally:
        log.close()
    return int(process.pid)


def wait_until_ready(
    port: int, pid: int, timeout: float = DEFAULT_STARTUP_TIMEOUT
) -> bool:
    """Polls until the engine's port accepts, the engine dies, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return False
        if port_accepts(port, timeout=0.5):
            return True
        time.sleep(STARTUP_POLL_SECONDS)
    return False


def stop(pid: int, timeout: float = 15.0) -> bool:
    """Terminates an engine, escalating to SIGKILL. True if it is gone after.

    Godot is asked politely first so it can flush its ``.godot`` cache; a
    half-written import cache is what makes the NEXT cold start slow.
    """
    if not pid_alive(pid):
        return True
    with contextlib.suppress(OSError):
        os.kill(pid, _term_signal())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        os.kill(pid, _kill_signal())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def _term_signal() -> int:
    import signal

    return int(getattr(signal, "SIGTERM", 15))


def _kill_signal() -> int:
    import signal

    return int(getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", 15)))


def ensure(
    root: str,
    registry: Registry | None = None,
    godot: str = "",
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    logger: "object | None" = None,
) -> EngineHandle:
    """Returns a live engine for ``root``, reusing a warm one when possible.

    The whole read-verify-spawn-record sequence runs inside ONE registry
    transaction. Two bridges starting against one root at the same instant
    would otherwise both miss, both spawn, and leave one engine orphaned with
    no record of it.
    """
    physical = paths.physical_root(root)
    key = paths.project_key(physical)
    store = registry or Registry()
    binary = find_godot(godot)
    log_path = paths.engine_log_path(key)

    def note(message: str) -> None:
        if logger is not None:
            getattr(logger, "info", lambda _m: None)(message)

    with store.transaction() as entries:
        existing = entries.get(key)
        if existing is not None:
            # The stored root is the guard described in paths.py: a hash
            # collision from unconditional case folding must not serve another
            # project's symbols.
            if existing.root and existing.root != physical:
                note(
                    f"registry key {key} holds a different root "
                    f"({existing.root!r}); replacing"
                )
                entries.pop(key, None)
            elif verify(existing, physical):
                existing.last_used = time.time()
                note(f"reusing warm engine pid={existing.pid} port={existing.port}")
                return EngineHandle(
                    root=physical,
                    key=key,
                    port=existing.port,
                    pid=existing.pid,
                    godot=existing.godot or binary,
                    log=existing.log or log_path,
                    reused=True,
                )
            else:
                note(
                    f"stale registry entry pid={existing.pid} port={existing.port}; "
                    "reaping"
                )
                stop(existing.pid)
                entries.pop(key, None)

        port = free_port()
        note(f"spawning {binary} for {physical} on port {port}")
        pid = spawn(binary, physical, port, log_path)
        if not wait_until_ready(port, pid, startup_timeout):
            stop(pid)
            raise EngineError(
                f"Godot LSP did not open port {port} within {startup_timeout:.0f}s "
                f"for {physical}; see {log_path}"
            )
        now = time.time()
        entries[key] = Entry(
            root=physical,
            port=port,
            pid=pid,
            godot=binary,
            started_at=now,
            last_used=now,
            log=log_path,
        )
        note(f"engine ready pid={pid} port={port}")
        return EngineHandle(
            root=physical,
            key=key,
            port=port,
            pid=pid,
            godot=binary,
            log=log_path,
            reused=False,
        )


def reap_stale(registry: Registry | None = None) -> list[str]:
    """Drops registry entries whose engine is gone. Returns the removed keys."""
    store = registry or Registry()
    removed: list[str] = []
    with store.transaction() as entries:
        for key, entry in list(entries.items()):
            if not verify(entry, entry.root):
                entries.pop(key, None)
                removed.append(key)
    return removed


def reap_idle(
    max_idle_seconds: float, registry: Registry | None = None
) -> list[str]:
    """Stops engines unused for longer than ``max_idle_seconds``.

    Opt-in (``max_idle_seconds <= 0`` does nothing) because "unused" here means
    "no bridge has started against it lately", which is a weak signal: a single
    long Claude Code session touches its entry once, at the beginning.
    """
    if max_idle_seconds <= 0:
        return []
    store = registry or Registry()
    cutoff = time.time() - max_idle_seconds
    removed: list[str] = []
    with store.transaction() as entries:
        for key, entry in list(entries.items()):
            if entry.last_used < cutoff:
                stop(entry.pid)
                entries.pop(key, None)
                removed.append(key)
    return removed


def stop_all(registry: Registry | None = None) -> list[Entry]:
    """Stops every recorded engine and empties the registry."""
    store = registry or Registry()
    stopped: list[Entry] = []
    with store.transaction() as entries:
        for _key, entry in list(entries.items()):
            stop(entry.pid)
            stopped.append(entry)
        entries.clear()
    return stopped


def stop_root(root: str, registry: Registry | None = None) -> Entry | None:
    """Stops the engine serving ``root``, if any. Returns the removed entry."""
    physical = paths.physical_root(root)
    key = paths.project_key(physical)
    store = registry or Registry()
    with store.transaction() as entries:
        entry = entries.pop(key, None)
        if entry is not None:
            stop(entry.pid)
        return entry


def describe(entries: Sequence[tuple[str, Entry]]) -> str:
    """Renders registry entries as a human-readable status table."""
    if not entries:
        return "no engines recorded"
    lines = [f"{'KEY':<34}{'PID':>8}{'PORT':>7}  {'LIVE':<6}ROOT"]
    for key, entry in entries:
        live = "yes" if verify(entry, entry.root) else "no"
        lines.append(
            f"{key:<34}{entry.pid:>8}{entry.port:>7}  {live:<6}{entry.root}"
        )
    return "\n".join(lines)
