"""
Easing and interpolation utilities for animation export.
"""

import bpy
from ..core.utils import get_action_fcurves


def easing_requires_bake(interpolation, easing):
    """Whether Pose easing can reproduce this Blender segment exactly."""
    return (
        interpolation not in {"LINEAR", "CONSTANT", "CUBIC", "BOUNCE"}
        # Studio's Bounce InOut differs from Blender's combined bounce curve.
        or (interpolation == "BOUNCE" and easing == "EASE_IN_OUT")
    )


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
    if easing_requires_bake(interpolation, easing):
        return "Linear", "Out"
    # Define the direct mappings from Blender interpolation types to Roblox EasingStyles.
    style_map = {
        "LINEAR": "Linear",
        "CONSTANT": "Constant",
        "CUBIC": "CubicV2",
        "BOUNCE": "Bounce",
    }

    roblox_style = style_map.get(interpolation, None)

    # If the interpolation type from Blender isn't in our map, it's unsupported.
    # Keep fallback linear; unsupported curves should be handled by bake paths.
    if roblox_style is None:
        return "Linear", "Out"

    # Constant Out holds the previous pose; In jumps to the next pose immediately.
    # Linear is independent of direction, so use a canonical value.
    if roblox_style in {"Constant", "Linear"}:
        return roblox_style, "Out"

    # If the style was supported, map the easing direction.
    # Verified against Animator:StepAnimations, not just enum names.
    # Blender AUTO uses ease-out for bounce/elastic and ease-in for cubic.
    if easing == "AUTO":
        easing = "EASE_OUT" if interpolation in {"BOUNCE", "ELASTIC"} else "EASE_IN"
    direction_map = {
        "EASE_IN": "In",
        "EASE_OUT": "Out",
        "EASE_IN_OUT": "InOut",
    }
    # Default to "Out" if the Blender easing type is something unexpected.
    roblox_direction = direction_map.get(easing, "Out")

    return roblox_style, roblox_direction
