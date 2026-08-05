"""End-to-end: a real bridge subprocess driving a real Godot language server.

These tests start Godot. They are slow (a cold engine takes seconds to tens of
seconds) and they are the only tests that prove the thing actually works, so
they are worth the wait. Set ``GDSCRIPT_LSP_SKIP_ENGINE_TESTS=1`` to skip them.

ISOLATION. Every engine started here is keyed under a private ``TMPDIR``, so
the registry these tests read and write is not the developer's. Without that, a
test run would reuse -- or worse, stop -- an engine serving a real project. The
module tears down every engine it started.

The project under test is the throwaway one in this repository. Nothing here
points at a real game project, deliberately: a second engine against a project
root someone is working in contends for its ``.godot`` cache.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from gdscript_lsp_bridge import engine, paths
from gdscript_lsp_bridge.registry import Registry

from .lsp_client import BridgeClient

REPOSITORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(REPOSITORY, "bridge.py")
PROJECT = os.path.join(REPOSITORY, "test_project")
MAIN_GD = os.path.join(PROJECT, "scripts", "main.gd")
GREETER_GD = os.path.join(PROJECT, "scripts", "greeter.gd")

#: Generous: the first engine of a run pays a cold import.
INITIALIZE_TIMEOUT = 240.0

#: Godot indexes asynchronously after the port opens, so the first query can
#: land before the workspace is fully built. Retry rather than sleep-and-hope.
QUERY_RETRIES = 12
QUERY_RETRY_DELAY = 1.0

_state_dir: str = ""
_environment: dict[str, str] = {}


def setUpModule() -> None:
    """Points the bridge's state directory at a private temporary one."""
    global _state_dir, _environment
    if os.environ.get("GDSCRIPT_LSP_SKIP_ENGINE_TESTS"):
        raise unittest.SkipTest("GDSCRIPT_LSP_SKIP_ENGINE_TESTS is set")
    if not shutil.which("godot") and not os.path.exists(
        "/Applications/Godot.app/Contents/MacOS/Godot"
    ):
        raise unittest.SkipTest("no Godot binary available")
    _state_dir = tempfile.mkdtemp(prefix="gdscript-lsp-tests-")
    _environment = dict(os.environ)
    _environment["TMPDIR"] = _state_dir
    _environment["GDSCRIPT_LSP_LOG_LEVEL"] = "info"
    # Never inherit a developer's yield configuration into a test run.
    _environment.pop("GDSCRIPT_LSP_YIELD_LOCKFILE", None)


def tearDownModule() -> None:
    """Stops every engine these tests started."""
    if not _state_dir:
        return
    try:
        engine.stop_all(test_registry())
    finally:
        shutil.rmtree(_state_dir, ignore_errors=True)


def test_registry() -> Registry:
    """Returns a Registry pointed at the tests' private state directory."""
    return Registry(os.path.join(_state_dir, "gdscript-lsp-bridge", "registry.json"))


def make_client(stderr_name: str = "") -> BridgeClient:
    """Starts a bridge subprocess against the private state directory."""
    stderr_path = os.path.join(_state_dir, stderr_name) if stderr_name else ""
    return BridgeClient(BRIDGE, env=_environment, stderr_path=stderr_path)


def retry_query(client: BridgeClient, method: str, params: dict, predicate) -> object:
    """Repeats a query until ``predicate`` accepts the result.

    Godot's index is built asynchronously, so a fixed sleep is either too short
    (flaky) or too long (slow). Retrying on the actual condition is neither.
    """
    last: object = None
    for _ in range(QUERY_RETRIES):
        response = client.request(method, params)
        last = response.get("result")
        if predicate(last):
            return last
        time.sleep(QUERY_RETRY_DELAY)
    return last


def flatten(symbols: object) -> list[dict]:
    """Flattens a possibly-nested documentSymbol tree into one list."""
    output: list[dict] = []
    if not isinstance(symbols, list):
        return output
    for symbol in symbols:
        if not isinstance(symbol, dict):
            continue
        output.append(symbol)
        output.extend(flatten(symbol.get("children")))
    return output


