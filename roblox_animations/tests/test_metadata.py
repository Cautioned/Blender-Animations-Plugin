import bpy
import unittest
import mathutils
import importlib
from ..core import utils
from ..operators import import_ops
from ..server import requests

# Reload utils to ensure we test the latest version
importlib.reload(utils)
importlib.reload(import_ops)
importlib.reload(requests)

class TestBoneMetadata(unittest.TestCase):
    def setUp(self):
        """Set up a clean scene before each test."""
        # Clean up any leftover data
        for armature in bpy.data.armatures:
            bpy.data.armatures.remove(armature)
        for obj in bpy.data.objects:
            bpy.data.objects.remove(obj)
            
        # Create a dummy armature and bone for testing IDProperties
        bpy.ops.object.add(type="ARMATURE", enter_editmode=True)
        self.armature_obj = bpy.context.object
        self.armature = self.armature_obj.data
        self.bone = self.armature.edit_bones.new("TestBone")
        self.bone.head = (0, 0, 0)
        self.bone.tail = (0, 1, 0)
        bpy.ops.object.mode_set(mode="POSE")
        self.pose_bone = self.armature_obj.pose.bones["TestBone"]

    def tearDown(self):
        """Clean up after tests."""
        if self.armature_obj:
            bpy.data.objects.remove(self.armature_obj)

    def test_to_matrix_identity(self):
        """Test to_matrix with Identity matrix input."""
        mat = mathutils.Matrix.Identity(4)
        result = utils.to_matrix(mat)
        self.assertEqual(result, mat)

    def test_to_matrix_list_of_lists(self):
        """Test to_matrix with 4x4 list of lists (standard Blender IDProperty storage)."""
        mat_list = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]
        ]
        result = utils.to_matrix(mat_list)
        expected = mathutils.Matrix.Identity(4)
        self.assertEqual(result, expected)

    def test_to_matrix_flattened_list(self):
        """Test to_matrix with flattened 16-element list (IDPropertyArray behavior)."""
        flat_list = [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0
        ]
        result = utils.to_matrix(flat_list)
        expected = mathutils.Matrix.Identity(4)
        self.assertEqual(result, expected)

    def test_to_matrix_cframe_list(self):
        """Test to_matrix with 12-element CFrame list."""
        # Identity CFrame: x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22
        cframe_list = [
            0.0, 0.0, 0.0,
            1.0, 0.0, 0.0,
            0.0, 1.0, 0.0,
            0.0, 0.0, 1.0
        ]
        result = utils.to_matrix(cframe_list)
        expected = mathutils.Matrix.Identity(4)
        self.assertEqual(result, expected)

    def test_to_matrix_id_property_array(self):
        """Test to_matrix with actual IDPropertyArray from Blender."""
        # Assign a list to a custom property - Blender converts this to IDPropertyArray
        # for certain types, or keeps it as IDProperty
        
        # Case 1: Assigning a flattened list often results in IDPropertyArray
        flat_list = [float(i) for i in range(16)]
        self.pose_bone.bone["test_prop"] = flat_list
        
        # Read it back
        prop_val = self.pose_bone.bone["test_prop"]
        
        # Verify it works with to_matrix
        result = utils.to_matrix(prop_val)
        
        # Construct expected matrix manually
        expected = mathutils.Matrix([
            [0.0, 1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0, 7.0],
            [8.0, 9.0, 10.0, 11.0],
            [12.0, 13.0, 14.0, 15.0]
        ])
        
        self.assertEqual(result, expected)

    def test_to_matrix_invalid_input(self):
        """Test to_matrix with invalid input returns Identity."""
        # Empty list
        self.assertEqual(utils.to_matrix([]), mathutils.Matrix.Identity(4))
        # Wrong size list
        self.assertEqual(utils.to_matrix([1, 2, 3]), mathutils.Matrix.Identity(4))
        # None
        self.assertEqual(utils.to_matrix(None), mathutils.Matrix.Identity(4))
        # String
        self.assertEqual(utils.to_matrix("invalid"), mathutils.Matrix.Identity(4))

    def test_is_transformable_types(self):
        """Test that is_transformable can be stored/retrieved as bool or int."""
        # Case 1: Boolean True
        self.pose_bone.bone["is_transformable"] = True
        val_bool = self.pose_bone.bone.get("is_transformable", False)
        self.assertTrue(bool(val_bool))
        
        # Case 2: Integer 1
        self.pose_bone.bone["is_transformable"] = 1
        val_int = self.pose_bone.bone.get("is_transformable", False)
        self.assertTrue(bool(val_int))
        
        # Case 3: Boolean False
        self.pose_bone.bone["is_transformable"] = False
        val_false = self.pose_bone.bone.get("is_transformable", False)
        self.assertFalse(bool(val_false))
        
        # Case 4: Integer 0
        self.pose_bone.bone["is_transformable"] = 0
        val_zero = self.pose_bone.bone.get("is_transformable", False)
        self.assertFalse(bool(val_zero))
        
        # Case 5: Missing (default)
        if "is_transformable" in self.pose_bone.bone:
            del self.pose_bone.bone["is_transformable"]
        val_missing = self.pose_bone.bone.get("is_transformable", False)
        self.assertFalse(bool(val_missing))

    def test_annotate_weapon_original_parents_prefers_real_motor_parent(self):
        right_hand_world = [
            1.5, 0.0, -2.2750000953674316,
            1.0, 0.0, 0.0,
            0.0, 1.0, 0.0,
            0.0, 0.0, 1.0,
        ]
        joints_tree = {
            "jname": "Grip",
            "transform": [
                1.4680471420288086, -0.19142484664916992, -1.9057445526123047,
                5.4569682106375694e-12, 1.6370904631912708e-11, -1.0,
                -1.0235978731287787e-12, -1.0, 1.6370904631912708e-11,
                -1.0, -1.0235978731287787e-12, 5.4569682106375694e-12,
            ],
            "jointtransform0": [
                -0.031952857971191406, -0.24796056747436523, -0.19142484664916992,
                5.4569682106375694e-12, 1.6370904631912708e-11, -1.0,
                1.0, 1.0235978731287787e-12, -5.4569682106375694e-12,
                -1.0235978731287787e-12, -1.0, 1.6370904631912708e-11,
            ],
            "jointtransform1": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            "children": [
                {
                    "jname": "Base",
                    "transform": [
                        1.4604473114013672, -0.07422494888305664, -0.7596445679664612,
                        5.4569682106375694e-12, 1.6370904631912708e-11, -1.0,
                        -1.0235978731287787e-12, -1.0, 1.6370904631912708e-11,
                        -1.0, -1.0235978731287787e-12, 5.4569682106375694e-12,
                    ],
                    "jointtransform0": [
                        -1.1461000442504883, -0.11719989776611328, 0.007599830627441406,
                        1.0, 0.0, 0.0,
                        0.0, 1.0, 0.0,
                        0.0, 0.0, 1.0,
                    ],
                    "jointtransform1": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                    "children": [
                        {
                            "jname": "Bullets",
                            "transform": [
                                1.4574470520019531, -0.6325249671936035, -0.9844445586204529,
                                5.4569682106375694e-12, 1.6370904631912708e-11, -1.0,
                                -1.0235978731287787e-12, -1.0, 1.6370904631912708e-11,
                                -1.0, -1.0235978731287787e-12, 5.4569682106375694e-12,
                            ],
                            "jointtransform0": [
                                0.2247999906539917, 0.5583000183105469, 0.0030002593994140625,
                                1.0, 0.0, 0.0,
                                0.0, 1.0, 0.0,
                                0.0, 0.0, 1.0,
                            ],
                            "jointtransform1": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                            "children": [],
                        }
                    ],
                }
            ],
        }

        assignments = import_ops._annotate_weapon_original_parents(
            joints_tree,
            "RightHand",
            utils.to_matrix(right_hand_world),
        )

        self.assertEqual(assignments["Grip"], "RightHand")
        self.assertEqual(assignments["Base"], "RightHand")
        self.assertEqual(assignments["Bullets"], "RightHand")
        self.assertEqual(joints_tree["children"][0]["originalParentBone"], "RightHand")
        self.assertEqual(joints_tree["children"][0]["children"][0]["originalParentBone"], "RightHand")

    def test_get_reported_bone_parent_name_prefers_preserved_motor_parent(self):
        class DummyBone(dict):
            def __init__(self, name, parent=None, **props):
                super().__init__(props)
                self.name = name
                self.parent = parent

        class DummyPoseBone:
            def __init__(self, bone, parent=None):
                self.bone = bone
                self.parent = parent

        right_hand = DummyBone("RightHand")
        grip = DummyBone("Grip", parent=right_hand)
        handguard = DummyBone("Handguard", parent=grip, rbx_original_parent="RightHand")

        self.assertEqual(requests._get_reported_bone_parent_name(handguard), "RightHand")
        self.assertEqual(
            requests._get_reported_bone_parent_name(DummyPoseBone(handguard, parent=DummyPoseBone(grip))),
            "RightHand",
        )
        self.assertEqual(requests._get_reported_bone_parent_name(grip), "RightHand")

    def test_real_data_bone_can_store_preserved_motor_parent(self):
        self.pose_bone.bone["rbx_original_parent"] = "RightHand"
        self.assertEqual(requests._get_reported_bone_parent_name(self.pose_bone), "RightHand")

