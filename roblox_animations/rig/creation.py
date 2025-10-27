"""
Rig creation and bone management utilities.
"""

import json
import re
import bpy
from mathutils import Vector, Matrix
from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat, get_unique_name, find_master_collection_for_object, find_parts_collection_in_master


def _matrix_to_idprop(value):
    """Convert Matrix values to list-of-lists so IDProperties accept them."""
    if isinstance(value, Matrix):
        return [list(row) for row in value]
    return value


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
                        f"Warning: Index {index} out of range for partnames list (length: {len(partnames)})")
            except Exception as e:
                print(f"Error renaming part {object.name}: {str(e)}")


def load_rigbone(ao, rigging_type, rigsubdef, parent_bone, parts_collection):
    """Load a single rig bone with its children"""
    amt = ao.data
    bone = amt.edit_bones.new(rigsubdef["jname"])

    mat = cf_to_mat(rigsubdef["transform"])
    bone["transform"] = _matrix_to_idprop(mat)
    t2b = get_transform_to_blender()
    bone_dir = (t2b @ mat).to_3x3().to_4x4() @ Vector((0, 0, 1))

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
        bone["is_transformable"] = True

        bone.parent = parent_bone
        o_trans = t2b @ (mat @ mat1)
        bone.head = o_trans.to_translation()
        real_tail = o_trans @ Vector((0, 0.25, 0))

        neutral_pos = (t2b @ mat).to_translation()
        bone.tail = real_tail
        bone.align_roll(bone_dir)

        # store neutral matrix
        pre_mat = bone.matrix

        if rigging_type != "RAW":  # If so, apply some transform
            if len(rigsubdef["children"]) == 1:
                nextmat = cf_to_mat(rigsubdef["children"][0]["transform"])
                nextmat1 = cf_to_mat(
                    rigsubdef["children"][0]["jointtransform1"])
                next_joint_pos = (t2b @ (nextmat @ nextmat1)).to_translation()

                if rigging_type == "CONNECT":  # Instantly connect
                    bone.tail = next_joint_pos
                else:
                    axis = "y"
                    if rigging_type == "LOCAL_AXIS_EXTEND":  # Allow non-Y too
                        invtrf = pre_mat.inverted() * next_joint_pos
                        bestdist = abs(invtrf.y)
                        for paxis in ["x", "z"]:
                            dist = abs(getattr(invtrf, paxis))
                            if dist > bestdist:
                                bestdist = dist
                                axis = paxis

                    next_connect_to_parent = True

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
    bone["nicetransform"] = _matrix_to_idprop(o_trans.inverted() @ post_mat)

    # link objects to bone by searching ONLY within the provided parts_collection
    for aux_name in rigsubdef["aux"]:
        # Skip if the aux name is null/None, which can happen for bones with no associated parts.
        if not aux_name:
            continue

        # Find the object by its original name within the collection's objects
        found_obj = None
        for obj in parts_collection.objects:
            # Match the base name, ignoring any .001 suffixes
            if obj.name.startswith(aux_name):
                found_obj = obj
                break
        
        if found_obj:
            from .constraints import link_object_to_bone_rigid
            link_object_to_bone_rigid(found_obj, ao, bone)

    # handle child bones
    for child in rigsubdef["children"]:
        load_rigbone(ao, rigging_type, child, bone, parts_collection)


def create_rig(rigging_type, rig_meta_obj_name):
    """Create a complete rig from metadata"""
    # Ensure a clean slate by deselecting everything
    if bpy.ops.object.select_all.poll():
        bpy.ops.object.select_all(action='DESELECT')

    # Ensure we are in object mode
    if bpy.context.active_object and bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode="OBJECT")

    rig_meta_obj = bpy.data.objects.get(rig_meta_obj_name)
    if not rig_meta_obj:
        raise ValueError(f"Rig meta object '{rig_meta_obj_name}' not found.")
        return

    # Find the master collection and parts collection for the meta object
    master_collection = find_master_collection_for_object(rig_meta_obj)
    if not master_collection:
        raise ValueError(f"Could not find a master collection for rig meta object '{rig_meta_obj_name}'.")
        return

    parts_collection = find_parts_collection_in_master(master_collection)
    if not parts_collection:
        raise ValueError(f"Could not find a 'Parts' collection inside '{master_collection.name}'.")
        return

    # --- Deletion of old Armature ---
    # Find and delete any existing armature within this rig's master collection
    old_armature = None
    for obj in master_collection.objects:
        if obj.type == 'ARMATURE':
            old_armature = obj
            break

    if old_armature:
        bpy.data.objects.remove(old_armature, do_unlink=True)

    # Set the meta object as active to provide context for subsequent operators
    bpy.context.view_layer.objects.active = rig_meta_obj

    meta_loaded = json.loads(rig_meta_obj["RigMeta"])

    bpy.ops.object.add(type="ARMATURE", enter_editmode=True,
                       location=(0, 0, 0))
    ao = bpy.context.object
    ao.show_in_front = True

    # Move the new armature into the master collection
    for coll in ao.users_collection:
        coll.objects.unlink(ao)
    master_collection.objects.link(ao)

    # Set a unique name for the armature based on the rig name
    rig_name = meta_loaded.get('rigName', 'Rig')
    ao.name = get_unique_name(f"{rig_name}_Armature")
    amt = ao.data
    amt.name = get_unique_name(f"__{rig_name}_RigArm")
    amt.show_axes = True
    amt.show_names = True

    bpy.ops.object.mode_set(mode="EDIT")
    # Pass the specific parts_collection to be used for constraining
    load_rigbone(ao, rigging_type, meta_loaded["rig"], None, parts_collection)

    bpy.ops.object.mode_set(mode="OBJECT")
