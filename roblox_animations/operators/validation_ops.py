"""
validation operators and viewport overlay for roblox animation validation.

performs comprehensive validation checks including:
- per-frame world displacement of each limb (bone) against max studs/frame threshold
- animation duration limits (strictly less than 10 seconds)
- root displacement from its initial position
- body-part height and distance from HumanoidRootPart
- standard R15 hierarchy checks
- draws violation segments and warnings in the 3d viewport
"""

import bpy
import json
from bpy.types import Operator
from mathutils import Matrix, Vector
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..animation.serialization import (
    is_deform_bone_rig,
    resolve_manual_deform_rig_scale_factor,
    resolve_deform_rig_scale_factor,
)
from ..core.utils import (
    find_armatures_driving,
    find_constraint_driven_armature,
    get_scene_fps,
    get_object_by_name,
)
import math


# global state for the draw overlay
_violation_draw_handler = None  # lines
_violation_label_draw_handler = None  # labels
_keyframe_points_draw_handler = None  # keyframe markers
_floor_limit_draw_handler = None  # floor limit plane
_violation_segments: List[
    Tuple[Vector, Vector, int, str, bool, bool, float]
] = []  # (start, end, frame, bone_name, key_prev, key_curr, studs)
_bone_color_cache: Dict[str, Tuple[float, float, float, float]] = {}
_armature_name_for_cache: str = ""
_keyframe_points: List[Tuple[Vector, str, int]] = []  # (location, bone_name, frame)
_below_root_violations: List[Tuple[Vector, Vector, str]] = []  # (bone_pos, floor_pos, bone_name)
_floor_limit_z: Optional[float] = None  # world Z of floor limit
_floor_limit_cx: float = 0.0  # world XY center of floor limit grid
_floor_limit_cy: float = 0.0
_validation_display_armature_name: str = ""  # armature the overlay maps onto
_validation_display_units_per_stud: float = 1.0  # world units per stud of that armature

# Values below match the live Roblox UGC CurveAnimation validator
# (ValidateCurveAnimation.lua + FFlag tracker, 2026-08-22). The user-facing
# motion benchmark remains configurable.
#   UGCValidationMaxAnimationDeltas   = 1 (studs/frame at the 30 fps benchmark)
#   UGCValidationMaxAnimationBounds   = 5 (studs from HumanoidRootPart)
#   UGCValidateAnimationHeightTol     = -3.1 (studs below HumanoidRootPart)
#   UGCValidateMaxAnimationFPS        = 70 (tracker sampling rate)
#   UGCValidationMaxAnimationLength   = 10 (seconds; the limit is exclusive)
#   UGCValidateCurveAnimationMinLength = 0
#   UGCValidateCurveAnimRotationSpeed = false (rotation-speed check disabled)
#   UGCValidateMaxAnimationMovement   = 100 (positional separation effectively off)
ANIM_MAX_DURATION = 10.0  # seconds; the limit is exclusive
ANIM_FPS = 30.0  # default-frame benchmark used by the Roblox movement flag
ANIM_VALIDATION_FPS = 70.0  # Roblox's CurveAnimation sampler
ANIM_MAX_DELTA = 1.0  # studs per frame at ANIM_FPS
ANIM_ROOT_DISPLACEMENT_ADVISORY = 5.0  # retained local preflight heuristic
ANIM_MAX_BODY_DISTANCE = 5.0  # studs from HumanoidRootPart
ANIM_MAX_BELOW_ROOT = 3.1  # studs below HumanoidRootPart
_VALIDATION_SCALE_EPSILON = 1e-6


def _load_template_r15_fixture() -> List[Dict[str, Any]]:
    fixture_path = Path(__file__).with_name("template_r15_bones.json")
    with fixture_path.open(encoding="utf-8") as fixture_file:
        return json.load(fixture_file)["bones"]


def _load_template_r15_rest() -> Dict[str, Dict[str, Any]]:
    """Load the compact canonical R15 rest pose extracted from TemplateR15.rbxm."""
    positions: Dict[str, Vector] = {}
    parent_by_name: Dict[str, Optional[str]] = {}
    for bone in _TEMPLATE_R15_BONES:
        name = "".join(ch.lower() for ch in bone["name"] if ch.isalnum())
        positions[name] = Vector(bone["position"])
        parent = bone.get("parent")
        parent_by_name[name] = (
            "".join(ch.lower() for ch in parent if ch.isalnum()) if parent else None
        )

    rest: Dict[str, Dict[str, Any]] = {}
    for name, parent in parent_by_name.items():
        if parent is None or parent not in positions:
            continue
        rest[name] = {
            "parent": parent,
            "distance": float((positions[name] - positions[parent]).length),
        }
    return rest


# Generated from TemplateR15.rbxm. This is the same canonical R15 rest pose
# the local preflight uses for calibration, without shipping the 1.4 MB model.
_TEMPLATE_R15_BONES = _load_template_r15_fixture()
_INTERNAL_EMOTE_R15_REST = _load_template_r15_rest()

_INTERNAL_EMOTE_R15_FLOOR_BONES = (
    "lowertorso",
    "leftupperleg",
    "leftlowerleg",
    "leftfoot",
    "rightupperleg",
    "rightlowerleg",
    "rightfoot",
)
_MIN_CANONICAL_VALIDATION_BONES = 8


def _normalize_bone_name(name: str) -> str:
    return "".join(ch.lower() for ch in (name or "") if ch.isalnum())


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2 == 1:
        return sorted_values[middle]
    return 0.5 * (sorted_values[middle - 1] + sorted_values[middle])


def _resolve_validation_scale_from_rest_samples(
    source_samples: Dict[str, Dict[str, Any]],
    canonical_samples: Dict[str, Dict[str, Any]],
    preferred_bone_names: Optional[Tuple[str, ...]] = None,
) -> Optional[Tuple[float, int]]:
    ratios: List[float] = []
    if preferred_bone_names:
        canonical_items = [
            (bone_name, canonical_samples[bone_name])
            for bone_name in preferred_bone_names
            if bone_name in canonical_samples
        ]
    else:
        canonical_items = list(canonical_samples.items())

    for bone_name, canonical_entry in canonical_items:
        source_entry = source_samples.get(bone_name)
        if not source_entry:
            continue

        source_parent = source_entry.get("parent")
        canonical_parent = canonical_entry.get("parent")
        if canonical_parent and source_parent and canonical_parent != source_parent:
            continue

        try:
            source_distance = float(source_entry.get("distance") or 0.0)
            canonical_distance = float(canonical_entry.get("distance") or 0.0)
        except (TypeError, ValueError):
            continue

        if (
            source_distance <= _VALIDATION_SCALE_EPSILON
            or canonical_distance <= _VALIDATION_SCALE_EPSILON
        ):
            continue

        ratio = source_distance / canonical_distance
        if not math.isfinite(ratio) or ratio <= _VALIDATION_SCALE_EPSILON:
            continue

        ratios.append(ratio)

    median_ratio = _median(ratios)
    if median_ratio is None:
        return None

    return median_ratio, len(ratios)


