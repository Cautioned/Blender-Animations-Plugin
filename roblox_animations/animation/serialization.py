# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
"""
Animation export orchestration.

Pipeline stages:
  analyze - inspect the armature, actions, constraints, and NLA setup
  plan    - decide which frames and bones need evaluation (planning module)
  sample  - evaluate bone poses per frame (sampling module)
  reduce  - easing decisions, constant holds, dedupe
  emit    - convert the typed keyframe IR into plain JSON payloads
"""

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import bpy
from mathutils import Matrix

from ..core.constants import get_transform_to_blender, identity_cf
from ..core.utils import (
    get_action_fcurves,
    get_animation_data_action_slot,
    get_scene_fps,
    iter_scene_objects,
)
from .easing import map_blender_to_roblox_easing
from .face_controls import (
    face_control_property_name,
    is_face_control_bone,
    load_facs_payload_from_armature,
    property_group_control_state,
)
from .ir import KeyframePayload, PoseEntry, keyframes_equivalent, pose_entries_equivalent
from .planning import (
    _ROBLOX_MAPPED_INTERPOLATIONS,
    build_bake_plan,
    frame_in_set,
    lookup_interp_for_frame,
)
from .sampling import (
    serialize_animation_state as serialize_animation_state,
    serialize_combined_animation_state,
    serialize_deform_animation_state as serialize_deform_animation_state,
)
from .scale import (
    _object_scale_components,
    _scale_values_are_uniform,
    _uniform_object_scale,
    calculate_deform_target_scale_calibration,
    resolve_deform_rig_scale_factor,
    resolve_deform_translation_scale_factor,
    resolve_manual_deform_rig_scale_factor as resolve_manual_deform_rig_scale_factor,
    resolve_scene_unit_scale_factor,
)

_FACE_CONTROL_FCURVE_RE = re.compile(r'^rbx_face_controls\.([A-Za-z0-9_]+)$')


# Scale calibration and orthonormalization math moved to animation/scale.py.
# Generic interpolation lookup moved to animation/planning.py.


def _face_control_states_equal(
    prev_state: Optional[Dict[str, float]],
    next_state: Dict[str, float],
    tol: float = 1e-6,
) -> bool:
    if prev_state is None:
        return False
    if prev_state.keys() != next_state.keys():
        return False
    for key, prev_value in prev_state.items():
        if abs(prev_value - next_state.get(key, 0.0)) > tol:
            return False
    return True


def _build_face_control_export_context(
    armature_obj: "bpy.types.Object",
    actions: Optional[Set["bpy.types.Action"]] = None,
    action_slots: Optional[Dict["bpy.types.Action", Any]] = None,
) -> Dict[str, Any]:
    payload = load_facs_payload_from_armature(armature_obj)
    if not payload:
        return {
            "enabled": False,
            "control_names": [],
            "animated_controls": set(),
            "keyed_frames": set(),
            "interpolation": {},
            "face_bone_names": set(),
        }

    control_names = list(payload.get("face_control_names") or [])
    property_to_control = {
        face_control_property_name(control_name): control_name for control_name in control_names
    }
    keyed_frames: Set[float] = set()
    animated_controls: Set[str] = set()
    interpolation: Dict[str, Dict[float, Tuple[Optional[str], Optional[str]]]] = {}

    for action in actions or set():
        try:
            fcurves = get_action_fcurves(action, slot=(action_slots or {}).get(action))
        except Exception:
            continue
        for fcurve in fcurves:
            match = _FACE_CONTROL_FCURVE_RE.match(getattr(fcurve, "data_path", ""))
            if not match:
                continue
            control_name = property_to_control.get(match.group(1))
            if not control_name:
                continue
            animated_controls.add(control_name)
            interp_map = interpolation.setdefault(control_name, {})
            for keyframe_point in getattr(fcurve, "keyframe_points", []):
                frame = float(keyframe_point.co.x)
                keyed_frames.add(frame)
                interp_map[frame] = (
                    getattr(keyframe_point, "interpolation", None),
                    getattr(keyframe_point, "easing", None),
                )

    current_state = property_group_control_state(
        getattr(armature_obj, "rbx_face_controls", None),
        control_names,
    )
    has_nonzero_state = any(abs(value) > 1e-6 for value in current_state.values())

    return {
        "enabled": bool(animated_controls or has_nonzero_state),
        "control_names": control_names,
        "animated_controls": animated_controls,
        "keyed_frames": keyed_frames,
        "interpolation": interpolation,
        "face_bone_names": set(payload.get("face_bone_names") or []),
    }


def _serialize_face_control_state_for_frame(
    armature_obj: "bpy.types.Object",
    export_context: Dict[str, Any],
    frame: float,
    last_state: Optional[Dict[str, float]] = None,
    tol: float = 1e-6,
) -> Tuple[Optional[Dict[str, Dict[str, Any]]], Dict[str, float]]:
    control_names = export_context.get("control_names") or []
    raw_state = property_group_control_state(
        getattr(armature_obj, "rbx_face_controls", None),
        control_names,
    )
    explicit_key = frame in (export_context.get("keyed_frames") or set())
    changed = not _face_control_states_equal(last_state, raw_state, tol)
    any_nonzero = any(abs(value) > tol for value in raw_state.values())
    animated_controls = export_context.get("animated_controls") or set()

    if not explicit_key and not changed and not any_nonzero:
        return None, raw_state

    if animated_controls:
        export_names = [control_name for control_name in control_names if control_name in animated_controls]
    else:
        export_names = [control_name for control_name in control_names if abs(raw_state.get(control_name, 0.0)) > tol]
        if not export_names and any_nonzero:
            export_names = list(control_names)
        if not export_names and changed and last_state is not None:
            export_names = [
                control_name
                for control_name in control_names
                if abs((last_state or {}).get(control_name, 0.0) - raw_state.get(control_name, 0.0)) > tol
            ]

    if not export_names and not explicit_key:
        return None, raw_state

    face_state = {}
    interpolation = export_context.get("interpolation") or {}
    for control_name in export_names:
        interp, easing = lookup_interp_for_frame(interpolation.get(control_name), frame)
        if interp:
            easing_style, easing_direction = map_blender_to_roblox_easing(interp, easing)
        else:
            easing_style, easing_direction = ("Linear", "Out")
        face_state[control_name] = {
            "value": float(raw_state.get(control_name, 0.0)),
            "easingStyle": easing_style,
            "easingDirection": easing_direction,
        }

    return (face_state or None), raw_state


