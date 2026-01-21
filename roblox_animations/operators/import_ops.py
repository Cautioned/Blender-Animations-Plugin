"""
Import operators for rig and animation data.
"""

import json
import base64
import re
import bpy
from bpy_extras.io_utils import ImportHelper
from ..core.utils import get_unique_name
from ..core.utils import cf_to_mat
from ..core.constants import get_transform_to_blender
from ..rig.creation import autoname_parts, get_unique_collection_name


def _strip_suffix(name: str) -> str:
    return re.sub(r"\.\d+$", "", name or "")


def _get_mesh_world_center(obj):
    """Get the geometric center of a mesh in world space (from actual vertices)."""
    if obj.type != "MESH" or not obj.data.vertices:
        return obj.matrix_world.to_translation()
    
    # Calculate bounding box center in local space
    verts = obj.data.vertices
    min_co = [float('inf')] * 3
    max_co = [float('-inf')] * 3
    
    for v in verts:
        for i in range(3):
            min_co[i] = min(min_co[i], v.co[i])
            max_co[i] = max(max_co[i], v.co[i])
    
    # Local center
    local_center = [(min_co[i] + max_co[i]) / 2.0 for i in range(3)]
    
    # Transform to world space
    from mathutils import Vector
    world_center = obj.matrix_world @ Vector(local_center)
    return world_center


def _fingerprint_position(loc, precision=2) -> str:
    """Create a position-only fingerprint for coarse matching."""
    return f"{round(loc.x, precision)},{round(loc.y, precision)},{round(loc.z, precision)}"


