"""Import the hyphen-named plugin directory as an importable test package, and register its hooks for tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = Path("/srv/mindroom/src")
ALIAS = "agent_vault_bridge_test_import"

for path in (PLUGIN_ROOT, SRC_ROOT):
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

# The dispatch tests reuse the shell-tool harness in the `tests` package of the MindRoom checkout they run against,
# which the compatibility workflow installs MindRoom from. It goes first, since some MindRoom dependencies install a
# top-level `tests` package of their own.
mindroom_spec = importlib.util.find_spec("mindroom")
if mindroom_spec is not None and mindroom_spec.origin is not None:
    sys.path.insert(0, str(Path(mindroom_spec.origin).resolve().parents[2]))

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

# These need the import paths and the package alias set above.
from mindroom.config.plugin import PluginEntryConfig  # noqa: E402
from mindroom.hooks import HookRegistry, iter_module_hooks  # noqa: E402

from agent_vault_bridge_test_import import hooks  # noqa: E402


def _plugin_registry(**settings: object) -> HookRegistry:
    """Return a hook registry holding this plugin's hooks with ``settings``, as MindRoom builds it for a loaded plugin."""
    plugin = SimpleNamespace(
        name=hooks.PLUGIN_NAME,
        discovered_hooks=tuple(iter_module_hooks(hooks)),
        discovered_automations=(),
        entry_config=PluginEntryConfig(path="./plugins/agent-vault-bridge", settings=settings),
        plugin_order=0,
    )
    return HookRegistry.from_plugins([plugin])


@pytest.fixture
def plugin_registry() -> HookRegistry:
    """Return a hook registry holding this plugin's hooks with their default settings."""
    return _plugin_registry()


@pytest.fixture
def make_plugin_registry() -> Callable[..., HookRegistry]:
    """Return a builder of hook registries holding this plugin's hooks with the settings it is given."""
    return _plugin_registry