class LanguageFeatureTest(unittest.TestCase):
    """Real answers from Godot, relayed through the bridge."""

    client: BridgeClient
    main_uri: str
    greeter_uri: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = make_client("features.stderr.log")
        response = cls.client.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
        if "result" not in response:
            cls.client.close()
            raise AssertionError(f"initialize failed: {response}")
        cls.capabilities = response["result"].get("capabilities", {})
        cls.main_uri = cls.client.open_document(MAIN_GD)
        cls.greeter_uri = cls.client.open_document(GREETER_GD)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def test_initialize_reports_godots_own_capabilities(self) -> None:
        self.assertTrue(self.capabilities.get("documentSymbolProvider"))
        self.assertTrue(self.capabilities.get("definitionProvider"))
        self.assertTrue(self.capabilities.get("referencesProvider"))
        self.assertTrue(self.capabilities.get("hoverProvider"))

    def test_document_symbol_returns_the_real_symbols(self) -> None:
        result = retry_query(
            self.client,
            "textDocument/documentSymbol",
            {"textDocument": {"uri": self.main_uri}},
            lambda value: bool(flatten(value)),
        )
        names = {symbol.get("name") for symbol in flatten(result)}
        self.assertIn("_ready", names)
        self.assertIn("report", names)
        self.assertIn("_greeter", names)
        self.assertIn("_counter", names)

    def test_document_symbol_carries_detail_and_position(self) -> None:
        result = retry_query(
            self.client,
            "textDocument/documentSymbol",
            {"textDocument": {"uri": self.greeter_uri}},
            lambda value: bool(flatten(value)),
        )
        by_name = {symbol.get("name"): symbol for symbol in flatten(result)}
        self.assertIn("greet", by_name)
        greet = by_name["greet"]
        self.assertIn("who", str(greet.get("detail", "")))
        # Independently derived: read the declaration's line out of the file
        # rather than restating what the server said.
        with open(GREETER_GD, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        expected_line = next(
            index for index, line in enumerate(lines) if line.startswith("func greet(")
        )
        self.assertEqual(greet["selectionRange"]["start"]["line"], expected_line)

    def test_definition_crosses_files(self) -> None:
        line, character = locate(MAIN_GD, "_greeter.greet(", "greet(")
        result = retry_query(
            self.client,
            "textDocument/definition",
            {
                "textDocument": {"uri": self.main_uri},
                "position": {"line": line, "character": character},
            },
            lambda value: bool(value),
        )
        self.assertTrue(result, "definition returned nothing")
        locations = result if isinstance(result, list) else [result]
        self.assertTrue(
            any("greeter.gd" in location.get("uri", "") for location in locations),
            f"definition did not point at greeter.gd: {locations}",
        )

    def test_references_finds_call_sites_in_another_file(self) -> None:
        line, character = locate(GREETER_GD, "func greet(", "greet(")
        result = retry_query(
            self.client,
            "textDocument/references",
            {
                "textDocument": {"uri": self.greeter_uri},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": True},
            },
            lambda value: isinstance(value, list)
            and any("main.gd" in item.get("uri", "") for item in value),
        )
        self.assertIsInstance(result, list)
        files = {os.path.basename(paths.uri_to_path(item["uri"])) for item in result}
        self.assertIn("greeter.gd", files)
        self.assertIn(
            "main.gd", files, f"no cross-file reference found; got {result}"
        )
        # main.gd calls greet() from _ready and from report().
        main_hits = [item for item in result if item["uri"].endswith("main.gd")]
        self.assertGreaterEqual(len(main_hits), 2)

    def test_hover_returns_the_doc_comment(self) -> None:
        line, character = locate(MAIN_GD, "_greeter.greet(", "greet(")
        result = retry_query(
            self.client,
            "textDocument/hover",
            {
                "textDocument": {"uri": self.main_uri},
                "position": {"line": line, "character": character},
            },
            lambda value: isinstance(value, dict) and bool(value.get("contents")),
        )
        self.assertIsInstance(result, dict)
        contents = result.get("contents", {})
        text = contents.get("value", "") if isinstance(contents, dict) else str(contents)
        self.assertIn("greet", text)
        self.assertIn("Returns a greeting", text)

    def test_workspace_symbol_is_unsupported_by_godot(self) -> None:
        # Recorded as a fact about Godot 4.7, not as an aspiration. The
        # capability advertisement and the actual reply agree.
        self.assertFalse(self.capabilities.get("workspaceSymbolProvider"))
        response = self.client.request("workspace/symbol", {"query": "Greeter"})
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32601)

    def test_shutdown_is_answered_without_killing_the_engine(self) -> None:
        response = self.client.request("shutdown")
        self.assertIn("result", response)
        entry = _entry_for(PROJECT)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertTrue(engine.pid_alive(entry.pid))


