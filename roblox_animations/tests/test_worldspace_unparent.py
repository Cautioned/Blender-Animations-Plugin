import bpy
import unittest
import mathutils
import math
import json
import importlib

from ..operators import rig_ops
from ..core import utils
from ..core.utils import (
    get_action_fcurves,
    invalidate_armature_cache,
    pose_bone_set_selected,
)
from ..operators.rig_ops import (
    _get_action_frame_range,
    _has_any_keys,
    _sample_world_matrices,
    _rotation_data_path,
    _clear_bone_fcurves,
    _snapshot_bone_fcurves,
    _store_fcurve_snapshot,
    _fcurves_match_snapshot,
    _restore_fcurve_snapshot,
    _decimate_single_fcurve,
)

importlib.reload(utils)
importlib.reload(rig_ops)


class TestWorldSpaceUnparent(unittest.TestCase):
    """Tests for the world-space unparent/reparent system."""

    def setUp(self):
        """Set up a clean scene with a simple bone hierarchy before each test."""
        self._prev_keyframe_interp = (
            bpy.context.preferences.edit.keyframe_new_interpolation_type
        )
        bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"

        # nuke everything
        for action in bpy.data.actions:
            bpy.data.actions.remove(action)
        for armature in bpy.data.armatures:
            bpy.data.armatures.remove(armature)
        for mesh in bpy.data.meshes:
            bpy.data.meshes.remove(mesh)
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

        bpy.context.view_layer.update()
        invalidate_armature_cache()

        self.armature_obj = None
        self.create_test_rig()

    def tearDown(self):
        if hasattr(self, "_prev_keyframe_interp"):
            bpy.context.preferences.edit.keyframe_new_interpolation_type = (
                self._prev_keyframe_interp
            )
        try:
            if self.armature_obj and self.armature_obj.name in bpy.data.objects:
                d = self.armature_obj.data
                bpy.data.objects.remove(self.armature_obj, do_unlink=True)
                if d and d.name in bpy.data.armatures:
                    bpy.data.armatures.remove(d, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def create_test_rig(self):
        """Create a simple 3-bone chain: Root -> Torso -> Leg.

        Root stays still. Torso rotates. Leg is the typical unparent target.
        Also adds Motor6D-style custom properties so the serialiser won't choke.
        """
        bpy.ops.object.add(type="ARMATURE", enter_editmode=True, location=(0, 0, 0))
        self.armature_obj = bpy.context.object
        self.armature_obj.name = "TestWSRig"
        amt = self.armature_obj.data
        amt.name = "TestWSRig"

        root = amt.edit_bones.new("Root")
        root.head = (0, 0, 0)
        root.tail = (0, 0, 1)

        torso = amt.edit_bones.new("Torso")
        torso.head = (0, 0, 1)
        torso.tail = (0, 0, 2)
        torso.parent = root

        leg = amt.edit_bones.new("Leg")
        leg.head = (0, 0, 1)
        leg.tail = (0, 0, 0.5)
        leg.parent = torso

        # optional second child
        arm = amt.edit_bones.new("Arm")
        arm.head = (0, 0, 2)
        arm.tail = (1, 0, 2)
        arm.parent = torso

        bpy.ops.object.mode_set(mode="POSE")

        # stamp Motor6D props (identity) so downstream serialisation works
        for bone in self.armature_obj.pose.bones:
            bone.bone["transform"] = mathutils.Matrix.Identity(4)
            bone.bone["transform0"] = mathutils.Matrix.Identity(4)
            bone.bone["transform1"] = mathutils.Matrix.Identity(4)
            bone.bone["nicetransform"] = mathutils.Matrix.Identity(4)
            if bone.name != "Root":
                bone.bone["is_transformable"] = True

        # keyframe Torso rotating 90° around X
        torso_pb = self.armature_obj.pose.bones["Torso"]
        torso_pb.rotation_quaternion = (1, 0, 0, 0)
        torso_pb.keyframe_insert(data_path="rotation_quaternion", frame=1)
        torso_pb.rotation_quaternion = (0.707, 0.707, 0, 0)
        torso_pb.keyframe_insert(data_path="rotation_quaternion", frame=20)

        # keyframe Leg rotating a bit
        leg_pb = self.armature_obj.pose.bones["Leg"]
        leg_pb.rotation_quaternion = (1, 0, 0, 0)
        leg_pb.keyframe_insert(data_path="rotation_quaternion", frame=1)
        leg_pb.rotation_quaternion = (0.924, 0, 0.383, 0)  # ~45° around Z
        leg_pb.keyframe_insert(data_path="rotation_quaternion", frame=20)

        # keyframe Arm with location
        arm_pb = self.armature_obj.pose.bones["Arm"]
        arm_pb.location = (0, 0, 0)
        arm_pb.keyframe_insert(data_path="location", frame=1)
        arm_pb.location = (0.5, 0, 0)
        arm_pb.keyframe_insert(data_path="location", frame=20)

        # assign the action
        if self.armature_obj.animation_data is None:
            self.armature_obj.animation_data_create()
        self.armature_obj.animation_data.action = bpy.data.actions[-1]

        action = self.armature_obj.animation_data.action
        fcurves = get_action_fcurves(action)
        if fcurves:
            for fc in fcurves:
                for kp in fc.keyframe_points:
                    kp.interpolation = "LINEAR"

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = 20
        bpy.context.view_layer.update()
        invalidate_armature_cache()

    def select_bones(self, *names):
        """Clear selection and select the named pose bones."""
        ao = self.armature_obj
        if ao.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")
        for pb in ao.pose.bones:
            pose_bone_set_selected(pb, pb.name in names)
            if pb.name in names:
                ao.data.bones.active = pb.bone

    def world_pos_at_frame(self, bone_name, frame):
        """Get a bone's world-space head position at a given frame."""
        scene = bpy.context.scene
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        pb = self.armature_obj.pose.bones[bone_name]
        return pb.matrix.to_translation().copy()

    def world_rot_at_frame(self, bone_name, frame):
        """Get a bone's world-space rotation (quaternion) at a given frame."""
        scene = bpy.context.scene
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        pb = self.armature_obj.pose.bones[bone_name]
        return pb.matrix.to_quaternion().copy()

    def count_bone_fcurves(self, bone_name):
        """Count how many fcurves exist for a given bone."""
        action = self.armature_obj.animation_data.action
        fcurves = get_action_fcurves(action)
        esc = bpy.utils.escape_identifier(bone_name)
        return sum(
            1 for fc in fcurves
            if getattr(fc, "data_path", "").startswith(f'pose.bones["{esc}"]')
        )

    def total_key_count(self, bone_name):
        """Total keyframe points across all fcurves for a bone."""
        action = self.armature_obj.animation_data.action
        fcurves = get_action_fcurves(action)
        esc = bpy.utils.escape_identifier(bone_name)
        count = 0
        for fc in fcurves:
            if getattr(fc, "data_path", "").startswith(f'pose.bones["{esc}"]'):
                count += len(fc.keyframe_points)
        return count

    # ------------------------------------------------------------------
    # helper function tests
    # ------------------------------------------------------------------

    def test_get_action_frame_range(self):
        """_get_action_frame_range returns the action range or scene fallback."""
        ao = self.armature_obj
        start, end = _get_action_frame_range(ao)
        self.assertEqual(start, 1)
        self.assertEqual(end, 20)

    def test_has_any_keys(self):
        """_has_any_keys correctly detects keyed vs unkeyed bones."""
        ao = self.armature_obj
        self.assertTrue(_has_any_keys(ao, ["Leg"]))
        self.assertTrue(_has_any_keys(ao, ["Torso"]))
        self.assertFalse(_has_any_keys(ao, ["Root"]))

    def test_rotation_data_path_quaternion(self):
        """Quaternion mode returns the right data path."""
        pb = self.armature_obj.pose.bones["Leg"]
        pb.rotation_mode = "QUATERNION"
        self.assertEqual(_rotation_data_path(pb), "rotation_quaternion")

    def test_rotation_data_path_euler(self):
        """Euler mode returns the right data path."""
        pb = self.armature_obj.pose.bones["Leg"]
        pb.rotation_mode = "XYZ"
        self.assertEqual(_rotation_data_path(pb), "rotation_euler")

    def test_clear_bone_fcurves(self):
        """_clear_bone_fcurves removes all curves for a bone."""
        ao = self.armature_obj
        self.assertGreater(self.count_bone_fcurves("Leg"), 0)
        _clear_bone_fcurves(ao, {"Leg"})
        self.assertEqual(self.count_bone_fcurves("Leg"), 0)
        # torso should be untouched
        self.assertGreater(self.count_bone_fcurves("Torso"), 0)

    def test_snapshot_roundtrip(self):
        """Snapshotting and restoring fcurves is lossless."""
        ao = self.armature_obj
        snap = _snapshot_bone_fcurves(ao, ["Leg", "Torso"])
        self.assertIn("Leg", snap)
        self.assertIn("Torso", snap)

        # mutilate
        _clear_bone_fcurves(ao, {"Leg", "Torso"})
        self.assertEqual(self.count_bone_fcurves("Leg"), 0)

        # restore
        _store_fcurve_snapshot(ao, snap)
        _restore_fcurve_snapshot(ao, ["Leg", "Torso"])

        # verify keyframes came back
        self.assertGreater(self.count_bone_fcurves("Leg"), 0)
        self.assertGreater(self.count_bone_fcurves("Torso"), 0)

    def test_fcurves_match_snapshot_detects_no_change(self):
        """Snapshot match returns True when nothing has changed."""
        ao = self.armature_obj
        snap = _snapshot_bone_fcurves(ao, ["Leg"])
        _store_fcurve_snapshot(ao, snap, prop_name="test_snap")
        self.assertTrue(_fcurves_match_snapshot(ao, "Leg", "test_snap"))

    def test_fcurves_match_snapshot_detects_edit(self):
        """Snapshot match returns False after modifying a keyframe value."""
        ao = self.armature_obj
        snap = _snapshot_bone_fcurves(ao, ["Leg"])
        _store_fcurve_snapshot(ao, snap, prop_name="test_snap")

        # tweak a keyframe
        action = ao.animation_data.action
        fcurves = get_action_fcurves(action)
        esc = bpy.utils.escape_identifier("Leg")
        for fc in fcurves:
            if getattr(fc, "data_path", "").startswith(f'pose.bones["{esc}"]'):
                fc.keyframe_points[0].co.y += 1.0
                fc.update()
                break

        self.assertFalse(_fcurves_match_snapshot(ao, "Leg", "test_snap"))

    # ------------------------------------------------------------------
    # decimation tests
    # ------------------------------------------------------------------

    def test_decimate_single_fcurve_linear(self):
        """Decimation removes interior keys on a perfectly linear segment."""
        action = self.armature_obj.animation_data.action
        fcurves = get_action_fcurves(action)
        # create a fresh fcurve with many redundant linear keys
        dp = 'pose.bones["Root"].location'
        fc = fcurves.new(dp, index=0)
        for f in range(1, 21):
            kp = fc.keyframe_points.insert(float(f), float(f))
            kp.interpolation = "LINEAR"

        self.assertEqual(len(fc.keyframe_points), 20)
        _decimate_single_fcurve(fc, 0.001)
        # perfectly linear → should be reduced to just the two endpoints
        self.assertEqual(len(fc.keyframe_points), 2)

    def test_decimate_preserves_nonlinear(self):
        """Decimation keeps keys that contribute real curvature."""
        action = self.armature_obj.animation_data.action
        fcurves = get_action_fcurves(action)
        dp = 'pose.bones["Root"].scale'
        fc = fcurves.new(dp, index=0)
        # sine-ish curve — internal keys are NOT redundant
        for f in range(1, 21):
            val = math.sin(f * 0.5) * 2.0
            kp = fc.keyframe_points.insert(float(f), val)
            kp.interpolation = "LINEAR"

        before = len(fc.keyframe_points)
        _decimate_single_fcurve(fc, 0.001)
        after = len(fc.keyframe_points)
        # should keep most keys bc the curve isn't linear
        self.assertGreater(after, 2)
        # but might remove a few near-inflection points
        self.assertLessEqual(after, before)

    # ------------------------------------------------------------------
    # unparent operator tests
    # ------------------------------------------------------------------

    def test_unparent_sets_custom_props(self):
        """Unparent marks the bone with worldspace metadata."""
        ao = self.armature_obj
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        data_bone = ao.data.bones["Leg"]
        self.assertTrue(data_bone.get("worldspace_bone"))
        self.assertEqual(data_bone.get("worldspace_original_parent"), "Torso")

    def test_unparent_removes_parent(self):
        """After unparent, the edit bone has no parent."""
        ao = self.armature_obj
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        bpy.ops.object.mode_set(mode="EDIT")
        eb = ao.data.edit_bones["Leg"]
        self.assertIsNone(eb.parent)
        bpy.ops.object.mode_set(mode="POSE")

    def test_unparent_preserves_world_position(self):
        """World position at every frame stays the same after unparent."""
        ao = self.armature_obj

        # record pre-unparent positions
        positions_before = {}
        for f in range(1, 21):
            positions_before[f] = self.world_pos_at_frame("Leg", f)

        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        for f in range(1, 21):
            pos = self.world_pos_at_frame("Leg", f)
            for i in range(3):
                self.assertAlmostEqual(
                    pos[i], positions_before[f][i], places=3,
                    msg=f"frame {f} axis {i}: {pos[i]} != {positions_before[f][i]}"
                )

    def test_unparent_stores_original_fcurves(self):
        """Original fcurves snapshot is stored for lossless restore."""
        ao = self.armature_obj
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        raw = ao.data.bones["Leg"].get("worldspace_original_fcurves")
        self.assertIsNotNone(raw)
        curves = json.loads(raw)
        self.assertIsInstance(curves, list)
        self.assertGreater(len(curves), 0)

    def test_unparent_stores_baked_snapshot(self):
        """Baked world-space fcurves snapshot is stored for edit detection."""
        ao = self.armature_obj
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        raw = ao.data.bones["Leg"].get("worldspace_baked_fcurves")
        self.assertIsNotNone(raw)

    def test_unparent_poll_rejects_root(self):
        """Root bone has no parent, so poll should exclude it."""
        self.select_bones("Root")
        # unparent all, but Root has no parent → should not succeed
        # (poll returns False → operator not available)
        # we can test poll directly
        result = bpy.ops.object.rbxanims_worldspace_unparent.poll()
        self.assertFalse(result)

    def test_unparent_poll_rejects_already_unparented(self):
        """Bone already marked worldspace shouldn't be eligible again."""
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        # try again with same bone selected
        self.select_bones("Leg")
        result = bpy.ops.object.rbxanims_worldspace_unparent.poll()
        self.assertFalse(result)

    # ------------------------------------------------------------------
    # reparent operator tests
    # ------------------------------------------------------------------

    def test_reparent_restores_parent(self):
        """After reparent, the bone is back under its original parent."""
        ao = self.armature_obj
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_reparent()

        bpy.ops.object.mode_set(mode="EDIT")
        eb = ao.data.edit_bones["Leg"]
        self.assertIsNotNone(eb.parent)
        self.assertEqual(eb.parent.name, "Torso")
        bpy.ops.object.mode_set(mode="POSE")

    def test_reparent_clears_custom_props(self):
        """Reparent removes all worldspace metadata from the bone."""
        ao = self.armature_obj
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_reparent()

        data_bone = ao.data.bones["Leg"]
        self.assertIsNone(data_bone.get("worldspace_bone"))
        self.assertIsNone(data_bone.get("worldspace_original_parent"))
        self.assertIsNone(data_bone.get("worldspace_original_fcurves"))
        self.assertIsNone(data_bone.get("worldspace_baked_fcurves"))

    def test_lossless_roundtrip_no_edits(self):
        """Unparent then reparent with no edits restores exact original fcurves."""
        ao = self.armature_obj
        # snapshot original fcurves for comparison
        original_snap = _snapshot_bone_fcurves(ao, ["Leg"])

        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_reparent()

        after_snap = _snapshot_bone_fcurves(ao, ["Leg"])
        # same number of curves
        self.assertEqual(len(original_snap["Leg"]), len(after_snap["Leg"]))
        # same keyframe counts and values per curve
        for orig_c, after_c in zip(original_snap["Leg"], after_snap["Leg"]):
            self.assertEqual(
                len(orig_c["keyframes"]), len(after_c["keyframes"]),
                f"keyframe count mismatch on {orig_c['data_path']}[{orig_c['array_index']}]"
            )
            for ok, ak in zip(orig_c["keyframes"], after_c["keyframes"]):
                self.assertAlmostEqual(ok["co"][0], ak["co"][0], places=4)
                self.assertAlmostEqual(ok["co"][1], ak["co"][1], places=4)

    def test_reparent_preserves_world_position(self):
        """World position stays the same after reparent (for untouched bones)."""
        positions_before = {}
        for f in range(1, 21):
            positions_before[f] = self.world_pos_at_frame("Leg", f)

        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_reparent()

        for f in range(1, 21):
            pos = self.world_pos_at_frame("Leg", f)
            for i in range(3):
                self.assertAlmostEqual(
                    pos[i], positions_before[f][i], places=3,
                    msg=f"frame {f} axis {i}"
                )

    # ------------------------------------------------------------------
    # edit detection tests
    # ------------------------------------------------------------------

    def test_edited_bone_gets_baked(self):
        """If user edits keys while unparented, reparent should bake (not restore)."""
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        # edit: move the leg at frame 10
        bpy.context.scene.frame_set(10)
        pb = self.armature_obj.pose.bones["Leg"]
        pb.location = (2, 0, 0)
        pb.keyframe_insert(data_path="location", frame=10)

        new_pos = self.world_pos_at_frame("Leg", 10)

        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_reparent()

        restored_pos = self.world_pos_at_frame("Leg", 10)
        for i in range(3):
            self.assertAlmostEqual(
                restored_pos[i], new_pos[i], places=2,
                msg=f"axis {i}: edit not preserved"
            )

    def test_mixed_edited_and_unedited(self):
        """Bones with edits get baked, bones without get lossless restore."""
        ao = self.armature_obj
        original_arm_snap = _snapshot_bone_fcurves(ao, ["Arm"])

        self.select_bones("Leg", "Arm")
        bpy.ops.object.rbxanims_worldspace_unparent()

        # edit Leg only
        bpy.context.scene.frame_set(10)
        pb = ao.pose.bones["Leg"]
        pb.location = (3, 0, 0)
        pb.keyframe_insert(data_path="location", frame=10)

        # DON'T edit Arm

        self.select_bones("Leg", "Arm")
        bpy.ops.object.rbxanims_worldspace_reparent()

        # Arm should be losslessly restored — same keyframe data as original
        after_arm_snap = _snapshot_bone_fcurves(ao, ["Arm"])
        self.assertEqual(len(original_arm_snap.get("Arm", [])), len(after_arm_snap.get("Arm", [])))
        for orig_c, after_c in zip(original_arm_snap.get("Arm", []), after_arm_snap.get("Arm", [])):
            self.assertEqual(len(orig_c["keyframes"]), len(after_c["keyframes"]))

    # ------------------------------------------------------------------
    # multiple round trips
    # ------------------------------------------------------------------

    def test_double_roundtrip(self):
        """Two full unparent → reparent cycles should still be lossless."""
        ao = self.armature_obj
        original_snap = _snapshot_bone_fcurves(ao, ["Leg"])

        for _ in range(2):
            self.select_bones("Leg")
            bpy.ops.object.rbxanims_worldspace_unparent()
            self.select_bones("Leg")
            bpy.ops.object.rbxanims_worldspace_reparent()

        after_snap = _snapshot_bone_fcurves(ao, ["Leg"])
        for orig_c, after_c in zip(original_snap["Leg"], after_snap["Leg"]):
            self.assertEqual(len(orig_c["keyframes"]), len(after_c["keyframes"]))
            for ok, ak in zip(orig_c["keyframes"], after_c["keyframes"]):
                self.assertAlmostEqual(ok["co"][1], ak["co"][1], places=4)

    # ------------------------------------------------------------------
    # bone with no keys
    # ------------------------------------------------------------------

    def test_bone_with_no_keys_gets_keyed(self):
        """A bone with no keys (animated only via parent) should gain keys on unparent."""
        ao = self.armature_obj
        # Root has no keys — but it's not a child so let's use a helper.
        # Clear Arm keys to make it parent-only animated.
        _clear_bone_fcurves(ao, {"Arm"})
        self.assertEqual(self.count_bone_fcurves("Arm"), 0)

        self.select_bones("Arm")
        bpy.ops.object.rbxanims_worldspace_unparent()

        # after unparent, Arm should have fcurves (baked from parent motion)
        self.assertGreater(
            self.count_bone_fcurves("Arm"), 0,
            "bone with no keys should gain fcurves after world-space unparent"
        )

    # ------------------------------------------------------------------
    # rotation mode tests
    # ------------------------------------------------------------------

    def test_euler_rotation_mode_respected(self):
        """If bone uses euler rotation, keys should use rotation_euler data path."""
        ao = self.armature_obj
        # switch Leg to euler before unparent
        pb = ao.pose.bones["Leg"]
        pb.rotation_mode = "XYZ"
        # re-key in euler
        _clear_bone_fcurves(ao, {"Leg"})
        pb.rotation_euler = (0, 0, 0)
        pb.keyframe_insert(data_path="rotation_euler", frame=1)
        pb.rotation_euler = (0.5, 0, 0)
        pb.keyframe_insert(data_path="rotation_euler", frame=20)

        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()

        # verify the baked fcurves use rotation_euler, not rotation_quaternion
        action = ao.animation_data.action
        fcurves = get_action_fcurves(action)
        esc = bpy.utils.escape_identifier("Leg")
        rot_paths = set()
        for fc in fcurves:
            dp = getattr(fc, "data_path", "")
            if dp.startswith(f'pose.bones["{esc}"]') and "rotation" in dp:
                rot_paths.add(dp)

        self.assertTrue(
            any("rotation_euler" in p for p in rot_paths),
            f"expected rotation_euler in data paths, got: {rot_paths}"
        )
        self.assertFalse(
            any("rotation_quaternion" in p for p in rot_paths),
            f"unexpected rotation_quaternion in data paths: {rot_paths}"
        )

    # ------------------------------------------------------------------
    # sample_world_matrices
    # ------------------------------------------------------------------

    def test_sample_world_matrices_covers_all_frames(self):
        """Sampling returns a matrix for every frame in the range."""
        ao = self.armature_obj
        mats = _sample_world_matrices(ao, ["Leg"], 1, 20)
        self.assertEqual(len(mats["Leg"]), 20)
        for f in range(1, 21):
            self.assertIn(f, mats["Leg"])
            self.assertIsInstance(mats["Leg"][f], mathutils.Matrix)


if __name__ == "__main__":
    unittest.main()
