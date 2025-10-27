import bpy
import unittest
import os
import json
import mathutils
import sys
import math
import time # Add time module for benchmarking

# Add the project root (parent of 'roblox_animations') to the path
# This allows us to import the package modules
dir_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if dir_path not in sys.path:
    sys.path.append(dir_path)
    
# Import the specific modules we need for testing using relative imports
from ..animation import serialization
from ..animation import easing
from ..core import utils
from ..server import requests

# Reload modules to pick up changes in Blender's test environment
import importlib
importlib.reload(utils)
importlib.reload(requests)
importlib.reload(easing)
importlib.reload(serialization)

# Now we can import the functions from the addon's modules
from ..animation.serialization import (
    serialize,
    is_deform_bone_rig,
)
from ..animation.easing import map_blender_to_roblox_easing
from ..core.utils import invalidate_armature_cache
from ..server.requests import execute_import_animation

class TestAnimationSerialization(unittest.TestCase):
    def setUp(self):
        """Set up a clean scene before each test."""
        # Force newly inserted keyframes to default to LINEAR interpolation for deterministic sparse baking
        self._prev_keyframe_interp = bpy.context.preferences.edit.keyframe_new_interpolation_type
        bpy.context.preferences.edit.keyframe_new_interpolation_type = 'LINEAR'

        # Don't clear the scene property as it causes enum errors
        # The property will be updated when we create new armatures
        
        # Clean up any leftover data from previous runs
        for action in bpy.data.actions:
            bpy.data.actions.remove(action)
        for armature in bpy.data.armatures:
            bpy.data.armatures.remove(armature)
        for mesh in bpy.data.meshes:
            bpy.data.meshes.remove(mesh)
        for empty in bpy.data.objects:
            if empty.type == 'EMPTY':
                bpy.data.objects.remove(empty, do_unlink=True)
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()

        # Force update the scene
        bpy.context.view_layer.update()

        # Invalidate armature cache to ensure fresh state
        invalidate_armature_cache()
        
        # Force the enum property to update by calling the items function
        # This ensures the enum is properly updated with current armatures
        from ..core.utils import armature_items
        try:
            # Force update the enum items
            armature_items(None, bpy.context)
        except:
            # If this fails, just continue - the enum will update when needed
            pass
        
        # Don't clear the scene property as it causes enum errors
        # The property will be updated automatically when needed

        self.armature_obj = None
        self.ik_target = None
        self.unconstrained_bone = None

    def clear_scene_property(self):
        """Clear the scene property that tracks the active armature."""
        # Don't set the property to empty string as it causes enum errors
        # The property will be updated when we create new armatures
        pass

    def tearDown(self):
        """A safe cleanup after each test."""
        # Restore user preference for keyframe interpolation
        if hasattr(self, "_prev_keyframe_interp"):
            bpy.context.preferences.edit.keyframe_new_interpolation_type = self._prev_keyframe_interp

        # Don't clear the scene property as it causes enum errors
        # The next test's setUp will handle cleanup
        
        # By this point, setUp of the next test should have cleaned everything,
        # but we keep this for good measure in case a test is run individually.
        try:
            if self.armature_obj and self.armature_obj.name in bpy.data.objects:
                armature_data = self.armature_obj.data
                bpy.data.objects.remove(self.armature_obj, do_unlink=True)
                if armature_data and armature_data.name in bpy.data.armatures:
                    bpy.data.armatures.remove(armature_data, do_unlink=True)
        except (ReferenceError, RuntimeError):
            # This can happen if the test itself modifies the scene in unexpected ways.
            # The setUp will handle the full cleanup.
            pass

    def set_action_interpolation(self, action, interpolation='LINEAR'):
        """Helper to set interpolation for all keyframes in an action."""
        from ..core.utils import get_action_fcurves
        fcurves = get_action_fcurves(action)
        if not action or not fcurves:
            return
        for fcurve in fcurves:
            for kp in fcurve.keyframe_points:
                kp.interpolation = interpolation

    def set_full_range_bake(self, enabled: bool):
        """Helper to set the full range bake setting."""
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        if settings:
            settings.rbx_full_range_bake = enabled

    def create_ik_rig(self):
        """Creates a simple IK rig for testing."""
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        self.armature_obj = bpy.context.object
        armature = self.armature_obj.data
        armature.name = "TestRig"
        self.armature_obj.name = "TestRigObject"
        
        # Force the enum property to update after creating the armature
        from ..core.utils import armature_items
        try:
            armature_items(None, bpy.context)
        except:
            pass

        # Create bones
        bones = []
        bone_names = ["Root", "UpperLeg", "LowerLeg", "Foot"]
        for i, name in enumerate(bone_names):
            bone = armature.edit_bones.new(name)
            bone.head = (0, 0, 2 - i * 0.5)
            bone.tail = (0, 0, 2 - (i + 1) * 0.5)
            if i > 0:
                bone.parent = bones[i-1]
            bones.append(bone)
        
        # Unconstrained bone for checking sparse baking
        unconstrained_bone_edit = armature.edit_bones.new("Unconstrained")
        unconstrained_bone_edit.head = (1, 0, 2)
        unconstrained_bone_edit.tail = (1, 0, 1.5)
        unconstrained_bone_edit.parent = bones[0] # Parent to root

        # IK Target bone
        ik_target_edit = armature.edit_bones.new("IKTarget")
        ik_target_edit.head = (0.5, 0, 0)
        ik_target_edit.tail = (0.5, 0, -0.5)

        bpy.ops.object.mode_set(mode='POSE')

        # Add the custom property that the serializer expects
        for bone in self.armature_obj.pose.bones:
            # IK Targets are controllers, not part of the final animation data.
            # Root is usually static.
            if bone.name not in ["Root", "IKTarget"]:
                bone.bone["is_transformable"] = True

            # THIS IS THE FIX: Manually add the properties that load_rigbone would have added.
            # The serializer functions (`serialize_animation_state` and `serialize_deform_animation_state`)
            # absolutely require these to exist, even if they are just identity matrices for a test.
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Add IK constraint
        foot_pose_bone = self.armature_obj.pose.bones["Foot"]
        ik_constraint = foot_pose_bone.constraints.new(type='IK')
        ik_constraint.target = self.armature_obj
        ik_constraint.subtarget = "IKTarget"
        ik_constraint.chain_count = 2 # LowerLeg and UpperLeg

        # Animate IK target
        self.ik_target = self.armature_obj.pose.bones["IKTarget"]
        self.ik_target.location = (0, 0, 0)
        self.ik_target.keyframe_insert(data_path="location", frame=1)
        self.ik_target.location = (1, 0, 0)
        self.ik_target.keyframe_insert(data_path="location", frame=20)
        
        # Animate unconstrained bone
        self.unconstrained_bone = self.armature_obj.pose.bones["Unconstrained"]
        self.unconstrained_bone.rotation_quaternion = (1, 0, 0, 0)
        self.unconstrained_bone.keyframe_insert(data_path="rotation_quaternion", frame=1)
        self.unconstrained_bone.rotation_quaternion = (0.707, 0.707, 0, 0) # 90 deg rotation
        self.unconstrained_bone.keyframe_insert(data_path="rotation_quaternion", frame=20)

        # This is the crucial missing step:
        # Assign the created action to the armature's animation data
        if self.armature_obj.animation_data is None:
            self.armature_obj.animation_data_create()
        self.armature_obj.animation_data.action = bpy.data.actions[-1]
        self.set_action_interpolation(self.armature_obj.animation_data.action, 'LINEAR')

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        
        # Update the dependency graph to ensure all changes are propagated
        bpy.context.view_layer.update()

        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Set the active armature for the serializer
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed


    def test_ik_chain_is_fully_baked(self):
        """Tests that bones in an IK chain are baked on every frame,"""
        self.clear_scene_property()
        self.armature_obj = self.armature_obj
        self.create_ik_rig()
        
        # We need to be in POSE mode for serialization to work correctly
        bpy.ops.object.mode_set(mode='POSE')
        
        # Force a dependency graph update.
        bpy.context.scene.frame_set(bpy.context.scene.frame_current)
        
        start_time = time.perf_counter()
        result = serialize(self.armature_obj)
        end_time = time.perf_counter()
        print(f"\n[BENCHMARK] 'test_ik_chain_is_fully_baked' serialize time: {end_time - start_time:.4f} seconds")

        # --- ASSERTIONS for Hybrid Bake ---
        self.assertTrue(result, "Serialization returned no result.")
        self.assertIn("kfs", result, "Serialized data is missing 'kfs' key.")
        
        keyframes = result["kfs"]
        
        # In this specific test, the IK target is always moving, so every constrained bone
        # should have a key on every frame. This means the hybrid bake will produce a full
        # 20 keyframes, but ONLY the constrained bones will be in all of them.
        self.assertEqual(len(keyframes), 20, "Expected a full 20 keyframes because the IK target is always moving.")

        # 2. Check that constrained bones are fully baked.
        ik_bones = {"UpperLeg", "LowerLeg", "Foot"}
        for i in range(len(keyframes)):
            kf = keyframes[i]
            for bone_name in ik_bones:
                self.assertIn(bone_name, kf['kf'], f"IK bone '{bone_name}' missing from fully baked frame {i+1}")

        # 3. Check that the unconstrained bone is sparsely baked.
        unconstrained_bone_name = "Unconstrained"
        unconstrained_keyframes = 0
        for kf in keyframes:
            if unconstrained_bone_name in kf['kf']:
                unconstrained_keyframes += 1
        
        self.assertEqual(unconstrained_keyframes, 2, f"Expected 2 keyframes for sparsely baked unconstrained bone, but found {unconstrained_keyframes}.")
        
        
    def test_unconstrained_rig_is_sparse(self):
        """Tests that a simple rig with no constraints uses sparse baking."""
        self.clear_scene_property()
        # Create a new rig without constraints for this test
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()
        
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        self.armature_obj = bpy.context.object
        armature = self.armature_obj.data
        armature.name = "TestRigSparse"
        self.armature_obj.name = "TestRigObjectSparse"

        root_bone = armature.edit_bones.new("Root")
        root_bone.head = (0, 0, 1)
        root_bone.tail = (0, 0, 0)

        child_bone = armature.edit_bones.new("Child")
        child_bone.head = (0, 0, 0)
        child_bone.tail = (0, -1, 0)
        child_bone.parent = root_bone

        bpy.ops.object.mode_set(mode='POSE')

        for bone in self.armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Animate only the child bone
        child_pose_bone = self.armature_obj.pose.bones["Child"]
        child_pose_bone.location = (0, 0, 0)
        child_pose_bone.keyframe_insert(data_path="location", frame=1)
        child_pose_bone.location = (0, 1, 0)
        child_pose_bone.keyframe_insert(data_path="location", frame=20)
        
        if self.armature_obj.animation_data is None:
            self.armature_obj.animation_data_create()
        self.armature_obj.animation_data.action = bpy.data.actions[-1]
        
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        bpy.context.view_layer.update()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed

        bpy.ops.object.mode_set(mode='POSE')
        
        # Force a dependency graph update for consistency.
        bpy.context.scene.frame_set(bpy.context.scene.frame_current)
        
        start_time = time.perf_counter()
        result = serialize(self.armature_obj)
        end_time = time.perf_counter()
        print(f"\n[BENCHMARK] 'test_unconstrained_rig_is_sparse' serialize time: {end_time - start_time:.4f} seconds")

        self.assertTrue(result, "Serialization returned no result for sparse test.")
        self.assertIn("kfs", result, "Serialized data is missing 'kfs' key for sparse test.")
        
        keyframes = result["kfs"]
        # With full-range bake defaulting to True, expect all frames from frame_start to frame_end
        expected_frames = bpy.context.scene.frame_end - bpy.context.scene.frame_start + 1
        self.assertEqual(len(keyframes), expected_frames, f"Expected {expected_frames} keyframes for full-range bake, but got {len(keyframes)}.")
        
        # Check that the unanimated root bone is not in the keyframes
        for kf in keyframes:
            self.assertNotIn("Root", kf['kf'], "Unanimated root bone should not be present in sparse keyframes.")
            self.assertIn("Child", kf['kf'], "Animated child bone should be present in sparse keyframes.")

    def test_complex_rig_with_empty_ik_target(self):
        """Tests that a rig with an IK chain targeting an Empty object and other constraints is fully baked."""
        self.clear_scene_property()
        # --- SETUP ---
        # Create Armature
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "ComplexRig"
        armature = armature_obj.data
        armature.name = "ComplexArmature"

        bones = []
        for i, name in enumerate(["Root", "UpperLeg", "LowerLeg", "Foot"]):
            bone = armature.edit_bones.new(name)
            bone.head = (0, 0, 2 - i * 0.5)
            bone.tail = (0, 0, 2 - (i + 1) * 0.5)
            if i > 0:
                bone.parent = bones[i-1]
            bones.append(bone)
        
        bpy.ops.object.mode_set(mode='POSE')

        # Add custom properties required by the serializer
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Must be in Object Mode to add new objects to the scene
        bpy.ops.object.mode_set(mode='OBJECT')

        # Create Empty to act as IK target
        bpy.ops.object.add(type='EMPTY', location=(1, 0, 0))
        ik_target_empty = bpy.context.object
        ik_target_empty.name = "IK_Target_Empty"

        # To add constraints, the armature must be the active object and in Pose Mode
        bpy.context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='POSE')

        # Add IK constraint to Foot, targeting the Empty
        foot_pose_bone = armature_obj.pose.bones["Foot"]
        ik_constraint = foot_pose_bone.constraints.new(type='IK')
        ik_constraint.target = ik_target_empty
        ik_constraint.chain_count = 2

        # Add another constraint (e.g., Limit Rotation on the knee)
        lower_leg_pose_bone = armature_obj.pose.bones["LowerLeg"]
        limit_rot_constraint = lower_leg_pose_bone.constraints.new(type='LIMIT_ROTATION')
        limit_rot_constraint.use_limit_x = True
        limit_rot_constraint.min_x = -math.pi / 2
        limit_rot_constraint.max_x = 0
        limit_rot_constraint.owner_space = 'LOCAL'

        # The armature needs an action for the serializer to find, even if the animation
        # itself is on another object (the IK target). An empty action is sufficient.
        if armature_obj.animation_data is None:
            armature_obj.animation_data_create()
        armature_obj.animation_data.action = bpy.data.actions.new(name="ComplexRigAction")

        # Animate the Empty. keyframe_insert() will create and use an action on the Empty itself.
        ik_target_empty.location = (1, 0, 0)
        ik_target_empty.keyframe_insert(data_path="location", frame=1)
        ik_target_empty.location = (1, 1, 1)
        ik_target_empty.keyframe_insert(data_path="location", frame=20)
        
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # --- EXECUTION ---
        bpy.context.scene.frame_set(1) # Force depsgraph update
        
        result = serialize(armature_obj)

        # --- ASSERTION ---
        self.assertTrue(result, "Serialization returned no result for complex rig.")
        self.assertIn("kfs", result, "Serialized data is missing 'kfs' key for complex rig.")
        
        keyframes = result["kfs"]
        expected_frames = bpy.context.scene.frame_end - bpy.context.scene.frame_start + 1
        self.assertEqual(len(keyframes), expected_frames, f"Expected {expected_frames} baked frames for complex rig, but got {len(keyframes)}.")
        
        # Check that the main animated bones are present
        constrained_bones = {"UpperLeg", "LowerLeg", "Foot"}
        mid_frame_kf = keyframes[10]['kf']
        for bone_name in constrained_bones:
            self.assertIn(bone_name, mid_frame_kf, f"Constrained bone '{bone_name}' was not found in a baked keyframe of the complex rig.")

    def test_dynamic_parenting_with_child_of(self):
        """Tests a bone with a Child Of constraint whose influence is animated."""
        self.clear_scene_property()
        # --- SETUP ---
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "DynamicParentRig"
        armature = armature_obj.data
        armature.name = "DynamicParentArmature"

        # Create a 'parent' bone that will be animated
        parent_bone = armature.edit_bones.new("ParentBone")
        parent_bone.head = (0, 0, 1)
        parent_bone.tail = (0, 1, 1)

        # Create a 'child' bone that will be constrained
        child_bone = armature.edit_bones.new("ChildBone")
        child_bone.head = (2, 0, 0)
        child_bone.tail = (2, 1, 0)
        
        bpy.ops.object.mode_set(mode='POSE')

        # Add custom properties
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Animate the parent bone
        parent_pose_bone = armature_obj.pose.bones["ParentBone"]
        parent_pose_bone.location = (0, 0, 0)
        parent_pose_bone.keyframe_insert(data_path="location", frame=1)
        parent_pose_bone.location = (0, 5, 0)
        parent_pose_bone.keyframe_insert(data_path="location", frame=20)
        
        # Add and animate the Child Of constraint
        child_pose_bone = armature_obj.pose.bones["ChildBone"]
        constraint = child_pose_bone.constraints.new(type='CHILD_OF')
        constraint.target = armature_obj
        constraint.subtarget = "ParentBone"
        
        constraint.influence = 0.0
        constraint.keyframe_insert(data_path='influence', frame=5)
        constraint.influence = 1.0
        constraint.keyframe_insert(data_path='influence', frame=10)

        # Assign an action to the armature object
        if armature_obj.animation_data is None:
            armature_obj.animation_data_create()
        armature_obj.animation_data.action = bpy.data.actions[-1]
        
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # --- EXECUTION ---
        bpy.context.scene.frame_set(1)
        result = serialize(armature_obj)

        # --- ASSERTION ---
        self.assertTrue(result, "Serialization returned no result for dynamic parenting test.")
        keyframes = result["kfs"]
        
        # The 'ChildBone' has a constraint, so it should be fully baked.
        # The 'ParentBone' is animated sparsely, but the hybrid bake logic will also
        # insert keys for it at frames where other significant events happen (like the constraint's influence changing).
        # Frames 1, 5, 10, 20 are the key moments.
        self.assertEqual(len(keyframes), 20, "Expected 20 frames for a rig with an animated constraint.")
        
        child_bone_name = "ChildBone"
        parent_bone_name = "ParentBone"
        parent_keyframe_count = 0
        
        for kf in keyframes:
            self.assertIn(child_bone_name, kf['kf'], f"'{child_bone_name}' should be in every frame of a constrained bake.")
            if parent_bone_name in kf['kf']:
                parent_keyframe_count += 1
        
        # With full-range bake defaulting to True, parent bone should appear in all frames
        expected_frames = bpy.context.scene.frame_end - bpy.context.scene.frame_start + 1
        self.assertEqual(parent_keyframe_count, expected_frames, f"Parent bone should appear in all {expected_frames} frames with full-range bake.")
        
        # Check for presence at specific key times
        parent_frames = [kf['t'] for kf in keyframes if parent_bone_name in kf['kf']]
        fps = bpy.context.scene.render.fps
        self.assertIn(0.0, [round(t * fps) / fps for t in parent_frames]) # Frame 1
        self.assertIn(round(19/fps, 4), [round(t, 4) for t in parent_frames]) # Frame 20

    def test_kitchen_sink_constraints(self):
        """Tests multiple, varied constraints targeting different animated Empties."""
        # --- SETUP ---
        # Armature
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "KitchenSinkRig"
        armature = armature_obj.data
        armature.name = "KitchenSinkArmature"

        bone_names = ["Root", "BoneA", "BoneB", "BoneC"]
        for i, name in enumerate(bone_names):
            bone = armature.edit_bones.new(name)
            bone.head = (i * 2, 0, 2)
            bone.tail = (i * 2, 0, 1)
            if i > 0:
                bone.parent = armature.edit_bones["Root"]
        bpy.ops.object.mode_set(mode='POSE')
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)
        
        # Empties and Actions
        bpy.ops.object.mode_set(mode='OBJECT')
        empty_a = bpy.ops.object.add(type='EMPTY', location=(0, 2, 0))
        empty_a = bpy.context.object
        empty_a.name = "TargetA"
        
        empty_b = bpy.ops.object.add(type='EMPTY', location=(2, 2, 0))
        empty_b = bpy.context.object
        empty_b.name = "TargetB"

        empty_c = bpy.ops.object.add(type='EMPTY', location=(4, 2, 0))
        empty_c = bpy.context.object
        empty_c.name = "TargetC"

        # Animate empties
        empty_a.keyframe_insert(data_path="location", frame=1)
        empty_a.animation_data.action = empty_a.animation_data.action or bpy.data.actions[-1]
        self.set_action_interpolation(empty_a.animation_data.action, 'LINEAR')
        empty_a.location.z = 5
        empty_a.keyframe_insert(data_path="location", frame=20)
        
        empty_b.keyframe_insert(data_path="rotation_euler", frame=1)
        empty_b.animation_data.action = empty_b.animation_data.action or bpy.data.actions[-1]
        self.set_action_interpolation(empty_b.animation_data.action, 'LINEAR')
        empty_b.rotation_euler.x = math.pi
        empty_b.keyframe_insert(data_path="rotation_euler", frame=20)
        
        empty_c.keyframe_insert(data_path="location", frame=1)
        empty_c.animation_data.action = empty_c.animation_data.action or bpy.data.actions[-1]
        self.set_action_interpolation(empty_c.animation_data.action, 'LINEAR')
        empty_c.location.y = -5
        empty_c.keyframe_insert(data_path="location", frame=20)
        
        # Add Constraints
        bpy.context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='POSE')
        
        armature_obj.pose.bones["BoneA"].constraints.new('COPY_LOCATION').target = empty_a
        armature_obj.pose.bones["BoneB"].constraints.new('COPY_ROTATION').target = empty_b
        armature_obj.pose.bones["BoneC"].constraints.new('DAMPED_TRACK').target = empty_c

        # Assign a dummy action to the armature itself
        if armature_obj.animation_data is None:
            armature_obj.animation_data_create()
        armature_obj.animation_data.action = bpy.data.actions.new(name="KitchenSinkAction")
        
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # --- EXECUTION ---
        bpy.context.scene.frame_set(1)
        
        # --- BENCHMARKING ---
        start_time = time.perf_counter()
        result = serialize(armature_obj)
        end_time = time.perf_counter()
        print(f"\n[BENCHMARK] 'test_kitchen_sink_constraints' serialize time: {end_time - start_time:.4f} seconds")

        # --- ASSERTION ---
        self.assertTrue(result, "Serialization returned no result for kitchen sink test.")
        keyframes = result["kfs"]
        
        self.assertEqual(len(keyframes), 20, "Expected a full 20 frames for a rig with multiple constraints.")
        
        constrained_bones = {"BoneA", "BoneB", "BoneC"}
        for kf in keyframes:
            for bone_name in constrained_bones:
                self.assertIn(bone_name, kf['kf'], f"Constrained bone '{bone_name}' should be in every frame.")
            self.assertNotIn("Root", kf['kf'], "Unanimated, unconstrained 'Root' bone should not be baked.")

    def test_branched_hierarchy_and_interleaved_keyframes(self):
        """
        Tests a rig with a branched hierarchy (a torso with two arms),
        multiple independent IK constraints, and interleaved keyframes on the parent bone.
        """
        # --- SETUP ---
        # Armature
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "BranchedRig"
        armature = armature_obj.data
        armature.name = "BranchedArmature"

        # Torso
        torso = armature.edit_bones.new("Torso")
        torso.head = (0, 0, 2)
        torso.tail = (0, 0, 0)

        # Left Arm
        l_upper = armature.edit_bones.new("L_UpperArm")
        l_upper.parent = torso
        l_upper.head = (0, 0.1, 1.8)
        l_upper.tail = (2, 0.1, 1.8)
        l_lower = armature.edit_bones.new("L_LowerArm")
        l_lower.parent = l_upper
        l_lower.head = (2, 0.1, 1.8)
        l_lower.tail = (4, 0.1, 1.8)
        
        # Right Arm
        r_upper = armature.edit_bones.new("R_UpperArm")
        r_upper.parent = torso
        r_upper.head = (0, -0.1, 1.8)
        r_upper.tail = (2, -0.1, 1.8)
        r_lower = armature.edit_bones.new("R_LowerArm")
        r_lower.parent = r_upper
        r_lower.head = (2, -0.1, 1.8)
        r_lower.tail = (4, -0.1, 1.8)
        
        bpy.ops.object.mode_set(mode='POSE')
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)
        
        # Empties for IK targets
        bpy.ops.object.mode_set(mode='OBJECT')
        l_ik_target = bpy.ops.object.add(type='EMPTY', location=(5, 0.1, 1.8)); l_ik_target = bpy.context.object
        r_ik_target = bpy.ops.object.add(type='EMPTY', location=(5, -0.1, 1.8)); r_ik_target = bpy.context.object
        l_ik_target.name = "L_IK_Target"
        r_ik_target.name = "R_IK_Target"

        # Animate IK targets and Torso
        bpy.context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='POSE')
        
        torso_pose = armature_obj.pose.bones["Torso"]
        torso_pose.keyframe_insert(data_path="location", frame=1)
        torso_pose.location.y = 2
        torso_pose.keyframe_insert(data_path="location", frame=10)
        torso_pose.location.y = 0
        torso_pose.keyframe_insert(data_path="location", frame=20)
        
        # Add constraints. A chain_count of 1 ensures the IK only affects the UpperArm, not the Torso.
        armature_obj.pose.bones["L_LowerArm"].constraints.new('IK').target = l_ik_target
        armature_obj.pose.bones["R_LowerArm"].constraints.new('IK').target = r_ik_target
        armature_obj.pose.bones["L_LowerArm"].constraints[0].chain_count = 1
        armature_obj.pose.bones["R_LowerArm"].constraints[0].chain_count = 1

        # Assign action to armature
        if armature_obj.animation_data is None:
            armature_obj.animation_data_create()
        armature_obj.animation_data.action = bpy.data.actions[-1]
        
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # --- EXECUTION ---
        bpy.context.scene.frame_set(1)
        result = serialize(armature_obj)

        # --- ASSERTION ---
        self.assertTrue(result, "Serialization returned no result for branched hierarchy test.")
        keyframes = result["kfs"]
        
        self.assertEqual(len(keyframes), 20, "Expected a full 20 frames for the branched rig with constraints.")
        
        constrained_bones = {"L_UpperArm", "L_LowerArm", "R_UpperArm", "R_LowerArm"}
        sparse_bone = "Torso"
        
        torso_keyframe_count = 0
        for kf in keyframes:
            for bone_name in constrained_bones:
                self.assertIn(bone_name, kf['kf'], f"Constrained arm bone '{bone_name}' should be in every frame.")
            if sparse_bone in kf['kf']:
                torso_keyframe_count += 1
                
        # With full-range bake defaulting to True, expect all frames
        expected_torso_frames = bpy.context.scene.frame_end - bpy.context.scene.frame_start + 1
        self.assertEqual(torso_keyframe_count, expected_torso_frames, f"Expected {expected_torso_frames} keyframes for full-range baked 'Torso' bone, but found {torso_keyframe_count}.")

    def test_nla_tracks_force_full_bake(self):
        """Tests that having active NLA tracks forces a full, simple bake."""
        # --- SETUP ---
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "NlaRig"
        armature = armature_obj.data
        armature.name = "NlaArmature"

        armature.edit_bones.new("BoneA").head = (0,0,1); armature.edit_bones[-1].tail = (0,0,0)
        armature.edit_bones.new("BoneB").head = (1,0,1); armature.edit_bones[-1].tail = (1,0,0)
        
        bpy.ops.object.mode_set(mode='POSE')
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Create Action 1 for BoneA
        action_a = bpy.data.actions.new("ActionA")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action_a
        bone_a_pose = armature_obj.pose.bones["BoneA"]
        bone_a_pose.location = (0, 0, 0)
        bone_a_pose.keyframe_insert(data_path="location", frame=1)
        bone_a_pose.location = (0, 5, 0)
        bone_a_pose.keyframe_insert(data_path="location", frame=20)
        
        # Create Action 2 for BoneB
        action_b = bpy.data.actions.new("ActionB")
        armature_obj.animation_data.action = action_b
        bone_b_pose = armature_obj.pose.bones["BoneB"]
        bone_b_pose.rotation_quaternion = (1, 0, 0, 0)
        bone_b_pose.keyframe_insert(data_path="rotation_quaternion", frame=1)
        bone_b_pose.rotation_quaternion = (0.707, 0.707, 0, 0)
        bone_b_pose.keyframe_insert(data_path="rotation_quaternion", frame=20)
        
        # Set up NLA tracks
        armature_obj.animation_data.action = None # Unlink active action
        tracks = armature_obj.animation_data.nla_tracks
        track_a = tracks.new()
        track_a.name = "TrackA"
        track_a.strips.new(name="StripA", start=1, action=action_a)
        
        track_b = tracks.new()
        track_b.name = "TrackB"
        track_b.strips.new(name="StripB", start=1, action=action_b)

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # --- EXECUTION ---
        bpy.context.scene.frame_set(1)

        start_time = time.perf_counter()
        result = serialize(armature_obj)
        end_time = time.perf_counter()
        print(f"\n[BENCHMARK] 'test_nla_tracks_force_full_bake' serialize time: {end_time - start_time:.4f} seconds")

        # --- ASSERTION ---
        self.assertTrue(result, "Serialization returned no result for NLA test.")
        keyframes = result["kfs"]
        
        self.assertEqual(len(keyframes), 20, "Expected a full 20 frames for a rig with NLA tracks.")
        
        # Check that data from both strips is present in the bake
        mid_frame_kf = keyframes[10]['kf']
        self.assertIn("BoneA", mid_frame_kf, "Bone from first NLA track not found in baked keyframe.")
        self.assertIn("BoneB", mid_frame_kf, "Bone from second NLA track not found in baked keyframe.")

    def test_easing_serialization(self):
        """Tests that Blender's easing types are correctly mapped to Roblox enums."""
        # --- SETUP ---
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "EasingRig"
        armature = armature_obj.data
        armature.name = "EasingArmature"

        bone_names = ["SupportedEase", "UnsupportedEase", "ConstantEase"]
        for i, name in enumerate(bone_names):
            bone = armature.edit_bones.new(name)
            bone.head = (i, 0, 1)
            bone.tail = (i, 0, 0)
        
        bpy.ops.object.mode_set(mode='POSE')
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Create a single action
        action = bpy.data.actions.new("EasingAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        # Animate bones
        for bone_name in ["SupportedEase", "UnsupportedEase", "ConstantEase"]:
            pbone = armature_obj.pose.bones[bone_name]
            pbone.location = (0, 0, 0)
            pbone.keyframe_insert(data_path="location", frame=1)
            pbone.location = (0, 5, 0)
            pbone.keyframe_insert(data_path="location", frame=20)

        # Set specific easing types on ALL f-curves for the given property to ensure consistent test data
        from ..core.utils import get_action_fcurves
        fcurves = get_action_fcurves(action)
        for fcurve in fcurves:
            kp = fcurve.keyframe_points[0]
            if 'SupportedEase' in fcurve.data_path:
                kp.interpolation = 'CUBIC'
                kp.easing = 'EASE_IN_OUT'
            elif 'UnsupportedEase' in fcurve.data_path:
                kp.interpolation = 'SINE'
                kp.easing = 'EASE_IN'
            elif 'ConstantEase' in fcurve.data_path:
                kp.interpolation = 'CONSTANT'

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
            
        # --- EXECUTION ---
        bpy.context.scene.frame_set(1)
        result = serialize(armature_obj)

        # --- ASSERTION ---
        self.assertTrue(result, "Serialization returned no result for easing test.")
        keyframes = result["kfs"]
        
        self.assertEqual(len(keyframes), 2, "Expected 2 keyframes for a sparse bake.")
        
        first_frame_kf = keyframes[0]['kf']
        
        # Check SupportedEase (CUBIC, EASE_IN_OUT) -> ("CubicV2", "InOut")
        supported_data = first_frame_kf.get("SupportedEase")
        self.assertIsNotNone(supported_data, "SupportedEase bone missing from keyframe.")
        self.assertEqual(supported_data[1], "CubicV2", "Supported easing style did not map correctly.")
        self.assertEqual(supported_data[2], "InOut", "Supported easing direction did not map correctly.")
        
        # Check UnsupportedEase (SINE, EASE_IN) -> ("Linear", "Out")
        unsupported_data = first_frame_kf.get("UnsupportedEase")
        self.assertIsNotNone(unsupported_data, "UnsupportedEase bone missing from keyframe.")
        self.assertEqual(unsupported_data[1], "Linear", "Unsupported easing style did not fall back to Linear.")
        self.assertEqual(unsupported_data[2], "Out", "Unsupported easing direction did not fall back to Out.")

        # Check ConstantEase (CONSTANT) -> ("Constant", "Out")
        constant_data = first_frame_kf.get("ConstantEase")
        self.assertIsNotNone(constant_data, "ConstantEase bone missing from keyframe.")
        self.assertEqual(constant_data[1], "Constant", "Constant easing style did not map correctly.")
        self.assertEqual(constant_data[2], "Out", "Constant easing direction did not map correctly.")

    def test_copy_transforms_no_keys(self):
        """
        Tests that a bone with a Copy Transforms constraint is fully baked,
        even if it has no keyframes itself.
        """
        # --- SETUP ---
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "CopyTransformRig"
        armature = armature_obj.data
        armature.name = "CopyTransformArmature"

        armature.edit_bones.new("Driver").head = (0,0,1); armature.edit_bones[-1].tail = (0,1,1)
        armature.edit_bones.new("Follower").head = (2,0,1); armature.edit_bones[-1].tail = (2,1,1)
        armature.edit_bones.new("Independent").head = (-2,0,1); armature.edit_bones[-1].tail = (-2,1,1)
        
        bpy.ops.object.mode_set(mode='POSE')
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Create an action and animate the Driver and Independent bones
        action = bpy.data.actions.new("CopyAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        driver_pose = armature_obj.pose.bones["Driver"]
        driver_pose.rotation_quaternion = (1, 0, 0, 0)
        driver_pose.keyframe_insert(data_path="rotation_quaternion", frame=1)
        driver_pose.rotation_quaternion = (0.707, 0, 0.707, 0)
        driver_pose.keyframe_insert(data_path="rotation_quaternion", frame=20)
        
        independent_pose = armature_obj.pose.bones["Independent"]
        independent_pose.location = (0, 0, 0)
        independent_pose.keyframe_insert(data_path="location", frame=1)
        independent_pose.location = (0, 5, 0)
        independent_pose.keyframe_insert(data_path="location", frame=20)

        self.set_action_interpolation(action, 'LINEAR')
        
        # Add the constraint to the Follower bone
        follower_pose = armature_obj.pose.bones["Follower"]
        constraint = follower_pose.constraints.new(type='COPY_TRANSFORMS')
        constraint.target = armature_obj
        constraint.subtarget = "Driver"

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # --- EXECUTION ---
        bpy.context.scene.frame_set(1)
        result = serialize(armature_obj)

        # --- ASSERTION ---
        self.assertTrue(result, "Serialization returned no result for copy transforms test.")
        keyframes = result["kfs"]
        
        # Because one bone is constrained, a full 20 frames will be sampled.
        # The optimization step will preserve all frames containing the constrained bone.
        self.assertEqual(len(keyframes), 20, "Expected a full 20 frames due to the constraint.")

        follower_kf_count = 0
        driver_kf_count = 0
        independent_kf_count = 0
        
        for kf in keyframes:
            kf_bones = kf['kf'].keys()
            if "Follower" in kf_bones:
                follower_kf_count += 1
            if "Driver" in kf_bones:
                driver_kf_count += 1
            if "Independent" in kf_bones:
                independent_kf_count += 1
        
        self.assertEqual(follower_kf_count, 20, "Constrained 'Follower' bone should be baked on every frame.")
        self.assertEqual(driver_kf_count, 2, "Sparsely animated 'Driver' bone should only have 2 keyframes.")
        self.assertEqual(independent_kf_count, 2, "Sparsely animated 'Independent' bone should only have 2 keyframes.")

    def test_constraint_driven_with_no_action(self):
        """
        Tests that a rig with NO action is still exported if its bones are
        driven by constraints targeting an animated external object.
        """
        # --- SETUP ---
        # 1. Armature with one bone, no animation
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "NoActionRig"
        armature = armature_obj.data
        armature.name = "NoActionArmature"
        armature.edit_bones.new("Follower").head=(0,0,1); armature.edit_bones[-1].tail=(0,0,0)
        
        bpy.ops.object.mode_set(mode='POSE')
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # 2. An animated Empty
        bpy.ops.object.mode_set(mode='OBJECT')
        driver_empty = bpy.ops.object.add(type='EMPTY', location=(0, 0, 0)); driver_empty = bpy.context.object
        driver_empty.name = "DriverEmpty"
        driver_empty.keyframe_insert(data_path="location", frame=1)
        driver_empty.location.z = 5
        driver_empty.keyframe_insert(data_path="location", frame=20)
        if driver_empty.animation_data and driver_empty.animation_data.action:
            self.set_action_interpolation(driver_empty.animation_data.action, 'LINEAR')
        
        # 3. Constraint linking the two
        bpy.context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='POSE')
        follower_pose = armature_obj.pose.bones["Follower"]
        constraint = follower_pose.constraints.new(type='COPY_LOCATION')
        constraint.target = driver_empty
        
        # 4. Ensure the armature has NO action
        if armature_obj.animation_data:
            armature_obj.animation_data_clear()

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # --- EXECUTION ---
        bpy.context.scene.frame_set(1)
        result = serialize(armature_obj)

        # --- ASSERTION ---
        self.assertTrue(result, "Serialization returned no result for constraint-only test.")
        keyframes = result["kfs"]
        
        self.assertEqual(len(keyframes), 20, "Expected a full 20 frames for a rig driven only by constraints.")
        
        follower_kf_count = 0
        for kf in keyframes:
            if "Follower" in kf['kf'].keys():
                follower_kf_count += 1
        
        self.assertEqual(follower_kf_count, 20, "Constrained 'Follower' bone should be baked on every frame.")

    def test_external_rig_constraint_no_action(self):
        """
        Tests that a "puppet" rig with no action is correctly baked when its
        bones are constrained to a separate, animated "master" rig.
        """
        # --- SETUP ---
        # 1. Create Master Rig and animate it
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        master_obj = bpy.context.object
        master_obj.name = "MasterRig"
        master_armature = master_obj.data
        master_armature.name = "MasterArmature"
        master_armature.edit_bones.new("MasterBone").head=(0,0,1); master_armature.edit_bones[-1].tail=(0,0,0)
        
        bpy.ops.object.mode_set(mode='POSE')
        master_action = bpy.data.actions.new("MasterAction")
        master_obj.animation_data_create()
        master_obj.animation_data.action = master_action
        master_pose_bone = master_obj.pose.bones["MasterBone"]
        master_pose_bone.location = (0,0,0)
        master_pose_bone.keyframe_insert(data_path="location", frame=1)
        master_pose_bone.location = (5,0,0)
        master_pose_bone.keyframe_insert(data_path="location", frame=20)
        self.set_action_interpolation(master_action, 'LINEAR')

        # 2. Create Puppet Rig (the one we will export)
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(2, 0, 0))
        puppet_obj = bpy.context.object
        puppet_obj.name = "PuppetRig"
        puppet_armature = puppet_obj.data
        puppet_armature.name = "PuppetArmature"
        puppet_armature.edit_bones.new("PuppetBone").head=(2,0,1); puppet_armature.edit_bones[-1].tail=(2,0,0)
        
        bpy.ops.object.mode_set(mode='POSE')
        for bone in puppet_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)
        
        # 3. Constrain Puppet to Master
        puppet_pose_bone = puppet_obj.pose.bones["PuppetBone"]
        constraint = puppet_pose_bone.constraints.new(type='COPY_TRANSFORMS')
        constraint.target = master_obj
        constraint.subtarget = "MasterBone"

        # 4. Ensure Puppet has NO action
        if puppet_obj.animation_data:
            puppet_obj.animation_data_clear()

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed

        # --- EXECUTION ---
        bpy.context.scene.frame_set(1)
        result = serialize(puppet_obj)
        
        # --- ASSERTION ---
        self.assertTrue(result, "Serialization returned no result for puppet rig test.")
        keyframes = result["kfs"]
        self.assertEqual(len(keyframes), 20, "Expected a full 20 frames for a puppet rig.")

        # Check that the bone was actually baked and has non-identity transforms
        last_frame_kf = keyframes[-1]['kf']
        self.assertIn("PuppetBone", last_frame_kf, "PuppetBone not found in the last keyframe.")
        
        puppet_bone_data = last_frame_kf["PuppetBone"]
        self.assertIsInstance(puppet_bone_data, list, "Puppet bone data should be a list [cframe, style, dir].")
        self.assertEqual(len(puppet_bone_data), 3, "Puppet bone data should have 3 elements.")

        cframe_components = puppet_bone_data[0]
        # The position should be around (3,0,0) because it started at (2,0,0) and the master moved to (5,0,0)
        self.assertAlmostEqual(cframe_components[0], 3, places=4, msg="Puppet bone was not in the correct final position.")

    def test_deform_rig_detection_with_modifier(self):
        """
        Tests that a rig is correctly identified as a deform bone rig when
        it's linked to a mesh via an Armature modifier.
        """
        # --- SETUP ---
        # 1. Create an armature
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "DeformModifierRig"
        armature_obj.data.name = "DeformModifierArmature"
        armature_obj.data.edit_bones.new("DeformBone").head=(0,0,1); armature_obj.data.edit_bones[-1].tail=(0,0,0)
        
        # 2. Create a mesh object
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        mesh_obj = bpy.context.object
        mesh_obj.name = "DeformingMesh"

        # 3. Link them with an Armature modifier
        modifier = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')
        modifier.object = armature_obj
        
        # --- EXECUTION & ASSERTION ---
        self.assertTrue(is_deform_bone_rig(armature_obj), "Rig with Armature modifier was not detected as a deform rig.")


    def test_deform_rig_export(self):
        """
        Tests the full animation export pipeline for a deform bone rig.
        """
        # --- SETUP ---
        # 1. Create Armature
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "DeformExportRig"
        armature_obj.data.name = "DeformExportArmature"
        
        deform_bone = armature_obj.data.edit_bones.new("TestDeformBone")
        deform_bone.head = (0, 0, 1)
        deform_bone.tail = (0, 0, 0)

        bpy.ops.object.mode_set(mode='OBJECT')

        # 2. Create Mesh and parent it
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0.5))
        mesh_obj = bpy.context.object
        mesh_obj.name = "DeformTestMesh"
        
        # Parent mesh to armature and create vertex groups
        mesh_obj.select_set(True)
        armature_obj.select_set(True)
        bpy.context.view_layer.objects.active = armature_obj
        bpy.ops.object.parent_set(type='ARMATURE_AUTO')
        
        # Ensure the deform bone property is set
        bpy.ops.object.mode_set(mode='POSE')
        pbone = armature_obj.pose.bones["TestDeformBone"]
        self.assertTrue(pbone.bone.use_deform, "Bone should be a deform bone after parenting.")
        
        # 3. Animate the deform bone
        action = bpy.data.actions.new("DeformExportAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action
        
        pbone.location = (0, 0, 0)
        pbone.keyframe_insert(data_path="location", frame=1)
        pbone.location = (2, 3, 4) # Move the bone
        pbone.keyframe_insert(data_path="location", frame=20)
        
        # 4. Set Scene Properties
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        if settings:
            settings.rbx_deform_rig_scale = 0.1  # Use a known scale for consistent testing
        
        # --- EXECUTION ---
        bpy.context.scene.frame_set(20) # Go to the final frame to check the state
        
        start_time = time.perf_counter()
        result = serialize(armature_obj)
        end_time = time.perf_counter()
        print(f"\n[BENCHMARK] 'test_deform_rig_export' serialize time: {end_time - start_time:.4f} seconds")

        # --- ASSERTION ---
        self.assertIsNotNone(result, "Serialization returned None for deform rig.")
        self.assertTrue(result.get("is_deform_bone_rig"), "is_deform_bone_rig flag should be true.")
        self.assertIn("bone_hierarchy", result, "Deform rig export should include hierarchy.")
        self.assertEqual(result["bone_hierarchy"], {"TestDeformBone": None})
        
        self.assertIn("kfs", result, "Result should have keyframes.")
        # With full-range bake defaulting to True, expect all frames
        expected_frames = bpy.context.scene.frame_end - bpy.context.scene.frame_start + 1
        self.assertEqual(len(result["kfs"]), expected_frames, f"Expected {expected_frames} keyframes for full-range deform rig animation.")
        
        last_frame_data = result["kfs"][-1]["kf"]
        self.assertIn("TestDeformBone", last_frame_data, "Deform bone not found in last keyframe.")
        
        # Check the transformed CFrame data.
        # This is the most critical part, as it verifies the complex math in serialize_deform_animation_state.
        cframe_components = last_frame_data["TestDeformBone"][0]
        
        # Blender location: (2, 3, 4)
        # Scale factor: 0.1
        # Expected Roblox location:
        # x_roblox = -loc.x / scale = -2 / 0.1 = -20
        # y_roblox = loc.y / scale = 3 / 0.1 = 30
        # z_roblox = -loc.z / scale = -4 / 0.1 = -40
        self.assertAlmostEqual(cframe_components[0], -20.0, places=4)
        self.assertAlmostEqual(cframe_components[1], 30.0, places=4)
        self.assertAlmostEqual(cframe_components[2], -40.0, places=4)


    def test_static_pose_export(self):
        """
        Tests that an armature with no animation data exports its current
        pose as a single-frame animation.
        """
        # --- SETUP ---
        # 1. Create a simple armature
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "StaticPoseRig"
        armature_obj.data.name = "StaticPoseArmature"
        
        root_bone = armature_obj.data.edit_bones.new("Root")
        root_bone.head = (0, 0, 0)
        root_bone.tail = (0, 0.01, 0) # Use a small Y-axis offset for an identity rest matrix
        
        bone = armature_obj.data.edit_bones.new("Bone")
        bone.head = (0, 0.01, 0)
        bone.tail = (0, 1, 0)
        bone.parent = root_bone
        
        # 2. Set a specific pose but add NO keyframes
        bpy.ops.object.mode_set(mode='POSE')
        pbone = armature_obj.pose.bones["Bone"]
        pbone.rotation_quaternion.rotate(mathutils.Euler((math.radians(90), 0, 0), 'XYZ'))
        
        # Add custom properties so it uses the Motor6D serialization path
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Ensure there is NO action
        if armature_obj.animation_data:
            armature_obj.animation_data_clear()
            
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # --- EXECUTION ---
        result = serialize(armature_obj)
        
        # --- ASSERTION ---
        self.assertIsNotNone(result)
        self.assertEqual(result["t"], 0, "Duration should be 0 for a static pose.")
        self.assertEqual(len(result["kfs"]), 1, "Expected exactly one keyframe for a static pose.")
        
        kf_data = result["kfs"][0]["kf"]
        self.assertIn("Bone", kf_data, "Posed bone should be in the keyframe.")
        self.assertNotIn("Root", kf_data, "Un-posed root bone should not be in the keyframe.")
        
        # Check that the pose is roughly correct (a 90-degree rotation on X)
        cframe_components = kf_data["Bone"][0]
        rows = [
            tuple(float(v) for v in cframe_components[3:6]),
            tuple(float(v) for v in cframe_components[6:9]),
            tuple(float(v) for v in cframe_components[9:12]),
        ]
        rotation_matrix = mathutils.Matrix(rows)
        euler = rotation_matrix.to_euler('XYZ')
        
        self.assertAlmostEqual(math.degrees(euler.x), 90, places=4)


    def test_mixed_rig_export(self):
        """
        Tests that a rig with both deform bones and Motor6D-style bones
        serializes both types of bones correctly in a single animation.
        """
        # --- SETUP ---
        # 1. Create Armature with two child bones
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "MixedRig"
        armature_obj.data.name = "MixedArmature"

        root = armature_obj.data.edit_bones.new("Root")
        root.head = (0, 0, 0); root.tail = (0, 0.01, 0)
        
        deform_child = armature_obj.data.edit_bones.new("DeformChild")
        deform_child.parent = root
        deform_child.head = (-1, 0.01, 0); deform_child.tail = (-1, 1, 0)
        
        motor_child = armature_obj.data.edit_bones.new("MotorChild")
        motor_child.parent = root
        motor_child.head = (1, 0.01, 0); motor_child.tail = (1, 1, 0)
        
        bpy.ops.object.mode_set(mode='OBJECT')

        # 2. Create a mesh and link it to make this a "deform rig"
        bpy.ops.mesh.primitive_cube_add(location=(-1, 0.5, 0))
        mesh_obj = bpy.context.object
        bpy.ops.object.parent_set(type='ARMATURE_AUTO') # This will set use_deform=True on bones with weights
        
        # 3. Configure bones
        bpy.context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='POSE')
        # This bone got weights, so it should be a deform bone
        self.assertTrue(armature_obj.pose.bones["DeformChild"].bone.use_deform)
        
        # Manually configure the other as a Motor6D-style bone
        motor_pbone = armature_obj.pose.bones["MotorChild"]
        motor_pbone.bone.use_deform = False
        motor_pbone.bone["is_transformable"] = True
        motor_pbone.bone["transform"] = mathutils.Matrix.Identity(4)
        motor_pbone.bone["transform0"] = mathutils.Matrix.Identity(4)
        motor_pbone.bone["transform1"] = mathutils.Matrix.Identity(4)
        motor_pbone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # 4. Animate both bones
        action = bpy.data.actions.new("MixedAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action
        
        # Animate DeformChild location
        deform_pbone = armature_obj.pose.bones["DeformChild"]
        deform_pbone.location.x = 0
        deform_pbone.keyframe_insert(data_path="location", frame=1)
        deform_pbone.location.x = -2
        deform_pbone.keyframe_insert(data_path="location", frame=10)
        
        # Animate MotorChild rotation
        motor_pbone.rotation_quaternion = (1, 0, 0, 0)
        motor_pbone.keyframe_insert(data_path="rotation_quaternion", frame=1)
        motor_pbone.rotation_quaternion.rotate(mathutils.Euler((0, 0, math.radians(90)), 'XYZ'))
        motor_pbone.keyframe_insert(data_path="rotation_quaternion", frame=10)

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 10
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # --- EXECUTION ---
        result = serialize(armature_obj)
        
        # --- ASSERTION ---
        self.assertIsNotNone(result)
        last_frame_kf = result["kfs"][-1]["kf"]
        
        # This is the key assertion: both bones should be in the exported data.
        self.assertIn("DeformChild", last_frame_kf, "Deform bone was not exported in mixed rig.")
        self.assertIn("MotorChild", last_frame_kf, "Motor6D bone was not exported in mixed rig.")


    def test_stress_benchmark_many_bones(self):
        """
        Benchmarks the serializer with a large number of bones and a long,
        complex animation to test performance under heavy load.
        """
        # --- SETUP ---
        BONE_COUNT = 100
        FRAME_COUNT = 100

        # 1. Create Armature
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "StressTestRig"
        armature = armature_obj.data
        armature.name = "StressTestArmature"

        # Create a chain of bones
        last_bone = None
        for i in range(BONE_COUNT):
            bone = armature.edit_bones.new(f"Bone.{i:03d}")
            bone.head = (i, 0, 0)
            bone.tail = (i + 0.5, 0, 0)
            if last_bone:
                bone.parent = last_bone
            last_bone = bone
        
        # Add an IK target at the end of the chain
        ik_target_bone = armature.edit_bones.new("IKTarget")
        ik_target_bone.head = (BONE_COUNT, 1, 0)
        ik_target_bone.tail = (BONE_COUNT, 0, 0)

        bpy.ops.object.mode_set(mode='POSE')

        # 2. Add properties and constraints
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Add IK constraint to the last bone in the chain
        last_bone_name = f"Bone.{(BONE_COUNT - 1):03d}"
        last_pose_bone = armature_obj.pose.bones[last_bone_name]
        ik_constraint = last_pose_bone.constraints.new(type='IK')
        ik_constraint.target = armature_obj
        ik_constraint.subtarget = "IKTarget"
        ik_constraint.chain_count = BONE_COUNT # The entire chain is affected

        # 3. Animate the IK target
        action = bpy.data.actions.new("StressTestAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        ik_target_pose = armature_obj.pose.bones["IKTarget"]
        ik_target_pose.location = (0, 0, 0)
        ik_target_pose.keyframe_insert(data_path="location", frame=1)
        ik_target_pose.location = (0, BONE_COUNT / 2, 0)
        ik_target_pose.keyframe_insert(data_path="location", frame=FRAME_COUNT / 2)
        ik_target_pose.location = (0, 0, 0)
        ik_target_pose.keyframe_insert(data_path="location", frame=FRAME_COUNT)

        # 4. Set Scene Properties
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = FRAME_COUNT
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed

        # --- EXECUTION & BENCHMARKING ---
        print(f"\n[BENCHMARK] Starting stress test: {BONE_COUNT} bones, {FRAME_COUNT} frames...")
        bpy.context.scene.frame_set(1) # Ensure depsgraph is updated
        
        start_time = time.perf_counter()
        result = serialize(armature_obj)
        end_time = time.perf_counter()
        
        print(f"[BENCHMARK] 'test_stress_benchmark_many_bones' serialize time: {end_time - start_time:.4f} seconds")
        print(f"[BENCHMARK] that's {(end_time - start_time) / FRAME_COUNT * 1000:.2f}ms per frame")
        print(f"[BENCHMARK] or {(end_time - start_time) / (BONE_COUNT * FRAME_COUNT) * 1000000:.2f}μs per bone per frame")

        # --- ASSERTION ---
        self.assertIsNotNone(result, "Serialization returned None for stress test.")
        # Due to the IK constraint, we expect a full bake
        self.assertEqual(len(result["kfs"]), FRAME_COUNT, f"Expected {FRAME_COUNT} keyframes for stress test.")
        # Check that the last bone in the chain is present in a keyframe
        self.assertIn(last_bone_name, result["kfs"][-1]["kf"], "Last bone in chain not found in final keyframe.")


    def test_benchmark_sparse_vs_full_bake(self):
        """
        Compares performance of sparse baking vs full constraint-driven baking
        to isolate the impact of frame count vs bone count.
        """
        BONE_COUNT = 50
        FRAME_COUNT = 100
        
        # Test 1: Sparse baking (no constraints)
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        sparse_armature = bpy.context.object
        sparse_armature.name = "SparseTestRig"
        
        for i in range(BONE_COUNT):
            bone = sparse_armature.data.edit_bones.new(f"SparseBone.{i:03d}")
            bone.head = (i, 0, 0)
            bone.tail = (i + 0.5, 0, 0)
            
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='POSE')
        
        for bone in sparse_armature.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)
        
        # Animate just one bone sparsely
        action = bpy.data.actions.new("SparseAction")
        sparse_armature.animation_data_create()
        sparse_armature.animation_data.action = action
        
        first_bone = sparse_armature.pose.bones["SparseBone.000"]
        first_bone.location = (0, 0, 0)
        first_bone.keyframe_insert(data_path="location", frame=1)
        first_bone.location = (0, 5, 0)
        first_bone.keyframe_insert(data_path="location", frame=FRAME_COUNT)
        
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = FRAME_COUNT
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        bpy.context.scene.frame_set(1)
        
        start_sparse = time.perf_counter()
        sparse_result = serialize(sparse_armature)
        end_sparse = time.perf_counter()
        
        sparse_time = end_sparse - start_sparse
        print(f"\n[BENCHMARK] Sparse baking ({BONE_COUNT} bones, 2 keyframes): {sparse_time:.4f}s")
        
        # Test 2: Full baking with constraints
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()
        
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        full_armature = bpy.context.object
        full_armature.name = "FullTestRig"
        
        last_bone = None
        for i in range(BONE_COUNT):
            bone = full_armature.data.edit_bones.new(f"FullBone.{i:03d}")
            bone.head = (i, 0, 0)
            bone.tail = (i + 0.5, 0, 0)
            if last_bone:
                bone.parent = last_bone
            last_bone = bone
            
        # Add IK target
        ik_target = full_armature.data.edit_bones.new("IKTarget")
        ik_target.head = (BONE_COUNT, 1, 0)
        ik_target.tail = (BONE_COUNT, 0, 0)
        
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='POSE')
        
        for bone in full_armature.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)
        
        # Add constraint
        last_bone_name = f"FullBone.{(BONE_COUNT - 1):03d}"
        constraint = full_armature.pose.bones[last_bone_name].constraints.new(type='IK')
        constraint.target = full_armature
        constraint.subtarget = "IKTarget"
        constraint.chain_count = BONE_COUNT
        
        # Same animation as sparse test
        action2 = bpy.data.actions.new("FullAction")
        full_armature.animation_data_create()
        full_armature.animation_data.action = action2
        
        ik_pose = full_armature.pose.bones["IKTarget"]
        ik_pose.location = (0, 0, 0)
        ik_pose.keyframe_insert(data_path="location", frame=1)
        ik_pose.location = (0, 5, 0)
        ik_pose.keyframe_insert(data_path="location", frame=FRAME_COUNT)
        
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        bpy.context.scene.frame_set(1)
        
        start_full = time.perf_counter()
        full_result = serialize(full_armature)
        end_full = time.perf_counter()
        
        full_time = end_full - start_full
        print(f"[BENCHMARK] Full baking ({BONE_COUNT} bones, {FRAME_COUNT} frames): {full_time:.4f}s")
        print(f"[BENCHMARK] Slowdown factor: {full_time / sparse_time:.1f}x")
        print(f"[BENCHMARK] Time per frame in full bake: {full_time / FRAME_COUNT * 1000:.2f}ms")
        
        # Verify results - sparse test now uses full-range bake by default
        expected_sparse_frames = FRAME_COUNT  # full-range bake means all frames
        self.assertEqual(len(sparse_result["kfs"]), expected_sparse_frames, f"Sparse with full-range should have {expected_sparse_frames} keyframes")
        self.assertEqual(len(full_result["kfs"]), FRAME_COUNT, f"Full should have {FRAME_COUNT} keyframes")
        
        # Verify keyframe ordering
        sparse_times = [kf["t"] for kf in sparse_result["kfs"]]
        full_times = [kf["t"] for kf in full_result["kfs"]]
        self.assertEqual(sparse_times, sorted(sparse_times), "Sparse keyframes should be ordered")
        self.assertEqual(full_times, sorted(full_times), "Full bake keyframes should be ordered")

    def test_keyframe_ordering_robustness(self):
        """
        Tests that keyframes are always ordered correctly, even with complex timing scenarios
        that could cause floating point precision issues.
        """
        # Create armature with complex timing
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "OrderingTestRig"
        armature = armature_obj.data
        armature.name = "OrderingTestArmature"

        # Create bones
        for i in range(5):
            bone = armature.edit_bones.new(f"Bone.{i:03d}")
            bone.head = (i, 0, 0)
            bone.tail = (i + 0.5, 0, 0)
            if i > 0:
                bone.parent = armature.edit_bones[f"Bone.{i-1:03d}"]
        
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='POSE')
        
        # Add custom properties
        for bone in armature_obj.pose.bones:
            bone.bone["is_transformable"] = True
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # Create action with complex timing (non-integer fps, sub-frame keyframes)
        action = bpy.data.actions.new("ComplexTimingAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action
        
        # Set complex fps that could cause precision issues
        bpy.context.scene.render.fps = 30  # Use integer fps but test with sub-frame keyframes
        
        # Add keyframes with potentially problematic timing
        bone = armature_obj.pose.bones["Bone.000"]
        bone.location = (0, 0, 0)
        bone.keyframe_insert(data_path="location", frame=1)
        bone.location = (1, 0, 0)
        bone.keyframe_insert(data_path="location", frame=10.5)  # Sub-frame
        bone.location = (2, 0, 0)
        bone.keyframe_insert(data_path="location", frame=20)
        bone.location = (3, 0, 0)
        bone.keyframe_insert(data_path="location", frame=30.33)  # Another sub-frame
        
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 30
        
        # Invalidate cache to ensure new armature is available
        invalidate_armature_cache()
        
        # Don't set the scene property as it causes enum errors
        # The property will be updated automatically when needed
        
        # Serialize
        result = serialize(armature_obj)
        
        # Verify ordering
        self.assertTrue(result, "Serialization should succeed")
        self.assertIn("kfs", result, "Result should contain keyframes")
        
        keyframes = result["kfs"]
        self.assertGreater(len(keyframes), 0, "Should have keyframes")
        
        # Extract times and verify they're ordered
        times = [kf["t"] for kf in keyframes]
        self.assertEqual(times, sorted(times), "Keyframes should be ordered by time")
        
        # Verify no duplicate times
        self.assertEqual(len(times), len(set(times)), "Should have no duplicate times")


    def test_bezier_curve_is_fully_baked(self):
        """
        Tests that a BEZIER interpolation curve is baked on every frame
        between its keyframes, ensuring a lossless result.
        """
        # --- SETUP ---
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.data.edit_bones.new("Bone").head = (0, 0, 0); armature_obj.data.edit_bones[-1].tail = (0, 1, 0)
        bpy.ops.object.mode_set(mode='POSE')
        pbone = armature_obj.pose.bones["Bone"]
        
        action = bpy.data.actions.new("BezierTestAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action
        
        # Create a curved bezier animation that deviates from linear
        pbone.location = (0, 0, 0)
        pbone.keyframe_insert(data_path="location", frame=1)
        pbone.location = (10, 0, 0)
        pbone.keyframe_insert(data_path="location", frame=10)

        # Set the interpolation for the first keyframe to BEZIER
        from ..core.utils import get_action_fcurves
        fcurves = get_action_fcurves(action)
        fcurve = fcurves.find('pose.bones["Bone"].location', index=0)
        self.assertIsNotNone(fcurve, "F-curve for bone location not found.")
        fcurve.keyframe_points[0].interpolation = 'BEZIER'
        
        # Modify the bezier handles to create a curved segment
        kp = fcurve.keyframe_points[0]
        kp.handle_right_type = 'FREE'
        kp.handle_right = (3, 5)  # create a curve that goes up then down
        
        # Set scene frame range
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 10

        # --- EXECUTION ---
        result = serialize(armature_obj)
        
        # --- ASSERTION ---
        # The bezier curve is between frame 1 and 10.
        # This means we expect 10 frames of data (1, 2, 3, 4, 5, 6, 7, 8, 9, 10).
        self.assertIn("kfs", result, "Result should have keyframes.")
        self.assertEqual(len(result["kfs"]), 10, "Expected 10 baked keyframes for the 10-frame bezier segment.")
        
        # Check that the bone is present in all keyframes
        for kf in result["kfs"]:
            self.assertIn("Bone", kf["kf"], "Bone data should be present in every keyframe of a bezier bake.")


    def test_cyclic_animation_extends_to_scene_end(self):
        """cyclic modifiers should cause baking to continue sparsely up to the scene's frame_end."""
        self.clear_scene_property()

        # --- SETUP ---
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.data.edit_bones.new("Bone").head = (0, 0, 0); armature_obj.data.edit_bones[-1].tail = (0, 1, 0)

        bpy.ops.object.mode_set(mode='POSE')
        pbone = armature_obj.pose.bones["Bone"]

        action = bpy.data.actions.new("CyclicAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        # Animate a short range
        pbone.location = (0, 0, 0)
        pbone.keyframe_insert(data_path="location", frame=1)
        pbone.location = (0, 2, 0)
        pbone.keyframe_insert(data_path="location", frame=5)

        # Make sure interpolation is linear for predictable values
        self.set_action_interpolation(action, 'LINEAR')

        # Add a cyclic modifier so the motion repeats beyond the last keyframe
        from ..core.utils import get_action_fcurves
        fcurves = get_action_fcurves(action)
        fcurve = fcurves.find('pose.bones["Bone"].location', index=1)
        self.assertIsNotNone(fcurve, "expected Y location fcurve to exist")
        fcurve.modifiers.new(type='CYCLES')

        scene = bpy.context.scene
        original_fps = scene.render.fps
        original_full_range = getattr(getattr(scene, "rbx_anim_settings", None), "rbx_full_range_bake", True)
        try:
            scene.render.fps = 24
            scene.frame_start = 1
            scene.frame_end = 20

            settings = getattr(scene, "rbx_anim_settings", None)
            if settings:
                settings.rbx_full_range_bake = False  # ensure cycles override sparse bake preference

            bpy.context.view_layer.update()
            result = serialize(armature_obj)

            self.assertIn("kfs", result)
            desired_fps = scene.render.fps / scene.render.fps_base
            baked_frames = {scene.frame_start + int(round(kf["t"] * desired_fps)) for kf in result["kfs"]}

            expected_frames = {1, 5, 9, 13, 17, 20}
            self.assertSetEqual(baked_frames, expected_frames)

            last_frame = max(baked_frames)
            self.assertEqual(last_frame, scene.frame_end, "cyclic animation should extend baking to scene end")
        finally:
            scene.render.fps = original_fps
            if settings:
                settings.rbx_full_range_bake = original_full_range


    def test_non_cyclic_holds_last_pose_when_full_range_disabled(self):
        """without cyclic modifiers and full-range disabled, bake only sparse keys plus a held final pose."""
        self.clear_scene_property()

        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.data.edit_bones.new("Bone").head = (0, 0, 0); armature_obj.data.edit_bones[-1].tail = (0, 1, 0)

        bpy.ops.object.mode_set(mode='POSE')
        pbone = armature_obj.pose.bones["Bone"]

        action = bpy.data.actions.new("NonCyclicAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        pbone.location = (0, 0, 0)
        pbone.keyframe_insert(data_path="location", frame=1)
        pbone.location = (0, 3, 0)
        pbone.keyframe_insert(data_path="location", frame=5)

        self.set_action_interpolation(action, 'LINEAR')

        scene = bpy.context.scene
        original_fps = scene.render.fps
        original_full_range = getattr(getattr(scene, "rbx_anim_settings", None), "rbx_full_range_bake", True)
        try:
            scene.render.fps = 24
            scene.frame_start = 1
            scene.frame_end = 20

            settings = getattr(scene, "rbx_anim_settings", None)
            if settings:
                settings.rbx_full_range_bake = False

            bpy.context.view_layer.update()
            result = serialize(armature_obj)

            self.assertIn("kfs", result)
            desired_fps = scene.render.fps / scene.render.fps_base
            baked_frames = [scene.frame_start + int(round(kf["t"] * desired_fps)) for kf in result["kfs"]]
            self.assertEqual(baked_frames, [1, 5, 20])

            last_pose = result["kfs"][-1]["kf"].get("Bone")
            self.assertIsNotNone(last_pose, "Bone should be present in the held final pose")
            self.assertAlmostEqual(last_pose[0][1], 3.0, places=4, msg="Final pose should hold the last keyed value")
        finally:
            scene.render.fps = original_fps
            if settings:
                settings.rbx_full_range_bake = original_full_range


    def test_cyclic_multiple_channels_union(self):
        """when multiple cycle-enabled fcurves have different key timings, export should union their offsets."""
        self.clear_scene_property()

        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.data.edit_bones.new("Bone").head = (0, 0, 0); armature_obj.data.edit_bones[-1].tail = (0, 1, 0)

        bpy.ops.object.mode_set(mode='POSE')
        pbone = armature_obj.pose.bones["Bone"]

        action = bpy.data.actions.new("CyclicUnionAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        # keyframes staggered across axes
        pbone.location = (1, 0, 0)
        pbone.keyframe_insert(data_path="location", frame=1)

        pbone.location = (1, 1, 0)
        pbone.keyframe_insert(data_path="location", frame=2)

        pbone.location = (2, 1, 0)
        pbone.keyframe_insert(data_path="location", frame=5)

        pbone.location = (2, 2, 0)
        pbone.keyframe_insert(data_path="location", frame=6)

        self.set_action_interpolation(action, 'LINEAR')

        from ..core.utils import get_action_fcurves
        fcurves = get_action_fcurves(action)
        self.assertIsNotNone(fcurves.find('pose.bones["Bone"].location', index=0))
        self.assertIsNotNone(fcurves.find('pose.bones["Bone"].location', index=1))
        fcurves.find('pose.bones["Bone"].location', index=0).modifiers.new(type='CYCLES')
        fcurves.find('pose.bones["Bone"].location', index=1).modifiers.new(type='CYCLES')

        scene = bpy.context.scene
        original_fps = scene.render.fps
        original_full_range = getattr(getattr(scene, "rbx_anim_settings", None), "rbx_full_range_bake", True)
        frame_step_original = scene.frame_step
        try:
            scene.render.fps = 24
            scene.frame_start = 1
            scene.frame_end = 20
            scene.frame_step = 1

            settings = getattr(scene, "rbx_anim_settings", None)
            if settings:
                settings.rbx_full_range_bake = False

            bpy.context.view_layer.update()
            result = serialize(armature_obj)

            self.assertIn("kfs", result)
            desired_fps = scene.render.fps / scene.render.fps_base
            baked_frames = {scene.frame_start + int(round(kf["t"] * desired_fps)) for kf in result["kfs"]}

            expected_frames = {1, 2, 5, 6, 7, 10, 11, 12, 15, 16, 17, 20}
            self.assertSetEqual(baked_frames, expected_frames)

            final_frame = max(baked_frames)
            self.assertEqual(final_frame, scene.frame_end)

            final_data = result["kfs"][-1]["kf"].get("Bone")
            self.assertIsNotNone(final_data, "cycled bone should appear in final keyframe")
        finally:
            scene.render.fps = original_fps
            scene.frame_step = frame_step_original
            if settings:
                settings.rbx_full_range_bake = original_full_range


    def test_cyclic_before_range_does_not_emit_pre_start_frames(self):
        """cycles repeating before frame_start should not create negative-time samples."""
        self.clear_scene_property()

        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.data.edit_bones.new("Bone").head = (0, 0, 0); armature_obj.data.edit_bones[-1].tail = (0, 1, 0)

        bpy.ops.object.mode_set(mode='POSE')
        pbone = armature_obj.pose.bones["Bone"]

        action = bpy.data.actions.new("CyclicBeforeAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        pbone.location = (0, 0, 0)
        pbone.keyframe_insert(data_path="location", frame=10)
        pbone.location = (0, 4, 0)
        pbone.keyframe_insert(data_path="location", frame=14)

        self.set_action_interpolation(action, 'LINEAR')

        from ..core.utils import get_action_fcurves
        fcurves = get_action_fcurves(action)
        fcurve_y = fcurves.find('pose.bones["Bone"].location', index=1)
        self.assertIsNotNone(fcurve_y)
        cycles_mod = fcurve_y.modifiers.new(type='CYCLES')
        cycles_mod.mode_before = 'REPEAT'

        scene = bpy.context.scene
        original_fps = scene.render.fps
        original_full_range = getattr(getattr(scene, "rbx_anim_settings", None), "rbx_full_range_bake", True)
        frame_step_original = scene.frame_step
        try:
            scene.render.fps = 24
            scene.frame_start = 5
            scene.frame_end = 25
            scene.frame_step = 1

            settings = getattr(scene, "rbx_anim_settings", None)
            if settings:
                settings.rbx_full_range_bake = False

            bpy.context.view_layer.update()
            result = serialize(armature_obj)

            desired_fps = scene.render.fps / scene.render.fps_base
            baked_frames = [scene.frame_start + int(round(kf["t"] * desired_fps)) for kf in result["kfs"]]

            self.assertGreaterEqual(min(baked_frames), scene.frame_start, "no frames before frame_start should be emitted")
            self.assertIn(scene.frame_end, baked_frames)
        finally:
            scene.render.fps = original_fps
            scene.frame_step = frame_step_original
            if settings:
                settings.rbx_full_range_bake = original_full_range


    def test_cyclic_respects_frame_step_setting(self):
        """even with frame_step > 1, cyclic export should cover scene end and replicate sparse keys."""
        self.clear_scene_property()

        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.data.edit_bones.new("Bone").head = (0, 0, 0); armature_obj.data.edit_bones[-1].tail = (0, 1, 0)

        bpy.ops.object.mode_set(mode='POSE')
        pbone = armature_obj.pose.bones["Bone"]

        action = bpy.data.actions.new("CyclicFrameStepAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        pbone.location = (0, 0, 0)
        pbone.keyframe_insert(data_path="location", frame=1)
        pbone.location = (0, 5, 0)
        pbone.keyframe_insert(data_path="location", frame=4)

        self.set_action_interpolation(action, 'LINEAR')

        from ..core.utils import get_action_fcurves
        fcurve_y = get_action_fcurves(action).find('pose.bones["Bone"].location', index=1)
        self.assertIsNotNone(fcurve_y)
        fcurve_y.modifiers.new(type='CYCLES')

        scene = bpy.context.scene
        original_fps = scene.render.fps
        original_full_range = getattr(getattr(scene, "rbx_anim_settings", None), "rbx_full_range_bake", True)
        frame_step_original = scene.frame_step
        try:
            scene.render.fps = 24
            scene.frame_start = 1
            scene.frame_end = 20
            scene.frame_step = 3

            settings = getattr(scene, "rbx_anim_settings", None)
            if settings:
                settings.rbx_full_range_bake = False

            bpy.context.view_layer.update()
            result = serialize(armature_obj)

            desired_fps = scene.render.fps / scene.render.fps_base
            baked_frames = {scene.frame_start + int(round(kf["t"] * desired_fps)) for kf in result["kfs"]}

            expected_frames = {1, 4, 7, 10, 13, 16, 19, 20}
            self.assertSetEqual(baked_frames, expected_frames)
            self.assertEqual(max(baked_frames), scene.frame_end)
        finally:
            scene.render.fps = original_fps
            scene.frame_step = frame_step_original
            if settings:
                settings.rbx_full_range_bake = original_full_range


    def test_deform_vs_new_bone_space_conversion(self):
        """
        verifies deform bones apply roblox swizzles/scaling, while new/helper bones do not.
        - deform bone expected loc = (-x/scale, y/scale, -z/scale)
        - new/helper bone expected loc ≈ (-x, y, -z)
        """
        # --- SETUP ---
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "DeformVsNewRig"
        arm = armature_obj.data
        arm.name = "DeformVsNewArmature"

        # create two root bones: one deform, one helper (non-deform)
        deform_b = arm.edit_bones.new("DeformBone")
        deform_b.head = (0, 0, 0)
        deform_b.tail = (0, 1, 0)

        helper_b = arm.edit_bones.new("HelperBone")
        helper_b.head = (1, 0, 0)
        helper_b.tail = (1, 1, 0)

        bpy.ops.object.mode_set(mode='POSE')
        p_deform = armature_obj.pose.bones["DeformBone"]
        p_helper = armature_obj.pose.bones["HelperBone"]

        # ensure helper bone is non-deform
        p_helper.bone.use_deform = False
        p_deform.bone.use_deform = True

        # create a mesh bound only to the deform bone so rig is treated as skinned
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.mesh.primitive_cube_add(location=(0, 0.5, 0))
        mesh_obj = bpy.context.object
        mesh_obj.name = "DeformVsNewMesh"
        modifier = mesh_obj.modifiers.new(name="Armature", type='ARMATURE')
        modifier.object = armature_obj

        # build vertex group for deform bone only
        vg = mesh_obj.vertex_groups.new(name="DeformBone")
        all_indices = list(range(len(mesh_obj.data.vertices)))
        vg.add(all_indices, 1.0, 'REPLACE')

        # assign armature as parent without auto weights (we already set group)
        mesh_obj.parent = armature_obj

        # reselect armature before returning to pose mode
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.context.view_layer.objects.active = armature_obj
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        armature_obj.select_set(True)
        bpy.ops.object.mode_set(mode='POSE')

        # animate both with identical translations
        action = bpy.data.actions.new("DeformVsNewAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 10

        for bone in (p_deform, p_helper):
            bone.location = (0, 0, 0)
            bone.keyframe_insert(data_path="location", frame=1)
            bone.location = (2, 3, 4)
            bone.keyframe_insert(data_path="location", frame=10)

        # set deform export scale
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        if settings:
            settings.rbx_deform_rig_scale = 0.1

        # --- EXECUTION ---
        result = serialize(armature_obj)

        # --- ASSERTION ---
        self.assertIn("kfs", result)
        # With full-range bake defaulting to True, expect all frames
        expected_frames = bpy.context.scene.frame_end - bpy.context.scene.frame_start + 1
        self.assertEqual(len(result["kfs"]), expected_frames)
        self.assertTrue(result.get("is_deform_bone_rig"), "Skinned rig should be flagged as deform")

        last_kf = result["kfs"][-1]["kf"]
        self.assertIn("DeformBone", last_kf)
        self.assertIn("HelperBone", last_kf)

        deform_cframe = last_kf["DeformBone"][0]
        helper_cframe = last_kf["HelperBone"][0]

        # deform expected: (-20, 30, -40)
        self.assertAlmostEqual(deform_cframe[0], -20.0, places=4)
        self.assertAlmostEqual(deform_cframe[1], 30.0, places=4)
        self.assertAlmostEqual(deform_cframe[2], -40.0, places=4)

        # helper/new expected ~ (-2, 3, -4) (no scale applied, same swizzle)
        self.assertAlmostEqual(helper_cframe[0], -2.0, places=4)
        self.assertAlmostEqual(helper_cframe[1], 3.0, places=4)
        self.assertAlmostEqual(helper_cframe[2], -4.0, places=4)


    def test_motor_rig_with_helper_new_bone(self):
        """motor rigs with helper new bones should export helper in motor space without deform scaling and not flag as deform."""
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = "MotorHelperRig"
        arm = armature_obj.data
        arm.name = "MotorHelperArmature"

        motor_edit = arm.edit_bones.new("MotorRoot")
        motor_edit.head = (0, 0, 0)
        motor_edit.tail = (0, 1, 0)

        helper_edit = arm.edit_bones.new("HelperChild")
        helper_edit.head = (0, 1, 0)
        helper_edit.tail = (0, 2, 0)
        helper_edit.parent = motor_edit

        bpy.ops.object.mode_set(mode='POSE')
        motor_pose = armature_obj.pose.bones["MotorRoot"]
        helper_pose = armature_obj.pose.bones["HelperChild"]

        # mark motor bone with motor6d properties
        motor_pose.bone["is_transformable"] = True
        motor_pose.bone["transform"] = mathutils.Matrix.Identity(4)
        motor_pose.bone["transform0"] = mathutils.Matrix.Identity(4)
        motor_pose.bone["transform1"] = mathutils.Matrix.Identity(4)
        motor_pose.bone["nicetransform"] = mathutils.Matrix.Identity(4)

        # helper bone intentionally lacks motor props and is non-deform
        helper_pose.bone.use_deform = False

        # animate helper only
        action = bpy.data.actions.new("MotorHelperAction")
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        helper_pose.location = (0, 0, 0)
        helper_pose.keyframe_insert(data_path="location", frame=1)
        helper_pose.location = (1.5, 2.5, -3.5)
        helper_pose.keyframe_insert(data_path="location", frame=10)

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 10

        result = serialize(armature_obj)

        self.assertFalse(result.get("is_deform_bone_rig", False), "Motor rig with helper should not be marked as deform")
        # With full-range bake defaulting to True, expect all frames
        expected_frames = bpy.context.scene.frame_end - bpy.context.scene.frame_start + 1
        self.assertEqual(len(result["kfs"]), expected_frames)

        helper_cframe = result["kfs"][-1]["kf"].get("HelperChild")
        self.assertIsNotNone(helper_cframe, "Helper child data missing from export")
        helper_loc = helper_cframe[0][:3]
        self.assertAlmostEqual(helper_loc[0], -1.5, places=4)
        self.assertAlmostEqual(helper_loc[1], 2.5, places=4)
        self.assertAlmostEqual(helper_loc[2], 3.5, places=4)

# This allows running the tests from the Blender text editor
if __name__ == "__main__":
    suite = unittest.TestSuite()
    suite.addTest(unittest.TestLoader().loadTestsFromTestCase(TestAnimationSerialization))
    unittest.TextTestRunner().run(suite)