class WarmReuseTest(unittest.TestCase):
    """A second bridge process reuses the first one's engine."""

    def test_a_second_process_reuses_the_same_engine(self) -> None:
        first = make_client("reuse-a.stderr.log")
        try:
            first.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
            entry = _entry_for(PROJECT)
            self.assertIsNotNone(entry)
            assert entry is not None
            original_pid, original_port = entry.pid, entry.port
        finally:
            first.close()

        # The engine must survive the bridge that started it -- that is the
        # whole point of warm reuse.
        self.assertTrue(engine.pid_alive(original_pid))

        second = make_client("reuse-b.stderr.log")
        try:
            started = time.monotonic()
            second.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
            elapsed = time.monotonic() - started
            reused = _entry_for(PROJECT)
            self.assertIsNotNone(reused)
            assert reused is not None
            self.assertEqual(reused.pid, original_pid)
            self.assertEqual(reused.port, original_port)
            # A reuse skips the whole editor boot. Asserting a bound rather
            # than a measured duration keeps this from being a benchmark.
            self.assertLess(elapsed, 10.0, "reuse should not pay a cold start")

            # And it is a working session, not merely a connected one.
            uri = second.open_document(MAIN_GD)
            result = retry_query(
                second,
                "textDocument/documentSymbol",
                {"textDocument": {"uri": uri}},
                lambda value: bool(flatten(value)),
            )
            self.assertIn("_ready", {s.get("name") for s in flatten(result)})
        finally:
            second.close()

    def test_two_concurrent_sessions_share_one_engine(self) -> None:
        first = make_client("concurrent-a.stderr.log")
        second = make_client("concurrent-b.stderr.log")
        try:
            first.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
            second.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
            entries = [
                entry for entry in test_registry().load().values()
                if entry.root == paths.physical_root(PROJECT)
            ]
            self.assertEqual(len(entries), 1, "one root must yield one engine")

            first_uri = first.open_document(MAIN_GD)
            second_uri = second.open_document(GREETER_GD)
            first_result = retry_query(
                first,
                "textDocument/documentSymbol",
                {"textDocument": {"uri": first_uri}},
                lambda value: bool(flatten(value)),
            )
            second_result = retry_query(
                second,
                "textDocument/documentSymbol",
                {"textDocument": {"uri": second_uri}},
                lambda value: bool(flatten(value)),
            )
            self.assertIn("_ready", {s.get("name") for s in flatten(first_result)})
            self.assertIn("greet", {s.get("name") for s in flatten(second_result)})
        finally:
            first.close()
            second.close()

    def test_a_stale_entry_is_replaced_rather_than_trusted(self) -> None:
        # Start an engine, kill it behind the registry's back, then confirm the
        # next session notices and respawns instead of connecting to nothing.
        primer = make_client("stale-a.stderr.log")
        try:
            primer.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
        finally:
            primer.close()
        entry = _entry_for(PROJECT)
        self.assertIsNotNone(entry)
        assert entry is not None
        dead_pid = entry.pid
        engine.stop(dead_pid)
        self.assertFalse(engine.pid_alive(dead_pid))
        # The registry still claims the dead engine at this point.
        self.assertEqual(_entry_for(PROJECT).pid, dead_pid)  # type: ignore[union-attr]

        recovered = make_client("stale-b.stderr.log")
        try:
            response = recovered.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
            self.assertIn("result", response)
            replacement = _entry_for(PROJECT)
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertNotEqual(replacement.pid, dead_pid)
            self.assertTrue(engine.pid_alive(replacement.pid))
        finally:
            recovered.close()


class InitializeFailureTest(unittest.TestCase):
    """A workspace that is not a Godot project fails with a usable message."""

    def test_a_non_godot_workspace_is_refused(self) -> None:
        empty = tempfile.mkdtemp(prefix="not-a-godot-project-", dir=_state_dir)
        client = make_client("failure.stderr.log")
        try:
            response = client.initialize(empty, timeout=60.0)
            self.assertIn("error", response)
            self.assertIn("project.godot", response["error"]["message"])
        finally:
            client.close()

    def test_no_engine_is_recorded_for_a_failed_initialize(self) -> None:
        empty = tempfile.mkdtemp(prefix="also-not-a-project-", dir=_state_dir)
        client = make_client("failure2.stderr.log")
        try:
            client.initialize(empty, timeout=60.0)
        finally:
            client.close()
        self.assertIsNone(_entry_for(empty))


