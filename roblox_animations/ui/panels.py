"""
UI panels for the Roblox Animations Blender Addon.
"""

import bpy
from ..animation.serialization import is_deform_bone_rig
from ..server.server import get_server_status


class OBJECT_PT_RbxAnimations(bpy.types.Panel):
    bl_label = "Rbx Animations"
    bl_idname = "OBJECT_PT_RbxAnimations"
    bl_category = "Rbx Animations"  # Create a dedicated tab
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context):
        # Always show the panel
        return True

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # --- 1. SETUP & IMPORT ---
        setup_box = layout.box()
        setup_box.label(text="Setup", icon='TOOL_SETTINGS')

        rig_meta_exists = any(
            "RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name
            for obj in bpy.data.objects
        )

        # Also check for armatures with HumanoidRootPart bones
        humanoid_rig_exists = any(
            obj.type == 'ARMATURE' and any(bone.name.lower() == "humanoidrootpart" for bone in obj.data.bones)
            for obj in bpy.data.objects
        )

        roblox_rig_exists = rig_meta_exists or humanoid_rig_exists
        
        if not roblox_rig_exists:
            setup_box.label(text="No Roblox Rig Project Found.", icon='INFO')
            row = setup_box.row()
            row.scale_y = 1.5
            row.operator("object.rbxanims_importmodel",
                         text="Import Rig (.obj)", icon='IMPORT')
        else:
            setup_box.operator("object.rbxanims_importmodel",
                               text="Import New Rig (.obj)", icon='IMPORT')

        # This operator's poll method correctly handles disabling it.
        setup_box.operator("object.rbxanims_genrig",
                           text="Generate Armature", icon='ARMATURE_DATA')
        
        

        # --- 2. LIVE-SYNC SERVER & UPDATES ---
        server_box = layout.box()
        row = server_box.row(align=True)  # Align row elements
        row.label(text="Live Sync", icon='WORLD_DATA')
        row.operator("my_plugin.update", text="", icon='FILE_REFRESH')

        row = server_box.row(align=True)
        if not get_server_status():
            row.operator("object.start_server",
                         text="Start Server", icon='PLAY')
        else:
            row.operator("object.stop_server",
                         text="Stop Server", icon='PAUSE')
        row.prop(scene, "rbx_server_port", text="")

        # --- 3. ARMATURE OPERATIONS ---
        layout.separator()
        armatures_exist = any(
            obj.type == 'ARMATURE' for obj in bpy.data.objects)

        # Check if any armatures have HumanoidRootPart (Roblox rigs)
        roblox_armatures_exist = any(
            obj.type == 'ARMATURE' and any(bone.name.lower() == "humanoidrootpart" for bone in obj.data.bones)
            for obj in bpy.data.objects
        )

        armature_ops_box = layout.box()
        
        if not armatures_exist:
            armature_ops_box.label(text="No Armatures in Scene", icon='INFO')
            return

        armature_ops_box.label(text="Armature Operations")
        armature_ops_box.prop(scene, "rbx_anim_armature", text="Target")

        selected_armature = bpy.data.objects.get(scene.rbx_anim_armature)

        inner_box = armature_ops_box.box()
        inner_box.enabled = selected_armature is not None

        if not selected_armature:
            inner_box.label(
                text="Select an Armature to continue.", icon='INFO')
            return

        # Use the same logic as the serializer to determine if this is a deform rig.
        # This ensures UI consistency with the export behavior.
        has_new_bones = any(
            not ("transform" in bone.bone and "transform1" in bone.bone and "nicetransform" in bone.bone)
            for bone in selected_armature.pose.bones
        )
        is_skinned_rig = is_deform_bone_rig(selected_armature)
        run_deform_path = is_skinned_rig or scene.force_deform_bone_serialization or has_new_bones

        # --- Rigging Sub-panel ---
        rigging_box = inner_box.box()
        col = rigging_box.column()
        col.label(text="Rigging", icon="MOD_ARMATURE")
        col.operator("object.rbxanims_autoconstraint",
                     text="Constraint Matching Parts to Bones")
        col.operator("object.rbxanims_manualconstraint",
                     text="Manual Constraint Editor")
        ik_row = col.row(align=True)
        ik_row.operator("object.rbxanims_genik", text="Generate IK")
        ik_row.operator("object.rbxanims_removeik", text="Remove IK")
        if is_skinned_rig:
            col.label(text="Mesh (Deform) Rig Detected", icon="BONE_DATA")
        elif has_new_bones:
            col.label(text="Helper Bones Detected (treated as deform when exporting)", icon="INFO")
        else:
            col.label(text="Motor-style Rig", icon="POSE_HLT")

        # --- Animation Sub-panel ---
        animation_box = inner_box.box()
        col = animation_box.column()
        col.label(text="Animation", icon="ACTION")
        if run_deform_path:
            col.prop(scene, "rbx_deform_rig_scale", text="Deform Scale")
        col.operator("object.rbxanims_importfbxanimation",
                     text="Import from .fbx")
        col.operator("object.rbxanims_mapkeyframes",
                     text="Map from Active Rig")
        col.operator("object.rbxanims_applytransform",
                     text="Apply Object Transform")
        col.separator()
        col.operator("object.rbxanims_bake", text="Bake (Clipboard)", icon='EXPORT')
        col.operator("object.rbxanims_bake_file", text="Bake to File", icon='FILE')

        # Add the force deform serialization checkbox for testing
        dev_box = inner_box.box()
        # Add test button to setup section so it's always visible
        dev_box.separator()
        dev_box.operator("object.rbxanims_run_tests", text="Run Tests", icon='SCRIPT')
        # dev_box.label(text="Developer Options", icon='SCRIPT')
        # dev_box.prop(scene, "force_deform_bone_serialization", text="Force Deform Serialization")

        # --- Validation Sub-panel ---
        validation_box = inner_box.box()
        validation_box.label(text="UGC Emote Validation", icon='CHECKMARK')
        row = validation_box.row(align=True)
        row.prop(scene, "rbx_max_studs_per_frame", text="Max studs/frame")
        row = validation_box.row(align=True)
        row.operator("object.rbxanims_validate_motionpaths", text="Validate Motion Paths", icon='ANIM_DATA')
        row.operator("object.rbxanims_clear_motionpaths", text="Clear", icon='TRASH')


class OBJECT_PT_RbxAnimations_Tool(bpy.types.Panel):
    bl_label = "Rbx Animations"
    bl_idname = "OBJECT_PT_RbxAnimations_Tool"
    bl_category = "Tool"  # Add to the Tool tab
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context):
        # Always show the panel
        return True

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Essentials
        layout.operator("object.rbxanims_importmodel",
                        text="Import Rig (.obj)", icon='IMPORT')
        layout.operator("object.rbxanims_genrig",
                        text="Generate Armature", icon='ARMATURE_DATA')

        layout.separator()

        armatures_exist = any(
            obj.type == 'ARMATURE' for obj in bpy.data.objects)
        if not armatures_exist:
            return

        layout.prop(scene, "rbx_anim_armature", text="Rig")
        selected_armature = bpy.data.objects.get(scene.rbx_anim_armature)

        row = layout.row(align=True)
        row.enabled = selected_armature is not None
        row.operator("object.rbxanims_bake", text="Bake", icon='EXPORT')
        row.operator("object.rbxanims_bake_file",
                     text="Bake to File", icon='FILE_TICK')

        layout.separator()
        layout.label(text="See 'Rbx Animations' panel for more options")