def _rename_parts_by_fingerprint(rig_def, parts_collection):
    """Rename meshes using transform position matching from rig metadata.
    
    Uses fingerprint matching first, then falls back to nearest-neighbor for collisions.
    Computes geometric center of meshes (from vertices) since OBJ import places objects at origin.
    """
    if not rig_def:
        print("[RigImport] No rig definition provided")
        return False

    t2b = get_transform_to_blender()
    used = set()
    
    # Build position index with multiple precision levels for fallback
    position_index_p2 = {}  # precision 2 (0.01 units)
    position_index_p1 = {}  # precision 1 (0.1 units)
    position_index_p0 = {}  # precision 0 (1 unit)
    
    mesh_objects = [obj for obj in parts_collection.objects if obj.type == "MESH"]
    print(f"[RigImport] Building position index for {len(mesh_objects)} mesh objects (using geometric centers)")
    
    # Precompute geometric centers for all meshes
    mesh_centers = {}
    for obj in mesh_objects:
        mesh_centers[obj] = _get_mesh_world_center(obj)
    
    # Log first few objects for debugging
    for i, obj in enumerate(mesh_objects[:5]):
        loc = mesh_centers[obj]
        print(f"[RigImport]   Mesh '{obj.name}' geometric center: ({loc.x:.4f}, {loc.y:.4f}, {loc.z:.4f})")
    if len(mesh_objects) > 5:
        print(f"[RigImport]   ... and {len(mesh_objects) - 5} more meshes")
    
    for obj in mesh_objects:
        loc = mesh_centers[obj]
        for prec, idx in [(2, position_index_p2), (1, position_index_p1), (0, position_index_p0)]:
            fp = _fingerprint_position(loc, prec)
            idx.setdefault(fp, []).append(obj)
    
    # Log fingerprint index stats
    print(f"[RigImport] Position index sizes: p2={len(position_index_p2)}, p1={len(position_index_p1)}, p0={len(position_index_p0)}")
    
    def find_nearest_unused(target_loc, max_distance=0.5):
        """Find the nearest unused mesh within max_distance."""
        best_obj = None
        best_dist = max_distance
        for obj in mesh_objects:
            if obj in used:
                continue
            loc = mesh_centers[obj]
            dist = (loc - target_loc).length
            if dist < best_dist:
                best_dist = dist
                best_obj = obj
        return best_obj, best_dist
    
    def match_by_position(cf, aux_name_for_log):
        """Try to match by position fingerprint, then nearest neighbor."""
        if not cf:
            return None
        
        try:
            raw_mat = cf_to_mat(cf)
            expected_mat = t2b @ raw_mat
            expected_loc = expected_mat.to_translation()
        except Exception as e:
            print(f"[RigImport]   '{aux_name_for_log}' Failed to convert CFrame: {e}")
            return None
        
        # Try each precision level from finest to coarsest
        for prec, idx in [(2, position_index_p2), (1, position_index_p1), (0, position_index_p0)]:
            fp = _fingerprint_position(expected_loc, prec)
            candidates = idx.get(fp, [])
            available = [o for o in candidates if o not in used]
            if available:
                print(f"[RigImport]   '{aux_name_for_log}' MATCHED at precision {prec} fp='{fp}' -> '{available[0].name}'")
                return available[0]
        
        # Fallback: find nearest unused mesh within tolerance
        nearest, dist = find_nearest_unused(expected_loc)
        if nearest:
            print(f"[RigImport]   '{aux_name_for_log}' NEAREST match (dist={dist:.4f}) -> '{nearest.name}'")
            return nearest
        
        print(f"[RigImport]   '{aux_name_for_log}' NO MATCH at ({expected_loc.x:.4f}, {expected_loc.y:.4f}, {expected_loc.z:.4f})")
        return None

    matched_count = 0
    unmatched_names = []
    # Collect renames first, then apply in two passes to avoid name collisions
    pending_renames = []  # List of (obj, target_name)

    def walk(node, depth=0):
        nonlocal matched_count
        jname = node.get("jname") or node.get("pname") or ""
        children = node.get("children") or []
        indent = "  " * depth
        
        # Get the node's own transform (the part's world CFrame)
        node_transform = node.get("transform")
        
        # Also check auxTransform - first entry is typically the part itself
        aux_transforms = node.get("auxTransform") or []
        aux_names = node.get("aux") or []
        
        # Skip root part (HumanoidRootPart) - it conflicts with UpperTorso position
        # and doesn't need fingerprint matching
        is_root = (depth == 0)
        
        # Try to match this node's part using its transform (skip root)
        if jname and node_transform and not is_root:
            print(f"[RigImport] {indent}Matching node '{jname}' using transform")
            obj = match_by_position(node_transform, jname)
            if obj:
                current_base = _strip_suffix(obj.name)
                if current_base != jname:
                    print(f"[RigImport] {indent}  WILL RENAME: '{obj.name}' -> '{jname}'")
                    pending_renames.append((obj, jname))
                    matched_count += 1
                else:
                    print(f"[RigImport] {indent}  '{jname}' already correctly named")
                used.add(obj)
            else:
                unmatched_names.append(jname)
        elif is_root:
            print(f"[RigImport] {indent}Skipping root node '{jname}' (no fingerprint match needed)")
        
        # Also process aux entries if they have names
        for idx, aux_name in enumerate(aux_names):
            if not aux_name:
                continue
            cf = aux_transforms[idx] if idx < len(aux_transforms) else None
            if not cf:
                continue
            
            obj = match_by_position(cf, aux_name)
            if obj:
                current_base = _strip_suffix(obj.name)
                if current_base != aux_name:
                    print(f"[RigImport] {indent}  AUX WILL RENAME: '{obj.name}' -> '{aux_name}'")
                    pending_renames.append((obj, aux_name))
                    matched_count += 1
                used.add(obj)
        
        for child in children:
            walk(child, depth + 1)

    walk(rig_def)
    
    # Two-pass rename to avoid name collisions (e.g., Handle2->Handle1 when Handle1 exists)
    # Pass 1: Rename all to temporary unique names
    print(f"[RigImport] Applying {len(pending_renames)} renames (two-pass to avoid collisions)")
    temp_names = []
    for i, (obj, _) in enumerate(pending_renames):
        temp_name = f"__rbxtemp_{i}__"
        temp_names.append((obj, temp_name))
        obj.name = temp_name
    
    # Pass 2: Rename to final target names
    for i, (obj, target_name) in enumerate(pending_renames):
        print(f"[RigImport]   RENAME: '{temp_names[i][1]}' -> '{target_name}'")
        obj.name = target_name
    
    print(f"[RigImport] " + "="*50)
    print(f"[RigImport] SUMMARY: {matched_count} parts renamed, {len(unmatched_names)} unmatched")
    if unmatched_names:
        print(f"[RigImport] Unmatched parts: {unmatched_names}")
    print(f"[RigImport] " + "="*50)
    
    return matched_count > 0


