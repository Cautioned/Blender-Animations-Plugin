"""
Easing and interpolation utilities for animation export.
"""

import bpy
from ..core.utils import get_action_fcurves


def get_easing_for_bone(action, bone_name, frame):
    """
    Gets the interpolation and easing for a bone at a specific frame by checking its f-curves.
    Returns None if no keyframe exists for this bone at this frame.
    """
    if not action:
        return None, None

    # Get fcurves using compatibility function
    fcurves = get_action_fcurves(action)
    if not fcurves:
        return None, None

    # Check across all transform properties for a keyframe at this frame
    props_to_check = [("location", 3), ("rotation_quaternion", 4), ("scale", 3)]

    # Find the closest keyframe to the target frame (within 0.5 frames)
    closest_interpolation = None
    closest_easing = None
    closest_distance = float("inf")

    for prop_name, num_indices in props_to_check:
        for i in range(num_indices):
            datapath = (
                f'pose.bones["{bpy.utils.escape_identifier(bone_name)}"].{prop_name}'
            )
            fcurve = fcurves.find(datapath, index=i)
            if fcurve:
                for kp in fcurve.keyframe_points:
                    distance = abs(kp.co.x - frame)
                    if distance < 0.5 and distance < closest_distance:
                        closest_interpolation = kp.interpolation
                        closest_easing = kp.easing
                        closest_distance = distance

    return closest_interpolation, closest_easing


def map_blender_to_roblox_easing(interpolation, easing):
    """
    Maps Blender's f-curve interpolation and easing properties to Roblox's
    EasingStyle and EasingDirection enums.
    """
    # Define the direct mappings from Blender interpolation types to Roblox EasingStyles.
    style_map = {
        "LINEAR": "Linear",
        "CONSTANT": "Constant",
        "CUBIC": "CubicV2",
        "BOUNCE": "Bounce",
        "ELASTIC": "Elastic",
    }

    roblox_style = style_map.get(interpolation, None)

    # If the interpolation type from Blender isn't in our map, it's unsupported.
    # Keep fallback linear; unsupported curves should be handled by bake paths.
    if roblox_style is None:
        return "Linear", "Out"

    # Constant easing in Roblox doesn't use a direction, but "Out" is the closest
    # semantic equivalent to Blender's "hold" behavior.
    if roblox_style == "Constant":
        return "Constant", "Out"

    # If the style was supported, map the easing direction.
    direction_map = {
        "EASE_IN": "In",
        "EASE_OUT": "Out",
        "EASE_IN_OUT": "InOut",
    }
    # Default to "Out" if the Blender easing type is something unexpected.
    roblox_direction = direction_map.get(easing, "Out")

    return roblox_style, roblox_direction
