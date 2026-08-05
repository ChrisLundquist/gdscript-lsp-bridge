"""Regression cover for the parent-death guard.

QA found an engine outliving a bridge that was SIGKILLed with persist disabled:
teardown stops the engine on an orderly exit, but a SIGKILL runs no teardown of
ours at all, and ``engine.spawn`` detaches the engine on purpose. The guard
sidecar closes that gap; these tests keep it closed.
"""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
import time
import unittest

from gdscript_lsp_bridge import engine, guard, paths
from gdscript_lsp_bridge.registry import Registry

from .lsp_client import BridgeClient
from .test_end_to_end import BRIDGE, PROJECT

INITIALIZE_TIMEOUT = 240.0
#: The guard polls once a second and gives SIGTERM a grace window.
GUARD_DEADLINE_SECONDS = 30.0

_state_dir: str = ""
_environment: dict[str, str] = {}


def setUpModule() -> None:
    global _state_dir, _environment
    if os.environ.get("GDSCRIPT_LSP_SKIP_ENGINE_TESTS"):
        raise unittest.SkipTest("GDSCRIPT_LSP_SKIP_ENGINE_TESTS is set")
    if os.name == "nt":
        raise unittest.SkipTest("the POSIX guard does not apply on Windows")
    if not shutil.which("godot") and not os.path.exists(
        "/Applications/Godot.app/Contents/MacOS/Godot"
    ):
        raise unittest.SkipTest("no Godot binary available")
    _state_dir = tempfile.mkdtemp(prefix="gdscript-lsp-guard-")
    _environment = dict(os.environ)
    _environment["TMPDIR"] = _state_dir
    _environment["GDSCRIPT_LSP_LOG_LEVEL"] = "info"
    _environment.pop("GDSCRIPT_LSP_YIELD_LOCKFILE", None)


def tearDownModule() -> None:
    if not _state_dir:
        return
    try:
        engine.stop_all(
            Registry(os.path.join(_state_dir, "gdscript-lsp-bridge", "registry.json"))
        )
    finally:
        shutil.rmtree(_state_dir, ignore_errors=True)


def _engine_pid_for(root: str) -> int:
    """The recorded engine pid for ``root`` in this module's private registry."""
    registry = Registry(
        os.path.join(_state_dir, "gdscript-lsp-bridge", "registry.json")
    )
    entry = registry.get(paths.project_key(paths.physical_root(root)))
    return entry.pid if entry is not None else 0


def _wait_gone(pid: int, deadline: float) -> bool:
    """True once ``pid`` is gone; False if it outlives ``deadline`` seconds."""
    limit = time.monotonic() + deadline
    while time.monotonic() < limit:
        if not engine.pid_alive(pid):
            return True
        time.sleep(0.5)
    return not engine.pid_alive(pid)


class GuardIdentityTest(unittest.TestCase):
    """The PID-reuse proof, which is what keeps the guard from killing strangers."""

    def test_marker_names_the_port_not_the_binary(self) -> None:
        self.assertEqual(engine.guard_marker(61234), "--lsp-port 61234")

    def test_a_pid_running_something_else_is_never_ours(self) -> None:
        # This test process is alive and is certainly not a Godot LSP server.
        self.assertFalse(guard.still_our_engine(os.getpid(), "--lsp-port 61234"))

    def test_an_empty_marker_is_never_a_match(self) -> None:
        self.assertFalse(guard.still_our_engine(os.getpid(), ""))

    def test_a_dead_pid_is_not_alive(self) -> None:
        self.assertFalse(guard.pid_alive(-1))


class GuardRegressionTest(unittest.TestCase):
    """The behaviour QA caught: a SIGKILLed bridge must not leave an engine."""

    def setUp(self) -> None:
        """Starts every test from zero engines.

        Without this these tests silently pass for the wrong reason: a warm
        engine left by a sibling test is REUSED, and a reused engine is
        deliberately neither guarded nor stopped -- it belongs to whoever
        spawned it. The session would then leave it running and the assertion
        would be measuring test order rather than the guard. QA hit exactly
        this confound before the guard existed.
        """
        registry = Registry(
            os.path.join(_state_dir, "gdscript-lsp-bridge", "registry.json")
        )
        engine.stop_all(registry)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and _engine_pid_for(PROJECT):
            time.sleep(0.5)
        self.assertFalse(
            _engine_pid_for(PROJECT), "could not reach a zero-engine baseline"
        )

    def test_sigkilled_bridge_does_not_leak_its_engine(self) -> None:
        client = BridgeClient(BRIDGE, env={**_environment, "GDSCRIPT_LSP_PERSIST": "0"})
        client.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
        engine_pid = _engine_pid_for(PROJECT)
        self.assertTrue(engine_pid, "the session should have recorded an engine")
        self.assertTrue(engine.pid_alive(engine_pid))

        os.kill(client.process.pid, signal.SIGKILL)  # no teardown of ours runs
        try:
            client.process.wait(timeout=10)
        except Exception:
            pass

        self.assertTrue(
            _wait_gone(engine_pid, GUARD_DEADLINE_SECONDS),
            f"engine pid={engine_pid} outlived the bridge that owned it",
        )

    def test_persist_keeps_the_engine_when_the_bridge_is_killed(self) -> None:
        """The default is warm reuse: a kill must NOT take the engine."""
        client = BridgeClient(BRIDGE, env={**_environment, "GDSCRIPT_LSP_PERSIST": "1"})
        client.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
        engine_pid = _engine_pid_for(PROJECT)
        self.assertTrue(engine_pid, "the session should have recorded an engine")

        os.kill(client.process.pid, signal.SIGKILL)
        try:
            client.process.wait(timeout=10)
        except Exception:
            pass
        time.sleep(8)  # comfortably past the guard's poll interval

        self.assertTrue(
            engine.pid_alive(engine_pid),
            "a persisted engine must survive its bridge being killed",
        )


if __name__ == "__main__":
    unittest.main()
