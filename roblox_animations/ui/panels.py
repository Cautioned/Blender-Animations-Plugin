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
        setup_box.label(text="Setup", icon="TOOL_SETTINGS")

        rig_meta_exists = any(
            "RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name
            for obj in bpy.data.objects
        )

        # Also check for armatures with Motor6D properties
        motor6d_rig_exists = any(
            obj.type == "ARMATURE"
            and any(
                "transform" in bone and "transform1" in bone and "nicetransform" in bone
                for bone in obj.data.bones
            )
            for obj in bpy.data.objects
        )

        roblox_rig_exists = rig_meta_exists or motor6d_rig_exists

        if not roblox_rig_exists:
            setup_box.label(text="No Roblox Rig Project Found.", icon="INFO")
            row = setup_box.row()
            row.scale_y = 1.5
            row.operator(
                "object.rbxanims_importmodel", text="Import Rig (.obj)", icon="IMPORT"
            )
        else:
            setup_box.operator(
                "object.rbxanims_importmodel",
                text="Import New Rig (.obj)",
                icon="IMPORT",
            )

        # This operator's poll method correctly handles disabling it.
        setup_box.operator(
            "object.rbxanims_genrig", text="Generate Armature", icon="ARMATURE_DATA"
        )

        # --- 2. LIVE-SYNC SERVER & UPDATES ---
        server_box = layout.box()
        row = server_box.row(align=True)  # Align row elements
        row.label(text="Connection", icon="WORLD_DATA")

        row = server_box.row(align=True)
        if not get_server_status():
            row.operator("object.start_server", text="Start Server", icon="PLAY")
        else:
            row.operator("object.stop_server", text="Stop Server", icon="PAUSE")
        settings = getattr(scene, "rbx_anim_settings", None)
        row.prop(settings, "rbx_server_port", text="")

        # --- 3. ARMATURE OPERATIONS ---
        layout.separator()
        armatures_exist = any(obj.type == "ARMATURE" for obj in bpy.data.objects)

        # Check if any armatures have Motor6D properties (Roblox rigs)
        any(
            obj.type == "ARMATURE"
            and any(
                "transform" in bone and "transform1" in bone and "nicetransform" in bone
                for bone in obj.data.bones
            )
            for obj in bpy.data.objects
        )

        armature_ops_box = layout.box()

        if not armatures_exist:
            armature_ops_box.label(text="No Armatures in Scene", icon="INFO")
            return

        armature_ops_box.label(text="Armature Operations")
        armature_ops_box.prop(settings, "rbx_anim_armature", text="Target")

        selected_armature = (
            bpy.data.objects.get(settings.rbx_anim_armature) if settings else None
        )

        inner_box = armature_ops_box.box()
        inner_box.enabled = selected_armature is not None

        if not selected_armature:
            inner_box.label(text="Select an Armature to continue.", icon="INFO")
            return

        # Use the same logic as the serializer to determine if this is a deform rig.
        # This ensures UI consistency with the export behavior.
        has_new_bones = any(
            not (
                "transform" in bone.bone
                and "transform1" in bone.bone
                and "nicetransform" in bone.bone
            )
            for bone in selected_armature.pose.bones
        )
        is_skinned_rig = is_deform_bone_rig(selected_armature)
        force_deform = getattr(settings, "force_deform_bone_serialization", False)
        run_deform_path = is_skinned_rig or force_deform or has_new_bones

        # --- Rigging Sub-panel ---
        rigging_box = inner_box.box()
        col = rigging_box.column()
        col.label(text="Rigging", icon="MOD_ARMATURE")
        col.operator(
            "object.rbxanims_autoconstraint", text="Constraint Matching Parts to Bones"
        )
        col.operator(
            "object.rbxanims_manualconstraint", text="Manual Constraint Editor"
        )
        ik_row = col.row(align=True)
        # check if selected bones have IK constraints
        has_ik = False
        selected_ik_target = None
        if selected_armature and selected_armature.mode == "POSE":
            from ..rig.ik import has_ik_constraint
            from ..core.utils import pose_bone_selected
            for b in selected_armature.pose.bones:
                if pose_bone_selected(b):
                    if has_ik_constraint(selected_armature, b):
                        has_ik = True
                    # Check if this is an IK target bone with IK_FK property
                    if b.name.endswith("-IKTarget") and "IK_FK" in b:
                        selected_ik_target = b
        
        if has_ik:
            ik_row.operator("object.rbxanims_modifyik", text="Modify IK")
        else:
            ik_row.operator("object.rbxanims_genik", text="Generate IK")
        ik_row.operator("object.rbxanims_removeik", text="Remove IK")
        
        # Show IK-FK slider if an IK target with the property is selected
        if selected_ik_target:
            ikfk_box = col.box()
            ikfk_row = ikfk_box.row(align=True)
            ikfk_row.label(text="IK-FK:", icon="CON_KINEMATIC")
            ikfk_row.prop(selected_ik_target, '["IK_FK"]', text="", slider=True)
            # Add quick toggle buttons
            toggle_row = ikfk_box.row(align=True)
            toggle_row.operator("object.rbxanims_set_ikfk", text="IK").value = 1.0
            toggle_row.operator("object.rbxanims_set_ikfk", text="FK").value = 0.0
        
        # --- Center of Mass Sub-section ---
        col.separator()
        com_row = col.row(align=True)
        com_row.label(text="Center of Mass:", icon="PIVOT_MEDIAN")
        
        # Check if COM is enabled for THIS armature
        try:
            from ..rig.com import is_com_for_armature, is_com_grid_enabled
            obj = context.active_object
            com_enabled = is_com_for_armature(obj) if obj else False
            grid_enabled = is_com_grid_enabled() if com_enabled else False
        except:
            com_enabled = False
            grid_enabled = False
        
        com_toggle = com_row.operator(
            "object.rbxanims_toggle_com", 
            text="", 
            icon="HIDE_OFF" if com_enabled else "HIDE_ON",
            depress=com_enabled
        )
        
        # Grid toggle (only visible when COM is enabled)
        if com_enabled:
            com_row.operator(
                "object.rbxanims_toggle_com_grid",
                text="",
                icon="MESH_CIRCLE" if grid_enabled else "MESH_CIRCLE",
                depress=grid_enabled
            )
        
        com_actions = col.row(align=True)
        
        # Check if selected bone is the current pivot
        pivot_text = "Set Pivot"
        try:
            from ..rig.com import is_bone_com_pivot
            obj = context.active_object
            if context.mode == 'POSE' and obj and obj.pose and context.active_pose_bone:
                if is_bone_com_pivot(context.active_pose_bone.name):
                    pivot_text = "Unset Pivot"
        except:
            pass
        
        com_actions.operator("object.rbxanims_set_com_pivot", text=pivot_text)
        com_actions.operator("object.rbxanims_edit_com_weights", text="Weights")
        
        # --- AutoPhysics Sub-section ---
        col.separator()
        physics_row = col.row(align=True)
        physics_row.label(text="AutoPhysics:", icon="PHYSICS")
        
        # Check if AutoPhysics is enabled
        try:
            from ..rig.physics import is_physics_enabled, is_ghost_enabled, get_frame_state
            physics_enabled = is_physics_enabled()
            ghost_enabled = is_ghost_enabled() if physics_enabled else False
        except:
            physics_enabled = False
            ghost_enabled = False
        
        physics_row.operator(
            "object.rbxanims_toggle_autophysics",
            text="",
            icon="PLAY" if not physics_enabled else "PAUSE",
            depress=physics_enabled
        )
        
        if physics_enabled:
            # Ghost toggle
            physics_row.operator(
                "object.rbxanims_toggle_physics_ghost",
                text="",
                icon="GHOST_ENABLED" if ghost_enabled else "GHOST_DISABLED",
                depress=ghost_enabled
            )
            
            # Gravity scale slider
            if settings:
                col.prop(settings, "rbx_physics_gravity", text="Gravity")
            
            # Show current frame state
            try:
                frame = context.scene.frame_current
                state = get_frame_state(frame)
                state_icons = {
                    "grounded": "CHECKMARK",
                    "airborne": "SORT_DESC",
                    "invalid": "ERROR",
                    "unknown": "QUESTION",
                }
                state_colors = {
                    "grounded": "Grounded",
                    "airborne": "Airborne",
                    "invalid": "Invalid",
                    "unknown": "Unknown",
                }
                col.label(text=f"Frame {frame}: {state_colors.get(state, state)}", 
                         icon=state_icons.get(state, "QUESTION"))
            except:
                pass
            
            # Re-analyze button
            col.operator("object.rbxanims_analyze_physics", text="Re-analyze", icon="FILE_REFRESH")
            
            # COM manipulation tools
            col.separator()
            com_tools = col.row(align=True)
            com_tools.operator("rbx.com_gizmo_modal", text="Move COM", icon="ORIENTATION_CURSOR")
            com_tools.operator("rbx.snap_rig_to_ground", text="", icon="IMPORT")
        
        col.separator()
        if is_skinned_rig:
            col.label(text="Mesh (Deform) Rig Detected", icon="BONE_DATA")
        elif has_new_bones:
            col.label(
                text="Helper Bones Detected (treated as deform when exporting)",
                icon="INFO",
            )
        else:
            col.label(text="Motor-style Rig", icon="POSE_HLT")

        # --- Animation Sub-panel ---
        animation_box = inner_box.box()
        col = animation_box.column()
        col.label(text="Animation", icon="ACTION")
        if run_deform_path and settings:
            col.prop(settings, "rbx_deform_rig_scale", text="Deform Scale")
        col.operator("object.rbxanims_importfbxanimation", text="Import from .fbx")
        col.operator("object.rbxanims_mapkeyframes", text="Map from Active Rig")
        col.operator("object.rbxanims_applytransform", text="Apply Object Transform")
        if settings:
            col.prop(settings, "rbx_full_range_bake", text="Full Range Bake")
        col.separator()
        col.operator("object.rbxanims_bake", text="Bake (Clipboard)", icon="EXPORT")
        col.operator("object.rbxanims_bake_file", text="Bake to File", icon="FILE")

        # Add the force deform serialization checkbox for testing
        dev_box = inner_box.box()
        # Add test button to setup section so it's always visible
        dev_box.label(text="Developer Options", icon='SCRIPT')
        dev_box.separator()
        dev_box.operator("object.rbxanims_run_tests", text="Run Tests", icon='SCRIPT')
        
        # dev_box.prop(scene, "force_deform_bone_serialization", text="Force Deform Serialization")

        # --- Validation Sub-panel ---
        validation_box = inner_box.box()
        validation_box.label(text="UGC Emote Validation", icon="CHECKMARK")
        row = validation_box.row(align=True)
        if settings:
            row.prop(settings, "rbx_max_studs_per_frame", text="Max studs/frame")
        row = validation_box.row(align=True)
        row.operator(
            "object.rbxanims_validate_motionpaths",
            text="Validate Motion Paths",
            icon="ANIM_DATA",
        )
        row.operator("object.rbxanims_clear_motionpaths", text="Clear", icon="TRASH")


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
        settings = getattr(scene, "rbx_anim_settings", None)

        # Essentials
        layout.operator(
            "object.rbxanims_importmodel", text="Import Rig (.obj)", icon="IMPORT"
        )
        layout.operator(
            "object.rbxanims_genrig", text="Generate Armature", icon="ARMATURE_DATA"
        )

        layout.separator()

        armatures_exist = any(obj.type == "ARMATURE" for obj in bpy.data.objects)
        if not armatures_exist:
            return

        layout.prop(settings, "rbx_anim_armature", text="Rig")
        selected_armature = (
            bpy.data.objects.get(settings.rbx_anim_armature) if settings else None
        )

        row = layout.row(align=True)
        row.enabled = selected_armature is not None
        row.operator("object.rbxanims_bake", text="Bake", icon="EXPORT")
        row.operator("object.rbxanims_bake_file", text="Bake to File", icon="FILE_TICK")

        layout.separator()
        layout.label(text="See 'Rbx Animations' panel for more options")
