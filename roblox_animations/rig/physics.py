"""
Physics-based animation analysis and visualization.

Provides AutoPhysics-like features for analyzing animation physical validity,
including ballistic trajectory prediction, fulcrum point detection, and
ghost character visualization showing physics-corrected positions.
"""

import bpy
import gpu
import math
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from typing import Optional, Dict, List, Tuple

from .com import calculate_com
from ..core.utils import get_object_by_name, get_scene_fps


# Default gravity constant (Blender units per second squared)
# 50 works well for typical Roblox-scale rigs in Blender
# This is overridden by scene property rbx_physics_gravity
DEFAULT_GRAVITY = 50.0

# Velocity threshold for considering a point "at rest"
VELOCITY_THRESHOLD = 0.1

# Distance from ground to consider "grounded"
# This is relative to the rig's rest foot height, not absolute Z=0
GROUND_THRESHOLD = 0.15  # Increased for better detection

# Minimum foot height when standing - will be auto-detected
MIN_FOOT_HEIGHT = 0.0

# Ground plane Z level (feet rest on this level)
GROUND_LEVEL = 0.0

# Number of frames to use for velocity smoothing (must be odd)
# Smaller = more responsive to quick movements, larger = smoother but may miss fast changes
VELOCITY_SMOOTHING_WINDOW = 3

TRAJECTORY_VIEW_BEHIND = 30
TRAJECTORY_VIEW_AHEAD = 60
TRAJECTORY_ERROR_WARN = 0.2
TRAJECTORY_ERROR_BAD = 0.5
TRAJECTORY_MARKER_SEGMENTS = 32
MIN_LAUNCH_SPEED = 0.25
GHOST_ONION_SECONDS = (0.0, 0.15, 0.3, 0.45)
GHOST_MAX_ONIONS = 4
CONTACT_SIDES = ("left", "right")
FOOT_LOCK_SPEED = 0.75
FOOT_SLIDE_SPEED = 2.5
FOOT_CONTACT_HEIGHT = GROUND_THRESHOLD * 1.35
FOOT_TARGET_RADIUS = 0.13
IK_ASSIST_MIN_CONFIDENCE = 0.55

LEFT_FOOT_PATTERNS = (
    "leftfoot", "left_foot", "foot_l", "foot.l", "l_foot", "l.foot",
    "lefttoes", "lefttoe", "toes_l", "toe_l", "toes.l", "toe.l",
    "l_toes", "l_toe", "l.toes", "l.toe", "leftankle", "leftheel",
    "heel_l", "heel.l", "left foot",
)
RIGHT_FOOT_PATTERNS = (
    "rightfoot", "right_foot", "foot_r", "foot.r", "r_foot", "r.foot",
    "righttoes", "righttoe", "toes_r", "toe_r", "toes.r", "toe.r",
    "r_toes", "r_toe", "r.toes", "r.toe", "rightankle", "rightheel",
    "heel_r", "heel.r", "right foot",
)
FOOT_NAME_PATTERNS = LEFT_FOOT_PATTERNS + RIGHT_FOOT_PATTERNS
LEG_IK_HINTS = ("foot", "toe", "toes", "ankle", "heel", "leg", "thigh", "shin")


def get_gravity() -> float:
    """Get gravity value from scene settings or use default."""
    try:
        settings = bpy.context.scene.rbx_anim_settings
        return settings.rbx_physics_gravity
    except Exception:
        return DEFAULT_GRAVITY


# Physics analysis state
_physics_data = {
    "enabled": False,
    "armature_name": None,
    "fps": 30.0,
    "gravity": DEFAULT_GRAVITY,

    # Frame analysis data
    "frame_states": {},  # frame -> "grounded" | "airborne" | "invalid"
    "com_positions": {},  # frame -> Vector
    "com_velocities": {},  # frame -> Vector (smoothed)
    "bone_positions": {},  # frame -> bone name -> (head_world, tail_world)
    "foot_positions": {},  # frame -> side -> Vector
    "foot_velocities": {},  # frame -> side -> Vector

    # Fulcrum detection
    "fulcrum_frames": set(),  # Frames where character has ground contact
    "fulcrum_positions": {},  # frame -> list of contact points
    "contact_count": {},  # frame -> number of contacts (0, 1, or 2)
    "contact_tracks": {"left": [], "right": []},
    "contact_state": {},  # frame -> side -> "plant" | "slide" | "swing"
    "contact_confidence": {},  # frame -> confidence 0..1

    # Ballistic trajectory
    "trajectory_start_frame": None,
    "trajectory_start_pos": None,
    "trajectory_start_vel": None,
    "trajectory_launch_frames": set(),
    "trajectory_landing_frames": set(),
    "trajectory_landing_positions": {},
    "predicted_positions": {},  # frame -> predicted COM position
    "root_corrections": {},  # frame -> predicted COM minus actual COM
    "foot_targets": {},  # frame -> side -> target Vector
    "invalid_frames": set(),
    "worst_frame": None,
    "worst_error": 0.0,

    # Ground detection
    "detected_ground_level": 0.0,
    "com_to_feet_offset": 1.0,

    # Ghost visualization
    "show_ghost": True,
    "show_com_marker": True,
    "show_ground_plane": True,
    "ghost_color": (0.0, 1.0, 0.5, 0.5),  # Green, semi-transparent

    # Visual settings
    "trajectory_color": (1.0, 0.5, 0.0, 0.9),  # Orange
    "actual_path_color": (0.3, 0.8, 1.0, 0.8),  # Cyan
    "launch_color": (0.1, 0.85, 1.0, 1.0),  # Cyan
    "landing_color": (0.25, 1.0, 0.35, 1.0),  # Green
    "focus_color": (1.0, 0.12, 0.05, 1.0),  # Red
    "foot_target_color": (0.1, 0.9, 1.0, 0.9),  # Cyan
    "foot_lock_color": (0.25, 1.0, 0.35, 0.9),  # Green
    "root_correction_color": (1.0, 0.95, 0.2, 0.95),  # Yellow
    "low_confidence_color": (0.65, 0.65, 0.65, 0.45),  # Gray
    "grounded_color": (0.0, 1.0, 0.0, 1.0),  # Green
    "airborne_color": (1.0, 0.7, 0.0, 1.0),  # Orange/Yellow
    "invalid_color": (1.0, 0.0, 0.0, 1.0),  # Red
    "com_marker_color": (1.0, 1.0, 0.0, 1.0),  # Yellow
    "ground_plane_color": (0.3, 0.3, 0.3, 0.3),  # Gray, transparent
}

_physics_draw_handler = None


