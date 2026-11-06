"""blender -b --factory-startup --python-exit-code 1 --python tools/visual_parity/run_tests.py"""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootstrap import load_addon_modules
load_addon_modules()
modules = ['test_content_serialization', 'test_scene_fidelity', 'test_materials', 'test_color_linear', 'test_material_roundtrip','test_neon','test_local_lights','test_post_processing','test_sphere_materials','test_ambient_occlusion']
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName('roblox_animations.tests.' + name) for name in modules)
if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
    raise SystemExit(1)
