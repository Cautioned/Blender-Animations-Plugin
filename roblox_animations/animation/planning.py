"""
Bake planning: pure frame/easing analysis for the hybrid bake path.

Given the armature's actions and f-curves, this module decides which frames
and bones need evaluation and how per-channel Blender interpolation reduces
to the per-pose Roblox easing model.  Nothing here mutates scene state.
"""

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.utils import get_action_fcurves, get_animation_data_action_slot

# Interpolation types with direct Roblox EasingStyle mappings. Every other
# interpolation is sampled densely so linear Roblox segments reproduce it.
_ROBLOX_MAPPED_INTERPOLATIONS = {
    "LINEAR",
    "CONSTANT",
    "CUBIC",
    "BOUNCE",
    "ELASTIC",
}

FRAME_KEY_PRECISION = 4


@dataclass
class BakePlan:
    """Which frames/bones to evaluate and how easing is represented."""

    frame_start: int
    frame_end: int
    fps: float
    frames: List[float]
    keyframe_times: Set[float]
    per_bone_interpolation: Dict[str, Dict[float, Tuple[Optional[str], Optional[str]]]]
    per_bone_keyframes: Dict[str, Set[float]]
    bone_constant_keyframes: Dict[str, Set[float]]
    bone_non_constant_keyframes: Dict[str, Set[float]]
    dense_interpolation_segments: Dict[str, Set[Tuple[int, int]]]
    mixed_interpolation_segments: Dict[str, Set[Tuple[float, float]]]
    constraint_target_easing: Dict[str, Dict[float, Tuple[Optional[str], Optional[str]]]]
    cyclic_bones: Set[str]
    force_cyclic_full_bake: bool


def lookup_interp_for_frame(
    interp_map: Optional[Dict[float, Tuple[Optional[str], Optional[str]]]],
    frame: float,
) -> Tuple[Optional[str], Optional[str]]:
    """Return the interpolation/easing in effect at a frame (most recent key)."""
    if not interp_map:
        return None, None
    cached = interp_map.get(frame)
    if cached:
        return cached
    most_recent_frame = None
    for kf_frame in interp_map.keys():
        if kf_frame < frame and (most_recent_frame is None or kf_frame > most_recent_frame):
            most_recent_frame = kf_frame
    if most_recent_frame is not None:
        return interp_map[most_recent_frame]
    return None, None


def frame_in_set(frames: Optional[Set[float]], frame: float, eps: float = 1e-5) -> bool:
    if not frames:
        return False
    if frame in frames:
        return True
    for f in frames:
        if abs(f - frame) <= eps:
            return True
    return False


def _norm_frame(v: float) -> float:
    return round(float(v), FRAME_KEY_PRECISION)


def _uses_cyclic(fc) -> bool:
    try:
        return any(mod.type == "CYCLES" for mod in getattr(fc, "modifiers", []))
    except Exception:
        return False


def _cyclic_curve_requires_dense(fc) -> bool:
    """Return True when a cyclic curve should not use sparse export.

    Policy:
    - Roblox-supported interpolation styles stay sparse.
    - Unsupported styles fall back to dense baking.
    - Certain cycle modifier modes also force dense baking.
    """
    try:
        for kp in getattr(fc, "keyframe_points", []):
            if kp.interpolation not in _ROBLOX_MAPPED_INTERPOLATIONS:
                return True

        for mod in getattr(fc, "modifiers", []):
            if mod.type != "CYCLES":
                continue
            mode_before = getattr(mod, "mode_before", "REPEAT")
            mode_after = getattr(mod, "mode_after", "REPEAT")
            # Mirror/offset cycle modes are less reliable with sparse-only
            # replication.
            risky_modes = {"MIRROR", "REPEAT_OFFSET"}
            if mode_before in risky_modes or mode_after in risky_modes:
                return True
    except Exception:
        # If inspection fails, be conservative.
        return True
    return False


