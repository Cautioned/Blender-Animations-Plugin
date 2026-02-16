"""
Weapon/accessory attachment operators.

Lets users import external OBJ meshes and attach them to bones on an
existing Roblox rig, with optional bone creation for independent animation.
"""

import bpy
from bpy_extras.io_utils import ImportHelper
from ..core.utils import (
    find_master_collection_for_object,
    find_parts_collection_in_master,
    get_object_by_name,
    object_exists,
)


class OBJECT_OT_AttachMeshToBone(bpy.types.Operator):
    """Attach one or more selected meshes to a bone on the active armature.
    
    Creates a CHILD_OF constraint so the mesh follows the bone.
    Optionally creates a new child bone for independent weapon animation.
    """
    bl_idname = "object.rbxanims_attach_to_bone"
    bl_label = "Attach to Bone"
    bl_description = (
        "Attach the selected mesh(es) to a bone on the target armature. "
        "The mesh will follow the bone during animation."
    )
    bl_options = {"REGISTER", "UNDO"}

    # --- properties shown in the invoke dialog ---
    
    bone_name: bpy.props.StringProperty(
        name="Bone",
        description="Bone to attach the mesh to",
        default="",
    )
    
    create_bone: bpy.props.BoolProperty(
        name="Create Weapon Bone",
        description=(
            "Create a new child bone for this weapon so it can be "
            "animated independently (e.g. sword swing). If disabled, "
            "the weapon just rigidly follows the parent bone."
        ),
        default=False,
    )
    
    weapon_bone_name: bpy.props.StringProperty(
        name="Bone Name",
        description="Name for the new weapon bone",
        default="Weapon",
    )

    move_to_parts: bpy.props.BoolProperty(
        name="Move to Parts Collection",
        description="Move the mesh into the rig's Parts collection",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        if not object_exists(arm_name, context.scene):
            return False
        # need at least one selected mesh
        return any(obj.type == "MESH" and obj.select_get() for obj in context.selected_objects)

    def invoke(self, context, event):
        settings = getattr(context.scene, "rbx_anim_settings", None)
        armature = get_object_by_name(settings.rbx_anim_armature, context.scene) if settings else None
        if not armature:
            self.report({"ERROR"}, "No target armature selected")
            return {"CANCELLED"}
        
        # default bone name: active bone if in pose mode, else first bone
        if armature.mode == "POSE" and context.active_pose_bone:
            self.bone_name = context.active_pose_bone.name
        elif armature.data.bones:
            self.bone_name = armature.data.bones[0].name
        
        # default weapon bone name from first selected mesh
        for obj in context.selected_objects:
            if obj.type == "MESH":
                self.weapon_bone_name = obj.name
                break
        
        return context.window_manager.invoke_props_dialog(self, width=350)

    def draw(self, context):
        layout = self.layout
        settings = getattr(context.scene, "rbx_anim_settings", None)
        armature = get_object_by_name(settings.rbx_anim_armature, context.scene) if settings else None
        
        # bone picker
        if armature:
            layout.prop_search(self, "bone_name", armature.data, "bones", text="Parent Bone")
        else:
            layout.prop(self, "bone_name")
        
        layout.separator()
        layout.prop(self, "create_bone")
        if self.create_bone:
            layout.prop(self, "weapon_bone_name")
        
        layout.separator()
        layout.prop(self, "move_to_parts")
        
        # show what will be attached
        meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
        layout.separator()
        layout.label(text=f"{len(meshes)} mesh(es) selected", icon="MESH_DATA")

    def execute(self, context):
        settings = getattr(context.scene, "rbx_anim_settings", None)
        armature = get_object_by_name(settings.rbx_anim_armature, context.scene) if settings else None
        if not armature or armature.type != "ARMATURE":
            self.report({"ERROR"}, "No valid armature selected")
            return {"CANCELLED"}
        
        if not self.bone_name or self.bone_name not in armature.data.bones:
            self.report({"ERROR"}, f"Bone '{self.bone_name}' not found on armature")
            return {"CANCELLED"}
        
        meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "No mesh objects selected")
            return {"CANCELLED"}
        
        target_bone_name = self.bone_name
        
        # optionally create a new child bone
        if self.create_bone:
            target_bone_name = self._create_weapon_bone(
                context, armature, self.bone_name, self.weapon_bone_name, meshes
            )
            if not target_bone_name:
                return {"CANCELLED"}
        
        # move meshes to parts collection if requested
        if self.move_to_parts:
            master_coll = find_master_collection_for_object(armature)
            parts_coll = find_parts_collection_in_master(master_coll, create_if_missing=True)
            if parts_coll:
                for obj in meshes:
                    # unlink from current collections, link to parts
                    for coll in list(obj.users_collection):
                        coll.objects.unlink(obj)
                    parts_coll.objects.link(obj)
        
        # create CHILD_OF constraints
        attached = []
        for obj in meshes:
            # remove existing CHILD_OF constraints targeting this armature
            for c in list(obj.constraints):
                if c.type == "CHILD_OF" and c.target == armature:
                    obj.constraints.remove(c)
            
            constraint = obj.constraints.new(type="CHILD_OF")
            constraint.target = armature
            constraint.subtarget = target_bone_name
            
            # set inverse matrix so the mesh stays at its current position
            # relative to the bone
            bone = armature.data.bones[target_bone_name]
            bone_mat = bone.matrix_local
            if hasattr(bone_mat, "to_4x4"):
                bone_mat = bone_mat.to_4x4()
            constraint.inverse_matrix = (armature.matrix_world @ bone_mat).inverted()
            
            attached.append(obj.name)
        
        bone_info = f" (new bone '{target_bone_name}')" if self.create_bone else ""
        self.report({"INFO"}, f"Attached {len(attached)} mesh(es) to '{self.bone_name}'{bone_info}")
        return {"FINISHED"}
    
    def _create_weapon_bone(self, context, armature, parent_bone_name, weapon_name, meshes):
        """Create a new child bone on the armature for the weapon.
        
        The bone is positioned at the centroid of all weapon meshes.
        Returns the new bone's name, or None on failure.
        """
        from mathutils import Vector
        
        # compute weapon centroid from all meshes
        centers = []
        for obj in meshes:
            if obj.data.vertices:
                min_co = [float('inf')] * 3
                max_co = [float('-inf')] * 3
                for v in obj.data.vertices:
                    world_co = obj.matrix_world @ v.co
                    for i in range(3):
                        min_co[i] = min(min_co[i], world_co[i])
                        max_co[i] = max(max_co[i], world_co[i])
                centers.append(Vector([(min_co[i] + max_co[i]) / 2.0 for i in range(3)]))
            else:
                centers.append(obj.matrix_world.to_translation())
        
        if not centers:
            self.report({"ERROR"}, "No valid mesh geometry for bone placement")
            return None
        
        centroid = sum(centers, Vector((0, 0, 0))) / len(centers)
        
        # switch to edit mode on the armature to add the bone
        prev_active = context.view_layer.objects.active
        prev_mode = armature.mode if armature == context.view_layer.objects.active else None
        
        context.view_layer.objects.active = armature
        bpy.ops.object.mode_set(mode="EDIT")
        
        try:
            edit_bones = armature.data.edit_bones
            
            # ensure unique name
            final_name = weapon_name
            counter = 1
            while final_name in edit_bones:
                final_name = f"{weapon_name}.{counter:03d}"
                counter += 1
            
            # create bone
            new_bone = edit_bones.new(final_name)
            new_bone.head = armature.matrix_world.inverted() @ centroid
            # tail offset — small extension along parent's direction or Y-up
            parent_ebone = edit_bones.get(parent_bone_name)
            if parent_ebone:
                new_bone.parent = parent_ebone
                direction = (parent_ebone.tail - parent_ebone.head).normalized()
                new_bone.tail = new_bone.head + direction * 0.3
            else:
                new_bone.tail = new_bone.head + Vector((0, 0, 0.3))
            
            new_bone.use_deform = False
            
            created_name = new_bone.name  # blender may have changed it
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
            if prev_active:
                context.view_layer.objects.active = prev_active
            if prev_mode and prev_active == armature:
                bpy.ops.object.mode_set(mode=prev_mode)
        
        return created_name


