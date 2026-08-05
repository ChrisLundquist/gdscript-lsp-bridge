"""Discovering which Godot project this bridge is serving.

The client tells us, and it does so in the ``initialize`` request -- that is the
whole configuration story. ``rootUri``, ``workspaceFolders`` and the deprecated
``rootPath`` all carry a workspace directory, and one of them is always present
in practice. Requiring a configured project path instead would mean a per-repo
setup step for something the protocol already states.

A workspace root and a GODOT project root are not always the same directory,
though. Godot's language server needs the directory holding ``project.godot``;
the client's workspace may be a parent (a monorepo whose game lives in
``game/``) or a child (an editor opened on ``scripts/``). :func:`find_project_root`
therefore searches outward from what the client said: the directory itself,
then its ancestors, then a bounded scan downward.

The downward scan is bounded and ordered, because it is the one part of this
that could otherwise misbehave: unbounded, it would walk a monorepo; unordered,
it would pick a different project between runs on the same input.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from . import paths

#: The file whose presence defines a Godot project root.
PROJECT_MARKER = "project.godot"

#: How many directory levels below the workspace root to search. Two covers
#: the common ``repo/game/project.godot`` and ``repo/src/game/project.godot``
#: layouts without turning into a filesystem crawl.
MAX_DESCENT_DEPTH = 2

#: Never descend into these. Build output and dependency trees can contain
#: vendored Godot projects that are not what the user meant.
SKIP_DIRECTORY_NAMES = frozenset(
    {
        ".git", ".hg", ".svn", ".godot", ".import", "node_modules", ".venv",
        "venv", "__pycache__", "build", "dist", "target", ".cache", "addons",
        "android", "ios", "export", "exports", ".claude",
    }
)


def root_from_initialize(params: dict[str, Any]) -> str:
    """Extracts the workspace directory from ``initialize`` params.

    Preference order follows the specification's own: ``workspaceFolders``
    supersedes ``rootUri``, which supersedes the deprecated ``rootPath``. The
    first folder wins when several are open, since a language server here can
    serve exactly one Godot project.
    """
    folders = params.get("workspaceFolders")
    if isinstance(folders, list):
        for folder in folders:
            if isinstance(folder, dict):
                uri = folder.get("uri")
                if isinstance(uri, str) and uri:
                    return paths.uri_to_path(uri)
    root_uri = params.get("rootUri")
    if isinstance(root_uri, str) and root_uri:
        return paths.uri_to_path(root_uri)
    root_path = params.get("rootPath")
    if isinstance(root_path, str) and root_path:
        return root_path
    return ""


def is_project_root(directory: str) -> bool:
    """True when ``directory`` holds a ``project.godot``."""
    return os.path.isfile(os.path.join(directory, PROJECT_MARKER))


def ancestors(start: str) -> Iterable[str]:
    """Yields ``start`` and each of its ancestors up to the filesystem root."""
    current = os.path.abspath(start)
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        yield current
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def descendants(start: str, max_depth: int = MAX_DESCENT_DEPTH) -> Iterable[str]:
    """Yields directories below ``start``, breadth-first and name-ordered.

    Ordering matters: an unordered walk would resolve a workspace containing
    two Godot projects differently on different runs, which reads as the tool
    being flaky rather than the workspace being ambiguous.
    """
    frontier = [(start, 0)]
    while frontier:
        directory, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if name in SKIP_DIRECTORY_NAMES or name.startswith("."):
                continue
            child = os.path.join(directory, name)
            if os.path.isdir(child) and not os.path.islink(child):
                yield child
                frontier.append((child, depth + 1))


def find_project_root(start: str) -> str:
    """Returns the Godot project root for a workspace directory, or "".

    Search order is outward-then-inward: the directory itself, its ancestors
    (nearest first), then a bounded descent. Ancestors beat descendants because
    a workspace opened on ``game/scripts/`` means the project at ``game/``, and
    ``project.godot`` is unique per project so the nearest one up is unambiguous.
    """
    if not start:
        return ""
    if not os.path.isdir(start):
        start = os.path.dirname(start)
        if not os.path.isdir(start):
            return ""
    for directory in ancestors(start):
        if is_project_root(directory):
            return paths.physical_root(directory)
    for directory in descendants(start):
        if is_project_root(directory):
            return paths.physical_root(directory)
    return ""


def rewrite_initialize_params(
    params: dict[str, Any], project_root: str
) -> dict[str, Any]:
    """Returns ``params`` with the workspace repointed at ``project_root``.

    Only called when the client's workspace and the Godot project differ.
    Godot resolves ``res://`` against the root it is told about, so leaving a
    monorepo's top directory in place would have every URI it returns resolve
    against the wrong base.
    """
    updated = dict(params)
    uri = paths.path_to_uri(project_root)
    updated["rootUri"] = uri
    updated["rootPath"] = project_root
    updated["workspaceFolders"] = [
        {"uri": uri, "name": os.path.basename(project_root) or project_root}
    ]
    return updated
