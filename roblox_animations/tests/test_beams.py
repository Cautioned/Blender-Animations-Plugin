"""Tests for the static Roblox Beam recreation builder."""
import unittest

import bpy
from mathutils import Vector

from ..operators import import_ops


def _identity_cf(x=0.0, y=0.0, z=0.0):
    return [x, y, z, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def _z_rotation_cf(degrees, x=0.0, y=0.0, z=0.0):
    """CFrame whose local X axis is rotated in the XY plane."""
    import math

    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return [x, y, z, c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0]


def _beam(**overrides):
    data = {
        "name": "TestBeam",
        "texture": "",
        "width0": 0.2,
        "width1": 0.1,
        "curve_size0": 0.1,
        "curve_size1": 0.05,
        "segments": 8,
        "texture_length": 2.0,
        "texture_speed": 0.0,
        "face_camera": False,
        "light_emission": 1.0,
        "light_influence": 0.0,
        "z_offset": 0.0,
        "attachment0": {"cf": _identity_cf(), "part_cf": None},
        "attachment1": {"cf": _identity_cf(z=10.0), "part_cf": None},
    }
    data.update(overrides)
    return data


class BeamBuilderTests(unittest.TestCase):
    def setUp(self):
        self._collection = bpy.data.collections.new("rbx_test_beams")
        bpy.context.scene.collection.children.link(self._collection)

    def tearDown(self):
        for obj in list(self._collection.all_objects):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except ReferenceError:
                pass
        try:
            bpy.data.collections.remove(self._collection)
        except ReferenceError:
            pass

    def _built(self, beams):
        count = import_ops._create_rbxl_beams(self._collection, beams)
        objects = [obj for obj in self._collection.all_objects if obj.type == "MESH"]
        return count, objects

    def test_beam_strip_builds(self):
        count, objects = self._built([_beam()])
        self.assertEqual(count, 1)
        self.assertEqual(len(objects), 1)
        mesh = objects[0].data
        self.assertEqual(len(mesh.vertices), 2 * (8 + 1))
        self.assertEqual(len(mesh.polygons), 8)
        self.assertEqual(len(mesh.materials), 1)
        self.assertEqual(len(mesh.uv_layers), 1)
        material = mesh.materials[0]
        self.assertIsNotNone(material.node_tree.nodes.get("Emission"))
        # UVs are bulk-uploaded in polygon-loop order.  Confirm the ribbon
        # ends still retain the expected across-width coordinates.
        self.assertEqual(tuple(mesh.uv_layers[0].data[0].uv), (0.0, 1.0))
        self.assertEqual(tuple(mesh.uv_layers[0].data[1].uv), (0.0, 0.0))

    def test_beam_face_camera_billboard(self):
        camera_data = bpy.data.cameras.new("rbx_test_cam")
        camera = bpy.data.objects.new("rbx_test_cam", camera_data)
        bpy.context.scene.collection.objects.link(camera)
        bpy.context.scene.camera = camera
        try:
            count, objects = self._built([_beam(face_camera=True)])
            self.assertEqual(count, 1)
            mesh = objects[0].data
            self.assertEqual(len(mesh.vertices), 2 * (8 + 1))
            self.assertEqual(len(mesh.polygons), 8)
            self.assertFalse(objects[0].constraints)
            # Camera-facing construction must never rotate the endpoint away
            # from Attachment1. Average each end ring to remove width.
            start = (mesh.vertices[0].co + mesh.vertices[1].co) * 0.5
            end = (mesh.vertices[-2].co + mesh.vertices[-1].co) * 0.5
            self.assertLess(start.length, 1e-5)
            self.assertLess((end - Vector((0.0, -10.0, 0.0))).length, 1e-5)
        finally:
            bpy.context.scene.camera = None
            bpy.data.objects.remove(camera, do_unlink=True)
            bpy.data.cameras.remove(camera_data)

    def test_beam_skips_degenerate(self):
        count, objects = self._built(
            [_beam(attachment1={"cf": _identity_cf(), "part_cf": None})]
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(objects), 0)

    def test_rotated_attachment_controls_curve_direction(self):
        """CurveSize follows Attachment.RightVector, never a CFrame row."""
        count, objects = self._built([_beam(
            curve_size0=2.0,
            curve_size1=0.0,
            attachment0={"cf": _z_rotation_cf(90.0), "part_cf": None},
        )])
        self.assertEqual(count, 1)
        # At t=.5 a cubic curve with P1=(0,2,0) lies .75 units on +Y.
        left = objects[0].data.vertices[8].co
        right = objects[0].data.vertices[9].co
        middle = (left + right) * 0.5
        # The addon maps Roblox +Y to Blender +Z.  Averaging the two ribbon
        # vertices removes width, leaving the Bezier centreline.
        self.assertAlmostEqual(middle.z, 0.75, places=5)

    def test_face_camera_basis_preserves_beam_axis(self):
        count, objects = self._built([_beam(face_camera=True)])
        self.assertEqual(count, 1)
        mesh = objects[0].data
        start = (mesh.vertices[0].co + mesh.vertices[1].co) * 0.5
        end = (mesh.vertices[-2].co + mesh.vertices[-1].co) * 0.5
        self.assertLess((end - start - Vector((0, -10, 0))).length, 1e-5)
