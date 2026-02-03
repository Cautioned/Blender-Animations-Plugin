"""
Center of Mass (COM) calculation and visualization utilities.

Provides tools for calculating the center of mass of a rig and
visualizing it in the viewport. Uses bone weights for fast real-time calculation.

Custom bone weights can be set via the 'com_weight' custom property
on individual bones, allowing per-rig customization without cluttering the UI.
"""

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector, Matrix
from typing import Optional, Dict, Tuple

# Default bone weights for R15 rigs
DEFAULT_BONE_WEIGHTS_R15 = {
    "HumanoidRootPart": 0.0,
    "LowerTorso": 0.18,
    "UpperTorso": 0.20,
    "Head": 0.08,
    "LeftUpperArm": 0.028,
    "LeftLowerArm": 0.016,
    "LeftHand": 0.006,
    "RightUpperArm": 0.028,
    "RightLowerArm": 0.016,
    "RightHand": 0.006,
    "LeftUpperLeg": 0.10,
    "LeftLowerLeg": 0.047,
    "LeftFoot": 0.015,
    "RightUpperLeg": 0.10,
    "RightLowerLeg": 0.047,
    "RightFoot": 0.015,
}

# Default bone weights for R6 rigs
DEFAULT_BONE_WEIGHTS_R6 = {
    "HumanoidRootPart": 0.0,
    "Torso": 0.46,
    "Head": 0.08,
    "Left Arm": 0.10,
    "Right Arm": 0.10,
    "Left Leg": 0.10,
    "Right Leg": 0.10,
}

# Combined view (R15 prioritized during matching thanks to normalization/length checks)
DEFAULT_BONE_WEIGHTS = {**DEFAULT_BONE_WEIGHTS_R15, **DEFAULT_BONE_WEIGHTS_R6}

# Default weight for bones not in the default list and without custom weight
DEFAULT_WEIGHT = 0.05

# Custom property name for storing bone weights
COM_WEIGHT_PROP = "com_weight"

# IK bone suffixes to skip in COM calculation
_IK_SUFFIXES = ("-IKTarget", "-IKPole", "-IKStretch")


def _normalize_name(s: str) -> str:
    """Normalize a bone/key name for robust matching.

    Removes non-alphanumeric characters and lowercases the string so we can
    compare 'Left Leg' with 'LeftLeg' or 'left_leg' reliably.
    """
    return ''.join(ch.lower() for ch in (s or "") if ch.isalnum())


def detect_rig_type(armature: "bpy.types.Object") -> str:
    """Detect whether an armature appears to be R6, R15, or unknown.

    Returns: 'R6', 'R15', or 'unknown'
    """
    if not armature or armature.type != "ARMATURE":
        return "unknown"

    names = { _normalize_name(b.name) for b in armature.data.bones }

    # R15 markers (more specific names)
    r15_markers = ("lowertorso", "uppertorso", "leftupperarm", "rightupperarm", "leftupperleg", "rightupperleg")
    if any(m in names for m in r15_markers):
        return "R15"

    # R6 markers
    r6_markers = ("torso", "leftarm", "rightarm", "leftleg", "rightleg")
    if any(m in names for m in r6_markers):
        return "R6"

    return "unknown"


def get_bone_weight(bone: "bpy.types.Bone") -> float:
    """Get the COM weight for a bone.
    
    Priority order:
    1. Custom 'com_weight' property on the bone
    2. Root bones default to 0 (e.g., 'root' in name)
    3. Default weight from DEFAULT_BONE_WEIGHTS dict
    4. Partial name match in DEFAULT_BONE_WEIGHTS
    5. DEFAULT_WEIGHT constant
    
    Args:
        bone: The bone to get weight for.
        
    Returns:
        Weight value (0.0 to 1.0 typically, but can be any positive value).
    """
    # Check for custom weight property first
    if COM_WEIGHT_PROP in bone:
        return float(bone[COM_WEIGHT_PROP])

    # Always default root-like bones to 0 (unless user explicitly set com_weight)
    if "root" in bone.name.lower():
        return 0.0
    
    # Check exact match in defaults
    if bone.name in DEFAULT_BONE_WEIGHTS:
        return DEFAULT_BONE_WEIGHTS[bone.name]
    
    # Check partial match using normalized names and prefer longer (more specific) keys
    normalized_bone = _normalize_name(bone.name)
    # Sort keys by length descending so specific keys (e.g., 'lower torso') are matched
    # before generic ones (e.g., 'torso') to avoid R6->R15 overrides.
    for key, weight in sorted(DEFAULT_BONE_WEIGHTS.items(), key=lambda kv: len(_normalize_name(kv[0])), reverse=True):
        if _normalize_name(key) in normalized_bone:
            return weight
    
    return DEFAULT_WEIGHT


