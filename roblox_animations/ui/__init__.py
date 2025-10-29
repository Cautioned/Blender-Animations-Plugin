"""
UI module for the Roblox Animations Blender Addon.

This module contains all UI panels, properties, and interface components.
"""

from .panels import (
    OBJECT_PT_RbxAnimations,
    OBJECT_PT_RbxAnimations_Tool,
)
from .properties import (
    RobloxAnimationSettings,
    register_properties,
    unregister_properties,
)

__all__ = [
    # Panels
    "OBJECT_PT_RbxAnimations",
    "OBJECT_PT_RbxAnimations_Tool",
    # Properties
    "RobloxAnimationSettings",
    "register_properties",
    "unregister_properties",
]
