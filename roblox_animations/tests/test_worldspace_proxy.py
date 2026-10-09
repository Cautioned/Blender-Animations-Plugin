"""Regression checks for proxy controls and hierarchy baking."""

import unittest
import bpy
from mathutils import Matrix
from .test_worldspace_unparent import TestWorldSpaceUnparent as Fixture
from ..operators import rig_ops
from ..core.utils import get_action_fcurves


class TestWorldSpaceProxy(unittest.TestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    create_test_rig = Fixture.create_test_rig
    select_bones = Fixture.select_bones

    def sample(self, *names):
        return rig_ops._sample_world_matrices(self.armature_obj, names, 1, 20)

    def assertMotion(self, expected):
        actual = self.sample(*expected)
        for name in expected:
            for frame, matrix in expected[name].items():
                error = max(
                    abs(matrix[r][c] - actual[name][frame][r][c])
                    for r in range(4)
                    for c in range(4)
                )
                self.assertLess(error, 0.0002, (name, frame, error))

    def test_constraint_visual_bake_and_lossless_restore(self):
        ao = self.armature_obj
        target = bpy.data.objects.new("ProxyTarget", None)
        bpy.context.scene.collection.objects.link(target)
        target.location = (0.6, 0.2, 0.1)
        constraint = ao.pose.bones["Leg"].constraints.new("COPY_LOCATION")
        constraint.target = target
        constraint.use_offset = True
        constraint.influence = 0.65
        expected = self.sample("Leg")
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        self.assertTrue(constraint.mute)
        self.assertMotion(expected)
        bpy.ops.object.rbxanims_worldspace_reparent()
        self.assertFalse(constraint.mute)
        self.assertMotion(expected)

    def test_custom_property_animation_survives(self):
        pb = self.armature_obj.pose.bones["Leg"]
        for f, v in [(1, 0), (20, 1)]:
            pb["grip"] = v
            pb.keyframe_insert(data_path='["grip"]', frame=f)
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        curves = get_action_fcurves(self.armature_obj.animation_data.action)
        self.assertIsNotNone(curves.find('pose.bones["Leg"]["grip"]'))

    def test_multi_bone_edited_reparent_preserves_full_matrices(self):
        bpy.context.preferences.edit.keyframe_new_interpolation_type = "BEZIER"
        expected = self.sample("Torso", "Leg")
        self.select_bones("Torso", "Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        self.assertMotion(expected)
        pb = self.armature_obj.pose.bones["Torso"]
        bpy.context.scene.frame_set(10)
        pb.location.x += 0.3
        pb.keyframe_insert(data_path="location", frame=10)
        pb = self.armature_obj.pose.bones["Leg"]
        pb.location.x += 0.2
        pb.keyframe_insert(data_path="location", frame=10)
        expected = self.sample("Torso", "Leg")
        bpy.ops.object.rbxanims_worldspace_reparent()
        self.assertMotion(expected)

    def test_missing_parent_keeps_recovery_metadata(self):
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        bpy.ops.object.mode_set(mode="EDIT")
        self.armature_obj.data.edit_bones.remove(
            self.armature_obj.data.edit_bones["Torso"]
        )
        bpy.ops.object.mode_set(mode="POSE")
        self.select_bones("Leg")
        self.assertEqual(bpy.ops.object.rbxanims_worldspace_reparent(), {"CANCELLED"})
        self.assertTrue(self.armature_obj.data.bones["Leg"]["worldspace_bone"])

    def test_proxy_controls_keep_export_follower_motion(self):
        control = self.armature_obj
        source = bpy.data.objects.new("ExportRig", control.data.copy())
        bpy.context.scene.collection.objects.link(source)
        bpy.context.view_layer.update()
        source.matrix_world = Matrix.Translation((2, 3, 1))
        for name in source.pose.bones.keys():
            c = source.pose.bones[name].constraints.new("COPY_TRANSFORMS")
            c.target = control
            c.subtarget = name
            c.owner_space = "WORLD"
            c.target_space = "WORLD"
        expected = rig_ops._sample_world_matrices(source, ["Leg"], 1, 20)
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        actual = rig_ops._sample_world_matrices(source, ["Leg"], 1, 20)
        for frame in expected["Leg"]:
            self.assertLess(
                max(
                    abs(expected["Leg"][frame][r][c] - actual["Leg"][frame][r][c])
                    for r in range(4)
                    for c in range(4)
                ),
                0.0002,
            )
        self.assertEqual(source.data.bones["Leg"].parent.name, "Torso")

    def test_reparent_after_parent_animation_edit(self):
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        pb = self.armature_obj.pose.bones["Torso"]
        bpy.context.scene.frame_set(10)
        pb.location.x = 2
        pb.keyframe_insert(data_path="location", frame=10)
        expected = self.sample("Leg")
        bpy.ops.object.rbxanims_worldspace_reparent()
        self.assertMotion(expected)

    def test_no_keys_and_nonidentity_basis_restore(self):
        rig_ops._clear_bone_fcurves(self.armature_obj, {"Leg"})
        pb = self.armature_obj.pose.bones["Leg"]
        pb.location = (0.25, 0.1, 0)
        bpy.context.view_layer.update()
        expected = self.sample("Leg")
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        bpy.ops.object.rbxanims_worldspace_reparent()
        self.assertMotion(expected)
        self.assertEqual(
            rig_ops._snapshot_bone_fcurves(self.armature_obj, ["Leg"])["Leg"], []
        )

    def test_modifier_and_easing_snapshot_roundtrip(self):
        curves = get_action_fcurves(self.armature_obj.animation_data.action)
        fc = curves.find('pose.bones["Leg"].rotation_quaternion', index=0)
        fc.modifiers.new("NOISE").strength = 0.05
        point = fc.keyframe_points[0]
        point.interpolation = "ELASTIC"
        point.amplitude = 0.7
        point.period = 0.6
        original = rig_ops._snapshot_bone_fcurves(self.armature_obj, ["Leg"])
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        bpy.ops.object.rbxanims_worldspace_reparent()
        self.assertEqual(
            original, rig_ops._snapshot_bone_fcurves(self.armature_obj, ["Leg"])
        )

    def test_export_state_is_preserved(self):
        from ..animation.sampling import serialize_animation_state

        def sample_export():
            result = []
            for frame in range(1, 21):
                bpy.context.scene.frame_set(frame)
                bpy.context.view_layer.update()
                result.append(serialize_animation_state(self.armature_obj))
            return result

        expected = sample_export()
        self.select_bones("Torso", "Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        actual = sample_export()
        for a, b in zip(expected, actual):
            self.assertEqual(set(a), set(b))
            for name in a:
                self.assertLess(
                    max(abs(x - y) for x, y in zip(a[name], b[name])), 0.0002
                )

    def test_external_target_extends_sample_range(self):
        target = bpy.data.objects.new("LongProxyAnimation", None)
        bpy.context.scene.collection.objects.link(target)
        target.location.x = 0
        target.keyframe_insert(data_path="location", frame=1)
        target.location.x = 1
        target.keyframe_insert(data_path="location", frame=40)
        c = self.armature_obj.pose.bones["Leg"].constraints.new("COPY_LOCATION")
        c.target = target
        self.assertEqual(rig_ops._get_action_frame_range(self.armature_obj), (1, 40))

    def test_save_reload_then_restore_parent(self):
        import tempfile
        from pathlib import Path

        expected = self.sample("Leg")
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "detached.blend")
            bpy.ops.object.mode_set(mode="OBJECT")
            name = self.armature_obj.name
            bpy.data.libraries.write(path, {self.armature_obj})
            with bpy.data.libraries.load(path) as (source, target):
                target.objects = [name]
            loaded = target.objects[0]
            bpy.context.scene.collection.objects.link(loaded)
            self.armature_obj = loaded
            bpy.context.view_layer.objects.active = loaded
            loaded.select_set(True)
            bpy.context.view_layer.update()
            self.select_bones("Leg")
            bpy.ops.object.rbxanims_worldspace_reparent()
            self.assertMotion(expected)
            self.assertEqual(loaded.data.bones["Leg"].parent.name, "Torso")

    def test_connected_bone_roundtrip(self):
        bpy.ops.object.mode_set(mode="EDIT")
        self.armature_obj.data.edit_bones["Leg"].use_connect = True
        bpy.ops.object.mode_set(mode="POSE")
        expected = self.sample("Leg")
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        self.assertMotion(expected)
        bpy.ops.object.rbxanims_worldspace_reparent()
        self.assertMotion(expected)
        self.assertTrue(self.armature_obj.data.bones["Leg"].use_connect)

    def test_frame_and_subframe_are_restored(self):
        bpy.context.scene.frame_set(7, subframe=0.25)
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        self.assertEqual(bpy.context.scene.frame_current, 7)
        self.assertEqual(bpy.context.scene.frame_subframe, 0.25)

    def test_constant_parent_motion_does_not_ramp_before_jump(self):
        for curve in get_action_fcurves(self.armature_obj.animation_data.action):
            for point in curve.keyframe_points:
                point.interpolation = "CONSTANT"

        def pose():
            bpy.context.scene.frame_set(19, subframe=0.9)
            bpy.context.view_layer.update()
            return self.armature_obj.pose.bones["Leg"].matrix.copy()

        expected = pose()
        self.select_bones("Leg")
        bpy.ops.object.rbxanims_worldspace_unparent()
        actual = pose()
        self.assertLess(
            max(abs(expected[r][c] - actual[r][c]) for r in range(4) for c in range(4)),
            1e-5,
        )


del Fixture


class TestWorldSpaceTemplates(unittest.TestCase):
    def test_templates_all_generation_modes_and_renamed_proxies(self):
        from .test_rbxm_rebuild import (
            import_binary,
            template_binary,
            rebuild,
            clean_scene,
        )
        from ..animation.serialization import serialize
        from ..core.utils import pose_bone_set_selected

        try:
            for template in ("TemplateR6", "TemplateR15"):
                for mode in (
                    "RAW",
                    "LOCAL_AXIS_EXTEND",
                    "LOCAL_YAXIS_EXTEND",
                    "CONNECT",
                ):
                    for proxy in (False, True):
                        with self.subTest(template=template, mode=mode, proxy=proxy):
                            source, meta = import_binary(
                                template, template_binary(template)
                            )
                            source = rebuild(source, meta, mode)
                            controls = source
                            if proxy:
                                controls = bpy.data.objects.new(
                                    "RenamedControls", source.data.copy()
                                )
                                bpy.context.scene.collection.objects.link(controls)
                                bpy.context.view_layer.update()
                                for bone in controls.data.bones:
                                    bone.name = "CTRL_" + bone.name
                                for pb in source.pose.bones:
                                    c = pb.constraints.new("COPY_TRANSFORMS")
                                    c.target = controls
                                    c.subtarget = "CTRL_" + pb.name
                                    c.owner_space = "WORLD"
                                    c.target_space = "WORLD"
                            name = (
                                "Right Arm"
                                if template == "TemplateR6"
                                else "RightLowerArm"
                            )
                            if proxy:
                                name = "CTRL_" + name
                            child = controls.pose.bones[name]
                            parent = child.parent
                            for pb in (parent, child):
                                pb.rotation_mode = "XYZ"
                                for frame, angle in ((1, 0), (7, 0.25), (13, -0.1)):
                                    pb.rotation_euler.z = angle
                                    pb.keyframe_insert(
                                        data_path="rotation_euler", frame=frame
                                    )
                            bpy.context.scene.frame_start = 1
                            bpy.context.scene.frame_end = 13
                            bpy.context.scene.rbx_anim_settings.rbx_full_range_bake = (
                                True
                            )
                            expected = serialize(source)
                            if (
                                bpy.context.object
                                and bpy.context.object.mode != "OBJECT"
                            ):
                                bpy.ops.object.mode_set(mode="OBJECT")
                            bpy.ops.object.select_all(action="DESELECT")
                            controls.select_set(True)
                            bpy.context.view_layer.objects.active = controls
                            bpy.ops.object.mode_set(mode="POSE")
                            for pb in controls.pose.bones:
                                pose_bone_set_selected(pb, pb.name == name)
                            bpy.ops.object.rbxanims_worldspace_unparent()
                            actual = serialize(source)
                            rows = {round(row["t"], 8): row for row in actual["kfs"]}
                            for a in expected["kfs"]:
                                b = rows[round(a["t"], 8)]
                                for bone in a["kf"]:
                                    self.assertIn(bone, b["kf"])
                                    self.assertLess(
                                        max(
                                            abs(x - y)
                                            for x, y in zip(
                                                a["kf"][bone][0], b["kf"][bone][0]
                                            )
                                        ),
                                        0.0005,
                                    )
                            bpy.ops.object.rbxanims_worldspace_reparent()
        finally:
            clean_scene()
