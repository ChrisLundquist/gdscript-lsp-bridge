"""The bridge session: client stdio on one side, Godot's TCP LSP on the other.

SHAPE. One thread pumps each direction. The client-to-server direction runs on
the main thread and is the only one that inspects traffic; the server-to-client
direction is a pure relay on a worker thread. Messages this bridge does not
deliberately rewrite are forwarded as the exact bytes that arrived.

THE THREE THINGS IT DOES NOT JUST FORWARD, and why each is worth the exception:

* ``initialize`` is held back. It is the only message that names the workspace,
  so it must be read before there is anywhere to forward it TO -- discovering
  the project root is what decides which engine to reuse or spawn. If the Godot
  project is not the workspace directory itself, the params are repointed at it
  so ``res://`` resolves against the right base.
* ``shutdown`` and ``exit`` are answered locally and never forwarded. The
  engine is deliberately shared and outlives this session; relaying a client's
  shutdown to it would tear down a warm server other sessions are using.
* ``didOpen`` / ``didChange`` / ``didClose`` are mirrored into a document
  table. Nothing reads that table during normal operation -- it exists so the
  optional lock-yield policy can rebuild the server's view of open documents
  after it restarts the engine underneath a live session.

WHAT A RECONNECT COSTS. The client is never told the engine restarted, because
the protocol has no way to say it and the honest alternative -- failing the
session -- is worse. Requests in flight across the swap are lost; the client
retries or times out. This is the accepted trade of the yield policy and the
reason it is opt-in.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time
from typing import Any

from . import engine as engine_module
from . import framing, paths, project, yielding
from .registry import Registry

#: JSON-RPC error codes used for the few failures the bridge answers itself.
ERROR_INTERNAL = -32603
ERROR_SERVER_NOT_INITIALIZED = -32002

#: Request id used for the bridge's own re-handshake after a yield. A string id
#: is legal JSON-RPC and cannot collide with a client's integer ids.
REINIT_ID_PREFIX = "gdscript-lsp-bridge/reinit-"

#: How long the server-to-client relay waits for a swap to complete before
#: concluding the engine is simply gone.
SWAP_WAIT_SECONDS = 1800.0

#: How long it waits after a plain EOF, in case a swap is about to begin.
EOF_GRACE_SECONDS = 0.5


class StdioChannel:
    """The client side: framed messages over this process's stdin/stdout.

    Reads go through :func:`os.read` rather than the buffered reader's
    ``read``, which would block for a full buffer instead of returning what has
    arrived. Writes are serialized, because the relay thread and the main
    thread can both answer the client.
    """

    def __init__(self) -> None:
        self._stdin_fd = sys.stdin.fileno()
        self._stdout = sys.stdout.buffer
        self._write_lock = threading.Lock()
        self.reader = framing.MessageReader(self._read)

    def _read(self, count: int) -> bytes:
        try:
            return os.read(self._stdin_fd, count)
        except OSError:
            return b""

    def read_message(self) -> bytes | None:
        """Returns the next message body from the client, or None at EOF."""
        return self.reader.read_message()

    def write_body(self, body: bytes) -> None:
        """Writes an already-serialized message body to the client."""
        with self._write_lock:
            try:
                self._stdout.write(framing.encode_body(body))
                self._stdout.flush()
            except (OSError, ValueError):
                # The client is gone; the pumps notice through EOF.
                pass

    def write(self, payload: dict[str, Any]) -> None:
        """Serializes and writes a JSON-RPC payload to the client."""
        with self._write_lock:
            try:
                self._stdout.write(framing.encode(payload))
                self._stdout.flush()
            except (OSError, ValueError):
                pass


class ServerLink:
    """The engine side: the current TCP connection, swappable underneath.

    A swap is a three-step handshake -- ``begin_swap`` stops senders and breaks
    the current socket so the relay thread unblocks, ``complete_swap`` installs
    the replacement and wakes everyone. Senders block for the duration rather
    than failing, so a request that arrives mid-yield is merely slow.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock: socket.socket | None = sock
        self._condition = threading.Condition()
        self._generation = 0
        self._swapping = False
        self._closed = False

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has run."""
        with self._condition:
            return self._closed

    @property
    def swapping(self) -> bool:
        """True between ``begin_swap`` and ``complete_swap``/``abort_swap``."""
        with self._condition:
            return self._swapping

    def snapshot(self) -> tuple[int, socket.socket | None]:
        """Returns the current (generation, socket) pair."""
        with self._condition:
            return self._generation, self._sock

    def send(self, data: bytes, timeout: float = SWAP_WAIT_SECONDS) -> bool:
        """Sends raw bytes to the engine, waiting out any swap in progress."""
        with self._condition:
            if self._swapping:
                self._condition.wait_for(
                    lambda: self._closed or not self._swapping, timeout=timeout
                )
            if self._closed or self._sock is None:
                return False
            sock = self._sock
        try:
            sock.sendall(data)
            return True
        except OSError:
            return False

    def begin_swap(self) -> None:
        """Marks a swap in progress and breaks the current connection."""
        with self._condition:
            if self._closed or self._swapping:
                return
            self._swapping = True
            sock = self._sock
            self._sock = None
        _shutdown(sock)

    def complete_swap(self, sock: socket.socket) -> None:
        """Installs the replacement connection and releases waiters."""
        with self._condition:
            self._sock = sock
            self._generation += 1
            self._swapping = False
            self._condition.notify_all()

    def abort_swap(self) -> None:
        """Ends a failed swap without a replacement; the session is finished."""
        with self._condition:
            self._swapping = False
            self._closed = True
            self._condition.notify_all()

    def await_generation(self, generation: int, timeout: float) -> tuple[int, socket.socket | None]:
        """Waits for a generation newer than ``generation``.

        Returns the new pair, or ``(generation, None)`` if none arrives -- which
        the relay reads as "the engine is gone", not "try again".
        """
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed or self._generation > generation, timeout=timeout
            )
            if self._closed or self._generation <= generation:
                return generation, None
            return self._generation, self._sock

    def close(self) -> None:
        """Closes the connection and releases every waiter."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            sock = self._sock
            self._sock = None
            self._condition.notify_all()
        _shutdown(sock)


