"""
Animation module for the Roblox Animations Blender Addon.

This module handles animation serialization, baking, and import/export logic.
"""

from .serialization import (
    serialize_animation_state,
    serialize_deform_animation_state,
    serialize_combined_animation_state,
    serialize,
)
from .easing import (
    get_easing_for_bone,
    map_blender_to_roblox_easing,
)
from .import_export import (
    copy_anim_state_bone,
    copy_anim_state,
    prepare_for_kf_map,
    get_mapping_error_bones,
    apply_ao_transform,
)

__all__ = [
    # Serialization
    "serialize_animation_state",
    "serialize_deform_animation_state",
    "serialize_combined_animation_state",
    "serialize",
    # Easing
    "get_easing_for_bone",
    "map_blender_to_roblox_easing",
    # Import/Export
    "copy_anim_state_bone",
    "copy_anim_state",
    "prepare_for_kf_map",
    "get_mapping_error_bones",
    "apply_ao_transform",
]