def analyze_animation(armature: "bpy.types.Object", start_frame: int = None, end_frame: int = None):
    """Analyze the animation for physics validity.

    Calculates COM positions, velocities, detects fulcrum points,
    and determines frame states (grounded/airborne/invalid).

    Args:
        armature: The armature to analyze.
        start_frame: Start frame (defaults to scene start).
        end_frame: End frame (defaults to scene end).
    """
    if not armature or armature.type != "ARMATURE":
        return

    scene = bpy.context.scene
    original_frame = scene.frame_current

    if start_frame is None:
        start_frame = scene.frame_start
    if end_frame is None:
        end_frame = scene.frame_end

    fps = get_scene_fps()
    if fps <= 0:
        fps = 30.0
    _physics_data["fps"] = fps
    _physics_data["armature_name"] = armature.name
    _physics_data["gravity"] = get_gravity()  # Store current gravity setting
    _physics_data["start_frame"] = start_frame
    _physics_data["end_frame"] = end_frame

    # Clear ALL previous data completely
    _physics_data["com_positions"] = {}
    _physics_data["com_velocities"] = {}
    _physics_data["bone_positions"] = {}
    _physics_data["foot_positions"] = {}
    _physics_data["foot_velocities"] = {}
    _physics_data["frame_states"] = {}
    _physics_data["fulcrum_frames"] = set()
    _physics_data["fulcrum_positions"] = {}
    _physics_data["predicted_positions"] = {}
    _physics_data["root_corrections"] = {}
    _physics_data["foot_targets"] = {}
    _physics_data["contact_count"] = {}
    _physics_data["contact_tracks"] = {"left": [], "right": []}
    _physics_data["contact_state"] = {}
    _physics_data["contact_confidence"] = {}

    # Reset trajectory tracking
    _physics_data["trajectory_start_frame"] = None
    _physics_data["trajectory_start_pos"] = None
    _physics_data["trajectory_start_vel"] = None
    _physics_data["trajectory_launch_frames"] = set()
    _physics_data["trajectory_landing_frames"] = set()
    _physics_data["trajectory_landing_positions"] = {}
    _physics_data["invalid_frames"] = set()
    _physics_data["worst_frame"] = None
    _physics_data["worst_error"] = 0.0
    _physics_data["detected_ground_level"] = 0.0
    _physics_data["com_to_feet_offset"] = 1.0

    # Detect ground level from the lowest foot/toe position in the animation
    ground_level = _detect_ground_level(armature, start_frame, end_frame, scene)
    _physics_data["detected_ground_level"] = ground_level

    # First pass: collect COM positions and contact info
    for frame in range(start_frame, end_frame + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()

        com = calculate_com(armature)
        _physics_data["com_positions"][frame] = com.copy()
        _physics_data["bone_positions"][frame] = _capture_bone_positions(armature)
        foot_points = _get_foot_points(armature)
        _physics_data["foot_positions"][frame] = foot_points

        # Detect fulcrum points (feet on ground)
        fulcrums = _contacts_from_foot_points(foot_points)
        _physics_data["contact_count"][frame] = len(fulcrums)
        if fulcrums:
            _physics_data["fulcrum_frames"].add(frame)
            _physics_data["fulcrum_positions"][frame] = fulcrums

    # Second pass: calculate smoothed velocities
    _calculate_smoothed_velocities(start_frame, end_frame, fps)
    _calculate_foot_velocities(start_frame, end_frame, fps)
    _analyze_contact_tracks(start_frame, end_frame)

    # Third pass: determine frame states and calculate predictions
    _analyze_frame_states(start_frame, end_frame, fps)
    _calculate_constraint_proposals(start_frame, end_frame)

    # Restore original frame
    scene.frame_set(original_frame)

    # Force viewport redraw to show new analysis
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def _calculate_smoothed_velocities(start_frame: int, end_frame: int, fps: float):
    """Calculate smoothed velocities using a moving window average.

    This reduces noise in velocity estimation which improves trajectory prediction.
    """
    half_window = VELOCITY_SMOOTHING_WINDOW // 2

    for frame in range(start_frame, end_frame + 1):
        # Gather positions in the window
        positions = []
        frames_in_window = []

        for offset in range(-half_window, half_window + 1):
            f = frame + offset
            if f in _physics_data["com_positions"]:
                positions.append(_physics_data["com_positions"][f])
                frames_in_window.append(f)

        if len(positions) < 2:
            _physics_data["com_velocities"][frame] = Vector((0, 0, 0))
            continue

        # Use linear regression to find best-fit velocity
        # This is more robust than simple differences
        n = len(positions)

        # Calculate means
        mean_t = sum(frames_in_window) / n
        mean_pos = Vector((0, 0, 0))
        for p in positions:
            mean_pos += p
        mean_pos /= n

        # Calculate slope (velocity)
        numerator = Vector((0, 0, 0))
        denominator = 0.0

        for i, (f, p) in enumerate(zip(frames_in_window, positions)):
            t_diff = f - mean_t
            pos_diff = p - mean_pos
            numerator += pos_diff * t_diff
            denominator += t_diff * t_diff

        if denominator > 0.0001:
            # Velocity in units per frame, convert to units per second
            vel = numerator / denominator * fps
        else:
            vel = Vector((0, 0, 0))

        _physics_data["com_velocities"][frame] = vel


def _foot_side_from_name(bone_name: str) -> Optional[str]:
    bone_name_lower = bone_name.lower()
    is_left = any(pattern in bone_name_lower for pattern in LEFT_FOOT_PATTERNS)
    is_right = any(pattern in bone_name_lower for pattern in RIGHT_FOOT_PATTERNS)
    if is_left and not is_right:
        return "left"
    if is_right and not is_left:
        return "right"
    return None


def _side_from_name(name: str) -> Optional[str]:
    name_lower = (name or "").lower()
    if any(token in name_lower for token in ("left", "_l", ".l", "-l", " l")) or name_lower.endswith("l"):
        return "left"
    if any(token in name_lower for token in ("right", "_r", ".r", "-r", " r")) or name_lower.endswith("r"):
        return "right"
    return None


def _has_leg_ik_hint(name: str) -> bool:
    name_lower = (name or "").lower()
    return any(hint in name_lower for hint in LEG_IK_HINTS)


def _constraint_side(constrained_bone: "bpy.types.PoseBone", constraint) -> Optional[str]:
    candidates = [
        getattr(constrained_bone, "name", ""),
        getattr(constraint, "subtarget", ""),
        getattr(getattr(constraint, "target", None), "name", ""),
    ]
    candidates.extend(child.name for child in getattr(constrained_bone, "children", []) or [])

    if not any(_has_leg_ik_hint(candidate) for candidate in candidates):
        return None

    for candidate in candidates:
        side = _foot_side_from_name(candidate) or _side_from_name(candidate)
        if side is not None:
            return side
    return None


def detect_trajectory_ik_controls(armature: "bpy.types.Object") -> Dict[str, Dict[str, object]]:
    """Return existing left/right leg IK controls without creating or modifying them."""
    controls = {}
    if not armature or armature.type != "ARMATURE" or not getattr(armature, "pose", None):
        return controls

    for constrained_bone in armature.pose.bones:
        for constraint in constrained_bone.constraints:
            if constraint.type != "IK" or not getattr(constraint, "target", None):
                continue

            side = _constraint_side(constrained_bone, constraint)
            if side is None or side in controls:
                continue

            target_object = constraint.target
            target_bone = target_object.pose.bones.get(
                constraint.subtarget) if constraint.subtarget and getattr(target_object, "pose", None) else None
            if constraint.subtarget and target_bone is None:
                continue

            controls[side] = {
                "side": side,
                "constraint_bone": constrained_bone.name,
                "target_object": target_object,
                "target_bone": target_bone,
                "target_bone_name": target_bone.name if target_bone else "",
                "target_object_name": target_object.name,
            }

    return controls


def _lower_bone_endpoint(armature: "bpy.types.Object", pose_bone: "bpy.types.PoseBone") -> Vector:
    head_pos = armature.matrix_world @ pose_bone.head
    tail_pos = armature.matrix_world @ pose_bone.tail
    return head_pos if head_pos.z <= tail_pos.z else tail_pos


def _get_foot_points(armature: "bpy.types.Object") -> Dict[str, Vector]:
    foot_points = {}
    min_z_by_side = {"left": float('inf'), "right": float('inf')}

    for pose_bone in armature.pose.bones:
        side = _foot_side_from_name(pose_bone.name)
        if side is None:
            continue

        foot_pos = _lower_bone_endpoint(armature, pose_bone)
        if foot_pos.z < min_z_by_side[side]:
            min_z_by_side[side] = foot_pos.z
            foot_points[side] = foot_pos.copy()

    return foot_points


def _contacts_from_foot_points(foot_points: Dict[str, Vector]) -> List[Tuple[Vector, str]]:
    ground_level = _physics_data.get("detected_ground_level", GROUND_LEVEL)
    contacts = []
    for side in CONTACT_SIDES:
        foot_pos = foot_points.get(side)
        if foot_pos is not None and foot_pos.z <= ground_level + GROUND_THRESHOLD:
            contacts.append((foot_pos.copy(), side))
    return contacts


def detect_fulcrum_points(armature: "bpy.types.Object") -> List[Tuple[Vector, str]]:
    """Detect ground contact points for the current pose.

    Looks for foot bones that are close to their minimum height,
    accounting for the fact that ankle joints are above the ground.

    Args:
        armature: The armature to check.

    Returns:
        List of (world_position, side) tuples where side is "left" or "right".
    """
    return _contacts_from_foot_points(_get_foot_points(armature))


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _calculate_foot_velocities(start_frame: int, end_frame: int, fps: float):
    foot_positions = _physics_data.get("foot_positions", {})
    _physics_data["foot_velocities"] = {}

    for frame in range(start_frame, end_frame + 1):
        frame_velocities = {}
        for side in CONTACT_SIDES:
            current = foot_positions.get(frame, {}).get(side)
            if current is None:
                continue

            prev_pos = foot_positions.get(frame - 1, {}).get(side)
            next_pos = foot_positions.get(frame + 1, {}).get(side)
            if prev_pos is not None and next_pos is not None:
                frame_velocities[side] = (next_pos - prev_pos) * (fps * 0.5)
            elif prev_pos is not None:
                frame_velocities[side] = (current - prev_pos) * fps
            elif next_pos is not None:
                frame_velocities[side] = (next_pos - current) * fps
            else:
                frame_velocities[side] = Vector((0, 0, 0))

        _physics_data["foot_velocities"][frame] = frame_velocities


def _classify_foot_contact(frame: int, side: str) -> Tuple[str, float]:
    foot = _physics_data.get("foot_positions", {}).get(frame, {}).get(side)
    if foot is None:
        return "unknown", 0.0

    ground_level = _physics_data.get("detected_ground_level", GROUND_LEVEL)
    height = max(0.0, foot.z - ground_level)
    velocity = _physics_data.get("foot_velocities", {}).get(frame, {}).get(side, Vector((0, 0, 0)))
    speed = velocity.length
    vertical_speed = abs(velocity.z)
    height_score = _clamp01(1.0 - (height / max(FOOT_CONTACT_HEIGHT, 0.001)))
    speed_score = _clamp01(1.0 - (speed / max(FOOT_SLIDE_SPEED, 0.001)))

    if height <= FOOT_CONTACT_HEIGHT:
        confidence = _clamp01((height_score * 0.7) + (speed_score * 0.3))
        if speed <= FOOT_LOCK_SPEED:
            return "plant", max(confidence, 0.55)
        if vertical_speed <= FOOT_SLIDE_SPEED:
            return "slide", max(confidence * 0.8, 0.3)

    return "swing", 1.0 - height_score


def _analyze_contact_tracks(start_frame: int, end_frame: int):
    contact_state = {}
    contact_confidence = {}
    tracks = {"left": [], "right": []}

    for frame in range(start_frame, end_frame + 1):
        frame_state = {}
        frame_confidence = {}
        for side in CONTACT_SIDES:
            kind, confidence = _classify_foot_contact(frame, side)
            frame_state[side] = kind
            frame_confidence[side] = confidence
        contact_state[frame] = frame_state
        contact_confidence[frame] = frame_confidence

    for frame in range(start_frame + 1, end_frame):
        for side in CONTACT_SIDES:
            if contact_state[frame].get(side) != "swing":
                continue
            prev_kind = contact_state[frame - 1].get(side)
            next_kind = contact_state[frame + 1].get(side)
            if prev_kind in {"plant", "slide"} and next_kind in {"plant", "slide"}:
                contact_state[frame][side] = prev_kind if prev_kind == next_kind else "slide"
                contact_confidence[frame][side] = min(
                    contact_confidence[frame - 1].get(side, 0.0),
                    contact_confidence[frame + 1].get(side, 0.0),
                    0.65,
                )

    for side in CONTACT_SIDES:
        active_track = None
        for frame in range(start_frame, end_frame + 1):
            kind = contact_state[frame].get(side, "unknown")
            confidence = contact_confidence[frame].get(side, 0.0)
            is_contact = kind in {"plant", "slide"}

            if is_contact and active_track is None:
                active_track = {
                    "start": frame,
                    "end": frame,
                    "kind": kind,
                    "confidence_sum": confidence,
                    "samples": 1,
                    "slide_samples": 1 if kind == "slide" else 0,
                }
            elif is_contact and active_track is not None:
                active_track["end"] = frame
                active_track["confidence_sum"] += confidence
                active_track["samples"] += 1
                if kind == "slide":
                    active_track["slide_samples"] += 1
            elif active_track is not None:
                slide_ratio = active_track["slide_samples"] / max(active_track["samples"], 1)
                active_track["kind"] = "slide" if slide_ratio > 0.4 else "plant"
                active_track["confidence"] = active_track["confidence_sum"] / max(active_track["samples"], 1)
                del active_track["confidence_sum"]
                del active_track["samples"]
                del active_track["slide_samples"]
                tracks[side].append(active_track)
                active_track = None

        if active_track is not None:
            slide_ratio = active_track["slide_samples"] / max(active_track["samples"], 1)
            active_track["kind"] = "slide" if slide_ratio > 0.4 else "plant"
            active_track["confidence"] = active_track["confidence_sum"] / max(active_track["samples"], 1)
            del active_track["confidence_sum"]
            del active_track["samples"]
            del active_track["slide_samples"]
            tracks[side].append(active_track)

    _physics_data["contact_state"] = contact_state
    _physics_data["contact_confidence"] = contact_confidence
    _physics_data["contact_tracks"] = tracks
    _physics_data["fulcrum_frames"] = set()
    _physics_data["fulcrum_positions"] = {}
    _physics_data["contact_count"] = {}

    for frame in range(start_frame, end_frame + 1):
        contacts = []
        for side in CONTACT_SIDES:
            if contact_state[frame].get(side) in {"plant", "slide"}:
                foot_pos = _physics_data.get("foot_positions", {}).get(frame, {}).get(side)
                if foot_pos is not None:
                    contacts.append((foot_pos.copy(), side))
        _physics_data["contact_count"][frame] = len(contacts)
        if contacts:
            _physics_data["fulcrum_frames"].add(frame)
            _physics_data["fulcrum_positions"][frame] = contacts


def _median_number(values: List[float]) -> Optional[float]:
    if not values:
        return None
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2 == 1:
        return sorted_values[middle]
    return 0.5 * (sorted_values[middle - 1] + sorted_values[middle])


def _detect_ground_level(armature: "bpy.types.Object", start_frame: int, end_frame: int, scene) -> float:
    """Detect the ground level from the stable low band of foot/toe heights.

    This accounts for the fact that ankle joints are above the actual ground plane.
    """
    foot_heights = []

    sample_step = 1

    for frame in range(start_frame, end_frame + 1, sample_step):
        scene.frame_set(frame)
        bpy.context.view_layer.update()

        for pos in _get_foot_points(armature).values():
            foot_heights.append(pos.z)

    # If we couldn't find foot bones, default to 0
    if not foot_heights:
        return GROUND_LEVEL

    sorted_heights = sorted(foot_heights)
    low_band_count = min(len(sorted_heights), max(3, int(math.ceil(len(sorted_heights) * 0.1))))
    stable_floor = _median_number(sorted_heights[:low_band_count])
    return stable_floor if stable_floor is not None else sorted_heights[0]


def _resolve_com_to_feet_offset(start_frame: int, ground_level: float) -> float:
    offsets = []
    for frame in _physics_data["fulcrum_frames"]:
        com = _physics_data["com_positions"].get(frame)
        if com is not None:
            offsets.append(com.z - ground_level)

    median_offset = _median_number(offsets)
    if median_offset is not None and median_offset > 0:
        return median_offset

    first_com = _physics_data["com_positions"].get(start_frame)
    if first_com is not None:
        return max(first_com.z - ground_level, 0.01)

    return 1.0


def _median_vector(values: List[Vector]) -> Optional[Vector]:
    if not values:
        return None
    x = _median_number([value.x for value in values])
    y = _median_number([value.y for value in values])
    z = _median_number([value.z for value in values])
    if x is None or y is None or z is None:
        return None
    return Vector((x, y, z))


def _analyze_frame_states(start_frame: int, end_frame: int, fps: float):
    """Analyze frame states and calculate ballistic predictions with ground collision.

    Determines whether each frame is grounded, airborne, or physically invalid,
    and calculates predicted positions during airborne phases.
    The ghost simulation includes ground collision - once it lands, it stays on ground.
    """
    ground_level = _physics_data.get("detected_ground_level", GROUND_LEVEL)
    gravity = _physics_data.get("gravity", DEFAULT_GRAVITY)
    dt = 1.0 / max(fps, 1.0)
    landing_friction = 0.8 ** (30.0 / max(fps, 1.0))

    com_to_feet_offset = _resolve_com_to_feet_offset(start_frame, ground_level)
    _physics_data["com_to_feet_offset"] = com_to_feet_offset
    min_com_z = ground_level + com_to_feet_offset

    if not _physics_data["fulcrum_frames"]:
        for frame in range(start_frame, min(start_frame + 5, end_frame + 1)):
            vel = _physics_data["com_velocities"].get(frame, Vector((0, 0, 0)))
            if abs(vel.z) < 1.0:
                _physics_data["fulcrum_frames"].add(frame)

    trajectory_start = None
    ghost_pos = None
    ghost_vel = None
    ghost_landed = False
    ghost_landed_pos = None
    ghost_active = False

    def reset_ghost():
        nonlocal trajectory_start, ghost_pos, ghost_vel, ghost_landed, ghost_landed_pos, ghost_active
        trajectory_start = None
        ghost_pos = None
        ghost_vel = None
        ghost_landed = False
        ghost_landed_pos = None
        ghost_active = False

    def start_ghost(frame: int, position: Vector, velocity: Vector):
        nonlocal trajectory_start, ghost_pos, ghost_vel, ghost_landed, ghost_landed_pos, ghost_active
        trajectory_start = frame
        ghost_pos = position.copy()
        ghost_vel = velocity.copy()
        ghost_landed = False
        ghost_landed_pos = None
        ghost_active = True

        _physics_data["trajectory_start_frame"] = frame
        _physics_data["trajectory_start_pos"] = ghost_pos.copy()
        _physics_data["trajectory_start_vel"] = ghost_vel.copy()
        _physics_data["trajectory_launch_frames"].add(frame)
        _physics_data["predicted_positions"][frame] = ghost_pos.copy()

    def step_ghost(frame: int):
        nonlocal ghost_pos, ghost_vel, ghost_landed, ghost_landed_pos
        if ghost_pos is None or ghost_vel is None:
            return None

        if ghost_landed:
            ghost_vel.x *= landing_friction
            ghost_vel.y *= landing_friction
            ghost_landed_pos = ghost_landed_pos + ghost_vel * dt
            ghost_landed_pos.z = min_com_z
            ghost_pos = ghost_landed_pos.copy()
            return ghost_pos.copy()

        ghost_vel.z -= gravity * dt
        new_ghost_pos = ghost_pos + ghost_vel * dt
        if new_ghost_pos.z <= min_com_z:
            new_ghost_pos.z = min_com_z
            ghost_vel.z = 0
            ghost_landed = True
            ghost_landed_pos = new_ghost_pos.copy()
            _physics_data["trajectory_landing_frames"].add(frame)
            _physics_data["trajectory_landing_positions"][frame] = ghost_landed_pos.copy()

        ghost_pos = new_ghost_pos
        return ghost_pos.copy()

    def record_error(frame: int, actual: Vector, predicted: Vector):
        error = (actual - predicted).length
        if error > _physics_data["worst_error"]:
            _physics_data["worst_error"] = error
            _physics_data["worst_frame"] = frame

        t = (frame - trajectory_start) / fps if trajectory_start else 0
        tolerance = 0.1 + t * 0.3
        if error < tolerance:
            _physics_data["frame_states"][frame] = "airborne"
        else:
            _physics_data["frame_states"][frame] = "invalid"
            _physics_data["invalid_frames"].add(frame)

    for frame in range(start_frame, end_frame + 1):
        actual = _physics_data["com_positions"].get(frame)
        if actual is None:
            continue

        is_grounded = frame in _physics_data["fulcrum_frames"] or actual.z <= min_com_z + (GROUND_THRESHOLD * 0.5)

        if is_grounded:
            if ghost_active:
                predicted = step_ghost(frame)
                if predicted is not None:
                    _physics_data["predicted_positions"][frame] = predicted.copy()
                    landing_error = (actual - predicted).length
                    if landing_error >= TRAJECTORY_ERROR_WARN:
                        record_error(frame, actual, predicted)
                    else:
                        _physics_data["frame_states"][frame] = "grounded"
                else:
                    _physics_data["frame_states"][frame] = "grounded"
                    _physics_data["predicted_positions"][frame] = actual.copy()
                reset_ghost()
            else:
                _physics_data["frame_states"][frame] = "grounded"
                _physics_data["predicted_positions"][frame] = actual.copy()
            continue

        launch_frame = frame - 1 if frame - 1 in _physics_data["com_velocities"] else frame
        launch_vel = _physics_data["com_velocities"].get(launch_frame, Vector((0, 0, 0)))
        height_above_ground = actual.z - min_com_z

        if not ghost_active:
            if height_above_ground <= GROUND_THRESHOLD and launch_vel.length < MIN_LAUNCH_SPEED:
                _physics_data["frame_states"][frame] = "grounded"
                _physics_data["predicted_positions"][frame] = actual.copy()
                continue

            start_ghost(frame, actual, launch_vel)
            _physics_data["frame_states"][frame] = "airborne"
            continue

        predicted = step_ghost(frame)
        if predicted is None:
            _physics_data["frame_states"][frame] = "unknown"
            continue

        _physics_data["predicted_positions"][frame] = predicted.copy()
        record_error(frame, actual, predicted)

    if ghost_active and ghost_pos is not None:
        extra_frame = end_frame + 1
        max_extra_frames = int(fps * 5)

        while extra_frame <= end_frame + max_extra_frames:
            predicted = step_ghost(extra_frame)
            if predicted is None:
                break

            _physics_data["predicted_positions"][extra_frame] = predicted.copy()
            _physics_data["frame_states"][extra_frame] = "extrapolated"

            if ghost_landed:
                for slide_frame in range(extra_frame + 1, extra_frame + int(fps * 2) + 1):
                    predicted = step_ghost(slide_frame)
                    if predicted is None:
                        break
                    _physics_data["predicted_positions"][slide_frame] = predicted.copy()
                    _physics_data["frame_states"][slide_frame] = "extrapolated"
                break

            extra_frame += 1


def _get_foot_snapshot(frame: int) -> Dict[str, Vector]:
    snapshots = _physics_data.get("foot_positions", {})
    if frame in snapshots:
        return snapshots[frame]

    if not snapshots:
        return {}

    end_frame = _physics_data.get("end_frame")
    if end_frame in snapshots and frame >= end_frame:
        return snapshots[end_frame]

    nearest_frame = min(snapshots.keys(), key=lambda candidate: abs(candidate - frame))
    return snapshots[nearest_frame]


def _calculate_constraint_proposals(start_frame: int, end_frame: int):
    ground_level = _physics_data.get("detected_ground_level", GROUND_LEVEL)
    root_corrections = {}
    foot_targets = {}

    for frame in _physics_data.get("predicted_positions", {}):
        offset = get_ghost_offset(frame)
        if offset.length > 0.01:
            root_corrections[frame] = offset

    for side, tracks in _physics_data.get("contact_tracks", {}).items():
        for track in tracks:
            track_start = int(track.get("start", start_frame))
            track_end = int(track.get("end", track_start))
            positions = []
            for frame in range(track_start, track_end + 1):
                foot = _physics_data.get("foot_positions", {}).get(frame, {}).get(side)
                if foot is not None:
                    positions.append(foot)

            if not positions:
                continue

            if track.get("kind") == "plant":
                target = _median_vector(positions)
            else:
                target = None

            for frame in range(track_start, track_end + 1):
                foot = _physics_data.get("foot_positions", {}).get(frame, {}).get(side)
                if foot is None:
                    continue
                frame_target = target.copy() if target is not None else foot.copy()
                frame_target.z = ground_level
                foot_targets.setdefault(frame, {})[side] = frame_target

    for frame in _physics_data.get("trajectory_landing_frames", set()):
        offset = get_ghost_offset(frame)
        for side, foot in _get_foot_snapshot(frame).items():
            target = foot + offset
            target.z = ground_level
            foot_targets.setdefault(frame, {})[side] = target

    _physics_data["root_corrections"] = root_corrections
    _physics_data["foot_targets"] = foot_targets


def _resolve_ik_assist_frame(frame: Optional[int]) -> Optional[int]:
    foot_targets = _physics_data.get("foot_targets", {})
    if frame in foot_targets:
        return frame

    focus_frame = get_trajectory_focus_frame()
    if focus_frame in foot_targets:
        return focus_frame

    if frame is not None:
        landing_frame = _get_next_trajectory_landing_frame(frame)
        if landing_frame in foot_targets:
            return landing_frame

    for candidate in sorted(foot_targets.keys()):
        return candidate
    return None


def _control_world_position(control: Dict[str, object]) -> Optional[Vector]:
    target_object = control.get("target_object")
    target_bone = control.get("target_bone")
    if target_object is None:
        return None

    if target_bone is not None:
        return target_object.matrix_world @ target_bone.head

    return target_object.matrix_world.translation.copy()


def _set_pose_bone_head_world(armature: "bpy.types.Object", pose_bone: "bpy.types.PoseBone",
                              world_position: Vector, frame: int) -> bool:
    if all(getattr(pose_bone, "lock_location", (False, False, False))):
        return False

    local_position = armature.matrix_world.inverted() @ world_position
    matrix = pose_bone.matrix.copy()
    matrix.translation = local_position
    pose_bone.matrix = matrix
    bpy.context.view_layer.update()
    pose_bone.keyframe_insert(data_path="location", frame=frame)
    return True


def _set_object_world_position(obj: "bpy.types.Object", world_position: Vector, frame: int) -> bool:
    if all(getattr(obj, "lock_location", (False, False, False))):
        return False

    matrix = obj.matrix_world.copy()
    matrix.translation = world_position
    obj.matrix_world = matrix
    bpy.context.view_layer.update()
    obj.keyframe_insert(data_path="location", frame=frame)
    return True


def _set_control_world_position(control: Dict[str, object], world_position: Vector, frame: int) -> bool:
    target_object = control.get("target_object")
    target_bone = control.get("target_bone")
    if target_object is None:
        return False
    if target_bone is not None:
        return _set_pose_bone_head_world(target_object, target_bone, world_position, frame)
    return _set_object_world_position(target_object, world_position, frame)


def apply_trajectory_ik_assist(
    armature: "bpy.types.Object",
    frame: Optional[int] = None,
    min_confidence: float = IK_ASSIST_MIN_CONFIDENCE,
) -> Dict[str, object]:
    """Move existing IK controls to the current trajectory foot proposals."""
    scene = bpy.context.scene
    if frame is None:
        frame = scene.frame_current

    assist_frame = _resolve_ik_assist_frame(frame)
    controls = detect_trajectory_ik_controls(armature)
    result = {
        "frame": assist_frame,
        "requested_frame": frame,
        "moved": 0,
        "skipped": [],
        "controls": sorted(controls.keys()),
    }

    if assist_frame is None:
        result["skipped"].append("no_target_frame")
        return result
    if not controls:
        result["skipped"].append("no_ik_controls")
        return result

    scene.frame_set(int(assist_frame))
    bpy.context.view_layer.objects.active = armature
    try:
        if armature.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")
    except Exception:
        pass
    bpy.context.view_layer.update()

    targets = _physics_data.get("foot_targets", {}).get(assist_frame, {})
    foot_snapshot = _get_foot_snapshot(assist_frame)
    is_landing = assist_frame in _physics_data.get("trajectory_landing_frames", set())

    for side, target in targets.items():
        control = controls.get(side)
        if control is None:
            result["skipped"].append(f"{side}:no_control")
            continue

        confidence = _contact_confidence(assist_frame, side, 0.0)
        if is_landing:
            confidence = max(confidence, 0.7)
        if confidence < min_confidence:
            result["skipped"].append(f"{side}:low_confidence")
            continue

        foot = foot_snapshot.get(side)
        current_control_pos = _control_world_position(control)
        if foot is None or current_control_pos is None:
            result["skipped"].append(f"{side}:missing_position")
            continue

        desired_control_pos = current_control_pos + (target - foot)
        if (desired_control_pos - current_control_pos).length <= 0.001:
            result["skipped"].append(f"{side}:already_aligned")
            continue

        if _set_control_world_position(control, desired_control_pos, int(assist_frame)):
            result["moved"] += 1
        else:
            result["skipped"].append(f"{side}:locked")

    bpy.context.view_layer.update()
    return result


def get_trajectory_ik_assist_summary(armature: Optional["bpy.types.Object"] = None) -> Dict[str, object]:
    """Return compact ik-assist state for icon-only UI."""
    if armature is None:
        armature_name = _physics_data.get("armature_name")
        armature = get_object_by_name(armature_name) if armature_name else None

    controls = detect_trajectory_ik_controls(armature) if armature else {}
    frame = bpy.context.scene.frame_current
    assist_frame = _resolve_ik_assist_frame(frame)
    targets = _physics_data.get("foot_targets", {}).get(assist_frame, {}) if assist_frame is not None else {}
    apply_sides = []
    for side in CONTACT_SIDES:
        if side not in controls or side not in targets:
            continue
        confidence = _contact_confidence(assist_frame, side, 0.0)
        if assist_frame in _physics_data.get("trajectory_landing_frames", set()):
            confidence = max(confidence, 0.7)
        if confidence >= IK_ASSIST_MIN_CONFIDENCE:
            apply_sides.append(side)

    return {
        "has_controls": bool(controls),
        "control_sides": sorted(controls.keys()),
        "target_frame": assist_frame,
        "target_sides": sorted(targets.keys()),
        "apply_sides": apply_sides,
        "can_apply": bool(apply_sides),
    }


def get_ghost_offset(frame: int) -> Vector:
    """Calculate the offset needed to move character to physics-correct position.

    Args:
        frame: The frame to calculate offset for.

    Returns:
        World-space offset vector (predicted - actual).
    """
    if frame not in _physics_data["predicted_positions"]:
        return Vector((0, 0, 0))

    predicted = _physics_data["predicted_positions"][frame]

    # For frames within animation range, use actual COM
    if frame in _physics_data["com_positions"]:
        actual = _physics_data["com_positions"][frame]
    else:
        # For extrapolated frames (beyond animation), use the last known COM position
        # This shows where the ghost continues vs where the character is "stuck"
        end_frame = _physics_data.get("end_frame", 0)
        if end_frame in _physics_data["com_positions"]:
            actual = _physics_data["com_positions"][end_frame]
        else:
            return Vector((0, 0, 0))

    return predicted - actual


def _capture_bone_positions(armature: "bpy.types.Object") -> Dict[str, Tuple[Vector, Vector]]:
    return {
        pose_bone.name: (
            armature.matrix_world @ pose_bone.head,
            armature.matrix_world @ pose_bone.tail,
        )
        for pose_bone in armature.pose.bones
    }


def _get_bone_snapshot(frame: int) -> Optional[Dict[str, Tuple[Vector, Vector]]]:
    snapshots = _physics_data.get("bone_positions", {})
    if frame in snapshots:
        return snapshots[frame]

    if not snapshots:
        return None

    end_frame = _physics_data.get("end_frame")
    if end_frame in snapshots and frame >= end_frame:
        return snapshots[end_frame]

    nearest_frame = min(snapshots.keys(), key=lambda candidate: abs(candidate - frame))
    return snapshots[nearest_frame]


def _offset_bone_snapshot(source_bones: Dict[str, Tuple[Vector, Vector]],
                          offset: Vector) -> Dict[str, Tuple[Vector, Vector]]:
    ghost_bones = {}
    ground_level = _physics_data.get("detected_ground_level", GROUND_LEVEL)
    min_bone_z = float('inf')

    for head_world, tail_world in source_bones.values():
        min_bone_z = min(min_bone_z, head_world.z, tail_world.z)

    if min_bone_z == float('inf'):
        return ghost_bones

    clamped_offset = offset.copy()
    ghost_min_z = min_bone_z + offset.z
    if ghost_min_z < ground_level:
        clamped_offset.z = ground_level - min_bone_z

    for bone_name, (head_world, tail_world) in source_bones.items():
        ghost_bones[bone_name] = (
            head_world + clamped_offset,
            tail_world + clamped_offset,
        )

    return ghost_bones


def calculate_ghost_bones(armature: "bpy.types.Object", offset: Vector,
                          frame: Optional[int] = None) -> Dict[str, Tuple[Vector, Vector]]:
    """Calculate ghost bone positions by offsetting a sampled pose.

    Args:
        armature: The armature object.
        offset: World-space offset to apply.
        frame: Animation frame whose pose should be ghosted. Defaults to current pose.

    Returns:
        Dict mapping bone name to (head_world, tail_world) tuple.
    """
    source_bones = _get_bone_snapshot(frame) if frame is not None else _capture_bone_positions(armature)
    if source_bones is None:
        return {}

    return _offset_bone_snapshot(source_bones, offset)


def _get_ghost_guide_frames(current_frame: int) -> List[int]:
    predicted_positions = _physics_data.get("predicted_positions", {})
    if not predicted_positions:
        return []

    fps = max(float(_physics_data.get("fps", 30.0) or 30.0), 1.0)
    frames = []
    for seconds in GHOST_ONION_SECONDS:
        frame = current_frame + int(round(seconds * fps))
        if frame in predicted_positions and frame not in frames and _is_ghost_guide_frame(frame):
            frames.append(frame)

    if len(frames) < GHOST_MAX_ONIONS:
        landing_frame = _get_next_trajectory_landing_frame(current_frame)
        if landing_frame is not None and landing_frame not in frames:
            frames.append(landing_frame)

    return frames[:GHOST_MAX_ONIONS]


def _is_ghost_guide_frame(frame: int) -> bool:
    state = _physics_data["frame_states"].get(frame, "unknown")
    if state in {"airborne", "invalid", "extrapolated"}:
        return True
    if frame in _physics_data.get("trajectory_landing_frames", set()):
        return True
    return get_physics_error(frame) > 0.01


def _get_next_trajectory_landing_frame(current_frame: int) -> Optional[int]:
    landing_frames = sorted(
        frame for frame in _physics_data.get("trajectory_landing_frames", set())
        if frame >= current_frame
    )
    if landing_frames:
        return landing_frames[0]
    return None


def _ghost_color_for_frame(frame: int, alpha: float):
    state = _physics_data["frame_states"].get(frame, "unknown")
    if state == "invalid":
        base = _physics_data["invalid_color"]
    elif state == "grounded":
        base = _physics_data["grounded_color"]
    elif state == "airborne":
        base = _physics_data["airborne_color"]
    else:
        base = _physics_data["ghost_color"]
    return (base[0], base[1], base[2], alpha)


def _draw_physics_callback():
    """OpenGL callback to draw physics visualization."""
    if not _physics_data["enabled"]:
        return

    armature_name = _physics_data.get("armature_name")
    if not armature_name:
        return

    armature = get_object_by_name(armature_name)
    if not armature or armature.type != "ARMATURE":
        _physics_data["enabled"] = False
        return

    frame = bpy.context.scene.frame_current

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    # Draw ground plane reference
    if _physics_data.get("show_ground_plane", True):
        _draw_ground_plane(shader)

    # Draw ballistic trajectory
    _draw_trajectory(shader, frame)
    _draw_trajectory_landmarks(shader)

    # Draw COM marker on current position
    if _physics_data.get("show_com_marker", True):
        _draw_com_marker(shader, frame)

    # Draw ghost character
    if _physics_data["show_ghost"]:
        _draw_ghost_guides(shader, armature, frame)
        _draw_constraint_proposals(shader, frame)
        offset = get_ghost_offset(frame)
        if offset.length > 0.01:
            _draw_error_line(shader, frame, offset)

    # Draw fulcrum points
    _draw_fulcrum_points(shader, frame)

    gpu.state.blend_set('NONE')


def _draw_ground_plane(shader):
    """Draw a reference grid at ground level."""
    ground_level = _physics_data.get("detected_ground_level", GROUND_LEVEL)

    # Draw a simple grid
    size = 2.0
    divisions = 4
    step = size / divisions

    vertices = []
    for i in range(-divisions, divisions + 1):
        # Lines along X
        vertices.extend([
            (-size, i * step, ground_level),
            (size, i * step, ground_level),
        ])
        # Lines along Y
        vertices.extend([
            (i * step, -size, ground_level),
            (i * step, size, ground_level),
        ])

    if vertices:
        batch = batch_for_shader(shader, 'LINES', {"pos": vertices})
        shader.bind()
        shader.uniform_float("color", _physics_data["ground_plane_color"])
        gpu.state.line_width_set(1.0)
        batch.draw(shader)


def _draw_com_marker(shader, frame: int):
    """Draw a marker at the current COM position."""
    if frame not in _physics_data["com_positions"]:
        return

    pos = _physics_data["com_positions"][frame]
    size = 0.08

    # Draw a 3D cross
    vertices = [
        (pos.x - size, pos.y, pos.z), (pos.x + size, pos.y, pos.z),
        (pos.x, pos.y - size, pos.z), (pos.x, pos.y + size, pos.z),
        (pos.x, pos.y, pos.z - size), (pos.x, pos.y, pos.z + size),
    ]

    batch = batch_for_shader(shader, 'LINES', {"pos": vertices})
    shader.bind()
    shader.uniform_float("color", _physics_data["com_marker_color"])
    gpu.state.line_width_set(3.0)
    batch.draw(shader)
    gpu.state.line_width_set(1.0)


def _draw_error_line(shader, frame: int, offset: Vector):
    """Draw a line showing the physics error (actual to predicted)."""
    if frame not in _physics_data["com_positions"]:
        return

    actual = _physics_data["com_positions"][frame]
    predicted = actual + offset

    vertices = [
        (actual.x, actual.y, actual.z),
        (predicted.x, predicted.y, predicted.z),
    ]

    # Color based on error magnitude
    error = offset.length
    if error < TRAJECTORY_ERROR_WARN:
        color = _physics_data["grounded_color"]  # Green - small error
    elif error < TRAJECTORY_ERROR_BAD:
        color = _physics_data["airborne_color"]  # Orange - medium error
    else:
        color = _physics_data["invalid_color"]  # Red - large error

    batch = batch_for_shader(shader, 'LINES', {"pos": vertices})
    shader.bind()
    shader.uniform_float("color", color)
    gpu.state.line_width_set(2.0)
    batch.draw(shader)


def _draw_line_vertices(shader, vertices, color, line_width: float):
    if not vertices:
        return

    batch = batch_for_shader(shader, 'LINES', {"pos": vertices})
    shader.bind()
    shader.uniform_float("color", color)
    gpu.state.line_width_set(line_width)
    batch.draw(shader)


def _trajectory_state_color(frame: int):
    state = _physics_data["frame_states"].get(frame, "unknown")
    if state == "invalid":
        return _physics_data["invalid_color"]
    if state == "extrapolated":
        return _physics_data["trajectory_color"]
    if state == "airborne":
        return _physics_data["airborne_color"]
    if state == "grounded":
        return _physics_data["grounded_color"]
    return _physics_data["trajectory_color"]


def _ring_vertices(center: Vector, radius: float, segments: int = TRAJECTORY_MARKER_SEGMENTS):
    vertices = []
    for i in range(segments):
        a0 = (i / segments) * math.tau
        a1 = ((i + 1) / segments) * math.tau
        vertices.extend([
            (center.x + math.cos(a0) * radius, center.y + math.sin(a0) * radius, center.z),
            (center.x + math.cos(a1) * radius, center.y + math.sin(a1) * radius, center.z),
        ])
    return vertices


def _draw_ring(shader, center: Vector, radius: float, color, line_width: float = 2.0):
    _draw_line_vertices(shader, _ring_vertices(center, radius), color, line_width)


def _draw_pin(shader, center: Vector, color, radius: float, height: float):
    top = center + Vector((0, 0, height))
    vertices = [
        (center.x, center.y, center.z),
        (top.x, top.y, top.z),
        (top.x - radius, top.y, top.z),
        (top.x + radius, top.y, top.z),
        (top.x, top.y - radius, top.z),
        (top.x, top.y + radius, top.z),
    ]
    _draw_line_vertices(shader, vertices, color, 3.0)
    _draw_ring(shader, center, radius, color, 2.0)


def _draw_target(shader, center: Vector, color, radius: float):
    _draw_ring(shader, center, radius, color, 3.0)
    _draw_ring(shader, center, radius * 0.55, color, 2.0)
    vertices = [
        (center.x - radius, center.y, center.z),
        (center.x + radius, center.y, center.z),
        (center.x, center.y - radius, center.z),
        (center.x, center.y + radius, center.z),
    ]
    _draw_line_vertices(shader, vertices, color, 2.0)


def _draw_trajectory_landmarks(shader):
    launch_color = _physics_data["launch_color"]
    landing_color = _physics_data["landing_color"]
    focus_color = _physics_data["focus_color"]

    for frame in _physics_data["trajectory_launch_frames"]:
        pos = _physics_data["predicted_positions"].get(frame)
        if pos is None:
            pos = _physics_data["com_positions"].get(frame)
        if pos is not None:
            _draw_pin(shader, pos, launch_color, 0.12, 0.35)

    for frame in _physics_data["trajectory_landing_frames"]:
        pos = _physics_data["trajectory_landing_positions"].get(frame)
        if pos is not None:
            _draw_target(shader, pos, landing_color, 0.18)

    focus_frame = get_trajectory_focus_frame()
    if focus_frame is None:
        return

    actual = _physics_data["com_positions"].get(focus_frame)
    predicted = _physics_data["predicted_positions"].get(focus_frame)
    if actual is not None and predicted is not None:
        _draw_target(shader, actual, focus_color, 0.14)
        _draw_ring(shader, predicted, 0.11, _physics_data["trajectory_color"], 2.0)
        _draw_line_vertices(
            shader,
            [(actual.x, actual.y, actual.z), (predicted.x, predicted.y, predicted.z)],
            focus_color,
            3.0,
        )


def _color_with_alpha(color, alpha: float):
    return (color[0], color[1], color[2], _clamp01(alpha))


def _contact_confidence(frame: int, side: str, fallback: float = 0.65) -> float:
    return float(
        _physics_data.get("contact_confidence", {})
        .get(frame, {})
        .get(side, fallback)
    )


def _draw_footprint(shader, center: Vector, color, radius: float, line_width: float):
    heel = center + Vector((0, -radius * 0.55, 0))
    toe = center + Vector((0, radius * 0.55, 0))
    _draw_ring(shader, heel, radius * 0.42, color, line_width)
    _draw_ring(shader, toe, radius * 0.52, color, line_width)
    _draw_line_vertices(
        shader,
        [(heel.x, heel.y, heel.z), (toe.x, toe.y, toe.z)],
        color,
        line_width,
    )


def _draw_root_correction(shader, frame: int):
    offset = _physics_data.get("root_corrections", {}).get(frame)
    actual = _physics_data.get("com_positions", {}).get(frame)
    if offset is None or actual is None or offset.length <= 0.01:
        return

    target = actual + offset
    color = _physics_data["root_correction_color"]
    _draw_pin(shader, target, color, 0.075, 0.24)
    _draw_line_vertices(
        shader,
        [(actual.x, actual.y, actual.z), (target.x, target.y, target.z)],
        color,
        3.0,
    )


def _draw_foot_targets_for_frame(shader, frame: int, current_frame: int, future: bool = False):
    targets = _physics_data.get("foot_targets", {}).get(frame, {})
    if not targets:
        return

    foot_snapshot = _get_foot_snapshot(frame)
    is_landing = frame in _physics_data.get("trajectory_landing_frames", set())
    for side, target in targets.items():
        confidence = _contact_confidence(frame, side, 0.7 if is_landing else 0.55)
        if is_landing:
            confidence = max(confidence, 0.7)
        contact_kind = _physics_data.get("contact_state", {}).get(frame, {}).get(side, "swing")
        if confidence < 0.35:
            color = _physics_data["low_confidence_color"]
        elif is_landing or future:
            color = _physics_data["foot_target_color"]
        elif contact_kind == "plant":
            color = _physics_data["foot_lock_color"]
        else:
            color = _physics_data["airborne_color"]

        alpha_scale = 0.65 if future and frame != current_frame else 1.0
        draw_color = _color_with_alpha(color, max(0.2, confidence) * alpha_scale)
        _draw_footprint(shader, target, draw_color, FOOT_TARGET_RADIUS, 3.0 if is_landing else 2.2)

        foot = foot_snapshot.get(side)
        if foot is not None and (foot - target).length > 0.035:
            _draw_line_vertices(
                shader,
                [(foot.x, foot.y, foot.z), (target.x, target.y, target.z)],
                draw_color,
                2.0,
            )


def _draw_constraint_proposals(shader, current_frame: int):
    _draw_root_correction(shader, current_frame)
    _draw_foot_targets_for_frame(shader, current_frame, current_frame)

    landing_frame = _get_next_trajectory_landing_frame(current_frame)
    if landing_frame is not None and landing_frame != current_frame:
        _draw_foot_targets_for_frame(shader, landing_frame, current_frame, future=True)


def _draw_trajectory(shader, current_frame: int):
    """Draw the ballistic trajectory curves - both actual and predicted."""
    if not _physics_data["predicted_positions"]:
        return

    # Draw trajectory for frames around current (extended range to show landing)
    frames = sorted(_physics_data["predicted_positions"].keys())
    view_behind = TRAJECTORY_VIEW_BEHIND
    view_ahead = TRAJECTORY_VIEW_AHEAD

    # Draw ACTUAL COM path (white/cyan)
    actual_vertices = []
    for i, frame in enumerate(frames):
        if frame < current_frame - view_behind or frame > current_frame + view_ahead:
            continue

        pos = _physics_data["com_positions"].get(frame)
        if pos is None:
            continue

        if i > 0 and frames[i - 1] >= current_frame - view_behind:
            prev_pos = _physics_data["com_positions"].get(frames[i - 1])
            if prev_pos is not None:
                actual_vertices.extend([(prev_pos.x, prev_pos.y, prev_pos.z),
                                       (pos.x, pos.y, pos.z)])

    if actual_vertices:
        _draw_line_vertices(shader, actual_vertices, _physics_data["actual_path_color"], 2.0)

    # Draw predicted physics path as a traffic-light arc.
    predicted_by_color = {}
    for i, frame in enumerate(frames):
        if frame < current_frame - view_behind or frame > current_frame + view_ahead:
            continue

        pos = _physics_data["predicted_positions"][frame]

        if i > 0 and frames[i - 1] >= current_frame - view_behind:
            prev_pos = _physics_data["predicted_positions"].get(frames[i - 1])
            if prev_pos is not None and frames[i - 1] == frame - 1:
                color = _trajectory_state_color(frame)
                predicted_by_color.setdefault(color, []).extend([
                    (prev_pos.x, prev_pos.y, prev_pos.z),
                    (pos.x, pos.y, pos.z),
                ])

    for color, vertices in predicted_by_color.items():
        _draw_line_vertices(shader, vertices, color, 3.0)

    gpu.state.line_width_set(1.0)


def _draw_ghost_guides(shader, armature: "bpy.types.Object", current_frame: int):
    guide_frames = _get_ghost_guide_frames(current_frame)
    if not guide_frames:
        return

    for index, frame in enumerate(reversed(guide_frames)):
        offset = get_ghost_offset(frame)
        if frame == current_frame and offset.length <= 0.01:
            continue

        ghost_bones = calculate_ghost_bones(armature, offset, frame)
        if not ghost_bones:
            continue

        age = len(guide_frames) - index - 1
        alpha = max(0.18, 0.58 - age * 0.12)
        line_width = max(1.5, 3.5 - age * 0.5)
        _draw_ghost_armature(shader, ghost_bones, _ghost_color_for_frame(frame, alpha), line_width)

    landing_frame = _get_next_trajectory_landing_frame(current_frame)
    if landing_frame is None:
        return

    offset = get_ghost_offset(landing_frame)
    ghost_bones = calculate_ghost_bones(armature, offset, landing_frame)
    if ghost_bones:
        landing_color = _physics_data["landing_color"]
        _draw_ghost_armature(shader, ghost_bones, (landing_color[0], landing_color[1], landing_color[2], 0.55), 4.0)


def _draw_ghost_armature(shader, ghost_bones: Dict[str, Tuple[Vector, Vector]], color=None, line_width: float = 3.0):
    """Draw the ghost armature as lines."""
    vertices = []

    for bone_name, (head, tail) in ghost_bones.items():
        # Skip IK helper bones
        if any(s in bone_name for s in ["-IKTarget", "-IKPole", "-IKStretch"]):
            continue

        vertices.extend([
            (head.x, head.y, head.z),
            (tail.x, tail.y, tail.z)
        ])

    if vertices:
        batch = batch_for_shader(shader, 'LINES', {"pos": vertices})
        shader.bind()
        shader.uniform_float("color", color or _physics_data["ghost_color"])
        gpu.state.line_width_set(line_width)
        batch.draw(shader)
        gpu.state.line_width_set(1.0)


def _draw_fulcrum_points(shader, frame: int):
    """Draw fulcrum (ground contact) points."""
    if frame not in _physics_data["fulcrum_positions"]:
        return

    contacts = _physics_data["fulcrum_positions"][frame]
    vertices = []

    size = 0.1
    for contact in contacts:
        # Handle both old format (Vector) and new format (Vector, side)
        if isinstance(contact, tuple):
            pos, side = contact
        else:
            pos = contact

        # Draw small cross at contact point
        vertices.extend([
            (pos.x - size, pos.y, pos.z),
            (pos.x + size, pos.y, pos.z),
            (pos.x, pos.y - size, pos.z),
            (pos.x, pos.y + size, pos.z),
        ])

    if vertices:
        batch = batch_for_shader(shader, 'LINES', {"pos": vertices})
        shader.bind()
        shader.uniform_float("color", _physics_data["grounded_color"])
        gpu.state.line_width_set(2.0)
        batch.draw(shader)
        gpu.state.line_width_set(1.0)


def enable_physics_visualization(enable: bool = True):
    """Enable or disable physics visualization."""
    global _physics_draw_handler

    _physics_data["enabled"] = enable

    if enable and _physics_draw_handler is None:
        _physics_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_physics_callback, (), 'WINDOW', 'POST_VIEW'
        )
    elif not enable and _physics_draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_physics_draw_handler, 'WINDOW')
        _physics_draw_handler = None
        _physics_data["armature_name"] = None

        # Clear data to free memory when disabled
        _physics_data["com_positions"] = {}
        _physics_data["com_velocities"] = {}
        _physics_data["bone_positions"] = {}
        _physics_data["foot_positions"] = {}
        _physics_data["foot_velocities"] = {}
        _physics_data["frame_states"] = {}
        _physics_data["fulcrum_frames"] = set()
        _physics_data["fulcrum_positions"] = {}
        _physics_data["predicted_positions"] = {}
        _physics_data["root_corrections"] = {}
        _physics_data["foot_targets"] = {}
        _physics_data["contact_count"] = {}
        _physics_data["contact_tracks"] = {"left": [], "right": []}
        _physics_data["contact_state"] = {}
        _physics_data["contact_confidence"] = {}
        _physics_data["trajectory_launch_frames"] = set()
        _physics_data["trajectory_landing_frames"] = set()
        _physics_data["trajectory_landing_positions"] = {}
        _physics_data["invalid_frames"] = set()
        _physics_data["worst_frame"] = None
        _physics_data["worst_error"] = 0.0

    # Redraw viewports
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def is_physics_enabled() -> bool:
    """Check if physics visualization is enabled."""
    return _physics_data["enabled"]


