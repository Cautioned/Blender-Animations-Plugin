"""
Constraint management utilities for linking objects to bones.
"""

import re
from ..core.utils import (
    find_master_collection_for_object,
    find_parts_collection_in_master,
    get_object_by_name,
)


def link_object_to_bone_rigid(obj, ao, bone):
    """Link an object to a bone with rigid transformation"""
    # remove existing
    for constraint in [c for c in obj.constraints if c.type == "CHILD_OF"]:
        obj.constraints.remove(constraint)

    # create new
    constraint = obj.constraints.new(type="CHILD_OF")
    constraint.target = ao
    constraint.subtarget = bone.name
    bone_mat = getattr(bone, "matrix_local", None)
    if bone_mat is None:
        bone_mat = bone.matrix
    if hasattr(bone_mat, "to_4x4"):
        bone_mat = bone_mat.to_4x4()
    constraint.inverse_matrix = (ao.matrix_world @ bone_mat).inverted()


def auto_constraint_parts(armature_name, skip_objects=None):
    """Automatically constrain parts/meshes with matching bone names.
    
    Uses position-based disambiguation when multiple bones share the same
    base name (e.g. "left hand", "left hand.001"). This prevents meshes
    from being constrained to the wrong bone on the opposite side of the rig.
    
    Args:
        armature_name: Name of the armature to constrain parts to
        skip_objects: Set of objects to skip (already constrained authoritatively)
    """
    if skip_objects is None:
        skip_objects = set()
        
    armature = get_object_by_name(armature_name)
    if not armature:
        return False, f"Armature '{armature_name}' not found."

    # Find the master collection and parts collection for this rig
    master_collection = find_master_collection_for_object(armature)
    if not master_collection:
        return (
            False,
            f"Could not find a master collection for armature '{armature_name}'.",
        )

    parts_collection = find_parts_collection_in_master(master_collection)
    if not parts_collection:
        return (
            False,
            f"Could not find a 'Parts' collection inside '{master_collection.name}'.",
        )

    # Build a mapping of base name -> list of bone names
    # This handles duplicates correctly (e.g. "left hand", "left hand.001")
    from collections import defaultdict
    bone_groups = defaultdict(list)  # base_name_lower -> [bone_name, ...]
    for bone in armature.data.bones:
        base = re.sub(r"\.\d+$", "", bone.name).lower()
        bone_groups[base].append(bone.name)
    
    # Precompute bone head positions in world space for disambiguation
    bone_positions = {}
    for bone in armature.data.bones:
        bone_positions[bone.name] = armature.matrix_world @ bone.head_local
    
    matched_parts = []
    used_bones = set()  # track which specific bones have been claimed

    # Only process objects within this rig's parts collection
    for obj in parts_collection.objects:
        if obj.type == "MESH":
            
            if obj in skip_objects:
                continue
                
            # Strip .001, .002 etc from name for matching
            base_name = re.sub(r"\.\d+$", "", obj.name).lower()
            bone_candidates = bone_groups.get(base_name)
            if not bone_candidates:
                continue
            
            # Filter out already-claimed bones
            available = [b for b in bone_candidates if b not in used_bones]
            if not available:
                continue
            
            # Pick the best bone: closest to the mesh's world center
            if len(available) == 1:
                bone_name = available[0]
            else:
                # Compute mesh center
                mesh_center = obj.matrix_world.to_translation()  # rough center
                if obj.data.vertices:
                    from mathutils import Vector
                    verts = obj.data.vertices
                    min_co = [float('inf')] * 3
                    max_co = [float('-inf')] * 3
                    for v in verts:
                        for i in range(3):
                            min_co[i] = min(min_co[i], v.co[i])
                            max_co[i] = max(max_co[i], v.co[i])
                    local_center = Vector([(min_co[i] + max_co[i]) / 2.0 for i in range(3)])
                    mesh_center = obj.matrix_world @ local_center
                
                # Sort by distance to mesh center
                def _bone_dist(bn):
                    return (bone_positions[bn] - mesh_center).length
                available.sort(key=_bone_dist)
                bone_name = available[0]

            used_bones.add(bone_name)

            # Ensure exactly one correct Child Of constraint exists
            correct_constraint_found = False
            
            for c in list(obj.constraints):
                if c.type == "CHILD_OF":
                    is_correct_target = (c.target == armature)
                    is_correct_bone = (c.subtarget == bone_name)
                    
                    if is_correct_target and is_correct_bone and not correct_constraint_found:
                        correct_constraint_found = True
                    else:
                        obj.constraints.remove(c)

            if not correct_constraint_found:
                constraint = obj.constraints.new(type="CHILD_OF")
                constraint.target = armature
                constraint.subtarget = bone_name
            
            matched_parts.append(obj.name)

    if not matched_parts:
        return (
            True,
            f"No matching parts found for armature {armature_name} in its collection.",
        )
    else:
        return True, f"Constraints added to parts: {', '.join(matched_parts)}"


def manual_constraint_parts(armature_name, bone_mesh_assignments):
    """Manually constrain parts based on provided assignments"""
    armature = get_object_by_name(armature_name)
    if not armature:
        return False, f"Armature '{armature_name}' not found."

    parts_collection = find_parts_collection_in_master(
        find_master_collection_for_object(armature)
    )
    if not parts_collection:
        return False, "Could not find 'Parts' collection to execute on."

    # Update constraints for all objects within this rig's parts collection
    for obj in parts_collection.objects:
        if obj.type != "MESH":
            continue

        # First, remove any existing CHILD_OF constraint that targets this armature
        # iterating over a copy of the list is crucial to safe removal
        for c in list(obj.constraints):
            if c.type == "CHILD_OF" and c.target == armature:
                obj.constraints.remove(c)

        # Now, if this object is in our new assignment list, add the new constraint
        if obj in bone_mesh_assignments:
            bone_name = bone_mesh_assignments[obj]
            constraint = obj.constraints.new(type="CHILD_OF")
            constraint.target = armature
            constraint.subtarget = bone_name

    return True, "Constraints updated."