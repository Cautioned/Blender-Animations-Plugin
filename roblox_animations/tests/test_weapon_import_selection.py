"""Weapon import must not restore invalid dynamic armature enum values."""

import unittest
from types import SimpleNamespace
from unittest import mock

import bpy

from ..core.utils import invalidate_armature_cache
from ..operators import import_ops
from ..rig import weapon_proxy
from .test_rbxm_rebuild import clean_scene
from .test_studio_template_roundtrip import build_export_case, templates


class TestWeaponImportSelection(unittest.TestCase):
    def setUp(self):
        clean_scene()
        self.target = bpy.data.objects.new("Target", bpy.data.armatures.new("Target"))
        bpy.context.scene.collection.objects.link(self.target)
        invalidate_armature_cache()
        self.settings = bpy.context.scene.rbx_anim_settings
        self.operator = SimpleNamespace(
            target_rig=self.target.name,
            report=mock.Mock(),
            _apply_rbxm_weapon=mock.Mock(return_value={"FINISHED"}),
        )

    def tearDown(self):
        import_ops._pending_weapon_import.clear()
        clean_scene()

    def unset_selection(self):
        # Invalid stored enum indices occur after rig removal/cache changes.
        self.settings["rbx_anim_armature"] = 99999
        self.assertEqual(self.settings.rbx_anim_armature, "")

    def execute(self, mode="rbxm"):
        import_ops._pending_weapon_import.update(
            mode=mode, data={"schema": 1, "meta_loaded": {}, "rig_part_obj_names": []}
        )
        return import_ops.OBJECT_OT_ApplyWeaponImport.execute(
            self.operator, bpy.context
        )

    def test_success_with_empty_previous_selection(self):
        self.unset_selection()
        self.assertEqual(self.execute(), {"FINISHED"})
        self.assertEqual(self.settings.rbx_anim_armature, self.target.name)

    def test_cancellation_with_empty_previous_selection(self):
        self.unset_selection()
        self.operator._apply_rbxm_weapon.return_value = {"CANCELLED"}
        self.assertEqual(self.execute(), {"CANCELLED"})
        self.assertEqual(self.settings.rbx_anim_armature, self.target.name)

    def test_failure_preserves_original_exception(self):
        self.unset_selection()
        self.operator._apply_rbxm_weapon.side_effect = ValueError(
            "Original attachment failure"
        )
        with self.assertRaisesRegex(ValueError, "Original attachment failure"):
            self.execute()

    def test_cancellation_restores_valid_previous_selection(self):
        other = bpy.data.objects.new("Other", bpy.data.armatures.new("Other"))
        bpy.context.scene.collection.objects.link(other)
        invalidate_armature_cache()
        self.settings.rbx_anim_armature = other.name
        self.operator._apply_rbxm_weapon.return_value = {"CANCELLED"}
        self.assertEqual(self.execute(), {"CANCELLED"})
        self.assertEqual(self.settings.rbx_anim_armature, other.name)

    def test_previous_armature_deleted_during_import_is_not_restored(self):
        other = bpy.data.objects.new("Other", bpy.data.armatures.new("Other"))
        bpy.context.scene.collection.objects.link(other)
        invalidate_armature_cache()
        self.settings.rbx_anim_armature = other.name

        def cancel(*args):
            bpy.data.objects.remove(other, do_unlink=True)
            return {"CANCELLED"}

        self.operator._apply_rbxm_weapon.side_effect = cancel
        self.assertEqual(self.execute(), {"CANCELLED"})

    def test_obj_weapon_path_also_accepts_empty_previous_selection(self):
        self.unset_selection()
        with mock.patch.object(
            import_ops.OBJECT_OT_ImportModel,
            "_import_weapon",
            return_value={"FINISHED"},
        ):
            self.assertEqual(self.execute("obj"), {"FINISHED"})
        self.assertEqual(self.settings.rbx_anim_armature, self.target.name)

    def test_real_r15_weapon_and_proxy_import_with_empty_selection(self):
        original = weapon_proxy.resolve_weapon_rigs

        def resolve(*args, **kwargs):
            resolved = original(*args, **kwargs)
            self.unset_selection()
            return resolved

        with mock.patch.object(
            weapon_proxy, "resolve_weapon_rigs", side_effect=resolve
        ):
            case = build_export_case("TemplateR15", templates()["TemplateR15"])
        self.assertTrue(case["baked"]["kfs"])
        selected = bpy.data.objects[self.settings.rbx_anim_armature]
        self.assertIn("VerificationWeapon", selected.data.bones)
        self.assertFalse(selected.name.endswith("_Controls"))