def set_bone_weight(bone: "bpy.types.Bone", weight: float):
    """Set a custom COM weight for a bone.
    
    Args:
        bone: The bone to set weight for.
        weight: Weight value (use -1 or None to remove custom weight).
    """
    if weight is None or weight < 0:
        # Remove custom weight, revert to default
        if COM_WEIGHT_PROP in bone:
            del bone[COM_WEIGHT_PROP]
    else:
        bone[COM_WEIGHT_PROP] = weight


def get_all_bone_weights(armature: "bpy.types.Object") -> Dict[str, Tuple[float, bool]]:
    """Get all bone weights for an armature.
    
    Args:
        armature: The armature object.
        
    Returns:
        Dict mapping bone name to (weight, is_custom) tuple.
    """
    weights = {}
    if not armature or armature.type != "ARMATURE":
        return weights
    
    for bone in armature.data.bones:
        is_custom = COM_WEIGHT_PROP in bone
        weight = get_bone_weight(bone)
        weights[bone.name] = (weight, is_custom)
    
    return weights


def clear_all_custom_weights(armature: "bpy.types.Object"):
    """Remove all custom COM weights from an armature.
    
    Args:
        armature: The armature object.
    """
    if not armature or armature.type != "ARMATURE":
        return
    
    for bone in armature.data.bones:
        if COM_WEIGHT_PROP in bone:
            del bone[COM_WEIGHT_PROP]


def apply_default_weights(armature: "bpy.types.Object", overwrite: bool = False) -> int:
    """Apply default weights as custom properties to all bones.

    This chooses an appropriate defaults set for R6 vs R15 rigs and applies
    weights only when we can confidently detect the rig format. Returns the
    number of bones that were assigned weights.

    Args:
        armature: The armature object.
        overwrite: If True, overwrite existing custom weights.

    Returns:
        Number of bones that had default weights applied.
    """
    if not armature or armature.type != "ARMATURE":
        return 0

    rig_type = detect_rig_type(armature)

    # If rig type is unknown, avoid applying potentially incorrect R6 defaults
    if rig_type == "unknown" and not overwrite:
        return 0

    # Select mapping based on rig type
    if rig_type == "R6":
        # Use normalized keys for matching
        mapping = { _normalize_name(k): v for k, v in DEFAULT_BONE_WEIGHTS_R6.items() }
    else:
        # R15 or fallback: use the R15 mapping
        mapping = { _normalize_name(k): v for k, v in DEFAULT_BONE_WEIGHTS_R15.items() }

    applied = 0

    for bone in armature.data.bones:
        # Skip if custom weight exists and we're not overwriting
        if not overwrite and COM_WEIGHT_PROP in bone:
            continue

        # Root-like bones should default to 0
        if "root" in bone.name.lower():
            bone[COM_WEIGHT_PROP] = 0.0
            applied += 1
            continue

        norm_name = _normalize_name(bone.name)

        weight = None
        # Exact normalized match
        if norm_name in mapping:
            weight = mapping[norm_name]
        else:
            # Partial match: prefer longer/more specific keys
            for key, w in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
                if key in norm_name:
                    weight = w
                    break

        if weight is None:
            # Fallback: leave unset unless overwrite is True (in which case apply DEFAULT_WEIGHT)
            if overwrite:
                bone[COM_WEIGHT_PROP] = DEFAULT_WEIGHT
                applied += 1
        else:
            bone[COM_WEIGHT_PROP] = weight
            applied += 1

    return applied


