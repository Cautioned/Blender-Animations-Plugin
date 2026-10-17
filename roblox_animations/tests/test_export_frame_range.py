import importlib
import json
import unittest
import zlib

import bpy

from ..animation import serialization
from ..core.utils import (
    get_action_channelbag,
    get_animation_data_action_slot,
    invalidate_armature_cache,
)
from ..server import requests as server_requests

importlib.reload(serialization)


class TestExportFrameRange(unittest.TestCase):
    def setUp(self):
        for action in list(bpy.data.actions):
            bpy.data.actions.remove(action)
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for armature in list(bpy.data.armatures):
            bpy.data.armatures.remove(armature, do_unlink=True)
        bpy.context.view_layer.update()
        invalidate_armature_cache()

    def _make_armature_with_action(self, action_name, keyframes):
        bpy.ops.object.add(type="ARMATURE", enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = f"{action_name}Rig"

        bone = armature_obj.data.edit_bones.new("Root")
        bone.head = (0, 0, 0)
        bone.tail = (0, 0, 1)

        bpy.ops.object.mode_set(mode="POSE")
        pose_bone = armature_obj.pose.bones["Root"]
        pose_bone.bone["transform"] = [1, 0, 0, 0, 1, 0, 0, 0, 1]
        pose_bone.bone["transform1"] = [1, 0, 0, 0, 1, 0, 0, 0, 1]
        pose_bone.bone["nicetransform"] = [1, 0, 0, 0, 1, 0, 0, 0, 1]

        action = bpy.data.actions.new(action_name)
        armature_obj.animation_data_create()
        armature_obj.animation_data.action = action

        fcurve = self._new_action_fcurve(
            armature_obj,
            action,
            'pose.bones["Root"].location',
            0,
        )
        if hasattr(action, "slots") and action.slots:
            armature_obj.animation_data.action_slot = action.slots[0]
        fcurve.keyframe_points.add(len(keyframes))
        for point, frame in zip(fcurve.keyframe_points, keyframes):
            point.co = (float(frame), float(frame))

        return armature_obj

    def _new_action_fcurve(self, armature_obj, action, data_path, index):
        slot = get_animation_data_action_slot(
            getattr(armature_obj, "animation_data", None),
            action=action,
        )
        channelbag = get_action_channelbag(action, slot=slot)
        if channelbag and hasattr(channelbag, "fcurves"):
            return channelbag.fcurves.new(data_path, index=index)
        return action.fcurves.new(data_path=data_path, index=index)

    def _make_export_armature_without_action(self, name):
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        bpy.ops.object.add(type="ARMATURE", enter_editmode=True, location=(0, 0, 0))
        armature_obj = bpy.context.object
        armature_obj.name = name

        if "Root" not in armature_obj.data.edit_bones:
            bone = armature_obj.data.edit_bones.new("Root")
            bone.head = (0, 0, 0)
            bone.tail = (0, 0, 1)

        bpy.ops.object.mode_set(mode="POSE")
        pose_bone = armature_obj.pose.bones["Root"]
        pose_bone.bone["transform"] = [1, 0, 0, 0, 1, 0, 0, 0, 1]
        pose_bone.bone["transform1"] = [1, 0, 0, 0, 1, 0, 0, 0, 1]
        pose_bone.bone["nicetransform"] = [1, 0, 0, 0, 1, 0, 0, 0, 1]

        return armature_obj

    def _add_copy_location_constraint(self, driven_armature, target_armature):
        bpy.context.view_layer.objects.active = driven_armature
        if driven_armature.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")

        pose_bone = driven_armature.pose.bones["Root"]
        constraint = pose_bone.constraints.new(type="COPY_LOCATION")
        constraint.target = target_armature
        constraint.subtarget = "Root"
        constraint.owner_space = "WORLD"
        constraint.target_space = "WORLD"
        return constraint

    def test_export_range_sync_uses_new_action_after_file_switch(self):
        armature_obj = self._make_armature_with_action("First", (1, 44))
        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_end = 44

        new_action = bpy.data.actions.new("Second")
        armature_obj.animation_data.action = new_action
        fcurve = self._new_action_fcurve(
            armature_obj,
            new_action,
            'pose.bones["Root"].location',
            0,
        )
        fcurve.keyframe_points.add(2)
        fcurve.keyframe_points[0].co = (8.0, 0.0)
        fcurve.keyframe_points[1].co = (12.0, 1.0)

        resolved = serialization.sync_scene_frame_range_to_export_source(
            scene,
            armature_obj,
        )

        self.assertEqual(resolved, (8, 12))
        self.assertEqual(scene.frame_start, 8)
        self.assertEqual(scene.frame_end, 12)

    def test_export_range_uses_constraint_target_action_for_proxy_rig(self):
        control_armature = self._make_armature_with_action("Control", (6, 38))
        export_armature = self._make_export_armature_without_action("__Rig")
        self._add_copy_location_constraint(export_armature, control_armature)

        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_end = 120

        resolved = serialization.sync_scene_frame_range_to_export_source(
            scene,
            export_armature,
        )

        self.assertEqual(resolved, (6, 38))
        self.assertEqual(scene.frame_start, 6)
        self.assertEqual(scene.frame_end, 38)

    def test_server_export_respects_scene_frame_range_each_time(self):
        armature_obj = self._make_armature_with_action("ActionA", (1, 90))
        scene = bpy.data.scenes.get("Scene") or bpy.context.scene

        action_b = bpy.data.actions.new("ActionB")
        fcurve_b = self._new_action_fcurve(
            armature_obj,
            action_b,
            'pose.bones["Root"].location',
            0,
        )
        fcurve_b.keyframe_points.add(2)
        fcurve_b.keyframe_points[0].co = (1.0, 0.0)
        fcurve_b.keyframe_points[1].co = (45.0, 1.0)

        action_c = bpy.data.actions.new("ActionC")
        fcurve_c = self._new_action_fcurve(
            armature_obj,
            action_c,
            'pose.bones["Root"].location',
            0,
        )
        fcurve_c.keyframe_points.add(2)
        fcurve_c.keyframe_points[0].co = (1.0, 0.0)
        fcurve_c.keyframe_points[1].co = (100.0, 1.0)

        def export_with_scene_range(action, frame_end, task_id):
            scene.frame_start = 1
            scene.frame_end = frame_end
            armature_obj.animation_data.action = action
            server_requests.pending_responses.pop(task_id, None)
            server_requests.execute_in_main_thread(task_id, armature_obj.name)
            success, payload = server_requests.pending_responses.pop(task_id)
            self.assertTrue(success)
            exported = json.loads(zlib.decompress(payload).decode("utf-8"))
            return exported["export_info"]["frame_start"], exported["export_info"]["frame_end"]

        a_start, a_end = export_with_scene_range(armature_obj.animation_data.action, 90, "task_a")
        self.assertEqual((a_start, a_end), (1, 90))

        b_start, b_end = export_with_scene_range(action_b, 90, "task_b")
        self.assertEqual((b_start, b_end), (1, 90))

        c_start, c_end = export_with_scene_range(action_c, 45, "task_c")
        self.assertEqual((c_start, c_end), (1, 45))

        b2_start, b2_end = export_with_scene_range(action_b, 100, "task_b2")
        self.assertEqual((b2_start, b2_end), (1, 100))

    def test_motor_only_skin_does_not_repeat_deform_ancestry_checks(self):
        from unittest.mock import patch
        from ..animation import sampling
        armature = self._make_export_armature_without_action("MotorSkin")
        if armature.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        cache = {}
        with patch.object(sampling, "is_face_control_bone", wraps=sampling.is_face_control_bone) as classify:
            initial_checks = None
            for frame in (1, 1.25, 1.5, 2):
                bpy.context.scene.frame_set(int(frame), subframe=frame - int(frame))
                evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
                sampling.serialize_animation_state(evaluated, static_cache=cache)
                self.assertEqual(sampling.serialize_deform_animation_state(
                    evaluated, True, static_cache=cache), {})
                if initial_checks is None:
                    initial_checks = classify.call_count
                    self.assertGreater(initial_checks, 0)
                self.assertEqual(classify.call_count, initial_checks)

    def test_subset_deform_sampling_keeps_space_parents_and_source_names(self):
        from ..animation import sampling
        rig = self._make_export_armature_without_action("SubsetRig")
        bpy.ops.object.mode_set(mode="EDIT")
        parent = rig.data.edit_bones.new("Helper")
        parent.head, parent.tail = (0, 0, 1), (0, 0, 2)
        parent.parent = rig.data.edit_bones["Root"]
        child = rig.data.edit_bones.new("Leaf")
        child.head, child.tail = (0, 0, 2), (0, 0, 3)
        child.parent = parent
        bpy.ops.object.mode_set(mode="OBJECT")
        rig.data.bones["Leaf"]["rbx_source_name"] = "OriginalLeaf"
        rig.pose.bones["Helper"].rotation_mode = "XYZ"
        rig.pose.bones["Helper"].rotation_euler.z = 0.7
        rig.pose.bones["Leaf"].location.x = 0.4
        bpy.context.view_layer.update()
        evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        full = sampling.serialize_deform_animation_state(evaluated, True)
        selected = sampling.serialize_deform_animation_state(
            evaluated, True, bone_names={"OriginalLeaf"})
        self.assertEqual(selected, {"OriginalLeaf": full["OriginalLeaf"]})
        self.assertEqual(sampling.serialize_animation_state(evaluated, bone_names=set()), {})

    def test_default_export_has_linear_scene_evaluation_cost(self):
        from unittest.mock import patch
        from ..animation import visual_bake
        control = self._make_armature_with_action("FrameControl", (1, 30))
        output = self._make_export_armature_without_action("FrameOutput")
        self._add_copy_location_constraint(output, control)
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, 30
        with patch.object(visual_bake, "refine_visual_bake", side_effect=AssertionError("Unexpected adaptive bake")):
            with patch.object(serialization, "serialize_combined_animation_state",
                              wraps=serialization.serialize_combined_animation_state) as sample:
                result = serialization.serialize(output)
        self.assertEqual(result["export_info"]["sampling_mode"], "AUTO")
        self.assertNotIn("rbx_sampling_mode", bpy.context.scene.rbx_anim_settings.bl_rna.properties)
        self.assertLessEqual(sample.call_count, 60)
        self.assertLessEqual(result["export_info"]["visual_bake_evaluated_samples"], 29)

    def test_nla_export_retains_mapped_fractional_source_keys(self):
        rig = self._make_armature_with_action("FractionalClip", (1.2, 2.6))
        action = rig.animation_data.action
        rig.animation_data.action = None
        track = rig.animation_data.nla_tracks.new()
        strip = track.strips.new("FractionalClip", 4, action)
        strip.action_frame_start, strip.action_frame_end = 1, 3
        strip.scale = 2
        strip.frame_start = 4.5
        strip.frame_end = 8.5
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = 4, 9
        result = serialization.serialize(rig)
        fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        frames = [row["t"] * fps + 4 for row in result["kfs"]]
        for expected in (4.9, 7.7):
            self.assertTrue(any(abs(frame - expected) < 2e-5 for frame in frames), frames)
        from ..animation.planning import nla_source_key_times
        strip.use_reverse = True
        strip.repeat = 2
        strip.frame_end = 12.5
        mapped = nla_source_key_times(rig.animation_data, 4, 13)
        for expected in (5.3, 8.1, 9.3, 12.1):
            self.assertTrue(any(abs(frame - expected) < 2e-5 for frame in mapped), mapped)

    def test_compressed_nla_curve_detects_subframe_motion_automatically(self):
        from ..core.utils import get_action_fcurves
        from ..animation.planning import nla_source_key_times
        rig = self._make_armature_with_action("CompressedCurve", (1, 1001))
        action = rig.animation_data.action
        for curve in get_action_fcurves(action):
            for key in curve.keyframe_points:
                key.interpolation = "CUBIC"
                key.easing = "EASE_IN"
            curve.keyframe_points[0].co.y = 0
            curve.keyframe_points[-1].co.y = 1
        rig.animation_data.action = None
        strip = rig.animation_data.nla_tracks.new().strips.new("CompressedCurve", 4, action)
        strip.scale = 0.0005
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = 4, 5
        mapped = nla_source_key_times(rig.animation_data, 4, 5)
        self.assertIn(4.25, mapped)
        result = serialization.serialize(rig)
        fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        frames = [row["t"] * fps + 4 for row in result["kfs"]]
        self.assertTrue(any(4 < frame < 4.5 for frame in frames), frames)

    def test_short_direct_elastic_curve_is_automatically_sampled_and_emitted(self):
        from ..core.utils import get_action_fcurves
        rig = self._make_armature_with_action("SubframeElastic", (1.1, 1.8))
        for curve in get_action_fcurves(rig.animation_data.action):
            for key in curve.keyframe_points:
                key.interpolation = "ELASTIC"
                key.easing = "EASE_OUT"
                key.period = .3
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, 2
        result = serialization.serialize(rig)
        fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        keys = [(1 + row["t"] * fps, row["kf"]["Root"])
                for row in result["kfs"] if "Root" in row["kf"]]
        self.assertGreater(len(keys), 4)
        for frame in (1.2, 1.4, 1.6, 1.7):
            bpy.context.scene.frame_set(1, subframe=frame - 1)
            expected = serialization.serialize_animation_state(
                rig.evaluated_get(bpy.context.evaluated_depsgraph_get()))["Root"]
            for (left, a), (right, b) in zip(keys, keys[1:]):
                if left <= frame <= right:
                    alpha = 0 if a[1] == "Constant" else (frame - left) / (right - left)
                    actual = [x + alpha * (y - x) for x, y in zip(a[0][:3], b[0][:3])]
                    self.assertLess(max(abs(x - y) for x, y in zip(actual, expected[:3])), .002)
                    break
            else:
                self.fail(f"Missing exported segment around {frame}")


if __name__ == "__main__":
    unittest.main()
