"""Run with: blender -b --factory-startup --python-exit-code 1 --python
roblox_animations/tests/run_draco_compat.py -- report.json

Exercises the installed Blender decoder, cold RBXL worker loads, geometry
creation, platform discovery fallbacks, and FileMesh parsing regressions.
"""
import json
from pathlib import Path
import platform
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import bpy  # noqa: E402
import roblox_animations  # noqa: E402
from roblox_animations.rig import filemesh  # noqa: E402


def main():
    # HTTP tests mock the transport, but still exercise its access guard.
    # Set the process preference only; never save user preferences.
    if hasattr(bpy.context.preferences.system, "use_online_access"):
        bpy.context.preferences.system.use_online_access = True
    roblox_animations.register()
    modules = ("test_draco_native", "test_draco_loader", "test_filemesh")
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName("roblox_animations.tests." + name)
        for name in modules
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    report = {
        "blender": bpy.app.version_string,
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "source": str(Path(filemesh.__file__).resolve()),
        "library": str(getattr(filemesh._DRACO_DLL, "_name", "")),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": result.wasSuccessful(),
    }
    print("DRACO_COMPAT_RESULT " + json.dumps(report), flush=True)
    if "--" in sys.argv:
        Path(sys.argv[sys.argv.index("--") + 1]).write_text(json.dumps(report, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
