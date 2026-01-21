"""
Animation import/export utilities for mapping between different rigs.
"""

import bpy
from mathutils import Matrix
from ..core.utils import (
    get_action_fcurves,
    pose_bone_set_selected,
)


def _set_all_ik_fk(ao: bpy.types.Object, value: float) -> None:
    """Set IK_FK custom property on all IK target bones (if present)."""
    if not ao or ao.type != 'ARMATURE':
        return
    bpy.context.view_layer.objects.active = ao
    bpy.ops.object.mode_set(mode="POSE")
    for pb in ao.pose.bones:
        if "IK_FK" in pb:
            pb["IK_FK"] = float(value)
    bpy.context.evaluated_depsgraph_get().update()


def _collect_ik_controls(ao: bpy.types.Object):
    """Collect IK targets and poles from IK constraints (independent of naming)."""
    targets = set()
    poles = set()
    chains = set()
    for pb in ao.pose.bones:
        for c in pb.constraints:
            if c.type == "IK":
                if c.subtarget:
                    targets.add(c.subtarget)
                if getattr(c, "pole_subtarget", None):
                    poles.add(c.pole_subtarget)
                # Collect the chain bones (end-effector + parents up to chain_count)
                chain_bones = [pb]
                current = pb.parent
                while current and len(chain_bones) < c.chain_count:
                    chain_bones.append(current)
                    current = current.parent
                for b in chain_bones:
                    chains.add(b.name)
    return targets, poles, chains


def _key_ik_targets_from_fk_motion(ao: bpy.types.Object, frame_start: int, frame_end: int) -> None:
    """Key IK targets (and poles) to match the FK-driven motion frame-by-frame.

    Works with arbitrary IK target names by reading constraint subtargets/poles.
    Assumes IK is disabled (IK_FK=0) so pose is purely FK when sampling.
    """
    if not ao or ao.type != 'ARMATURE':
        return
    bpy.context.view_layer.objects.active = ao
    bpy.ops.object.mode_set(mode="POSE")
    targets, poles, _ = _collect_ik_controls(ao)
    for frame in range(frame_start, frame_end + 1):
        bpy.context.scene.frame_set(frame)
        # Align each IK target to its constrained bone's current FK pose
        for pb in ao.pose.bones:
            for c in pb.constraints:
                if c.type != "IK" or not c.subtarget:
                    continue
                tgt = ao.pose.bones.get(c.subtarget)
                if tgt:
                    tgt.matrix = pb.matrix.copy()
                    tgt.keyframe_insert(data_path="location")
                    tgt.keyframe_insert(data_path="rotation_quaternion")
                    tgt.keyframe_insert(data_path="scale")
                pole_name = getattr(c, "pole_subtarget", None)
                if pole_name:
                    pole = ao.pose.bones.get(pole_name)
                    if pole:
                        # Keep pole where it currently is (no solve), but ensure it gets keyed
                        pole.keyframe_insert(data_path="location")
                        pole.keyframe_insert(data_path="rotation_quaternion")
                        pole.keyframe_insert(data_path="scale")
    bpy.context.evaluated_depsgraph_get().update()


def _clear_fk_fcurves_for_ik_chains(ao: bpy.types.Object) -> None:
    """Remove FK fcurves on bones driven by IK chains to avoid double transforms."""
    if not ao or not ao.animation_data or not ao.animation_data.action:
        return
    action = ao.animation_data.action
    targets, poles, chains = _collect_ik_controls(ao)
    # Don't remove curves on IK targets/poles themselves
    protected = targets | poles
    to_clear = chains - protected
    if not to_clear:
        return
    fcurves = list(getattr(action, "fcurves", []) or [])
    for fc in fcurves:
        dp = getattr(fc, "data_path", "")
        for name in to_clear:
            if dp.startswith(f'pose.bones["{bpy.utils.escape_identifier(name)}"]'):
                action.fcurves.remove(fc)
                break


def import_animation_preserve_ik(import_fn, *args, **kwargs):
    """Run an import function while preserving IK rig usability.

    Workflow: temporarily set all IK_FK props to FK (0), run the provided
    import_fn, then bake the resulting FK motion back onto IK controls and
    restore IK_FK to 1.

    Args:
        import_fn: callable that performs the animation import.
        *args/**kwargs: forwarded to import_fn.
    Returns:
        The return value from import_fn.
    """
    ao = bpy.context.object
    if not ao or ao.type != 'ARMATURE':
        return import_fn(*args, **kwargs)

    scene = bpy.context.scene
    frame_start = scene.frame_start
    frame_end = scene.frame_end

    _set_all_ik_fk(ao, 0.0)
    result = import_fn(*args, **kwargs)
    # While in FK, sample FK pose and key the IK targets frame by frame
    _key_ik_targets_from_fk_motion(ao, frame_start, frame_end)
    # Remove FK fcurves on IK chains to prevent double-transform when IK is re-enabled
    _clear_fk_fcurves_for_ik_chains(ao)
    # Restore IK
    _set_all_ik_fk(ao, 1.0)
    return result


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
