"""
Rig creation and bone management utilities.
"""

import json
import re
import bpy
from mathutils import Vector, Matrix
from ..core.constants import get_transform_to_blender
from ..core.utils import (
    cf_to_mat,
    get_unique_name,
    find_master_collection_for_object,
    find_parts_collection_in_master,
)


def _matrix_to_idprop(value):
    """Convert Matrix values to list-of-lists so IDProperties accept them."""
    if isinstance(value, Matrix):
        return [list(row) for row in value]
    return value


def _strip_suffix(name: str) -> str:
    """Strip .001/.002 style suffixes for stable matching."""
    return re.sub(r"\.\d+$", "", name or "")


def _fingerprint_position(matrix: Matrix, precision: int = 2) -> str:
    """Create a position-only fingerprint for coarse matching."""
    loc = matrix.to_translation()
    return f"{round(loc.x, precision)},{round(loc.y, precision)},{round(loc.z, precision)}"


def _build_match_context(parts_collection):
    """Precompute lookup maps for matching imported meshes to rig metadata."""
    name_index = {}
    # Position indices at multiple precision levels
    position_index_p2 = {}  # precision 2 (0.01 units)
    position_index_p1 = {}  # precision 1 (0.1 units)
    position_index_p0 = {}  # precision 0 (1 unit)
    
    for obj in parts_collection.objects:
        if obj.type != "MESH":
            continue
        base = _strip_suffix(obj.name).lower()
        name_index.setdefault(base, []).append(obj)
        for prec, idx in [(2, position_index_p2), (1, position_index_p1), (0, position_index_p0)]:
            fp = _fingerprint_position(obj.matrix_world, prec)
            idx.setdefault(fp, []).append(obj)

    return {
        "name_index": name_index,
        "position_index_p2": position_index_p2,
        "position_index_p1": position_index_p1,
        "position_index_p0": position_index_p0,
        "used": set(),
        "t2b": get_transform_to_blender(),
        "parts_collection": parts_collection,
    }


def _find_matching_part(aux_name, aux_cf, match_ctx):
    """Resolve an aux entry to a mesh by name first, then conservative position fingerprint."""
    name_index = match_ctx["name_index"]
    used = match_ctx["used"]
    t2b = match_ctx["t2b"]

    # Name-based candidates (base name match, ignoring suffixes)
    candidates = []
    base_name = _strip_suffix(aux_name or "").lower()
    if base_name and base_name in name_index:
        for obj in name_index[base_name]:
            if obj not in used:
                candidates.append(obj)

    if candidates:
        return candidates[0]

    # Position fingerprint fallback at multiple precision levels
    # Only accept unambiguous matches within a small distance threshold.
    if aux_cf:
        try:
            expected_mat = t2b @ cf_to_mat(aux_cf)
            expected_pos = expected_mat.to_translation()
            parts_collection = match_ctx.get("parts_collection")
            max_dist = 0.05

            for prec in [2, 1, 0]:
                fp = _fingerprint_position(expected_mat, prec)
                idx = match_ctx.get(f"position_index_p{prec}", {})
                candidates = [obj for obj in idx.get(fp, []) if obj not in used]
                if len(candidates) != 1:
                    continue

                obj = candidates[0]
                actual_pos = obj.matrix_world.to_translation()
                if (actual_pos - expected_pos).length <= max_dist:
                    return obj
        except Exception:
            pass
    return None


def _apply_fingerprint_renames(rig_def, match_ctx):
    """Rename meshes by comparing position fingerprints from rig metadata."""
    name_index = match_ctx["name_index"]

    def walk(node):
        aux_list = node.get("aux") or []
        aux_tf = node.get("auxTransform") or []
        for idx, aux_name in enumerate(aux_list):
            if not aux_name:
                continue
            aux_cf = aux_tf[idx] if idx < len(aux_tf) else None
            if not aux_cf:
                continue
            obj = _find_matching_part(aux_name, aux_cf, match_ctx)
            if obj and _strip_suffix(obj.name) != aux_name:
                obj.name = aux_name
                base = _strip_suffix(obj.name).lower()
                name_index.setdefault(base, []).append(obj)
        for child in node.get("children", []):
            walk(child)

    walk(rig_def)


