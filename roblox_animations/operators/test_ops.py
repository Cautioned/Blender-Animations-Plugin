import io
import sys
import traceback
import importlib
import unittest
from pathlib import Path

import bpy


class OBJECT_OT_RunTests(bpy.types.Operator):
    bl_idname = "object.rbxanims_run_tests"
    bl_label = "Run Add-on Tests"
    bl_description = "Run the add-on's Python unit tests inside Blender"
    bl_options = {"REGISTER"}

    def execute(self, context):
        package_root = Path(__file__).resolve().parent.parent
        project_root = package_root.parent
        tests_dir = package_root / "tests"

        if not tests_dir.exists():
            self.report({"ERROR"}, "Tests directory not found")
            return {"CANCELLED"}

        # Ensure project root is in sys.path so discover works when Blender launches us directly
        project_root_str = str(project_root)
        if project_root_str not in sys.path:
            sys.path.insert(0, project_root_str)

        # Invalidate loader caches to pick up fresh files
        importlib.invalidate_caches()

        loader = unittest.defaultTestLoader

        try:
            # Only discover tests in the roblox_animations/tests directory
            suite = loader.discover(
                start_dir=str(tests_dir),
                pattern="test_*.py",
                top_level_dir=str(project_root),
            )
        except Exception:  # pragma: no cover - defensive: discovery failure path
            err = traceback.format_exc()
            context.window_manager.clipboard = err
            self.report({"ERROR"}, "Failed to discover tests; traceback copied to clipboard")
            return {"CANCELLED"}

        if suite.countTestCases() == 0:
            self.report({"WARNING"}, "No tests were discovered")
            return {"CANCELLED"}

        buffer = io.StringIO()
        runner = unittest.TextTestRunner(stream=buffer, verbosity=2)

        try:
            result = runner.run(suite)
        except Exception:  # pragma: no cover - defensive: runtime failure path
            err = traceback.format_exc()
            context.window_manager.clipboard = err
            self.report({"ERROR"}, "Tests crashed; traceback copied to clipboard")
            return {"CANCELLED"}

        output = buffer.getvalue()
        context.window_manager.clipboard = output
        
        # Print to console as well
        print("=" * 50)
        print("TEST RESULTS")
        print("=" * 50)
        print(output)
        print("=" * 50)

        summary = (
            f"ran {result.testsRun} tests, "
            f"failures: {len(result.failures)}, errors: {len(result.errors)}, skips: {len(result.skipped)}"
        )

        if result.wasSuccessful():
            self.report({"INFO"}, f"Tests passed ({summary}). Full log copied to clipboard and printed to console")
            return {"FINISHED"}

        self.report({"ERROR"}, f"Tests failed ({summary}). Full log copied to clipboard and printed to console")
        return {"CANCELLED"}
