"""
Rig generation and management operators.
"""

import bpy
from ..rig.creation import create_rig
from ..rig.ik import create_ik_config, remove_ik_config, has_ik_constraint, update_pole_axis
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
            for obj in bpy.data.objects
        )

        # Check for armatures with Motor6D properties (transform, transform1, nicetransform)
        has_motor6d_rig = any(
            obj.type == "ARMATURE"
            and any(
                "transform" in bone and "transform1" in bone and "nicetransform" in bone
                for bone in obj.data.bones
            )
            for obj in bpy.data.objects
        )

        return has_rig_meta or has_motor6d_rig

    @classmethod
    def poll(cls, context):
        # Check if scene has either rig meta objects or armatures with Motor6D properties
        return any(
            "RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name
            for obj in bpy.data.objects
        ) or any(
            obj.type == "ARMATURE"
            and any(
                "transform" in bone and "transform1" in bone and "nicetransform" in bone
                for bone in obj.data.bones
            )
            for obj in bpy.data.objects
        )

    def create_rig_meta_from_armature(self, armature_obj):
        """Create a temporary rig meta object from an armature with Motor6D properties"""
        # Find the root bone (bone with no parent) or first bone with Motor6D properties
        root_bone_name = None
        for bone in armature_obj.data.bones:
            if not bone.parent and (
                "transform" in bone and "transform1" in bone and "nicetransform" in bone
            ):
                root_bone_name = bone.name
                break
        
        # Fallback to first bone if no root found
        if not root_bone_name and armature_obj.data.bones:
            root_bone_name = armature_obj.data.bones[0].name
        
        # Generate a basic rig structure based on the armature
        rig_structure = {
            "rigName": armature_obj.name.replace("__", "").replace("_Armature", "").replace(
                "Armature", ""
            ),
            "rig": {
                "jname": root_bone_name or "RootPart",
                "transform": [
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                ],  # Identity matrix
                "jointtransform0": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                "jointtransform1": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                "aux": [root_bone_name or "RootPart"],
                "children": [],
            },
        }

        # Create temporary meta object
        meta_obj_name = f"__{rig_structure['rigName']}Meta_Detected"
        if meta_obj_name in bpy.data.objects:
            bpy.data.objects.remove(bpy.data.objects[meta_obj_name], do_unlink=True)

        bpy.ops.object.add(type="EMPTY", location=(0, 0, 0))
        temp_meta = bpy.context.object
        temp_meta.name = meta_obj_name
        temp_meta["RigMeta"] = str(rig_structure).replace(
            "'", '"'
        )  # Convert to JSON-like string

        return meta_obj_name

    def execute(self, context):
        try:
            # Check if the selected item is a rig meta object or an armature
            selected_obj = bpy.data.objects.get(self.pr_rig_meta_name)
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
                # New case: armature with Motor6D properties
                meta_obj_name = self.create_rig_meta_from_armature(selected_obj)
                create_rig(self.pr_rigging_type, meta_obj_name)
                # Clean up temporary meta object
                if meta_obj_name in bpy.data.objects:
                    bpy.data.objects.remove(
                        bpy.data.objects[meta_obj_name], do_unlink=True
                    )
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
        for obj in bpy.data.objects:
            if "RigMeta" in obj and obj.name.startswith("__") and "Meta" in obj.name:
                item = (obj.name, obj.name, "")
                OBJECT_OT_GenRig.rig_meta_items_cache.append(item)

        # Add armatures with Motor6D properties (new detection method)
        for obj in bpy.data.objects:
            if obj.type == "ARMATURE" and any(
                "transform" in bone and "transform1" in bone and "nicetransform" in bone
                for bone in obj.data.bones
            ):
                # Create a display name that indicates this is a detected rig
                display_name = f"{obj.name} (Detected Motor6D Rig)"
                item = (obj.name, display_name, "Detected via Motor6D properties")
                OBJECT_OT_GenRig.rig_meta_items_cache.append(item)

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
            is_com_visualization_enabled,
            is_com_for_armature,
            enable_com_visualization,
            update_com_visualization,
            register_frame_handler,
            unregister_frame_handler,
        )
        
        obj = context.active_object
        
        # Check if COM is enabled for THIS armature specifically
        com_enabled_for_this = is_com_for_armature(obj)
        
        if com_enabled_for_this:
            # Turn off COM for this armature
            enable_com_visualization(False)
            unregister_frame_handler()
            self.report({"INFO"}, "COM visualization disabled")
        else:
            # Turn on COM for this armature (will switch if another armature had it)
            enable_com_visualization(True)
            register_frame_handler()
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