def get_unique_collection_name(basename):
    """Generate a unique collection name to avoid conflicts."""
    if basename not in bpy.data.collections:
        return basename
    i = 1
    while True:
        name = f"{basename}.{i:03d}"
        if name not in bpy.data.collections:
            return name
        i += 1


def autoname_parts(partnames, basename, objects_to_rename):
    """Rename parts to match metadata-defined names"""
    indexmatcher = re.compile(basename + "(\d+)1(\.\d+)?", re.IGNORECASE)
    for object in objects_to_rename:
        match = indexmatcher.match(object.name.lower())
        if match:
            try:
                index = int(match.group(1))
                if 0 <= index - 1 < len(partnames):
                    object.name = partnames[index - 1]
                else:
                    print(
                        f"Warning: Index {index} out of range for partnames list (length: {len(partnames)})"
                    )
            except Exception as e:
                print(f"Error renaming part {object.name}: {str(e)}")


def load_rigbone(ao, rigging_type, rigsubdef, parent_bone, parts_collection, match_ctx):
    """Load a single rig bone with its children"""
    amt = ao.data
    bone = amt.edit_bones.new(rigsubdef["jname"])
    joint_type = rigsubdef.get("jointType") or "Motor6D"
    is_weld = joint_type in ("Weld", "WeldConstraint")

    mat = cf_to_mat(rigsubdef["transform"])
    bone["transform"] = _matrix_to_idprop(mat)
    t2b = get_transform_to_blender()
    bone_dir = (t2b @ mat).to_3x3().to_4x4() @ Vector((0, 0, 1))

    # Check if this bone is marked as a deform bone from Studio export
    is_deform_bone = rigsubdef.get("isDeformBone", False)
    if joint_type:
        # Preserve joint type for downstream serialization/diagnostics (Motor6D/Weld/WeldConstraint/Bone)
        bone["rbx_joint_type"] = joint_type
    if is_deform_bone:
        # Mark as a deform bone for proper animation import handling
        bone["rbx_is_deform_bone"] = True
        bone["is_transformable"] = True
        bone.use_deform = True

    if "jointtransform0" not in rigsubdef:
        # Rig root
        bone.head = (t2b @ mat).to_translation()
        bone.tail = (t2b @ mat) @ Vector((0, 0.01, 0))
        bone["transform0"] = _matrix_to_idprop(Matrix())
        bone["transform1"] = _matrix_to_idprop(Matrix())
        bone["nicetransform"] = _matrix_to_idprop(Matrix())
        bone.align_roll(bone_dir)
        bone.hide_select = True
        pre_mat = bone.matrix
        o_trans = t2b @ mat
    else:
        mat0 = cf_to_mat(rigsubdef["jointtransform0"])
        mat1 = cf_to_mat(rigsubdef["jointtransform1"])
        bone["transform0"] = _matrix_to_idprop(mat0)
        bone["transform1"] = _matrix_to_idprop(mat1)
        # Only set is_transformable for Motor6D bones if not already set for deform bones
        if not is_deform_bone:
            bone["is_transformable"] = True

        bone.parent = parent_bone
        o_trans = t2b @ (mat @ mat1)
        bone.head = o_trans.to_translation()
        real_tail = o_trans @ Vector((0, 0.25, 0))

        neutral_pos = (t2b @ mat).to_translation()
        bone.tail = real_tail
        bone.align_roll(bone_dir)

        # Store neutral matrix before any transforms (needed for all modes)
        pre_mat = bone.matrix

        # For RAW (nodes only), use original bone positions without any modifications
        # This preserves the exact bone structure from the original rig data
        if rigging_type != "RAW":
            # For other rigging types, apply "nice" transforms for better visualization/IK
            if len(rigsubdef["children"]) == 1:
                nextmat = cf_to_mat(rigsubdef["children"][0]["transform"])
                nextmat1 = cf_to_mat(rigsubdef["children"][0]["jointtransform1"])
                next_joint_pos = (t2b @ (nextmat @ nextmat1)).to_translation()

                if rigging_type == "CONNECT":  # Instantly connect
                    bone.tail = next_joint_pos
                else:
                    # For LOCAL_AXIS_EXTEND, determine best axis (calculation kept for consistency with backup.py)
                    if rigging_type == "LOCAL_AXIS_EXTEND":  # Allow non-Y too
                        invtrf = pre_mat.inverted() @ next_joint_pos
                        bestdist = abs(invtrf.y)
                        for paxis in ["x", "z"]:
                            dist = abs(getattr(invtrf, paxis))
                            if dist > bestdist:
                                bestdist = dist

                    ppd_nr_dir = real_tail - bone.head
                    ppd_nr_dir.normalize()
                    proj = ppd_nr_dir.dot(next_joint_pos - bone.head)
                    vis_world_root = ppd_nr_dir * proj
                    bone.tail = bone.head + vis_world_root

            else:
                bone.tail = bone.head + (bone.head - neutral_pos) * -2

            if (bone.tail - bone.head).length < 0.01:
                # just reset, no "nice" config can be found
                bone.tail = real_tail
                bone.align_roll(bone_dir)

    # fix roll
    bone.align_roll(bone_dir)

    post_mat = bone.matrix

    # this value stores the transform between the "proper" matrix and the "nice" matrix where bones are oriented in a more friendly way
    # For RAW mode, this should be close to identity since we're not applying nice transforms
    bone["nicetransform"] = _matrix_to_idprop(o_trans.inverted() @ post_mat)

    # link objects to bone by matching name, then fingerprint
    aux_transform_list = rigsubdef.get("auxTransform") or []
    for idx, aux_name in enumerate(rigsubdef["aux"]):
        if not aux_name:
            continue

        local_cf = aux_transform_list[idx] if idx < len(aux_transform_list) else None
        found_obj = _find_matching_part(aux_name, local_cf, match_ctx)

        if found_obj:
            match_ctx["used"].add(found_obj)
            # Queue constraint for later - can't apply in edit mode
            pending = match_ctx.setdefault("pending_constraints", [])
            pending.append((found_obj, bone.name))

    # handle child bones
    for child in rigsubdef["children"]:
        load_rigbone(ao, rigging_type, child, bone, parts_collection, match_ctx)