def _parts_list_from_rig_def(rig_def):
    """Derive a deterministic parts list from rig metadata."""
    if not rig_def:
        return []
    parts = {}

    def walk(node):
        if not node:
            return
        local_pname = node.get("pname") or node.get("jname")
        if local_pname:
            parts[local_pname] = True

        aux = node.get("aux") or []
        for aux_name in aux:
            if aux_name:
                parts[aux_name] = True

        for child in node.get("children") or []:
            walk(child)

    walk(rig_def)

    return sorted(parts.keys())


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
        imported_objs = [
            obj for obj in bpy.data.objects if obj.name not in objnames_before_import
        ]

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
        rig_part_objs = [obj for obj in imported_objs if obj not in meta_objs_to_delete]

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

            # Try to restore correct names using transform fingerprints.
            _rename_parts_by_fingerprint(meta_loaded.get("rig"), parts_collection)

            # Optional legacy fallback: if fingerprinting didn't rename, try autoname using provided parts list.
            parts_payload = meta_loaded.get("parts")
            if isinstance(parts_payload, list):
                parts_list = parts_payload
            else:
                parts_list = _parts_list_from_rig_def(meta_loaded.get("rig"))

            if parts_list and meta_loaded.get("rigName"):
                unmatched_objects = [obj for obj in parts_collection.objects if obj.type == "MESH"]
                if unmatched_objects:
                    autoname_parts(
                        parts_list,
                        meta_loaded["rigName"],
                        unmatched_objects,
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
        from ..core.utils import get_action_fcurves
        import math

        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        
        # Get target armature early to fail fast
        armature = bpy.data.objects.get(armature_name)
        if not armature:
            self.report(
                {"ERROR"},
                f"No armature named '{armature_name}' found. Please ensure the correct rig is selected.",
            )
            return {"CANCELLED"}

        # Ensure active keying set exists, create one if needed
        if not bpy.context.scene.keying_sets.active:
            bpy.ops.anim.keying_set_add()
            self.report({"INFO"}, "Created new keying set for animation import.")

        # Import and keep track of what is imported (use set for faster lookup)
        objnames_before_import = {obj.name for obj in bpy.data.objects}
        bpy.ops.import_scene.fbx(filepath=self.properties.filepath)
        objnames_imported = [
            obj.name for obj in bpy.data.objects if obj.name not in objnames_before_import
        ]

        def clear_imported():
            """Clean up all objects imported from the FBX file."""
            for obj_name in objnames_imported:
                obj = bpy.data.objects.get(obj_name)
                if obj:
                    bpy.data.objects.remove(obj)

        # Check that there's exactly 1 armature in the imported file
        armatures_imported = [
            obj for obj in bpy.data.objects
            if obj.type == "ARMATURE" and obj.name in objnames_imported
        ]
        if len(armatures_imported) == 0:
            self.report({"ERROR"}, "Imported FBX file contains no armature.")
            clear_imported()
            return {"CANCELLED"}
        if len(armatures_imported) > 1:
            self.report(
                {"ERROR"},
                f"Imported FBX file contains {len(armatures_imported)} armatures, expected 1.",
            )
            clear_imported()
            return {"CANCELLED"}

        ao_imp = armatures_imported[0]

        # Validate bone mapping between source and target
        err_mappings = get_mapping_error_bones(armature, ao_imp)
        if err_mappings:
            self.report(
                {"ERROR"},
                f"Cannot map rig, the following bones are missing from the source rig: {', '.join(err_mappings)}.",
            )
            clear_imported()
            return {"CANCELLED"}

        # Validate imported armature has animation data
        if not ao_imp.animation_data or not ao_imp.animation_data.action:
            self.report({"ERROR"}, "Imported FBX armature contains no animation data.")
            clear_imported()
            return {"CANCELLED"}

        fcurves = get_action_fcurves(ao_imp.animation_data.action)
        if not fcurves:
            self.report({"ERROR"}, "Imported FBX armature contains no animation curves.")
            clear_imported()
            return {"CANCELLED"}

        # Get keyframes and set frame range
        kp_frames = [kp.co.x for fcurve in fcurves for kp in fcurve.keyframe_points]
        if not kp_frames:
            self.report({"ERROR"}, "Imported FBX armature contains no keyframes.")
            clear_imported()
            return {"CANCELLED"}

        bpy.context.scene.frame_start = math.floor(min(kp_frames))
        bpy.context.scene.frame_end = math.ceil(max(kp_frames))

        # Apply transforms and prepare for keyframe mapping
        bpy.context.view_layer.objects.active = ao_imp
        apply_ao_transform(ao_imp)
        prepare_for_kf_map()

        # Ensure the target armature has animation_data initialized
        if armature.animation_data is None:
            armature.animation_data_create()

        # Copy animation state from imported armature to target
        copy_anim_state(armature, ao_imp)

        clear_imported()
        self.report({"INFO"}, f"Successfully imported animation with {len(kp_frames)} keyframes.")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}
