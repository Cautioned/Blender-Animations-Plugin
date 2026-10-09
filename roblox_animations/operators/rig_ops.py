"""
Rig generation and management operators.
"""

import bpy
from ..rig.creation import create_rig
from ..rig.ik import create_ik_config, remove_ik_config, has_ik_constraint, update_pole_axis
from ..core.utils import (
    pose_bone_selected,
    pose_bone_set_selected,
    iter_scene_objects,
    get_object_by_name,
    find_master_collection_for_object,
)


def _resolve_autophysics_armature(context):
    settings = getattr(getattr(context, "scene", None), "rbx_anim_settings", None)
    armature_name = getattr(settings, "rbx_anim_armature", "") if settings else ""
    if armature_name:
        armature = get_object_by_name(armature_name)
        if armature and armature.type == "ARMATURE":
            return armature

    obj = getattr(context, "active_object", None)
    if obj and obj.type == "ARMATURE":
        return obj

    return None


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
        description="Select the rig data to use for generation",
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
        """Check if scene has either rig meta objects or armatures with Motor6D properties"""
        # Check for rig meta objects (existing method)
        has_rig_meta = any(
            "RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name
            for obj in iter_scene_objects()
        )

        # Check for armatures with Motor6D properties (transform, transform1, nicetransform)
        has_motor6d_rig = any(
            obj.type == "ARMATURE"
            and any(
                "transform" in bone and "transform1" in bone and "nicetransform" in bone
                for bone in obj.data.bones
            )
            for obj in iter_scene_objects()
        )

        return has_rig_meta or has_motor6d_rig

    @classmethod
    def poll(cls, context):
        # Check if scene has either rig meta objects or armatures with Motor6D properties
        return any(
            "RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name
            for obj in iter_scene_objects(context.scene)
        ) or any(
            obj.type == "ARMATURE"
            and any(
                "transform" in bone and "transform1" in bone and "nicetransform" in bone
                for bone in obj.data.bones
            )
            for obj in iter_scene_objects(context.scene)
        )

    def resolve_rig_meta_from_armature(self, armature_obj):
        """Use the import's complete metadata, including skin and part bindings."""
        master = find_master_collection_for_object(armature_obj)
        if master:
            candidates = [
                obj for obj in master.all_objects
                if "RigMeta" in obj
                and find_master_collection_for_object(obj) == master
            ]
            source_name = armature_obj.get("rbx_rig_meta_name")
            for candidate in candidates:
                if candidate.name == source_name:
                    return candidate.name
            # Older imports have no source tag. Their owning collection still
            # identifies the metadata without guessing from object names.
            if len(candidates) == 1:
                return candidates[0].name
        raise ValueError(
            "Original rig metadata could not be identified. Reimport the Roblox "
            "model before rebuilding this armature."
        )

    def execute(self, context):
        try:
            # Check if the selected item is a rig meta object or an armature
            selected_obj = get_object_by_name(self.pr_rig_meta_name, context.scene)
            if selected_obj and "RigMeta" in selected_obj:
                # Existing case: rig meta object
                create_rig(self.pr_rigging_type, self.pr_rig_meta_name)
                self.report({"INFO"}, f"Rig rebuilt from {self.pr_rig_meta_name}.")
            elif (
                selected_obj
                and selected_obj.type == "ARMATURE"
                and any(
                    "transform" in bone and "transform1" in bone and "nicetransform" in bone
                    for bone in selected_obj.data.bones
                )
            ):
                meta_obj_name = self.resolve_rig_meta_from_armature(selected_obj)
                create_rig(self.pr_rigging_type, meta_obj_name, target_armature=selected_obj)
                self.report(
                    {"INFO"},
                    f"Rig rebuilt from detected armature {self.pr_rig_meta_name}.",
                )
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
        for obj in iter_scene_objects(context.scene):
            if "RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name:
                item = (obj.name, obj.name, "")
                OBJECT_OT_GenRig.rig_meta_items_cache.append(item)

        # Add armatures with Motor6D properties (new detection method)
        for obj in iter_scene_objects(context.scene):
            if obj.type == "ARMATURE" and any(
                "transform" in bone and "transform1" in bone and "nicetransform" in bone
                for bone in obj.data.bones
            ):
                # Create a display name that indicates this is a detected rig
                display_name = f"{obj.name} (Detected Motor6D Rig)"
                item = (obj.name, display_name, "Detected via Motor6D properties")
                OBJECT_OT_GenRig.rig_meta_items_cache.append(item)

        active = context.active_object
        if active and any(item[0] == active.name for item in self.rig_meta_items_cache):
            self.pr_rig_meta_name = active.name
        return wm.invoke_props_dialog(self)


class OBJECT_OT_GenIK(bpy.types.Operator):
    bl_label = "Generate IK"
    bl_idname = "object.rbxanims_genik"
    bl_description = "Generate IK"

    pr_chain_count: bpy.props.IntProperty(
        name="Chain count (0 = to root)", min=0, default=1
    )
    pr_create_pose_bone: bpy.props.BoolProperty(name="Create pose bone", default=False)
    pr_lock_tail_bone: bpy.props.BoolProperty(
        name="Lock final bone orientation", default=False
    )
    pr_copy_rotation: bpy.props.BoolProperty(
        name="Copy IK control rotation to foot", default=False,
        description="Makes the last bone copy the IK control's rotation (useful for foot and hand controls)"
    )
    pr_enable_stretch: bpy.props.BoolProperty(
        name="Enable IK Stretch", default=False,
        description="Add slight stretch when fully extended to prevent knee/elbow popping"
    )
    pr_max_stretch: bpy.props.FloatProperty(
        name="Max Stretch", default=1.05, min=1.0, max=1.2,
        description="Maximum stretch factor (1.05 = 5% stretch)"
    )
    pr_enable_ik_fk_switch: bpy.props.BoolProperty(
        name="Enable IK-FK Switch", default=False,
        description="Add a custom property to blend between IK and FK modes"
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.mode == "POSE"
            and obj.type == "ARMATURE"
            and any(pose_bone_selected(b) for b in obj.pose.bones)
        )

    def execute(self, context):
        obj = context.active_object
        selected_bones = [b for b in obj.pose.bones if pose_bone_selected(b)]

        created_helper_names = []
        for bone in selected_bones:
            ik_name, pole_name = create_ik_config(
                obj,
                bone,
                self.pr_chain_count,
                self.pr_create_pose_bone,
                self.pr_lock_tail_bone,
                self.pr_copy_rotation,
                self.pr_enable_stretch,
                self.pr_max_stretch,
                self.pr_enable_ik_fk_switch,
            )
            created_helper_names.append(ik_name)
            if pole_name:
                created_helper_names.append(pole_name)

        # post-create ux: select created helpers and focus
        bpy.context.view_layer.objects.active = obj
        if obj.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")
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

        self.report({"INFO"}, f"created {len(created_helper_names)} ik helpers")
        return {"FINISHED"}

    def invoke(self, context, event):
        obj = context.active_object
        selected_bones = [b for b in obj.pose.bones if pose_bone_selected(b)]

        if not selected_bones:
            self.report({"WARNING"}, "No bones selected")
            return {"CANCELLED"}

        rec_chain_len = 1
        no_loop_mech = set()
        bone = selected_bones[0].bone
        while (
            bone
            and bone.parent
            and len(bone.parent.children) == 1
            and bone not in no_loop_mech
        ):
            rec_chain_len += 1
            no_loop_mech.add(bone)
            bone = bone.parent

        self.pr_chain_count = rec_chain_len

        wm = context.window_manager
        return wm.invoke_props_dialog(self)