def _get_or_create_weld_bone_shape():
    """Get or create a simple line curve to use as custom bone shape for welds."""
    shape_name = "__WeldBoneShape"
    
    # Check if it already exists
    if shape_name in bpy.data.objects:
        return bpy.data.objects[shape_name]
    
    # Create a simple line curve
    curve_data = bpy.data.curves.new(name=shape_name, type='CURVE')
    curve_data.dimensions = '3D'
    
    # Create a simple straight line spline
    spline = curve_data.splines.new('POLY')
    spline.points.add(1)  # Start with 1 point, add 1 more = 2 points total
    spline.points[0].co = (0, 0, 0, 1)
    spline.points[1].co = (0, 1, 0, 1)  # Line along Y axis (bone direction)
    
    # Create the object
    shape_obj = bpy.data.objects.new(shape_name, curve_data)
    
    # Don't link to any collection - it's just for bone display
    shape_obj.hide_viewport = True
    shape_obj.hide_render = True
    
    return shape_obj


def _configure_weld_bones(armature_obj):
    """Configure weld bones with custom display (wire/line) and lock them from animation."""
    amt = armature_obj.data
    
    # Get or create the custom bone shape for welds
    weld_shape = _get_or_create_weld_bone_shape()
    
    # Switch to pose mode to access pose bones
    bpy.context.view_layer.objects.active = armature_obj
    bpy.ops.object.mode_set(mode="POSE")
    
    for bone in amt.bones:
        joint_type = bone.get("rbx_joint_type", "Motor6D")
        if joint_type in ("Weld", "WeldConstraint"):
            pose_bone = armature_obj.pose.bones.get(bone.name)
            if pose_bone:
                # Assign custom shape (line)
                pose_bone.custom_shape = weld_shape
                pose_bone.use_custom_shape_bone_size = True
                
                # Lock all transforms to prevent accidental animation
                pose_bone.lock_location = (True, True, True)
                pose_bone.lock_rotation = (True, True, True)
                pose_bone.lock_rotation_w = True
                pose_bone.lock_scale = (True, True, True)
                
                # Use custom bone color group to visually distinguish weld bones
                if hasattr(pose_bone, "color"):
                    # Blender 4.0+ bone colors
                    pose_bone.color.palette = 'CUSTOM'
                    pose_bone.color.custom.normal = (0.3, 0.3, 0.3)
                    pose_bone.color.custom.select = (0.5, 0.5, 0.5)
                    pose_bone.color.custom.active = (0.6, 0.6, 0.6)
    
    bpy.ops.object.mode_set(mode="OBJECT")


