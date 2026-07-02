"""Import the hyphen-named plugin directory as an importable test package."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = Path("/srv/mindroom/src")
ALIAS = "agent_vault_bridge_test_import"

for path in (PLUGIN_ROOT, SRC_ROOT):
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

if ALIAS not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        ALIAS,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        msg = f"could not build import spec for {PLUGIN_ROOT}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[ALIAS] = module
    spec.loader.exec_module(module)
