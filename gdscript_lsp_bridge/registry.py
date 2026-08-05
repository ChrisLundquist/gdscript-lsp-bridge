"""The warm-engine registry: project key -> {port, pid} in a JSON file.

Warm reuse is the whole reason this file exists. A cold Godot editor spends
10-20 seconds importing and indexing before its language server answers
usefully, and that cost is per PROJECT ROOT, not per editor session. Recording
the running engine somewhere both durable and machine-wide lets the NEXT bridge
process -- a different Claude Code session, possibly hours later -- find the
warm one instead of paying that cost again.

Durable and machine-wide means a file in the OS temp directory, which brings
two hazards this module is built around.

CONCURRENCY. Two bridges may start against two projects at the same moment. A
naive read-modify-write loses one of the entries. Every mutation therefore runs
inside :class:`_FileLock` -- an OS advisory lock, so the kernel releases it if a
holder dies -- and writes through a temp file plus :func:`os.replace`, so a
reader never observes a half-written registry even without taking the lock.

STALENESS. Nothing guarantees a recorded process still exists. A pid can be
dead, or worse, RECYCLED onto an unrelated program. :func:`Registry.get` is
therefore only a claim; :mod:`gdscript_lsp_bridge.engine` is what verifies it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import time
from typing import Any, Iterator

from . import paths

#: Registry filename inside the state directory.
REGISTRY_FILE_NAME = "registry.json"

#: Advisory lock guarding registry mutations.
REGISTRY_LOCK_NAME = "registry.lock"

#: Bumped only if the on-disk shape changes incompatibly. A registry written by
#: a different version is discarded rather than guessed at -- it names running
#: processes, and misreading those is worse than a cold start.
SCHEMA_VERSION = 1

#: How long to wait for the registry lock before giving up.
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05


class RegistryError(Exception):
    """Raised when the registry cannot be read or written."""


class _FileLock:
    """A blocking-with-timeout OS advisory lock on a lockfile.

    Advisory rather than a lockfile-existence protocol on purpose: the kernel
    drops an advisory lock when the owning handle closes for ANY reason,
    including the process being killed, so a crashed holder needs no stale-lock
    heuristic to recover from.
    """

    def __init__(self, path: str, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self._path = path
        self._timeout = timeout
        self._descriptor: int | None = None

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + self._timeout
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        while True:
            if _try_lock(descriptor):
                self._descriptor = descriptor
                return self
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise RegistryError(
                    f"timed out after {self._timeout}s waiting for {self._path}"
                )
            time.sleep(LOCK_POLL_SECONDS)

    def __exit__(self, *_: object) -> bool:
        if self._descriptor is not None:
            _unlock(self._descriptor)
            os.close(self._descriptor)
            self._descriptor = None
        return False


def _try_lock(descriptor: int) -> bool:
    """One non-blocking attempt. True when this handle now owns the lock."""
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                return False
            raise
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            return False
        raise


def _unlock(descriptor: int) -> None:
    """Releases the lock. Closing would release it anyway; this is tidiness."""
    if os.name == "nt":
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class Entry:
    """One warm engine's recorded state.

    ``root`` is the PHYSICAL root, stored so a lookup can confirm the entry
    really belongs to the path being asked about rather than trusting the hash
    alone -- see :mod:`gdscript_lsp_bridge.paths` on why that guard exists.
    """

    __slots__ = ("root", "port", "pid", "godot", "started_at", "last_used", "log")

    def __init__(
        self,
        root: str,
        port: int,
        pid: int,
        godot: str = "",
        started_at: float = 0.0,
        last_used: float = 0.0,
        log: str = "",
    ) -> None:
        self.root = root
        self.port = int(port)
        self.pid = int(pid)
        self.godot = godot
        self.started_at = started_at or time.time()
        self.last_used = last_used or self.started_at
        self.log = log

    def to_dict(self) -> dict[str, Any]:
        """Returns the JSON-serializable form written to disk."""
        return {
            "root": self.root,
            "port": self.port,
            "pid": self.pid,
            "godot": self.godot,
            "started_at": self.started_at,
            "last_used": self.last_used,
            "log": self.log,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entry":
        """Rebuilds an entry from its on-disk form."""
        return cls(
            root=str(data.get("root", "")),
            port=int(data.get("port", 0)),
            pid=int(data.get("pid", 0)),
            godot=str(data.get("godot", "")),
            started_at=float(data.get("started_at", 0.0)),
            last_used=float(data.get("last_used", 0.0)),
            log=str(data.get("log", "")),
        )

    def __repr__(self) -> str:
        return f"Entry(root={self.root!r}, port={self.port}, pid={self.pid})"


class Registry:
    """Read/write access to the machine-wide warm-engine registry."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(paths.state_dir(), REGISTRY_FILE_NAME)
        self._lock_path = os.path.join(
            os.path.dirname(self.path) or ".", REGISTRY_LOCK_NAME
        )
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def load(self) -> dict[str, Entry]:
        """Returns every recorded entry, keyed by project key.

        A registry that is missing, empty, corrupt, or written by an
        incompatible schema reads as EMPTY rather than raising. The cost of
        being wrong here is one cold start; the cost of raising is a language
        server that refuses to start at all.
        """
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise RegistryError(f"cannot read {self.path}: {error}") from error
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            return {}
        entries = data.get("engines")
        if not isinstance(entries, dict):
            return {}
        result: dict[str, Entry] = {}
        for key, value in entries.items():
            if isinstance(value, dict):
                try:
                    result[str(key)] = Entry.from_dict(value)
                except (TypeError, ValueError):
                    continue
        return result

    def get(self, key: str) -> Entry | None:
        """Returns the entry for ``key``, or None. Liveness is NOT checked."""
        return self.load().get(key)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict[str, Entry]]:
        """Yields the mutable entry map under the registry lock.

        Whatever the body leaves in the mapping is what gets written, so a
        read-modify-write cannot interleave with another bridge's.
        """
        with _FileLock(self._lock_path):
            entries = self.load()
            yield entries
            self._write(entries)

    def _write(self, entries: dict[str, Entry]) -> None:
        """Atomically replaces the registry file with ``entries``."""
        payload = {
            "schema": SCHEMA_VERSION,
            "engines": {key: entry.to_dict() for key, entry in entries.items()},
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        temporary = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(temporary, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise RegistryError(f"cannot write {self.path}: {error}") from error

    def put(self, key: str, entry: Entry) -> None:
        """Records ``entry`` under ``key``, replacing any previous one."""
        with self.transaction() as entries:
            entries[key] = entry

    def delete(self, key: str) -> None:
        """Removes ``key`` if present. Absence is not an error."""
        with self.transaction() as entries:
            entries.pop(key, None)

    def touch(self, key: str) -> None:
        """Marks ``key`` as used now, for idle reaping. Absence is not an error."""
        with self.transaction() as entries:
            entry = entries.get(key)
            if entry is not None:
                entry.last_used = time.time()
