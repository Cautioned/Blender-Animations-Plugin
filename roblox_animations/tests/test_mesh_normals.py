"""Asset normals must survive mesh updates, evaluation, and blend reloads."""

import tempfile
import unittest
from pathlib import Path

import bpy
from mathutils import Vector

from ..rig.mesh_surface import (
    _apply_mesh_custom_normals,
    _apply_mesh_loop_uvs,
    _populate_mesh_geometry,
    _set_mesh_smooth_shading,
)


class TestMeshNormals(unittest.TestCase):
    def setUp(self):
        self.mesh = bpy.data.meshes.new("normal_regression")
        # Split vertices mimic FileMesh UV seams: smoothing topology alone
        # cannot recover the authored normals across these separate triangles.
        _populate_mesh_geometry(
            self.mesh,
            [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0), (0, 0, 1)],
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

    def test_repairs_repeated_vertex_without_losing_valid_uvs(self):
        # This face causes EXCEPTION_ACCESS_VIOLATION in Blender 5.2's
        # mesh_normals_corner_custom_set without topology validation.
        positions = [tuple(v.co) for v in self.mesh.vertices]
        self.mesh.clear_geometry()
        self.assertTrue(_populate_mesh_geometry(
            self.mesh, positions, [(0, 1, 2), (0, 1, 1), (3, 4, 5)]
        ))
        _set_mesh_smooth_shading(self.mesh)
        uvs = [(i / 10, i / 20) for i in range(9)]
        self.assertTrue(_apply_mesh_loop_uvs(self.mesh, self.vertices, uvs))
        self.assertTrue(_apply_mesh_custom_normals(self.mesh, self.vertices))
        self.assertEqual([tuple(p.vertices) for p in self.mesh.polygons],
                         [(0, 1, 2), (3, 4, 5)])
        self.assertEqual(len(self.mesh.vertices), 6)
        for loop, original in zip(self.mesh.uv_layers.active.data, [0, 1, 2, 6, 7, 8]):
            self.assertAlmostEqual(loop.uv.x, uvs[original][0])
            self.assertAlmostEqual(loop.uv.y, 1 - uvs[original][1])
        self.mesh.update()
        self.assertNormals(self.mesh)
        bpy.context.view_layer.update()
        self.assertNormals(self.obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data)

    def test_all_invalid_faces_skip_native_setter(self):
        positions = [tuple(v.co) for v in self.mesh.vertices]
        self.mesh.clear_geometry()
        _populate_mesh_geometry(self.mesh, positions, [(0, 1, 1), (2, 2, 2)])
        self.assertFalse(_apply_mesh_custom_normals(self.mesh, self.vertices))
        self.assertEqual(len(self.mesh.polygons), 0)
        self.assertFalse(self.mesh.has_custom_normals)

    def test_duplicate_faces_are_repaired(self):
        positions = [tuple(v.co) for v in self.mesh.vertices]
        self.mesh.clear_geometry()
        _populate_mesh_geometry(self.mesh, positions, [(0, 1, 2), (0, 1, 2), (3, 4, 5)])
        _set_mesh_smooth_shading(self.mesh)
        self.assertTrue(_apply_mesh_custom_normals(self.mesh, self.vertices))
        self.assertNormals(self.mesh)
