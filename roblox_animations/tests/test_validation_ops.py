import importlib
import unittest
from types import SimpleNamespace
from unittest import mock

from ..operators import validation_ops

importlib.reload(validation_ops)


class TestValidationScaleCalibration(unittest.TestCase):
    def test_internal_r15_armature_uses_the_bundled_fixture(self):
        bpy = validation_ops.bpy
        source_data = bpy.data.armatures.new("ValidationFixtureSource")
        source = bpy.data.objects.new("ValidationFixtureSource", source_data)
        bpy.context.collection.objects.link(source)
        internal = None
        try:
            internal = validation_ops._create_internal_r15_armature(bpy.context, source)
            self.assertEqual(len(internal.data.bones), 16)
            self.assertEqual(
                internal.data.bones["LowerTorso"].parent.name,
                "HumanoidRootPart",
            )
        finally:
            validation_ops._destroy_internal_r15_armature(internal)
            bpy.data.objects.remove(source, do_unlink=True)
            bpy.data.armatures.remove(source_data)

    def test_template_r15_fixture_contains_the_complete_standard_body(self):
        canonical = validation_ops._INTERNAL_EMOTE_R15_REST

        self.assertEqual(len(canonical), 15)
        self.assertEqual(canonical["lowertorso"]["parent"], "humanoidrootpart")
        self.assertEqual(canonical["head"]["parent"], "uppertorso")

    def test_collect_bone_world_head_allowlist_bypasses_transformable_filter(self):
        """The internal R15 validation template has no is_transformable /
        use_deform markers, so an explicit allowlist must be authoritative —
        otherwise the per-frame studs check collects no bones and fast
        movement is never flagged."""
        bpy = validation_ops.bpy
        arm_data = bpy.data.armatures.new("ValidationAllowlistArm")
        arm = bpy.data.objects.new("ValidationAllowlistArm", arm_data)
        bpy.context.collection.objects.link(arm)
        try:
            bpy.context.view_layer.objects.active = arm
            bpy.ops.object.mode_set(mode="EDIT")
            bone = arm_data.edit_bones.new("Torso")
            bone.head = (0, 0, 1)
            bone.tail = (0, 0, 2)
            bpy.ops.object.mode_set(mode="OBJECT")

            positions = validation_ops._collect_bone_world_head(
                arm, arm, allowed_bone_names={"Torso"}
            )
            self.assertIn("Torso", positions)

            # Without an allowlist the discovery filter still applies.
            positions_discovery = validation_ops._collect_bone_world_head(arm, arm)
            self.assertNotIn("Torso", positions_discovery)
        finally:
            bpy.data.objects.remove(arm, do_unlink=True)
            bpy.data.armatures.remove(arm_data)

    def test_sampling_grid_mirrors_tracker(self):
        """The tracker samples t=0..length inclusive at 1/70s."""
        times = validation_ops._build_validation_sample_times(1.0, 0.5)
        self.assertEqual(times, [0.0, 0.5, 1.0])

        times70 = validation_ops._build_validation_sample_times(1.0, 1.0 / 70.0)
        self.assertEqual(times70[0], 0.0)
        self.assertEqual(len(times70), 71)
        self.assertLessEqual(times70[-1], 1.0)
        self.assertGreater(times70[-1], 0.98)

        empty = validation_ops._build_validation_sample_times(0.0, 1.0 / 70.0)
        self.assertEqual(empty, [0.0])