def create_rig(rigging_type, rig_meta_obj_name):
    """Create a complete rig from metadata"""
    # Ensure a clean slate by deselecting everything
    if bpy.ops.object.select_all.poll():
        bpy.ops.object.select_all(action="DESELECT")

    # Ensure we are in object mode
    if bpy.context.active_object and bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    rig_meta_obj = bpy.data.objects.get(rig_meta_obj_name)
    if not rig_meta_obj:
        raise ValueError(f"Rig meta object '{rig_meta_obj_name}' not found.")
        return

    # Find the master collection and parts collection for the meta object
    master_collection = find_master_collection_for_object(rig_meta_obj)
    if not master_collection:
        raise ValueError(
            f"Could not find a master collection for rig meta object '{rig_meta_obj_name}'."
        )
        return

    parts_collection = find_parts_collection_in_master(master_collection)
    if not parts_collection:
        raise ValueError(
            f"Could not find a 'Parts' collection inside '{master_collection.name}'."
        )
        return

    # Build a matching context so we can resolve meshes even if Roblox renames them.
    match_ctx = _build_match_context(parts_collection)

    # --- Deletion of old Armature ---
    # Find and delete any existing armature within this rig's master collection
    old_armature = None
    for obj in master_collection.objects:
        if obj.type == "ARMATURE":
            old_armature = obj
            break

    if old_armature:
        bpy.data.objects.remove(old_armature, do_unlink=True)

    # Set the meta object as active to provide context for subsequent operators
    bpy.context.view_layer.objects.active = rig_meta_obj

    meta_loaded = json.loads(rig_meta_obj["RigMeta"])
    # Try to restore correct part names using fingerprinting before building constraints.
    _apply_fingerprint_renames(meta_loaded["rig"], match_ctx)

    bpy.ops.object.add(type="ARMATURE", enter_editmode=True, location=(0, 0, 0))
    ao = bpy.context.object
    ao.show_in_front = True

    # Move the new armature into the master collection
    for coll in ao.users_collection:
        coll.objects.unlink(ao)
    master_collection.objects.link(ao)

    # Set a unique name for the armature based on the rig name
    rig_name = meta_loaded.get("rigName", "Rig")
    ao.name = get_unique_name(f"__{rig_name}_Armature")
    amt = ao.data
    amt.name = get_unique_name(f"__{rig_name}_RigArm")
    amt.show_axes = True
    amt.show_names = True

    bpy.ops.object.mode_set(mode="EDIT")
    # Pass the specific parts_collection to be used for constraining
    load_rigbone(ao, rigging_type, meta_loaded["rig"], None, parts_collection, match_ctx)

    bpy.ops.object.mode_set(mode="OBJECT")
    
    # Apply pending constraints now that we're in object mode
    from .constraints import link_object_to_bone_rigid, auto_constraint_parts
    for obj, bone_name in match_ctx.get("pending_constraints", []):
        bone = ao.data.bones.get(bone_name)
        if bone:
            link_object_to_bone_rigid(obj, ao, bone)
    
    # Auto-constraint parts by matching bone names to mesh names
    # This handles parts that were renamed by fingerprint matching during import
    bpy.context.view_layer.update()
    ok, msg = auto_constraint_parts(ao.name)

    # If no parts matched, retry once on the next tick to allow depsgraph updates
    if ok and msg and "No matching parts found" in msg:
        def _retry_auto_constraint():
            try:
                auto_constraint_parts(ao.name)
            except Exception:
                pass
            return None

        try:
            bpy.app.timers.register(_retry_auto_constraint, first_interval=0.0)
        except Exception:
            pass
    
    # Configure weld bones with custom display and lock them from animation
    _configure_weld_bones(ao)
