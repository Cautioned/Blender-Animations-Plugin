"""Stepped controls must survive external helper/parent constraint chains."""
import unittest

import bpy
from mathutils import Matrix

from ..animation.planning import constant_dependency_frames
from ..animation.serialization import serialize
from ..core.utils import get_action_fcurves, get_animation_data_action_slot


class TestConstantDependencies(unittest.TestCase):
    def setUp(self):
        bpy.ops.object.mode_set(mode="OBJECT") if bpy.context.object and bpy.context.object.mode != "OBJECT" else None
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        self.rigs = []
        for name in ("Export", "Controls"):
            bpy.ops.object.armature_add(enter_editmode=True)
            rig = bpy.context.object
            rig.name = name
            root = rig.data.edit_bones[0]
            root.name = "Root"
            child = rig.data.edit_bones.new("Helper")
            child.head, child.tail, child.parent = (0, 0, 1), (0, 0, 2), root
            bpy.ops.object.mode_set(mode="OBJECT")
            for bone in rig.pose.bones:
                bone.bone["is_transformable"] = True
                for prop in ("transform", "transform0", "transform1", "nicetransform"):
                    bone.bone[prop] = Matrix.Identity(4)
            self.rigs.append(rig)
        self.export, self.controls = self.rigs
        target = self.controls.pose.bones["Root"]
        for frame, value in ((1, 0), (4.5, 2), (9, 0)):
            target.location.x = value
            target.keyframe_insert("location", frame=frame)
        animation = self.controls.animation_data
        self.curves = get_action_fcurves(animation.action, slot=get_animation_data_action_slot(animation))
        for curve in self.curves:
            for key in curve.keyframe_points:
                key.interpolation = "CONSTANT"
        output = self.export.pose.bones["Helper"]
        output.keyframe_insert("location", frame=1)
        animation = self.export.animation_data
        for curve in get_action_fcurves(animation.action, slot=get_animation_data_action_slot(animation)):
            curve.keyframe_points[0].interpolation = "BEZIER"
        constraint = output.constraints.new("COPY_TRANSFORMS")
        constraint.target, constraint.subtarget = self.controls, "Helper"
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, 17

    def test_static_setup_and_unkeyed_helper_export_constant(self):
        self.assertEqual(constant_dependency_frames(self.export, 1, 17), {1, 4.5, 9})
        result = serialize(self.export)
        poses = [key["kf"]["Helper"] for key in result["kfs"] if "Helper" in key["kf"]]
        self.assertTrue(poses)
        self.assertTrue(all(pose[1:] == ["Constant", "Out"] for pose in poses))
        fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        self.assertTrue(any(abs(key["t"] - 3.5 / fps) < 1e-6 for key in result["kfs"]))
        self.assertGreater(max(p[0][0] for p in poses) - min(p[0][0] for p in poses), 1)

    def test_cycles_expand_fractional_step_times(self):
        for curve in self.curves:
            curve.modifiers.new("CYCLES")
        frames = constant_dependency_frames(self.export, 1, 17)
        self.assertIn(12.5, frames)
        self.assertIn(17, frames)

    def test_continuous_channel_does_not_get_frozen(self):
        self.curves[0].keyframe_points[0].interpolation = "LINEAR"
        self.assertIsNone(constant_dependency_frames(self.export, 1, 17))

    def test_noise_and_drivers_are_not_assumed_constant(self):
        noise = self.curves[0].modifiers.new("NOISE")
        self.assertIsNone(constant_dependency_frames(self.export, 1, 17))
        self.curves[0].modifiers.remove(noise)
        self.controls.driver_add("location", 0).driver.expression = "frame / 10"
        self.assertIsNone(constant_dependency_frames(self.export, 1, 17))

    def test_nla_blending_is_not_assumed_constant(self):
        animation = self.controls.animation_data
        animation.nla_tracks.new().strips.new("Blend", 1, animation.action)
        self.assertIsNone(constant_dependency_frames(self.export, 1, 17))
