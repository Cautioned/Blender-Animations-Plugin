"""
Easing and interpolation utilities for animation export.
"""

import re
from ..core.constants import identity_cf
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
    props_to_check = [
        ('location', 3), ('rotation_quaternion', 4), ('scale', 3)]
    for prop_name, num_indices in props_to_check:
        for i in range(num_indices):
            datapath = f'pose.bones["{bone_name}"].{prop_name}'
            fcurve = fcurves.find(datapath, index=i)
            if fcurve:
                for kp in fcurve.keyframe_points:
                    if abs(kp.co.x - frame) < 0.001:
                        return kp.interpolation, kp.easing
    return None, None


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
    # In this case, we fall back to Linear and a default "Out" direction.
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
