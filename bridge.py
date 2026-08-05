#!/usr/bin/env python3
"""Executable entry point for the GDScript LSP bridge.

This file is what a client's configuration points at. It stays a shim so that
path can be a stable one-liner while the implementation moves around beneath
it. The package directory is put on ``sys.path`` explicitly so the bridge runs
from an absolute path with no installation step, which is the entire deployment
story: clone it, point ``.lsp.json`` at this file.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gdscript_lsp_bridge.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
