"""
Physics-based animation analysis and visualization.

Provides AutoPhysics-like features for analyzing animation physical validity,
including ballistic trajectory prediction, fulcrum point detection, and
ghost character visualization showing physics-corrected positions.
"""

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector, Matrix
from typing import Optional, Dict, List, Tuple
import math

from .com import calculate_com


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


def get_gravity() -> float:
    """Get gravity value from scene settings or use default."""
    try:
        settings = bpy.context.scene.rbx_anim_settings
        return settings.rbx_physics_gravity
    except:
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
    
    # Fulcrum detection
    "fulcrum_frames": set(),  # Frames where character has ground contact
    "fulcrum_positions": {},  # frame -> list of contact points
    "contact_count": {},  # frame -> number of contacts (0, 1, or 2)
    
    # Ballistic trajectory
    "trajectory_start_frame": None,
    "trajectory_start_pos": None,
    "trajectory_start_vel": None,
    "predicted_positions": {},  # frame -> predicted COM position
    
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
    
    fps = scene.render.fps
    _physics_data["fps"] = fps
    _physics_data["armature_name"] = armature.name
    _physics_data["gravity"] = get_gravity()  # Store current gravity setting
    _physics_data["start_frame"] = start_frame
    _physics_data["end_frame"] = end_frame
    
    # Clear ALL previous data completely
    _physics_data["com_positions"] = {}
    _physics_data["com_velocities"] = {}
    _physics_data["frame_states"] = {}
    _physics_data["fulcrum_frames"] = set()
    _physics_data["fulcrum_positions"] = {}
    _physics_data["predicted_positions"] = {}
    _physics_data["contact_count"] = {}
    
    # Reset trajectory tracking
    _physics_data["trajectory_start_frame"] = None
    _physics_data["trajectory_start_pos"] = None
    _physics_data["trajectory_start_vel"] = None
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
        
        # Detect fulcrum points (feet on ground)
        fulcrums = detect_fulcrum_points(armature)
        _physics_data["contact_count"][frame] = len(fulcrums)
        if fulcrums:
            _physics_data["fulcrum_frames"].add(frame)
            _physics_data["fulcrum_positions"][frame] = fulcrums
    
    # Second pass: calculate smoothed velocities
    _calculate_smoothed_velocities(start_frame, end_frame, fps)
    
    # Third pass: determine frame states and calculate predictions
    _analyze_frame_states(start_frame, end_frame, fps)
    
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


def detect_fulcrum_points(armature: "bpy.types.Object") -> List[Tuple[Vector, str]]:
    """Detect ground contact points for the current pose.
    
    Looks for foot bones that are close to their minimum height,
    accounting for the fact that ankle joints are above the ground.
    
    Args:
        armature: The armature to check.
        
    Returns:
        List of (world_position, side) tuples where side is "left" or "right".
    """
    contacts = []
    ground_level = _physics_data.get("detected_ground_level", GROUND_LEVEL)
    
    # Track which sides have contact (to avoid duplicate left/right)
    left_contact = None
    right_contact = None
    left_min_z = float('inf')
    right_min_z = float('inf')
    
    # Patterns for foot bone detection (Roblox uses LeftFoot, RightFoot)
    left_patterns = ["leftfoot", "left_foot", "foot_l", "foot.l", "l_foot", "l.foot",
                     "lefttoes", "toes_l", "toes.l", "l_toes", "l.toes", "left foot"]
    right_patterns = ["rightfoot", "right_foot", "foot_r", "foot.r", "r_foot", "r.foot",
                      "righttoes", "toes_r", "toes.r", "r_toes", "r.toes", "right foot"]
    
    for pose_bone in armature.pose.bones:
        bone_name_lower = pose_bone.name.lower()
        
        # Determine side
        is_left = any(pat in bone_name_lower for pat in left_patterns)
        is_right = any(pat in bone_name_lower for pat in right_patterns)
        
        if not is_left and not is_right:
            continue
        
        # Get foot position in world space - use tail for toe bones (lower point)
        if "toe" in bone_name_lower:
            foot_pos = armature.matrix_world @ pose_bone.tail
        else:
            foot_pos = armature.matrix_world @ pose_bone.head
        
        # Track the lowest contact point per side
        if is_left and foot_pos.z < left_min_z:
            left_min_z = foot_pos.z
            if foot_pos.z <= ground_level + GROUND_THRESHOLD:
                left_contact = foot_pos.copy()
        elif is_right and foot_pos.z < right_min_z:
            right_min_z = foot_pos.z
            if foot_pos.z <= ground_level + GROUND_THRESHOLD:
                right_contact = foot_pos.copy()
    
    # Return contacts with side info
    if left_contact:
        contacts.append((left_contact, "left"))
    if right_contact:
        contacts.append((right_contact, "right"))
    
    return contacts


