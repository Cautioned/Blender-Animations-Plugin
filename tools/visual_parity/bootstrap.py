"""Load individual addon modules without running UI/operator registration.

This keeps headless shader/geometry checks independent of unrelated operators
and does not register the addon or change its authentication behavior.
"""
import sys
import types
from pathlib import Path


def load_addon_modules():
    root = Path(__file__).resolve().parents[2] / 'roblox_animations'
    for suffix in ('', '.core', '.rig', '.operators', '.ui', '.server', '.animation', '.tests'):
        name = 'roblox_animations' + suffix
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        module.__path__ = [str(root / suffix.lstrip('.')) if suffix else str(root)]
        sys.modules[name] = module
        if suffix:
            setattr(sys.modules['roblox_animations'], suffix[1:], module)
