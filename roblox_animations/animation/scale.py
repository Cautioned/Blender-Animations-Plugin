"""
Deform-rig scale calibration and coordinate normalization helpers.

Kept separate from sampling and planning so the scale math is inspectable
and testable without evaluating any armature.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

import bpy
from mathutils import Matrix

from ..core.constants import get_transform_to_blender
from .face_controls import is_face_control_bone

_SCALE_EPSILON = 1e-6


def _object_scale_components(obj: "bpy.types.Object") -> Optional[Tuple[float, float, float]]:
    if not obj:
        return None

    scale = obj.matrix_world.to_scale()
    values = (abs(float(scale.x)), abs(float(scale.y)), abs(float(scale.z)))
    if any(value <= _SCALE_EPSILON for value in values):
        return None

    return values


def _scale_values_are_uniform(values: Optional[Tuple[float, float, float]]) -> bool:
    if not values:
        return False

    average = sum(values) / 3.0
    tolerance = max(_SCALE_EPSILON, average * 1e-5)
    return not any(abs(value - average) > tolerance for value in values)


def _uniform_object_scale(obj: "bpy.types.Object") -> Optional[float]:
    values = _object_scale_components(obj)
    if not _scale_values_are_uniform(values):
        return None

    return sum(values) / 3.0


def resolve_scene_unit_scale_factor(
    settings: Optional[Any] = None,
    scene: Optional[Any] = None,
) -> float:
    if scene is None:
        scene = getattr(settings, "id_data", None)
    if scene is None:
        scene = getattr(bpy.context, "scene", None)

    unit_settings = getattr(scene, "unit_settings", None)
    scene_unit_scale = getattr(unit_settings, "scale_length", 1.0)
    try:
        scene_unit_scale = float(scene_unit_scale)
    except (TypeError, ValueError):
        scene_unit_scale = 1.0

    if abs(scene_unit_scale) <= _SCALE_EPSILON:
        return 1.0
    return abs(scene_unit_scale)


def resolve_manual_deform_rig_scale_factor(
    settings: Optional[Any] = None,
    scene: Optional[Any] = None,
) -> float:
    configured_scale = getattr(settings, "rbx_deform_rig_scale", None)
    if configured_scale is None:
        return resolve_scene_unit_scale_factor(settings, scene)

    try:
        configured_scale = float(configured_scale)
    except (TypeError, ValueError):
        configured_scale = 0.0

    if abs(configured_scale) <= _SCALE_EPSILON:
        return resolve_scene_unit_scale_factor(settings, scene)
    return abs(configured_scale)


def resolve_deform_rig_scale_factor(
    ao: "bpy.types.Object",
    settings: Optional[Any] = None,
    target_scale_multiplier: Optional[float] = None,
) -> float:
    auto_scale = getattr(settings, "rbx_auto_deform_scale", True)
    if auto_scale:
        target_calibration = 1.0
        if target_scale_multiplier is not None:
            try:
                target_scale_multiplier = float(target_scale_multiplier)
            except (TypeError, ValueError):
                target_scale_multiplier = 1.0
            if target_scale_multiplier > _SCALE_EPSILON:
                target_calibration = target_scale_multiplier
        object_scale = _uniform_object_scale(ao) or 1.0
        return 1.0 / (object_scale * target_calibration)

    return resolve_manual_deform_rig_scale_factor(settings)


def resolve_deform_translation_scale_factor(
    ao: "bpy.types.Object",
    settings: Optional[Any] = None,
    target_scale_multiplier: Optional[float] = None,
    scale_factor: Optional[float] = None,
) -> float:
    """Return the translation normalization factor for deform export.

    Deform serialization works from evaluated/world-space bone matrices, so
    object scale is already baked into translation deltas. Auto scale should
    therefore normalize only by target calibration, not by object scale a second
    time.
    """
    auto_scale = getattr(settings, "rbx_auto_deform_scale", True)
    if scale_factor is None:
        scale_factor = resolve_deform_rig_scale_factor(
            ao,
            settings,
            target_scale_multiplier=target_scale_multiplier,
        )

    try:
        scale_factor = float(scale_factor)
    except (TypeError, ValueError):
        scale_factor = 1.0

    if not auto_scale:
        return scale_factor if abs(scale_factor) > _SCALE_EPSILON else 1.0

    object_scale = _uniform_object_scale(ao) or 1.0
    translation_scale_factor = scale_factor * object_scale
    if abs(translation_scale_factor) <= _SCALE_EPSILON:
        return 1.0
    return translation_scale_factor


def extract_deform_rest_scale_samples(
    ao: "bpy.types.Object",
) -> Dict[str, Dict[str, Any]]:
    samples: Dict[str, Dict[str, Any]] = {}
    if not ao or ao.type != "ARMATURE" or not getattr(ao, "pose", None):
        return samples

    back_trans = get_transform_to_blender().inverted()
    world_transform = back_trans @ ao.matrix_world
    rest_cache: Dict[str, Matrix] = {}
    sorted_bones = sorted(ao.pose.bones, key=lambda bone: len(bone.parent_recursive))

    for pose_bone in sorted_bones:
        if is_face_control_bone(pose_bone):
            continue

        rest_matrix = _orthonormalized_transform(
            world_transform @ pose_bone.bone.matrix_local
        )
        rest_cache[pose_bone.name] = rest_matrix

        parent_name = pose_bone.parent.name if pose_bone.parent else None
        parent_rest = rest_cache.get(parent_name) if parent_name else None
        if parent_rest is not None:
            try:
                local_rest = parent_rest.inverted() @ rest_matrix
            except ValueError:
                local_rest = rest_matrix
        else:
            local_rest = rest_matrix

        distance = float(local_rest.to_translation().length)
        if distance <= _SCALE_EPSILON:
            continue

        samples[pose_bone.name] = {
            "parent": parent_name,
            "distance": distance,
        }

    return samples


def _target_rest_bones_from_payload(
    target_bone_rest: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    if not isinstance(target_bone_rest, dict):
        return {}

    payload_bones = target_bone_rest.get("bones", target_bone_rest)
    bones: Dict[str, Dict[str, Any]] = {}

    if isinstance(payload_bones, dict):
        for bone_name, entry in payload_bones.items():
            if not isinstance(bone_name, str):
                continue
            if isinstance(entry, dict):
                bones[bone_name] = entry
            elif isinstance(entry, (int, float)):
                bones[bone_name] = {"distance": entry}
    elif isinstance(payload_bones, list):
        for entry in payload_bones:
            if not isinstance(entry, dict):
                continue
            bone_name = entry.get("name") or entry.get("bone")
            if isinstance(bone_name, str):
                bones[bone_name] = entry

    return bones


def calculate_deform_target_scale_calibration(
    ao: "bpy.types.Object",
    target_bone_rest: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    target_bones = _target_rest_bones_from_payload(target_bone_rest)
    if not target_bones:
        return None

    source_samples = extract_deform_rest_scale_samples(ao)
    if not source_samples:
        return None

    ratios: List[float] = []
    matches: List[Dict[str, Any]] = []

    for bone_name, target_entry in target_bones.items():
        source_entry = source_samples.get(bone_name)
        if not source_entry:
            continue

        try:
            target_distance = float(
                target_entry.get("distance", target_entry.get("localDistance", 0.0))
            )
        except (TypeError, ValueError):
            continue

        source_distance = float(source_entry.get("distance") or 0.0)
        if target_distance <= _SCALE_EPSILON or source_distance <= _SCALE_EPSILON:
            continue

        target_parent = target_entry.get("parent")
        source_parent = source_entry.get("parent")
        if (
            isinstance(target_parent, str)
            and target_parent in source_samples
            and source_parent
            and target_parent != source_parent
        ):
            continue

        ratio = target_distance / source_distance
        if ratio <= _SCALE_EPSILON or not math.isfinite(ratio):
            continue

        ratios.append(ratio)
        matches.append(
            {
                "bone": bone_name,
                "source_distance": source_distance,
                "target_distance": target_distance,
                "ratio": ratio,
            }
        )

    if not ratios:
        return None

    sorted_ratios = sorted(ratios)
    middle = len(sorted_ratios) // 2
    if len(sorted_ratios) % 2:
        multiplier = sorted_ratios[middle]
    else:
        multiplier = (sorted_ratios[middle - 1] + sorted_ratios[middle]) / 2.0

    return {
        "multiplier": float(multiplier),
        "sample_count": len(ratios),
        "source_sample_count": len(source_samples),
        "target_sample_count": len(target_bones),
        "samples": matches[:12],
    }


def _orthonormalized_transform(mat: Matrix) -> Matrix:
    try:
        loc, rot, _scale = mat.decompose()
    except Exception:
        loc = mat.to_translation()
        rot = mat.to_quaternion()
    normalized = rot.to_matrix().to_4x4()
    normalized.translation = loc
    return normalized
