"""
Animation serialization logic for exporting to Roblox format.
"""

import bpy
import re
import math
from typing import Dict, Set, List, Optional, Any, Tuple
from mathutils import Vector, Matrix
from ..core.constants import (
    get_transform_to_blender,
    identity_cf,
    cf_round,
    cf_round_fac,
)
from ..core.utils import get_scene_fps, mat_to_cf, get_action_fcurves, to_matrix
from .easing import get_easing_for_bone, map_blender_to_roblox_easing


def is_deform_bone_rig(armature: "bpy.types.Object") -> bool:
    """
    Determines if an armature is a deform bone rig by checking if any mesh
    in the scene uses it in an Armature modifier. This is the standard
    and most reliable way to identify skinned meshes.
    """
    if not armature or armature.type != "ARMATURE":
        return False

    # Iterate through all mesh objects in the scene
    for mesh_obj in bpy.data.objects:
        if mesh_obj.type == "MESH":
            # Check if the mesh has an Armature modifier targeting our armature
            for modifier in mesh_obj.modifiers:
                if modifier.type == "ARMATURE" and modifier.object == armature:
                    return True

    return False


def extract_bone_hierarchy(armature: "bpy.types.Object") -> Dict[str, Optional[str]]:
    """
    Extracts the bone hierarchy from an armature.
    Returns a dictionary with bone names as keys and their parent bone names as values.
    Root bones will have None as their parent.
    """
    hierarchy = {}

    if not armature or armature.type != "ARMATURE":
        return hierarchy

    for bone in armature.data.bones:
        if bone.parent:
            hierarchy[bone.name] = bone.parent.name
        else:
            hierarchy[bone.name] = None

    return hierarchy


def serialize_animation_state(
    ao: "bpy.types.Object",
    back_trans_cached: Optional[Matrix] = None,
    static_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, List[float]]:
    """Serialize Motor6D animation state with aggressive static caching"""
    state: Dict[str, List[float]] = {}

    # Use cached transform or compute once
    back_trans = (
        back_trans_cached
        if back_trans_cached is not None
        else get_transform_to_blender().inverted()
    )

    # Local bindings for speed
    pose_bones = ao.pose.bones
    cache: Dict[str, Dict[str, Any]] = static_cache or {}
    get_bone_cache = cache.get
    
    # Build a lookup for world-space bones and their original parents
    worldspace_bones: Dict[str, str] = {}  # bone_name -> original_parent_name
    for bone in pose_bones:
        if bone.bone.get("worldspace_bone"):
            original_parent = bone.bone.get("worldspace_original_parent", "")
            if original_parent:
                worldspace_bones[bone.name] = original_parent

    for bone in pose_bones:
        has_motor6d_props = (
            "transform" in bone.bone
            and "transform1" in bone.bone
            and "nicetransform" in bone.bone
        )

        if has_motor6d_props:
            # --- Traditional Motor6D bone logic ---
            bcache = get_bone_cache(bone.name, {})
            extr_inv = bcache.get("extr_inv")
            orig_base_mat = bcache.get("orig_base_mat")

            if extr_inv is None:
                nicetransform = to_matrix(bone.bone.get("nicetransform"))
                extr_inv = nicetransform.inverted()
            if orig_base_mat is None:
                orig_mat = to_matrix(bone.bone.get("transform"))
                orig_mat_tr1 = to_matrix(bone.bone.get("transform1"))
                orig_base_mat = back_trans @ (orig_mat @ orig_mat_tr1)

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
                        pcb = get_bone_cache(original_parent_name, {})
                        parent_extr_inv = pcb.get("extr_inv")
                        parent_orig_base_mat = pcb.get("orig_base_mat")
                        
                        if parent_extr_inv is None:
                            parent_nicetransform = to_matrix(original_parent_bone.bone.get("nicetransform"))
                            parent_extr_inv = parent_nicetransform.inverted()
                        if parent_orig_base_mat is None:
                            p_orig_mat = to_matrix(original_parent_bone.bone.get("transform"))
                            p_orig_mat_tr1 = to_matrix(original_parent_bone.bone.get("transform1"))
                            parent_orig_base_mat = back_trans @ (p_orig_mat @ p_orig_mat_tr1)
                        
                        # Current parent world transform
                        parent_cur_transform = back_trans @ (original_parent_bone.matrix @ parent_extr_inv)
                        
                        # The bone's world-space target (where it should stay)
                        # This is the current pose of the bone in world space
                        world_target = cur_obj_transform
                        
                        # Calculate what the local transform should be relative to current parent
                        # to achieve the world target position
                        # local = parent^-1 @ world
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
                    pcb = get_bone_cache(bone.parent.name, {})
                    parent_extr_inv = pcb.get("extr_inv")
                    parent_orig_base_mat = pcb.get("orig_base_mat")
                    if parent_extr_inv is None:
                        parent_nicetransform = to_matrix(bone.parent.bone.get("nicetransform"))
                        parent_extr_inv = parent_nicetransform.inverted()
                    if parent_orig_base_mat is None:
                        p_orig_mat = to_matrix(bone.parent.bone.get("transform"))
                        p_orig_mat_tr1 = to_matrix(bone.parent.bone.get("transform1"))
                        parent_orig_base_mat = back_trans @ (
                            p_orig_mat @ p_orig_mat_tr1
                        )

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

    return state


