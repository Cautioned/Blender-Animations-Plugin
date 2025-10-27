"""
Rig generation and management operators.
"""

import bpy
from bpy.types import Operator
from ..rig.creation import create_rig
from ..rig.ik import create_ik_config, remove_ik_config
from ..core.utils import pose_bone_selected, pose_bone_set_selected


class OBJECT_OT_GenRig(bpy.types.Operator):
    bl_label = "Generate rig"
    bl_idname = "object.rbxanims_genrig"
    bl_description = "Generate rig from selected or available rig meta object"

    # A class-level cache to hold the dynamically generated list of rig items.
    # This is a standard pattern to work around Blender's UI caching for EnumProperties.
    rig_meta_items_cache = []

    def get_rig_meta_items(self, context):
        """Callback function for the EnumProperty. Returns the cached list."""
        return OBJECT_OT_GenRig.rig_meta_items_cache

    pr_rig_meta_name: bpy.props.EnumProperty(
        items=get_rig_meta_items,
        name="Rig Data",
        description="Select the rig data to use for generation"
    )

    pr_rigging_type: bpy.props.EnumProperty(
        items=[
            ("RAW", "Nodes only", ""),
            ("LOCAL_AXIS_EXTEND", "Local axis aligned bones", ""),
            ("LOCAL_YAXIS_EXTEND", "Local Y-axis aligned bones", ""),
            ("CONNECT", "Connect", ""),
        ],
        name="Rigging type",
    )

    def has_roblox_rig(self):
        """Check if scene has either rig meta objects or armatures with HumanoidRootPart bones"""
        # Check for rig meta objects (existing method)
        has_rig_meta = any("RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name for obj in bpy.data.objects)

        # Check for armatures with HumanoidRootPart bone (new detection method)
        has_humanoid_rig = any(
            obj.type == 'ARMATURE' and any(bone.name.lower() == "humanoidrootpart" for bone in obj.data.bones)
            for obj in bpy.data.objects
        )

        return has_rig_meta or has_humanoid_rig

    @classmethod
    def poll(cls, context):
        # Check if scene has either rig meta objects or armatures with HumanoidRootPart bones
        return any("RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name for obj in bpy.data.objects) or \
               any(obj.type == 'ARMATURE' and any(bone.name.lower() == "humanoidrootpart" for bone in obj.data.bones)
                   for obj in bpy.data.objects)

    def create_rig_meta_from_armature(self, armature_obj):
        """Create a temporary rig meta object from an armature with HumanoidRootPart"""
        # Generate a basic rig structure based on the armature
        rig_structure = {
            "rigName": armature_obj.name.replace("_Armature", "").replace("Armature", ""),
            "rig": {
                "jname": "HumanoidRootPart",
                "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],  # Identity matrix
                "jointtransform0": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                "jointtransform1": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                "aux": ["HumanoidRootPart"],
                "children": []
            }
        }

        # Create temporary meta object
        meta_obj_name = f"__{rig_structure['rigName']}Meta_Detected"
        if meta_obj_name in bpy.data.objects:
            bpy.data.objects.remove(bpy.data.objects[meta_obj_name], do_unlink=True)

        bpy.ops.object.add(type='EMPTY', location=(0, 0, 0))
        temp_meta = bpy.context.object
        temp_meta.name = meta_obj_name
        temp_meta["RigMeta"] = str(rig_structure).replace("'", '"')  # Convert to JSON-like string

        return meta_obj_name

    def execute(self, context):
        try:
            # Check if the selected item is a rig meta object or an armature
            selected_obj = bpy.data.objects.get(self.pr_rig_meta_name)
            if selected_obj and "RigMeta" in selected_obj:
                # Existing case: rig meta object
                create_rig(self.pr_rigging_type, self.pr_rig_meta_name)
                self.report({"INFO"}, f"Rig rebuilt from {self.pr_rig_meta_name}.")
            elif selected_obj and selected_obj.type == 'ARMATURE' and any(bone.name.lower() == "humanoidrootpart" for bone in selected_obj.data.bones):
                # New case: armature with HumanoidRootPart
                meta_obj_name = self.create_rig_meta_from_armature(selected_obj)
                create_rig(self.pr_rigging_type, meta_obj_name)
                # Clean up temporary meta object
                if meta_obj_name in bpy.data.objects:
                    bpy.data.objects.remove(bpy.data.objects[meta_obj_name], do_unlink=True)
                self.report({"INFO"}, f"Rig rebuilt from detected armature {self.pr_rig_meta_name}.")
            else:
                raise ValueError(f"Invalid rig source: {self.pr_rig_meta_name}")
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}

    def invoke(self, context, event):
        self.pr_rigging_type = "LOCAL_YAXIS_EXTEND"

        wm = context.window_manager
        
        # --- DYNAMIC ENUM POPULATION ---
        # Clear the old list and rebuild it from the current scene state.
        # This ensures the list is fresh every time the operator is invoked.
        OBJECT_OT_GenRig.rig_meta_items_cache.clear()

        # Add rig meta objects (existing method)
        for obj in bpy.data.objects:
            if "RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name:
                item = (obj.name, obj.name, "")
                OBJECT_OT_GenRig.rig_meta_items_cache.append(item)

        # Add armatures with HumanoidRootPart bones (new detection method)
        for obj in bpy.data.objects:
            if obj.type == 'ARMATURE' and any(bone.name.lower() == "humanoidrootpart" for bone in obj.data.bones):
                # Create a display name that indicates this is a detected rig
                display_name = f"{obj.name} (Detected Roblox Rig)"
                item = (obj.name, display_name, "Detected via HumanoidRootPart bone")
                OBJECT_OT_GenRig.rig_meta_items_cache.append(item)
        
        return wm.invoke_props_dialog(self)


