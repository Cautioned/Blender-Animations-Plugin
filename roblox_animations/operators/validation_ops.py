"""
validation operators and viewport overlay for roblox animation validation.

performs comprehensive validation checks including:
- per-frame world displacement of each limb (bone) against max studs/frame threshold
- animation duration limits (max 10 seconds)
- bounds checking (max 5 studs from root)
- rotation validation for proper bone constraints
- draws violation segments and warnings in the 3d viewport
"""

import bpy
from bpy.types import Operator
from mathutils import Vector
from typing import Dict, List, Tuple, Set

from ..animation.serialization import is_deform_bone_rig
from ..core.utils import get_scene_fps, get_object_by_name
import math


# global state for the draw overlay
_violation_draw_handler = None  # lines
_violation_label_draw_handler = None  # labels
_keyframe_points_draw_handler = None  # keyframe markers
_violation_segments: List[
    Tuple[Vector, Vector, int, str, bool, bool]
] = []  # (start, end, frame, bone_name, key_prev, key_curr)
_bone_color_cache: Dict[str, Tuple[float, float, float, float]] = {}
_armature_name_for_cache: str = ""
_keyframe_points: List[Tuple[Vector, str, int]] = []  # (location, bone_name, frame)

# Roblox animation validation constants (matching Lua script)
ANIM_MAX_DURATION = 10.0  # seconds
ANIM_MAX_BOUNDS = 5.0  # studs from root
ANIM_MAX_DELTA = 1.0  # studs per frame
ANIM_FPS = 30.0  # target fps


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
    arm_name = settings.rbx_anim_armature if settings else None
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
    for start, end, _frame, bone_name, _kp, _kc in _violation_segments:
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
        arm_name = settings.rbx_anim_armature if settings else None
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

    for start, end, frame, bone_name, _kp, _kc in _violation_segments:
        mid = (start + end) * 0.5
        pos2d = view3d_utils.location_3d_to_region_2d(region, rv3d, mid)
        if not pos2d:
            continue
        text = f"{bone_name}  f:{frame}"
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

    if duration > ANIM_MAX_DURATION:
        warnings.append(
            f"Animation duration {duration:.2f}s exceeds Roblox limit of {ANIM_MAX_DURATION}s"
        )

    return warnings


def _validate_bounds(
    positions: Dict[str, Vector], root_pos: Vector, scale: float
) -> List[Tuple[str, str]]:
    """Validate bone positions against bounds from root."""
    violations = []

    for bone_name, pos in positions.items():
        offset = root_pos - pos
        distance = offset.length / scale

        if distance > ANIM_MAX_BOUNDS:
            violations.append(
                (
                    bone_name,
                    f"Bone '{bone_name}' is {distance:.2f} studs from root (max: {ANIM_MAX_BOUNDS})",
                )
            )

    return violations


