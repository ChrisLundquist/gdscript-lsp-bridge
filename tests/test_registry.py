"""Registry durability: survives corruption, concurrency and stale entries.

Every test here uses a registry file in its own temporary directory. The real
one lives in the OS temp directory and names live processes, so a test that
touched it could stop an engine a developer is using.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest

from gdscript_lsp_bridge import engine, paths
from gdscript_lsp_bridge.registry import Entry, Registry


class RegistryFileTest(unittest.TestCase):
    """Reading and writing the registry file."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = os.path.join(self._temporary.name, "registry.json")
        self.registry = Registry(self.path)

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(self.registry.load(), {})

    def test_put_then_get_round_trips_every_field(self) -> None:
        entry = Entry(root="/a/b", port=6005, pid=4242, godot="/bin/godot", log="/l")
        self.registry.put("k", entry)
        loaded = self.registry.get("k")
        assert loaded is not None
        self.assertEqual(loaded.root, "/a/b")
        self.assertEqual(loaded.port, 6005)
        self.assertEqual(loaded.pid, 4242)
        self.assertEqual(loaded.godot, "/bin/godot")
        self.assertEqual(loaded.log, "/l")

    def test_delete_removes_and_is_idempotent(self) -> None:
        self.registry.put("k", Entry(root="/a", port=1, pid=2))
        self.registry.delete("k")
        self.assertIsNone(self.registry.get("k"))
        self.registry.delete("k")

    def test_corrupt_file_reads_as_empty_rather_than_raising(self) -> None:
        # A cold start is a far better failure than a language server that
        # refuses to start because a temp file got truncated.
        with open(self.path, "wb") as handle:
            handle.write(b"{ this is not json")
        self.assertEqual(self.registry.load(), {})

    def test_empty_file_reads_as_empty(self) -> None:
        with open(self.path, "wb") as handle:
            handle.write(b"")
        self.assertEqual(self.registry.load(), {})

    def test_foreign_schema_is_discarded(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"schema": 999, "engines": {"k": {"pid": 1}}}, handle)
        self.assertEqual(self.registry.load(), {})

    def test_a_corrupt_registry_is_repaired_by_the_next_write(self) -> None:
        with open(self.path, "wb") as handle:
            handle.write(b"garbage")
        self.registry.put("k", Entry(root="/a", port=1, pid=2))
        self.assertIn("k", self.registry.load())

    def test_transaction_writes_what_the_body_left(self) -> None:
        with self.registry.transaction() as entries:
            entries["a"] = Entry(root="/a", port=1, pid=1)
            entries["b"] = Entry(root="/b", port=2, pid=2)
        with self.registry.transaction() as entries:
            entries.pop("a")
        self.assertEqual(sorted(self.registry.load()), ["b"])

    def test_concurrent_writers_do_not_lose_entries(self) -> None:
        # The read-modify-write that a naive implementation would race on.
        def writer(index: int) -> None:
            self.registry.put(f"k{index}", Entry(root=f"/r{index}", port=index, pid=index))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.registry.load()), 12)

    def test_touch_updates_last_used_only(self) -> None:
        self.registry.put("k", Entry(root="/a", port=1, pid=2, last_used=1.0))
        self.registry.touch("k")
        loaded = self.registry.get("k")
        assert loaded is not None
        self.assertGreater(loaded.last_used, 1.0)
        self.assertEqual(loaded.pid, 2)

    def test_touch_on_a_missing_key_is_not_an_error(self) -> None:
        self.registry.touch("absent")


class StaleEntryTest(unittest.TestCase):
    """Stale entries are detected and cleaned up rather than trusted."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.registry = Registry(os.path.join(self._temporary.name, "registry.json"))

    def test_a_dead_pid_does_not_verify(self) -> None:
        dead = _definitely_dead_pid()
        self.assertFalse(engine.verify(Entry(root="/a", port=6005, pid=dead), "/a"))

    def test_a_live_pid_with_a_dead_port_does_not_verify(self) -> None:
        # This process is certainly alive, and is certainly not a Godot LSP.
        entry = Entry(root="/a", port=_closed_port(), pid=os.getpid())
        self.assertFalse(engine.verify(entry, "/a"))

    def test_a_live_port_owned_by_a_non_godot_pid_does_not_verify(self) -> None:
        # Guards pid reuse: something is listening and the pid is alive, but
        # the process is not a Godot serving this root.
        import socket

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        entry = Entry(root="/some/root", port=port, pid=os.getpid())
        self.assertFalse(engine.verify(entry, "/some/root"))

    def test_reap_stale_removes_dead_entries_and_keeps_the_registry_usable(self) -> None:
        self.registry.put("dead1", Entry(root="/a", port=1, pid=_definitely_dead_pid()))
        self.registry.put("dead2", Entry(root="/b", port=2, pid=_definitely_dead_pid()))
        removed = engine.reap_stale(self.registry)
        self.assertEqual(sorted(removed), ["dead1", "dead2"])
        self.assertEqual(self.registry.load(), {})

    def test_reap_idle_is_off_unless_a_timeout_is_given(self) -> None:
        self.registry.put("k", Entry(root="/a", port=1, pid=1, last_used=0.0))
        self.assertEqual(engine.reap_idle(0, self.registry), [])
        self.assertIn("k", self.registry.load())

    def test_reap_idle_removes_entries_past_the_cutoff(self) -> None:
        self.registry.put(
            "old", Entry(root="/a", port=1, pid=_definitely_dead_pid(), last_used=1.0)
        )
        self.registry.put(
            "new", Entry(root="/b", port=2, pid=_definitely_dead_pid(),
                         last_used=time.time())
        )
        removed = engine.reap_idle(60.0, self.registry)
        self.assertEqual(removed, ["old"])
        self.assertIn("new", self.registry.load())

    def test_stop_root_removes_the_entry_for_that_root(self) -> None:
        root = os.path.realpath(self._temporary.name)
        key = paths.project_key(root)
        self.registry.put(key, Entry(root=root, port=1, pid=_definitely_dead_pid()))
        removed = engine.stop_root(root, self.registry)
        self.assertIsNotNone(removed)
        self.assertEqual(self.registry.load(), {})

    def test_stop_root_finds_the_entry_through_a_different_spelling(self) -> None:
        root = os.path.realpath(self._temporary.name)
        key = paths.project_key(root)
        self.registry.put(key, Entry(root=root, port=1, pid=_definitely_dead_pid()))
        self.assertIsNotNone(engine.stop_root(root + os.sep, self.registry))

    def test_stop_all_empties_the_registry(self) -> None:
        self.registry.put("a", Entry(root="/a", port=1, pid=_definitely_dead_pid()))
        self.registry.put("b", Entry(root="/b", port=2, pid=_definitely_dead_pid()))
        engine.stop_all(self.registry)
        self.assertEqual(self.registry.load(), {})


def _definitely_dead_pid() -> int:
    """Returns a pid that is not running.

    Searches upward from an implausible number rather than picking a constant,
    since any fixed pid could be in use on a busy machine.
    """
    candidate = 999_000
    while candidate < 4_000_000:
        if not engine.pid_alive(candidate):
            return candidate
        candidate += 7
    raise AssertionError("could not find an unused pid")


def _closed_port() -> int:
    """Returns a port nothing is listening on."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return port


if __name__ == "__main__":
    unittest.main()
