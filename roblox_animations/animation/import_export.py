"""
Animation import/export utilities for mapping between different rigs.
"""

import bpy
from mathutils import Matrix
from ..core.utils import (
    get_action_fcurves,
    pose_bone_set_selected,
)


def copy_anim_state_bone(target, source, bone):
    """Copy animation state for a single bone from source to target rig"""
    # get transform mat of the bone in the source ao
    bpy.context.view_layer.objects.active = source
    t_mat = source.pose.bones[bone.name].matrix

    bpy.context.view_layer.objects.active = target

    # root bone transform is ignored, this is carried to child bones (keeps HRP static)
    if bone.parent:
        # apply transform w.r.t. the current parent bone transform
        r_mat = bone.bone.matrix_local
        p_mat = bone.parent.matrix
        p_r_mat = bone.parent.bone.matrix_local
        bone.matrix_basis = (p_r_mat.inverted() @ r_mat).inverted() @ (
            p_mat.inverted() @ t_mat
        )

    # update properties (hacky :p)
    bone.keyframe_insert(data_path="location")
    bone.keyframe_insert(data_path="rotation_quaternion")
    bone.keyframe_insert(data_path="scale")
    bpy.context.scene.frame_set(bpy.context.scene.frame_current)

    # now apply on children (which use the parents transform)
    for ch in bone.children:
        copy_anim_state_bone(target, source, ch)


def copy_anim_state(target, source):
    """Copy animation state from source rig to target rig"""
    # to pose mode
    bpy.context.view_layer.objects.active = source
    bpy.ops.object.mode_set(mode="POSE")

    bpy.context.view_layer.objects.active = target
    bpy.ops.object.mode_set(mode="POSE")

    root = target.pose.bones["HumanoidRootPart"]

    for i in range(bpy.context.scene.frame_start, bpy.context.scene.frame_end + 1):
        bpy.context.scene.frame_set(i)
        copy_anim_state_bone(target, source, root)
        # Keyframes are already inserted in copy_anim_state_bone


def prepare_for_kf_map():
    """Prepare target rig for keyframe mapping by clearing animation data"""
    # clear anim data from target rig
    settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
    armature_name = settings.rbx_anim_armature if settings else None
    bpy.data.objects[armature_name].animation_data_clear()

    # select all pose bones in the target rig (simply generate kfs for everything)
    bpy.context.view_layer.objects.active = bpy.data.objects[armature_name]
    bpy.ops.object.mode_set(mode="POSE")
    for bone in bpy.data.objects[armature_name].pose.bones:
        pose_bone_set_selected(bone, bool(bone.parent))


def get_mapping_error_bones(target, source):
    """Get list of bones that exist in target but not in source rig"""
    return [
        bone.name
        for bone in target.data.bones
        if bone.name not in [bone2.name for bone2 in source.data.bones]
    ]


def apply_ao_transform(ao):
    """Apply armature object transforms to the root bone for each keyframe"""
    bpy.context.view_layer.objects.active = ao
    bpy.ops.object.mode_set(mode="POSE")

    # select only root bones
    for bone in ao.pose.bones:
        pose_bone_set_selected(bone, not bone.parent)

    for root in [bone for bone in ao.pose.bones if not bone.parent]:
        # collect initial root matrices (if they do not exist yet, this will prevent interpolation from keyframes that are being set in the next loop)
        root_matrix_at = {}
        for i in range(bpy.context.scene.frame_start, bpy.context.scene.frame_end + 1):
            bpy.context.scene.frame_set(i)
            root_matrix_at[i] = root.matrix.copy()

        # apply world space transform to root bone
        for i in range(bpy.context.scene.frame_start, bpy.context.scene.frame_end + 1):
            bpy.context.scene.frame_set(i)
            root.matrix = ao.matrix_world @ root_matrix_at[i]
            root.keyframe_insert(data_path="location")
            root.keyframe_insert(data_path="rotation_quaternion")
            root.keyframe_insert(data_path="scale")

    # clear non-pose fcurves
    fcurves = get_action_fcurves(ao.animation_data.action)
    for c in [c for c in fcurves if not c.data_path.startswith("pose")]:
        fcurves.remove(c)

    # reset ao transform
    ao.matrix_basis = Matrix.Identity(4)
    bpy.context.evaluated_depsgraph_get().update()