def _validate_rotation_constraints(
    armature_obj: "bpy.types.Object", evaluated_obj: "bpy.types.Object"
) -> List[str]:
    """Validate bone rotations for proper constraints."""
    warnings = []

    for pbone in evaluated_obj.pose.bones:
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
    armature_obj: "bpy.types.Object", evaluated_obj: "bpy.types.Object"
) -> Dict[str, Vector]:
    """return world-space head positions for relevant bones on the evaluated armature."""
    positions: Dict[str, Vector] = {}
    # determine which bones to include
    settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
    force_deform = getattr(settings, "force_deform_bone_serialization", False)
    is_deform = is_deform_bone_rig(armature_obj) or force_deform

    world_mat = evaluated_obj.matrix_world
    for pbone in evaluated_obj.pose.bones:
        if is_deform:
            if not pbone.bone.use_deform:
                continue
        else:
            if "is_transformable" not in pbone.bone:
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

    # Prefer an explicit root bone name if present
    for name in ("Root", "root"):
        pbone = evaluated_obj.pose.bones.get(name)
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
    bl_label = "Validate Motion Paths (Roblox)"
    bl_idname = "object.rbxanims_validate_motionpaths"
    bl_description = "check per-frame world displacement of limbs against the max studs/frame and draw violations"

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
            _keyframe_points_draw_handler

        scene = context.scene
        settings = getattr(scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        armature = get_object_by_name(arm_name)
        if not armature or armature.type != "ARMATURE":
            self.report({"ERROR"}, "no valid armature selected")
            return {"CANCELLED"}

        depsgraph = context.evaluated_depsgraph_get()
        fps = get_scene_fps()
        max_studs = (
            getattr(settings, "rbx_max_studs_per_frame", ANIM_MAX_DELTA)
            or ANIM_MAX_DELTA
        )

        force_deform = getattr(settings, "force_deform_bone_serialization", False)
        deform_scale = getattr(settings, "rbx_deform_rig_scale", 1.0)

        is_deform = is_deform_bone_rig(armature) or force_deform
        scale = deform_scale if is_deform else 1.0
        if scale == 0:
            scale = 1.0

        frame_start = scene.frame_start
        frame_end = scene.frame_end

        # Comprehensive validation checks
        all_warnings = []
        all_violations = []

        # 1. Duration validation
        duration_warnings = _validate_animation_duration(scene, fps)
        all_warnings.extend(duration_warnings)

        last_positions: Dict[str, Vector] = {}
        _violation_segments = []
        total_violations = 0
        _keyframe_points = []

        # collect keyframe frames per bone from active action (if any)
        bone_keyframes: Dict[str, Set[int]] = {}
        action = armature.animation_data.action if armature.animation_data else None
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
                bname = m.group(1)
                frames = bone_keyframes.setdefault(bname, set())
                for kp in fcurve.keyframe_points:
                    frames.add(int(round(kp.co.x)))

        # iterate frames
        for f in range(frame_start, frame_end + 1):
            scene.frame_set(f)
            arm_eval = armature.evaluated_get(depsgraph)
            root_pos = _get_root_world_pos(armature, arm_eval)
            positions = _collect_bone_world_head(armature, arm_eval)

            # 3. Bounds validation (check every frame)
            if root_pos is not None:
                bounds_violations = _validate_bounds(positions, root_pos, scale)
                for bone_name, violation_msg in bounds_violations:
                    all_violations.append((f, bone_name, violation_msg))
                    self.report({"WARNING"}, f"[frame {f}] {violation_msg}")

            # 4. Rotation validation (check every frame)
            rotation_warnings = _validate_rotation_constraints(armature, arm_eval)
            for warning in rotation_warnings:
                all_warnings.append(f"[frame {f}] {warning}")
                self.report({"WARNING"}, f"[frame {f}] {warning}")

            for bone_name, pos in positions.items():
                prev = last_positions.get(bone_name)
                if prev is not None:
                    dist_blender = (pos - prev).length
                    studs = dist_blender / scale
                    if studs > max_studs:
                        frames = bone_keyframes.get(bone_name, set())
                        key_prev = (f - 1) in frames
                        key_curr = f in frames
                        _violation_segments.append(
                            (prev.copy(), pos.copy(), f, bone_name, key_prev, key_curr)
                        )
                        total_violations += 1
                        self.report(
                            {"WARNING"},
                            f"[frame {f}] bone '{bone_name}' moved {studs:.3f} studs (> {max_studs})",
                        )
                last_positions[bone_name] = pos

                # record keyframe point if this bone has a key at this frame
                frames = bone_keyframes.get(bone_name, set())
                if f in frames:
                    _keyframe_points.append((pos.copy(), bone_name, f))

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

        settings = getattr(scene, "rbx_anim_settings", None)
        if settings:
            setattr(settings, "rbx_show_motionpath_validation", True)

        # Summary report
        total_warnings = len(all_warnings)
        total_bounds_violations = len(all_violations)

        summary_msg = f"Validation complete: {total_violations} motion violations"
        if total_warnings > 0:
            summary_msg += f", {total_warnings} warnings"
        if total_bounds_violations > 0:
            summary_msg += f", {total_bounds_violations} bounds violations"

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
            _keyframe_points_draw_handler

        _violation_segments = []
        _keyframe_points = []
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
        _keyframe_points_draw_handler
    _violation_segments = []
    _keyframe_points = []
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