class OBJECT_OT_ModifyIK(bpy.types.Operator):
    bl_label = "Modify IK"
    bl_idname = "object.rbxanims_modifyik"
    bl_description = "Modify existing IK constraints (change pole axis for arms)"
    bl_options = {'REGISTER', 'UNDO'}

    pr_pole_axis: bpy.props.EnumProperty(
        name="Pole Axis",
        items=[
            ("+X", "+X", "Positive X axis"),
            ("-X", "-X", "Negative X axis"),
            ("+Y", "+Y", "Positive Y axis"),
            ("-Y", "-Y", "Negative Y axis"),
            ("+Z", "+Z", "Positive Z axis"),
            ("-Z", "-Z", "Negative Z axis"),
        ],
        default="+X",
        description="Set the pole bone axis direction"
    )

    def do_update_pole_axis(self, context):
        """Update pole axis using the update_pole_axis function from ik module"""
        from mathutils import Vector

        obj = context.active_object
        if not obj or obj.type != "ARMATURE":
            return

        # Get selected bones
        selected_bones = [b for b in obj.pose.bones if pose_bone_selected(b)]
        if not selected_bones:
            if hasattr(context, 'active_pose_bone') and context.active_pose_bone:
                selected_bones = [context.active_pose_bone]

        if not selected_bones:
            return

        # Map axis string to vector
        axis_map = {
            "+X": Vector((1, 0, 0)),
            "-X": Vector((-1, 0, 0)),
            "+Y": Vector((0, 1, 0)),
            "-Y": Vector((0, -1, 0)),
            "+Z": Vector((0, 0, 1)),
            "-Z": Vector((0, 0, -1)),
        }

        target_axis = axis_map.get(self.pr_pole_axis, Vector((1, 0, 0)))

        # Update pole for each selected bone with IK
        for pose_bone in selected_bones:
            update_pole_axis(obj, pose_bone, target_axis)

        context.view_layer.update()

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.mode != "POSE" or obj.type != "ARMATURE":
            return False
        return any(
            has_ik_constraint(obj, b) for b in obj.pose.bones if pose_bone_selected(b)
        )

    def execute(self, context):
        # Always call do_update_pole_axis to apply the changes
        try:
            self.do_update_pole_axis(context)
            self.report({"INFO"}, f"IK pole axis updated to {self.pr_pole_axis}")
        except Exception as e:
            self.report({"ERROR"}, f"Failed to update IK: {str(e)}")
            import traceback
            traceback.print_exc()
            return {"CANCELLED"}
        return {"FINISHED"}

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self)


class OBJECT_OT_RemoveIK(bpy.types.Operator):
    bl_label = "Remove IK"
    bl_idname = "object.rbxanims_removeik"
    bl_description = "Remove IK"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.mode == "POSE"
            and any(pose_bone_selected(b) for b in obj.pose.bones)
        )

    def execute(self, context):
        obj = context.active_object
        selected_bones = [b for b in obj.pose.bones if pose_bone_selected(b)]

        for bone in selected_bones:
            remove_ik_config(obj, bone)

        return {"FINISHED"}