def calculate_com(armature: "bpy.types.Object") -> Vector:
    """Calculate the center of mass for an armature using bone weights.
    
    Uses anatomical bone weights for fast real-time calculation.
    Custom weights can be set via the 'com_weight' property on bones.
    
    Args:
        armature: The armature object to calculate COM for.
        
    Returns:
        World-space position of the center of mass.
    """
    if not armature or armature.type != "ARMATURE":
        return Vector((0, 0, 0))
    
    total_weight = 0.0
    weighted_position = Vector((0, 0, 0))
    
    # Cache the matrix for all bones
    matrix_world = armature.matrix_world
    
    for pose_bone in armature.pose.bones:
        # Skip IK helper bones
        bone_name = pose_bone.name
        if any(bone_name.endswith(suffix) for suffix in _IK_SUFFIXES):
            continue
        
        # Get bone weight (custom or default)
        weight = get_bone_weight(pose_bone.bone)
        
        # Skip bones with zero weight
        if weight <= 0:
            continue
        
        # Get bone center position in world space
        # Use head + (tail - head) * 0.5 for center
        bone_center = matrix_world @ ((pose_bone.head + pose_bone.tail) * 0.5)
        
        weighted_position += bone_center * weight
        total_weight += weight
    
    if total_weight > 0:
        return weighted_position / total_weight
    
    return armature.location.copy()


def calculate_com_velocity(
    armature: "bpy.types.Object",
    frame_current: int,
    frame_prev: int,
    fps: float = 30.0
) -> Vector:
    """Calculate the velocity of the center of mass between two frames.
    
    Args:
        armature: The armature object.
        frame_current: Current frame number.
        frame_prev: Previous frame number.
        fps: Frames per second.
        
    Returns:
        Velocity vector (units per second).
    """
    scene = bpy.context.scene
    original_frame = scene.frame_current
    
    # Get COM at previous frame
    scene.frame_set(frame_prev)
    bpy.context.view_layer.update()
    com_prev = calculate_com(armature)
    
    # Get COM at current frame
    scene.frame_set(frame_current)
    bpy.context.view_layer.update()
    com_current = calculate_com(armature)
    
    # Restore original frame
    scene.frame_set(original_frame)
    
    # Calculate velocity
    frame_delta = abs(frame_current - frame_prev)
    if frame_delta == 0:
        return Vector((0, 0, 0))
    
    time_delta = frame_delta / fps
    velocity = (com_current - com_prev) / time_delta
    
    return velocity


# Global state for COM visualization
_com_draw_handler = None
_com_data = {
    "enabled": False,
    "armature_name": None,  # Track which armature the COM is for
    "position": Vector((0, 0, 0)),
    "show_projection": True,
    "show_grid": True,  # Circular grid at ground level
    "grid_radius": 1.0,  # Radius of the circular grid
    "grid_rings": 3,  # Number of concentric rings
    "projection_z": 0.0,
    "color": (1.0, 0.8, 0.0, 1.0),  # Yellow
    "projection_color": (0.0, 0.8, 1.0, 0.5),  # Cyan, semi-transparent
    "grid_color": (0.5, 0.5, 0.5, 0.3),  # Gray, semi-transparent
    "size": 0.15,
}


def _get_com_shader():
    try:
        return gpu.shader.from_builtin('UNIFORM_COLOR')
    except Exception:
        return gpu.shader.from_builtin('3D_UNIFORM_COLOR')