def is_deform_bone_rig(armature: "bpy.types.Object") -> bool:
    """
    Determines if an armature is a deform bone rig by checking if any mesh
    in the scene uses it in an Armature modifier. This is the standard
    and most reliable way to identify skinned meshes.
    """
    if not armature or armature.type != "ARMATURE":
        return False

    # Iterate through all mesh objects in the scene
    for mesh_obj in iter_scene_objects():
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
        if is_face_control_bone(bone):
            continue
        if bone.parent:
            hierarchy[bone.name] = bone.parent.name
        else:
            hierarchy[bone.name] = None

    return hierarchy


# Bone samplers moved to animation/sampling.py.


def get_ik_affected_bones(armature_obj: "bpy.types.Object") -> Set[str]:
    """
    Scans an armature for IK constraints and returns a set of all bone names
    that are part of an IK chain.
    """
    ik_bones: Set[str] = set()
    if not armature_obj or armature_obj.type != "ARMATURE":
        return ik_bones

    def add_ik_chain(tail_bone, chain_count):
        ik_bones.add(tail_bone.name)
        current_bone = tail_bone
        if chain_count and chain_count > 0:
            remaining = int(chain_count)
            while remaining > 0 and current_bone.parent:
                current_bone = current_bone.parent
                ik_bones.add(current_bone.name)
                remaining -= 1
        else:
            while current_bone.parent:
                current_bone = current_bone.parent
                ik_bones.add(current_bone.name)

    for bone in armature_obj.pose.bones:
        for constraint in bone.constraints:
            if constraint.type == "IK":
                add_ik_chain(bone, getattr(constraint, "chain_count", 0))
    return ik_bones


def get_all_constrained_bones(armature_obj: "bpy.types.Object") -> Set[str]:
    """
    Finds all bones that are directly or indirectly affected by any constraint.
    For IK, it includes the entire chain. For others, it's the bone with the constraint.
    """
    constrained_bones: Set[str] = set()
    if not armature_obj or armature_obj.type != "ARMATURE":
        return constrained_bones

    def add_ik_chain(tail_bone, chain_count):
        constrained_bones.add(tail_bone.name)
        current_bone = tail_bone
        if chain_count and chain_count > 0:
            remaining = int(chain_count)
            while remaining > 0 and current_bone.parent:
                current_bone = current_bone.parent
                constrained_bones.add(current_bone.name)
                remaining -= 1
        else:
            while current_bone.parent:
                current_bone = current_bone.parent
                constrained_bones.add(current_bone.name)

    for bone in armature_obj.pose.bones:
        if bone.constraints:
            constrained_bones.add(bone.name)
            for constraint in bone.constraints:
                if constraint.type == "IK":
                    add_ik_chain(bone, getattr(constraint, "chain_count", 0))
    return constrained_bones


def get_deform_descendant_bones(
    armature_obj: "bpy.types.Object",
    seed_bones: Set[str],
) -> Set[str]:
    """Return seed bones plus transform descendants for skinned/deform baking."""
    affected_bones: Set[str] = set(seed_bones)
    if not armature_obj or armature_obj.type != "ARMATURE" or not seed_bones:
        return affected_bones

    stack = []
    for bone_name in seed_bones:
        pose_bone = armature_obj.pose.bones.get(bone_name)
        if pose_bone:
            stack.extend(pose_bone.children)

    while stack:
        pose_bone = stack.pop()
        if is_face_control_bone(pose_bone):
            continue
        affected_bones.add(pose_bone.name)
        stack.extend(pose_bone.children)

    return affected_bones


def get_all_driven_bones(armature_obj: "bpy.types.Object") -> Set[str]:
    """
    Finds all bones that are affected by animation drivers.
    This includes drivers on bone transforms, constraints, and custom properties.
    """
    driven_bones: Set[str] = set()
    if not armature_obj or armature_obj.type != "ARMATURE":
        return driven_bones

    anim_data = armature_obj.animation_data
    if not anim_data or not getattr(anim_data, "drivers", None):
        return driven_bones

    bone_name_pattern = re.compile(r'pose\.bones\["(.+?)"\]')
    for fcurve in anim_data.drivers:
        data_path = getattr(fcurve, "data_path", "")
        if not data_path or not data_path.startswith("pose.bones"):
            continue
        match = bone_name_pattern.search(data_path)
        if match:
            driven_bones.add(match.group(1))

    return driven_bones


def _frame_range_from_fcurves(fcurves: Any) -> Optional[Tuple[int, int]]:
    frames: List[float] = []
    for fcurve in fcurves or []:
        for keyframe_point in getattr(fcurve, "keyframe_points", []) or []:
            try:
                frames.append(float(keyframe_point.co.x))
            except Exception:
                continue

    if not frames:
        return None

    return math.floor(min(frames)), math.ceil(max(frames))


def _action_keyframe_range(
    animation_data: Any,
    action: Optional["bpy.types.Action"],
) -> Optional[Tuple[int, int]]:
    if action is None:
        return None

    try:
        fcurves = get_action_fcurves(
            action,
            slot=get_animation_data_action_slot(animation_data, action=action),
        )
    except Exception:
        fcurves = getattr(action, "fcurves", None) or []

    return _frame_range_from_fcurves(fcurves)


