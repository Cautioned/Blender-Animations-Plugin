"""
Constraint management utilities for linking objects to bones.
"""

import re
import bpy
from ..core.utils import find_master_collection_for_object, find_parts_collection_in_master


def link_object_to_bone_rigid(obj, ao, bone):
    """Link an object to a bone with rigid transformation"""
    # remove existing
    for constraint in [c for c in obj.constraints if c.type == "CHILD_OF"]:
        obj.constraints.remove(constraint)

    # create new
    constraint = obj.constraints.new(type="CHILD_OF")
    constraint.target = ao
    constraint.subtarget = bone.name
    constraint.inverse_matrix = (ao.matrix_world @ bone.matrix).inverted()


def auto_constraint_parts(armature_name):
    """Automatically constrain parts/meshes with matching bone names"""
    armature = bpy.data.objects.get(armature_name)
    if not armature:
        return False, f"Armature '{armature_name}' not found."

    # Find the master collection and parts collection for this rig
    master_collection = find_master_collection_for_object(armature)
    if not master_collection:
        return False, f"Could not find a master collection for armature '{armature_name}'."

    parts_collection = find_parts_collection_in_master(master_collection)
    if not parts_collection:
        return False, f"Could not find a 'Parts' collection inside '{master_collection.name}'."
    
    # Create a mapping of lowercase to actual bone names
    bone_name_map = {
        bone.name.lower(): bone.name for bone in armature.data.bones}
    matched_parts = []

    # Only process objects within this rig's parts collection
    for obj in parts_collection.objects:
        if obj.type == 'MESH':
            # Strip .001, .002 etc from name for matching
            base_name = re.sub(r"\.\d+$", "", obj.name).lower()
            if base_name in bone_name_map:
                bone_name = bone_name_map[base_name]

            # Check for existing constraints and clear if they belong to another armature
            for constraint in obj.constraints:
                if constraint.type == 'CHILD_OF' and constraint.target != armature:
                    obj.constraints.remove(constraint)

            # Add constraint if not already constrained to the correct bone
            existing_constraint = next((c for c in obj.constraints if c.type ==
                                       'CHILD_OF' and c.target == armature and c.subtarget == bone_name), None)
            if not existing_constraint:
                constraint = obj.constraints.new(type='CHILD_OF')
                constraint.target = armature
                constraint.subtarget = bone_name
            matched_parts.append(obj.name)

    if not matched_parts:
        return True, f'No matching parts found for armature {armature_name} in its collection.'
    else:
        return True, f'Constraints added to parts: {", ".join(matched_parts)}'


def manual_constraint_parts(armature_name, bone_mesh_assignments):
    """Manually constrain parts based on provided assignments"""
    armature = bpy.data.objects.get(armature_name)
    if not armature:
        return False, f"Armature '{armature_name}' not found."

    parts_collection = find_parts_collection_in_master(
        find_master_collection_for_object(armature))
    if not parts_collection:
        return False, f"Could not find 'Parts' collection to execute on."

    # Update constraints for all objects within this rig's parts collection
    for obj in parts_collection.objects:
        if obj.type != 'MESH':
            continue

        # First, remove any existing CHILD_OF constraint that targets this armature
        for c in obj.constraints:
            if c.type == 'CHILD_OF' and c.target == armature:
                obj.constraints.remove(c)
        
        # Now, if this object is in our new assignment list, add the new constraint
        if obj in bone_mesh_assignments:
            bone_name = bone_mesh_assignments[obj]
            constraint = obj.constraints.new(type='CHILD_OF')
            constraint.target = armature
            constraint.subtarget = bone_name

    return True, "Constraints updated."
