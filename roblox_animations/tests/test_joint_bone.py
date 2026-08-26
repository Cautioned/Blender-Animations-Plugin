"""Tests for the shared weapon-joint helpers extracted from the rbxm
weapon import flow: the equipped-joint solve in core.utils and the
Motor6D-style bone builder in rig.creation."""
import unittest

import bpy
from mathutils import Matrix

from roblox_animations.core.constants import get_transform_to_blender
from roblox_animations.core.utils import (
    cf_to_mat,
    mat_to_cf,
    solve_equipped_joint_matrix,
)
from roblox_animations.rig.creation import create_joint_bone

_IDENTITY_CF12 = [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]


def _cf(x, y, z):
    return [x, y, z, 1, 0, 0, 0, 1, 0, 0, 0, 1]


def _max_abs(mat):
    return max(abs(mat[row][col]) for row in range(4) for col in range(4))


class TestSolveEquippedJoint(unittest.TestCase):
    def test_identity_joint_keeps_parent(self):
        parent = cf_to_mat(_cf(0, 5, 0))
        equipped = solve_equipped_joint_matrix(parent, _IDENTITY_CF12, _IDENTITY_CF12)
        self.assertAlmostEqual(_max_abs(equipped - parent), 0.0, places=6)

    def test_c0_offset_shifts_equipped(self):
        parent = cf_to_mat(_cf(0, 5, 0))
        c0 = _cf(1, 0, 0)
        c1 = _IDENTITY_CF12
        equipped = solve_equipped_joint_matrix(parent, c0, c1)
        self.assertAlmostEqual(equipped[0][3], 1.0, places=6)
        self.assertAlmostEqual(equipped[1][3], 5.0, places=6)

    def test_c1_cancels_in_joint_space(self):
        # joint position = parent * C0; C1 must not move the joint anchor.
        parent = cf_to_mat(_cf(0, 0, 0))
        c0 = _cf(2, 0, 0)
        c1 = _cf(0.5, 0.25, 0)
        equipped = solve_equipped_joint_matrix(parent, c0, c1)
        joint = equipped @ cf_to_mat(c1)
        expected = parent @ cf_to_mat(c0)
        self.assertAlmostEqual(_max_abs(joint - expected), 0.0, places=6)


class TestCreateJointBone(unittest.TestCase):
    def setUp(self):
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        bpy.ops.object.add(type="ARMATURE", enter_editmode=True)
        self.ao = bpy.context.object
        self.ao.name = "JointBoneTest"
        self.amt = self.ao.data
        parent = self.amt.edit_bones.new("Hand")
        parent.head = (0, 0, 0)
        parent.tail = (0, 1, 0)
        bpy.ops.object.mode_set(mode="OBJECT")

    def tearDown(self):
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.data.objects.remove(self.ao, do_unlink=True)

    def test_creates_bone_with_motor6d_props(self):
        t2b = get_transform_to_blender()
        transform = _cf(0, 5, 0)
        name = create_joint_bone(
            self.ao, "Hand", transform, _IDENTITY_CF12, _IDENTITY_CF12, "Grip"
        )
        self.assertIsNotNone(name)
        bone = self.amt.bones.get(name)
        self.assertIsNotNone(bone)
        self.assertEqual(bone.parent.name, "Hand")
        for prop in ("transform", "transform0", "transform1", "nicetransform"):
            self.assertIn(prop, bone, f"missing {prop}")
        self.assertTrue(bone.get("is_transformable"))
        self.assertEqual(bone.get("rbx_joint_type"), "Motor6D")
        self.assertEqual(bone.get("rbx_original_parent"), "Hand")
        expected_head = (t2b @ cf_to_mat(transform)).to_translation()
        self.assertAlmostEqual(
            _max_abs(Matrix.Translation(bone.head_local) - Matrix.Translation(expected_head)),
            0.0,
            places=5,
        )

    def test_duplicate_name_is_suffixed(self):
        transform = _cf(0, 5, 0)
        first = create_joint_bone(
            self.ao, "Hand", transform, _IDENTITY_CF12, _IDENTITY_CF12, "Grip"
        )
        second = create_joint_bone(
            self.ao, "Hand", transform, _IDENTITY_CF12, _IDENTITY_CF12, "Grip"
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertIn(first, self.amt.bones)
        self.assertIn(second, self.amt.bones)

    def test_missing_parent_returns_none(self):
        result = create_joint_bone(
            self.ao, "NoSuchBone", _cf(0, 0, 0), _IDENTITY_CF12, _IDENTITY_CF12, "Grip"
        )
        self.assertIsNone(result)
        # helper must leave the armature in object mode even on failure
        self.assertEqual(self.ao.mode, "OBJECT")

    def test_mat_to_cf_roundtrip(self):
        mat = Matrix.Translation((1, 2, 3)) @ Matrix.Rotation(0.7, 4, "Z")
        cf = mat_to_cf(mat)
        self.assertAlmostEqual(_max_abs(cf_to_mat(cf) - mat), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