def _animation_data_frame_range(animation_data: Any) -> Optional[Tuple[int, int]]:
    if animation_data is None:
        return None
    if getattr(animation_data, "use_nla", False):
        strip_ranges: List[Tuple[float, float]] = []
        for track in getattr(animation_data, "nla_tracks", []) or []:
            if getattr(track, "mute", False):
                continue
            for strip in getattr(track, "strips", []) or []:
                if getattr(strip, "action", None) is None:
                    continue
                try:
                    strip_ranges.append((float(strip.frame_start), float(strip.frame_end)))
                except Exception:
                    continue
        if strip_ranges:
            start = min(frame_range[0] for frame_range in strip_ranges)
            end = max(frame_range[1] for frame_range in strip_ranges)
            return math.floor(start), math.ceil(end)

    return _action_keyframe_range(
        animation_data,
        getattr(animation_data, "action", None),
    )


def _constraint_target_frame_ranges(
    ao: "bpy.types.Object",
) -> List[Tuple[int, int]]:
    ranges: List[Tuple[int, int]] = []
    if not ao or getattr(ao, "type", None) != "ARMATURE":
        return ranges

    seen_targets: Set[int] = set()
    for bone in getattr(ao.pose, "bones", []) or []:
        for constraint in getattr(bone, "constraints", []) or []:
            target = getattr(constraint, "target", None)
            if target is None or target == ao:
                continue
            if getattr(target, "type", None) != "ARMATURE":
                continue
            target_id = id(target)
            if target_id in seen_targets:
                continue
            seen_targets.add(target_id)

            frame_range = _animation_data_frame_range(
                getattr(target, "animation_data", None)
            )
            if frame_range is not None:
                ranges.append(frame_range)

    return ranges


def resolve_export_frame_range(ao: "bpy.types.Object") -> Optional[Tuple[int, int]]:
    """Return the frame range for the armature's current export source."""
    ranges: List[Tuple[int, int]] = []

    own_range = _animation_data_frame_range(getattr(ao, "animation_data", None))
    if own_range is not None:
        ranges.append(own_range)

    ranges.extend(_constraint_target_frame_ranges(ao))

    if not ranges:
        return None

    return min(frame_range[0] for frame_range in ranges), max(
        frame_range[1] for frame_range in ranges
    )


def sync_scene_frame_range_to_export_source(
    scene: "bpy.types.Scene",
    ao: "bpy.types.Object",
) -> Optional[Tuple[int, int]]:
    frame_range = resolve_export_frame_range(ao)
    if frame_range is None:
        return None

    frame_start, frame_end = frame_range
    if frame_end < frame_start:
        return None

    scene.frame_start = frame_start
    scene.frame_end = frame_end
    return frame_range


@dataclass
class ExportAnalysis:
    """Everything known about the export before any frame is evaluated."""

    ao: Any
    ao_eval: Any
    depsgraph: Any
    desired_fps: float
    animation_data: Any
    action: Optional[Any]
    action_slot: Any
    action_slots: Dict[Any, Any]
    face_export_context: Dict[str, Any]
    excluded_face_bones: Set[str]
    is_skinned_rig: bool
    run_deform_path: bool
    back_trans_cached: Matrix
    world_transform_cached: Matrix
    scale_factor_cached: float
    target_scale_multiplier: Optional[float]
    target_scale_calibration: Optional[Dict[str, Any]]
    use_nla_bake: bool
    nla_single_action: Optional[Any]
    has_constraints: bool
    has_drivers: bool
    has_constraint_target_action: bool


def _analyze_export(
    ao: "bpy.types.Object",
    target_bone_rest: Optional[Dict[str, Any]],
) -> ExportAnalysis:
    """Stage 1: inspect the armature, actions, constraints, and NLA setup."""
    ctx = bpy.context
    desired_fps = get_scene_fps()
    animation_data = getattr(ao, "animation_data", None)
    settings = getattr(ctx.scene, "rbx_anim_settings", None)

    # Get the dependency graph once.
    depsgraph = ctx.evaluated_depsgraph_get()
    ao_eval = ao.evaluated_get(depsgraph)

    # is_skinned_rig: true deform/skin rig (armature modifier) or forced by
    # user. has_new_bones: bones without Motor6D props present (run the deform
    # path, but do not mark the rig as deform).
    has_new_bones = any(
        not (
            "transform" in bone.bone
            and "transform1" in bone.bone
            and "nicetransform" in bone.bone
        )
        and not is_face_control_bone(bone)
        for bone in ao_eval.pose.bones
    )
    force_deform = getattr(settings, "force_deform_bone_serialization", False)
    is_skinned_rig = is_deform_bone_rig(ao) or force_deform
    run_deform_path = is_skinned_rig or has_new_bones

    # Cache static transforms once per serialize call.
    back_trans_cached = get_transform_to_blender().inverted()
    world_transform_cached = back_trans_cached @ ao.matrix_world

    auto_deform_scale_for_calibration = getattr(settings, "rbx_auto_deform_scale", True)
    target_scale_calibration = None
    if target_bone_rest and auto_deform_scale_for_calibration:
        target_scale_calibration = calculate_deform_target_scale_calibration(
            ao,
            target_bone_rest,
        )
    target_scale_multiplier = (
        target_scale_calibration.get("multiplier")
        if target_scale_calibration
        else None
    )
    scale_factor_cached = resolve_deform_rig_scale_factor(
        ao,
        settings,
        target_scale_multiplier=target_scale_multiplier,
    )

    # Check if we should use a simple bake for NLA tracks.
    use_nla_bake = False
    nla_single_action = None
    if animation_data and animation_data.use_nla:
        active_strips = 0
        single_strip = None
        for track in animation_data.nla_tracks:
            if track.mute:
                continue
            for strip in track.strips:
                active_strips += 1
                if single_strip is None:
                    single_strip = strip
        if active_strips > 0:
            # If there's exactly one active strip, prefer hybrid bake unless it
            # contains interpolation Roblox cannot represent directly.
            if active_strips == 1 and single_strip and single_strip.action:
                strip_action = single_strip.action
                try:
                    strip_fcurves = get_action_fcurves(
                        strip_action,
                        slot=get_animation_data_action_slot(
                            animation_data,
                            action=strip_action,
                        ),
                    )
                    requires_dense_bake = any(
                        kp.interpolation not in _ROBLOX_MAPPED_INTERPOLATIONS
                        for fc in strip_fcurves
                        for kp in fc.keyframe_points
                    )
                except Exception:
                    requires_dense_bake = False

                if requires_dense_bake:
                    use_nla_bake = True
                else:
                    nla_single_action = strip_action
            else:
                use_nla_bake = True

    action = nla_single_action or (animation_data.action if animation_data else None)
    action_slot = get_animation_data_action_slot(animation_data, action=action)
    action_slots = {}
    if action is not None:
        action_slots[action] = action_slot

    face_export_context = _build_face_control_export_context(
        ao,
        {action} if action is not None else set(),
        action_slots,
    )
    export_face_controls = bool(face_export_context.get("enabled"))
    excluded_face_bones = (
        face_export_context.get("face_bone_names", set()) if export_face_controls else set()
    )

    # Consider constraints only if they actually affect bones (ik chains, copy, etc.)
    has_constraints = len(get_all_constrained_bones(ao)) > 0
    has_drivers = len(get_all_driven_bones(ao)) > 0
    has_constraint_target_action = any(
        getattr(getattr(constraint, "target", None), "animation_data", None)
        and getattr(constraint.target.animation_data, "action", None)
        for bone in ao.pose.bones
        for constraint in bone.constraints
    )

    return ExportAnalysis(
        ao=ao,
        ao_eval=ao_eval,
        depsgraph=depsgraph,
        desired_fps=desired_fps,
        animation_data=animation_data,
        action=action,
        action_slot=action_slot,
        action_slots=action_slots,
        face_export_context=face_export_context,
        excluded_face_bones=excluded_face_bones,
        is_skinned_rig=is_skinned_rig,
        run_deform_path=run_deform_path,
        back_trans_cached=back_trans_cached,
        world_transform_cached=world_transform_cached,
        scale_factor_cached=scale_factor_cached,
        target_scale_multiplier=target_scale_multiplier,
        target_scale_calibration=target_scale_calibration,
        use_nla_bake=use_nla_bake,
        nla_single_action=nla_single_action,
        has_constraints=has_constraints,
        has_drivers=has_drivers,
        has_constraint_target_action=has_constraint_target_action,
    )


