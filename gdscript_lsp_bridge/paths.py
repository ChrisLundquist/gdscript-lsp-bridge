"""Project-root identity: the key everything else in the bridge is filed under.

WHY THIS EXACT SCHEME. The resource two Godot engine runs contend over is a
project root's ``.godot`` directory -- one regenerable filesystem cache, one
import-readiness marker. Two runs against the SAME root must share (or
serialize); two runs against DIFFERENT roots share none of that and must never
be confused for each other. So the identity has to satisfy two properties at
once:

* Two *spellings* of one root -- a symlinked ancestor, a trailing separator, a
  differing case -- must collapse to ONE key, or a second engine gets spawned
  for a project that already has a warm one.
* Two genuinely different roots must never collide, or one project's symbols
  get served for another's.

The normalization below is inherited deliberately from the per-worktree engine
lock in the author's game repository (``tools/srtools/locks.py``), which solves
exactly this problem for exactly this reason: physical path, no trailing
separator, case folded, sha256, first 32 lowercase hex characters. Sharing the
derivation means a project keyed by one tool is keyed identically by the other.

ONE DELIBERATE DIFFERENCE, AND THE GUARD THAT PAYS FOR IT. That lock measures
whether the volume actually folds case before folding. This bridge folds
unconditionally, which is simpler and correct on the case-insensitive volumes
it targets, but on a case-sensitive volume it would map ``/src/Foo`` and
``/src/foo`` -- two real, distinct directories -- onto one key. Rather than
re-derive the volume probe, :mod:`gdscript_lsp_bridge.registry` stores the
physical root beside every entry and refuses a lookup whose stored root is not
the one asked for. A collision therefore degrades to "spawn a second engine",
never to "serve the wrong project's symbols".
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from urllib.parse import quote, unquote, urlparse

#: How many hex characters of the sha256 digest name one project. Matches the
#: engine lock's ``IDENTITY_PREFIX_LENGTH`` so the two agree on a name.
IDENTITY_PREFIX_LENGTH = 32

#: Directory under the OS temp root holding the registry and engine logs.
STATE_DIR_NAME = "gdscript-lsp-bridge"


def physical_root(project_root: str) -> str:
    """Returns ``project_root`` as an absolute path with symlinks resolved.

    Trailing separators are stripped so ``/a/b`` and ``/a/b/`` normalize alike.
    The strip is guarded: a filesystem root is all separator, and stripping it
    to the empty string would key every root identically.
    """
    resolved = os.path.realpath(os.path.abspath(project_root))
    trimmed = resolved.rstrip(os.sep)
    if os.altsep:
        trimmed = trimmed.rstrip(os.altsep)
    return trimmed or resolved


def project_key(project_root: str) -> str:
    """Returns the 32-character hex identity of one project root.

    See the module docstring for why this derivation and not another.
    """
    folded = physical_root(project_root).casefold()
    digest = hashlib.sha256(folded.encode("utf-8")).hexdigest()
    return digest[:IDENTITY_PREFIX_LENGTH]


def state_dir() -> str:
    """Returns the bridge's state directory, creating it if necessary."""
    path = os.path.join(tempfile.gettempdir(), STATE_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def engine_log_path(key: str) -> str:
    """Returns the path the engine's stdout/stderr is captured to."""
    return os.path.join(state_dir(), f"godot-{key}.log")


def uri_to_path(uri: str) -> str:
    """Converts a ``file:`` URI to a local filesystem path.

    Non-``file:`` input and bare paths are returned unchanged, so a client that
    sends ``rootPath`` instead of ``rootUri`` needs no special case upstream.
    """
    if not uri:
        return ""
    if not uri.startswith("file:"):
        return uri
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    # A Windows URI is file:///C:/x; drop the separator the drive letter does
    # not want. Harmless on POSIX, where no path matches this shape.
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def path_to_uri(path: str) -> str:
    """Converts a local filesystem path to a ``file:`` URI."""
    absolute = os.path.abspath(path)
    if os.sep != "/":
        absolute = absolute.replace(os.sep, "/")
    if not absolute.startswith("/"):
        absolute = "/" + absolute
    return "file://" + quote(absolute)
