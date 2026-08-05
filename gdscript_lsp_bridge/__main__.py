"""Allows ``python3 -m gdscript_lsp_bridge`` as an alternative to bridge.py."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
