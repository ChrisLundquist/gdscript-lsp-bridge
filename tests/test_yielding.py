"""Lock-yield detection: is the lock actually held, and by someone else?

The detection is the part that can be wrong quietly. "Held" reported when the
lock is free strands the engine off; "free" reported when it is held defeats the
whole policy. Both modes are tested against a lock genuinely held by ANOTHER
process, because that is the only case that matters in practice.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest

from gdscript_lsp_bridge import yielding

#: Holds an exclusive advisory lock on argv[1] until its stdin closes, then
#: exits. Printing before waiting is what lets the test know the lock is taken
#: rather than sleeping and hoping.
LOCK_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
sys.stdout.write("locked\\n")
sys.stdout.flush()
sys.stdin.read()
"""


class LockHolder:
    """A subprocess holding a real advisory lock on a real file."""

    def __init__(self, path: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", LOCK_HOLDER, path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdout is not None
        ready = self.process.stdout.readline()
        if ready.strip() != "locked":
            raise AssertionError(f"lock holder failed to start: {ready!r}")

    def release(self) -> None:
        """Drops the lock and waits for the holder to exit."""
        if self.process.poll() is not None:
            return
        assert self.process.stdin is not None
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        for handle in (self.process.stdout, self.process.stdin):
            if handle is not None and not handle.closed:
                handle.close()


class DetectionTest(unittest.TestCase):
    """``lock_is_held`` in both modes."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.base = self._temporary.name
        self.lock_path = os.path.join(self.base, "engine_a.lock")
        self.pattern = os.path.join(self.base, "*.lock")

    def test_an_unset_pattern_is_never_held(self) -> None:
        self.assertFalse(yielding.lock_is_held(""))

    def test_a_pattern_matching_nothing_is_not_held(self) -> None:
        self.assertFalse(yielding.lock_is_held(self.pattern))

    def test_an_existing_but_unlocked_file_is_not_held(self) -> None:
        # The case a file-existence test gets wrong. The advisory-lock
        # convention this targets leaves its lockfile on disk after release.
        with open(self.lock_path, "wb"):
            pass
        self.assertFalse(yielding.lock_is_held(self.pattern, yielding.MODE_FLOCK))

    def test_a_lock_held_by_another_process_is_detected(self) -> None:
        holder = LockHolder(self.lock_path)
        self.addCleanup(holder.release)
        self.assertTrue(yielding.lock_is_held(self.pattern, yielding.MODE_FLOCK))
        self.assertEqual(
            yielding.held_paths(self.pattern, yielding.MODE_FLOCK), [self.lock_path]
        )

    def test_release_is_observed(self) -> None:
        holder = LockHolder(self.lock_path)
        self.assertTrue(yielding.lock_is_held(self.pattern, yielding.MODE_FLOCK))
        holder.release()
        self.assertFalse(yielding.lock_is_held(self.pattern, yielding.MODE_FLOCK))

    def test_any_match_in_the_glob_counts(self) -> None:
        with open(os.path.join(self.base, "engine_b.lock"), "wb"):
            pass
        holder = LockHolder(self.lock_path)
        self.addCleanup(holder.release)
        self.assertTrue(yielding.lock_is_held(self.pattern, yielding.MODE_FLOCK))

    def test_exists_mode_reports_an_unlocked_file_as_held(self) -> None:
        with open(self.lock_path, "wb"):
            pass
        self.assertFalse(yielding.lock_is_held(self.pattern, yielding.MODE_FLOCK))
        self.assertTrue(yielding.lock_is_held(self.pattern, yielding.MODE_EXISTS))

    def test_an_unreadable_match_is_not_assumed_held(self) -> None:
        # Guessing "held" on a file we cannot inspect would strand the engine.
        with open(self.lock_path, "wb"):
            pass
        os.chmod(self.lock_path, 0o000)
        self.addCleanup(os.chmod, self.lock_path, 0o600)
        if os.geteuid() == 0:
            self.skipTest("root can open anything")
        self.assertFalse(yielding.lock_is_held(self.pattern, yielding.MODE_FLOCK))


class ConfigurationTest(unittest.TestCase):
    """The policy is off unless the environment turns it on."""

    def setUp(self) -> None:
        self._saved = {
            name: os.environ.get(name)
            for name in (
                yielding.YIELD_GLOB_ENV,
                yielding.YIELD_MODE_ENV,
                yielding.YIELD_POLL_ENV,
                yielding.YIELD_MAX_WAIT_ENV,
            )
        }
        for name in self._saved:
            os.environ.pop(name, None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_off_by_default(self) -> None:
        self.assertEqual(yielding.configured_glob(), "")

    def test_glob_is_read_from_the_environment(self) -> None:
        os.environ[yielding.YIELD_GLOB_ENV] = "/tmp/sr_lock_*.lock"
        self.assertEqual(yielding.configured_glob(), "/tmp/sr_lock_*.lock")

    def test_mode_defaults_to_flock(self) -> None:
        self.assertEqual(yielding.configured_mode(), yielding.MODE_FLOCK)

    def test_mode_can_be_set_to_exists(self) -> None:
        os.environ[yielding.YIELD_MODE_ENV] = "exists"
        self.assertEqual(yielding.configured_mode(), yielding.MODE_EXISTS)

    def test_an_unrecognized_mode_falls_back_to_flock(self) -> None:
        os.environ[yielding.YIELD_MODE_ENV] = "nonsense"
        self.assertEqual(yielding.configured_mode(), yielding.MODE_FLOCK)

    def test_a_nonsense_poll_interval_falls_back_to_the_default(self) -> None:
        os.environ[yielding.YIELD_POLL_ENV] = "not-a-number"
        self.assertEqual(
            yielding.configured_poll_seconds(), yielding.DEFAULT_POLL_SECONDS
        )

    def test_a_negative_poll_interval_falls_back_to_the_default(self) -> None:
        os.environ[yielding.YIELD_POLL_ENV] = "-5"
        self.assertEqual(
            yielding.configured_poll_seconds(), yielding.DEFAULT_POLL_SECONDS
        )


class WatcherTest(unittest.TestCase):
    """The watcher thread fires the callback and reports the release."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.lock_path = os.path.join(self._temporary.name, "engine.lock")
        self.pattern = os.path.join(self._temporary.name, "*.lock")

    def test_the_callback_fires_while_the_lock_is_held(self) -> None:
        fired: list[float] = []
        watcher = yielding.LockYieldWatcher(
            self.pattern, on_yield=lambda: fired.append(time.monotonic()),
            poll_seconds=0.05,
        )
        holder = LockHolder(self.lock_path)
        self.addCleanup(holder.release)
        watcher.start()
        self.addCleanup(watcher.stop)
        deadline = time.monotonic() + 10.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(fired, "watcher never reported the held lock")

    def test_the_callback_does_not_fire_when_the_lock_is_free(self) -> None:
        with open(self.lock_path, "wb"):
            pass
        fired: list[float] = []
        watcher = yielding.LockYieldWatcher(
            self.pattern, on_yield=lambda: fired.append(time.monotonic()),
            poll_seconds=0.05,
        )
        watcher.start()
        self.addCleanup(watcher.stop)
        time.sleep(1.0)
        self.assertEqual(fired, [])

    def test_wait_for_release_returns_when_the_holder_exits(self) -> None:
        watcher = yielding.LockYieldWatcher(
            self.pattern, on_yield=lambda: None, poll_seconds=0.05
        )
        holder = LockHolder(self.lock_path)
        import threading

        threading.Timer(0.5, holder.release).start()
        self.assertTrue(watcher.wait_for_release(max_wait=20.0))

    def test_wait_for_release_times_out_rather_than_hanging_forever(self) -> None:
        # An LSP that stays dead because someone left a lock held is worse
        # than one that contends, so the wait is bounded.
        watcher = yielding.LockYieldWatcher(
            self.pattern, on_yield=lambda: None, poll_seconds=0.05
        )
        holder = LockHolder(self.lock_path)
        self.addCleanup(holder.release)
        self.assertFalse(watcher.wait_for_release(max_wait=1.0))

    def test_a_raising_callback_does_not_kill_the_watcher(self) -> None:
        calls: list[int] = []

        def explode() -> None:
            calls.append(1)
            raise RuntimeError("boom")

        watcher = yielding.LockYieldWatcher(
            self.pattern, on_yield=explode, poll_seconds=0.05
        )
        holder = LockHolder(self.lock_path)
        self.addCleanup(holder.release)
        watcher.start()
        self.addCleanup(watcher.stop)
        deadline = time.monotonic() + 10.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreaterEqual(len(calls), 2, "watcher died on the first exception")


if __name__ == "__main__":
    unittest.main()
