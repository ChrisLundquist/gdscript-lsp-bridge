"""A stdio-to-TCP bridge fronting Godot's built-in GDScript language server.

Godot ships a full LSP implementation inside the editor binary; it listens on
TCP when the editor is started with ``--lsp-port``. Editors that speak LSP over
a child process's stdio -- Claude Code among them -- cannot talk to it directly.
This package is the pipe between those two transports, plus the process
lifecycle management that makes a warm engine reusable across sessions.

Nothing here parses GDScript. Godot does that.

The package depends only on the Python standard library, by design.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