def build_bake_plan(
    ao: Any,
    constrained_bones: Set[str],
    action: Any,
    action_slots: Dict[Any, Any],
    all_fcurves: List[Any],
    keyframe_times: Set[float],
    frame_start: int,
    frame_end: int,
    fps: float,
    full_range: bool,
    has_constraints_local: bool,
    scene_frame_step: int,
) -> BakePlan:
    """Decide which frames/bones to evaluate and how easing is represented.

    Mutates ``keyframe_times`` by adding discovered explicit key times.
    """
    # Pre-compile regex for performance.
    bone_name_pattern = re.compile(r'pose\.bones\["(.+?)"\]')

    dense_interpolation_segments: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)

    # Interpolations with direct Roblox easing style mappings in this exporter.
    # Any interpolation outside that set is treated as unsupported and
    # will be densely baked between keys for fidelity.
    for fcurve in all_fcurves:
        # Determine bone name for this fcurve (if any).
        bone_name_for_curve = None
        if fcurve.data_path.startswith("pose.bones"):
            m = bone_name_pattern.search(fcurve.data_path)
            if m:
                bone_name_for_curve = m.group(1)
        # Use an indexed loop to check interpolation between keyframes.
        for i, kp in enumerate(fcurve.keyframe_points):
            frame = _norm_frame(kp.co.x)
            if frame_start <= frame <= frame_end:
                keyframe_times.add(frame)

            # The evaluated samples use Linear between adjacent frames, which
            # preserves BEZIER, SINE, QUAD, EXPO, and other unsupported curves.
            if (
                kp.interpolation not in _ROBLOX_MAPPED_INTERPOLATIONS
                and i + 1 < len(fcurve.keyframe_points)
            ):
                next_kp = fcurve.keyframe_points[i + 1]
                start_bezier_frame = int(kp.co.x + 0.5)
                end_bezier_frame = int(next_kp.co.x + 0.5)

                # Only densify if the segment actually curves.
                if end_bezier_frame - start_bezier_frame > 1:
                    if bone_name_for_curve:
                        dense_interpolation_segments[bone_name_for_curve].add(
                            (
                                min(start_bezier_frame, end_bezier_frame),
                                max(start_bezier_frame, end_bezier_frame),
                            )
                        )

    # Map of {bone_name: {frame: (interpolation, easing)}} built from action fcurves.
    per_bone_interpolation: Dict[
        str, Dict[float, Tuple[Optional[str], Optional[str]]]
    ] = {}
    # Map of {bone_name: {frame}} for explicit keyframes only.
    per_bone_keyframes: Dict[str, Set[float]] = {}
    # Track per-bone interpolation classes by frame so constant fallbacks
    # are local to that bone (not inherited from unrelated controller keys).
    bone_constant_keyframes: Dict[str, Set[float]] = {}
    bone_non_constant_keyframes: Dict[str, Set[float]] = {}
    if action:
        for fcurve in all_fcurves:
            if not fcurve.data_path.startswith("pose.bones"):
                continue

            match = bone_name_pattern.search(fcurve.data_path)
            if not match:
                continue

            bone_name_for_curve = match.group(1)
            frame_map = per_bone_interpolation.setdefault(bone_name_for_curve, {})
            keyframe_set = per_bone_keyframes.setdefault(bone_name_for_curve, set())
            const_set = bone_constant_keyframes.setdefault(bone_name_for_curve, set())
            nonconst_set = bone_non_constant_keyframes.setdefault(
                bone_name_for_curve, set()
            )

            for keyframe_point in fcurve.keyframe_points:
                frame_idx = _norm_frame(keyframe_point.co.x)

                # Track keyframes within range for sparse emission.
                if frame_start <= frame_idx <= frame_end:
                    keyframe_set.add(frame_idx)
                    if keyframe_point.interpolation == "CONSTANT":
                        const_set.add(frame_idx)
                    else:
                        nonconst_set.add(frame_idx)

                # But capture interpolation data even for keys just outside range
                # so shifted keys still get correct easing.
                if frame_idx < frame_start - 1 or frame_idx > frame_end + 1:
                    continue

                existing = frame_map.get(frame_idx)
                if existing is None:
                    frame_map[frame_idx] = (
                        keyframe_point.interpolation,
                        keyframe_point.easing,
                    )
                else:
                    # Roblox uses one easing style per pose/bone keyframe.
                    # If channels disagree at the same frame, prefer CONSTANT
                    # so intentional hold channels do not get softened.
                    existing_interp, existing_easing = existing
                    new_interp = keyframe_point.interpolation
                    if existing_interp == "CONSTANT" or new_interp == "CONSTANT":
                        if existing_interp != "CONSTANT":
                            frame_map[frame_idx] = (new_interp, keyframe_point.easing)

    # Propagate CONSTANT interpolation across segments so held channels stay
    # constant between keys (important for unparented/world-space/controller rigs).
    for fcurve in all_fcurves:
        if not fcurve.data_path.startswith("pose.bones"):
            continue

        match = bone_name_pattern.search(fcurve.data_path)
        if not match:
            continue

        bone_name_for_curve = match.group(1)
        frame_map = per_bone_interpolation.setdefault(
            bone_name_for_curve, {}
        )

        keypoints = list(fcurve.keyframe_points)
        if len(keypoints) < 2:
            continue

        for i, kp in enumerate(keypoints[:-1]):
            if kp.interpolation != "CONSTANT":
                continue

            start_frame = int(round(kp.co.x))
            end_frame = int(round(keypoints[i + 1].co.x))
            if end_frame <= start_frame + 1:
                continue

            seg_start = max(start_frame + 1, frame_start)
            seg_end = min(end_frame, frame_end)
            nonconst_set = bone_non_constant_keyframes.get(bone_name_for_curve, set())
            for frame_idx in range(seg_start, seg_end):
                existing = frame_map.get(frame_idx)
                if existing is not None and frame_idx in nonconst_set:
                    continue
                if existing is None or existing[0] != "CONSTANT":
                    frame_map[frame_idx] = ("CONSTANT", kp.easing)

    # Roblox stores one easing style on each Pose, whereas Blender stores
    # interpolation per f-curve.  Do not let one CONSTANT component freeze
    # the other components of a bone: mixed segments must be baked as
    # dense linear poses.  A single Pose cannot represent, for example,
    # stepped X and linear Y exactly without that bake.
    mixed_interpolation_segments: Dict[str, Set[Tuple[float, float]]] = defaultdict(set)
    transform_suffixes = (
        ".location", ".rotation_quaternion", ".rotation_euler",
        ".rotation_axis_angle", ".scale",
    )
    bone_transform_curves: Dict[str, List[Any]] = defaultdict(list)
    for fcurve in all_fcurves:
        if not fcurve.data_path.startswith("pose.bones"):
            continue
        if not fcurve.data_path.endswith(transform_suffixes):
            continue
        match = bone_name_pattern.search(fcurve.data_path)
        if match and len(fcurve.keyframe_points) >= 2:
            bone_transform_curves[match.group(1)].append(fcurve)

    for bone_name_for_curve, curves in bone_transform_curves.items():
        boundaries = sorted({
            float(kp.co.x)
            for fcurve in curves
            for kp in fcurve.keyframe_points
        })
        for segment_start, segment_end in zip(boundaries, boundaries[1:]):
            styles = set()
            for fcurve in curves:
                outgoing = None
                for kp in fcurve.keyframe_points:
                    if kp.co.x <= segment_start + 1e-6:
                        outgoing = kp
                    else:
                        break
                if outgoing is not None:
                    styles.add(outgoing.interpolation)
            if len(styles) > 1:
                mixed_interpolation_segments[bone_name_for_curve].add(
                    (segment_start, segment_end)
                )

    # Pre-compute constraint target easing data to avoid nested loops per frame.
    constraint_target_easing: Dict[str, Dict[float, Tuple[Optional[str], Optional[str]]]] = {}
    for bone in ao.pose.bones:
        if bone.name in constrained_bones:
            for constraint in bone.constraints:
                # Handle same-armature COPY constraints by inheriting easing
                # from the source bone's fcurves in the current action.
                if (
                    constraint.type in {"COPY_TRANSFORMS", "COPY_LOCATION", "COPY_ROTATION", "COPY_SCALE"}
                    and getattr(constraint, "target", None) == ao
                    and getattr(constraint, "subtarget", None)
                    and action
                ):
                    source_bone = constraint.subtarget
                    source_interp_map = per_bone_interpolation.get(source_bone)
                    if source_interp_map:
                        frame_map = constraint_target_easing.setdefault(bone.name, {})
                        for frame_idx, interp_pair in source_interp_map.items():
                            if frame_start <= frame_idx <= frame_end and frame_idx not in frame_map:
                                frame_map[frame_idx] = interp_pair

                # Handle IK constraints where target is the same armature.
                if constraint.type == "IK" and constraint.chain_count > 0:
                    target_bones = [bone.name]
                    current_bone = bone
                    for _ in range(constraint.chain_count):
                        if current_bone.parent:
                            current_bone = current_bone.parent
                            target_bones.append(current_bone.name)
                        else:
                            break

                    # Look for IK target bone's keyframes in the current action.
                    subtarget = getattr(constraint, "subtarget", None)
                    if subtarget and action:
                        for fcurve in all_fcurves:
                            if not fcurve.data_path.startswith("pose.bones"):
                                continue
                            match = bone_name_pattern.search(fcurve.data_path)
                            if match and match.group(1) == subtarget:
                                for kp in fcurve.keyframe_points:
                                    frame_idx = _norm_frame(kp.co.x)
                                    if frame_start <= frame_idx <= frame_end:
                                        for target_bone_name in target_bones:
                                            frame_map = constraint_target_easing.setdefault(
                                                target_bone_name, {}
                                            )
                                            if frame_idx not in frame_map:
                                                frame_map[frame_idx] = (
                                                    kp.interpolation,
                                                    kp.easing,
                                                )

                # Handle other constraints with external targets.
                elif (
                    hasattr(constraint, "target")
                    and constraint.target
                    and constraint.target != ao
                    and constraint.target.animation_data
                    and constraint.target.animation_data.action
                ):
                    target_action = constraint.target.animation_data.action
                    target_fcurves = get_action_fcurves(
                        target_action,
                        slot=get_animation_data_action_slot(
                            constraint.target.animation_data,
                            action=target_action,
                        ),
                    )
                    target_bones = [bone.name]
                    for fcurve in target_fcurves:
                        if (
                            fcurve.data_path.startswith("pose.bones")
                            and hasattr(constraint, "subtarget")
                            and constraint.subtarget
                        ):
                            match = bone_name_pattern.search(fcurve.data_path)
                            if match and match.group(1) == constraint.subtarget:
                                for kp in fcurve.keyframe_points:
                                    frame_idx = _norm_frame(kp.co.x)
                                    if frame_start <= frame_idx <= frame_end:
                                        for target_bone_name in target_bones:
                                            frame_map = constraint_target_easing.setdefault(
                                                target_bone_name, {}
                                            )
                                            if frame_idx not in frame_map:
                                                frame_map[frame_idx] = (
                                                    kp.interpolation,
                                                    kp.easing,
                                                )

    # Propagate constraint_target_easing through COPY constraints:
    # if bone A copies from bone B, and B has easing info, A should inherit it.
    copy_constraint_types = {"COPY_TRANSFORMS", "COPY_LOCATION", "COPY_ROTATION", "COPY_SCALE"}

    # Build a map of copy relationships: bone -> source bone.
    copy_source_map: Dict[str, str] = {}
    for bone in ao.pose.bones:
        for constraint in bone.constraints:
            if constraint.type in copy_constraint_types:
                source_bone = getattr(constraint, "subtarget", None)
                if source_bone:
                    copy_source_map[bone.name] = source_bone
                    break  # Take the first copy constraint.

    # Now propagate easing through the chain iteratively.
    changed = True
    iterations = 0
    max_iterations = 20  # Prevent infinite loops.
    while changed and iterations < max_iterations:
        changed = False
        iterations += 1
        for bone_name, source_bone in copy_source_map.items():
            if bone_name in constraint_target_easing:
                continue  # Already has easing info.
            if source_bone in constraint_target_easing:
                # Inherit easing from source bone.
                constraint_target_easing[bone_name] = dict(constraint_target_easing[source_bone])
                changed = True

    fcurves_with_cycles = [fc for fc in all_fcurves if _uses_cyclic(fc)]
    cyclic_bones: Set[str] = set()
    if fcurves_with_cycles:
        for fc in fcurves_with_cycles:
            if not fc.data_path.startswith("pose.bones"):
                continue
            match = bone_name_pattern.search(fc.data_path)
            if match:
                cyclic_bones.add(match.group(1))

    # Prefer sparse replication for cyclic curves, but fall back to dense
    # sampling for risky cyclic setups where sparse evaluation can drift.
    force_cyclic_full_bake = any(
        _cyclic_curve_requires_dense(fc) for fc in fcurves_with_cycles
    )

    if has_constraints_local:
        all_frames_to_bake = list(range(frame_start, frame_end + 1))
    elif force_cyclic_full_bake:
        all_frames_to_bake = range(frame_start, frame_end + 1)
    elif fcurves_with_cycles:
        # For cyclic animations, replicate the base cycle sparsely across the range.
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

            # Determine base cycle interval from the action if available.
            base_start = cycle_sorted[0]
            base_end = cycle_sorted[-1]
            if action and action.frame_range:
                action_start, action_end = action.frame_range
                base_start = int(math.floor(action_start))
                base_end = int(math.ceil(action_end - 1e-6))

            frame_step = max(scene_frame_step, 1)
            cycle_len = base_end - base_start
            if cycle_len <= 0:
                cycle_len = frame_step

            # Collect base cycle frames within one cycle interval.
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
                # Fallback: sample densely across the base cycle using frame_step.
                base_cycle_frames = list(
                    range(base_start, base_end + 1, frame_step)
                )

            if base_end not in base_cycle_frames:
                base_cycle_frames.append(base_end)
            if base_start not in base_cycle_frames:
                base_cycle_frames.insert(0, base_start)

            # Include previous cycle samples for reference but do not bake them directly.
            base_cycle_with_prev = sorted(
                set(
                    [frame for frame in base_cycle_frames]
                    + [frame - cycle_len for frame in base_cycle_frames]
                )
            )

            # Replicate backwards to cover frames before the base cycle.
            if cycle_len > 0:
                offset = math.floor((frame_start - base_end) / cycle_len)
                while base_end + offset * cycle_len >= frame_start:
                    for base_frame in base_cycle_with_prev:
                        new_frame = base_frame + offset * cycle_len
                        if frame_start <= new_frame <= frame_end:
                            extended_frames.add(new_frame)
                    offset -= 1

            # Replicate forward to cover entire range through frame_end.
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

        # Expand per_bone_keyframes for each cyclic bone so the sparse emission
        # logic treats replicated frames as explicit keyframes.
        # Also replicate per_bone_interpolation so easing data is available.
        for cbone in cyclic_bones:
            kf_set = per_bone_keyframes.setdefault(cbone, set())
            interp_map = per_bone_interpolation.get(cbone, {})

            # Collect the original base keyframes and their interpolation data
            # for this bone from its cyclic fcurves.
            bone_base_keys: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
            for fc in fcurves_with_cycles:
                if not fc.data_path.startswith("pose.bones"):
                    continue
                m = bone_name_pattern.search(fc.data_path)
                if not m or m.group(1) != cbone:
                    continue
                for kp in fc.keyframe_points:
                    kf = int(round(kp.co.x))
                    if kf not in bone_base_keys:
                        bone_base_keys[kf] = (kp.interpolation, kp.easing)

            # For each extended frame, check if it maps to a base keyframe
            # offset by a multiple of cycle_len.  Boundary frames are
            # included so they receive correct interpolation/easing data
            # (otherwise cyclic bones at frame_start/frame_end fall back
            # to Linear default, causing identity glitches).
            if cycle_len > 0 and bone_base_keys:
                for ef in extended_frames:
                    for base_kf, base_interp in bone_base_keys.items():
                        diff = ef - base_kf
                        if diff != 0 and diff % cycle_len == 0:
                            kf_set.add(ef)
                            if ef not in interp_map:
                                interp_map_full = per_bone_interpolation.setdefault(cbone, {})
                                interp_map_full[ef] = base_interp
                            break

            # Also add the original base keyframes that fall within range.
            for base_kf in bone_base_keys:
                if frame_start <= base_kf <= frame_end:
                    kf_set.add(base_kf)

        all_frames_to_bake = sorted(extended_frames)
        keyframe_times.update(extended_frames)
    elif full_range:
        # Use range object directly to avoid per-frame list allocation.
        all_frames_to_bake = range(frame_start, frame_end + 1)
    else:
        all_frames_to_bake = sorted(keyframe_times)

    # Final safety check: ensure all frames are within valid range.
    # Also preserve subframe key times to avoid collapsing tightly spaced keys.
    if isinstance(all_frames_to_bake, range):
        base_frames = [_norm_frame(f) for f in all_frames_to_bake]
    else:
        base_frames = [_norm_frame(f) for f in all_frames_to_bake if frame_start <= f <= frame_end]

    subframe_keys = [
        _norm_frame(f)
        for f in keyframe_times
        if frame_start <= f <= frame_end and abs(float(f) - round(float(f))) > 1e-8
    ]
    # Collect per-bone dense frames from bezier and mixed-interpolation
    # segments so only bones that need dense sampling are evaluated there.
    dense_interpolation_frames = set()
    for bone_segs in dense_interpolation_segments.values():
        for seg_start, seg_end in bone_segs:
            dense_interpolation_frames.update(
                frame
                for frame in range(seg_start + 1, seg_end)
                if frame_start <= frame <= frame_end
            )
    mixed_dense_frames = set()
    for bone_segs in mixed_interpolation_segments.values():
        for seg_start, seg_end in bone_segs:
            mixed_dense_frames.update(
                frame
                for frame in range(int(math.floor(seg_start)) + 1, int(math.ceil(seg_end)))
                if frame_start <= frame <= frame_end
            )
    if subframe_keys or dense_interpolation_frames or mixed_dense_frames:
        frames = sorted(
            frame for frame in (
                set(base_frames).union(subframe_keys).union(dense_interpolation_frames).union(mixed_dense_frames)
            )
            if frame_start <= frame <= frame_end
        )
    else:
        frames = base_frames

    return BakePlan(
        frame_start=frame_start,
        frame_end=frame_end,
        fps=fps,
        frames=frames,
        keyframe_times=keyframe_times,
        per_bone_interpolation=per_bone_interpolation,
        per_bone_keyframes=per_bone_keyframes,
        bone_constant_keyframes=bone_constant_keyframes,
        bone_non_constant_keyframes=bone_non_constant_keyframes,
        dense_interpolation_segments=dense_interpolation_segments,
        mixed_interpolation_segments=mixed_interpolation_segments,
        constraint_target_easing=constraint_target_easing,
        cyclic_bones=cyclic_bones,
        force_cyclic_full_bake=force_cyclic_full_bake,
    )