def _collect_validation_rest_scale_samples(
    armature_obj: "bpy.types.Object",
) -> Dict[str, Dict[str, Any]]:
    samples: Dict[str, Dict[str, Any]] = {}
    if not armature_obj or armature_obj.type != "ARMATURE":
        return samples

    settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
    force_deform = getattr(settings, "force_deform_bone_serialization", False)
    is_deform = is_deform_bone_rig(armature_obj) or force_deform

    # Sample in armature-LOCAL space: pose translations that validation
    # measures are local (matrix_channel), so the unit ratio must match.
    # Including matrix_world here would fold the object scale into the
    # ratio and skew every studs measurement by that factor.
    rest_positions: Dict[str, Vector] = {}
    relevant_bones: Dict[str, "bpy.types.Bone"] = {}
    for bone in armature_obj.data.bones:
        if is_deform:
            if not bone.use_deform:
                continue
        elif not bool(bone.get("is_transformable", False)):
            continue

        relevant_bones[bone.name] = bone
        rest_positions[bone.name] = bone.head_local

    for bone_name, bone in relevant_bones.items():
        parent = bone.parent
        if parent is None or parent.name not in rest_positions:
            continue

        distance = float((rest_positions[bone_name] - rest_positions[parent.name]).length)
        if distance <= _VALIDATION_SCALE_EPSILON:
            continue

        samples[_normalize_bone_name(bone_name)] = {
            "parent": _normalize_bone_name(parent.name),
            "distance": distance,
        }

    return samples


def _fallback_validation_units_per_stud(
    armature_obj: "bpy.types.Object",
    settings: Optional[Any],
) -> Tuple[float, str, int]:
    force_deform = getattr(settings, "force_deform_bone_serialization", False)
    is_deform = is_deform_bone_rig(armature_obj) or force_deform
    if is_deform:
        auto_scale = getattr(settings, "rbx_auto_deform_scale", True)
        if auto_scale:
            scale_factor = float(
                resolve_deform_rig_scale_factor(armature_obj, settings) or 1.0
            )
            if abs(scale_factor) > _VALIDATION_SCALE_EPSILON:
                return 1.0 / abs(scale_factor), "deform_export_fallback", 0
        else:
            manual_scale = float(
                resolve_manual_deform_rig_scale_factor(settings) or 1.0
            )
            if abs(manual_scale) > _VALIDATION_SCALE_EPSILON:
                return abs(manual_scale), "manual", 0

    return 1.0, "scene_units", 0


def _resolve_validation_units_per_stud(
    armature_obj: "bpy.types.Object",
    settings: Optional[Any],
    preferred_bone_names: Optional[Tuple[str, ...]] = None,
) -> Tuple[float, str, int]:
    source_samples = _collect_validation_rest_scale_samples(armature_obj)
    resolved = _resolve_validation_scale_from_rest_samples(
        source_samples,
        _INTERNAL_EMOTE_R15_REST,
        preferred_bone_names=preferred_bone_names,
    )
    if resolved is not None:
        units_per_stud, sample_count = resolved
        return units_per_stud, "internal_r15", sample_count

    return _fallback_validation_units_per_stud(armature_obj, settings)


def _accumulate_floor_limit_z(
    current_floor_limit_z: Optional[float],
    candidate_floor_z: Optional[float],
) -> Optional[float]:
    if candidate_floor_z is None:
        return current_floor_limit_z
    if current_floor_limit_z is None:
        return candidate_floor_z
    return min(current_floor_limit_z, candidate_floor_z)


def _resolve_validation_root_bone_name(
    pose_bones: Any,
) -> Optional[str]:
    preferred_names = (
        "HumanoidRootPart",
        "humanoidrootpart",
        "LowerTorso",
        "lowertorso",
        "Torso",
        "torso",
        "UpperTorso",
        "uppertorso",
        "Root",
        "root",
    )

    for name in preferred_names:
        try:
            pbone = pose_bones.get(name)
        except AttributeError:
            pbone = None
        if pbone is not None:
            return pbone.name

    for pbone in pose_bones:
        if getattr(pbone, "parent", None) is None:
            return pbone.name

    return None


def _resolve_validation_body_bone_name_set(
    bone_names: List[str],
) -> Optional[Set[str]]:
    canonical_matches: Dict[str, str] = {}
    for bone_name in bone_names:
        normalized_name = _normalize_bone_name(bone_name)
        if normalized_name not in _INTERNAL_EMOTE_R15_REST:
            continue
        canonical_matches.setdefault(normalized_name, bone_name)

    if len(canonical_matches) < _MIN_CANONICAL_VALIDATION_BONES:
        return None

    return set(canonical_matches.values())


def _validate_standard_r15_rig(bones: Any) -> List[str]:
    """Return structural issues that prevent an animation from targeting standard R15."""
    by_normalized_name = {
        _normalize_bone_name(bone.name): bone for bone in bones
    }
    warnings: List[str] = []

    for bone_name, expected in _INTERNAL_EMOTE_R15_REST.items():
        bone = by_normalized_name.get(bone_name)
        if bone is None:
            warnings.append(f"Standard R15 bone '{bone_name}' is missing")
            continue

        parent = getattr(bone, "parent", None)
        parent_name = _normalize_bone_name(getattr(parent, "name", ""))
        if bone_name == "lowertorso":
            valid_parents = {"humanoidrootpart", "humanoidrootnode", "root"}
        else:
            valid_parents = {expected["parent"]}
        if parent_name not in valid_parents:
            expected_names = ", ".join(sorted(valid_parents))
            warnings.append(
                f"Standard R15 bone '{bone.name}' must be parented to {expected_names}"
            )

    return warnings


def _fixture_position_to_blender(position: List[float]) -> Vector:
    """Convert root-relative Roblox coordinates to Blender's Z-up space."""
    return Vector((position[0], -position[2], position[1]))


