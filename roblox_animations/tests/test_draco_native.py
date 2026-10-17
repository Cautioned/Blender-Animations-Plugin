"""Real bundled-decoder checks; run inside each Blender build, without network.

The fixture is our own single triangle, encoded with Blender 3.4's glTF
exporter (positions only), wrapped in a Roblox version-7 COREMESH chunk.
Missing native decoders are failures, not skips or mocked successes.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from unittest import mock

import bpy

from ..operators.import_ops import _lazy_fetch_filemesh
from ..rig import filemesh
from ..rig.mesh_surface import _populate_mesh_geometry


class TestDracoNative(unittest.TestCase):
    def setUp(self):
        self.raw = base64.b64decode(
            (Path(__file__).parent / "fixtures/draco_triangle.mesh.b64").read_bytes()
        )
        filemesh._DRACO_DLL = filemesh._DRACO_DLL_UNINITIALIZED
        filemesh._DRACO_LOAD_ERROR = None

    def check_triangle(self, mesh):
        self.assertEqual(mesh["num_vertices"], 3)
        self.assertEqual(len(mesh["faces"]), 1)
        self.assertEqual(set(mesh["faces"][0]), {0, 1, 2})
        self.assertEqual(set(tuple(p) for p in mesh["positions"]),
                         {(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0)})

    def test_cold_parallel_place_import_decoder(self):
        # RBXL's lazy jobs request arrays; Draco must fall back to the native
        # decoder, including when its first load happens on a worker thread.
        with mock.patch.object(filemesh, "fetch_filemesh_bytes", return_value=self.raw):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda i: _lazy_fetch_filemesh(str(910000000000000 + i), {}), range(32)))
        for mesh in results:
            self.check_triangle(mesh)

    def test_real_mesh_construction_after_decode(self):
        data = filemesh.parse_filemesh(self.raw)
        self.check_triangle(data)
        mesh = bpy.data.meshes.new("DracoCompatibilityTest")
        try:
            _populate_mesh_geometry(mesh, data["positions"], data["faces"])
            self.assertEqual(len(mesh.vertices), 3)
            self.assertEqual(len(mesh.polygons), 1)
            self.assertFalse(mesh.validate())
        finally:
            bpy.data.meshes.remove(mesh)

    def test_repeat_decoding_releases_each_decoder(self):
        for _ in range(20):
            self.check_triangle(filemesh.parse_filemesh(self.raw))
