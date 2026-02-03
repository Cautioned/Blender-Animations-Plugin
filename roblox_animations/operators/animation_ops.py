"""
Animation-related operators for baking and keyframe management.
"""

import json
import base64
import zlib
import bpy
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper
from bpy.props import StringProperty
from ..animation.serialization import serialize, is_deform_bone_rig
from ..animation.import_export import (
    get_mapping_error_bones,
    prepare_for_kf_map,
    copy_anim_state,
    apply_ao_transform,
)
from ..core.utils import get_scene_fps, set_scene_fps, get_object_by_name, object_exists


class OBJECT_OT_ApplyTransform(bpy.types.Operator):
    bl_label = "Apply armature object transform to the root bone for each keyframe"
    bl_idname = "object.rbxanims_applytransform"
    bl_description = "Apply armature object transform to the root bone for each keyframe -- Must set a proper frame range first!"

    @classmethod
    def poll(cls, context):
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        grig = get_object_by_name(armature_name)
        return (
            grig
            and bpy.context.active_object
            and bpy.context.active_object.animation_data
        )

    def execute(self, context):
        if not bpy.context.scene.keying_sets.active:
            self.report({"ERROR"}, "There is no active keying set, this is required.")
            return {"FINISHED"}

        apply_ao_transform(bpy.context.view_layer.objects.active)

        return {"FINISHED"}


class OBJECT_OT_MapKeyframes(bpy.types.Operator):
    bl_label = "Map keyframes by bone name"
    bl_idname = "object.rbxanims_mapkeyframes"
    bl_description = "Map keyframes by bone name --- From a selected armature, maps data (using a new keyframe per frame) onto the generated rig by name. Set frame ranges first!"

    @classmethod
    def poll(cls, context):
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        grig = get_object_by_name(armature_name)
        return grig and bpy.context.active_object and bpy.context.active_object != grig

    def execute(self, context):
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        if not bpy.context.scene.keying_sets.active:
            self.report({"ERROR"}, "There is no active keying set, this is required.")
            return {"FINISHED"}

        ao_imp = bpy.context.view_layer.objects.active

        src_armature = get_object_by_name(armature_name)
        err_mappings = get_mapping_error_bones(src_armature, ao_imp)
        if len(err_mappings) > 0:
            self.report(
                {"ERROR"},
                "Cannot map rig, the following bones are missing from the source rig: {}.".format(
                    ", ".join(err_mappings)
                ),
            )
            return {"FINISHED"}

        prepare_for_kf_map()

        copy_anim_state(src_armature, ao_imp)

        return {"FINISHED"}


class OBJECT_OT_Bake(bpy.types.Operator):
    bl_label = "Bake"
    bl_idname = "object.rbxanims_bake"
    bl_description = "Bake animation for export to clipboard"

    @classmethod
    def poll(cls, context):
        # Allow baking if there's a selected armature
        settings = getattr(context.scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        return (
            object_exists(arm_name, context.scene)
            and get_object_by_name(arm_name, context.scene).type == "ARMATURE"
        )

    def execute(self, context):
        try:
            desired_fps = get_scene_fps()
            set_scene_fps(desired_fps)

            settings = getattr(context.scene, "rbx_anim_settings", None)
            armature_name = settings.rbx_anim_armature if settings else None
            ao = get_object_by_name(armature_name)

            if ao.type != "ARMATURE":
                self.report(
                    {"ERROR"}, f"Selected object '{armature_name}' is not an armature"
                )
                return {"CANCELLED"}

            # Check if this is a deform bone rig or if deform bone serialization is forced
            force_deform = getattr(settings, "force_deform_bone_serialization", False)
            use_deform_bone_serialization = is_deform_bone_rig(ao) or force_deform

            serialized = serialize(ao)
            if not serialized:
                self.report({"ERROR"}, "Failed to serialize animation")
                return {"CANCELLED"}

            encoded = json.dumps(serialized, separators=(",", ":"))
            compressed = zlib.compress(encoded.encode("utf-8"))
            b64_data = base64.b64encode(compressed).decode("utf-8")

            bpy.context.window_manager.clipboard = b64_data

            duration = serialized["t"]
            num_keyframes = len(serialized["kfs"])

            # Include information about the rig type in the report
            rig_type = (
                "Deform Bone Rig" if use_deform_bone_serialization else "Motor6D Rig"
            )
            self.report(
                {"INFO"},
                f"Baked {rig_type} animation data exported to clipboard ({num_keyframes} keyframes, {duration:.2f} seconds, {desired_fps} FPS).",
            )
        except Exception as e:
            self.report({"ERROR"}, f"Error during baking: {str(e)}")

        return {"FINISHED"}


class OBJECT_OT_Bake_File(Operator, ExportHelper):
    bl_label = "Bake to File"
    bl_idname = "object.rbxanims_bake_file"
    bl_description = "Bake animation for export to file"

    # ExportHelper mixin class uses this
    filename_ext = ".rbxanim"

    filter_glob: StringProperty(
        default="*.rbxanim",
        options={"HIDDEN"},
        maxlen=255,  # Max internal buffer length, longer would be clamped.
    )

    @classmethod
    def poll(cls, context):
        # Allow baking if there's a selected armature
        settings = getattr(context.scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        return (
            object_exists(arm_name, context.scene)
            and get_object_by_name(arm_name, context.scene).type == "ARMATURE"
        )

    def execute(self, context):
        try:
            desired_fps = get_scene_fps()
            set_scene_fps(desired_fps)

            # Try to use the selected armature first
            settings = getattr(context.scene, "rbx_anim_settings", None)
            arm_name = settings.rbx_anim_armature if settings else None
            if arm_name and object_exists(arm_name, context.scene):
                armature = get_object_by_name(arm_name, context.scene)
                # Set as active object to ensure serialize() uses it
                bpy.context.view_layer.objects.active = armature
            else:
                # Fall back to active object
                armature = bpy.context.view_layer.objects.active

            if not armature or armature.type != "ARMATURE":
                self.report({"ERROR"}, "No valid armature selected")
                return {"CANCELLED"}

            # Check if this is a deform bone rig or if deform bone serialization is forced
            force_deform = getattr(settings, "force_deform_bone_serialization", False)
            use_deform_bone_serialization = is_deform_bone_rig(armature) or force_deform

            serialized = serialize(armature)
            if not serialized:
                self.report({"ERROR"}, "Failed to serialize animation")
                return {"CANCELLED"}

            encoded = json.dumps(serialized, separators=(",", ":"))
            compressed_data = zlib.compress(encoded.encode(), 9)

            # Save to file using the provided file path
            filepath = self.filepath
            with open(filepath, "wb") as file:
                file.write(compressed_data)

            duration = serialized["t"]
            num_keyframes = len(serialized["kfs"])

            # Include information about the rig type in the report
            rig_type = (
                "Deform Bone Rig" if use_deform_bone_serialization else "Motor6D Rig"
            )
            self.report(
                {"INFO"},
                f"Baked {rig_type} animation data exported to {filepath} ({num_keyframes} keyframes, {duration:.2f} seconds, {desired_fps} FPS).",
            )
            return {"FINISHED"}
        except Exception as e:
            self.report({"ERROR"}, f"Error during baking: {str(e)}")
            return {"CANCELLED"}