def toggle_ghost(enable: Optional[bool] = None):
    """Toggle ghost character display."""
    if enable is None:
        _physics_data["show_ghost"] = not _physics_data["show_ghost"]
    else:
        _physics_data["show_ghost"] = enable

    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def is_ghost_enabled() -> bool:
    """Check if ghost display is enabled."""
    return _physics_data["show_ghost"]


def get_frame_state(frame: int) -> str:
    """Get the physics state of a frame.

    Returns:
        "grounded", "airborne", "invalid", or "unknown"
    """
    return _physics_data["frame_states"].get(frame, "unknown")


def get_physics_error(frame: int) -> float:
    """Get the physics error (distance between actual and predicted) for a frame.

    Returns:
        Error distance in Blender units, or 0 if no prediction.
    """
    if frame not in _physics_data["predicted_positions"]:
        return 0.0
    if frame not in _physics_data["com_positions"]:
        return 0.0

    predicted = _physics_data["predicted_positions"][frame]
    actual = _physics_data["com_positions"][frame]

    return (actual - predicted).length


def get_trajectory_focus_frame() -> Optional[int]:
    """Return the frame the user is most likely to need next."""
    invalid_frames = sorted(_physics_data.get("invalid_frames", set()))
    if invalid_frames:
        worst_frame = _physics_data.get("worst_frame")
        if worst_frame in invalid_frames:
            return worst_frame
        return invalid_frames[0]

    worst_frame = _physics_data.get("worst_frame")
    if worst_frame is not None and _physics_data.get("worst_error", 0.0) >= TRAJECTORY_ERROR_WARN:
        return worst_frame

    landing_frames = sorted(_physics_data.get("trajectory_landing_frames", set()))
    if landing_frames:
        return landing_frames[0]

    launch_frames = sorted(_physics_data.get("trajectory_launch_frames", set()))
    if launch_frames:
        return launch_frames[0]

    return None