class TestValidationControlRigRedirect(unittest.TestCase):
    """Control armature vs true rig detection for validation measurement."""

    def _make_control_rig(self, name="CtrlRig"):
        bpy = validation_ops.bpy
        data = bpy.data.armatures.new(name)
        obj = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(obj)
        try:
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="EDIT")
            bone = data.edit_bones.new("CtrlRoot")
            bone.head = (0, 0, 0)
            bone.tail = (0, 0, 1)
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            bpy.ops.object.mode_set(mode="OBJECT")
            raise
        return obj

    def _make_true_rig(self, source):
        return validation_ops._create_internal_r15_armature(
            validation_ops.bpy.context, source
        )

    def _add_copy_constraint(self, driven_armature, bone_name, target_armature):
        bpy = validation_ops.bpy
        bpy.context.view_layer.objects.active = driven_armature
        bpy.ops.object.mode_set(mode="OBJECT")
        constraint = driven_armature.pose.bones[bone_name].constraints.new(
            type="COPY_LOCATION"
        )
        constraint.target = target_armature
        constraint.subtarget = target_armature.pose.bones[0].name
        constraint.owner_space = "WORLD"
        constraint.target_space = "WORLD"
        return constraint

    def test_outgoing_copy_constraints_redirect_to_true_rig(self):
        """Control rig bones copy-constraining a standard R15 rig: validation
        resolves to the driven rig (weapon-import convention)."""
        bpy = validation_ops.bpy
        control = self._make_control_rig("OutCtrl")
        true_rig = None
        control_data = control.data
        try:
            true_rig = self._make_true_rig(control)
            self._add_copy_constraint(control, "CtrlRoot", true_rig)
            self.assertEqual(
                validation_ops._resolve_validation_measure_rig(control),
                true_rig,
            )
        finally:
            validation_ops._destroy_internal_r15_armature(true_rig)
            bpy.data.objects.remove(control, do_unlink=True)
            bpy.data.armatures.remove(control_data)

    def test_incoming_copy_constraints_redirect_to_true_rig(self):
        """True rig bones copy-constraining the control rig (animation lives
        on the control): validation still resolves to the true rig."""
        bpy = validation_ops.bpy
        control = self._make_control_rig("InCtrl")
        true_rig = None
        control_data = control.data
        try:
            true_rig = self._make_true_rig(control)
            self._add_copy_constraint(true_rig, "RightHand", control)
            self.assertEqual(
                validation_ops._resolve_validation_measure_rig(control),
                true_rig,
            )
        finally:
            validation_ops._destroy_internal_r15_armature(true_rig)
            bpy.data.objects.remove(control, do_unlink=True)
            bpy.data.armatures.remove(control_data)

    def test_standard_r15_selection_resolves_to_itself(self):
        bpy = validation_ops.bpy
        control = self._make_control_rig("SelfCtrl")
        true_rig = None
        control_data = control.data
        try:
            true_rig = self._make_true_rig(control)
            self.assertEqual(
                validation_ops._resolve_validation_measure_rig(true_rig),
                true_rig,
            )
        finally:
            validation_ops._destroy_internal_r15_armature(true_rig)
            bpy.data.objects.remove(control, do_unlink=True)
            bpy.data.armatures.remove(control_data)

    def test_constraint_driven_motion_is_measured(self):
        """Visual-head measurement captures motion keyed on a control rig
        and driven into the true rig via copy constraints."""
        bpy = validation_ops.bpy
        control = self._make_control_rig("MotionCtrl")
        true_rig = None
        control_data = control.data
        try:
            true_rig = self._make_true_rig(control)
            self._add_copy_constraint(true_rig, "RightHand", control)

            bpy.context.view_layer.objects.active = control
            bpy.ops.object.mode_set(mode="POSE")
            pose_bone = control.pose.bones["CtrlRoot"]
            pose_bone.location = (0, 0, 0)
            pose_bone.keyframe_insert(data_path="location", frame=1)
            pose_bone.location = (0, 1, 0)
            pose_bone.keyframe_insert(data_path="location", frame=10)
            bpy.ops.object.mode_set(mode="OBJECT")

            depsgraph = bpy.context.evaluated_depsgraph_get()
            scene = bpy.context.scene
            scene.frame_set(1)
            depsgraph.update()
            source_eval = true_rig.evaluated_get(depsgraph)
            name_map = validation_ops._collect_validation_name_map(source_eval)
            rest_positions, _root = (
                validation_ops._collect_rig_display_positions(
                    source_eval, true_rig, name_map
                )
            )
            rest_head = rest_positions["RightHand"]
            scene.frame_set(10)
            depsgraph.update()
            source_eval = true_rig.evaluated_get(depsgraph)
            moved_positions, _root = (
                validation_ops._collect_rig_display_positions(
                    source_eval, true_rig, name_map
                )
            )
            moved_head = moved_positions["RightHand"]

            self.assertGreater((moved_head - rest_head).length, 0.01)
        finally:
            validation_ops._destroy_internal_r15_armature(true_rig)
            bpy.data.objects.remove(control, do_unlink=True)
            bpy.data.armatures.remove(control_data)