class OBJECT_OT_SetIKFK(bpy.types.Operator):
    bl_label = "Set IK-FK"
    bl_idname = "object.rbxanims_set_ikfk"
    bl_description = "Quick toggle between IK and FK modes"
    bl_options = {'REGISTER', 'UNDO'}

    value: bpy.props.FloatProperty(
        name="IK-FK Value",
        default=1.0,
        min=0.0,
        max=1.0,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.mode != "POSE" or obj.type != "ARMATURE":
            return False
        # Check if any selected bone is an IK target with IK_FK property
        for b in obj.pose.bones:
            if pose_bone_selected(b) and b.name.endswith("-IKTarget") and "IK_FK" in b:
                return True
        return False

    def execute(self, context):
        obj = context.active_object
        current_frame = context.scene.frame_current

        for b in obj.pose.bones:
            if pose_bone_selected(b) and b.name.endswith("-IKTarget") and "IK_FK" in b:
                b["IK_FK"] = self.value
                # Insert keyframe for the IK_FK property
                b.keyframe_insert(data_path='["IK_FK"]', frame=current_frame)

        # Force update
        context.view_layer.update()
        return {"FINISHED"}


class OBJECT_OT_ToggleCOM(bpy.types.Operator):
    bl_label = "Toggle Center of Mass"
    bl_idname = "object.rbxanims_toggle_com"
    bl_description = "Toggle Center of Mass visualization in the viewport"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        from ..rig.com import (
            is_com_for_armature,
            enable_com_visualization,
            update_com_visualization,
            register_frame_handler,
            unregister_frame_handler,
            register_depsgraph_handler,
            unregister_depsgraph_handler,
        )

        obj = context.active_object

        # Check if COM is enabled for THIS armature specifically
        com_enabled_for_this = is_com_for_armature(obj)

        if com_enabled_for_this:
            # Turn off COM for this armature
            enable_com_visualization(False)
            unregister_frame_handler()
            unregister_depsgraph_handler()
            self.report({"INFO"}, "COM visualization disabled")
        else:
            # Turn on COM for this armature (will switch if another armature had it)
            enable_com_visualization(True)
            register_frame_handler()
            register_depsgraph_handler()
            update_com_visualization(obj)
            self.report({"INFO"}, f"COM visualization enabled for '{obj.name}'")

        return {"FINISHED"}


class OBJECT_OT_ToggleCOMGrid(bpy.types.Operator):
    bl_label = "Toggle COM Grid"
    bl_idname = "object.rbxanims_toggle_com_grid"
    bl_description = "Toggle the circular grid display at ground level"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        from ..rig.com import toggle_com_grid, is_com_grid_enabled

        toggle_com_grid()
        state = "enabled" if is_com_grid_enabled() else "disabled"
        self.report({"INFO"}, f"COM grid {state}")

        return {"FINISHED"}


class OBJECT_OT_EditCOMWeights(bpy.types.Operator):
    """Edit Center of Mass bone weights"""
    bl_label = "Edit COM Weights"
    bl_idname = "object.rbxanims_edit_com_weights"
    bl_description = "Edit bone weights for Center of Mass calculation"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        from ..rig.com import get_bone_weight, COM_WEIGHT_PROP

        # Initialize com_weight property on all bones BEFORE opening the dialog
        # This must be done here, not in draw(), because draw() doesn't allow writing
        obj = context.active_object
        if obj and obj.type == "ARMATURE":
            for bone in obj.data.bones:
                if COM_WEIGHT_PROP not in bone:
                    bone[COM_WEIGHT_PROP] = get_bone_weight(bone)

        wm = context.window_manager
        return wm.invoke_props_dialog(self, width=400)

    def draw(self, context):
        from ..rig.com import COM_WEIGHT_PROP, DEFAULT_BONE_WEIGHTS

        layout = self.layout
        obj = context.active_object

        layout.label(text="Bone COM Weights:", icon="BONE_DATA")
        layout.separator()

        # Categorize bones: custom weights, default weights (known), and zero/unknown
        custom_weight_bones = []
        default_weight_bones = []
        other_bones = []

        for bone in obj.data.bones:
            # Skip IK helper bones
            if any(bone.name.endswith(s) for s in ("-IKTarget", "-IKPole", "-IKStretch")):
                continue

            has_custom = COM_WEIGHT_PROP in bone
            has_default = bone.name in DEFAULT_BONE_WEIGHTS or any(
                k.lower() in bone.name.lower() for k in DEFAULT_BONE_WEIGHTS
            )

            if has_custom:
                custom_weight_bones.append(bone)
            elif has_default:
                default_weight_bones.append(bone)
            else:
                other_bones.append(bone)

        # Custom weights section
        if custom_weight_bones:
            box = layout.box()
            box.label(text="Custom Weights:", icon="MODIFIER")
            for bone in custom_weight_bones:
                row = box.row(align=True)
                row.label(text=bone.name)
                row.prop(bone, f'["{COM_WEIGHT_PROP}"]', text="")
                op = row.operator("object.rbxanims_reset_bone_weight", text="", icon="LOOP_BACK")
                op.bone_name = bone.name

        # Default weight bones (main body parts)
        if default_weight_bones:
            box = layout.box()
            box.label(text="Main Body Parts:", icon="ARMATURE_DATA")
            for bone in default_weight_bones:
                row = box.row(align=True)
                row.label(text=bone.name)
                row.prop(bone, f'["{COM_WEIGHT_PROP}"]', text="")
                op = row.operator("object.rbxanims_reset_bone_weight", text="", icon="LOOP_BACK")
                op.bone_name = bone.name

        # Other bones (accessories, extra bones)
        if other_bones:
            box = layout.box()
            col = box.column()
            col.label(text=f"Other Bones ({len(other_bones)}):", icon="BONE_DATA")

            for bone in other_bones[:15]:
                row = col.row(align=True)
                row.label(text=bone.name)
                row.prop(bone, f'["{COM_WEIGHT_PROP}"]', text="")
                op = row.operator("object.rbxanims_reset_bone_weight", text="", icon="LOOP_BACK")
                op.bone_name = bone.name

            if len(other_bones) > 15:
                col.label(text=f"... and {len(other_bones) - 15} more bones")

        layout.separator()
        row = layout.row(align=True)
        row.operator("object.rbxanims_apply_default_weights", text="Apply Defaults")
        row.operator("object.rbxanims_clear_com_weights", text="Clear Custom")


class OBJECT_OT_ResetBoneWeight(bpy.types.Operator):
    """Reset a single bone weight to default"""
    bl_label = "Reset Weight"
    bl_idname = "object.rbxanims_reset_bone_weight"
    bl_description = "Reset this bone's COM weight to default"
    bl_options = {'REGISTER', 'UNDO'}

    bone_name: bpy.props.StringProperty(name="Bone Name")

    def execute(self, context):
        from ..rig.com import set_bone_weight, update_com_visualization, is_com_for_armature

        obj = context.active_object
        if obj and obj.type == "ARMATURE" and self.bone_name:
            bone = obj.data.bones.get(self.bone_name)
            if bone:
                set_bone_weight(bone, -1)  # -1 removes custom weight
                if is_com_for_armature(obj):
                    update_com_visualization(obj)
                self.report({"INFO"}, f"Reset weight for {self.bone_name}")

        return {"FINISHED"}


class OBJECT_OT_ApplyDefaultWeights(bpy.types.Operator):
    """Apply default weights to all bones as custom properties"""
    bl_label = "Apply Default Weights"
    bl_idname = "object.rbxanims_apply_default_weights"
    bl_description = "Apply default COM weights to all bones (makes them editable)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        from ..rig.com import apply_default_weights, update_com_visualization, is_com_for_armature

        obj = context.active_object
        applied = apply_default_weights(obj, overwrite=False)
        if applied <= 0:
            self.report({"INFO"}, "No defaults applied — rig type not recognized. Use Overwrite to force apply.")
        else:
            self.report({"INFO"}, f"Applied default COM weights to {applied} bones")

        if is_com_for_armature(obj):
            update_com_visualization(obj)

        return {"FINISHED"}


class OBJECT_OT_ClearCOMWeights(bpy.types.Operator):
    """Clear all custom COM weights"""
    bl_label = "Clear Custom Weights"
    bl_idname = "object.rbxanims_clear_com_weights"
    bl_description = "Remove all custom COM weights, reverting to defaults"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        from ..rig.com import clear_all_custom_weights, update_com_visualization, is_com_for_armature

        obj = context.active_object
        clear_all_custom_weights(obj)
        if is_com_for_armature(obj):
            update_com_visualization(obj)
        self.report({"INFO"}, "Cleared custom COM weights")

        return {"FINISHED"}


class OBJECT_OT_SetSelectedBoneWeight(bpy.types.Operator):
    """Set COM weight for selected bones"""
    bl_label = "Set Bone Weight"
    bl_idname = "object.rbxanims_set_bone_weight"
    bl_description = "Set COM weight for selected bones"
    bl_options = {'REGISTER', 'UNDO'}

    weight: bpy.props.FloatProperty(
        name="Weight",
        default=0.05,
        min=0.0,
        max=1.0,
        description="COM weight for the bone"
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE" and obj.mode == 'POSE'

    def execute(self, context):
        from ..rig.com import set_bone_weight

        obj = context.active_object
        count = 0

        for pose_bone in obj.pose.bones:
            if pose_bone.bone.select:
                set_bone_weight(pose_bone.bone, self.weight)
                count += 1

        self.report({"INFO"}, f"Set weight {self.weight:.3f} for {count} bones")
        return {"FINISHED"}

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self)


# =============================================================================
# AutoPhysics Operators
# =============================================================================

# class OBJECT_OT_ToggleAutoPhysics(bpy.types.Operator):
#     """Toggle AutoPhysics visualization"""
#     bl_label = "Toggle AutoPhysics"
#     bl_idname = "object.rbxanims_toggle_autophysics"
#     bl_description = "Toggle physics-based animation analysis and ghost preview"
#     bl_options = {'REGISTER'}

#     @classmethod
#     def poll(cls, context):
#         return _resolve_autophysics_armature(context) is not None

#     def execute(self, context):
#         from ..rig.physics import (
#             is_physics_enabled,
#             enable_physics_visualization,
#             analyze_animation,
#             register_physics_frame_handler,
#             unregister_physics_frame_handler,
#         )

#         obj = _resolve_autophysics_armature(context)
#         if obj is None:
#             self.report({"ERROR"}, "No armature selected for AutoPhysics")
#             return {"CANCELLED"}

#         if is_physics_enabled():
#             enable_physics_visualization(False)
#             unregister_physics_frame_handler()
#             self.report({"INFO"}, "AutoPhysics disabled")
#         else:
#             # Analyze the animation first
#             self.report({"INFO"}, "Analyzing animation physics...")
#             analyze_animation(obj)
#             enable_physics_visualization(True)
#             register_physics_frame_handler()
#             self.report({"INFO"}, "AutoPhysics enabled")

#         return {"FINISHED"}


# class OBJECT_OT_AnalyzePhysics(bpy.types.Operator):
#     """Re-analyze animation physics"""
#     bl_label = "Analyze Physics"
#     bl_idname = "object.rbxanims_analyze_physics"
#     bl_description = "Re-analyze the animation for physics validity"
#     bl_options = {'REGISTER'}

#     @classmethod
#     def poll(cls, context):
#         return _resolve_autophysics_armature(context) is not None

#     def execute(self, context):
#         from ..rig.physics import analyze_animation, is_physics_enabled

#         obj = _resolve_autophysics_armature(context)
#         if obj is None:
#             self.report({"ERROR"}, "No armature selected for physics analysis")
#             return {"CANCELLED"}
#         analyze_animation(obj)

#         if is_physics_enabled():
#             self.report({"INFO"}, "Physics analysis updated")
#         else:
#             self.report({"INFO"}, "Physics analyzed (enable AutoPhysics to visualize)")

#         return {"FINISHED"}


# class OBJECT_OT_TogglePhysicsGhost(bpy.types.Operator):
#     """Toggle ghost character display"""
#     bl_label = "Toggle Ghost"
#     bl_idname = "object.rbxanims_toggle_physics_ghost"
#     bl_description = "Toggle the physics ghost character display"
#     bl_options = {'REGISTER'}

#     @classmethod
#     def poll(cls, context):
#         return _resolve_autophysics_armature(context) is not None

#     def execute(self, context):
#         from ..rig.physics import toggle_ghost, is_ghost_enabled

#         toggle_ghost()
#         state = "enabled" if is_ghost_enabled() else "disabled"
#         self.report({"INFO"}, f"Physics ghost {state}")

#         return {"FINISHED"}


# class OBJECT_OT_FocusTrajectory(bpy.types.Operator):
#     """Jump to the trajectory frame that most needs attention"""
#     bl_label = "Focus Trajectory"
#     bl_idname = "object.rbxanims_focus_trajectory"
#     bl_description = "Jump to the most useful trajectory frame"
#     bl_options = {'REGISTER'}

#     @classmethod
#     def poll(cls, context):
#         return _resolve_autophysics_armature(context) is not None

#     def execute(self, context):
#         from ..rig.physics import (
#             analyze_animation,
#             enable_physics_visualization,
#             get_trajectory_focus_frame,
#             get_trajectory_summary,
#             is_physics_enabled,
#             register_physics_frame_handler,
#         )

#         obj = _resolve_autophysics_armature(context)
#         if obj is None:
#             self.report({"ERROR"}, "No armature selected for trajectory focus")
#             return {"CANCELLED"}

#         summary = get_trajectory_summary()
#         if not summary.get("has_analysis"):
#             analyze_animation(obj)

#         if not is_physics_enabled():
#             enable_physics_visualization(True)
#             register_physics_frame_handler()

#         focus_frame = get_trajectory_focus_frame()
#         if focus_frame is None:
#             self.report({"INFO"}, "No trajectory frame found")
#             return {"FINISHED"}

#         context.scene.frame_set(int(focus_frame))
#         self.report({"INFO"}, f"Focused trajectory frame {focus_frame}")
#         return {"FINISHED"}


# class OBJECT_OT_ApplyTrajectoryIK(bpy.types.Operator):
#     """Apply trajectory foot targets to existing IK controls"""
#     bl_label = "Apply Trajectory IK"
#     bl_idname = "object.rbxanims_apply_trajectory_ik"
#     bl_description = "Move existing foot IK controls to the trajectory targets"
#     bl_options = {'REGISTER', 'UNDO'}

#     @classmethod
#     def poll(cls, context):
#         return _resolve_autophysics_armature(context) is not None

#     def execute(self, context):
#         from ..rig.physics import (
#             analyze_animation,
#             apply_trajectory_ik_assist,
#             enable_physics_visualization,
#             get_trajectory_summary,
#             is_physics_enabled,
#             register_physics_frame_handler,
#         )

#         obj = _resolve_autophysics_armature(context)
#         if obj is None:
#             self.report({"ERROR"}, "no armature selected for trajectory ik")
#             return {"CANCELLED"}

#         summary = get_trajectory_summary()
#         if not summary.get("has_analysis"):
#             analyze_animation(obj)

#         if not is_physics_enabled():
#             enable_physics_visualization(True)
#             register_physics_frame_handler()

#         result = apply_trajectory_ik_assist(obj, context.scene.frame_current)
#         moved = int(result.get("moved", 0) or 0)
#         frame = result.get("frame")
#         skipped = result.get("skipped", []) or []

#         if moved <= 0:
#             reason = ", ".join(str(item) for item in skipped[:3]) or "no applicable target"
#             self.report({"WARNING"}, f"trajectory ik skipped: {reason}")
#             return {"CANCELLED"}

#         self.report({"INFO"}, f"trajectory ik keyed {moved} control(s) at frame {frame}")
#         return {"FINISHED"}


# class OBJECT_OT_ToggleRotationMomentum(bpy.types.Operator):
#     """Toggle rotation-based momentum visualization"""
#     bl_label = "Toggle Rotation Momentum"
#     bl_idname = "object.rbxanims_toggle_rotation_momentum"
#     bl_description = "Toggle the angular momentum / rotation visualization"
#     bl_options = {'REGISTER'}

#     @classmethod
#     def poll(cls, context):
#         obj = context.active_object
#         return obj and obj.type == "ARMATURE"

#     def execute(self, context):
#         from ..rig.physics import toggle_angular_momentum, is_angular_momentum_enabled

#         toggle_angular_momentum()
#         state = "enabled" if is_angular_momentum_enabled() else "disabled"
#         self.report({"INFO"}, f"Rotation momentum {state}")

#         return {"FINISHED"}


class OBJECT_OT_ToggleWeldBones(bpy.types.Operator):
    """Toggle visibility of weld bones in the armature"""
    bl_label = "Toggle Weld Bones"
    bl_idname = "object.rbxanims_toggle_weld_bones"
    bl_description = "Show or hide weld/weldconstraint bones"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        settings = context.scene.rbx_anim_settings
        settings.rbx_hide_weld_bones = not settings.rbx_hide_weld_bones
        hide = settings.rbx_hide_weld_bones

        armature = context.active_object
        if armature and armature.type == "ARMATURE":
            amt = armature.data
            count = 0

            try:
                collections = amt.collections
                use_collections = True
            except Exception:
                collections = None
                use_collections = False

            if use_collections:
                weld_coll_name = "_WeldBones"
                weld_coll = collections.get(weld_coll_name)

                if weld_coll is None:
                    weld_coll = collections.new(weld_coll_name)
                    for bone in amt.bones:
                        joint_type = bone.get("rbx_joint_type", "Motor6D")
                        if joint_type in ("Weld", "WeldConstraint"):
                            weld_coll.assign(bone)
                            count += 1
                else:
                    count = len([b for b in amt.bones if b.get("rbx_joint_type") in ("Weld", "WeldConstraint")])

                weld_coll.is_visible = not hide
            else:
                # Blender 3.x fallback
                for bone in amt.bones:
                    joint_type = bone.get("rbx_joint_type", "Motor6D")
                    if joint_type in ("Weld", "WeldConstraint"):
                        bone.hide = hide
                        count += 1

            state = "hidden" if hide else "visible"
            self.report({"INFO"}, f"{count} weld bones now {state}")

        return {"FINISHED"}


def _worldspace_dependencies(ao):
    """Objects whose animation can affect this armature."""
    seen, pending = set(), [ao]
    while pending:
        obj = pending.pop()
        if obj in seen:
            continue
        seen.add(obj)
        if obj.parent:
            pending.append(obj.parent)
        yield obj
        constraints = list(obj.constraints)
        if obj.type == 'ARMATURE':
            constraints.extend(c for pb in obj.pose.bones for c in pb.constraints)
        for constraint in constraints:
            for attr in ('target', 'pole_target'):
                target = getattr(constraint, attr, None)
                if target:
                    pending.append(target)
            pending.extend(item.target for item in getattr(constraint, 'targets', ()) if item.target)

def _get_action_frame_range(ao):
    """Include animation on constraint targets and NLA strip scene ranges."""
    import math
    ranges = []
    for obj in _worldspace_dependencies(ao):
        animation = obj.animation_data
        if not animation:
            continue
        if animation.action:
            ranges.append(tuple(animation.action.frame_range))
        for track in animation.nla_tracks:
            if not track.mute:
                ranges.extend((strip.frame_start, strip.frame_end) for strip in track.strips if not strip.mute)
    if ranges:
        return math.floor(min(r[0] for r in ranges)), math.ceil(max(r[1] for r in ranges))
    scene = bpy.context.scene
    return scene.frame_start, scene.frame_end


def _worldspace_sample_frames(ao, start, end, substeps):
    from ..core.utils import get_action_fcurves
    frames = {tick / substeps for tick in range(start * substeps, end * substeps + 1)}
    if substeps > 1:
        for obj in _worldspace_dependencies(ao):
            animation = obj.animation_data
            if not animation or not animation.action:
                continue
            for curve in get_action_fcurves(animation.action, slot=getattr(animation, 'action_slot', None)):
                previous = None
                for point in curve.keyframe_points:
                    frame = float(point.co.x)
                    if start <= frame <= end:
                        frames.add(frame)
                        if previous and previous.interpolation == 'CONSTANT' and frame > start:
                            # Keep a hold up to the discontinuity instead of
                            # turning it into a quarter-frame linear ramp.
                            frames.add(max(start, frame - 0.02))
                    previous = point
    return sorted(frames)


def _has_any_keys(ao, bone_names):
    """Check if any of the given bones have keyframes at all."""
    action = ao.animation_data and ao.animation_data.action
    if not action:
        return False
    from ..core.utils import get_action_fcurves
    fcurves = get_action_fcurves(action)
    escaped = {n: bpy.utils.escape_identifier(n) for n in bone_names}
    for fc in fcurves:
        dp = getattr(fc, "data_path", "")
        for name, esc in escaped.items():
            if dp.startswith(f'pose.bones["{esc}"]'):
                return True
    return False


def _sample_world_matrices(ao, bone_names, frame_start, frame_end, substeps=1):
    """Sample evaluated armature-space matrices, including hidden controls."""
    from ..core.evaluation import rig_evaluation_context

    scene = bpy.context.scene
    result = {n: {} for n in bone_names}
    orig_frame, orig_subframe = scene.frame_current, scene.frame_subframe
    try:
        with rig_evaluation_context(ao):
            for f in _worldspace_sample_frames(ao, frame_start, frame_end, substeps):
                scene.frame_set(int(f // 1), subframe=f % 1)
                bpy.context.view_layer.update()
                evaluated = ao.evaluated_get(bpy.context.evaluated_depsgraph_get())
                for name in bone_names:
                    pb = evaluated.pose.bones.get(name)
                    if pb:
                        result[name][f] = pb.matrix.copy()
    finally:
        scene.frame_set(orig_frame, subframe=orig_subframe)
    return result


def _bone_transform_path(path, escaped_name):
    return path in {
        f'pose.bones["{escaped_name}"].{channel}'
        for channel in ("location", "rotation_quaternion", "rotation_euler",
                        "rotation_axis_angle", "scale")
    }


def _rotation_data_path(pb):
    """Return the correct rotation data_path for a pose bone's rotation mode."""
    mode = pb.rotation_mode
    if mode == 'QUATERNION':
        return "rotation_quaternion"
    elif mode == 'AXIS_ANGLE':
        return "rotation_axis_angle"
    else:
        # euler modes: XYZ, XZY, YXZ, YZX, ZXY, ZYX
        return "rotation_euler"


def _clear_bone_fcurves(ao, bone_names):
    """Remove all existing fcurves for the given bones.

    Must be done BEFORE re-keying to prevent stale keyframes from
    corrupting bezier handle auto-computation on newly inserted keys.
    """
    action = ao.animation_data and ao.animation_data.action
    if not action:
        return
    from ..core.utils import get_action_fcurves
    fcurves = get_action_fcurves(action, slot=getattr(ao.animation_data, "action_slot", None))
    escaped = {n: bpy.utils.escape_identifier(n) for n in bone_names}
    to_remove = []
    for fc in fcurves:
        dp = getattr(fc, "data_path", "")
        for name, esc in escaped.items():
            if _bone_transform_path(dp, esc):
                to_remove.append(fc)
                break
    for fc in reversed(to_remove):
        try:
            fcurves.remove(fc)
        except Exception:
            pass


def _snapshot_bone_fcurves(ao, bone_names):
    """Serialize the fcurve data for the given bones into a JSON-safe dict.

    Captures every keyframe point with its value, handles, interpolation,
    and easing — enough to perfectly reconstruct the original curves.
    Returns {bone_name: [{data_path, array_index, keyframes: [...]}]}.
    """
    snapshot = {name: [] for name in bone_names}
    action = ao.animation_data and ao.animation_data.action
    if not action:
        return snapshot
    from ..core.utils import get_action_fcurves
    fcurves = get_action_fcurves(action, slot=getattr(ao.animation_data, "action_slot", None))
    escaped = {n: bpy.utils.escape_identifier(n) for n in bone_names}
    for fc in fcurves:
        dp = getattr(fc, "data_path", "")
        for name, esc in escaped.items():
            if not _bone_transform_path(dp, esc):
                continue
            curve_data = {
                "data_path": dp,
                "array_index": fc.array_index,
                "keyframes": [],
                "extrapolation": fc.extrapolation,
                "modifiers": [
                    {"type": modifier.type, "values": {
                        prop.identifier: (list(getattr(modifier, prop.identifier)) if getattr(prop, "is_array", False) else getattr(modifier, prop.identifier))
                        for prop in modifier.bl_rna.properties
                        if not prop.is_readonly and prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}
                    }} for modifier in fc.modifiers
                ],
            }
            for kp in fc.keyframe_points:
                curve_data["keyframes"].append({
                    "co": [kp.co.x, kp.co.y],
                    "hl": [kp.handle_left.x, kp.handle_left.y],
                    "hr": [kp.handle_right.x, kp.handle_right.y],
                    "interp": kp.interpolation,
                    "easing": kp.easing,
                    "ht": kp.handle_left_type,
                    "hrt": kp.handle_right_type,
                    "amplitude": kp.amplitude,
                    "back": kp.back,
                    "period": kp.period,
                })
            snapshot.setdefault(name, []).append(curve_data)
            break
    return snapshot


def _store_fcurve_snapshot(ao, snapshot, prop_name="worldspace_original_fcurves"):
    """Store the fcurve snapshot as a JSON string in a bone custom property."""
    import json
    for bone_name, curves in snapshot.items():
        data_bone = ao.data.bones.get(bone_name)
        if data_bone:
            data_bone[prop_name] = json.dumps(curves)


def _fcurves_match_snapshot(ao, bone_name, prop_name):
    """Detect channel, handle, interpolation, and value edits."""
    import json
    raw = ao.data.bones[bone_name].get(prop_name)
    if raw is None:
        return False
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return False
    current = _snapshot_bone_fcurves(ao, [bone_name]).get(bone_name, [])
    def key(curve):
        return curve["data_path"], curve["array_index"]
    return sorted(stored, key=key) == sorted(current, key=key)


def _restore_fcurve_snapshot(ao, bone_names):
    """Restore fcurves from the stored snapshot, perfectly recreating originals.

    Returns True if restoration succeeded for at least one bone.
    """
    import json
    from ..core.utils import get_action_fcurves
    action = ao.animation_data and ao.animation_data.action
    if not action:
        return False

    restored_any = False
    for bone_name in bone_names:
        data_bone = ao.data.bones.get(bone_name)
        if not data_bone:
            continue
        raw = data_bone.get("worldspace_original_fcurves")
        if not raw:
            continue
        try:
            curves = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        fcurves = get_action_fcurves(action, slot=getattr(ao.animation_data, "action_slot", None))
        for curve_data in curves:
            dp = curve_data["data_path"]
            idx = curve_data["array_index"]

            # find or create the fcurve
            fc = fcurves.find(dp, index=idx)
            if fc is None:
                fc = fcurves.new(dp, index=idx)
            else:
                # clear existing keyframes
                while len(fc.keyframe_points) > 0:
                    fc.keyframe_points.remove(fc.keyframe_points[0])

            fc.extrapolation = curve_data.get("extrapolation", "CONSTANT")
            for modifier in list(fc.modifiers):
                fc.modifiers.remove(modifier)
            for stored in curve_data.get("modifiers", []):
                modifier = fc.modifiers.new(stored["type"])
                for key, value in stored["values"].items():
                    setattr(modifier, key, value)

            for kf in curve_data["keyframes"]:
                kp = fc.keyframe_points.insert(kf["co"][0], kf["co"][1])
                kp.handle_left_type = kf.get("ht", "AUTO_CLAMPED")
                kp.handle_right_type = kf.get("hrt", "AUTO_CLAMPED")
                kp.handle_left = (kf["hl"][0], kf["hl"][1])
                kp.handle_right = (kf["hr"][0], kf["hr"][1])
                kp.interpolation = kf.get("interp", "BEZIER")
                kp.easing = kf.get("easing", "AUTO")
                for key in ("amplitude", "back", "period"):
                    if key in kf:
                        setattr(kp, key, kf[key])

            fc.update()
        restored_any = True

    return restored_any


def _rekey_bones_with_matrices(ao, world_mats):
    """Bake parent-first with explicit linear samples and continuous rotations."""
    from ..core.evaluation import rig_evaluation_context
    with rig_evaluation_context(ao):
        if not ao.animation_data or not ao.animation_data.action:
            name = next(iter(world_mats))
            ao.pose.bones[name].keyframe_insert(data_path='location')
        _clear_bone_fcurves(ao, set(world_mats))
        scene = bpy.context.scene
        orig_frame, orig_subframe = scene.frame_current, scene.frame_subframe
        names = sorted(world_mats, key=lambda name: len(ao.data.bones[name].parent_recursive))
        previous, channels = {}, {}
        try:
            for f in sorted({f for samples in world_mats.values() for f in samples}):
                scene.frame_set(int(f // 1), subframe=f % 1)
                bpy.context.view_layer.update()
                for name in names:
                    mat = world_mats[name].get(f)
                    if mat is None:
                        continue
                    pb = ao.pose.bones[name]
                    pb.matrix = mat
                    if pb.rotation_mode == 'QUATERNION':
                        rotation = pb.rotation_quaternion.copy()
                        if name in previous:
                            rotation.make_compatible(previous[name])
                        pb.rotation_quaternion = rotation
                        previous[name] = rotation
                    elif pb.rotation_mode not in {'AXIS_ANGLE'}:
                        rotation = pb.rotation_euler.copy()
                        if name in previous:
                            rotation.make_compatible(previous[name])
                        pb.rotation_euler = rotation
                        previous[name] = rotation
                    for path in ('location', _rotation_data_path(pb), 'scale'):
                        for index, value in enumerate(getattr(pb, path)):
                            channels.setdefault((pb.path_from_id(path), index), []).append((f, value))
                    bpy.context.view_layer.update()
            from ..core.utils import get_action_fcurves
            action = ao.animation_data.action
            curves = get_action_fcurves(action, slot=getattr(ao.animation_data, "action_slot", None))
            for (path, component), samples in channels.items():
                fc = curves.new(path, index=component)
                # Write the sampled channels without insertion-time merging.
                # Hold guards stay outside Blender's 0.01-frame dedup tolerance.
                fc.keyframe_points.add(len(samples))
                for index, (point, sample) in enumerate(zip(fc.keyframe_points, samples)):
                    point.co = sample
                    near_step = index + 1 < len(samples) and 0 < samples[index + 1][0] - sample[0] <= 0.0201
                    point.interpolation = 'CONSTANT' if near_step else 'LINEAR'
                fc.update()
        finally:
            scene.frame_set(orig_frame, subframe=orig_subframe)



def _decimate_bone_fcurves(ao, bone_names, error_threshold=0.001):
    """Remove redundant keyframes from the given bones' fcurves.

    Uses blender's built-in decimate with an error threshold (in channel
    units).  Default 0.001 is imperceptible for both position and rotation
    while typically eliminating 60-90% of baked keys.
    """
    action = ao.animation_data and ao.animation_data.action
    if not action:
        return
    from ..core.utils import get_action_fcurves
    fcurves = get_action_fcurves(action)
    escaped = {n: bpy.utils.escape_identifier(n) for n in bone_names}
    target_indices = []
    for i, fc in enumerate(fcurves):
        dp = getattr(fc, "data_path", "")
        for name, esc in escaped.items():
            if dp.startswith(f'pose.bones["{esc}"]'):
                target_indices.append(i)
                break

    if not target_indices:
        return

    # select only our target fcurves in the graph editor context, then decimate
    # we do this manually per-curve to avoid needing graph editor context
    for i, fc in enumerate(fcurves):
        fc.select = i in set(target_indices)

    # manual decimate: for each target fcurve, iteratively remove
    # the keypoint whose removal causes the least error, until
    # all remaining removals would exceed the threshold.
    target_set = set(target_indices)
    for i, fc in enumerate(fcurves):
        if i not in target_set:
            continue
        _decimate_single_fcurve(fc, error_threshold)


def _decimate_single_fcurve(fc, threshold):
    """Remove keyframes from a single fcurve where the error stays below threshold.

    Uses iterative least-error removal (greedy):
    - for each interior keyframe, compute the error if it were removed
      (linear interpolation between its neighbors vs its actual value)
    - remove the one with smallest error, if that error < threshold
    - repeat until no more can be removed
    """
    while len(fc.keyframe_points) > 2:
        best_idx = -1
        best_err = float('inf')

        pts = fc.keyframe_points
        for j in range(1, len(pts) - 1):
            prev = pts[j - 1]
            curr = pts[j]
            nxt = pts[j + 1]

            # linear interpolation between prev and next at curr's time
            t_range = nxt.co.x - prev.co.x
            if t_range == 0:
                best_idx = j
                best_err = 0
                break
            t = (curr.co.x - prev.co.x) / t_range
            interp = prev.co.y + t * (nxt.co.y - prev.co.y)
            err = abs(curr.co.y - interp)

            if err < best_err:
                best_err = err
                best_idx = j

        if best_err > threshold or best_idx < 0:
            break

        pts.remove(pts[best_idx])


class OBJECT_OT_WorldSpaceUnparent(bpy.types.Operator):
    """Unparent selected bones so they animate in world space, but export as if still parented.

    Stores the original parent in a custom property so the serializer can
    compensate at export time.  Existing keyframes are converted so the
    bone keeps its world-space motion intact.
    """
    bl_label = "World-Space Unparent"
    bl_idname = "object.rbxanims_worldspace_unparent"
    bl_description = (
        "Unparent selected bones for easier animation while preserving "
        "the original hierarchy on export"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != "ARMATURE":
            return False
        # need at least one selected pose bone that has a parent
        if obj.mode == "POSE":
            return any(
                pose_bone_selected(b) and b.bone.parent
                and not b.bone.get("worldspace_bone")
                for b in obj.pose.bones
            )
        return False

    def execute(self, context):
        ao = context.active_object
        amt = ao.data

        # collect names + original parents while still in pose mode
        targets = []
        for pb in ao.pose.bones:
            if (
                pose_bone_selected(pb)
                and pb.bone.parent
                and not pb.bone.get("worldspace_bone")
            ):
                targets.append((pb.name, pb.bone.parent.name))

        if not targets:
            self.report({"WARNING"}, "no eligible bones selected")
            return {"CANCELLED"}

        bone_names = [t[0] for t in targets]

        import json
        for name in bone_names:
            pb = ao.pose.bones[name]
            pb.bone["worldspace_original_basis"] = [v for row in pb.matrix_basis for v in row]
            pb.bone["worldspace_original_connected"] = pb.bone.use_connect
            pb.bone["worldspace_constraints"] = json.dumps({c.name: c.mute for c in pb.constraints})

        # snapshot the original parent-local fcurves so reparent can
        # restore them losslessly, no matter how many round-trips
        fcurve_snapshot = _snapshot_bone_fcurves(ao, bone_names)

        # sample world matrices BEFORE unparenting at every frame in the action range.
        # this handles: bones with no keys (animated via parent), interpolation
        # drift, and ensures exact visual fidelity after hierarchy change.
        frame_start, frame_end = _get_action_frame_range(ao)
        world_mats = _sample_world_matrices(ao, bone_names, frame_start, frame_end, substeps=4)

        for name in bone_names:
            for constraint in ao.pose.bones[name].constraints:
                constraint.mute = True

        # switch to edit mode to do the actual unparent
        bpy.ops.object.mode_set(mode="EDIT")

        for bone_name, parent_name in targets:
            edit_bone = amt.edit_bones.get(bone_name)
            if edit_bone and edit_bone.parent:
                edit_bone.use_connect = False
                edit_bone.parent = None

        bpy.ops.object.mode_set(mode="POSE")

        # stamp custom properties on the data bone (persistent)
        for bone_name, parent_name in targets:
            data_bone = amt.bones.get(bone_name)
            if data_bone:
                data_bone["worldspace_bone"] = True
                data_bone["worldspace_original_parent"] = parent_name

        # store original fcurve snapshot for lossless reparent
        _store_fcurve_snapshot(ao, fcurve_snapshot)

        # re-key with world matrices so animation stays the same visually
        _rekey_bones_with_matrices(ao, world_mats)

        # snapshot the baked world-space fcurves so reparent can detect
        # whether the user edited anything while unparented
        ws_snapshot = _snapshot_bone_fcurves(ao, bone_names)
        _store_fcurve_snapshot(ao, ws_snapshot, prop_name="worldspace_baked_fcurves")

        self.report(
            {"INFO"},
            f"unparented {len(targets)} bone(s) — export will compensate",
        )
        return {"FINISHED"}


class OBJECT_OT_WorldSpaceReparent(bpy.types.Operator):
    """Restore original parent for world-space-unparented bones.

    Existing keyframes are converted so the bone keeps its world-space
    motion intact under the restored parent.
    """
    bl_label = "Restore Parent"
    bl_idname = "object.rbxanims_worldspace_reparent"
    bl_description = (
        "Re-parent selected bones back to their original parent "
        "and remove the world-space export flag"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != "ARMATURE":
            return False
        if obj.mode == "POSE":
            return any(
                pose_bone_selected(b) and b.bone.get("worldspace_bone")
                for b in obj.pose.bones
            )
        return False

    def execute(self, context):
        ao = context.active_object
        amt = ao.data

        targets = []
        for pb in ao.pose.bones:
            if pose_bone_selected(pb) and pb.bone.get("worldspace_bone"):
                original_parent = pb.bone.get("worldspace_original_parent", "")
                if original_parent:
                    targets.append((pb.name, original_parent))

        if not targets:
            self.report({"WARNING"}, "no world-space bones selected")
            return {"CANCELLED"}

        bone_names = [t[0] for t in targets]

        missing = [parent for _, parent in targets if parent not in amt.bones]
        if missing:
            self.report({'WARNING'}, "Cannot restore missing parent: " + ", ".join(sorted(set(missing))))
            return {'CANCELLED'}

        # per-bone: detect if the user edited the world-space animation.
        # compare current fcurves against the baked snapshot from unparent.
        # edited bones → bake current world-space into parent-local (preserves edits).
        # untouched bones → restore original pre-unparent fcurves (lossless).
        bones_edited = set()
        bones_lossless = set()
        for name in bone_names:
            has_original = (
                ao.data.bones.get(name)
                and ao.data.bones[name].get("worldspace_original_fcurves")
            )
            if has_original and _fcurves_match_snapshot(
                ao, name, "worldspace_baked_fcurves"
            ):
                bones_lossless.add(name)
            else:
                bones_edited.add(name)

        frame_start, frame_end = _get_action_frame_range(ao)
        all_world_mats = _sample_world_matrices(ao, bone_names, frame_start, frame_end, substeps=4)
        world_mats = {name: all_world_mats[name] for name in bones_edited}

        bpy.ops.object.mode_set(mode="EDIT")

        restored = 0
        for bone_name, parent_name in targets:
            edit_bone = amt.edit_bones.get(bone_name)
            parent_edit = amt.edit_bones.get(parent_name)
            if edit_bone and parent_edit:
                edit_bone.parent = parent_edit
                edit_bone.use_connect = bone_name in bones_lossless and bool(amt.bones[bone_name].get("worldspace_original_connected", False))
                restored += 1

        bpy.ops.object.mode_set(mode="POSE")

        import json
        from mathutils import Matrix
        for name in bones_lossless:
            pb = ao.pose.bones[name]
            basis = pb.bone.get("worldspace_original_basis")
            if basis is not None:
                pb.matrix_basis = Matrix([basis[i:i + 4] for i in range(0, 16, 4)])
            for constraint_name, muted in json.loads(pb.bone.get("worldspace_constraints", "{}")).items():
                constraint = pb.constraints.get(constraint_name)
                if constraint:
                    constraint.mute = muted

        # restore lossless bones from snapshot
        if bones_lossless:
            _clear_bone_fcurves(ao, bones_lossless)
            _restore_fcurve_snapshot(ao, list(bones_lossless))

        # Unchanged child keys are not enough: a parent or proxy target may
        # have changed. Keep the lossless restore only when its visual motion
        # still matches the detached animation.
        if bones_lossless:
            restored_mats = _sample_world_matrices(ao, list(bones_lossless), frame_start, frame_end, substeps=4)
            for name in list(bones_lossless):
                if any(max(abs(restored_mats[name][f][r][c] - mat[r][c])
                           for r in range(4) for c in range(4)) > 1e-4
                       for f, mat in all_world_mats[name].items()):
                    bones_lossless.remove(name)
                    bones_edited.add(name)
                    world_mats[name] = all_world_mats[name]
                    for constraint in ao.pose.bones[name].constraints:
                        constraint.mute = True

        # A connected bone cannot keep an independently edited translation.
        # Restore connection only on the genuinely lossless path.
        if any(amt.bones[name].use_connect for name in bones_edited):
            bpy.ops.object.mode_set(mode="EDIT")
            for name in bones_edited:
                amt.edit_bones[name].use_connect = False
            bpy.ops.object.mode_set(mode="POSE")

        # bake edited bones from world-space matrices
        if world_mats:
            _rekey_bones_with_matrices(ao, world_mats)

        if any(ao.pose.bones[name].constraints for name in bones_edited):
            self.report({'INFO'}, "Kept constraints muted on edited bones to preserve their baked motion")

        # clear all custom props
        for bone_name, _ in targets:
            data_bone = amt.bones.get(bone_name)
            if data_bone:
                for key in ("worldspace_bone", "worldspace_original_parent",
                            "worldspace_original_fcurves",
                            "worldspace_baked_fcurves", "worldspace_original_basis",
                            "worldspace_original_connected", "worldspace_constraints"):
                    if key in data_bone:
                        del data_bone[key]

        n_l = len(bones_lossless)
        n_e = len(bones_edited)
        parts = []
        if n_l:
            parts.append(f"{n_l} lossless")
        if n_e:
            parts.append(f"{n_e} baked")
        self.report({"INFO"}, f"restored parent on {restored} bone(s) ({', '.join(parts)})")
        return {"FINISHED"}
