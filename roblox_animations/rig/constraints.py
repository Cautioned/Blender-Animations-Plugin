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

    # Create a mapping of lowercase to actual bone names
    bone_name_map = {bone.name.lower(): bone.name for bone in armature.data.bones}
    matched_parts = []

    # Only process objects within this rig's parts collection
    for obj in parts_collection.objects:
        if obj.type == "MESH":
            
            if obj in skip_objects:
                continue
                
            # Strip .001, .002 etc from name for matching
            base_name = re.sub(r"\.\d+$", "", obj.name).lower()
            bone_name = bone_name_map.get(base_name)
            if not bone_name:
                continue

            # Ensure exactly one correct Child Of constraint exists
            # We want to preserve the existing correct one (to keep inverse_matrix if set)
            # and remove ANY other Child Of constraints (duplicates, wrong bone, wrong target)
            correct_constraint_found = False
            
            # Iterate over a copy to safely remove items
            for c in list(obj.constraints):
                if c.type == "CHILD_OF":
                    is_correct_target = (c.target == armature)
                    is_correct_bone = (c.subtarget == bone_name)
                    
                    if is_correct_target and is_correct_bone and not correct_constraint_found:
                        # Found the first valid matching constraint - keep it
                        correct_constraint_found = True
                    else:
                        # Remove if:
                        # - Wrong target (different armature)
                        # - Wrong bone (different subtarget on this armature)
                        # - Duplicate (we already found one correct constraint)
                        obj.constraints.remove(c)

            # Create constraint only if we didn't find one to preserve
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