def _draw_com_callback():
    """OpenGL callback to draw the COM indicator."""
    if not _com_data["enabled"]:
        return
    
    # Only draw if the tracked armature still exists and is valid
    armature_name = _com_data.get("armature_name")
    if armature_name:
        armature = bpy.data.objects.get(armature_name)
        if not armature or armature.type != "ARMATURE":
            # Armature was deleted or renamed, disable visualization
            _com_data["enabled"] = False
            return
    
    pos = _com_data["position"]
    size = _com_data["size"]
    color = _com_data["color"]
    
    shader = _get_com_shader()
    
    # Draw COM sphere (approximated with lines)
    vertices = []
    
    # Create a simple cross/star pattern for the COM
    # X axis
    vertices.extend([
        (pos.x - size, pos.y, pos.z),
        (pos.x + size, pos.y, pos.z),
    ])
    # Y axis
    vertices.extend([
        (pos.x, pos.y - size, pos.z),
        (pos.x, pos.y + size, pos.z),
    ])
    # Z axis
    vertices.extend([
        (pos.x, pos.y, pos.z - size),
        (pos.x, pos.y, pos.z + size),
    ])
    
    # Draw circle in XY plane
    import math
    segments = 16
    for i in range(segments):
        angle1 = (i / segments) * 2 * math.pi
        angle2 = ((i + 1) / segments) * 2 * math.pi
        vertices.extend([
            (pos.x + math.cos(angle1) * size * 0.7, pos.y + math.sin(angle1) * size * 0.7, pos.z),
            (pos.x + math.cos(angle2) * size * 0.7, pos.y + math.sin(angle2) * size * 0.7, pos.z),
        ])
    
    batch = batch_for_shader(shader, 'LINES', {"pos": vertices})
    
    shader.bind()
    shader.uniform_float("color", color)
    
    gpu.state.line_width_set(2.0)
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)
    
    # Draw projection line to ground
    if _com_data["show_projection"]:
        proj_z = _com_data["projection_z"]
        proj_color = _com_data["projection_color"]
        
        proj_vertices = [
            (pos.x, pos.y, pos.z),
            (pos.x, pos.y, proj_z),
        ]
        
        # Draw projection point (small cross on ground)
        cross_size = size * 0.5
        proj_vertices.extend([
            (pos.x - cross_size, pos.y, proj_z),
            (pos.x + cross_size, pos.y, proj_z),
            (pos.x, pos.y - cross_size, proj_z),
            (pos.x, pos.y + cross_size, proj_z),
        ])
        
        batch_proj = batch_for_shader(shader, 'LINES', {"pos": proj_vertices})
        shader.uniform_float("color", proj_color)
        gpu.state.line_width_set(1.0)
        batch_proj.draw(shader)
    
    # Draw circular grid at ground level
    if _com_data["show_grid"]:
        proj_z = _com_data["projection_z"]
        grid_color = _com_data["grid_color"]
        grid_radius = _com_data["grid_radius"]
        grid_rings = _com_data["grid_rings"]
        
        grid_vertices = []
        segments = 32  # Segments per circle
        
        # Draw concentric rings centered on COM projection
        for ring in range(1, grid_rings + 1):
            ring_radius = (ring / grid_rings) * grid_radius
            for i in range(segments):
                angle1 = (i / segments) * 2 * math.pi
                angle2 = ((i + 1) / segments) * 2 * math.pi
                grid_vertices.extend([
                    (pos.x + math.cos(angle1) * ring_radius, pos.y + math.sin(angle1) * ring_radius, proj_z),
                    (pos.x + math.cos(angle2) * ring_radius, pos.y + math.sin(angle2) * ring_radius, proj_z),
                ])
        
        # Draw cross lines through center
        grid_vertices.extend([
            (pos.x - grid_radius, pos.y, proj_z),
            (pos.x + grid_radius, pos.y, proj_z),
            (pos.x, pos.y - grid_radius, proj_z),
            (pos.x, pos.y + grid_radius, proj_z),
        ])
        
        # Draw diagonal lines
        diag = grid_radius * 0.707  # cos(45°)
        grid_vertices.extend([
            (pos.x - diag, pos.y - diag, proj_z),
            (pos.x + diag, pos.y + diag, proj_z),
            (pos.x - diag, pos.y + diag, proj_z),
            (pos.x + diag, pos.y - diag, proj_z),
        ])
        
        batch_grid = batch_for_shader(shader, 'LINES', {"pos": grid_vertices})
        shader.uniform_float("color", grid_color)
        gpu.state.line_width_set(1.0)
        batch_grid.draw(shader)
    
    gpu.state.blend_set('NONE')
    gpu.state.line_width_set(1.0)