def _create_internal_r15_armature(
    context: "bpy.types.Context", source_armature: "bpy.types.Object"
) -> "bpy.types.Object":
    """Create an unscaled disposable R15 armature from the bundled fixture."""
    armature_data = bpy.data.armatures.new("RBX_UGC_InternalR15")
    armature_obj = bpy.data.objects.new("RBX_UGC_InternalR15", armature_data)
    context.collection.objects.link(armature_obj)
    armature_obj.hide_render = True
    armature_obj.hide_set(True)
    armature_obj.matrix_world = Matrix.LocRotScale(
        source_armature.matrix_world.translation,
        source_armature.matrix_world.to_quaternion(),
        (1.0, 1.0, 1.0),
    )

    previous_active = context.view_layer.objects.active
    previous_mode = previous_active.mode if previous_active else "OBJECT"
    previous_selected = list(context.selected_objects)
    try:
        for obj in previous_selected:
            obj.select_set(False)
        armature_obj.hide_set(False)
        armature_obj.select_set(True)
        context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode="EDIT")

        edit_bones: Dict[str, Any] = {}
        positions = {
            entry["name"]: _fixture_position_to_blender(entry["position"])
            for entry in _TEMPLATE_R15_BONES
        }
        for entry in _TEMPLATE_R15_BONES:
            bone = armature_data.edit_bones.new(entry["name"])
            bone.head = positions[entry["name"]]
            child_positions = [
                positions[child["name"]]
                for child in _TEMPLATE_R15_BONES
                if child.get("parent") == entry["name"]
            ]
            direction = (
                child_positions[0] - bone.head
                if child_positions
                else Vector((0.0, 0.0, 0.25))
            )
            if direction.length < _VALIDATION_SCALE_EPSILON:
                direction = Vector((0.0, 0.0, 0.25))
            bone.tail = bone.head + direction.normalized() * 0.25
            edit_bones[entry["name"]] = bone
        for entry in _TEMPLATE_R15_BONES:
            parent_name = entry.get("parent")
            if parent_name:
                edit_bones[entry["name"]].parent = edit_bones[parent_name]
        bpy.ops.object.mode_set(mode="OBJECT")
        for bone in armature_data.bones:
            bone.use_deform = True
    finally:
        if armature_obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        armature_obj.select_set(False)
        for obj in previous_selected:
            if obj and obj.name in bpy.data.objects:
                obj.select_set(True)
        context.view_layer.objects.active = previous_active
        if previous_active and previous_mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode=previous_mode)
            except RuntimeError:
                pass
        armature_obj.hide_set(True)
    return armature_obj


def _collect_validation_name_map(
    source_evaluated: "bpy.types.Object",
) -> Dict[str, str]:
    """Map canonical internal R15 bone names to the source rig's bone names."""
    source_by_name = {
        _normalize_bone_name(pbone.name): pbone.name
        for pbone in source_evaluated.pose.bones
    }
    name_map: Dict[str, str] = {}
    for entry in _TEMPLATE_R15_BONES:
        normalized = _normalize_bone_name(entry["name"])
        if normalized in _INTERNAL_EMOTE_R15_REST and normalized in source_by_name:
            name_map[entry["name"]] = source_by_name[normalized]
    return name_map


def _collect_rig_display_positions(
    source_evaluated: "bpy.types.Object",
    measure_armature: "bpy.types.Object",
    name_map: Dict[str, str],
) -> Tuple[Dict[str, Vector], Vector]:
    """Map internal template bone names back to the ACTUAL rig's world-space
    head positions for viewport drawing."""
    world_mat = source_evaluated.matrix_world
    pose_bones = source_evaluated.pose.bones
    display_positions = {
        internal_name: world_mat @ pose_bones[source_name].head
        for internal_name, source_name in name_map.items()
        if source_name in pose_bones
    }
    display_root = _get_root_world_pos(measure_armature, source_evaluated)
    return display_positions, display_root


def _build_validation_sample_times(
    anim_length_seconds: float, sample_delta: float
) -> List[float]:
    """Mirror the tracker's sampling grid: t = 0..length inclusive at a fixed
    delta (1/UGCValidateMaxAnimationFPS)."""
    sample_times: List[float] = []
    time_s = 0.0
    while time_s <= anim_length_seconds + _VALIDATION_SCALE_EPSILON:
        sample_times.append(time_s)
        time_s += sample_delta
    if not sample_times:
        sample_times = [0.0]
    return sample_times


def _resolve_validation_measure_rig(armature: "bpy.types.Object") -> "bpy.types.Object":
    """Return the armature validation should measure.

    A selection that is not a standard R15 rig but is wired to another
    armature via copy constraints (a control rig driving a true deform rig,
    in either direction) resolves to the driven standard-R15 rig."""
    if not armature or getattr(armature, "type", None) != "ARMATURE":
        return armature
    if not _validate_standard_r15_rig(armature.data.bones):
        return armature
    candidates = []
    driven_target, _ = find_constraint_driven_armature(armature)
    if driven_target is not None:
        candidates.append(driven_target)
    for driver in find_armatures_driving(armature):
        if driver not in candidates:
            candidates.append(driver)
    for candidate in candidates:
        if not _validate_standard_r15_rig(candidate.data.bones):
            return candidate
    return armature


def _destroy_internal_r15_armature(armature_obj: Optional["bpy.types.Object"]) -> None:
    if armature_obj is None:
        return
    armature_data = armature_obj.data
    bpy.data.objects.remove(armature_obj, do_unlink=True)
    if armature_data.users == 0:
        bpy.data.armatures.remove(armature_data)


def _get_bone_display_color(
    pbone: "bpy.types.PoseBone",
) -> Tuple[float, float, float, float]:
    group = getattr(pbone, "bone_group", None)
    if group is not None:
        colors = getattr(group, "colors", None)
        if colors is not None and hasattr(colors, "normal"):
            col = colors.normal
            try:
                # some versions return Color, convert to 4-tuple
                return (col[0], col[1], col[2], 1.0)
            except Exception:
                pass
    # fallback deterministic color from name
    import random

    rnd = random.Random(hash(pbone.name) & 0xFFFFFFFF)
    r, g, b = rnd.random(), rnd.random(), rnd.random()
    return (r * 0.8 + 0.2, g * 0.8 + 0.2, b * 0.8 + 0.2, 1.0)