class OBJECT_OT_SetCOMPivot(bpy.types.Operator):
    bl_label = "Set/Unset Pivot to COM"
    bl_idname = "object.rbxanims_set_com_pivot"
    bl_description = "Toggle 3D cursor at Center of Mass for pivot-based rotation"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        from ..rig.com import set_com_pivot, get_com_pivot_bone, set_com_pivot_bone, is_bone_com_pivot
        
        obj = context.active_object
        
        # Get the currently selected bone (if any)
        selected_bone_name = None
        if context.mode == 'POSE' and obj.pose and context.active_pose_bone:
            selected_bone_name = context.active_pose_bone.name
        
        # Check if this bone is already the pivot
        if selected_bone_name and is_bone_com_pivot(selected_bone_name):
            # Unset the pivot
            set_com_pivot_bone(None)
            self.report({"INFO"}, f"COM pivot unset from '{selected_bone_name}'")
        else:
            # Set the pivot
            set_com_pivot(obj, selected_bone_name)
            context.scene.tool_settings.transform_pivot_point = 'CURSOR'
            
            if selected_bone_name:
                self.report({"INFO"}, f"COM pivot set on '{selected_bone_name}'")
            else:
                self.report({"INFO"}, "3D cursor moved to Center of Mass")
        
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
        from ..rig.com import set_bone_weight
        
        obj = context.active_object
        if obj and obj.type == "ARMATURE" and self.bone_name:
            bone = obj.data.bones.get(self.bone_name)
            if bone:
                set_bone_weight(bone, -1)  # -1 removes custom weight
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
        from ..rig.com import apply_default_weights
        
        obj = context.active_object
        apply_default_weights(obj, overwrite=False)
        self.report({"INFO"}, "Applied default COM weights")
        
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
        from ..rig.com import clear_all_custom_weights
        
        obj = context.active_object
        clear_all_custom_weights(obj)
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

class OBJECT_OT_ToggleAutoPhysics(bpy.types.Operator):
    """Toggle AutoPhysics visualization"""
    bl_label = "Toggle AutoPhysics"
    bl_idname = "object.rbxanims_toggle_autophysics"
    bl_description = "Toggle physics-based animation analysis and ghost preview"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        from ..rig.physics import (
            is_physics_enabled,
            enable_physics_visualization,
            analyze_animation,
            register_physics_frame_handler,
            unregister_physics_frame_handler,
        )
        
        obj = context.active_object
        
        if is_physics_enabled():
            enable_physics_visualization(False)
            unregister_physics_frame_handler()
            self.report({"INFO"}, "AutoPhysics disabled")
        else:
            # Analyze the animation first
            self.report({"INFO"}, "Analyzing animation physics...")
            analyze_animation(obj)
            enable_physics_visualization(True)
            register_physics_frame_handler()
            self.report({"INFO"}, "AutoPhysics enabled")
        
        return {"FINISHED"}


class OBJECT_OT_AnalyzePhysics(bpy.types.Operator):
    """Re-analyze animation physics"""
    bl_label = "Analyze Physics"
    bl_idname = "object.rbxanims_analyze_physics"
    bl_description = "Re-analyze the animation for physics validity"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        from ..rig.physics import analyze_animation, is_physics_enabled
        
        obj = context.active_object
        analyze_animation(obj)
        
        if is_physics_enabled():
            self.report({"INFO"}, "Physics analysis updated")
        else:
            self.report({"INFO"}, "Physics analyzed (enable AutoPhysics to visualize)")
        
        return {"FINISHED"}


class OBJECT_OT_TogglePhysicsGhost(bpy.types.Operator):
    """Toggle ghost character display"""
    bl_label = "Toggle Ghost"
    bl_idname = "object.rbxanims_toggle_physics_ghost"
    bl_description = "Toggle the physics ghost character display"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        from ..rig.physics import toggle_ghost, is_ghost_enabled
        
        toggle_ghost()
        state = "enabled" if is_ghost_enabled() else "disabled"
        self.report({"INFO"}, f"Physics ghost {state}")
        
        return {"FINISHED"}


class OBJECT_OT_ToggleRotationMomentum(bpy.types.Operator):
    """Toggle rotation-based momentum visualization"""
    bl_label = "Toggle Rotation Momentum"
    bl_idname = "object.rbxanims_toggle_rotation_momentum"
    bl_description = "Toggle the angular momentum / rotation visualization"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "ARMATURE"

    def execute(self, context):
        from ..rig.physics import toggle_angular_momentum, is_angular_momentum_enabled
        
        toggle_angular_momentum()
        state = "enabled" if is_angular_momentum_enabled() else "disabled"
        self.report({"INFO"}, f"Rotation momentum {state}")
        
        return {"FINISHED"}