class MonorepoWorkspaceTest(unittest.TestCase):
    """A workspace whose Godot project lives in a subdirectory.

    Exercises the initialize-rewrite path against a real engine: the client
    names the repository, the engine must be pointed at the project inside it,
    and the URIs that come back must resolve against the project -- not the
    repository -- or every location is subtly wrong.
    """

    def test_the_project_inside_the_workspace_is_served(self) -> None:
        repository = tempfile.mkdtemp(prefix="monorepo-", dir=_state_dir)
        game = os.path.join(repository, "game")
        shutil.copytree(
            PROJECT,
            game,
            ignore=shutil.ignore_patterns(".godot", ".import"),
        )
        # Sibling directories that are not the project, to prove the search
        # picks the one with project.godot rather than the first thing it sees.
        os.makedirs(os.path.join(repository, "docs"))
        os.makedirs(os.path.join(repository, "tools"))

        client = make_client("monorepo.stderr.log")
        try:
            response = client.initialize(repository, timeout=INITIALIZE_TIMEOUT)
            self.assertIn("result", response, f"initialize failed: {response}")

            # The engine must be recorded under the PROJECT, not the workspace.
            self.assertIsNone(_entry_for(repository))
            entry = _entry_for(game)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.root, paths.physical_root(game))

            uri = client.open_document(os.path.join(game, "scripts", "main.gd"))
            result = retry_query(
                client,
                "textDocument/documentSymbol",
                {"textDocument": {"uri": uri}},
                lambda value: bool(flatten(value)),
            )
            self.assertIn("_ready", {s.get("name") for s in flatten(result)})

            line, character = locate(
                os.path.join(game, "scripts", "main.gd"), "_greeter.greet(", "greet("
            )
            definition = retry_query(
                client,
                "textDocument/definition",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line, "character": character},
                },
                lambda value: bool(value),
            )
            locations = definition if isinstance(definition, list) else [definition]
            target = paths.uri_to_path(locations[0]["uri"])
            # Resolved inside the copied project, not the original fixture.
            self.assertTrue(
                target.startswith(paths.physical_root(game)),
                f"definition resolved outside the workspace's project: {target}",
            )
        finally:
            client.close()
            engine.stop_root(game, test_registry())


class LockYieldTest(unittest.TestCase):
    """The opt-in policy, end to end: stop, wait, relaunch, keep serving.

    Proves the whole sequence against a real engine and a lock really held by
    another process -- the unit tests cover detection, this covers the part
    that has to survive a live session losing its server underneath it.
    """

    def test_the_engine_yields_and_comes_back(self) -> None:
        from .test_yielding import LockHolder

        lock_dir = tempfile.mkdtemp(prefix="yield-locks-", dir=_state_dir)
        pattern = os.path.join(lock_dir, "*.lock")
        lock_path = os.path.join(lock_dir, "engine.lock")

        environment = dict(_environment)
        environment["GDSCRIPT_LSP_YIELD_LOCKFILE"] = pattern
        environment["GDSCRIPT_LSP_YIELD_POLL"] = "0.5"
        client = BridgeClient(
            BRIDGE,
            env=environment,
            stderr_path=os.path.join(_state_dir, "yield.stderr.log"),
        )
        holder: LockHolder | None = None
        try:
            client.initialize(PROJECT, timeout=INITIALIZE_TIMEOUT)
            uri = client.open_document(MAIN_GD)
            before = retry_query(
                client,
                "textDocument/documentSymbol",
                {"textDocument": {"uri": uri}},
                lambda value: bool(flatten(value)),
            )
            self.assertIn("_ready", {s.get("name") for s in flatten(before)})
            entry = _entry_for(PROJECT)
            self.assertIsNotNone(entry)
            assert entry is not None
            original_pid = entry.pid

            holder = LockHolder(lock_path)
            self.assertTrue(
                _wait_until(lambda: not engine.pid_alive(original_pid), 90.0),
                "the engine did not yield while the lock was held",
            )
            self.assertTrue(
                _wait_until(lambda: _entry_for(PROJECT) is None, 30.0),
                "the yielded engine was not removed from the registry",
            )

            holder.release()
            holder = None
            self.assertTrue(
                _wait_until(
                    lambda: (_entry_for(PROJECT) is not None)
                    and _entry_for(PROJECT).pid != original_pid,  # type: ignore[union-attr]
                    INITIALIZE_TIMEOUT,
                ),
                "the engine did not come back after the lock was released",
            )
            replacement = _entry_for(PROJECT)
            assert replacement is not None
            self.assertTrue(engine.pid_alive(replacement.pid))

            # The session survived the swap: the same client, the document it
            # opened before the yield, answered by a different engine process.
            after = retry_query(
                client,
                "textDocument/documentSymbol",
                {"textDocument": {"uri": uri}},
                lambda value: bool(flatten(value)),
            )
            self.assertIn(
                "_ready",
                {s.get("name") for s in flatten(after)},
                "the reconnected session did not answer for a replayed document",
            )
        finally:
            if holder is not None:
                holder.release()
            client.close()


def _wait_until(predicate, timeout: float, poll: float = 0.25) -> bool:
    """Polls ``predicate`` until it is true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return bool(predicate())


def locate(path: str, line_marker: str, token: str) -> tuple[int, int]:
    """Returns the (line, character) of ``token`` on the line holding a marker.

    Positions are derived from the file, so editing the fixtures cannot leave
    a test pointing at the wrong column.
    """
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    for index, line in enumerate(lines):
        if line_marker in line:
            return index, line.index(token) + 2
    raise AssertionError(f"{line_marker!r} not found in {path}")


def _entry_for(root: str):
    """Returns the registry entry for ``root`` in the tests' registry, or None."""
    return test_registry().get(paths.project_key(root))


if __name__ == "__main__":
    unittest.main()