class TestValidationDisplayMapping(unittest.TestCase):
    """Overlay drawing must map back to the actual rig, not the internal template."""

    def test_display_positions_map_to_actual_rig_world_space(self):
        bpy = validation_ops.bpy
        source = self._make_source_armature()
        source_data = source.data
        actual = None
        internal = None
        try:
            actual = validation_ops._create_internal_r15_armature(
                bpy.context, source
            )
            # Scale and move the ACTUAL rig away from the unscaled template.
            from mathutils import Matrix, Quaternion

            actual.matrix_world = Matrix.LocRotScale(
                (3.0, 4.0, 5.0),
                Quaternion(),
                (0.5, 0.5, 0.5),
            )
            internal = validation_ops._create_internal_r15_armature(
                bpy.context, actual
            )

            depsgraph = bpy.context.evaluated_depsgraph_get()
            bpy.context.scene.frame_set(1)
            depsgraph.update()
            source_eval = actual.evaluated_get(depsgraph)
            name_map = validation_ops._collect_validation_name_map(source_eval)
            display_positions, display_root = (
                validation_ops._collect_rig_display_positions(
                    source_eval, actual, name_map
                )
            )

            expected = (
                source_eval.matrix_world
                @ source_eval.pose.bones["RightHand"].head
            )
            self.assertLess(
                (display_positions["RightHand"] - expected).length, 1e-6
            )
            self.assertAlmostEqual(display_root.x, 3.0, places=6)
            self.assertAlmostEqual(display_root.y, 4.0, places=6)
            self.assertAlmostEqual(display_root.z, 5.0, places=6)

            # The internal template position must differ from the displayed
            # one (0.5-scaled rig), proving the overlay no longer uses
            # internal coordinates.
            bpy.context.view_layer.update()
            internal_eval = internal.evaluated_get(depsgraph)
            internal_positions = validation_ops._collect_bone_world_head(
                internal,
                internal_eval,
                allowed_bone_names={"RightHand"},
            )
            self.assertGreater(
                (
                    display_positions["RightHand"]
                    - internal_positions["RightHand"]
                ).length,
                0.5,
            )
        finally:
            validation_ops._destroy_internal_r15_armature(internal)
            validation_ops._destroy_internal_r15_armature(actual)
            bpy.data.objects.remove(source, do_unlink=True)
            bpy.data.armatures.remove(source_data)

    def _make_source_armature(self):
        bpy = validation_ops.bpy
        data = bpy.data.armatures.new("DisplaySource")
        obj = bpy.data.objects.new("DisplaySource", data)
        bpy.context.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        bone = data.edit_bones.new("Root")
        bone.head = (0, 0, 0)
        bone.tail = (0, 0, 1)
        bpy.ops.object.mode_set(mode="OBJECT")
        return obj

    def test_rest_scale_samples_ignore_object_scale(self):
        """Scale resolution must be armature-local, so scaling the rig
        object cannot skew the studs measurements."""
        from mathutils import Matrix, Quaternion

        bpy = validation_ops.bpy
        source = self._make_source_armature()
        source_data = source.data
        actual = None
        try:
            actual = validation_ops._create_internal_r15_armature(
                bpy.context, source
            )
            for bone in actual.data.bones:
                bone["is_transformable"] = True
            actual.matrix_world = Matrix.LocRotScale(
                (0, 0, 0),
                Quaternion(),
                (2.5, 2.5, 2.5),
            )

            samples = validation_ops._collect_validation_rest_scale_samples(
                actual
            )
            resolved = validation_ops._resolve_validation_scale_from_rest_samples(
                samples, validation_ops._INTERNAL_EMOTE_R15_REST
            )
            self.assertIsNotNone(resolved)
            self.assertAlmostEqual(resolved[0], 1.0, places=6)
        finally:
            validation_ops._destroy_internal_r15_armature(actual)
            bpy.data.objects.remove(source, do_unlink=True)
            bpy.data.armatures.remove(source_data)

    def test_internal_r15_scale_uses_rest_distance_ratio(self):
        canonical = validation_ops._INTERNAL_EMOTE_R15_REST
        source = {
            bone_name: {
                "parent": entry["parent"],
                "distance": entry["distance"] * 0.25,
            }
            for bone_name, entry in canonical.items()
        }

        resolved = validation_ops._resolve_validation_scale_from_rest_samples(
            source,
            canonical,
        )

        self.assertIsNotNone(resolved)
        scale, sample_count = resolved
        self.assertAlmostEqual(scale, 0.25, places=6)
        self.assertEqual(sample_count, len(canonical))

    def test_internal_r15_scale_skips_parent_mismatches(self):
        canonical = validation_ops._INTERNAL_EMOTE_R15_REST
        source = {
            "lowertorso": {
                "parent": "humanoidrootpart",
                "distance": canonical["lowertorso"]["distance"] * 0.5,
            },
            "uppertorso": {"parent": "humanoidrootpart", "distance": 9.0},
            "head": {
                "parent": canonical["head"]["parent"],
                "distance": canonical["head"]["distance"] * 0.5,
            },
        }

        resolved = validation_ops._resolve_validation_scale_from_rest_samples(
            source,
            canonical,
        )

        self.assertIsNotNone(resolved)
        scale, sample_count = resolved
        self.assertAlmostEqual(scale, 0.5, places=6)
        self.assertEqual(sample_count, 2)

    def test_floor_scale_prefers_lower_body_subset(self):
        canonical = validation_ops._INTERNAL_EMOTE_R15_REST
        source = {
            bone_name: {
                "parent": entry["parent"],
                "distance": entry["distance"] * 0.75,
            }
            for bone_name, entry in canonical.items()
        }

        for bone_name in validation_ops._INTERNAL_EMOTE_R15_FLOOR_BONES:
            source[bone_name]["distance"] = canonical[bone_name]["distance"] * 1.125

        resolved = validation_ops._resolve_validation_scale_from_rest_samples(
            source,
            canonical,
            preferred_bone_names=validation_ops._INTERNAL_EMOTE_R15_FLOOR_BONES,
        )

        self.assertIsNotNone(resolved)
        scale, sample_count = resolved
        self.assertAlmostEqual(scale, 1.125, places=6)
        self.assertEqual(sample_count, len(validation_ops._INTERNAL_EMOTE_R15_FLOOR_BONES))

    def test_floor_limit_tracks_lowest_candidate(self):
        floor_limit = None
        floor_limit = validation_ops._accumulate_floor_limit_z(floor_limit, 2.0)
        floor_limit = validation_ops._accumulate_floor_limit_z(floor_limit, -1.5)
        floor_limit = validation_ops._accumulate_floor_limit_z(floor_limit, 0.25)

        self.assertEqual(floor_limit, -1.5)

    def test_motion_threshold_scales_with_fps(self):
        self.assertAlmostEqual(
            validation_ops._resolve_motion_threshold_for_fps(1.0, 30.0),
            1.0,
            places=6,
        )
        self.assertAlmostEqual(
            validation_ops._resolve_motion_threshold_for_fps(1.0, 60.0),
            0.5,
            places=6,
        )
        self.assertAlmostEqual(
            validation_ops._resolve_motion_threshold_for_fps(1.0, 15.0),
            2.0,
            places=6,
        )

    def test_duration_limits_match_current_curve_validator(self):
        scene = SimpleNamespace(frame_start=1, frame_end=301)
        self.assertFalse(validation_ops._validate_animation_duration(scene, 30.0))
        scene.frame_end = 302
        self.assertTrue(validation_ops._validate_animation_duration(scene, 30.0))
        scene.frame_end = 1
        self.assertTrue(validation_ops._validate_animation_duration(scene, 30.0))
        scene.frame_end = 2
        self.assertFalse(validation_ops._validate_animation_duration(scene, 30.0))

    def test_duration_uses_exported_time_at_different_frame_rates(self):
        for fps in (24.0, 30.0, 60.0, 120.0):
            scene = SimpleNamespace(frame_start=11, frame_end=11 + int(10 * fps))
            self.assertFalse(validation_ops._validate_animation_duration(scene, fps))
            scene.frame_end += 1
            self.assertTrue(validation_ops._validate_animation_duration(scene, fps))

    def test_final_pose_between_sampling_grid_points_is_checked(self):
        end = 1.005
        times = validation_ops._build_validation_sample_times(end, 1.0 / 70.0)
        self.assertEqual(times[-1], end)
        self.assertTrue(all(a < b for a, b in zip(times, times[1:])))
        self.assertTrue(all(t <= end for t in times))
        # A violation occurring only after the final grid point was missed.
        violations = [t for t in times if t > 1.002]
        self.assertEqual(violations, [end])

    def test_short_animation_still_checks_both_endpoints(self):
        self.assertEqual(validation_ops._build_validation_sample_times(0.005, 1.0 / 70.0), [0.0, 0.005])


    def test_body_envelope_checks_are_relative_to_humanoid_root_part(self):
        root = validation_ops.Vector((0.0, 0.0, 0.0))
        positions = {
            "Near": validation_ops.Vector((0.0, 0.0, 0.0)),
            "Far": validation_ops.Vector((26.0, 0.0, 0.0)),
            "Below": validation_ops.Vector((0.0, 0.0, -3.2)),
        }

        distance_warnings = validation_ops._validate_body_distance_from_root(
            positions, root, 1.0
        )
        height_warnings = validation_ops._validate_body_height_from_root(
            positions, root, 1.0
        )

        self.assertEqual([name for name, _ in distance_warnings], ["Far"])
        self.assertEqual([name for name, _ in height_warnings], ["Below"])

    def test_standard_r15_rig_accepts_documented_hierarchy(self):
        root = _FakeBone("Root")
        humanoid_root = _FakeBone("HumanoidRootNode", parent=root)
        bones = [root, humanoid_root]
        by_name = {bone.name: bone for bone in bones}
        for bone_name, entry in validation_ops._INTERNAL_EMOTE_R15_REST.items():
            parent_name = (
                "HumanoidRootNode"
                if bone_name == "lowertorso"
                else next(
                    name for name in by_name
                    if validation_ops._normalize_bone_name(name) == entry["parent"]
                )
            )
            bone = _FakeBone(bone_name, parent=by_name[parent_name])
            bones.append(bone)
            by_name[bone_name] = bone

        self.assertFalse(validation_ops._validate_standard_r15_rig(bones))

    def test_standard_r15_rig_reports_missing_and_invalid_parent(self):
        lower_torso = _FakeBone("LowerTorso", parent=_FakeBone("WrongRoot"))
        warnings = validation_ops._validate_standard_r15_rig([lower_torso])

        self.assertIn("Standard R15 bone 'uppertorso' is missing", warnings)
        self.assertTrue(any("LowerTorso" in warning for warning in warnings))

    def test_manual_validation_fallback_uses_manual_scale(self):
        scene = SimpleNamespace(unit_settings=SimpleNamespace(scale_length=0.01))
        settings = SimpleNamespace(
            rbx_auto_deform_scale=False,
            rbx_deform_rig_scale=0.25,
            id_data=scene,
        )

        with mock.patch.object(validation_ops, "is_deform_bone_rig", return_value=True):
            scale, source, sample_count = validation_ops._fallback_validation_units_per_stud(
                SimpleNamespace(),
                settings,
            )

        self.assertAlmostEqual(scale, 0.25, places=6)
        self.assertEqual(source, "manual")
        self.assertEqual(sample_count, 0)

    def test_validation_root_prefers_humanoid_root_part(self):
        pose_bones = _FakePoseBones(
            [
                _FakePoseBone("Root"),
                _FakePoseBone("HumanoidRootPart"),
                _FakePoseBone("LowerTorso"),
            ]
        )

        self.assertEqual(
            validation_ops._resolve_validation_root_bone_name(pose_bones),
            "HumanoidRootPart",
        )

    def test_validation_root_falls_back_to_parentless_bone(self):
        child = _FakePoseBone("Child", parent=object())
        root = _FakePoseBone("WeirdRigRoot")
        pose_bones = _FakePoseBones([child, root])

        self.assertEqual(
            validation_ops._resolve_validation_root_bone_name(pose_bones),
            "WeirdRigRoot",
        )

    def test_validation_body_bones_filter_to_canonical_r15_set(self):
        resolved = validation_ops._resolve_validation_body_bone_name_set(
            [
                "HumanoidRootPart",
                "LowerTorso",
                "UpperTorso",
                "Head",
                "LeftUpperArm",
                "LeftLowerArm",
                "LeftHand",
                "RightUpperArm",
                "RightLowerArm",
                "RightHand",
                "LeftUpperLeg",
                "LeftLowerLeg",
                "LeftFoot",
                "RightUpperLeg",
                "RightLowerLeg",
                "RightFoot",
                "RightFoot.005",
                "LeftFoot.005",
                "RightLowerLeg-IKTarget",
                "LeftLowerLeg-IKTarget",
                "RightLowerLeg.001",
                "LeftLowerLeg.001",
            ]
        )

        self.assertIsNotNone(resolved)
        self.assertIn("LeftFoot", resolved)
        self.assertIn("RightFoot", resolved)
        self.assertNotIn("RightFoot.005", resolved)
        self.assertNotIn("LeftFoot.005", resolved)
        self.assertNotIn("RightLowerLeg-IKTarget", resolved)

    def test_validation_body_bones_fallback_when_not_enough_matches(self):
        resolved = validation_ops._resolve_validation_body_bone_name_set(
            ["ControlRoot", "Widget", "FootCtrl", "PoleTarget"]
        )

        self.assertIsNone(resolved)


class _FakePoseBone(SimpleNamespace):
    def __init__(self, name, parent=None):
        super().__init__(name=name, parent=parent)


class _FakeBone(_FakePoseBone):
    pass


class _FakePoseBones(list):
    def get(self, name):
        for bone in self:
            if bone.name == name:
                return bone
        return None


if __name__ == "__main__":
    unittest.main()