def serialize_deform_animation_state(
    ao: "bpy.types.Object",
    is_skinned_rig: bool,
    world_transform_cached: Optional[Matrix] = None,
    scale_factor_cached: Optional[float] = None,
    static_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, List[float]]:
    """Serialize Deform Bone animation state with static caching"""
    # Use cached transforms or compute once
    if world_transform_cached is None:
        back_trans = get_transform_to_blender().inverted()
        world_transform = back_trans @ ao.matrix_world
    else:
        world_transform = world_transform_cached

    if scale_factor_cached is None:
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        scale_factor = getattr(settings, "rbx_deform_rig_scale", 1.0)
        if scale_factor == 0:
            scale_factor = 1.0
    else:
        scale_factor = scale_factor_cached

    state: Dict[str, List[float]] = {}
    bone_cache: Dict[str, Tuple[Matrix, Matrix]] = {}

    # Pre-populate cache for all non-Motor6D bones to simplify parent lookups
    bones_to_process = []
    for bone in ao.pose.bones:
        # Exclude Motor6D bones from deform serialization
        if (
            "transform" in bone.bone
            and "transform1" in bone.bone
            and "nicetransform" in bone.bone
        ):
            continue
        bones_to_process.append(bone)

    # Enhanced parent transform lookup that handles motor6d parents and deform parents
    def get_parent_transforms(bone):
        if not bone.parent:
            return None, None

        # Check if parent is in deform bone cache
        if bone.parent.name in bone_cache:
            return bone_cache.get(bone.parent.name)

        # Parent is not in cache, check if it's a motor6d bone
        parent_has_motor6d_props = (
            "transform" in bone.parent.bone
            and "transform1" in bone.parent.bone
            and "nicetransform" in bone.parent.bone
        )

        if parent_has_motor6d_props:
            # Convert motor6d parent to roblox space for deform calculation
            parent_nicetransform = to_matrix(bone.parent.bone.get("nicetransform"))
            parent_extr_inv = parent_nicetransform.inverted()

            parent_current = world_transform @ (bone.parent.matrix @ parent_extr_inv)
            parent_rest = world_transform @ (
                bone.parent.bone.matrix_local @ parent_extr_inv
            )
            return (parent_current, parent_rest)

        # Parent is neither deform nor motor6d, treat as root
        return None, None

    for bone in bones_to_process:
        # For deform and new bones alike, use Blender-space matrices converted once to Roblox space
        current_matrix = world_transform @ bone.matrix
        rest_matrix = world_transform @ bone.bone.matrix_local
        bone_cache[bone.name] = (current_matrix, rest_matrix)

    for bone in bones_to_process:
        current_matrix, rest_matrix = bone_cache[bone.name]

        if bone.parent:
            parent_transforms = get_parent_transforms(bone)
            if parent_transforms:
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
                # Parent is not a deform bone, treat as root
                delta_transform = rest_matrix.inverted() @ current_matrix
        else:
            delta_transform = rest_matrix.inverted() @ current_matrix

        # Branch behavior: deform bones vs new/helper bones
        if is_skinned_rig and bone.bone.use_deform:
            # Deform bones: apply corrected Roblox space conversion (axis swizzles and scaling)
            loc, rot, sca = delta_transform.decompose()
            sf = scale_factor if scale_factor != 0 else 1.0

            # Apply inverse scale to translation and swizzle axes for Roblox
            loc = loc / sf
            loc_roblox = Vector((-loc.x, loc.y, -loc.z))

            # Swizzle scale axes for Roblox
            sca_roblox = Vector((sca.x, sca.z, sca.y))

            # Flip rotation axes for Roblox
            rot.x, rot.z = -rot.x, -rot.z

            # Reconstruct final transform: Translate -> Rotate -> Scale
            loc_mat = Matrix.Translation(loc_roblox)
            rot_mat = rot.to_matrix().to_4x4()
            sca_mat = Matrix.Diagonal(sca_roblox).to_4x4()
            final_transform = loc_mat @ rot_mat @ sca_mat
        else:
            # New/helper bones: no scaling; apply position swizzle only (-x, y, -z)
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
            state[bone.name] = statel

    return state