def _draw_motionpath_violations():
    """viewport draw callback to render violation segments as red lines."""
    if not _violation_segments:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except Exception:
        return

    # shader fallback across blender versions
    shader = None
    for name in ("UNIFORM_COLOR", "3D_UNIFORM_COLOR", "FLAT_COLOR"):
        try:
            shader = gpu.shader.from_builtin(name)
            break
        except Exception:
            continue
    if shader is None:
        return

    gpu.state.blend_set("ALPHA")
    try:
        gpu.state.line_width_set(2.0)
    except Exception:
        pass

    # build or refresh bone color cache for active armature
    settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
    arm_name = _validation_display_armature_name or (settings.rbx_anim_armature if settings else None)
    global _armature_name_for_cache
    if arm_name != _armature_name_for_cache or not _bone_color_cache:
        _bone_color_cache.clear()
        arm = get_object_by_name(arm_name)
        if arm and arm.type == "ARMATURE":
            for pb in arm.pose.bones:
                _bone_color_cache[pb.name] = _get_bone_display_color(pb)
        _armature_name_for_cache = arm_name

    # batch by color to reduce shader binds
    by_color: Dict[Tuple[float, float, float, float], List[Vector]] = {}
    for start, end, _frame, bone_name, _kp, _kc, _studs in _violation_segments:
        color = _bone_color_cache.get(bone_name, (1.0, 0.0, 0.0, 1.0))
        coords = by_color.setdefault(color, [])
        coords.append(start)
        coords.append(end)

    for color, coords in by_color.items():
        if not coords:
            continue
        batch = batch_for_shader(shader, "LINES", {"pos": coords})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)

    gpu.state.blend_set("NONE")


def _draw_floor_limit():
    """Draw floor limit plane and vertical drop lines for below-root violations."""
    if not _below_root_violations and _floor_limit_z is None:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except Exception:
        return

    shader = None
    for name in ("UNIFORM_COLOR", "3D_UNIFORM_COLOR", "FLAT_COLOR"):
        try:
            shader = gpu.shader.from_builtin(name)
            break
        except Exception:
            continue
    if shader is None:
        return

    gpu.state.blend_set("ALPHA")
    try:
        gpu.state.line_width_set(2.0)
    except Exception:
        pass

    # Draw floor limit as dashed grid lines (orange/red)
    if _floor_limit_z is not None:
        floor_color = (1.0, 0.3, 0.1, 0.5)
        display_scale = max(abs(_validation_display_units_per_stud), _VALIDATION_SCALE_EPSILON)
        grid_size = 3.0 * display_scale
        cx = _floor_limit_cx
        cy = _floor_limit_cy
        grid_lines = []
        for i in range(-5, 6):
            offset = i * grid_size / 5
            grid_lines.append(Vector((cx - grid_size, cy + offset, _floor_limit_z)))
            grid_lines.append(Vector((cx + grid_size, cy + offset, _floor_limit_z)))
            grid_lines.append(Vector((cx + offset, cy - grid_size, _floor_limit_z)))
            grid_lines.append(Vector((cx + offset, cy + grid_size, _floor_limit_z)))

        batch = batch_for_shader(shader, "LINES", {"pos": grid_lines})
        shader.bind()
        shader.uniform_float("color", floor_color)
        batch.draw(shader)

    # Draw vertical drop lines from violating bones to floor (red)
    if _below_root_violations:
        drop_color = (1.0, 0.0, 0.0, 0.8)
        drop_lines = []
        for bone_pos, floor_pos, _bone_name in _below_root_violations:
            drop_lines.append(bone_pos)
            drop_lines.append(floor_pos)

        batch = batch_for_shader(shader, "LINES", {"pos": drop_lines})
        shader.bind()
        shader.uniform_float("color", drop_color)
        batch.draw(shader)

        # Draw X markers at violation points
        x_color = (1.0, 0.0, 0.0, 1.0)
        x_size = 0.1 * max(abs(_validation_display_units_per_stud), _VALIDATION_SCALE_EPSILON)
        x_lines = []
        for bone_pos, _floor_pos, _bone_name in _below_root_violations:
            x_lines.append(Vector((bone_pos.x - x_size, bone_pos.y - x_size, bone_pos.z)))
            x_lines.append(Vector((bone_pos.x + x_size, bone_pos.y + x_size, bone_pos.z)))
            x_lines.append(Vector((bone_pos.x - x_size, bone_pos.y + x_size, bone_pos.z)))
            x_lines.append(Vector((bone_pos.x + x_size, bone_pos.y - x_size, bone_pos.z)))

        batch = batch_for_shader(shader, "LINES", {"pos": x_lines})
        shader.bind()
        shader.uniform_float("color", x_color)
        batch.draw(shader)

    gpu.state.blend_set("NONE")


def _draw_motionpath_keyframes():
    """viewport draw callback to render keyframe markers along the path, similar to blender's motion path dots."""
    if not _keyframe_points:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except Exception:
        return

    shader = None
    for name in ("UNIFORM_COLOR", "3D_UNIFORM_COLOR", "FLAT_COLOR"):
        try:
            shader = gpu.shader.from_builtin(name)
            break
        except Exception:
            continue
    if shader is None:
        return

    gpu.state.blend_set("ALPHA")
    try:
        gpu.state.point_size_set(5.0)
    except Exception:
        pass

    # group points by bone to minimize color binds
    points_by_bone = {}
    for loc, bone_name, _frame in _keyframe_points:
        points_by_bone.setdefault(bone_name, []).append(loc)

    for bone_name, pts in points_by_bone.items():
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        arm_name = _validation_display_armature_name or (
            settings.rbx_anim_armature if settings else None
        )
        arm = get_object_by_name(arm_name)
        pbone = arm.pose.bones.get(bone_name) if arm else None
        color = (1.0, 1.0, 1.0, 1.0)
        if pbone is not None:
            bc = _get_bone_display_color(pbone)
            # slightly brighter for visibility
            color = (
                min(1.0, bc[0] + 0.25),
                min(1.0, bc[1] + 0.25),
                min(1.0, bc[2] + 0.25),
                1.0,
            )
        batch = batch_for_shader(shader, "POINTS", {"pos": pts})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)

    gpu.state.blend_set("NONE")


