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
    get_object_by_name,
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


def _safe_mode_set(mode, obj=None):
    ctx = bpy.context
    if obj:
        try:
            ctx.view_layer.objects.active = obj
            obj.select_set(True)
        except Exception:
            pass

    try:
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode=mode)
            return True
    except Exception:
        pass

    if hasattr(ctx, "temp_override") and obj:
        try:
            with ctx.temp_override(active_object=obj, object=obj, selected_objects=[obj], selected_editable_objects=[obj]):
                if bpy.ops.object.mode_set.poll():
                    bpy.ops.object.mode_set(mode=mode)
                    return True
        except Exception:
            pass

    try:
        bpy.ops.object.mode_set(mode=mode)
        return True
    except Exception:
        return False


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
    """Resolve an aux entry to a mesh.
    
    Priority order:
    1. Fingerprint object map (authoritative, from size fingerprinting)
    2. Name-based lookup (fallback)
    3. Position fingerprint (last resort)
    """
    used = match_ctx["used"]
    t2b = match_ctx["t2b"]
    
    
    # This is the definitive mapping established during import fingerprinting
    fp_map = match_ctx.get("fingerprint_object_map", {})
    if aux_name and aux_name in fp_map:
        obj = fp_map[aux_name]
        if obj not in used:
            print(f"[_find_matching_part] FINGERPRINT HIT: '{aux_name}' -> mesh '{obj.name}'")
            return obj
        else:
            print(f"[_find_matching_part] FINGERPRINT found but already used: '{aux_name}'")
    elif aux_name:
        print(f"[_find_matching_part] FINGERPRINT MISS: '{aux_name}' not in map (map has {len(fp_map)} entries)")
    
    # Fallback: Name-based candidates (base name match, ignoring suffixes)
    name_index = match_ctx["name_index"]
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
    # Handle AUX parts (parts welded to this bone but not the primary part)
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
            
    # Handle PRIMARY part (pname)
    # This was previously left to auto_constraint_parts, which guessed based on bone name.
    # Now we explicitly link 'pname' to this bone, ensuring correct constraints for duplicates.
    p_name = rigsubdef.get("pname")
    if p_name:
        # We don't have a specific transform for pname relative to bone here (it's implicit in bone head),
        # so pass None for cf. strict name matching takes priority anyway.
        found_primary = _find_matching_part(p_name, None, match_ctx)
        
        # Fallback: simple lookup in collection if _find_matching_part fails (it strips suffixes)
        if not found_primary and parts_collection:
             found_primary = parts_collection.objects.get(p_name)

        if found_primary:
            match_ctx["used"].add(found_primary)
            pending = match_ctx.setdefault("pending_constraints", [])
            pending.append((found_primary, bone.name))

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
    """Configure weld bones: custom shape, lock transforms, gray color."""
    amt = armature_obj.data
    
    settings = bpy.context.scene.rbx_anim_settings
    hide_welds = getattr(settings, "rbx_hide_weld_bones", False)
    weld_shape = _get_or_create_weld_bone_shape()
    
    _safe_mode_set("POSE", armature_obj)
    
    # Blender 4.0+ uses bone collections, 3.x uses bone.hide
    try:
        collections = amt.collections
        use_collections = True
    except Exception:
        collections = None
        use_collections = False

    weld_coll = None
    if use_collections:
        weld_coll_name = "_WeldBones"
        weld_coll = collections.get(weld_coll_name)
        if weld_coll is None:
            weld_coll = collections.new(weld_coll_name)
    
    for bone in amt.bones:
        joint_type = bone.get("rbx_joint_type", "Motor6D")
        if joint_type in ("Weld", "WeldConstraint"):
            pose_bone = armature_obj.pose.bones.get(bone.name)
            if pose_bone:
                pose_bone.custom_shape = weld_shape
                pose_bone.use_custom_shape_bone_size = True
                
                pose_bone.lock_location = (True, True, True)
                pose_bone.lock_rotation = (True, True, True)
                pose_bone.lock_rotation_w = True
                pose_bone.lock_scale = (True, True, True)
                
                if hasattr(pose_bone, "color"):
                    pose_bone.color.palette = 'CUSTOM'
                    pose_bone.color.custom.normal = (0.3, 0.3, 0.3)
                    pose_bone.color.custom.select = (0.5, 0.5, 0.5)
                    pose_bone.color.custom.active = (0.6, 0.6, 0.6)
            
            if use_collections:
                weld_coll.assign(bone)
            else:
                bone.hide = hide_welds
    
    if use_collections:
        weld_coll.is_visible = not hide_welds
    
    _safe_mode_set("OBJECT", armature_obj)