def enable_com_visualization(enable: bool = True):
    """Enable or disable COM visualization in the viewport."""
    global _com_draw_handler
    
    _com_data["enabled"] = enable
    
    if enable and _com_draw_handler is None:
        _com_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_com_callback, (), 'WINDOW', 'POST_VIEW'
        )
    elif not enable and _com_draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_com_draw_handler, 'WINDOW')
        _com_draw_handler = None
        # Clear armature tracking when disabled
        _com_data["armature_name"] = None
    
    # Redraw viewports
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def update_com_visualization(armature: "bpy.types.Object"):
    """Update the COM visualization position."""
    if not _com_data["enabled"]:
        return
    
    # Check if armature is valid
    if not armature or armature.type != "ARMATURE":
        return
    
    # Track which armature we're visualizing
    _com_data["armature_name"] = armature.name
    
    com = calculate_com(armature)
    _com_data["position"] = com
    
    # Update projection Z to be at the lowest foot position or 0
    min_z = 0.0
    for pose_bone in armature.pose.bones:
        if "foot" in pose_bone.name.lower():
            foot_pos = armature.matrix_world @ pose_bone.head
            min_z = min(min_z, foot_pos.z)
    
    _com_data["projection_z"] = min_z
    
    # Redraw
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def get_com_armature_name() -> Optional[str]:
    """Get the name of the armature currently being visualized.
    
    Returns:
        Armature name or None if no visualization is active.
    """
    return _com_data.get("armature_name")


def is_com_for_armature(armature: "bpy.types.Object") -> bool:
    """Check if COM visualization is active for a specific armature.
    
    Args:
        armature: The armature to check.
        
    Returns:
        True if COM is enabled and tracking this armature.
    """
    if not _com_data["enabled"] or not armature:
        return False
    return _com_data.get("armature_name") == armature.name


def is_com_visualization_enabled() -> bool:
    """Check if COM visualization is enabled."""
    return _com_data["enabled"]


def is_com_grid_enabled() -> bool:
    """Check if COM circular grid is enabled."""
    return _com_data["show_grid"]


def toggle_com_grid(enable: Optional[bool] = None):
    """Toggle or set the circular grid display.
    
    Args:
        enable: If provided, set grid to this state. If None, toggle.
    """
    if enable is None:
        _com_data["show_grid"] = not _com_data["show_grid"]
    else:
        _com_data["show_grid"] = enable
    
    # Redraw viewports
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def set_com_grid_radius(radius: float):
    """Set the radius of the circular grid.
    
    Args:
        radius: Grid radius in Blender units.
    """
    _com_data["grid_radius"] = max(0.1, radius)
    
    # Redraw viewports
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def get_com_grid_radius() -> float:
    """Get the current grid radius."""
    return _com_data["grid_radius"]




def rotate_around_com(
    armature: "bpy.types.Object",
    axis: str,
    angle: float
):
    """Rotate the armature around its center of mass.
    
    Args:
        armature: The armature object.
        axis: Rotation axis ('X', 'Y', or 'Z').
        angle: Rotation angle in radians.
    """
    
    # Calculate COM
    com = calculate_com(armature)
    
    # Store original cursor location and pivot
    original_cursor = bpy.context.scene.cursor.location.copy()
    original_pivot = bpy.context.scene.tool_settings.transform_pivot_point
    
    # Set cursor to COM and use cursor as pivot
    bpy.context.scene.cursor.location = com
    bpy.context.scene.tool_settings.transform_pivot_point = 'CURSOR'
    
    # Select armature and rotate
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    
    # Apply rotation
    bpy.ops.transform.rotate(value=angle, orient_axis=axis, center_override=com)
    
    # Restore original cursor and pivot
    bpy.context.scene.cursor.location = original_cursor
    bpy.context.scene.tool_settings.transform_pivot_point = original_pivot


# Frame change handler for real-time COM updates
def _frame_change_handler(scene):
    """Update COM visualization when frame changes."""
    if not _com_data["enabled"]:
        return
    
    # Find active armature
    obj = bpy.context.active_object
    if obj and obj.type == "ARMATURE":
        update_com_visualization(obj)


def register_frame_handler():
    """Register the frame change handler for real-time COM updates."""
    if _frame_change_handler not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_frame_change_handler)


def unregister_frame_handler():
    """Unregister the frame change handler."""
    if _frame_change_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_frame_change_handler)
