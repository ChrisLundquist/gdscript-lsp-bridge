"""Parent-death guard: an engine must not outlive a bridge that cannot clean up.

WHY THIS EXISTS. ``engine.spawn`` detaches the engine with ``start_new_session``
so a warm engine survives the bridge exiting -- that is the whole point of warm
reuse, and it is correct for the default ``GDSCRIPT_LSP_PERSIST=1``.

With persist DISABLED the session stops the engine in its own teardown, but a
teardown only runs when the bridge gets to run it. A bridge killed outright --
SIGKILL from an editor shutting a language server down, or an agent runner's
watchdog ending a turn -- runs no teardown at all, and the detached engine
reparents to init and stays. QA reproduced exactly that.

This sidecar is the half that needs nobody alive to work. It starts in its OWN
session, so the process-group kill that ends the bridge does not take it, and it
watches ONE recorded engine, terminating it when its bridge goes away.

WHAT IT MAY KILL, AND HOW THAT IS BOUNDED. Never a process by name: scanning for
``Godot*`` and killing matches would be indistinguishable from killing somebody
else's live engine. This guard signals a process group only when all of:

* the recorded bridge pid is gone;
* the recorded engine pid is still alive;
* that pid's command line still matches the argv the bridge recorded at spawn --
  the PID-reuse proof, since a recycled pid running something else cannot match.

Any failed check exits without signalling. POSIX only: Windows detaches through
a job object instead, which the kernel empties on its own.
"""

from __future__ import annotations

import os
import signal
import sys
import time

POLL_SECONDS = 1.0
TERM_GRACE_SECONDS = 5.0
#: Give up rather than poll forever if a bridge somehow never exits.
MAX_LIFETIME_SECONDS = 60 * 60 * 24


def pid_alive(pid: int) -> bool:
    """True when ``pid`` exists and can be signalled."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def command_line(pid: int) -> str:
    """Best-effort command line for ``pid``; empty when it cannot be read."""
    try:
        import subprocess

        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def still_our_engine(pid: int, marker: str) -> bool:
    """PID-reuse proof: the pid must still be running the recorded argv."""
    if not marker:
        return False
    return marker in command_line(pid)


def stop_group(pid: int) -> None:
    """SIGTERM the engine's process group, then SIGKILL what remains."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def watch(bridge_pid: int, engine_pid: int, marker: str) -> int:
    """Blocks until the bridge exits, then stops the engine. Returns an rc."""
    deadline = time.monotonic() + MAX_LIFETIME_SECONDS
    while time.monotonic() < deadline:
        if not pid_alive(engine_pid):
            return 0                      # engine already gone; nothing to do
        if not pid_alive(bridge_pid):
            break
        time.sleep(POLL_SECONDS)
    else:
        return 0                          # lifetime cap; leave the engine alone

    if not still_our_engine(engine_pid, marker):
        return 0                          # pid was recycled -- never signal
    stop_group(engine_pid)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3:
        sys.stderr.write("usage: guard.py BRIDGE_PID ENGINE_PID MARKER\n")
        return 2
    try:
        bridge_pid = int(args[0])
        engine_pid = int(args[1])
    except ValueError:
        sys.stderr.write("guard: pids must be integers\n")
        return 2
    return watch(bridge_pid, engine_pid, args[2])


if __name__ == "__main__":
    raise SystemExit(main())
