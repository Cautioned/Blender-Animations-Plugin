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
from ..animation.import_export import get_mapping_error_bones, prepare_for_kf_map, copy_anim_state, apply_ao_transform
from ..core.utils import get_scene_fps, set_scene_fps


class OBJECT_OT_ApplyTransform(bpy.types.Operator):
    bl_label = "Apply armature object transform to the root bone for each keyframe"
    bl_idname = "object.rbxanims_applytransform"
    bl_description = "Apply armature object transform to the root bone for each keyframe -- Must set a proper frame range first!"

    @classmethod
    def poll(cls, context):
        armature_name = bpy.context.scene.rbx_anim_armature
        grig = bpy.data.objects.get(armature_name)
        return (
            grig
            and bpy.context.active_object
            and bpy.context.active_object.animation_data
        )

    def execute(self, context):
        if not bpy.context.scene.keying_sets.active:
            self.report(
                {"ERROR"}, "There is no active keying set, this is required.")
            return {"FINISHED"}

        apply_ao_transform(bpy.context.view_layer.objects.active)

        return {"FINISHED"}


class OBJECT_OT_MapKeyframes(bpy.types.Operator):
    bl_label = "Map keyframes by bone name"
    bl_idname = "object.rbxanims_mapkeyframes"
    bl_description = "Map keyframes by bone name --- From a selected armature, maps data (using a new keyframe per frame) onto the generated rig by name. Set frame ranges first!"

    @classmethod
    def poll(cls, context):
        armature_name = bpy.context.scene.rbx_anim_armature
        grig = bpy.data.objects.get(armature_name)
        return grig and bpy.context.active_object and bpy.context.active_object != grig

    def execute(self, context):
        armature_name = bpy.context.scene.rbx_anim_armature
        if not bpy.context.scene.keying_sets.active:
            self.report(
                {"ERROR"}, "There is no active keying set, this is required.")
            return {"FINISHED"}

        ao_imp = bpy.context.view_layer.objects.active

        err_mappings = get_mapping_error_bones(
            bpy.data.objects[armature_name], ao_imp)
        if len(err_mappings) > 0:
            self.report(
                {"ERROR"},
                "Cannot map rig, the following bones are missing from the source rig: {}.".format(
                    ", ".join(err_mappings)
                ),
            )
            return {"FINISHED"}

        prepare_for_kf_map()

        copy_anim_state(bpy.data.objects[armature_name], ao_imp)

        return {"FINISHED"}


class OBJECT_OT_Bake(bpy.types.Operator):
    bl_label = "Bake"
    bl_idname = "object.rbxanims_bake"
    bl_description = "Bake animation for export to clipboard"

    @classmethod
    def poll(cls, context):
        # Allow baking if there's a selected armature
        return context.scene.rbx_anim_armature in bpy.data.objects and bpy.data.objects[context.scene.rbx_anim_armature].type == 'ARMATURE'

    def execute(self, context):
        try:
            desired_fps = get_scene_fps()
            set_scene_fps(desired_fps)

            armature_name = context.scene.rbx_anim_armature
            ao = bpy.data.objects[armature_name]
            
            if ao.type != 'ARMATURE':
                self.report(
                    {"ERROR"}, f"Selected object '{armature_name}' is not an armature")
                return {"CANCELLED"}
                
            # Check if this is a deform bone rig or if deform bone serialization is forced
            use_deform_bone_serialization = is_deform_bone_rig(
                ao) or context.scene.force_deform_bone_serialization
            
            serialized = serialize(ao)
            if not serialized:
                self.report({"ERROR"}, "Failed to serialize animation")
                return {"CANCELLED"}
                
            encoded = json.dumps(serialized, separators=(",", ":"))
            compressed = zlib.compress(encoded.encode('utf-8'))
            b64_data = base64.b64encode(compressed).decode('utf-8')
            
            bpy.context.window_manager.clipboard = b64_data
            
            duration = serialized["t"]
            num_keyframes = len(serialized["kfs"])
            
            # Include information about the rig type in the report
            rig_type = "Deform Bone Rig" if use_deform_bone_serialization else "Motor6D Rig"
            self.report(
                {"INFO"},
                f"Baked {rig_type} animation data exported to clipboard ({num_keyframes} keyframes, {duration:.2f} seconds, {desired_fps} FPS)."
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
        options={'HIDDEN'},
        maxlen=255,  # Max internal buffer length, longer would be clamped.
    )
    
    @classmethod
    def poll(cls, context):
        # Allow baking if there's a selected armature
        return context.scene.rbx_anim_armature in bpy.data.objects and bpy.data.objects[context.scene.rbx_anim_armature].type == 'ARMATURE'

    def execute(self, context):
        try:
            desired_fps = get_scene_fps()
            set_scene_fps(desired_fps)

            # Try to use the selected armature first
            if context.scene.rbx_anim_armature and context.scene.rbx_anim_armature in bpy.data.objects:
                armature = bpy.data.objects[context.scene.rbx_anim_armature]
                # Set as active object to ensure serialize() uses it
                bpy.context.view_layer.objects.active = armature
            else:
                # Fall back to active object
                armature = bpy.context.view_layer.objects.active
                
            if not armature or armature.type != 'ARMATURE':
                self.report({"ERROR"}, "No valid armature selected")
                return {"CANCELLED"}
            
            # Check if this is a deform bone rig or if deform bone serialization is forced
            use_deform_bone_serialization = is_deform_bone_rig(
                armature) or context.scene.force_deform_bone_serialization
            
            serialized = serialize(armature)
            if not serialized:
                self.report({"ERROR"}, "Failed to serialize animation")
                return {"CANCELLED"}
                
            encoded = json.dumps(serialized, separators=(",", ":"))
            compressed_data = zlib.compress(encoded.encode(), 9)

            # Save to file using the provided file path
            filepath = self.filepath
            with open(filepath, 'wb') as file:
                file.write(compressed_data)

            duration = serialized["t"]
            num_keyframes = len(serialized["kfs"])
            
            # Include information about the rig type in the report
            rig_type = "Deform Bone Rig" if use_deform_bone_serialization else "Motor6D Rig"
            self.report(
                {"INFO"},
                f"Baked {rig_type} animation data exported to {filepath} ({num_keyframes} keyframes, {duration:.2f} seconds, {desired_fps} FPS)."
            )
            return {"FINISHED"}
        except Exception as e:
            self.report({"ERROR"}, f"Error during baking: {str(e)}")
            return {"CANCELLED"}
