"""Asset normals must survive mesh updates, evaluation, and blend reloads."""

import tempfile
import unittest
from pathlib import Path

import bpy
from mathutils import Vector

from ..rig.mesh_surface import _apply_mesh_custom_normals, _set_mesh_smooth_shading


class TestMeshNormals(unittest.TestCase):
    def setUp(self):
        self.mesh = bpy.data.meshes.new("normal_regression")
        # Split vertices mimic FileMesh UV seams: smoothing topology alone
        # cannot recover the authored normals across these separate triangles.
        self.mesh.from_pydata(
            [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0), (0, 0, 1)],
            [],
            [(0, 1, 2), (3, 4, 5)],
        )
        _set_mesh_smooth_shading(self.mesh)
        self.normal = Vector((0.3, 0.4, 0.5)).normalized()
        self.vertices = [{"normal": tuple(self.normal)} for _ in self.mesh.vertices]
        self.obj = bpy.data.objects.new("normal_regression", self.mesh)
        bpy.context.scene.collection.objects.link(self.obj)

    def tearDown(self):
        bpy.data.objects.remove(self.obj, do_unlink=True)
        bpy.data.meshes.remove(self.mesh)

    def assertNormals(self, mesh):
        self.assertTrue(mesh.has_custom_normals)
        if hasattr(mesh, "calc_normals_split"):
            mesh.calc_normals_split()
        normals = (
            [n.vector for n in mesh.corner_normals]
            if hasattr(mesh, "corner_normals")
            else [n.normal for n in mesh.loops]
        )
        self.assertEqual(len(normals), 6)
        for n in normals:
            self.assertGreater(Vector(n).dot(self.normal), 0.99999)

    def test_persists_through_update_and_evaluation(self):
        self.assertTrue(_apply_mesh_custom_normals(self.mesh, self.vertices))
        self.mesh.update()
        self.assertNormals(self.mesh)
        bpy.context.view_layer.update()
        self.assertNormals(
            self.obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
        )

    def test_persists_through_blend_reload(self):
        self.assertTrue(_apply_mesh_custom_normals(self.mesh, self.vertices))
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "normals.blend")
            bpy.data.libraries.write(path, {self.mesh})
            with bpy.data.libraries.load(path) as (source, target):
                target.meshes = [self.mesh.name]
            loaded = target.meshes[0]
            try:
                self.assertNormals(loaded)
            finally:
                bpy.data.meshes.remove(loaded)

    def test_rejects_invalid_external_normals(self):
        for bad in (
            None,
            (0, 0, 0),
            (float("nan"), 0, 1),
            (0, float("inf"), 1),
            (1, 2),
            ("bad", 0, 1),
        ):
            with self.subTest(normal=bad):
                vertices = list(self.vertices)
                vertices[-1] = {"normal": bad}
                self.assertFalse(_apply_mesh_custom_normals(self.mesh, vertices))
                self.assertFalse(self.mesh.has_custom_normals)
        self.assertFalse(_apply_mesh_custom_normals(self.mesh, self.vertices[:-1]))
