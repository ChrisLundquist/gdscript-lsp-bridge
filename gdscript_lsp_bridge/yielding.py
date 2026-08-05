"""Optional policy: yield the engine slot while another tool holds a lock.

OFF BY DEFAULT, AND OUT OF THE CORE PATH. Nothing here runs unless
``GDSCRIPT_LSP_YIELD_LOCKFILE`` names a glob, and the session's normal
request/response path contains no reference to this module beyond the one
construction site. That separation is the requirement: the bridge is a generic
Godot tool, and this is a courtesy for one particular kind of neighbour.

THE PROBLEM IT SOLVES. Some Godot projects serialize engine runs behind a lock,
because two engines against one project root fight over a single ``.godot``
directory -- one regenerable filesystem cache, one import-readiness marker. A
warm LSP engine is exactly such a run, and it is a *background* one, so a
validation or export run that takes the lock finds it already held by a process
the user forgot exists. Pointing this bridge at that lock makes the LSP the one
that steps aside.

HOW "HELD" IS DETECTED. By trying to take the lock, not by looking for the
file. The advisory-lock convention this targets does not delete its lockfile on
release -- the kernel simply drops the lock when the holder's handle closes --
so a file-existence test reports "held" forever after the first use. The
default mode therefore opens each match and attempts a non-blocking exclusive
lock, releasing immediately if it succeeds. ``GDSCRIPT_LSP_YIELD_MODE=exists``
selects the cruder test for locks that DO unlink on release.
"""

from __future__ import annotations

import contextlib
import errno
import glob as globbing
import os
import threading
import time
from typing import Callable

#: Environment variable naming the lockfile glob. Unset means "never yield".
YIELD_GLOB_ENV = "GDSCRIPT_LSP_YIELD_LOCKFILE"

#: ``flock`` (default) or ``exists``.
YIELD_MODE_ENV = "GDSCRIPT_LSP_YIELD_MODE"

#: Seconds between polls of the lock.
YIELD_POLL_ENV = "GDSCRIPT_LSP_YIELD_POLL"
DEFAULT_POLL_SECONDS = 2.0

#: How long to keep waiting for a release before giving up and restarting the
#: engine anyway. A validation run that never finishes must not leave the
#: editor permanently without code intelligence.
YIELD_MAX_WAIT_ENV = "GDSCRIPT_LSP_YIELD_MAX_WAIT"
DEFAULT_MAX_WAIT_SECONDS = 1800.0

MODE_FLOCK = "flock"
MODE_EXISTS = "exists"


def configured_glob() -> str:
    """Returns the configured lockfile glob, or "" when the policy is off."""
    return os.environ.get(YIELD_GLOB_ENV, "").strip()


def configured_mode() -> str:
    """Returns the configured detection mode, defaulting to ``flock``."""
    mode = os.environ.get(YIELD_MODE_ENV, MODE_FLOCK).strip().lower()
    return MODE_EXISTS if mode == MODE_EXISTS else MODE_FLOCK


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def configured_poll_seconds() -> float:
    """Returns the poll interval in seconds."""
    return _float_env(YIELD_POLL_ENV, DEFAULT_POLL_SECONDS)


def configured_max_wait_seconds() -> float:
    """Returns how long to wait for a release before proceeding regardless."""
    return _float_env(YIELD_MAX_WAIT_ENV, DEFAULT_MAX_WAIT_SECONDS)


def _flock_held(path: str) -> bool:
    """True when ``path`` carries an advisory lock this process cannot take."""
    try:
        descriptor = os.open(path, os.O_RDWR)
    except OSError:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            # Unreadable is not evidence of a holder, and guessing "held" here
            # would strand the engine off indefinitely.
            return False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                    return True
                return False
            with contextlib.suppress(OSError):
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return False
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            return False
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


def lock_is_held(pattern: str, mode: str = "") -> bool:
    """True when any file matching ``pattern`` is currently locked."""
    if not pattern:
        return False
    effective = mode or configured_mode()
    matches = globbing.glob(pattern)
    if effective == MODE_EXISTS:
        return bool(matches)
    for path in matches:
        if os.path.isfile(path) and _flock_held(path):
            return True
    return False


def held_paths(pattern: str, mode: str = "") -> list[str]:
    """Returns the matching paths currently considered held. For diagnostics."""
    if not pattern:
        return []
    effective = mode or configured_mode()
    matches = sorted(globbing.glob(pattern))
    if effective == MODE_EXISTS:
        return matches
    return [p for p in matches if os.path.isfile(p) and _flock_held(p)]


class LockYieldWatcher:
    """Polls a lockfile glob and drives yield/resume callbacks.

    ``on_yield`` is called once when the lock becomes held, and is expected to
    stop the engine, wait for the release this watcher reports through
    ``wait_for_release``, and bring a new engine up. Keeping the *sequencing*
    in the caller and only the *detection* here is what keeps the session's
    reconnect logic in one readable place.
    """

    def __init__(
        self,
        pattern: str,
        on_yield: Callable[[], None],
        mode: str = "",
        poll_seconds: float = 0.0,
        logger: object | None = None,
    ) -> None:
        self.pattern = pattern
        self.mode = mode or configured_mode()
        self.poll_seconds = poll_seconds or configured_poll_seconds()
        self._on_yield = on_yield
        self._logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Begins polling on a daemon thread. No-op if already started."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="gdscript-lsp-yield-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signals the watcher to finish."""
        self._stop.set()

    def _log(self, level: str, message: str) -> None:
        if self._logger is not None:
            getattr(self._logger, level, lambda _m: None)(message)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if lock_is_held(self.pattern, self.mode):
                    self._log(
                        "info",
                        f"lock held ({', '.join(held_paths(self.pattern, self.mode))}); "
                        "yielding the engine",
                    )
                    self._on_yield()
            except Exception as error:  # noqa: BLE001 - a watcher must not die
                self._log("warning", f"yield watcher error: {error}")
            self._stop.wait(self.poll_seconds)

    def wait_for_release(self, max_wait: float = 0.0) -> bool:
        """Blocks until the lock is free or ``max_wait`` elapses.

        Returns True on a genuine release, False on timeout -- the caller
        relaunches either way, because an LSP that stays dead because someone
        left a lock held is worse than one that contends.
        """
        limit = max_wait or configured_max_wait_seconds()
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            if not lock_is_held(self.pattern, self.mode):
                return True
            time.sleep(min(self.poll_seconds, 1.0))
        return not lock_is_held(self.pattern, self.mode)