class OBJECT_OT_ImportAndAttach(bpy.types.Operator, ImportHelper):
    """Import an OBJ file and attach it to a bone on the active rig.
    
    Combines OBJ import + attach-to-bone in one step.
    """
    bl_idname = "object.rbxanims_import_and_attach"
    bl_label = "Import Weapon/Accessory (.obj)"
    bl_description = (
        "Import an OBJ file and attach it to a bone on the target armature"
    )
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".obj"
    filter_glob: bpy.props.StringProperty(default="*.obj", options={"HIDDEN"})
    filepath: bpy.props.StringProperty(name="File Path", maxlen=1024, default="")

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        return object_exists(arm_name, context.scene)

    def execute(self, context):
        # remember what's already in the scene
        before = {obj.name for obj in context.scene.objects}
        
        # import the OBJ
        if bpy.app.version >= (4, 0, 0):
            bpy.ops.wm.obj_import(
                filepath=self.filepath,
                use_split_groups=True,
            )
        else:
            bpy.ops.import_scene.obj(
                filepath=self.filepath,
                use_split_groups=True,
            )
        
        # find newly imported meshes
        new_meshes = [
            obj for obj in context.scene.objects
            if obj.name not in before and obj.type == "MESH"
        ]
        
        if not new_meshes:
            self.report({"WARNING"}, "No mesh objects found in imported file")
            return {"CANCELLED"}
        
        # select only the new meshes
        bpy.ops.object.select_all(action="DESELECT")
        for obj in new_meshes:
            obj.select_set(True)
        context.view_layer.objects.active = new_meshes[0]
        
        # invoke the attach dialog
        return bpy.ops.object.rbxanims_attach_to_bone("INVOKE_DEFAULT")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}
