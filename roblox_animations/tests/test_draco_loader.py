"""Decoder discovery must not depend on a particular glTF/Python layout."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ..rig import filemesh


class TestDracoLoader(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bpy = SimpleNamespace(
            app=SimpleNamespace(binary_path=str(self.root / "blender.exe"), version=(4, 1, 1)),
            utils=SimpleNamespace(resource_path=lambda kind: "", script_paths=lambda: []),
        )
        for patch in (
            mock.patch.dict(filemesh.sys.modules, {"bpy": self.bpy}),
            mock.patch.object(filemesh.sys, "executable", str(self.root / "unrelated" / "python.exe")),
            mock.patch.object(filemesh, "_DRACO_DLL", filemesh._DRACO_DLL_UNINITIALIZED),
            mock.patch.object(filemesh, "_DRACO_LOAD_ERROR", None),
            mock.patch.dict(os.environ, {"BLENDER_EXTERN_DRACO_LIBRARY_PATH": ""}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_legacy_gltf_helper_is_queried(self):
        expected = self.root / "custom" / filemesh._draco_lib_name()
        def importer(name):
            if name.endswith("gltf2_io_draco_compression_extension"):
                return SimpleNamespace(dll_path=lambda: expected)
            raise ImportError(name)
        with mock.patch.object(filemesh.importlib, "import_module", side_effect=importer):
            self.assertIn(expected.resolve(), filemesh._get_blender_draco_dll_paths())

    def test_legacy_binary_layout_without_importable_gltf(self):
        expected = self.root / "4.1/python/lib/site-packages" / filemesh._draco_lib_name()
        with mock.patch.object(filemesh.importlib, "import_module", side_effect=ImportError):
            self.assertIn(expected.resolve(), filemesh._get_blender_draco_dll_paths())

    def test_legacy_versioned_python_resource_layout(self):
        self.bpy.utils.resource_path = lambda kind: str(self.root / "resources") if kind == "SYSTEM" else ""
        python = f"python{filemesh.sys.version_info.major}.{filemesh.sys.version_info.minor}"
        expected = self.root / "resources/python/lib" / python / "site-packages" / filemesh._draco_lib_name()
        with mock.patch.object(filemesh.importlib, "import_module", side_effect=ImportError):
            self.assertIn(expected.resolve(), filemesh._get_blender_draco_dll_paths())

    def test_split_system_libraries_without_importable_gltf(self):
        self.bpy.utils.resource_path = lambda kind: str(self.root / "split") if kind == "SYSTEM_LIBS" else ""
        expected = self.root / "split/scripts/addons_core/io_scene_gltf2" / filemesh._draco_bridge_lib_name()
        with mock.patch.object(filemesh.importlib, "import_module", side_effect=ImportError):
            self.assertIn(expected.resolve(), filemesh._get_blender_draco_dll_paths())

    def test_unknown_resource_enum_does_not_skip_script_paths(self):
        self.bpy.utils.resource_path = mock.Mock(side_effect=ValueError("Unsupported resource type"))
        self.bpy.utils.script_paths = lambda: [str(self.root / "scripts")]
        expected = self.root / "scripts/addons_core/io_scene_gltf2" / filemesh._draco_bridge_lib_name()
        with mock.patch.object(filemesh.importlib, "import_module", side_effect=ImportError):
            self.assertIn(expected.resolve(), filemesh._get_blender_draco_dll_paths())

    def test_portable_linux_bridge_beside_shared_libraries(self):
        self.bpy.utils.resource_path = lambda kind: str(self.root / "5.2") if kind == "LOCAL" else ""
        with mock.patch.object(filemesh.sys, "platform", "linux"):
            with mock.patch.object(filemesh.importlib, "import_module", side_effect=ImportError):
                expected = self.root / "lib/libbf_intern_draco_bridge.so"
                self.assertIn(expected.resolve(), filemesh._get_blender_draco_dll_paths())

    def test_mac_bundle_legacy_python_layout(self):
        self.bpy.app.binary_path = str(self.root / "Blender.app/Contents/MacOS/Blender")
        python = f"python{filemesh.sys.version_info.major}.{filemesh.sys.version_info.minor}"
        with mock.patch.object(filemesh.sys, "platform", "darwin"):
            with mock.patch.object(filemesh.importlib, "import_module", side_effect=ImportError):
                expected = self.root / "Blender.app/Contents/Resources/4.1/python/lib" / python / "site-packages/libextern_draco.dylib"
                self.assertIn(expected.resolve(), filemesh._get_blender_draco_dll_paths())

    def test_current_gltf_library_names_on_each_platform(self):
        self.bpy.utils.script_paths = lambda: [str(self.root / "scripts")]
        for platform, old_name, new_name in (
            ("win32", "extern_draco.dll", "bf_intern_draco_bridge.dll"),
            ("darwin", "libextern_draco.dylib", "libbf_intern_draco_bridge.dylib"),
            ("linux", "libextern_draco.so", "libbf_intern_draco_bridge.so"),
        ):
            with self.subTest(platform=platform), mock.patch.object(filemesh.sys, "platform", platform):
                with mock.patch.object(filemesh.importlib, "import_module", side_effect=ImportError):
                    candidates = filemesh._get_blender_draco_dll_paths()
                    for name in (old_name, new_name):
                        self.assertIn((self.root / "scripts/addons_core/io_scene_gltf2" / name).resolve(), candidates)

    def test_explicit_gltf_library_override_is_first(self):
        expected = self.root / filemesh._draco_lib_name()
        with mock.patch.dict(os.environ, {"BLENDER_EXTERN_DRACO_LIBRARY_PATH": str(expected)}):
            with mock.patch.object(filemesh.importlib, "import_module", side_effect=ImportError):
                self.assertEqual(filemesh._get_blender_draco_dll_paths()[0], expected.resolve())

    def test_invalid_library_does_not_hide_working_candidate(self):
        bad, good = self.root / "bad.dll", self.root / "good.dll"
        bad.touch()
        good.touch()
        library = mock.MagicMock()
        with mock.patch.object(filemesh, "_get_blender_draco_dll_paths", return_value=[bad, good]):
            with mock.patch.object(filemesh.ctypes.cdll, "LoadLibrary", side_effect=[SimpleNamespace(), library]) as load:
                self.assertIs(filemesh._load_blender_draco_dll(), library)
                self.assertIs(filemesh._load_blender_draco_dll(), library)
                self.assertEqual(load.call_count, 2)
                self.assertIsNone(filemesh._DRACO_LOAD_ERROR)

    def test_missing_library_retries_and_reports_searched_path(self):
        path = self.root / "decoder.dll"
        with mock.patch.object(filemesh, "_get_blender_draco_dll_paths", return_value=[path]):
            self.assertIsNone(filemesh._load_blender_draco_dll())
            self.assertIn(str(path), filemesh._DRACO_LOAD_ERROR)
            path.touch()
            library = mock.MagicMock()
            with mock.patch.object(filemesh.ctypes.cdll, "LoadLibrary", return_value=library):
                self.assertIs(filemesh._load_blender_draco_dll(), library)

    def test_load_errors_identify_every_existing_candidate(self):
        paths = [self.root / "first.dll", self.root / "second.dll"]
        for path in paths:
            path.touch()
        with mock.patch.object(filemesh, "_get_blender_draco_dll_paths", return_value=paths):
            with mock.patch.object(filemesh.ctypes.cdll, "LoadLibrary", side_effect=OSError("Missing dependency")):
                self.assertIsNone(filemesh._load_blender_draco_dll())
        for path in paths:
            self.assertIn(str(path), filemesh._DRACO_LOAD_ERROR)
        self.assertIn("Missing dependency", filemesh._DRACO_LOAD_ERROR)