def _draw_motionpath_labels():
    """overlay callback to render frame labels near violation segments."""
    if not _violation_segments:
        return
    try:
        import blf
        from bpy_extras import view3d_utils
    except Exception:
        return

    region = bpy.context.region
    rv3d = bpy.context.region_data
    if not region or not rv3d:
        return

    font_id = 0
    dpi = 72
    try:
        blf.size(font_id, 12, dpi)
    except Exception:
        pass

    for start, end, frame, bone_name, _kp, _kc, studs in _violation_segments:
        mid = (start + end) * 0.5
        pos2d = view3d_utils.location_3d_to_region_2d(region, rv3d, mid)
        if not pos2d:
            continue
        text = f"{bone_name}  f:{frame}  {studs:.2f} st"
        # small offset to avoid drawing on top of the line
        x = pos2d.x + 4
        y = pos2d.y + 4
        # match label color to line (bone) color (use cache)
        label_col = _bone_color_cache.get(bone_name, (1.0, 0.0, 0.0, 1.0))
        try:
            blf.position(font_id, x, y, 0)
            blf.color(font_id, *label_col)
            blf.draw(font_id, text)
        except Exception:
            # older versions may not support blf.color
            blf.position(font_id, x, y, 0)
            blf.draw(font_id, text)

        # draw keyframe markers as bullets at endpoints
        # compute endpoints in 2d
        bc = _bone_color_cache.get(bone_name, (1.0, 0.0, 0.0, 1.0))
        bcol = (
            min(1.0, bc[0] + 0.2),
            min(1.0, bc[1] + 0.2),
            min(1.0, bc[2] + 0.2),
            1.0,
        )

        start2d = view3d_utils.location_3d_to_region_2d(region, rv3d, start)
        end2d = view3d_utils.location_3d_to_region_2d(region, rv3d, end)
        try:
            blf.size(font_id, 18, dpi)
        except Exception:
            pass
        # draw dim base markers at endpoints for visibility (skip if too many to keep fps)
        many_segments = len(_violation_segments) > 800
        if start2d and not many_segments:
            try:
                blf.color(font_id, 1.0, 1.0, 1.0, 0.6)
            except Exception:
                pass
            blf.position(font_id, start2d.x - 4, start2d.y - 4, 0)
            blf.draw(font_id, "■")
        if end2d and not many_segments:
            try:
                blf.color(font_id, 1.0, 1.0, 1.0, 0.6)
            except Exception:
                pass
            blf.position(font_id, end2d.x - 4, end2d.y - 4, 0)
            blf.draw(font_id, "■")
        # highlight if keyframe
        if _kp and start2d:
            try:
                blf.color(font_id, *bcol)
            except Exception:
                pass
            blf.position(font_id, start2d.x - 4, start2d.y - 4, 0)
            blf.draw(font_id, "■")
        if _kc and end2d:
            try:
                blf.color(font_id, *bcol)
            except Exception:
                pass
            blf.position(font_id, end2d.x - 4, end2d.y - 4, 0)
            blf.draw(font_id, "■")


def _validate_animation_duration(scene, fps: float) -> List[str]:
    """Validate animation duration against Roblox limits."""
    warnings = []
    duration = (scene.frame_end - scene.frame_start + 1) / fps

    if duration >= ANIM_MAX_DURATION:
        warnings.append(
            f"Animation duration {duration:.2f}s must be less than {ANIM_MAX_DURATION}s"
        )

    return warnings


def _resolve_motion_threshold_for_fps(
    benchmark_studs_per_frame: float,
    fps: float,
) -> float:
    """Convert the 30 fps benchmark threshold into a per-frame limit for the scene fps."""
    try:
        benchmark_studs_per_frame = float(benchmark_studs_per_frame)
    except (TypeError, ValueError):
        benchmark_studs_per_frame = ANIM_MAX_DELTA

    try:
        fps = float(fps)
    except (TypeError, ValueError):
        fps = ANIM_FPS

    if benchmark_studs_per_frame < 0:
        benchmark_studs_per_frame = 0.0
    if fps <= _VALIDATION_SCALE_EPSILON:
        return benchmark_studs_per_frame

    return benchmark_studs_per_frame * (ANIM_FPS / fps)


def _validate_body_distance_from_root(
    positions: Dict[str, Vector],
    root_pos: Vector,
    units_per_stud: float,
    max_distance: float = ANIM_MAX_BODY_DISTANCE,
) -> List[Tuple[str, str]]:
    """Return advisory warnings for body parts outside the documented root envelope."""
    warnings = []
    scale = max(abs(units_per_stud), _VALIDATION_SCALE_EPSILON)

    for bone_name, pos in positions.items():
        distance = (root_pos - pos).length / scale
        if distance > max_distance:
            warnings.append(
                (
                    bone_name,
                    f"Body part '{bone_name}' is {distance:.2f} studs from HumanoidRootPart "
                    f"(max: {max_distance:.3f})",
                )
            )

    return warnings


def _validate_body_height_from_root(
    positions: Dict[str, Vector],
    root_pos: Vector,
    units_per_stud: float,
    max_depth: float = ANIM_MAX_BELOW_ROOT,
) -> List[Tuple[str, str]]:
    """Return advisory warnings for body parts below the documented root height."""
    warnings = []
    scale = max(abs(units_per_stud), _VALIDATION_SCALE_EPSILON)

    for bone_name, pos in positions.items():
        depth = (root_pos.z - pos.z) / scale  # Blender Z is Roblox Y.
        if depth > max_depth:
            warnings.append(
                (
                    bone_name,
                    f"Body part '{bone_name}' is {depth:.2f} studs below HumanoidRootPart "
                    f"(max: {max_depth:.3f})",
                )
            )

    return warnings


def _validate_rotation_constraints(
    armature_obj: "bpy.types.Object",
    evaluated_obj: "bpy.types.Object",
    allowed_bone_names: Optional[Set[str]] = None,
) -> List[str]:
    """Validate bone rotations for proper constraints."""
    warnings = []

    for pbone in evaluated_obj.pose.bones:
        if allowed_bone_names is not None and pbone.name not in allowed_bone_names:
            continue
        # Check for extreme rotations that might cause issues
        rot = pbone.rotation_quaternion

        # Check for NaN or infinite values
        if any(math.isnan(x) or math.isinf(x) for x in rot):
            warnings.append(f"Bone '{pbone.name}' has invalid rotation (NaN/Inf)")
            continue

        # Check for extreme rotation angles (more than 180 degrees in any axis)
        euler = rot.to_euler()
        max_angle = max(abs(euler.x), abs(euler.y), abs(euler.z))

        if max_angle > math.pi:  # 180 degrees
            warnings.append(
                f"Bone '{pbone.name}' has extreme rotation: {math.degrees(max_angle):.1f}°"
            )

    return warnings


def _collect_bone_world_head(
    armature_obj: "bpy.types.Object",
    evaluated_obj: "bpy.types.Object",
    allowed_bone_names: Optional[Set[str]] = None,
) -> Dict[str, Vector]:
    """return world-space head positions for relevant bones on the evaluated armature."""
    positions: Dict[str, Vector] = {}
    # determine which bones to include
    settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
    force_deform = getattr(settings, "force_deform_bone_serialization", False)
    is_deform = is_deform_bone_rig(armature_obj) or force_deform

    world_mat = evaluated_obj.matrix_world
    for pbone in evaluated_obj.pose.bones:
        if allowed_bone_names is not None and pbone.name not in allowed_bone_names:
            continue
        # An explicit allowlist is authoritative.  The is_transformable /
        # use_deform discovery filter only applies when no allowlist is given:
        # the internal R15 validation template (the motion-path measurement
        # armature) carries neither marker, and filtering it here produced
        # empty position sets, silently disabling the per-frame studs check.
        if allowed_bone_names is None:
            if is_deform:
                if not pbone.bone.use_deform:
                    continue
            elif "is_transformable" not in pbone.bone:
                continue
        # pbone.head is in armature space
        head_world = world_mat @ pbone.head
        positions[pbone.name] = head_world
    return positions