def _bake_full(analysis: ExportAnalysis) -> Tuple[float, List[KeyframePayload]]:
    """Dense bake: evaluate every frame of the scene range.

    Used when NLA strips or unkeyed constraints/drivers force a full
    evaluation of the visual result.
    """
    ctx = bpy.context
    ao = analysis.ao

    constrained_bones = get_all_constrained_bones(ao)
    driven_bones = get_all_driven_bones(ao)
    if driven_bones:
        constrained_bones.update(driven_bones)
    for bone in ao.pose.bones:
        if not bone.bone.use_inherit_rotation:
            constrained_bones.add(bone.name)
    if analysis.is_skinned_rig:
        constrained_bones = get_deform_descendant_bones(ao, constrained_bones)

    frames = ctx.scene.frame_end + 1 - ctx.scene.frame_start
    frame_start = ctx.scene.frame_start
    frame_end = ctx.scene.frame_end
    frame_step = getattr(ctx.scene, "frame_step", 1) or 1
    fps = analysis.desired_fps

    collected: List[KeyframePayload] = []
    shared_cache: Dict[str, Dict[str, Any]] = {}
    for i in range(frame_start, frame_end + 1, frame_step):
        ctx.scene.frame_set(i)
        ao_eval_for_frame = ao.evaluated_get(analysis.depsgraph)
        state = serialize_combined_animation_state(
            ao,
            ao_eval_for_frame,
            analysis.run_deform_path,
            analysis.is_skinned_rig,
            analysis.back_trans_cached,
            analysis.world_transform_cached,
            analysis.scale_factor_cached,
            shared_cache,
            analysis.excluded_face_bones,
        )
        face_state, _ = _serialize_face_control_state_for_frame(
            ao,
            analysis.face_export_context,
            float(i),
        )

        # This path has no easing data, so everything defaults to Linear.
        for bone_name in constrained_bones:
            if bone_name not in state:
                state[bone_name] = list(identity_cf)
        poses = {
            bone_name: PoseEntry(list(cframe_data), "Linear", "Out")
            for bone_name, cframe_data in state.items()
        }
        collected.append(
            KeyframePayload(
                time=(i - frame_start) / fps,
                poses=poses,
                face=face_state or None,
            )
        )

    duration = (frames - 1) / analysis.desired_fps
    return duration, collected


def _bake_single_frame(analysis: ExportAnalysis) -> Tuple[float, List[KeyframePayload]]:
    """Bake one keyframe of the current pose (no actions or constraints)."""
    ctx = bpy.context
    ao_eval_for_frame = analysis.ao.evaluated_get(analysis.depsgraph)
    state = serialize_combined_animation_state(
        analysis.ao,
        ao_eval_for_frame,
        analysis.run_deform_path,
        analysis.is_skinned_rig,
        analysis.back_trans_cached,
        analysis.world_transform_cached,
        analysis.scale_factor_cached,
        excluded_deform_bones=analysis.excluded_face_bones,
    )
    poses = {
        bone_name: PoseEntry(list(cframe_data), "Linear", "Out")
        for bone_name, cframe_data in state.items()
    }
    face_state, _ = _serialize_face_control_state_for_frame(
        analysis.ao,
        analysis.face_export_context,
        float(ctx.scene.frame_start),
    )
    payload = KeyframePayload(time=0, poses=poses, face=face_state or None)
    return 0, [payload]


