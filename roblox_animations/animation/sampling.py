# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
"""
Per-frame bone sampling for animation export.

Samplers are pure functions of the evaluated armature plus an optional
static cache shared across frames. They return fresh state dicts; nothing
here mutates caller-owned structures.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

import bpy
from mathutils import Matrix, Vector

from ..core.constants import (
    cf_round,
    cf_round_fac,
    get_transform_to_blender,
    identity_cf,
)
from ..core.utils import mat_to_cf, to_matrix
from .face_controls import is_face_control_bone
from .scale import (
    _SCALE_EPSILON,
    _orthonormalized_transform,
    resolve_deform_rig_scale_factor,
    resolve_deform_translation_scale_factor,
)


def serialize_animation_state(
    ao: "bpy.types.Object",
    back_trans_cached: Optional[Matrix] = None,
    static_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, List[float]]:
    """Serialize Motor6D animation state into a fresh state dict."""
    state: Dict[str, List[float]] = {}

    # Use cached transform or compute once.
    back_trans = (
        back_trans_cached
        if back_trans_cached is not None
        else get_transform_to_blender().inverted()
    )

    # Local bindings for speed.
    pose_bones = ao.pose.bones
    cache: Dict[str, Dict[str, Any]] = static_cache or {}

    def ensure_cache(name: str) -> Dict[str, Any]:
        entry = cache.get(name)
        if entry is None:
            entry = {}
            cache[name] = entry
        return entry

    def get_cached_matrix(bone_name: str, key: str, fallback_fn):
        bcache = ensure_cache(bone_name)
        mat = bcache.get(key)
        if mat is None:
            mat = fallback_fn()
            bcache[key] = mat
        return mat

    # Build a lookup for world-space bones and their original parents.
    worldspace_bones: Dict[str, str] = {}  # bone_name -> original_parent_name
    for bone in pose_bones:
        if is_face_control_bone(bone):
            continue
        if bone.bone.get("worldspace_bone"):
            original_parent = bone.bone.get("worldspace_original_parent", "")
            if original_parent:
                worldspace_bones[bone.name] = original_parent

    for bone in pose_bones:
        if is_face_control_bone(bone):
            continue
        has_motor6d_props = (
            "transform" in bone.bone
            and "transform1" in bone.bone
            and "nicetransform" in bone.bone
        )

        if has_motor6d_props:
            # --- Traditional Motor6D bone logic ---
            try:
                bcache = ensure_cache(bone.name)
                extr_inv = bcache.get("extr_inv")
                orig_base_mat = bcache.get("orig_base_mat")

                if extr_inv is None:
                    nicetransform = get_cached_matrix(
                        bone.name,
                        "nicetransform",
                        lambda: to_matrix(bone.bone.get("nicetransform")),
                    )
                    extr_inv = nicetransform.inverted()
                    bcache["extr_inv"] = extr_inv
                if orig_base_mat is None:
                    orig_mat = to_matrix(bone.bone.get("transform"))
                    orig_mat_tr1 = to_matrix(bone.bone.get("transform1"))
                    orig_base_mat = back_trans @ (orig_mat @ orig_mat_tr1)
                    bcache["orig_base_mat"] = orig_base_mat

                cur_obj_transform = back_trans @ (bone.matrix @ extr_inv)

                # Check if this is a world-space bone that needs parent compensation
                if bone.name in worldspace_bones:
                    original_parent_name = worldspace_bones[bone.name]
                    original_parent_bone = pose_bones.get(original_parent_name)

                    if original_parent_bone:
                        # Get the original parent's transforms
                        parent_has_motor6d = (
                            "transform" in original_parent_bone.bone
                            and "transform1" in original_parent_bone.bone
                            and "nicetransform" in original_parent_bone.bone
                        )

                        if parent_has_motor6d:
                            pcb = ensure_cache(original_parent_name)
                            parent_extr_inv = pcb.get("extr_inv")
                            parent_orig_base_mat = pcb.get("orig_base_mat")

                            if parent_extr_inv is None:
                                parent_nicetransform = get_cached_matrix(
                                    original_parent_name,
                                    "nicetransform",
                                    lambda: to_matrix(original_parent_bone.bone.get("nicetransform")),
                                )
                                parent_extr_inv = parent_nicetransform.inverted()
                                pcb["extr_inv"] = parent_extr_inv
                            if parent_orig_base_mat is None:
                                p_orig_mat = to_matrix(original_parent_bone.bone.get("transform"))
                                p_orig_mat_tr1 = to_matrix(original_parent_bone.bone.get("transform1"))
                                parent_orig_base_mat = back_trans @ (p_orig_mat @ p_orig_mat_tr1)
                                pcb["orig_base_mat"] = parent_orig_base_mat

                            # Current parent world transform
                            parent_cur_transform = back_trans @ (original_parent_bone.matrix @ parent_extr_inv)

                            # The bone's world-space target (where it should stay)
                            world_target = cur_obj_transform

                            # Calculate what the local transform should be relative to current parent
                            local_relative_to_parent = parent_cur_transform.inverted() @ world_target

                            # Original local transform (rest pose relative to parent at rest)
                            orig_local = parent_orig_base_mat.inverted() @ orig_base_mat

                            # The delta we need to apply
                            bone_transform = orig_local.inverted() @ local_relative_to_parent
                        else:
                            # Parent is not motor6d, just use world-space delta
                            bone_transform = orig_base_mat.inverted() @ cur_obj_transform
                    else:
                        # Original parent not found, use world-space delta
                        bone_transform = orig_base_mat.inverted() @ cur_obj_transform

                elif bone.parent:
                    parent_has_motor6d_props = (
                        "transform" in bone.parent.bone
                        and "transform1" in bone.parent.bone
                        and "nicetransform" in bone.parent.bone
                    )
                    if parent_has_motor6d_props:
                        pcb = ensure_cache(bone.parent.name)
                        parent_extr_inv = pcb.get("extr_inv")
                        parent_orig_base_mat = pcb.get("orig_base_mat")
                        if parent_extr_inv is None:
                            parent_nicetransform = get_cached_matrix(
                                bone.parent.name,
                                "nicetransform",
                                lambda: to_matrix(bone.parent.bone.get("nicetransform")),
                            )
                            parent_extr_inv = parent_nicetransform.inverted()
                            pcb["extr_inv"] = parent_extr_inv
                        if parent_orig_base_mat is None:
                            p_orig_mat = to_matrix(bone.parent.bone.get("transform"))
                            p_orig_mat_tr1 = to_matrix(bone.parent.bone.get("transform1"))
                            parent_orig_base_mat = back_trans @ (
                                p_orig_mat @ p_orig_mat_tr1
                            )
                            pcb["orig_base_mat"] = parent_orig_base_mat

                        parent_obj_transform = back_trans @ (
                            bone.parent.matrix @ parent_extr_inv
                        )
                        orig_transform = parent_orig_base_mat.inverted() @ orig_base_mat
                        cur_transform = parent_obj_transform.inverted() @ cur_obj_transform
                        bone_transform = orig_transform.inverted() @ cur_transform
                    else:
                        # Parent is a new bone, which is now handled by the deform serializer.
                        # This bone is treated as a root in the context of Motor6D calculations.
                        bone_transform = orig_base_mat.inverted() @ cur_obj_transform
                else:
                    bone_transform = orig_base_mat.inverted() @ cur_obj_transform

                statel = mat_to_cf(bone_transform)
                if cf_round:
                    statel = [round(x, cf_round_fac) for x in statel]

                if statel != identity_cf:
                    state[bone.name] = statel
            except Exception as e:
                print(f"[Export] Skipping bone '{bone.name}' due to serialization error: {e}")

    return state


def serialize_deform_animation_state(
    ao: "bpy.types.Object",
    is_skinned_rig: bool,
    world_transform_cached: Optional[Matrix] = None,
    scale_factor_cached: Optional[float] = None,
    static_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    excluded_bones: Optional[Set[str]] = None,
) -> Dict[str, List[float]]:
    """Serialize Deform Bone animation state into a fresh state dict."""
    # Use cached transforms or compute once.
    if world_transform_cached is None:
        back_trans = get_transform_to_blender().inverted()
        world_transform = back_trans @ ao.matrix_world
    else:
        world_transform = world_transform_cached

    if scale_factor_cached is None:
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        scale_factor = resolve_deform_rig_scale_factor(ao, settings)
    else:
        scale_factor = scale_factor_cached
    settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
    translation_scale_factor = resolve_deform_translation_scale_factor(
        ao,
        settings,
        scale_factor=scale_factor,
    )

    state: Dict[str, List[float]] = {}
    bone_cache: Dict[str, Tuple[Matrix, Matrix]] = {}

    deform_chain_bones: Set[str] = set()
    if is_skinned_rig:
        for pose_bone in ao.pose.bones:
            if is_face_control_bone(pose_bone) or not pose_bone.bone.use_deform:
                continue
            current_bone = pose_bone
            while current_bone:
                if not is_face_control_bone(current_bone):
                    deform_chain_bones.add(current_bone.name)
                current_bone = current_bone.parent

    # Pre-populate cache for deform/helper bones to simplify parent lookups.
    bones_to_process = []
    for bone in ao.pose.bones:
        if is_face_control_bone(bone):
            continue
        if excluded_bones and bone.name in excluded_bones:
            continue
        # Exclude Motor6D bones from deform serialization.
        if (
            "transform" in bone.bone
            and "transform1" in bone.bone
            and "nicetransform" in bone.bone
        ):
            continue
        bones_to_process.append(bone)

    # Fast bail-out: nothing to serialize on deform path.
    if not bones_to_process:
        return state

    # Cache for motor6d parent transforms to avoid repeated to_matrix/inverted work per frame.
    motor_parent_cache: Dict[str, Tuple[Matrix, Matrix]] = {}

    worldspace_bones: Dict[str, str] = {}
    for bone in ao.pose.bones:
        if is_face_control_bone(bone):
            continue
        if excluded_bones and bone.name in excluded_bones:
            continue
        if bone.bone.get("worldspace_bone"):
            original_parent = bone.bone.get("worldspace_original_parent", "")
            if original_parent:
                worldspace_bones[bone.name] = original_parent

    def get_bone_space_transforms(space_bone):
        if not space_bone:
            return None, None

        if space_bone.name in bone_cache:
            return bone_cache.get(space_bone.name)

        # Parent is not in cache, check if it's a motor6d bone.
        parent_has_motor6d_props = (
            "transform" in space_bone.bone
            and "transform1" in space_bone.bone
            and "nicetransform" in space_bone.bone
        )

        if parent_has_motor6d_props:
            # Convert motor6d parent to roblox space for deform calculation (cached).
            cached = motor_parent_cache.get(space_bone.name)
            if cached:
                return cached

            parent_nicetransform = to_matrix(space_bone.bone.get("nicetransform"))
            parent_extr_inv = parent_nicetransform.inverted()

            parent_current = _orthonormalized_transform(
                world_transform @ (space_bone.matrix @ parent_extr_inv)
            )
            parent_rest = _orthonormalized_transform(
                world_transform @ (
                    space_bone.bone.matrix_local @ parent_extr_inv
                )
            )

            motor_parent_cache[space_bone.name] = (parent_current, parent_rest)
            return (parent_current, parent_rest)

        # Parent is neither deform nor motor6d, treat as root.
        return None, None

    def get_parent_transforms(bone):
        if bone.name in worldspace_bones:
            original_parent = ao.pose.bones.get(worldspace_bones[bone.name])
            return get_bone_space_transforms(original_parent)

        return get_bone_space_transforms(bone.parent)

    for bone in bones_to_process:
        # For deform and new bones alike, use Blender-space matrices converted once to Roblox space.
        current_matrix = _orthonormalized_transform(world_transform @ bone.matrix)
        rest_matrix = _orthonormalized_transform(
            world_transform @ bone.bone.matrix_local
        )
        bone_cache[bone.name] = (current_matrix, rest_matrix)

    for bone in bones_to_process:
        try:
            current_matrix, rest_matrix = bone_cache[bone.name]

            if bone.parent or bone.name in worldspace_bones:
                parent_transforms = get_parent_transforms(bone)
                if (
                    parent_transforms is not None
                    and parent_transforms[0] is not None
                    and parent_transforms[1] is not None
                ):
                    parent_current, parent_rest = parent_transforms
                    try:
                        current_local_transform = parent_current.inverted() @ current_matrix
                        rest_local_transform = parent_rest.inverted() @ rest_matrix
                        delta_transform = (
                            rest_local_transform.inverted() @ current_local_transform
                        )
                    except ValueError:
                        delta_transform = rest_matrix.inverted() @ current_matrix
                else:
                    # Parent is not a deform bone, treat as root.
                    delta_transform = rest_matrix.inverted() @ current_matrix
            else:
                delta_transform = rest_matrix.inverted() @ current_matrix

            # Branch behavior: skinned deform-chain bones vs new/helper bones.
            if is_skinned_rig and bone.name in deform_chain_bones:
                # Deform-chain bones: apply corrected Roblox space conversion (axis swizzles and scaling).
                loc, rot, _sca = delta_transform.decompose()
                sf = (
                    translation_scale_factor
                    if abs(translation_scale_factor) > _SCALE_EPSILON
                    else 1.0
                )

                # Apply inverse scale to translation and swizzle axes for Roblox.
                loc = loc / sf
                loc_roblox = Vector((-loc.x, loc.y, -loc.z))

                # Flip rotation axes for Roblox.
                rot.x, rot.z = -rot.x, -rot.z

                # Reconstruct final transform without scale; Roblox Pose CFrames cannot
                # represent scale, and rest-matrix scale leakage distorts normalized playback.
                loc_mat = Matrix.Translation(loc_roblox)
                rot_mat = rot.to_matrix().to_4x4()
                final_transform = loc_mat @ rot_mat
            else:
                # New/helper bones: no scaling; apply position swizzle only (-x, y, -z).
                tr = delta_transform.to_translation()
                tr_swizzled = Vector((-tr.x, tr.y, -tr.z))
                rot_m3 = delta_transform.to_3x3()
                try:
                    rot_m3.normalize()
                except Exception:
                    pass
                loc_mat = Matrix.Translation(tr_swizzled)
                rot_mat = rot_m3.to_4x4()
                final_transform = loc_mat @ rot_mat

            statel = mat_to_cf(final_transform)
            if cf_round:
                statel = [round(x, cf_round_fac) for x in statel]

            if statel != identity_cf:
                export_name = bone.bone.get("rbx_source_name", bone.name)
                state[export_name] = statel
        except Exception as e:
            print(f"[Export] Skipping deform bone '{bone.name}' due to serialization error: {e}")

    return state


def serialize_combined_animation_state(
    ao: "bpy.types.Object",
    ao_eval: "bpy.types.Object",
    run_deform_path: bool,
    skinned_rig: bool,
    back_trans_cached: Optional[Matrix] = None,
    world_transform_cached: Optional[Matrix] = None,
    scale_factor_cached: Optional[float] = None,
    static_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    excluded_deform_bones: Optional[Set[str]] = None,
) -> Dict[str, List[float]]:
    """Sample both Motor6D and deform paths and merge into one state dict.

    Correctly handles mixed rigs; deform data overrides Motor6D data for any
    bone flagged as both.
    """
    state: Dict[str, List[float]] = {}
    # Prepare a lightweight static cache dict shared across both samplers.
    static_cache = static_cache if static_cache is not None else {}

    motor_state = serialize_animation_state(
        ao_eval,
        back_trans_cached,
        static_cache,
    )
    state.update(motor_state)

    if run_deform_path:
        deform_state = serialize_deform_animation_state(
            ao_eval,
            skinned_rig,
            world_transform_cached,
            scale_factor_cached,
            static_cache,
            excluded_deform_bones,
        )
        state.update(deform_state)

    return state