def _get_root_world_pos(
    armature_obj: "bpy.types.Object", evaluated_obj: "bpy.types.Object"
) -> Vector:
    """Return world-space root position based on pose bones (per-frame)."""
    world_mat = evaluated_obj.matrix_world

    root_bone_name = _resolve_validation_root_bone_name(evaluated_obj.pose.bones)
    if root_bone_name is not None:
        pbone = evaluated_obj.pose.bones.get(root_bone_name)
        if pbone is not None:
            return world_mat @ pbone.head

    # Fallback: first bone with no parent
    for pbone in evaluated_obj.pose.bones:
        if pbone.parent is None:
            return world_mat @ pbone.head

    # Final fallback: armature object origin (or parent if present)
    if armature_obj.parent:
        return armature_obj.parent.matrix_world.translation.copy()
    return armature_obj.matrix_world.translation.copy()


class OBJECT_OT_ValidateMotionPaths(Operator):
    bl_label = "Validate UGC Emote (Roblox)"
    bl_idname = "object.rbxanims_validate_motionpaths"
    bl_description = "run Roblox UGC emote validation checks and draw motion warnings"

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        obj = get_object_by_name(arm_name)
        return bool(obj and obj.type == "ARMATURE")

    def execute(self, context):
        global \
            _violation_segments, \
            _violation_draw_handler, \
            _violation_label_draw_handler, \
            _keyframe_points, \
            _keyframe_points_draw_handler, \
            _floor_limit_draw_handler, \
            _below_root_violations, \
            _floor_limit_z, \
            _floor_limit_cx, \
            _floor_limit_cy, \
            _validation_display_armature_name, \
            _validation_display_units_per_stud

        scene = context.scene
        settings = getattr(scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        armature = get_object_by_name(arm_name)
        if not armature or armature.type != "ARMATURE":
            self.report({"ERROR"}, "no valid armature selected")
            return {"CANCELLED"}

        measure_source = _resolve_validation_measure_rig(armature)
        if measure_source is not armature:
            self.report(
                {"INFO"},
                f"'{armature.name}' is a control rig; validating "
                f"driven rig '{measure_source.name}'",
            )

        rig_warnings = _validate_standard_r15_rig(measure_source.data.bones)
        if rig_warnings:
            for warning in rig_warnings:
                self.report({"ERROR"}, warning)
            self.report(
                {"ERROR"},
                "UGC emote validation requires a standard R15 rig; validation was not run.",
            )
            return {"CANCELLED"}

        depsgraph = context.evaluated_depsgraph_get()
        fps = get_scene_fps()
        max_studs = (
            getattr(settings, "rbx_max_studs_per_frame", ANIM_MAX_DELTA)
            or ANIM_MAX_DELTA
        )
        # The tracker samples on a fixed 1/70s grid regardless of scene fps.
        effective_max_studs = _resolve_motion_threshold_for_fps(
            max_studs, ANIM_VALIDATION_FPS
        )
        effective_root_displacement = _resolve_motion_threshold_for_fps(
            ANIM_ROOT_DISPLACEMENT_ADVISORY, ANIM_VALIDATION_FPS
        )

        source_units_per_stud, source_scale_source, source_scale_samples = (
            _resolve_validation_units_per_stud(measure_source, settings)
        )
        if source_units_per_stud <= _VALIDATION_SCALE_EPSILON:
            source_units_per_stud = 1.0
        units_per_stud = 1.0
        _validation_display_armature_name = measure_source.name
        # source_units_per_stud is LOCAL bu/stud; the viewport overlay draws
        # in world space, so scale it by the rig object's world scale.
        object_scale = measure_source.matrix_world.to_scale()
        source_object_scale = (object_scale.x + object_scale.y + object_scale.z) / 3.0
        _validation_display_units_per_stud = max(
            abs(source_units_per_stud) * abs(source_object_scale),
            _VALIDATION_SCALE_EPSILON,
        )
        scale_source = "template_r15"
        scale_sample_count = len(_INTERNAL_EMOTE_R15_REST)
        frame_start = scene.frame_start
        frame_end = scene.frame_end

        # Comprehensive validation checks
        all_warnings = []
        all_violations = []
        _below_root_violations = []
        _floor_limit_z = None
        _floor_limit_cx = 0.0
        _floor_limit_cy = 0.0
        validation_body_bone_names = {
            entry["name"]
            for entry in _TEMPLATE_R15_BONES
            if _normalize_bone_name(entry["name"]) in _INTERNAL_EMOTE_R15_REST
        }

        # 1. Duration validation
        duration_warnings = _validate_animation_duration(scene, fps)
        all_warnings.extend(duration_warnings)
        source_warning = (
            "CurveAnimation hierarchy, loop state, marker count, and Marketplace-only "
            "duration minimum cannot be verified in Blender; verify them in Studio."
        )
        all_warnings.append(source_warning)
        self.report({"WARNING"}, source_warning)
        last_positions: Dict[str, Vector] = {}
        last_display_positions: Dict[str, Vector] = {}
        initial_root_pos: Optional[Vector] = None
        max_root_displacement = 0.0
        _violation_segments = []
        total_violations = 0
        _keyframe_points = []

        # collect keyframe frames per bone from active action (if any)
        bone_keyframes: Dict[str, Set[int]] = {}
        action = armature.animation_data.action if armature.animation_data else None
        if action is None and measure_source is not armature:
            action = (
                measure_source.animation_data.action
                if measure_source.animation_data
                else None
            )
        if action is not None:
            import re
            from ..core.utils import get_action_fcurves

            fcurves = get_action_fcurves(action)
            for fcurve in fcurves:
                if not fcurve.data_path.startswith("pose.bones"):
                    continue
                m = re.search(r'pose\\.bones\["(.+?)"\]', fcurve.data_path)
                if not m:
                    continue
                source_name = m.group(1)
                bname = next(
                    (
                        target_name
                        for target_name in validation_body_bone_names
                        if _normalize_bone_name(target_name)
                        == _normalize_bone_name(source_name)
                    ),
                    None,
                )
                if bname is None:
                    continue
                frames = bone_keyframes.setdefault(bname, set())
                for kp in fcurve.keyframe_points:
                    frames.add(int(round(kp.co.x)))

        rest_samples = _collect_validation_rest_scale_samples(measure_source)
        worst_violation = None
        worst_below_root = None

        # iterate samples, measuring the VISUAL pose (NLA + constraints) of the
        # actual rig. the viewport plays the NLA stack and the exporter bakes
        # the visual result, so raw action channels must never be measured.
        # The tracker samples on a fixed 1/UGCValidateMaxAnimationFPS (70)
        # grid from t=0 to the animation length; mirror that grid.
        inv_world_scale = 1.0 / _validation_display_units_per_stud
        root_zero = Vector((0.0, 0.0, 0.0))
        anim_length_seconds = (
            (frame_end - frame_start) / fps
            if fps > _VALIDATION_SCALE_EPSILON
            else 0.0
        )
        sample_delta = 1.0 / ANIM_VALIDATION_FPS
        sample_times = _build_validation_sample_times(
            anim_length_seconds, sample_delta
        )

        for time_s in sample_times:
            scene_frame = frame_start + time_s * fps
            frame_round = int(round(scene_frame))
            frame_floor = int(math.floor(scene_frame))
            frame_frac = scene_frame - frame_floor
            if hasattr(scene, "frame_subframe"):
                # Blender 5.1+: frame_set is int-only; subframes move separately.
                scene.frame_set(frame_floor)
                scene.frame_subframe = frame_frac
            else:
                scene.frame_set(int(round(scene_frame)))
            depsgraph.update()
            source_eval = measure_source.evaluated_get(depsgraph)
            name_map = _collect_validation_name_map(source_eval)
            display_positions, display_root = _collect_rig_display_positions(
                source_eval, measure_source, name_map
            )
            _floor_limit_cx = display_root.x
            _floor_limit_cy = display_root.y

            # studs-space positions relative to the visual root
            positions = {
                internal_name: (world_pos - display_root) * inv_world_scale
                for internal_name, world_pos in display_positions.items()
            }
            root_pos = root_zero

            if initial_root_pos is None:
                initial_root_pos = display_root.copy()
            root_displacement = (
                (display_root - initial_root_pos).length * inv_world_scale
            )
            max_root_displacement = max(max_root_displacement, root_displacement)
            if root_displacement > effective_root_displacement:
                warning = (
                    f"[t={time_s:.2f}s] root moved {root_displacement:.2f} studs from its "
                    f"initial position (>{effective_root_displacement:.3f} advisory)"
                )
                all_violations.append((frame_round, "root", warning))
                self.report({"WARNING"}, warning)

            frame_floor_z = display_root.z - (
                ANIM_MAX_BELOW_ROOT * _validation_display_units_per_stud
            )
            _floor_limit_z = _accumulate_floor_limit_z(_floor_limit_z, frame_floor_z)

            for bone_name, warning in _validate_body_distance_from_root(
                positions, root_pos, units_per_stud
            ):
                all_violations.append((frame_round, bone_name, warning))
                self.report({"WARNING"}, f"[t={time_s:.2f}s] {warning}")

            for bone_name, warning in _validate_body_height_from_root(
                positions, root_pos, units_per_stud
            ):
                all_violations.append((frame_round, bone_name, warning))
                self.report({"WARNING"}, f"[t={time_s:.2f}s] {warning}")
                depth = (root_pos.z - positions[bone_name].z) / units_per_stud
                if worst_below_root is None or depth > worst_below_root[0]:
                    worst_below_root = (depth, bone_name, frame_round)
                bone_pos = display_positions.get(bone_name)
                if bone_pos is not None:
                    floor_pos = Vector((bone_pos.x, bone_pos.y, frame_floor_z))
                    _below_root_violations.append(
                        (
                            bone_pos.copy(),
                            floor_pos,
                            name_map.get(bone_name, bone_name),
                        )
                    )

            # Rotation validation (check every sample)
            rotation_warnings = _validate_rotation_constraints(
                measure_source,
                source_eval,
                allowed_bone_names=set(name_map.values()),
            )
            for warning in rotation_warnings:
                all_warnings.append(f"[t={time_s:.2f}s] {warning}")
                self.report({"WARNING"}, f"[t={time_s:.2f}s] {warning}")

            for bone_name, pos in positions.items():
                display_name = name_map.get(bone_name, bone_name)
                display_pos = display_positions.get(bone_name, pos)
                prev = last_positions.get(bone_name)
                if prev is not None:
                    dist_blender = (pos - prev).length
                    studs = dist_blender / units_per_stud
                    if studs > effective_max_studs:
                        frames = bone_keyframes.get(bone_name, set())
                        key_prev = (frame_round - 1) in frames
                        key_curr = frame_round in frames
                        start_display = last_display_positions.get(bone_name, prev)
                        _violation_segments.append(
                            (
                                start_display.copy(),
                                display_pos.copy(),
                                frame_round,
                                display_name,
                                key_prev,
                                key_curr,
                                studs,
                            )
                        )
                        total_violations += 1
                        self.report(
                            {"WARNING"},
                            f"[t={time_s:.2f}s] bone '{display_name}' moved {studs:.3f} studs "
                            f"(> {effective_max_studs:.3f} at {ANIM_VALIDATION_FPS:.0f} fps; "
                            f"benchmark {max_studs:.3f} @ {ANIM_FPS:.0f} fps)",
                        )
                        if worst_violation is None or studs > worst_violation[0]:
                            pose_bu = (display_pos - display_root).length
                            rest_entry = rest_samples.get(
                                _normalize_bone_name(bone_name)
                            )
                            rest_bu = (
                                float(rest_entry.get("distance", 0.0))
                                if rest_entry
                                else 0.0
                            )
                            canon_bu = float(
                                _INTERNAL_EMOTE_R15_REST.get(
                                    _normalize_bone_name(bone_name), {}
                                ).get("distance", 0.0)
                            )
                            source_pb = source_eval.pose.bones.get(display_name)
                            n_cons = (
                                len(source_pb.constraints)
                                if source_pb is not None
                                else 0
                            )
                            worst_violation = (
                                studs,
                                bone_name,
                                frame_round,
                                pose_bu,
                                rest_bu,
                                canon_bu,
                                n_cons,
                            )
                last_positions[bone_name] = pos
                last_display_positions[bone_name] = display_pos

                # record keyframe point if this bone has a key at this frame
                frames = bone_keyframes.get(bone_name, set())
                if frame_round in frames:
                    _keyframe_points.append(
                        (display_pos.copy(), display_name, frame_round)
                    )

        # install draw handlers if not present
        if _violation_draw_handler is None:
            _violation_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
                _draw_motionpath_violations, (), "WINDOW", "POST_VIEW"
            )
        if _violation_label_draw_handler is None:
            _violation_label_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
                _draw_motionpath_labels, (), "WINDOW", "POST_PIXEL"
            )
        if _keyframe_points_draw_handler is None:
            _keyframe_points_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
                _draw_motionpath_keyframes, (), "WINDOW", "POST_VIEW"
            )
        if _floor_limit_draw_handler is None:
            _floor_limit_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
                _draw_floor_limit, (), "WINDOW", "POST_VIEW"
            )

        settings = getattr(scene, "rbx_anim_settings", None)
        if settings:
            setattr(settings, "rbx_show_motionpath_validation", True)

        # Summary report
        total_warnings = len(all_warnings)
        total_root_violations = len(all_violations)

        summary_msg = f"Validation complete: {total_violations} motion violations"
        if total_warnings > 0:
            summary_msg += f", {total_warnings} warnings"
        if total_root_violations > 0:
            summary_msg += f", {total_root_violations} root/body-envelope warnings"
        summary_msg += (
            f", scale {units_per_stud:.4f} bu/stud"
            f" ({scale_source}, {scale_sample_count} samples)"
        )
        summary_msg += (
            f", source scale {source_units_per_stud:.4f} bu/stud"
            f" ({source_scale_source}, {source_scale_samples} samples)"
        )
        summary_msg += f", rig object scale {source_object_scale:.4f}"
        summary_msg += f", max root displacement {max_root_displacement:.4f} studs"
        if worst_violation is not None:
            studs_w, bone_w, frame_w, pose_bu, rest_bu, canon_bu, n_cons = (
                worst_violation
            )
            summary_msg += (
                f", worst '{bone_w}' f{frame_w} {studs_w:.3f} st"
                f" (pose {pose_bu:.3f} bu, rest {rest_bu:.3f} vs canon"
                f" {canon_bu:.3f} bu, {n_cons} constraints)"
            )
        if worst_below_root is not None:
            depth_w, bone_b, frame_b = worst_below_root
            summary_msg += (
                f", deepest '{bone_b}' f{frame_b} {depth_w:.3f} st below root"
            )
        if validation_body_bone_names is not None:
            summary_msg += f", validating {len(validation_body_bone_names)} body bones"
        summary_msg += (
            f", motion threshold {effective_max_studs:.4f} studs/frame"
            f" at {ANIM_VALIDATION_FPS:.0f} fps (Roblox samples CurveAnimation at {ANIM_VALIDATION_FPS:.0f} fps)"
        )
        summary_msg += (
            f", envelope limits {ANIM_MAX_BODY_DISTANCE:.1f} distance / "
            f"{ANIM_MAX_BELOW_ROOT:.1f} depth studs"
        )

        self.report({"INFO"}, summary_msg)

        # Log detailed warnings to console
        if all_warnings:
            print("=== ANIMATION VALIDATION WARNINGS ===")
            for warning in all_warnings:
                print(f"WARNING: {warning}")
            print("=====================================")

        return {"FINISHED"}