def _detect_ground_level(armature: "bpy.types.Object", start_frame: int, end_frame: int, scene) -> float:
    """Detect the ground level by finding the minimum foot/toe height in the animation.
    
    This accounts for the fact that ankle joints are above the actual ground plane.
    """
    min_height = float('inf')
    
    foot_names = ["leftfoot", "rightfoot", "left_foot", "right_foot", 
                  "foot_l", "foot_r", "foot.l", "foot.r",
                  "lefttoes", "righttoes", "toes_l", "toes_r"]
    
    # Sample a subset of frames for efficiency
    sample_step = max(1, (end_frame - start_frame) // 20)
    
    for frame in range(start_frame, end_frame + 1, sample_step):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        
        for pose_bone in armature.pose.bones:
            bone_name_lower = pose_bone.name.lower()
            
            is_foot = any(foot in bone_name_lower for foot in foot_names)
            if not is_foot:
                continue
            
            # Use tail for toes (lower point), head for feet
            if "toe" in bone_name_lower:
                pos = armature.matrix_world @ pose_bone.tail
            else:
                pos = armature.matrix_world @ pose_bone.head
            
            min_height = min(min_height, pos.z)
    
    # If we couldn't find foot bones, default to 0
    if min_height == float('inf'):
        return GROUND_LEVEL
    
    return min_height


def _analyze_frame_states(start_frame: int, end_frame: int, fps: float):
    """Analyze frame states and calculate ballistic predictions with ground collision.
    
    Determines whether each frame is grounded, airborne, or physically invalid,
    and calculates predicted positions during airborne phases.
    The ghost simulation includes ground collision - once it lands, it stays on ground.
    """
    trajectory_start = None
    ground_level = _physics_data.get("detected_ground_level", GROUND_LEVEL)
    gravity = _physics_data.get("gravity", DEFAULT_GRAVITY)
    
    # Estimate the height offset from COM to feet (so we know when feet hit ground)
    # This is the distance from COM to the lowest point of the character
    com_to_feet_offset = None
    for frame in _physics_data["fulcrum_frames"]:
        if frame in _physics_data["com_positions"]:
            com_z = _physics_data["com_positions"][frame].z
            com_to_feet_offset = com_z - ground_level
            break
    
    if com_to_feet_offset is None:
        # Fallback: estimate from first frame
        first_com = _physics_data["com_positions"].get(start_frame)
        com_to_feet_offset = first_com.z - ground_level if first_com else 1.0
    
    # Store this for ghost bone clamping
    _physics_data["com_to_feet_offset"] = com_to_feet_offset
    
    # The minimum COM Z when standing on ground
    min_com_z = ground_level + com_to_feet_offset
    
    # If no fulcrum frames detected at all, check if first few frames have low velocity
    # and assume they're grounded (common for animations starting from idle)
    if not _physics_data["fulcrum_frames"]:
        for frame in range(start_frame, min(start_frame + 5, end_frame + 1)):
            vel = _physics_data["com_velocities"].get(frame, Vector((0, 0, 0)))
            # If vertical velocity is near zero, assume grounded
            if abs(vel.z) < 1.0:  # Low vertical velocity threshold
                _physics_data["fulcrum_frames"].add(frame)
    
    # Track ghost simulation state separately from animation state
    ghost_pos = None  # Current simulated ghost position
    ghost_vel = None  # Current simulated ghost velocity  
    ghost_landed = False  # Has ghost hit the ground?
    ghost_landed_pos = None  # Position where ghost landed
    ghost_active = False  # Is a ghost simulation currently running?
    
    for frame in range(start_frame, end_frame + 1):
        is_grounded = frame in _physics_data["fulcrum_frames"]
        
        # If ghost is active (a jump has started), continue physics regardless of animation state
        if ghost_active:
            dt = 1.0 / fps
            
            if ghost_landed:
                # Ghost has landed - continue sliding with friction
                ghost_vel.x *= 0.8
                ghost_vel.y *= 0.8
                ghost_landed_pos = ghost_landed_pos + ghost_vel * dt
                ghost_landed_pos.z = min_com_z
                _physics_data["predicted_positions"][frame] = ghost_landed_pos.copy()
            else:
                # Ghost still airborne - continue physics
                ghost_vel.z -= gravity * dt
                new_ghost_pos = ghost_pos + ghost_vel * dt
                
                # Check for ground collision
                if new_ghost_pos.z <= min_com_z:
                    new_ghost_pos.z = min_com_z
                    ghost_vel.z = 0
                    ghost_landed = True
                    ghost_landed_pos = new_ghost_pos.copy()
                
                ghost_pos = new_ghost_pos
                _physics_data["predicted_positions"][frame] = ghost_pos.copy()
            
            # Set frame state based on animation vs physics comparison
            if is_grounded:
                _physics_data["frame_states"][frame] = "grounded"
            else:
                actual = _physics_data["com_positions"][frame]
                predicted = _physics_data["predicted_positions"][frame]
                error = (actual - predicted).length
                t = (frame - trajectory_start) / fps if trajectory_start else 0
                tolerance = 0.1 + t * 0.3
                if error < tolerance:
                    _physics_data["frame_states"][frame] = "airborne"
                else:
                    _physics_data["frame_states"][frame] = "invalid"
        
        elif is_grounded:
            # Animation is grounded and no ghost active - normal grounded state
            _physics_data["frame_states"][frame] = "grounded"
            _physics_data["predicted_positions"][frame] = _physics_data["com_positions"][frame].copy()
            
        else:
            # Animation is airborne - check if we should start ghost simulation
            launch_frame = frame - 1 if frame - 1 in _physics_data["com_velocities"] else frame
            launch_vel = _physics_data["com_velocities"].get(launch_frame, Vector((0, 0, 0)))
            
            # Only start if there's significant upward velocity (actually jumping)
            min_launch_velocity = 0.5
            
            if launch_vel.z < min_launch_velocity:
                # Not jumping - just crouch or slight foot lift
                _physics_data["frame_states"][frame] = "grounded"
                _physics_data["predicted_positions"][frame] = _physics_data["com_positions"][frame].copy()
                continue
            
            # Start ghost simulation
            ghost_active = True
            trajectory_start = frame
            ghost_pos = _physics_data["com_positions"][frame].copy()
            ghost_vel = launch_vel.copy()
            
            # Store for inspection
            _physics_data["trajectory_start_frame"] = trajectory_start
            _physics_data["trajectory_start_pos"] = ghost_pos.copy()
            _physics_data["trajectory_start_vel"] = ghost_vel.copy()
            
            # Apply first physics step
            dt = 1.0 / fps
            ghost_vel.z -= gravity * dt
            ghost_pos = ghost_pos + ghost_vel * dt
            
            _physics_data["predicted_positions"][frame] = ghost_pos.copy()
            _physics_data["frame_states"][frame] = "airborne"
    
    # If ghost is still airborne at the end, extrapolate until it lands
    # This allows the trajectory to show where the character WOULD land
    if ghost_active and ghost_pos is not None and not ghost_landed:
        extra_frame = end_frame + 1
        max_extra_frames = int(fps * 5)  # Max 5 seconds of extrapolation
        
        while extra_frame <= end_frame + max_extra_frames:
            dt = 1.0 / fps
            
            # Apply gravity
            ghost_vel.z -= gravity * dt
            
            # Update position
            new_ghost_pos = ghost_pos + ghost_vel * dt
            
            # Check for ground collision
            if new_ghost_pos.z <= min_com_z:
                new_ghost_pos.z = min_com_z
                ghost_vel.z = 0
                ghost_landed = True
                ghost_landed_pos = new_ghost_pos.copy()
            
            ghost_pos = new_ghost_pos
            _physics_data["predicted_positions"][extra_frame] = ghost_pos.copy()
            _physics_data["frame_states"][extra_frame] = "extrapolated"
            
            if ghost_landed:
                # Continue sliding for a few more frames then stop
                for slide_frame in range(extra_frame + 1, extra_frame + int(fps * 2) + 1):
                    ghost_vel.x *= 0.8
                    ghost_vel.y *= 0.8
                    ghost_landed_pos = ghost_landed_pos + ghost_vel * dt
                    ghost_landed_pos.z = min_com_z
                    _physics_data["predicted_positions"][slide_frame] = ghost_landed_pos.copy()
                    _physics_data["frame_states"][slide_frame] = "extrapolated"
                break
            
            extra_frame += 1
    
    # If ghost landed during the animation (before end), continue showing it for frames after
    elif ghost_active and ghost_landed and ghost_landed_pos is not None:
        dt = 1.0 / fps
        current_pos = ghost_landed_pos.copy()
        current_vel = ghost_vel.copy() if ghost_vel else Vector((0, 0, 0))
        
        for extra_frame in range(end_frame + 1, end_frame + int(fps * 3) + 1):
            # Apply friction and slide
            current_vel.x *= 0.8
            current_vel.y *= 0.8
            current_pos = current_pos + current_vel * dt
            current_pos.z = min_com_z
            _physics_data["predicted_positions"][extra_frame] = current_pos.copy()
            _physics_data["frame_states"][extra_frame] = "extrapolated"


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


def calculate_ghost_bones(armature: "bpy.types.Object", offset: Vector) -> Dict[str, Tuple[Vector, Vector]]:
    """Calculate ghost bone positions by offsetting the current pose.
    
    Args:
        armature: The armature object.
        offset: World-space offset to apply.
        
    Returns:
        Dict mapping bone name to (head_world, tail_world) tuple.
    """
    ghost_bones = {}
    ground_level = _physics_data.get("detected_ground_level", GROUND_LEVEL)
    
    # Find the lowest bone in the current pose to determine floor constraint
    min_bone_z = float('inf')
    for pose_bone in armature.pose.bones:
        head_world = armature.matrix_world @ pose_bone.head
        tail_world = armature.matrix_world @ pose_bone.tail
        min_bone_z = min(min_bone_z, head_world.z, tail_world.z)
    
    # If offset would push bones below ground, clamp the Z offset
    clamped_offset = offset.copy()
    ghost_min_z = min_bone_z + offset.z
    if ghost_min_z < ground_level:
        # Adjust offset so lowest bone sits at ground level
        clamped_offset.z = ground_level - min_bone_z
    
    for pose_bone in armature.pose.bones:
        head_world = armature.matrix_world @ pose_bone.head
        tail_world = armature.matrix_world @ pose_bone.tail
        
        ghost_bones[pose_bone.name] = (
            head_world + clamped_offset,
            tail_world + clamped_offset
        )
    
    return ghost_bones


def _draw_physics_callback():
    """OpenGL callback to draw physics visualization."""
    if not _physics_data["enabled"]:
        return
    
    armature_name = _physics_data.get("armature_name")
    if not armature_name:
        return
    
    armature = bpy.data.objects.get(armature_name)
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
    
    # Draw COM marker on current position
    if _physics_data.get("show_com_marker", True):
        _draw_com_marker(shader, frame)
    
    # Draw ghost character
    if _physics_data["show_ghost"]:
        offset = get_ghost_offset(frame)
        if offset.length > 0.01:  # Only draw if there's meaningful offset
            ghost_bones = calculate_ghost_bones(armature, offset)
            _draw_ghost_armature(shader, ghost_bones)
            # Draw error line connecting COM to ghost COM
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
    if error < 0.2:
        color = _physics_data["grounded_color"]  # Green - small error
    elif error < 0.5:
        color = _physics_data["airborne_color"]  # Orange - medium error
    else:
        color = _physics_data["invalid_color"]  # Red - large error
    
    batch = batch_for_shader(shader, 'LINES', {"pos": vertices})
    shader.bind()
    shader.uniform_float("color", color)
    gpu.state.line_width_set(2.0)
    batch.draw(shader)


def _draw_trajectory(shader, current_frame: int):
    """Draw the ballistic trajectory curves - both actual and predicted."""
    if not _physics_data["predicted_positions"]:
        return
    
    # Draw trajectory for frames around current (extended range to show landing)
    frames = sorted(_physics_data["predicted_positions"].keys())
    view_behind = 30
    view_ahead = 60  # Show more frames ahead to see where ghost lands
    
    # Draw ACTUAL COM path (white/cyan)
    actual_vertices = []
    for i, frame in enumerate(frames):
        if frame < current_frame - view_behind or frame > current_frame + view_ahead:
            continue
        
        pos = _physics_data["com_positions"].get(frame)
        if not pos:
            continue
            
        if i > 0 and frames[i-1] >= current_frame - view_behind:
            prev_pos = _physics_data["com_positions"].get(frames[i-1])
            if prev_pos:
                actual_vertices.extend([(prev_pos.x, prev_pos.y, prev_pos.z), 
                                       (pos.x, pos.y, pos.z)])
    
    if actual_vertices:
        batch = batch_for_shader(shader, 'LINES', {"pos": actual_vertices})
        shader.bind()
        shader.uniform_float("color", (0.3, 0.8, 1.0, 0.8))  # Cyan for actual path
        gpu.state.line_width_set(2.0)
        batch.draw(shader)
    
    # Draw PREDICTED physics path (color-coded by validity)
    predicted_vertices = []
    for i, frame in enumerate(frames):
        if frame < current_frame - view_behind or frame > current_frame + view_ahead:
            continue
            
        pos = _physics_data["predicted_positions"][frame]
        state = _physics_data["frame_states"].get(frame, "grounded")
        
        if i > 0 and frames[i-1] >= current_frame - view_behind:
            prev_pos = _physics_data["predicted_positions"].get(frames[i-1])
            if prev_pos:
                predicted_vertices.extend([(prev_pos.x, prev_pos.y, prev_pos.z), 
                                          (pos.x, pos.y, pos.z)])
    
    if predicted_vertices:
        batch = batch_for_shader(shader, 'LINES', {"pos": predicted_vertices})
        shader.bind()
        shader.uniform_float("color", _physics_data["trajectory_color"])  # Orange for predicted
        gpu.state.line_width_set(3.0)
        batch.draw(shader)
    
    gpu.state.line_width_set(1.0)


def _draw_ghost_armature(shader, ghost_bones: Dict[str, Tuple[Vector, Vector]]):
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
        shader.uniform_float("color", _physics_data["ghost_color"])
        gpu.state.line_width_set(3.0)
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
        _physics_data["frame_states"] = {}
        _physics_data["fulcrum_frames"] = set()
        _physics_data["fulcrum_positions"] = {}
        _physics_data["predicted_positions"] = {}
        _physics_data["contact_count"] = {}
    
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
    _physics_data["frame_states"] = {}
    _physics_data["fulcrum_frames"] = set()
    _physics_data["fulcrum_positions"] = {}
    _physics_data["predicted_positions"] = {}
    _physics_data["contact_count"] = {}
    _physics_data["trajectory_start_frame"] = None
    _physics_data["trajectory_start_pos"] = None
    _physics_data["trajectory_start_vel"] = None
