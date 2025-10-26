"""
ik (inverse kinematics) setup and management utilities.
"""

import math
from typing import Optional, Tuple

import bpy
from mathutils import Vector, Matrix


def remove_ik_config(ao: 'bpy.types.Object', tail_bone: 'bpy.types.PoseBone') -> None:
    """remove all ik constraints and utility bones for the given chain tail.

    this function removes the ik constraint from the tail bone and deletes any
    temporary target/pole bones that were previously created.
    """
    to_clear = []
    for constraint in [c for c in tail_bone.constraints if c.type == "IK"]:
        if constraint.target and constraint.subtarget:
            to_clear.append((constraint.target, constraint.subtarget))
        if constraint.pole_target and constraint.pole_subtarget:
            to_clear.append(
                (constraint.pole_target, constraint.pole_subtarget))

        tail_bone.constraints.remove(constraint)

    # ensure we're operating on the right object
    bpy.context.view_layer.objects.active = ao
    bpy.ops.object.mode_set(mode="EDIT")

    for util_bone in to_clear:
        util_bone[0].data.edit_bones.remove(
            util_bone[0].data.edit_bones[util_bone[1]])

    bpy.ops.object.mode_set(mode="POSE")


def create_ik_config(
    ao: 'bpy.types.Object',
    tail_bone: 'bpy.types.PoseBone',
    chain_count: int,
    create_pose_bone: bool,
    lock_tail: bool,
) -> Tuple[str, Optional[str]]:
    """create ik target (and optional pole) and apply an ik constraint.

    returns (ik_target_bone_name, ik_pole_bone_name_or_none).
    """
    # sanitize inputs
    chain_count = max(1, int(chain_count))

    # ensure correct active object
    bpy.context.view_layer.objects.active = ao
    bpy.ops.object.mode_set(mode='EDIT')

    amt = ao.data
    ik_target_src = tail_bone if not lock_tail else (tail_bone.parent or tail_bone)

    ik_target_bone_name = ik_target_src.name
    ik_name = f"{ik_target_bone_name}-IKTarget"
    ik_name_pole = f"{ik_target_bone_name}-IKPole"

    # create target bone roughly offset in local z-
    ik_bone = amt.edit_bones.new(ik_name)
    ik_bone.head = ik_target_src.tail
    ik_bone.tail = (
        Matrix.Translation(ik_bone.head)
        @ ik_target_src.matrix.to_3x3().to_4x4()
    ) @ Vector((0, 0, -0.5))
    ik_bone.bbone_x *= 1.5
    ik_bone.bbone_z *= 1.5

    ik_pole_name: Optional[str] = None
    if create_pose_bone:
        # clamp the effective chain depth to available parents
        effective_depth = min(chain_count, len(tail_bone.parent_recursive))
        pos_low = tail_bone.tail
        pos_high = tail_bone.parent_recursive[effective_depth - 1].head if effective_depth > 0 else tail_bone.head
        dist = (pos_low - pos_high).length

        # find basal bone for orientation
        basal = tail_bone
        for _ in range(1, effective_depth + 1):
            if basal.parent:
                basal = basal.parent

        basal_mat = basal.bone.matrix_local

        ik_pole = amt.edit_bones.new(ik_name_pole)
        ik_pole.head = basal_mat @ Vector((0, 0, -0.25 * dist))
        ik_pole.tail = basal_mat @ Vector((0, 0, -0.25 * dist - 0.3))
        ik_pole.bbone_x *= 0.5
        ik_pole.bbone_z *= 0.5
        ik_pole_name = ik_pole.name

    bpy.ops.object.mode_set(mode='POSE')

    pose_bone = ao.pose.bones.get(ik_target_bone_name)
    if not pose_bone:
        # fallback: no pose bone found
        return (ik_name, ik_pole_name)

    constraint = pose_bone.constraints.new(type='IK')
    constraint.target = ao
    constraint.subtarget = ik_name
    if create_pose_bone and ik_pole_name:
        constraint.pole_target = ao
        constraint.pole_subtarget = ik_pole_name
        constraint.pole_angle = math.pi * -0.5
    constraint.chain_count = chain_count

    return (ik_name, ik_pole_name)