def _bake_hybrid(analysis: ExportAnalysis) -> Tuple[float, List[KeyframePayload]]:
    """Sparse bake driven by keyframes, with dense fallbacks where needed."""
    ctx = bpy.context
    ao = analysis.ao
    action = analysis.action

    # 1. Identify bone groups and all relevant actions.
    constrained_bones = get_all_constrained_bones(ao)
    driven_bones = get_all_driven_bones(ao)
    if driven_bones:
        constrained_bones.update(driven_bones)
    if analysis.is_skinned_rig:
        constrained_bones = get_deform_descendant_bones(ao, constrained_bones)

    # Bones with inherit_rotation disabled must be baked every frame.
    # Roblox Motor6D hierarchy always inherits parent rotation, so the
    # serializer must emit a varying compensation CFrame each frame to
    # keep the bone world-space-stable when its parent moves.
    # These are tracked separately from constrained_bones because the
    # constrained path has easing-thinning logic that would skip them.
    non_inheriting_bones: Set[str] = set()
    worldspace_parent_map: Dict[str, str] = {}
    for bone in ao.pose.bones:
        if not bone.bone.use_inherit_rotation:
            non_inheriting_bones.add(bone.name)
        if bone.bone.get("worldspace_bone"):
            original_parent = bone.bone.get("worldspace_original_parent", "")
            if original_parent:
                worldspace_parent_map[bone.name] = original_parent
    if analysis.is_skinned_rig:
        non_inheriting_bones = get_deform_descendant_bones(
            ao,
            non_inheriting_bones,
        )

    animated_bones = set()
    all_actions = set()

    if action:
        all_actions.add(action)
        fcurves = get_action_fcurves(action, slot=analysis.action_slot)
        for fcurve in fcurves:
            if fcurve.data_path.startswith("pose.bones"):
                match = re.search(r'pose\.bones\["(.+?)"\]', fcurve.data_path)
                if match:
                    animated_bones.add(match.group(1))

    face_export_context = _build_face_control_export_context(
        ao,
        all_actions,
        analysis.action_slots,
    )
    export_face_controls = bool(face_export_context.get("enabled"))
    excluded_face_bones = (
        face_export_context.get("face_bone_names", set()) if export_face_controls else set()
    )

    # Also find bones driven by constrained targets and gather their actions.
    for bone in ao.pose.bones:
        for constraint in bone.constraints:
            if (
                hasattr(constraint, "target")
                and constraint.target
                and constraint.target.animation_data
                and constraint.target.animation_data.action
            ):
                animated_bones.add(bone.name)
                target_action = constraint.target.animation_data.action
                all_actions.add(target_action)
                analysis.action_slots[target_action] = get_animation_data_action_slot(
                    constraint.target.animation_data,
                    action=target_action,
                )

    has_constraints_local = len(constrained_bones) > 0 or len(non_inheriting_bones) > 0

    # 2. Stage the keyframe plan: which frames/bones to evaluate, and how
    # per-channel easing reduces to per-pose easing.
    frame_start = ctx.scene.frame_start
    frame_end = ctx.scene.frame_end
    keyframe_times = {frame_start, frame_end}
    keyframe_times.update(face_export_context.get("keyed_frames") or set())

    all_fcurves = []
    for act in all_actions:
        fcurves = get_action_fcurves(act, slot=analysis.action_slots.get(act))
        all_fcurves.extend(fcurves)

    settings = getattr(ctx.scene, "rbx_anim_settings", None)
    plan = build_bake_plan(
        ao,
        constrained_bones,
        action,
        analysis.action_slots,
        all_fcurves,
        keyframe_times,
        frame_start,
        frame_end,
        analysis.desired_fps,
        getattr(settings, "rbx_full_range_bake", True),
        has_constraints_local,
        getattr(ctx.scene, "frame_step", 1) or 1,
    )

    # 3. Single baking pass: sample -> reduce -> collect.
    collected: List[KeyframePayload] = []
    collected_frames: List[float] = []
    shared_cache: Dict[str, Dict[str, Any]] = {}
    last_baked_states: Dict[str, PoseEntry] = {}
    last_face_state: Optional[Dict[str, float]] = None
    current_full_pose: Dict[str, List[float]] = {}
    final_kf_state: Dict[str, PoseEntry] = {}

    # Set to first frame to ensure proper initialization.  frame_set()
    # automatically updates the depsgraph, so no explicit update is needed.
    ctx.scene.frame_set(plan.frame_start)

    for frame in plan.frames:
        frame_int = int(math.floor(frame))
        frame_sub = float(frame - frame_int)
        ctx.scene.frame_set(frame_int, subframe=frame_sub)
        ao_eval_for_frame = ao.evaluated_get(analysis.depsgraph)

        current_full_pose.clear()
        current_full_pose.update(
            serialize_combined_animation_state(
                ao,
                ao_eval_for_frame,
                analysis.run_deform_path,
                analysis.is_skinned_rig,
                analysis.back_trans_cached,
                analysis.world_transform_cached,
                analysis.scale_factor_cached,
                shared_cache,
                excluded_face_bones,
            )
        )

        if analysis.is_skinned_rig and last_baked_states:
            for bone_name in last_baked_states:
                if bone_name not in current_full_pose:
                    current_full_pose[bone_name] = identity_cf

        final_kf_state.clear()
        is_boundary_frame = frame == plan.frame_start or frame == plan.frame_end
        face_kf_state, last_face_state = _serialize_face_control_state_for_frame(
            ao,
            face_export_context,
            float(frame),
            last_face_state,
        )

        for bone_name in animated_bones:
            bone_keyframes = plan.per_bone_keyframes.get(bone_name)
            constraint_keyframes = plan.constraint_target_easing.get(bone_name)
            bone_kfs = plan.per_bone_keyframes.get(bone_name, set())
            # For cyclic bones, we only care about frames where THIS bone has keyframes.
            is_cyclic_key = (
                bool(plan.cyclic_bones)
                and bone_name in plan.cyclic_bones
                and frame_in_set(bone_kfs, frame)
            )
            # Cyclic bones should also be emitted at boundary frames.
            is_cyclic_boundary = (
                bool(plan.cyclic_bones)
                and bone_name in plan.cyclic_bones
                and is_boundary_frame
            )
            if (
                frame_in_set(bone_keyframes, frame)
                or frame_in_set(set(constraint_keyframes.keys()) if constraint_keyframes else None, frame)
                or is_cyclic_key
                or is_cyclic_boundary
            ):
                # If an animated bone is at its rest pose on an explicit keyframe,
                # it won't be in current_full_pose. Add identity back so the
                # keyframe is not dropped.
                if bone_name not in current_full_pose:
                    current_full_pose[bone_name] = identity_cf

        # Also ensure constrained bones are included even if at identity.
        for bone_name in constrained_bones:
            if bone_name not in current_full_pose:
                current_full_pose[bone_name] = identity_cf

        # Also ensure non-inheriting bones are included.
        for bone_name in non_inheriting_bones:
            if bone_name not in current_full_pose:
                current_full_pose[bone_name] = identity_cf

        for bone_name, cframe_data in current_full_pose.items():
            is_constrained = bone_name in constrained_bones
            is_non_inheriting = bone_name in non_inheriting_bones
            is_animated = bone_name in animated_bones
            is_cyclic_forced = plan.force_cyclic_full_bake and bone_name in plan.cyclic_bones
            bone_keyframes = plan.per_bone_keyframes.get(bone_name)
            constraint_keyframes = plan.constraint_target_easing.get(bone_name)
            # For cyclic bones, only consider frames where THIS bone has keyframes.
            is_cyclic_key = (
                bool(plan.cyclic_bones)
                and bone_name in plan.cyclic_bones
                and frame_in_set(bone_keyframes, frame)
            )
            is_sparse_key = (
                frame_in_set(bone_keyframes, frame)
                or frame_in_set(set(constraint_keyframes.keys()) if constraint_keyframes else None, frame)
                or is_cyclic_key
            )

            # For cyclic bones, bake boundary frames since the animation
            # must cover the entire scene range.
            is_cyclic_boundary = (
                bool(plan.cyclic_bones)
                and bone_name in plan.cyclic_bones
                and is_boundary_frame
            )
            is_boundary_bake = (is_boundary_frame or is_cyclic_boundary) and is_animated

            # Determine whether this bone should be baked on this frame.
            should_bake = False

            if is_constrained:
                should_bake = True
            elif is_non_inheriting:
                should_bake = True
            elif analysis.is_skinned_rig:
                should_bake = True
            elif is_cyclic_forced:
                should_bake = True
            elif is_boundary_bake:
                should_bake = True
            elif is_animated and is_sparse_key:
                should_bake = True
            elif is_animated:
                # Unsupported interpolation is represented by dense linear samples.
                for start_frame, end_frame in plan.dense_interpolation_segments.get(bone_name, set()):
                    if start_frame <= frame <= end_frame:
                        should_bake = True
                        break
                if not should_bake:
                    for start_frame, end_frame in plan.mixed_interpolation_segments.get(bone_name, set()):
                        if start_frame <= frame < end_frame:
                            should_bake = True
                            break

            if not should_bake:
                continue

            # Look up interpolation from pre-cached fcurve data: exact frame
            # first, then the most recent keyframe before this one.
            interpolation, easing = None, None
            if bone_name in plan.per_bone_interpolation:
                interpolation, easing = lookup_interp_for_frame(
                    plan.per_bone_interpolation.get(bone_name),
                    frame,
                )

            # The dense samples are already evaluated through Blender, so
            # linear Roblox segments reproduce their combined transform.
            # Choosing CONSTANT here would freeze every component again.
            if any(
                start_frame <= frame < end_frame
                for start_frame, end_frame in plan.mixed_interpolation_segments.get(bone_name, set())
            ):
                interpolation, easing = "LINEAR", "EASE_OUT"

            # Non-inheriting/world-space bones often have no direct fcurves.
            # In that case, borrow interpolation from their original parent
            # (typically the master/controller bone) so Constant holds are
            # preserved instead of defaulting to Linear smoothing.
            if interpolation is None and is_non_inheriting:
                source_bone = worldspace_parent_map.get(bone_name)
                if source_bone:
                    interpolation, easing = lookup_interp_for_frame(
                        plan.per_bone_interpolation.get(source_bone),
                        frame,
                    )
                    if interpolation is None and source_bone in plan.constraint_target_easing:
                        interpolation, easing = lookup_interp_for_frame(
                            plan.constraint_target_easing.get(source_bone),
                            frame,
                        )

            # Respect explicit keyframe interpolation when available. Fall back
            # to Linear only when Blender provides no interpolation data (e.g.
            # constraint-only output).
            previous_state = last_baked_states.get(bone_name)

            # If still no interpolation and this is a constrained bone, use the
            # pre-computed constraint target easing.
            if not interpolation and is_constrained and bone_name in plan.constraint_target_easing:
                cached_constraint = plan.constraint_target_easing[bone_name].get(frame)
                if cached_constraint:
                    interpolation, easing = cached_constraint

            if interpolation:
                roblox_style, roblox_direction = map_blender_to_roblox_easing(
                    interpolation, easing
                )
            elif previous_state is not None:
                roblox_style, roblox_direction = (
                    previous_state.style,
                    previous_state.direction,
                )
            else:
                roblox_style, roblox_direction = ("Linear", "Out")

            # If a constrained bone has no interpolation but frame keys are all
            # constant, treat as constant to avoid blending between rows of
            # constant keys.
            bone_const_frames = plan.bone_constant_keyframes.get(bone_name, set())
            bone_nonconst_frames = plan.bone_non_constant_keyframes.get(
                bone_name, set()
            )
            if (
                is_constrained
                and interpolation is None
                and frame in bone_const_frames
                and frame not in bone_nonconst_frames
            ):
                interpolation = "CONSTANT"
                roblox_style, roblox_direction = ("Constant", "Out")

            # If we're between constant keys (or at boundary) with no interpolation,
            # clamp the pose to the previous constant to avoid blended samples.
            if (
                previous_state is not None
                and previous_state.style == "Constant"
                and interpolation is None
                and not is_sparse_key
            ):
                cframe_data = previous_state.components
                roblox_style, roblox_direction = (
                    previous_state.style,
                    previous_state.direction,
                )

            has_explicit_easing = (
                bone_name in plan.per_bone_interpolation
                or bone_name in plan.constraint_target_easing
            )

            # For constrained mapped easing styles, only emit on keys/boundaries.
            # Unsupported interpolation is handled by the dense segment path.
            if (
                analysis.nla_single_action is not None
                and is_constrained
                and not is_non_inheriting
                and has_explicit_easing
                and not is_sparse_key
                and not is_boundary_frame
            ):
                if interpolation in {"CONSTANT", "LINEAR", "CUBIC", "BOUNCE", "ELASTIC"}:
                    continue
                if (
                    interpolation is None
                    and previous_state is not None
                    and previous_state.style in {"Constant", "Linear", "CubicV2", "Bounce", "Elastic"}
                ):
                    continue

            # Avoid emitting boundary frames for constrained mapped easing when no key exists.
            if (
                analysis.nla_single_action is not None
                and is_constrained
                and not is_non_inheriting
                and has_explicit_easing
                and is_boundary_frame
                and not is_sparse_key
                and previous_state is not None
                and previous_state.style in {"Constant", "Linear", "CubicV2", "Bounce", "Elastic"}
                and interpolation is None
            ):
                continue

            # For CONSTANT holds, clamp to previous pose to avoid blending.
            # For cyclic boundary frames (e.g. frame_end) that aren't explicit
            # keys, blender's cyclic modifier wraps the evaluation into the
            # NEXT cycle, producing a value jump.  Clamp those too.
            if (
                previous_state is not None
                and not is_sparse_key
                and not is_constrained
                and (interpolation == "CONSTANT" or roblox_style == "Constant")
                and (not is_boundary_frame or is_cyclic_boundary)
            ):
                cframe_data = previous_state.components

            candidate_state = PoseEntry(list(cframe_data), roblox_style, roblox_direction)

            is_constant_hold = (
                interpolation == "CONSTANT"
                and not is_sparse_key
                and not is_boundary_frame
                and not is_constrained
            )

            if is_constant_hold:
                if previous_state is not None:
                    continue

            # Skip unchanged states for unconstrained bones.
            # We still evaluate cyclic bones every frame for correctness,
            # but only emit frames where the sampled pose actually changes.
            # Constrained bones must be included on every frame for accurate IK playback.
            if not is_sparse_key and not is_boundary_frame and not is_constrained and not is_non_inheriting and not is_cyclic_boundary:
                if previous_state and pose_entries_equivalent(
                    previous_state, candidate_state
                ):
                    continue

            final_kf_state[bone_name] = candidate_state

        # Roblox treats a Pose absent from a Keyframe as CFrame.identity,
        # not as "hold previous."  When siblings have staggered keys a
        # constant-hold bone would be missing from keyframes created by
        # its siblings, snapping to identity.  Ensure every bone that is
        # mid-constant-hold appears in every emitted keyframe.
        if final_kf_state:
            for held_bone, held_state in last_baked_states.items():
                if held_bone in final_kf_state:
                    continue
                if held_state.style != "Constant":
                    continue
                # Constrained/non-inheriting bones should not be force-held
                # by sparse carry-forward because they are evaluated explicitly.
                if held_bone in constrained_bones or held_bone in non_inheriting_bones:
                    continue
                final_kf_state[held_bone] = PoseEntry(
                    list(held_state.components),
                    held_state.style,
                    held_state.direction,
                )

        if final_kf_state or face_kf_state:
            collected.append(
                KeyframePayload(
                    time=(frame - plan.frame_start) / plan.fps,
                    poses=dict(final_kf_state),
                    face=face_kf_state or None,
                )
            )
            collected_frames.append(frame)
            for baked_bone, entry in final_kf_state.items():
                last_baked_states[baked_bone] = entry

    # Ensure we end with a hold keyframe at the final frame to prevent early
    # resets.  Only add bones that don't already have a key at the end frame.
    if collected and last_baked_states:
        last_recorded_frame = collected_frames[-1] if collected_frames else None
        if last_recorded_frame is None or last_recorded_frame < plan.frame_end:
            end_time = (plan.frame_end - plan.frame_start) / plan.fps
            hold_poses: Dict[str, PoseEntry] = {}
            for baked_bone, entry in last_baked_states.items():
                # Check if this bone already has a key at the end frame.
                bone_kfs = plan.per_bone_keyframes.get(baked_bone, set())
                if plan.frame_end not in bone_kfs:
                    # Only add end hold for bones that don't have a key there.
                    hold_poses[baked_bone] = PoseEntry(
                        list(entry.components), entry.style, entry.direction,
                    )
            if hold_poses:
                collected.append(
                    KeyframePayload(time=end_time, poses=hold_poses)
                )
                collected_frames.append(plan.frame_end)

    # Safety sort to ensure keyframes are always ordered correctly.
    # This prevents rare floating point precision issues from causing unordered keyframes.
    if collected:
        combined = list(zip(collected, collected_frames))
        combined.sort(key=lambda item: item[0].time)
        collected = [item[0] for item in combined]
        collected_frames = [item[1] for item in combined]

    # 5. Optimization - remove consecutive duplicate keyframes.
    if len(collected) > 2:
        optimized_entries = [(collected[0], collected_frames[0])]
        for i in range(1, len(collected) - 1):
            kf_data = collected[i]
            frame_idx = collected_frames[i]
            if frame_in_set(plan.keyframe_times, frame_idx):
                optimized_entries.append((kf_data, frame_idx))
                continue
            if not keyframes_equivalent(optimized_entries[-1][0], kf_data):
                optimized_entries.append((kf_data, frame_idx))
        optimized_entries.append((collected[-1], collected_frames[-1]))
        collected = [entry for entry, _ in optimized_entries]
        collected_frames = [frame for _, frame in optimized_entries]

    final_duration = (
        (plan.frame_end - plan.frame_start) / analysis.desired_fps
        if plan.frame_end >= plan.frame_start
        else 0
    )
    return final_duration, collected