class OBJECT_OT_ClearMotionPathValidation(Operator):
    bl_label = "Clear Motion Path Validation"
    bl_idname = "object.rbxanims_clear_motionpaths"
    bl_description = "remove validation overlay and clear cached violations"

    def execute(self, context):
        global \
            _violation_segments, \
            _violation_draw_handler, \
            _violation_label_draw_handler, \
            _keyframe_points, \
            _keyframe_points_draw_handler, \
            _floor_limit_draw_handler, \
            _below_root_violations, \
            _floor_limit_z, \
            _floor_limit_cx, \
            _floor_limit_cy, \
            _validation_display_armature_name, \
            _validation_display_units_per_stud

        _violation_segments = []
        _keyframe_points = []
        _below_root_violations = []
        _floor_limit_z = None
        _floor_limit_cx = 0.0
        _floor_limit_cy = 0.0
        _validation_display_armature_name = ""
        _validation_display_units_per_stud = 1.0
        if _violation_draw_handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    _violation_draw_handler, "WINDOW"
                )
            except Exception:
                pass
            _violation_draw_handler = None
        if _violation_label_draw_handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    _violation_label_draw_handler, "WINDOW"
                )
            except Exception:
                pass
            _violation_label_draw_handler = None
        if _keyframe_points_draw_handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    _keyframe_points_draw_handler, "WINDOW"
                )
            except Exception:
                pass
            _keyframe_points_draw_handler = None
        if _floor_limit_draw_handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    _floor_limit_draw_handler, "WINDOW"
                )
            except Exception:
                pass
            _floor_limit_draw_handler = None

        settings = getattr(context.scene, "rbx_anim_settings", None)
        if settings:
            setattr(settings, "rbx_show_motionpath_validation", False)
        self.report({"INFO"}, "validation overlay cleared")
        return {"FINISHED"}