def serialize_combined_animation_state(
    ao: "bpy.types.Object",
    ao_eval: "bpy.types.Object",
    depsgraph: "bpy.types.Depsgraph",
    run_deform_path: bool,
    skinned_rig: bool,
    back_trans_cached: Optional[Matrix] = None,
    world_transform_cached: Optional[Matrix] = None,
    scale_factor_cached: Optional[float] = None,
    static_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, List[float]]:
    """
    Serializes the animation state by running both Motor6D and Deform Bone
    serialization and merging the results. This correctly handles mixed rigs.
    """
    state: Dict[str, List[float]] = {}
    # prepare a lightweight static cache dict shared across both serializers (can be provided by caller)
    static_cache = static_cache if static_cache is not None else {}
    # Always run the standard Motor6D serialization first.
    motor_state = serialize_animation_state(ao_eval, back_trans_cached, static_cache)
    state.update(motor_state)

    # Then, if we need deform data (skinned rig or helper bones), run deform serialization
    # and merge the results. Deform data will override Motor6D data for any
    # bone that might be flagged as both.
    if run_deform_path:
        deform_state = serialize_deform_animation_state(
            ao_eval,
            skinned_rig,
            world_transform_cached,
            scale_factor_cached,
            static_cache,
        )
        state.update(deform_state)

    return state


def get_ik_affected_bones(armature_obj: "bpy.types.Object") -> Set[str]:
    """
    Scans an armature for IK constraints and returns a set of all bone names
    that are part of an IK chain.
    """
    ik_bones: Set[str] = set()
    if not armature_obj or armature_obj.type != "ARMATURE":
        return ik_bones

    for bone in armature_obj.pose.bones:
        for constraint in bone.constraints:
            if constraint.type == "IK":
                # This bone is the end of the chain, add it
                ik_bones.add(bone.name)
                # Add the parents in the chain up to the chain_count
                current_bone = bone
                for _ in range(constraint.chain_count):
                    if current_bone.parent:
                        current_bone = current_bone.parent
                        ik_bones.add(current_bone.name)
                    else:
                        break  # Stop if we reach a root bone
    return ik_bones


def get_all_constrained_bones(armature_obj: "bpy.types.Object") -> Set[str]:
    """
    Finds all bones that are directly or indirectly affected by any constraint.
    For IK, it includes the entire chain. For others, it's the bone with the constraint.
    """
    constrained_bones: Set[str] = set()
    if not armature_obj or armature_obj.type != "ARMATURE":
        return constrained_bones

    for bone in armature_obj.pose.bones:
        if bone.constraints:
            constrained_bones.add(bone.name)
            for constraint in bone.constraints:
                if constraint.type == "IK" and constraint.chain_count > 0:
                    # chain_count specifies how many parent bones are affected.
                    # The bone with the constraint is already added above.
                    # Now add chain_count parent bones.
                    current_bone = bone
                    for _ in range(constraint.chain_count):
                        if current_bone.parent:
                            current_bone = current_bone.parent
                            constrained_bones.add(current_bone.name)
                        else:
                            break
    return constrained_bones


