"""
Import operators for rig and animation data.
"""

import json
import base64
import re
import bpy
from bpy_extras.io_utils import ImportHelper
from ..core.utils import get_unique_name
from ..rig.creation import autoname_parts, get_unique_collection_name


class OBJECT_OT_ImportModel(bpy.types.Operator, ImportHelper):
    bl_label = "Import rig data (.obj)"
    bl_idname = "object.rbxanims_importmodel"
    bl_description = "Import rig data (.obj)"

    filename_ext = ".obj"
    filter_glob: bpy.props.StringProperty(default="*.obj", options={"HIDDEN"})
    filepath: bpy.props.StringProperty(name="File Path", maxlen=1024, default="")

    def execute(self, context):
        # Do not clear objects
        objnames_before_import = {obj.name for obj in bpy.data.objects}
        if bpy.app.version >= (5, 0, 0):
            bpy.ops.wm.obj_import(
                filepath=self.properties.filepath,
                use_split_groups=True,
                forward_axis="NEGATIVE_Z",
                up_axis="Y",
            )
        elif bpy.app.version >= (4, 0, 0):
            bpy.ops.wm.obj_import(
                filepath=self.properties.filepath,
                use_split_groups=True,
            )
        else:
            bpy.ops.import_scene.obj(
                filepath=self.properties.filepath, use_split_groups=True
            )

        # Get the actual newly imported OBJECTS
        imported_objs = {
            obj for obj in bpy.data.objects if obj.name not in objnames_before_import
        }

        # Extract meta...
        encodedmeta = ""
        partial = {}
        meta_objs_to_delete = []
        for obj in imported_objs:
            match = re.search(r"^Meta(\d+)q1(.*?)q1\d*(\.\d+)?$", obj.name)
            if match:
                partial[int(match.group(1))] = match.group(2)
                meta_objs_to_delete.append(obj)

        # Check if this is actually a rig file (has metadata)
        if not meta_objs_to_delete:
            self.report(
                {"ERROR"},
                "This OBJ file does not contain Roblox rig metadata. "
                "Please use Blender's standard OBJ importer for regular 3D models, "
                "or export the rig from Roblox Studio using the Roblox Animations plugin.",
            )
            return {"CANCELLED"}

        # The rig parts are simply the imported objects that are not meta objects.
        # This is done before deleting, ensuring we have valid object references.
        rig_part_objs = imported_objs - set(meta_objs_to_delete)

        # Now, delete the meta objects using low-level API
        for obj in meta_objs_to_delete:
            bpy.data.objects.remove(obj)

        try:
            for i in range(1, len(partial) + 1):
                if i in partial:  # Check if the key exists
                    encodedmeta += partial[i]
                else:
                    self.report(
                        {"ERROR"},
                        f"Missing metadata part {i}. The rig file may be corrupted.",
                    )
                    return {"CANCELLED"}

            encodedmeta = encodedmeta.replace("0", "=")

            # Validate encoded metadata is not empty
            if not encodedmeta.strip():
                self.report(
                    {"ERROR"},
                    "Rig metadata is empty or corrupted. The rig file may be corrupted.",
                )
                return {"CANCELLED"}

            try:
                meta = base64.b32decode(encodedmeta, True).decode("utf-8")
            except Exception as e:
                self.report(
                    {"ERROR"},
                    f"Failed to decode rig metadata: {str(e)}. The rig file may be corrupted.",
                )
                return {"CANCELLED"}

            try:
                meta_loaded = json.loads(meta)
            except Exception as e:
                self.report(
                    {"ERROR"},
                    f"Failed to parse rig metadata JSON: {str(e)}. The rig file may be corrupted.",
                )
                return {"CANCELLED"}

            # Store meta in an empty
            bpy.ops.object.add(type="EMPTY", location=(0, 0, 0))
            ob = bpy.context.object
            rig_name = meta_loaded.get("rigName", "Rig")
            ob.name = get_unique_name(f"__{rig_name}Meta")
            ob["RigMeta"] = meta

            # Create a unique master collection for this rig
            master_collection_name = get_unique_collection_name(f"RIG: {rig_name}")
            master_collection = bpy.data.collections.new(master_collection_name)
            context.scene.collection.children.link(master_collection)

            # Create a sub-collection for the parts
            parts_collection = bpy.data.collections.new("Parts")
            master_collection.children.link(parts_collection)

            # Move the meta object to the master collection
            for coll in ob.users_collection:
                coll.objects.unlink(ob)
            master_collection.objects.link(ob)

            # Move all imported parts to the rig's parts collection
            for obj in rig_part_objs:
                if obj:  # Check if object still exists
                    for coll in obj.users_collection:
                        coll.objects.unlink(obj)
                    parts_collection.objects.link(obj)

            # Check if 'parts' and 'rigName' exist in meta_loaded and then autoname
            if "parts" in meta_loaded and "rigName" in meta_loaded:
                # We pass parts_collection.objects to ensure we are renaming the objects
                # that are actually in the collection, avoiding any ambiguity.
                autoname_parts(
                    meta_loaded["parts"],
                    meta_loaded["rigName"],
                    parts_collection.objects,
                )
            else:
                self.report({"WARNING"}, "Missing 'parts' or 'rigName' in rig metadata")

            return {"FINISHED"}
        except KeyError as e:
            self.report(
                {"ERROR"},
                f"KeyError: {str(e)} - The rig file may be corrupted or incompatible.",
            )
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Error importing rig: {str(e)}")
            return {"CANCELLED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class OBJECT_OT_ImportFbxAnimation(bpy.types.Operator, ImportHelper):
    bl_label = "Import animation data (.fbx)"
    bl_idname = "object.rbxanims_importfbxanimation"
    bl_description = "Import animation data (.fbx) --- FBX file should contain an armature, which will be mapped onto the generated rig by bone names."

    filename_ext = ".fbx"
    filter_glob: bpy.props.StringProperty(default="*.fbx", options={"HIDDEN"})
    filepath: bpy.props.StringProperty(name="File Path", maxlen=1024, default="")

    @classmethod
    def poll(cls, context):
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        return bpy.data.objects.get(armature_name)

    def execute(self, context):
        from ..animation.import_export import (
            get_mapping_error_bones,
            prepare_for_kf_map,
            copy_anim_state,
            apply_ao_transform,
        )
        import math

        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        # check active keying set
        if not bpy.context.scene.keying_sets.active:
            self.report({"ERROR"}, "There is no active keying set, this is required.")
            return {"FINISHED"}

        # import and keep track of what is imported
        objnames_before_import = [x.name for x in bpy.data.objects]
        bpy.ops.import_scene.fbx(filepath=self.properties.filepath)
        objnames_imported = [
            x.name for x in bpy.data.objects if x.name not in objnames_before_import
        ]

        def clear_imported():
            # Use low-level API to remove objects directly
            for obj_name in objnames_imported:
                obj = bpy.data.objects.get(obj_name)
                if obj:
                    bpy.data.objects.remove(obj)

        # check that there's only 1 armature
        armatures_imported = [
            x
            for x in bpy.data.objects
            if x.type == "ARMATURE" and x.name in objnames_imported
        ]
        if len(armatures_imported) != 1:
            self.report(
                {"ERROR"},
                "Imported file contains {:d} armatures, expected 1.".format(
                    len(armatures_imported)
                ),
            )
            clear_imported()
            return {"FINISHED"}

        ao_imp = armatures_imported[0]

        err_mappings = get_mapping_error_bones(bpy.data.objects[armature_name], ao_imp)
        if len(err_mappings) > 0:
            self.report(
                {"ERROR"},
                "Cannot map rig, the following bones are missing from the source rig: {}.".format(
                    ", ".join(err_mappings)
                ),
            )
            clear_imported()
            return {"FINISHED"}

        print(dir(bpy.context.scene))
        bpy.context.view_layer.objects.active = ao_imp

        # check that the ao contains anim data
        from ..core.utils import get_action_fcurves

        fcurves = (
            get_action_fcurves(ao_imp.animation_data.action)
            if ao_imp.animation_data and ao_imp.animation_data.action
            else []
        )
        if not ao_imp.animation_data or not ao_imp.animation_data.action or not fcurves:
            self.report({"ERROR"}, "Imported armature contains no animation data.")
            clear_imported()
            return {"FINISHED"}

        # get keyframes + boundary timestamps
        fcurves = get_action_fcurves(ao_imp.animation_data.action)
        kp_frames = []
        for key in fcurves:
            kp_frames += [kp.co.x for kp in key.keyframe_points]
        if len(kp_frames) <= 0:
            self.report({"ERROR"}, "Imported armature contains no keyframes.")
            clear_imported()
            return {"FINISHED"}

        # set frame range
        bpy.context.scene.frame_start = math.floor(min(kp_frames))
        bpy.context.scene.frame_end = math.ceil(max(kp_frames))

        # for the imported rig, apply ao transforms
        apply_ao_transform(ao_imp)

        prepare_for_kf_map()

        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        try:
            armature = bpy.data.objects[armature_name]
        except KeyError:
            self.report(
                {"ERROR"},
                f"No object named '{armature_name}' found. Please ensure the correct rig is selected.",
            )
            return {"FINISHED"}

        if armature.animation_data is None:
            self.report(
                {"ERROR"},
                f"The object '{armature_name}' has no animation data. Please ensure the correct rig is selected.",
            )
            return {"FINISHED"}

        # actually copy state
        copy_anim_state(bpy.data.objects[armature_name], ao_imp)

        clear_imported()
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}
