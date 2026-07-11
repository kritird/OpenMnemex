"""openmnemex — the OpenMnemex Context Graph engine, packaged (plan v2 §7, commit 0a).

This bridge makes the flat ``scripts/`` engine importable BOTH ways without touching the
``mnx_*`` modules themselves:

  * **repo / Claude-plugin path (unchanged):** ``python3 scripts/mnx_stage.py …`` and plain
    ``import mnx_stage`` with ``scripts/`` on ``sys.path`` — the modules import each other
    flat (``import mnx_common``), exactly as before. Running a script directly never
    executes this file.
  * **packaged path (pip / uvx):** ``pyproject.toml`` maps this directory to the
    ``openmnemex`` package. Importing ``openmnemex`` (or any ``openmnemex.mnx_*``) runs
    this bridge first, which puts the package directory on ``sys.path`` so the flat
    intra-engine imports keep resolving, then eagerly aliases every engine module as
    ``openmnemex.<name>``.

The eager aliasing guarantees a SINGLE module identity per engine module — ``import
mnx_common`` and ``from openmnemex import mnx_common`` return the very same object — so
module-level state can never fork between the two import styles (the dual-identity trap
of path-bridged packages). Guarded by tests/test_packaging_imports.py.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent

if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

for _mod_file in sorted(_PKG_DIR.glob("mnx_*.py")):
    _name = _mod_file.stem
    _module = importlib.import_module(_name)        # flat identity, same as the plugin path
    sys.modules[f"{__name__}.{_name}"] = _module    # `import openmnemex.mnx_x` → same object
    globals()[_name] = _module                      # `from openmnemex import mnx_x`
