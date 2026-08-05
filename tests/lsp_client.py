"""A minimal LSP client for driving the bridge in tests.

Speaks the same stdio protocol Claude Code does, so an end-to-end test
exercises the real path rather than a stub of it.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any

from gdscript_lsp_bridge import framing, paths


class BridgeClient:
    """Drives a bridge subprocess over stdio.

    Responses are collected off a reader thread and matched by id, so a server
    notification arriving between a request and its reply cannot be mistaken
    for that reply.
    """

    def __init__(
        self,
        bridge_path: str,
        env: dict[str, str] | None = None,
        stderr_path: str = "",
    ) -> None:
        self._stderr_handle = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        self.process = subprocess.Popen(
            ["python3", bridge_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_handle,
            env=env or dict(os.environ),
        )
        assert self.process.stdout is not None
        self._reader = framing.MessageReader(self.process.stdout.read1)
        self._next_id = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition()
        self._responses: dict[Any, dict[str, Any]] = {}
        self.notifications: list[dict[str, Any]] = []
        self._eof = False
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            while True:
                body = self._reader.read_message()
                if body is None:
                    break
                try:
                    message = json.loads(body.decode("utf-8"))
                except ValueError:
                    continue
                with self._condition:
                    if "id" in message and ("result" in message or "error" in message):
                        self._responses[message["id"]] = message
                    else:
                        self.notifications.append(message)
                    self._condition.notify_all()
        except Exception:  # noqa: BLE001 - reader thread must not raise
            pass
        finally:
            with self._condition:
                self._eof = True
                self._condition.notify_all()

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        with self._lock:
            self.process.stdin.write(framing.encode(payload))
            self.process.stdin.flush()

    def notify(self, method: str, params: Any = None) -> None:
        """Sends a notification."""
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: Any = None, timeout: float = 120.0) -> dict[str, Any]:
        """Sends a request and waits for its reply.

        Raises on timeout rather than returning None, so a test failure names
        the method that hung instead of a downstream ``NoneType`` error.
        """
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        with self._condition:
            ready = self._condition.wait_for(
                lambda: request_id in self._responses or self._eof, timeout=timeout
            )
            if request_id in self._responses:
                return self._responses.pop(request_id)
            if not ready:
                raise TimeoutError(f"no reply to {method} within {timeout}s")
            raise RuntimeError(f"bridge closed the stream before replying to {method}")

    def initialize(self, root: str, timeout: float = 180.0) -> dict[str, Any]:
        """Performs the initialize/initialized handshake for ``root``."""
        uri = paths.path_to_uri(root)
        response = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "gdscript-lsp-bridge-tests"},
                "rootUri": uri,
                "rootPath": root,
                "capabilities": {"textDocument": {"documentSymbol": {}, "hover": {}}},
                "workspaceFolders": [{"uri": uri, "name": os.path.basename(root)}],
            },
            timeout=timeout,
        )
        self.notify("initialized", {})
        return response

    def open_document(self, path: str, language_id: str = "gdscript") -> str:
        """Sends ``didOpen`` for a real file and returns its URI."""
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        uri = paths.path_to_uri(path)
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )
        return uri

    def close(self, timeout: float = 15.0) -> int:
        """Shuts the bridge down politely and returns its exit code."""
        try:
            self.request("shutdown", timeout=10.0)
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
        try:
            self.notify("exit")
        except Exception:  # noqa: BLE001
            pass
        try:
            assert self.process.stdin is not None
            self.process.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return self.process.wait(timeout=timeout)
        finally:
            self._thread.join(timeout=5.0)
            for handle in (self.process.stdout, self.process.stdin):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:  # noqa: BLE001 - teardown is best effort
                        pass
            if self._stderr_handle is not subprocess.DEVNULL:
                self._stderr_handle.close()