def serialize(ao: "bpy.types.Object") -> Dict[str, Any]:
    """Main serialization function that handles all animation export logic"""
    ctx = bpy.context
    desired_fps = get_scene_fps()

    # Store the current frame to restore it later
    original_frame = ctx.scene.frame_current

    # --- OPTIMIZATION: Get the dependency graph once. ---
    depsgraph = ctx.evaluated_depsgraph_get()
    ao_eval = ao.evaluated_get(depsgraph)

    # --- OPTIMIZATION: Cache deform/skinned detection and new-bone presence. ---
    # is_skinned_rig: true deform/skin rig (armature modifier) or forced by user
    # has_new_bones: bones without Motor6D props present (should run deform path, but not mark rig as deform)
    has_new_bones = any(
        not (
            "transform" in bone.bone
            and "transform1" in bone.bone
            and "nicetransform" in bone.bone
        )
        for bone in ao_eval.pose.bones
    )
    settings = getattr(ctx.scene, "rbx_anim_settings", None)
    force_deform = getattr(settings, "force_deform_bone_serialization", False)
    is_skinned_rig = is_deform_bone_rig(ao) or force_deform
    run_deform_path = is_skinned_rig or has_new_bones

    # Cache static transforms once per serialize call
    back_trans_cached = get_transform_to_blender().inverted()
    world_transform_cached = back_trans_cached @ ao.matrix_world
    settings = getattr(ctx.scene, "rbx_anim_settings", None)
    scale_factor_cached = getattr(settings, "rbx_deform_rig_scale", 1.0)
    if scale_factor_cached == 0:
        scale_factor_cached = 1.0

    # Check if we should use a simple bake for NLA tracks, using the evaluated object.
    use_nla_bake = False
    if ao_eval.animation_data and ao_eval.animation_data.use_nla:
        active_strips = 0
        for track in ao_eval.animation_data.nla_tracks:
            if not track.mute:
                active_strips += len(track.strips)
        if active_strips > 0:
            use_nla_bake = True

    action = ao_eval.animation_data.action if ao_eval.animation_data else None

    # consider constraints only if they actually affect bones (ik chains, copy, etc.)
    has_constraints = len(get_all_constrained_bones(ao_eval)) > 0

    # --- Removed force_full_bake for new bones - use sparse baking instead ---

    # If NLA tracks are active OR if there are constraints without a local action
    # on the armature, we must do a simple, full bake of the visual result.
    # This correctly handles "puppet" rigs driven entirely by other objects.
    if use_nla_bake or (has_constraints and not action):
        if use_nla_bake:
            pass
        else:
            pass

        collected = []
        frames = ctx.scene.frame_end + 1 - ctx.scene.frame_start

        # cache commonly used values
        frame_start = ctx.scene.frame_start
        frame_end = ctx.scene.frame_end
        frame_step = getattr(ctx.scene, "frame_step", 1) or 1  # Fallback for safety
        fps = desired_fps

        # reuse a shared per-bone cache across frames
        shared_cache = {}
        for i in range(frame_start, frame_end + 1, frame_step):
            ctx.scene.frame_set(i)
            # --- OPTIMIZATION: Pass the existing depsgraph instead of re-evaluating. ---
            ao_eval_for_frame = ao.evaluated_get(depsgraph)
            state = serialize_combined_animation_state(
                ao,
                ao_eval_for_frame,
                depsgraph,
                run_deform_path,
                is_skinned_rig,
                back_trans_cached,
                world_transform_cached,
                scale_factor_cached,
                shared_cache,
            )

            # Wrap the raw state in the same format as the hybrid baker for consistency.
            # Since this path has no easing data, we use a default "Linear".
            wrapped_state = {}
            for bone_name, cframe_data in state.items():
                wrapped_state[bone_name] = [cframe_data, "Linear", "Out"]

            collected.append({"t": (i - frame_start) / fps, "kf": wrapped_state})

        result = {"t": (frames - 1) / desired_fps, "kfs": collected}

    # If there's an action, use the intelligent hybrid baker.
    # This now correctly handles the case where an action AND constraints are present.
    elif action:
        # 1. Identify Bone Groups and all relevant Actions
        constrained_bones = get_all_constrained_bones(ao_eval)
        animated_bones = set()
        all_actions = set()

        if action:
            all_actions.add(action)
            fcurves = get_action_fcurves(action)
            for fcurve in fcurves:
                if fcurve.data_path.startswith("pose.bones"):
                    match = re.search(r'pose\.bones\["(.+?)"\]', fcurve.data_path)
                    if match:
                        animated_bones.add(match.group(1))

        # Also find bones driven by constrained targets and gather their actions
        for bone in ao_eval.pose.bones:
            for c in bone.constraints:
                if (
                    hasattr(c, "target")
                    and c.target
                    and c.target.animation_data
                    and c.target.animation_data.action
                ):
                    animated_bones.add(bone.name)
                    all_actions.add(c.target.animation_data.action)

        # decide hybrid vs sparse after we know which bones are actually constrained
        has_constraints_local = len(constrained_bones) > 0
        if has_constraints_local:
            pass
        else:
            pass

        # debug: report key bone groups
        try:
            pass
        except Exception:
            pass

        # 2. Get all relevant keyframe times from all found actions.
        # For Bezier curves, bake all intermediate frames to capture the curve.
        frame_start = ctx.scene.frame_start
        frame_end = ctx.scene.frame_end
        keyframe_times = {frame_start, frame_end}

        all_fcurves = []
        for act in all_actions:
            fcurves = get_action_fcurves(act)
            all_fcurves.extend(fcurves)

        # Track bezier segments per bone to force dense sampling later
        from collections import defaultdict

        bezier_segments: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)

        # Pre-compile regex for performance
        bone_name_pattern = re.compile(r'pose\.bones\["(.+?)"\]')
        
        # Define interpolation set once outside loop
        curved_interpolations = {"BEZIER"}

        for fcurve in all_fcurves:
            # determine bone name for this fcurve (if any)
            bone_name_for_curve = None
            if fcurve.data_path.startswith("pose.bones"):
                m = bone_name_pattern.search(fcurve.data_path)
                if m:
                    bone_name_for_curve = m.group(1)
            # Use an indexed loop to check interpolation between keyframes
            for i, kp in enumerate(fcurve.keyframe_points):
                frame = int(kp.co.x + 0.5)
                if frame_start <= frame <= frame_end:
                    keyframe_times.add(frame)

                # If a keyframe uses curved interpolation, we need to bake all the
                # frames between it and the next keyframe to accurately capture the curve.
                # only densify segments that actually curve (deviate from linear)
                if kp.interpolation in curved_interpolations and i + 1 < len(
                    fcurve.keyframe_points
                ):
                    next_kp = fcurve.keyframe_points[i + 1]
                    start_bezier_frame = int(kp.co.x + 0.5)
                    end_bezier_frame = int(next_kp.co.x + 0.5)

                    # only densify if the segment actually curves
                    if end_bezier_frame - start_bezier_frame > 1:
                        # For BEZIER: always densify to ensure full fidelity
                        keyframe_times.update(
                            range(start_bezier_frame + 1, end_bezier_frame)
                        )
                        if bone_name_for_curve:
                            bezier_segments[bone_name_for_curve].add(
                                (
                                    min(start_bezier_frame, end_bezier_frame),
                                    max(start_bezier_frame, end_bezier_frame),
                                )
                            )

        # 4. Single Baking Pass
        collected = []
        # --- OPTIMIZATION: Avoid redundant set/list conversions. ---
        frame_start = ctx.scene.frame_start
        frame_end = ctx.scene.frame_end
        fps = desired_fps
        # hybrid policy:
        # - with constraints: evaluate every frame; per-bone emission stays sparse except constrained bones
        # - without constraints: evaluate only sparse keyframes (plus bezier fills collected above)
        full_range = getattr(settings, "rbx_full_range_bake", True)

        # Map of {bone_name: {frame_index: (interpolation, easing)}} built from action fcurves
        per_bone_interpolation: Dict[
            str, Dict[int, Tuple[Optional[str], Optional[str]]]
        ] = {}
        if action:
            for fcurve in all_fcurves:
                if not fcurve.data_path.startswith("pose.bones"):
                    continue

                match = bone_name_pattern.search(fcurve.data_path)
                if not match:
                    continue

                bone_name_for_curve = match.group(1)
                frame_map = per_bone_interpolation.setdefault(bone_name_for_curve, {})

                for keyframe_point in fcurve.keyframe_points:
                    frame_idx = int(round(keyframe_point.co.x))
                    if frame_idx < frame_start or frame_idx > frame_end:
                        continue

                    existing = frame_map.get(frame_idx)
                    # Prefer non-constant interpolation over constant when multiple curves share the same frame
                    if (
                        existing
                        and existing[0] == "CONSTANT"
                        and keyframe_point.interpolation != "CONSTANT"
                    ):
                        frame_map[frame_idx] = (
                            keyframe_point.interpolation,
                            keyframe_point.easing,
                        )
                    elif existing is None:
                        frame_map[frame_idx] = (
                            keyframe_point.interpolation,
                            keyframe_point.easing,
                        )

        # Pre-compute constraint target easing data to avoid nested loops per frame
        constraint_target_easing: Dict[str, Dict[int, Tuple[str, str]]] = {}
        for bone in ao_eval.pose.bones:
            if bone.name in constrained_bones:
                for constraint in bone.constraints:
                    if (
                        hasattr(constraint, "target")
                        and constraint.target
                        and constraint.target.animation_data
                        and constraint.target.animation_data.action
                    ):
                        target_action = constraint.target.animation_data.action
                        target_fcurves = get_action_fcurves(target_action)
                        for fcurve in target_fcurves:
                            if (
                                fcurve.data_path.startswith("pose.bones")
                                and constraint.subtarget
                            ):
                                match = bone_name_pattern.search(fcurve.data_path)
                                if match and match.group(1) == constraint.subtarget:
                                    for kp in fcurve.keyframe_points:
                                        frame_idx = int(round(kp.co.x))
                                        if frame_start <= frame_idx <= frame_end:
                                            frame_map = constraint_target_easing.setdefault(
                                                bone.name, {}
                                            )
                                            if frame_idx not in frame_map:
                                                frame_map[frame_idx] = (
                                                    kp.interpolation,
                                                    kp.easing,
                                                )

        def _uses_cyclic(fc):
            try:
                return any(mod.type == "CYCLES" for mod in getattr(fc, "modifiers", []))
            except Exception:
                return False

        fcurves_with_cycles = [fc for fc in all_fcurves if _uses_cyclic(fc)]

        if has_constraints_local:
            all_frames_to_bake = list(range(frame_start, frame_end + 1))
        elif fcurves_with_cycles:
            # For cyclic animations, replicate the base cycle sparsely across the range
            cycle_frames_all: Set[int] = set()
            for fc in fcurves_with_cycles:
                try:
                    for kp in fc.keyframe_points:
                        cycle_frames_all.add(int(round(kp.co.x)))
                except Exception:
                    continue

            extended_frames = set(keyframe_times)

            if cycle_frames_all:
                cycle_sorted = sorted(cycle_frames_all)

                # Determine base cycle interval from the action if available
                base_start = cycle_sorted[0]
                base_end = cycle_sorted[-1]
                if action and action.frame_range:
                    action_start, action_end = action.frame_range
                    base_start = int(math.floor(action_start))
                    base_end = int(math.ceil(action_end - 1e-6))

                frame_step = max(getattr(ctx.scene, "frame_step", 1), 1)
                cycle_len = base_end - base_start
                if cycle_len <= 0:
                    cycle_len = frame_step

                # Collect base cycle frames within one cycle interval
                base_cycle_frames = []
                for fc in fcurves_with_cycles:
                    try:
                        for kp in fc.keyframe_points:
                            frame = int(round(kp.co.x))
                            if base_start - cycle_len <= frame <= base_end:
                                base_cycle_frames.append(frame)
                    except Exception:
                        continue
                base_cycle_frames = sorted(set(base_cycle_frames))

                if not base_cycle_frames:
                    # Fallback: sample densely across the base cycle using frame_step
                    base_cycle_frames = list(
                        range(base_start, base_end + 1, frame_step)
                    )

                if base_end not in base_cycle_frames:
                    base_cycle_frames.append(base_end)
                if base_start not in base_cycle_frames:
                    base_cycle_frames.insert(0, base_start)

                # include previous cycle samples for reference but do not bake them directly
                base_cycle_with_prev = sorted(
                    set(
                        [frame for frame in base_cycle_frames]
                        + [frame - cycle_len for frame in base_cycle_frames]
                    )
                )

                # Replicate backwards to cover frames before the base cycle
                if cycle_len > 0:
                    offset = math.floor((frame_start - base_end) / cycle_len)
                    while base_end + offset * cycle_len >= frame_start:
                        for base_frame in base_cycle_with_prev:
                            new_frame = base_frame + offset * cycle_len
                            if frame_start <= new_frame <= frame_end:
                                extended_frames.add(new_frame)
                        offset -= 1

                # Replicate forward to cover entire range through frame_end
                if cycle_len > 0:
                    offset = math.ceil((frame_start - base_start) / cycle_len)
                    while base_start + offset * cycle_len <= frame_end:
                        for base_frame in base_cycle_with_prev:
                            new_frame = base_frame + offset * cycle_len
                            if frame_start <= new_frame <= frame_end:
                                extended_frames.add(new_frame)
                        offset += 1

            extended_frames.add(frame_end)
            extended_frames.add(frame_start)

            all_frames_to_bake = sorted(extended_frames)
            keyframe_times.update(extended_frames)
        elif full_range:
            # Use range object directly to avoid creating large list in memory
            all_frames_to_bake = list(range(frame_start, frame_end + 1))
        else:
            all_frames_to_bake = sorted(keyframe_times)

        # Final safety check: ensure all frames are within valid range
        # Avoid intermediate set creation for better memory usage
        all_frames_to_bake = [f for f in all_frames_to_bake if frame_start <= f <= frame_end]

        # debug: frame count chosen
        try:
            pass
        except Exception:
            pass

        len(all_frames_to_bake)
        shared_cache = {}
        last_baked_states: Dict[str, List[Any]] = {}

        def _bone_state_equivalent(
            prev_values: List[Any], new_values: List[Any], tol: float = 1e-6
        ) -> bool:
            if len(prev_values) != len(new_values):
                return False

            prev_components, prev_style, prev_direction = prev_values
            new_components, new_style, new_direction = new_values

            if prev_style != new_style or prev_direction != new_direction:
                return False

            if len(prev_components) != len(new_components):
                return False

            for idx in range(len(prev_components)):
                if abs(prev_components[idx] - new_components[idx]) > tol:
                    return False

            return True

        # Set to first frame to ensure proper initialization
        # frame_set() automatically updates the depsgraph, so no need for explicit update
        ctx.scene.frame_set(frame_start)

        for i, frame in enumerate(all_frames_to_bake):
            ctx.scene.frame_set(frame)
            # --- OPTIMIZATION: Pass the existing depsgraph instead of re-evaluating. ---
            ao_eval_for_frame = ao.evaluated_get(depsgraph)

            current_full_pose = serialize_combined_animation_state(
                ao,
                ao_eval_for_frame,
                depsgraph,
                run_deform_path,
                is_skinned_rig,
                back_trans_cached,
                world_transform_cached,
                scale_factor_cached,
                shared_cache,
            )
            final_kf_state = {}
            is_boundary_frame = frame == frame_start or frame == frame_end

            is_sparse_key = frame in keyframe_times
            if is_sparse_key:
                for bone_name in animated_bones:
                    # If an animated bone is at its rest pose on an explicit keyframe,
                    # it won't be in current_full_pose. We need to add it back with an
                    # identity transform to ensure the keyframe is not dropped.
                    if bone_name not in current_full_pose:
                        current_full_pose[bone_name] = identity_cf

            # Also ensure constrained bones are included even if at identity
            for bone_name in constrained_bones:
                if bone_name not in current_full_pose:
                    current_full_pose[bone_name] = identity_cf

            roblox_style, roblox_direction = None, None
            for bone_name, cframe_data in current_full_pose.items():
                is_constrained = bone_name in constrained_bones
                is_animated = bone_name in animated_bones

                # Determine whether this bone should be baked on this frame
                should_bake = False

                if is_constrained:
                    should_bake = True
                elif is_animated and is_sparse_key:
                    should_bake = True
                elif is_animated and is_boundary_frame:
                    should_bake = True
                elif is_animated:
                    # Check whether this frame falls within any BEZIER segment for this bone
                    for start_frame, end_frame in bezier_segments.get(bone_name, set()):
                        if start_frame <= frame <= end_frame:
                            should_bake = True
                            break

                if not should_bake:
                    continue

                # Look up interpolation from pre-cached fcurve data
                interpolation, easing = None, None
                if bone_name in per_bone_interpolation:
                    cached = per_bone_interpolation[bone_name].get(frame)
                    if cached:
                        interpolation, easing = cached

                # Respect explicit keyframe interpolation when available. Fall back to Linear only when
                # Blender does not provide interpolation data (e.g. constraint-only output).
                previous_state = last_baked_states.get(bone_name)

                # If still no interpolation and this is a constrained bone, use pre-computed constraint target easing
                if not interpolation and is_constrained and bone_name in constraint_target_easing:
                    cached_constraint = constraint_target_easing[bone_name].get(frame)
                    if cached_constraint:
                        interpolation, easing = cached_constraint

                if interpolation:
                    roblox_style, roblox_direction = map_blender_to_roblox_easing(
                        interpolation, easing
                    )
                elif previous_state is not None:
                    roblox_style, roblox_direction = (
                        previous_state[1],
                        previous_state[2],
                    )
                else:
                    roblox_style, roblox_direction = ("Linear", "Out")

                candidate_state = [cframe_data, roblox_style, roblox_direction]

                is_constant_hold = (
                    interpolation == "CONSTANT"
                    and not is_sparse_key
                    and not is_boundary_frame
                )

                if is_constant_hold:
                    if previous_state is not None:
                        continue

                # Skip unchanged states for non-constrained bones only
                # Constrained bones must be included on every frame for accurate IK playback
                if not is_sparse_key and not is_boundary_frame and not is_constrained:
                    if previous_state and _bone_state_equivalent(
                        previous_state, candidate_state
                    ):
                        continue

                final_kf_state[bone_name] = candidate_state

            if final_kf_state:
                time_in_seconds = (frame - frame_start) / fps
                collected.append({"t": time_in_seconds, "kf": final_kf_state})
                for baked_bone, state in final_kf_state.items():
                    last_baked_states[baked_bone] = state

        # 4.5. Safety sort to ensure keyframes are always ordered correctly
        # This prevents rare floating point precision issues from causing unordered keyframes
        collected.sort(key=lambda kf: kf["t"])

        # 5. Optimization - remove consecutive duplicate keyframes.
        if len(collected) > 2:

            def _kf_states_equivalent(
                prev_state: Dict[str, List[Any]],
                new_state: Dict[str, List[Any]],
                tol: float = 1e-6,
            ) -> bool:
                if prev_state.keys() != new_state.keys():
                    return False

                for bone_name, prev_values in prev_state.items():
                    new_values = new_state.get(bone_name)
                    if new_values is None:
                        return False

                    if not _bone_state_equivalent(prev_values, new_values, tol):
                        return False

                return True

            optimized_kfs = [collected[0]]
            for i in range(1, len(collected) - 1):
                kf_data = collected[i]
                frame_from_time = int(round(kf_data["t"] * desired_fps + frame_start))
                is_explicit_key = frame_from_time in keyframe_times

                if is_explicit_key:
                    optimized_kfs.append(kf_data)
                    continue

                if not _kf_states_equivalent(optimized_kfs[-1]["kf"], kf_data["kf"]):
                    optimized_kfs.append(kf_data)

            optimized_kfs.append(collected[-1])
            collected = optimized_kfs

        final_duration = (
            (frame_end - frame_start) / desired_fps if frame_end >= frame_start else 0
        )
        result = {"t": final_duration, "kfs": collected}

    else:
        # No NLA, no action, and no constraints. Bake a single frame of the current pose.
        # Also grab the evaluated state for the single-frame pose bake
        ao_eval_for_frame = ao.evaluated_get(depsgraph)
        state = serialize_combined_animation_state(
            ao,
            ao_eval_for_frame,
            depsgraph,
            run_deform_path,
            is_skinned_rig,
            back_trans_cached,
            world_transform_cached,
            scale_factor_cached,
        )
        # For consistency with other code paths, wrap with default easing
        wrapped_state = {}
        for bone_name, cframe_data in state.items():
            wrapped_state[bone_name] = [cframe_data, "Linear", "Out"]
        collected = [{"t": 0, "kf": wrapped_state}]
        result = {"t": 0, "kfs": collected}

    if is_skinned_rig:
        result["is_deform_bone_rig"] = True
        result["bone_hierarchy"] = extract_bone_hierarchy(ao_eval)

    # Restore the original frame
    ctx.scene.frame_set(original_frame)

    # Ensure we always return a valid result, even for empty/static animations
    if not result.get("kfs"):
        # Return a minimal valid animation with the current pose
        ao_eval_for_frame = ao.evaluated_get(depsgraph)
        state = serialize_combined_animation_state(
            ao,
            ao_eval_for_frame,
            depsgraph,
            run_deform_path,
            is_skinned_rig,
            back_trans_cached,
            world_transform_cached,
            scale_factor_cached,
        )
        wrapped_state = {}
        for bone_name, cframe_data in state.items():
            wrapped_state[bone_name] = [cframe_data, "Linear", "Out"]
        result["kfs"] = [{"t": 0, "kf": wrapped_state}]
        result["t"] = 0

    return result
