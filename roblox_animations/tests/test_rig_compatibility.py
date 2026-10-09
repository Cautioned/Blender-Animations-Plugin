"""Headless regressions for constraint rigs and weapon controls."""

import unittest
import bpy
from mathutils import Matrix
from . import test_serialization as fixtures
from ..animation.serialization import serialize
from ..rig.weapon_proxy import resolve_weapon_rigs, clone_weapon_controls


class TestConstraintBake(unittest.TestCase):
    setUp = fixtures.TestAnimationSerialization.setUp
    tearDown = fixtures.TestAnimationSerialization.tearDown
    create_ik_rig = fixtures.TestAnimationSerialization.create_ik_rig
    set_action_interpolation = (
        fixtures.TestAnimationSerialization.set_action_interpolation
    )

    def test_nla_ik_keeps_evaluated_samples(self):
        self.create_ik_rig()
        ao = self.armature_obj
        action = ao.animation_data.action
        ao.animation_data.action = None
        track = ao.animation_data.nla_tracks.new()
        track.strips.new("IK", 1, action)
        result = serialize(ao)
        fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        foot_frames = {
            round(row["t"] * fps) + 1 for row in result["kfs"] if "Foot" in row["kf"]
        }
        self.assertEqual(foot_frames, set(range(1, 21)))

    def test_retimed_nla_uses_scene_time(self):
        self.create_ik_rig()
        ao = self.armature_obj
        for bone in ao.pose.bones:
            for constraint in list(bone.constraints):
                bone.constraints.remove(constraint)
        bone = ao.pose.bones["Unconstrained"]
        for frame, x in ((1, 0), (10, 2), (20, 0)):
            bone.location.x = x
            bone.keyframe_insert(data_path="location", frame=frame)
        action = ao.animation_data.action
        self.set_action_interpolation(action, "CONSTANT")
        ao.animation_data.action = None
        ao.animation_data.nla_tracks.new().strips.new("Shifted", 31, action)
        bpy.context.scene.frame_start = 31
        bpy.context.scene.frame_end = 50
        result = serialize(ao)
        fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        rows = {round(row["t"] * fps) + 31: row for row in result["kfs"]}
        self.assertIn(40, rows)
        self.assertIn("Unconstrained", rows[40]["kf"])

    def test_hidden_export_collection_is_evaluated_and_restored(self):
        self.create_ik_rig()
        ao = self.armature_obj
        expected = serialize(ao)
        collection = bpy.data.collections.new("Hidden Export")
        bpy.context.scene.collection.children.link(collection)
        for old in list(ao.users_collection):
            old.objects.unlink(ao)
        collection.objects.link(ao)
        collection.hide_viewport = True
        layer = bpy.context.view_layer.layer_collection.children[collection.name]
        layer.exclude = True
        bpy.context.scene.frame_set(7, subframe=0.25)
        actual = serialize(ao)
        self.assertEqual(actual, expected)
        self.assertTrue(collection.hide_viewport)
        self.assertTrue(layer.exclude)
        self.assertEqual(bpy.context.scene.frame_current, 7)
        self.assertAlmostEqual(bpy.context.scene.frame_subframe, 0.25)

    def test_spline_ik_marks_chain_for_baking(self):
        from ..animation.serialization import get_all_constrained_bones

        self.create_ik_rig()
        ao = self.armature_obj
        for bone in ao.pose.bones:
            for constraint in list(bone.constraints):
                bone.constraints.remove(constraint)
        spline = ao.pose.bones["Foot"].constraints.new("SPLINE_IK")
        spline.chain_count = 2
        self.assertTrue({"Foot", "LowerLeg"} <= get_all_constrained_bones(ao))


class TestWeaponProxy(unittest.TestCase):
    def setUp(self):
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        self.source = self.armature("Export", ["Hand", "Finger", "Weapon"])
        self.control = self.armature("Controls", ["GripControl", "Weapon"])
        for b in self.source.data.bones:
            b["transform"] = Matrix.Identity(4)
            b["transform1"] = Matrix.Identity(4)
        c = self.source.pose.bones["Hand"].constraints.new("CHILD_OF")
        c.target = self.control
        c.subtarget = "GripControl"

    def armature(self, name, bones):
        data = bpy.data.armatures.new(name)
        obj = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        root = None
        for i, name in enumerate(bones):
            b = data.edit_bones.new(name)
            b.head = (0, i, 0)
            b.tail = (0, i + 1, 0)
            b.parent = root
            if root is None:
                root = b
        bpy.ops.object.mode_set(mode="OBJECT")
        obj.select_set(False)
        return obj

    def test_incoming_child_of_resolves_export(self):
        self.assertEqual(resolve_weapon_rigs(self.control), (self.source, self.control))
        self.assertEqual(resolve_weapon_rigs(self.source), (self.source, self.control))

    def test_clone_maps_parent_handles_collision_and_drives_export(self):
        self.control.matrix_world = Matrix.Translation((3, -2, 1))
        bpy.context.view_layer.update()
        before = (
            self.source.evaluated_get(bpy.context.evaluated_depsgraph_get())
            .pose.bones["Weapon"]
            .matrix.copy()
        )
        mapping = clone_weapon_controls(
            bpy.context, self.source, self.control, {"Weapon"}
        )
        bpy.context.view_layer.update()
        after = (
            self.source.evaluated_get(bpy.context.evaluated_depsgraph_get())
            .pose.bones["Weapon"]
            .matrix
        )
        self.assertLess(
            max(abs(before[r][c] - after[r][c]) for r in range(4) for c in range(4)),
            1e-5,
        )
        self.assertNotEqual(mapping["Weapon"], "Weapon")
        b = self.control.data.bones[mapping["Weapon"]]
        self.assertEqual(b.parent.name, "GripControl")
        self.assertNotIn("Finger", self.control.data.bones)
        self.assertNotIn("transform", b)
        pb = self.control.pose.bones[b.name]
        pb.location = (0.4, 0.2, 0.1)
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        actual = (
            self.source.matrix_world
            @ self.source.evaluated_get(depsgraph).pose.bones["Weapon"].matrix
        )
        expected = (
            self.control.matrix_world
            @ self.control.evaluated_get(depsgraph).pose.bones[b.name].matrix
        )
        self.assertLess(
            max(abs(actual[r][c] - expected[r][c]) for r in range(4) for c in range(4)),
            1e-5,
        )

    def test_internal_hand_mapping_wins_over_external_master(self):
        bpy.context.view_layer.objects.active = self.source
        self.source.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        self.source.data.edit_bones["Finger"].parent = None
        bpy.ops.object.mode_set(mode="OBJECT")
        for c in list(self.source.pose.bones["Hand"].constraints):
            self.source.pose.bones["Hand"].constraints.remove(c)
        c = self.source.pose.bones["Hand"].constraints.new("COPY_TRANSFORMS")
        c.target = self.source
        c.subtarget = "Finger"
        mapping = clone_weapon_controls(
            bpy.context, self.source, self.control, {"Weapon"}
        )
        self.assertEqual(
            self.source.data.bones[mapping["Weapon"]].parent.name, "Finger"
        )
        self.assertEqual(
            self.source.pose.bones["Weapon"].constraints[-1].target, self.source
        )