def get_trajectory_summary() -> Dict[str, object]:
    """Return compact state for UI controls without requiring visible prose."""
    frame = bpy.context.scene.frame_current
    state = get_frame_state(frame)
    focus_frame = get_trajectory_focus_frame()
    has_analysis = bool(_physics_data.get("predicted_positions"))
    has_issue = bool(_physics_data.get("invalid_frames"))
    return {
        "enabled": bool(_physics_data.get("enabled")),
        "has_analysis": has_analysis,
        "has_issue": has_issue,
        "frame": frame,
        "state": state,
        "error": get_physics_error(frame),
        "focus_frame": focus_frame,
        "worst_error": float(_physics_data.get("worst_error") or 0.0),
        "launch_count": len(_physics_data.get("trajectory_launch_frames", set())),
        "landing_count": len(_physics_data.get("trajectory_landing_frames", set())),
    }


def update_physics_frame():
    """Called on frame change to update visualization."""
    if not _physics_data["enabled"]:
        return

    # Redraw viewports
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


# Frame change handler
def _physics_frame_handler(scene):
    """Handler for frame changes."""
    update_physics_frame()


def register_physics_frame_handler():
    """Register the frame change handler."""
    if _physics_frame_handler not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_physics_frame_handler)


def unregister_physics_frame_handler():
    """Unregister the frame change handler."""
    if _physics_frame_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_physics_frame_handler)


