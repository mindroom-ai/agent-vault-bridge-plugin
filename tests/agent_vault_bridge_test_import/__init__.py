"""Test import package for the plugin root.

The checked-out repository directory contains hyphens, so tests import the
plugin through this package name to preserve relative imports inside modules.
"""

from __future__ import annotations

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
__path__ = [str(PLUGIN_ROOT)]