__all__ = [
    "OBJECT_OT_ValidateMotionPaths",
    "OBJECT_OT_ClearMotionPathValidation",
]


def cleanup_validation_draw_handlers():
    """remove any active validation draw handlers and clear cache; safe on reload/unregister."""
    global \
        _violation_segments, \
        _violation_draw_handler, \
        _violation_label_draw_handler, \
        _keyframe_points, \
        _keyframe_points_draw_handler, \
        _floor_limit_draw_handler, \
        _below_root_violations, \
        _floor_limit_z, \
        _floor_limit_cx, \
        _floor_limit_cy, \
        _validation_display_armature_name, \
        _validation_display_units_per_stud
    _violation_segments = []
    _keyframe_points = []
    _below_root_violations = []
    _floor_limit_z = None
    _floor_limit_cx = 0.0
    _floor_limit_cy = 0.0
    _validation_display_armature_name = ""
    _validation_display_units_per_stud = 1.0
    if _violation_draw_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_violation_draw_handler, "WINDOW")
        except Exception:
            pass
        _violation_draw_handler = None
    if _violation_label_draw_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                _violation_label_draw_handler, "WINDOW"
            )
        except Exception:
            pass
        _violation_label_draw_handler = None
    if _keyframe_points_draw_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                _keyframe_points_draw_handler, "WINDOW"
            )
        except Exception:
            pass
        _keyframe_points_draw_handler = None
    if _floor_limit_draw_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(
                _floor_limit_draw_handler, "WINDOW"
            )
        except Exception:
            pass
        _floor_limit_draw_handler = None