class OBJECT_OT_GenIK(bpy.types.Operator):
    bl_label = "Generate IK"
    bl_idname = "object.rbxanims_genik"
    bl_description = "Generate IK"
    
    pr_chain_count: bpy.props.IntProperty(
        name="Chain count (0 = to root)", min=0, default=1)
    pr_create_pose_bone: bpy.props.BoolProperty(
        name="Create pose bone", default=False)
    pr_lock_tail_bone: bpy.props.BoolProperty(
        name="Lock final bone orientation", default=False)
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.mode == 'POSE'
            and obj.type == 'ARMATURE'
            and any(pose_bone_selected(b) for b in obj.pose.bones)
        )

    def execute(self, context):
        obj = context.active_object
        selected_bones = [b for b in obj.pose.bones if pose_bone_selected(b)]

        created_helper_names = []
        for bone in selected_bones:
            ik_name, pole_name = create_ik_config(
                obj, bone, self.pr_chain_count, self.pr_create_pose_bone, self.pr_lock_tail_bone
            )
            created_helper_names.append(ik_name)
            if pole_name:
                created_helper_names.append(pole_name)

        # post-create ux: select created helpers and focus
        bpy.context.view_layer.objects.active = obj
        if obj.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')
        # clear existing selection
        for pb in obj.pose.bones:
            pose_bone_set_selected(pb, False)
        # select helpers
        for name in created_helper_names:
            pb = obj.pose.bones.get(name)
            if pb:
                pose_bone_set_selected(pb, True)
                obj.data.bones.active = pb.bone
        try:
            bpy.ops.view3d.view_selected()
        except Exception:
            pass

        self.report({'INFO'}, f"created {len(created_helper_names)} ik helpers")
        return {'FINISHED'}

    def invoke(self, context, event):
        obj = context.active_object
        selected_bones = [b for b in obj.pose.bones if pose_bone_selected(b)]

        if not selected_bones:
            self.report({'WARNING'}, "No bones selected")
            return {'CANCELLED'}
        
        rec_chain_len = 1
        no_loop_mech = set()
        bone = selected_bones[0].bone
        while bone and bone.parent and len(bone.parent.children) == 1 and bone not in no_loop_mech:
            rec_chain_len += 1
            no_loop_mech.add(bone)
            bone = bone.parent
        
        self.pr_chain_count = rec_chain_len

        wm = context.window_manager
        return wm.invoke_props_dialog(self)


class OBJECT_OT_RemoveIK(bpy.types.Operator):
    bl_label = "Remove IK"
    bl_idname = "object.rbxanims_removeik"
    bl_description = "Remove IK"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.mode == 'POSE' and any(pose_bone_selected(b) for b in obj.pose.bones)

    def execute(self, context):
        obj = context.active_object
        selected_bones = [b for b in obj.pose.bones if pose_bone_selected(b)]
        
        for bone in selected_bones:
            remove_ik_config(obj, bone)
            
        return {'FINISHED'}


class OBJECT_OT_ToggleIKHelpers(bpy.types.Operator):
    bl_idname = "object.rbxanims_toggle_ik_helpers"
    bl_label = "Toggle IK Helpers Visibility"
    bl_description = "Show/Hide all IK target/pole bones on the active armature"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'ARMATURE'

    def execute(self, context):
        obj = context.active_object
        arm = obj.data
        # decide desired toggle state based on first helper found
        helper_bone_names = [b.name for b in arm.bones if b.name.endswith("-IKTarget") or b.name.endswith("-IKPole")]
        pose_helpers = [obj.pose.bones.get(name) for name in helper_bone_names]
        pose_helpers = [pb for pb in pose_helpers if pb is not None]

        if not pose_helpers:
            self.report({'INFO'}, "no ik helpers found")
            return {'CANCELLED'}

        target_hide = not any(getattr(pb, "hide", False) for pb in pose_helpers)
        for pb in pose_helpers:
            try:
                pb.hide = target_hide
            except AttributeError:
                # fall back to legacy edit bone hide when pose property missing (older blender)
                edit_bone = arm.bones.get(pb.name)
                if edit_bone is not None:
                    edit_bone.hide = target_hide
        self.report({'INFO'}, ("hidden" if target_hide else "shown") + " ik helpers")
        return {'FINISHED'}