def cleanup_physics():
    """Clean up all physics resources to prevent memory leaks.

    Call this on addon unregister or when completely done with physics.
    """
    global _physics_draw_handler

    # Remove draw handler
    if _physics_draw_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_physics_draw_handler, 'WINDOW')
        except Exception:
            pass
        _physics_draw_handler = None

    # Unregister frame handler
    unregister_physics_frame_handler()

    # Clear all data to free memory
    _physics_data["enabled"] = False
    _physics_data["armature_name"] = None
    _physics_data["com_positions"] = {}
    _physics_data["com_velocities"] = {}
    _physics_data["bone_positions"] = {}
    _physics_data["foot_positions"] = {}
    _physics_data["foot_velocities"] = {}
    _physics_data["frame_states"] = {}
    _physics_data["fulcrum_frames"] = set()
    _physics_data["fulcrum_positions"] = {}
    _physics_data["predicted_positions"] = {}
    _physics_data["root_corrections"] = {}
    _physics_data["foot_targets"] = {}
    _physics_data["contact_tracks"] = {"left": [], "right": []}
    _physics_data["contact_state"] = {}
    _physics_data["contact_confidence"] = {}
    _physics_data["contact_count"] = {}
    _physics_data["trajectory_start_frame"] = None
    _physics_data["trajectory_start_pos"] = None
    _physics_data["trajectory_start_vel"] = None
    _physics_data["trajectory_launch_frames"] = set()
    _physics_data["trajectory_landing_frames"] = set()
    _physics_data["trajectory_landing_positions"] = {}
    _physics_data["invalid_frames"] = set()
    _physics_data["worst_frame"] = None
    _physics_data["worst_error"] = 0.0