def create_rig(rigging_type, rig_meta_obj_name):
    """Create a complete rig from metadata"""
    # Ensure a clean slate by deselecting everything
    if bpy.ops.object.select_all.poll():
        bpy.ops.object.select_all(action="DESELECT")

    # Ensure we are in object mode
    if bpy.context.active_object and bpy.context.mode != "OBJECT":
        _safe_mode_set("OBJECT", bpy.context.active_object)

    rig_meta_obj = get_object_by_name(rig_meta_obj_name)
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
    
    # Load the authoritative fingerprint->object map
    # This was populated during import by _rename_parts_by_size_fingerprint
    fp_map = {}
    fp_map_json = rig_meta_obj.get("_FingerprintMap")
    if fp_map_json:
        try:
            fp_map_names = json.loads(fp_map_json)
            print(f"[RigCreate] Loading fingerprint map with {len(fp_map_names)} entries...")
            # Convert names back to object references
            for part_name, obj_name in fp_map_names.items():
                obj = parts_collection.objects.get(obj_name)
                if obj:
                    fp_map[part_name] = obj
                    print(f"[RigCreate]   '{part_name}' -> mesh '{obj.name}'")
                else:
                    print(f"[RigCreate]   WARNING: mesh '{obj_name}' not found for part '{part_name}'")
            print(f"[RigCreate] Loaded {len(fp_map)} authoritative fingerprint mappings")
        except Exception as e:
            print(f"[RigCreate] Failed to load fingerprint map: {e}")
    else:
        print("[RigCreate] WARNING: No _FingerprintMap found on meta object!")
    
    match_ctx["fingerprint_object_map"] = fp_map
    
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

    if bpy.context.mode != "EDIT":
        _safe_mode_set("EDIT", ao)
    # Pass the specific parts_collection to be used for constraining
    load_rigbone(ao, rigging_type, meta_loaded["rig"], None, parts_collection, match_ctx)

    if bpy.context.mode != "OBJECT":
        _safe_mode_set("OBJECT", ao)
    
    # Apply pending constraints now that we're in object mode
    from .constraints import link_object_to_bone_rigid, auto_constraint_parts
    
    # Track objects that were constrained via authoritative fingerprint mapping
    # These should NOT be touched by auto_constraint_parts
    authoritatively_constrained = set()
    
    pending = match_ctx.get("pending_constraints", [])
    print(f"[RigCreate] Applying {len(pending)} pending constraints...")
    
    for obj, bone_name in pending:
        bone = ao.data.bones.get(bone_name)
        if bone:
            link_object_to_bone_rigid(obj, ao, bone)
            authoritatively_constrained.add(obj)
            print(f"[RigCreate] AUTHORITATIVE: mesh '{obj.name}' -> bone '{bone_name}'")
        else:
            print(f"[RigCreate] WARNING: bone '{bone_name}' not found for mesh '{obj.name}'")
    
    # Auto-constraint ONLY parts that were NOT authoritatively constrained
    # This handles any parts that weren't in the fingerprint map (legacy/fallback)
    bpy.context.view_layer.update()
    ok, msg = auto_constraint_parts(ao.name, skip_objects=authoritatively_constrained)

    # If no parts matched via fallback, retry once (but STILL skip authoritative ones)
    if ok and msg and "No matching parts found" in msg:
        # Capture the set in closure
        _skip_set = authoritatively_constrained
        _ao_name = ao.name
        def _retry_auto_constraint():
            try:
                auto_constraint_parts(_ao_name, skip_objects=_skip_set)
            except Exception:
                pass
            return None

        try:
            bpy.app.timers.register(_retry_auto_constraint, first_interval=0.0)
        except Exception:
            pass
    
    # Configure weld bones with custom display and lock them from animation
    _configure_weld_bones(ao)