def serialize(
    ao: "bpy.types.Object",
    target_bone_rest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Main serialization entry point: analyze -> plan -> sample -> emit."""
    ctx = bpy.context

    # Store the current frame to restore it later.
    original_frame = ctx.scene.frame_current
    analysis = _analyze_export(ao, target_bone_rest)

    try:
        if (
            analysis.use_nla_bake
            or (
                analysis.has_constraints
                and not analysis.action
                and not analysis.has_constraint_target_action
            )
            or (analysis.has_drivers and not analysis.action)
        ):
            duration, collected = _bake_full(analysis)
        elif analysis.action or analysis.has_constraint_target_action:
            duration, collected = _bake_hybrid(analysis)
        else:
            duration, collected = _bake_single_frame(analysis)

        result = {
            "t": duration,
            "kfs": [keyframe.to_payload() for keyframe in collected],
        }
    finally:
        # Always restore the original frame, even on failure.
        ctx.scene.frame_set(original_frame)

    if analysis.is_skinned_rig:
        result["is_deform_bone_rig"] = True
        result["bone_hierarchy"] = extract_bone_hierarchy(analysis.ao_eval)

    # Export FPS metadata for consumers (e.g., Roblox) that want to preserve timing.
    try:
        scene = ctx.scene
        settings = getattr(scene, "rbx_anim_settings", None)
        object_scale_components = _object_scale_components(ao)
        object_scale_uniform = _scale_values_are_uniform(object_scale_components)
        object_scale_axes = list(object_scale_components or (1.0, 1.0, 1.0))
        auto_deform_scale = getattr(settings, "rbx_auto_deform_scale", True)
        scene_unit_scale = resolve_scene_unit_scale_factor(settings, scene)
        calibrated_target_multiplier = float(analysis.target_scale_multiplier or 1.0)
        deform_scale_mode = (
            "auto_calibrated"
            if auto_deform_scale and analysis.target_scale_calibration
            else "auto" if auto_deform_scale else "manual"
        )
        result["export_info"] = {
            "fps": float(analysis.desired_fps),
            "fps_base": float(getattr(scene.render, "fps_base", 1.0) or 1.0),
            "frame_start": int(getattr(scene, "frame_start", 0)),
            "frame_end": int(getattr(scene, "frame_end", 0)),
            "frame_step": int(getattr(scene, "frame_step", 1) or 1),
            "deform_scale_mode": deform_scale_mode,
            "deform_scale_factor": float(analysis.scale_factor_cached),
            "deform_translation_scale_factor": float(
                resolve_deform_translation_scale_factor(
                    ao,
                    settings,
                    target_scale_multiplier=analysis.target_scale_multiplier,
                    scale_factor=analysis.scale_factor_cached,
                )
            ),
            "deform_target_scale_multiplier": float(calibrated_target_multiplier),
            "scene_unit_scale": float(scene_unit_scale),
            "deform_target_scale_sample_count": int(
                analysis.target_scale_calibration.get("sample_count", 0)
                if analysis.target_scale_calibration
                else 0
            ),
            "armature_object_scale": float(_uniform_object_scale(ao) or 1.0),
            "armature_object_scale_axes": [float(value) for value in object_scale_axes],
            "armature_object_scale_uniform": bool(object_scale_uniform),
            "deform_position_scale_reliable": bool(
                (not auto_deform_scale) or object_scale_uniform
            ),
            "time_unit": "seconds",
        }
        if auto_deform_scale and not object_scale_uniform:
            result["export_info"]["deform_scale_warning"] = (
                "Auto deform scale only supports uniform armature object scale. "
                "Apply scale or use manual scale for nonuniform/scaled-shear rigs."
            )
        if analysis.target_scale_calibration:
            result["export_info"]["deform_target_scale_source_sample_count"] = int(
                analysis.target_scale_calibration.get("source_sample_count", 0)
            )
            result["export_info"]["deform_target_scale_target_sample_count"] = int(
                analysis.target_scale_calibration.get("target_sample_count", 0)
            )
        elif analysis.is_skinned_rig and auto_deform_scale and not target_bone_rest:
            result["export_info"]["deform_target_scale_warning"] = (
                "No target rig rest data was provided for this skinned export. "
                "Scale is using armature object scale only; live rig calibration "
                "was skipped."
            )
        elif target_bone_rest and auto_deform_scale:
            result["export_info"]["deform_target_scale_warning"] = (
                "Target rig rest data was provided, but no matching nonzero bone "
                "distances were found for scale calibration."
            )
        if analysis.is_skinned_rig:
            print(
                "Blender Addon: Deform export scale "
                f"mode={deform_scale_mode} "
                f"factor={float(analysis.scale_factor_cached):.6f} "
                f"object_scale={float(_uniform_object_scale(ao) or 1.0):.6f} "
                f"target_multiplier={float(calibrated_target_multiplier):.6f} "
                f"samples={int(result['export_info']['deform_target_scale_sample_count'])} "
                f"target_rest={'yes' if bool(target_bone_rest) else 'no'}"
            )
            scale_warning = (
                result["export_info"].get("deform_target_scale_warning")
                or result["export_info"].get("deform_scale_warning")
            )
            if scale_warning:
                print(f"Blender Addon: {scale_warning}")
    except Exception:
        pass

    # Ensure we always return a valid result, even for empty/static animations.
    if not result.get("kfs"):
        ao_eval_for_frame = ao.evaluated_get(analysis.depsgraph)
        state = serialize_combined_animation_state(
            ao,
            ao_eval_for_frame,
            analysis.run_deform_path,
            analysis.is_skinned_rig,
            analysis.back_trans_cached,
            analysis.world_transform_cached,
            analysis.scale_factor_cached,
            excluded_deform_bones=analysis.excluded_face_bones,
        )
        poses = {
            bone_name: PoseEntry(list(cframe_data), "Linear", "Out")
            for bone_name, cframe_data in state.items()
        }
        face_state, _ = _serialize_face_control_state_for_frame(
            ao,
            analysis.face_export_context,
            float(ctx.scene.frame_start),
        )
        payload = KeyframePayload(time=0, poses=poses, face=face_state or None)
        result["kfs"] = [payload.to_payload()]
        result["t"] = 0

    return result
