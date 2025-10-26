"""
Constraint management operators for linking objects to bones.
"""

import bpy
from bpy.types import Operator
from ..rig.constraints import auto_constraint_parts, manual_constraint_parts
from ..core.utils import find_master_collection_for_object, find_parts_collection_in_master


class OBJECT_OT_AutoConstraint(bpy.types.Operator):
    bl_label = "Auto Constraint Parts"
    bl_idname = "object.rbxanims_autoconstraint"
    bl_description = "Automatically constrain parts/meshes with the same name as the bones in the armature. Rename your parts to match the bone names, then this will attach them to the rig."

    @classmethod
    def poll(cls, context):
        return context.scene.rbx_anim_armature in bpy.data.objects

    def execute(self, context):
        armature_name = context.scene.rbx_anim_armature
        success, message = auto_constraint_parts(armature_name)
        
        if success:
            self.report({'INFO'}, message)
        else:
            self.report({'ERROR'}, message)
            
        return {'FINISHED' if success else 'CANCELLED'}


class OBJECT_OT_ManualConstraint(bpy.types.Operator):
    bl_idname = "object.rbxanims_manualconstraint"
    bl_label = "Manual Part Constraints"
    bl_description = "Manually constrain mesh parts to bones in a list UI. Mesh must be placed in the Parts collection to show up in the list."
    bl_options = {'REGISTER', 'UNDO'}

    # Use a simpler property structure to avoid registration issues
    bone_names: bpy.props.CollectionProperty(type=bpy.types.PropertyGroup)
    mesh_names: bpy.props.CollectionProperty(type=bpy.types.PropertyGroup)
    
    def get_available_objects(self, context):
        """Get objects that are available for constraining (not in other _Parts collections)"""
        armature = bpy.data.objects.get(context.scene.rbx_anim_armature)
        if not armature:
            return []
            
        collection_name = f"{armature.name}_Parts"
        available_objects = []
        
        for obj in context.view_layer.objects:
            if obj.type == 'MESH':
                # Skip objects that are already in other _Parts collections
                if any(col.name.endswith('_Parts') and col.name != collection_name for col in obj.users_collection):
                    continue
                available_objects.append(obj)
        
        return available_objects

    @classmethod
    def poll(cls, context):
        armature = bpy.data.objects.get(context.scene.rbx_anim_armature)
        return armature and armature.type == 'ARMATURE'

    def get_parts_collection(self, context):
        """Safely get the parts collection for the currently selected armature."""
        armature = bpy.data.objects.get(context.scene.rbx_anim_armature)
        if not armature:
            return None
        
        master_collection = find_master_collection_for_object(armature)
        return find_parts_collection_in_master(master_collection)

    def invoke(self, context, event):
        self.bone_names.clear()
        self.mesh_names.clear()
        
        armature = bpy.data.objects.get(context.scene.rbx_anim_armature)
        parts_collection = self.get_parts_collection(context)

        if not parts_collection:
            self.report({'ERROR'}, "Could not find 'Parts' collection for the selected armature.")
            return {'CANCELLED'}
        
        # Create a reverse map of currently constrained objects {object_name: bone_name}
        constrained_map = {}
        for obj in parts_collection.objects:
             if obj.type == 'MESH':
                for c in obj.constraints:
                    if c.type == 'CHILD_OF' and c.target == armature:
                        constrained_map[obj.name] = c.subtarget
                        break

        # Populate the list with bones from the target armature
        for bone in armature.data.bones:
            bone_item = self.bone_names.add()
            bone_item.name = bone.name
            
            mesh_item = self.mesh_names.add()
            mesh_item.name = ""

            # Check if any object is already constrained to this bone
            for obj_name, bone_name in constrained_map.items():
                if bone_name == bone.name:
                    mesh_item.name = obj_name
                    break

        return context.window_manager.invoke_props_dialog(self, width=500)

    def draw(self, context):
        layout = self.layout

        header = layout.row()
        header.label(text="Bone")
        header.label(text="Constrained Mesh Part")

        box = layout.box()
        
        parts_collection = self.get_parts_collection(context)
        if not parts_collection:
            box.label(text="Parts collection not found!", icon='ERROR')
            return

        for i, bone_item in enumerate(self.bone_names):
            if i < len(self.mesh_names):
                row = box.row()
                row.label(text=bone_item.name)
                # Use prop_search, but limit its search context to the rig's parts_collection
                row.prop_search(self.mesh_names[i], "name", parts_collection, "objects", text="")

    def execute(self, context):
        armature = bpy.data.objects.get(context.scene.rbx_anim_armature)
        master_collection = find_master_collection_for_object(armature)
        parts_collection = find_parts_collection_in_master(master_collection, create_if_missing=True)

        if not parts_collection:
            self.report({'ERROR'}, "Could not find 'Parts' collection to execute on.")
            return {'CANCELLED'}

        # 1. Get the final desired state from the UI
        new_assignments = {}
        for i, bone_item in enumerate(self.bone_names):
            if i < len(self.mesh_names) and self.mesh_names[i].name:
                mesh_obj = bpy.data.objects.get(self.mesh_names[i].name)
                if mesh_obj:
                    if mesh_obj.name not in parts_collection.objects:
                        parts_collection.objects.link(mesh_obj)
                    for col in list(mesh_obj.users_collection):
                        if col != parts_collection and col.name.endswith('_Parts'):
                            col.objects.unlink(mesh_obj)
                    new_assignments[mesh_obj] = bone_item.name

        # 2. Update constraints for all objects within this rig's parts collection
        for obj in parts_collection.objects:
            if obj.type != 'MESH':
                continue

            # First, remove any existing CHILD_OF constraint that targets this armature
            for c in obj.constraints:
                if c.type == 'CHILD_OF' and c.target == armature:
                    obj.constraints.remove(c)
            
            # Now, if this object is in our new assignment list, add the new constraint
            if obj in new_assignments:
                bone_name = new_assignments[obj]
                constraint = obj.constraints.new(type='CHILD_OF')
                constraint.target = armature
                constraint.subtarget = bone_name

        self.report({'INFO'}, "Constraints updated.")
        return {'FINISHED'}