def _shutdown(sock: socket.socket | None) -> None:
    """Half-closes then closes a socket, ignoring the usual teardown races."""
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


class Logger:
    """Minimal stderr logger.

    Deliberately not :mod:`logging`: stdout is the protocol channel, and a
    stray ``basicConfig`` anywhere in the process would be free to write there
    and corrupt the stream. Writing only to a handle this class owns removes
    that possibility.
    """

    LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "quiet": 100}

    def __init__(self, level: str = "info", stream: Any = None) -> None:
        self.threshold = self.LEVELS.get(level.lower(), 20)
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()

    def _emit(self, level: str, message: str) -> None:
        if self.LEVELS[level] < self.threshold:
            return
        with self._lock:
            with contextlib.suppress(Exception):
                self._stream.write(f"[gdscript-lsp-bridge] {level}: {message}\n")
                self._stream.flush()

    def debug(self, message: str) -> None:
        """Logs a debug-level message."""
        self._emit("debug", message)

    def info(self, message: str) -> None:
        """Logs an info-level message."""
        self._emit("info", message)

    def warning(self, message: str) -> None:
        """Logs a warning-level message."""
        self._emit("warning", message)

    def error(self, message: str) -> None:
        """Logs an error-level message."""
        self._emit("error", message)


class BridgeSession:
    """One Claude-Code-to-Godot LSP session."""

    def __init__(
        self,
        channel: StdioChannel | None = None,
        registry: Registry | None = None,
        godot: str = "",
        logger: Logger | None = None,
        persist: bool | None = None,
    ) -> None:
        self.channel = channel or StdioChannel()
        self.registry = registry or Registry()
        self.godot = godot
        self.log = logger or Logger(os.environ.get("GDSCRIPT_LSP_LOG_LEVEL", "info"))
        self.persist = _persist_default() if persist is None else persist

        self.handle: engine_module.EngineHandle | None = None
        self.link: ServerLink | None = None
        self.project_root = ""
        self._spawned_engine = False
        self._initialize_params: dict[str, Any] = {}
        self._open_documents: dict[str, dict[str, Any]] = {}
        self._documents_lock = threading.Lock()
        self._reinit_counter = 0
        self._stopping = threading.Event()
        self._relay: threading.Thread | None = None
        self._watcher: yielding.LockYieldWatcher | None = None
        self._yield_lock = threading.Lock()

    # ---------------------------------------------------------------- startup

    def run(self) -> int:
        """Runs the session to completion. Returns a process exit code."""
        try:
            if not self._start():
                return 1
            self._pump_client_to_server()
            return 0
        except framing.FramingError as error:
            self.log.error(f"client framing error: {error}")
            return 1
        except KeyboardInterrupt:
            return 0
        finally:
            self._teardown()

    def _start(self) -> bool:
        """Handles the initialize handshake and brings an engine up."""
        body = self._read_until_initialize()
        if body is None:
            self.log.info("client closed the stream before initialize")
            return False
        message = _parse(body) or {}
        request_id = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        workspace = project.root_from_initialize(params)
        if not workspace:
            self._fail_initialize(
                request_id,
                "initialize carried no rootUri, workspaceFolders or rootPath, so "
                "there is no way to tell which Godot project to serve",
            )
            return False

        root = project.find_project_root(workspace)
        if not root:
            self._fail_initialize(
                request_id,
                f"no {project.PROJECT_MARKER} found at, above, or just below "
                f"{workspace!r}; this workspace is not a Godot project",
            )
            return False
        self.project_root = root
        self.log.info(f"workspace {workspace!r} -> Godot project {root!r}")

        idle = _idle_timeout()
        if idle > 0:
            for key in engine_module.reap_idle(idle, self.registry):
                self.log.info(f"reaped idle engine {key}")

        try:
            self.handle = engine_module.ensure(
                root, registry=self.registry, godot=self.godot, logger=self.log
            )
        except engine_module.EngineError as error:
            self._fail_initialize(request_id, str(error))
            return False
        self._spawned_engine = not self.handle.reused

        outgoing = body
        if paths.physical_root(workspace) != root:
            rewritten = dict(message)
            rewritten["params"] = project.rewrite_initialize_params(params, root)
            outgoing = json.dumps(rewritten, ensure_ascii=False).encode("utf-8")
            self.log.info("repointed initialize at the Godot project root")
            self._initialize_params = rewritten["params"]
        else:
            self._initialize_params = params

        try:
            sock = engine_module.connect(self.handle.port)
        except OSError as error:
            self._fail_initialize(
                request_id,
                f"engine on port {self.handle.port} refused a connection: {error}",
            )
            return False
        self.link = ServerLink(sock)

        if not self.link.send(framing.encode_body(outgoing)):
            self._fail_initialize(request_id, "engine closed during initialize")
            return False

        self._relay = threading.Thread(
            target=self._pump_server_to_client, name="gdscript-lsp-relay", daemon=True
        )
        self._relay.start()
        self._start_yield_watcher()
        return True

    def _read_until_initialize(self) -> bytes | None:
        """Returns the ``initialize`` request body, answering anything earlier.

        The specification puts ``initialize`` first, but a client that sends a
        stray request before it gets the prescribed error rather than a hang.
        """
        while True:
            body = self.channel.read_message()
            if body is None:
                return None
            message = _parse(body)
            if message is None:
                continue
            if message.get("method") == "initialize":
                return body
            if message.get("id") is not None:
                self.channel.write(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {
                            "code": ERROR_SERVER_NOT_INITIALIZED,
                            "message": "expected initialize as the first request",
                        },
                    }
                )

    def _fail_initialize(self, request_id: Any, reason: str) -> None:
        """Answers a failed initialize with an error the user can act on."""
        self.log.error(reason)
        if request_id is not None:
            self.channel.write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": ERROR_INTERNAL, "message": reason},
                }
            )

    # ------------------------------------------------------------------ pumps

    def _pump_client_to_server(self) -> None:
        """Relays client traffic to the engine until the client goes away."""
        assert self.link is not None
        while not self._stopping.is_set():
            try:
                body = self.channel.read_message()
            except framing.FramingError as error:
                self.log.error(f"client framing error: {error}")
                return
            if body is None:
                self.log.info("client stream closed")
                return
            message = _parse(body)
            if message is not None:
                method = message.get("method")
                if method == "shutdown":
                    # Answered here, never forwarded: the engine is shared and
                    # must survive this session.
                    self.channel.write(
                        {"jsonrpc": "2.0", "id": message.get("id"), "result": None}
                    )
                    continue
                if method == "exit":
                    self.log.info("client sent exit")
                    return
                self._track_document(method, message.get("params"))
            if not self.link.send(framing.encode_body(body)):
                self.log.error("engine connection lost while sending")
                return

    def _pump_server_to_client(self) -> None:
        """Relays engine traffic to the client, surviving a deliberate swap."""
        assert self.link is not None
        generation, sock = self.link.snapshot()
        while sock is not None and not self._stopping.is_set():
            reader = framing.MessageReader(sock.recv)
            try:
                while True:
                    body = reader.read_message()
                    if body is None:
                        break
                    self.channel.write_body(body)
            except (OSError, framing.FramingError) as error:
                self.log.debug(f"engine stream ended: {error}")
            if self._stopping.is_set() or self.link.closed:
                return
            timeout = SWAP_WAIT_SECONDS if self.link.swapping else EOF_GRACE_SECONDS
            generation, sock = self.link.await_generation(generation, timeout)
            if sock is None:
                self.log.error("engine connection ended and was not replaced")
                self._stopping.set()
                _wake_stdin_reader()
                return

    # -------------------------------------------------------------- documents

    def _track_document(self, method: Any, params: Any) -> None:
        """Mirrors document lifecycle notifications into the replay table."""
        if not isinstance(params, dict):
            return
        if method == "textDocument/didOpen":
            document = params.get("textDocument")
            if isinstance(document, dict) and isinstance(document.get("uri"), str):
                with self._documents_lock:
                    self._open_documents[document["uri"]] = dict(document)
        elif method == "textDocument/didChange":
            document = params.get("textDocument")
            changes = params.get("contentChanges")
            if not isinstance(document, dict) or not isinstance(changes, list):
                return
            uri = document.get("uri")
            if not isinstance(uri, str):
                return
            # Godot advertises Full text sync, so a change without a range
            # carries the whole document and can replace the stored copy. A
            # ranged change would need the server's own state to apply, which
            # this bridge does not model; the stale copy it leaves behind is
            # corrected by the next full change or reopen.
            for change in changes:
                if isinstance(change, dict) and "range" not in change:
                    text = change.get("text")
                    if isinstance(text, str):
                        with self._documents_lock:
                            stored = self._open_documents.setdefault(uri, {"uri": uri})
                            stored["text"] = text
                            version = document.get("version")
                            if version is not None:
                                stored["version"] = version
        elif method == "textDocument/didClose":
            document = params.get("textDocument")
            if isinstance(document, dict):
                uri = document.get("uri")
                if isinstance(uri, str):
                    with self._documents_lock:
                        self._open_documents.pop(uri, None)

    def _open_document_snapshot(self) -> list[dict[str, Any]]:
        """Returns a copy of the tracked open documents, for replay."""
        with self._documents_lock:
            return [dict(document) for document in self._open_documents.values()]

    # ------------------------------------------------------------ yield policy

    def _start_yield_watcher(self) -> None:
        """Starts the lock-yield watcher if, and only if, it is configured."""
        pattern = yielding.configured_glob()
        if not pattern:
            return
        self.log.info(f"lock-yield policy armed on {pattern!r}")
        self._watcher = yielding.LockYieldWatcher(
            pattern, on_yield=self._yield_engine, logger=self.log
        )
        self._watcher.start()

    def _yield_engine(self) -> None:
        """Stops the engine, waits for the lock, relaunches and reconnects."""
        if not self._yield_lock.acquire(blocking=False):
            return
        try:
            if self._stopping.is_set() or self.link is None or self.handle is None:
                return
            watcher = self._watcher
            if watcher is None:
                return
            started = time.monotonic()
            self.link.begin_swap()
            engine_module.stop_root(self.project_root, self.registry)
            self.log.info("engine stopped; waiting for the lock to clear")
            released = watcher.wait_for_release()
            if not released:
                self.log.warning(
                    "lock still held after the maximum wait; relaunching anyway"
                )
            if self._stopping.is_set():
                self.link.abort_swap()
                return
            try:
                self.handle = engine_module.ensure(
                    self.project_root,
                    registry=self.registry,
                    godot=self.godot,
                    logger=self.log,
                )
                sock = engine_module.connect(self.handle.port)
                self._rehandshake(sock)
            except (engine_module.EngineError, OSError, framing.FramingError) as error:
                self.log.error(f"could not restore the engine after yielding: {error}")
                self.link.abort_swap()
                self._stopping.set()
                _wake_stdin_reader()
                return
            self._spawned_engine = True
            self.link.complete_swap(sock)
            self.log.info(
                f"engine restored after {time.monotonic() - started:.1f}s "
                f"(pid={self.handle.pid} port={self.handle.port})"
            )
        finally:
            self._yield_lock.release()

    def _rehandshake(self, sock: socket.socket) -> None:
        """Re-initializes a fresh engine connection and replays open documents.

        The replies to these messages belong to the bridge, not the client, so
        they are consumed here -- forwarding a response to an id the client
        never sent would be a protocol violation on a client that checks.
        """
        self._reinit_counter += 1
        reinit_id = f"{REINIT_ID_PREFIX}{self._reinit_counter}"
        sock.sendall(
            framing.encode(
                {
                    "jsonrpc": "2.0",
                    "id": reinit_id,
                    "method": "initialize",
                    "params": self._initialize_params,
                }
            )
        )
        reader = framing.MessageReader(sock.recv)
        sock.settimeout(60.0)
        try:
            while True:
                body = reader.read_message()
                if body is None:
                    raise framing.FramingError("engine closed during re-initialize")
                message = _parse(body)
                if message is not None and message.get("id") == reinit_id:
                    break
        finally:
            sock.settimeout(None)
        sock.sendall(
            framing.encode({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        )
        for document in self._open_document_snapshot():
            if "text" not in document:
                continue
            sock.sendall(
                framing.encode(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didOpen",
                        "params": {"textDocument": document},
                    }
                )
            )

    # --------------------------------------------------------------- teardown

    def _teardown(self) -> None:
        """Releases the session's own resources; the engine's fate is policy."""
        self._stopping.set()
        if self._watcher is not None:
            self._watcher.stop()
        if self.link is not None:
            self.link.close()
        if self._relay is not None:
            self._relay.join(timeout=5.0)
        if self.handle is None:
            return
        if self.persist:
            with contextlib.suppress(Exception):
                self.registry.touch(self.handle.key)
            self.log.info(
                f"leaving engine pid={self.handle.pid} warm on port {self.handle.port}"
            )
            return
        if not self._spawned_engine:
            # A reused engine belongs to whoever spawned it; stopping it here
            # would pull it out from under another live session.
            self.log.info("not stopping an engine this session did not spawn")
            return
        self.log.info(f"stopping engine pid={self.handle.pid} (persist disabled)")
        engine_module.stop_root(self.project_root, self.registry)


def _parse(body: bytes) -> dict[str, Any] | None:
    """Parses a message body, returning None when it is not a JSON object."""
    try:
        message = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return message if isinstance(message, dict) else None


def _persist_default() -> bool:
    """True unless ``GDSCRIPT_LSP_PERSIST`` is set to a falsey spelling."""
    value = os.environ.get("GDSCRIPT_LSP_PERSIST", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _idle_timeout() -> float:
    """Returns the configured idle reap timeout in seconds (0 disables)."""
    try:
        return float(os.environ.get("GDSCRIPT_LSP_IDLE_TIMEOUT", "0"))
    except ValueError:
        return 0.0


def _wake_stdin_reader() -> None:
    """Ends the process when the engine has died and cannot be replaced.

    The main thread is parked in ``os.read`` on stdin, and there is no portable
    guarantee that closing the descriptor from another thread makes that read
    return -- on some platforms a reader already blocked in the kernel stays
    blocked. Closing it is still tried first, because when it does work the
    session unwinds normally and runs its teardown.

    The timer is the part that must not be omitted. Without it, a bridge whose
    engine died would sit forever holding a client that thinks it has a working
    language server. Exiting is the honest outcome: the client sees the server
    terminate and can restart it, which is exactly what should happen. Teardown
    is skipped, which costs nothing here -- the engine this session would have
    tidied up is the one that just died.
    """
    with contextlib.suppress(Exception):
        os.close(sys.stdin.fileno())
    watchdog = threading.Timer(5.0, lambda: os._exit(1))
    watchdog.daemon = True
    watchdog.start()
