"""
Rig module for the Roblox Animations Blender Addon.

Rig creation, bone management and constraints, plus the asset-side
subsystems: FileMesh fetching, terrain meshing, texture/material
construction, and skin binding.
"""

from .creation import (
    create_rig,
    load_rigbone,
    autoname_parts,
)
from .constraints import (
    link_object_to_bone_rigid,
    auto_constraint_parts,
    manual_constraint_parts,
)
from .ik import (
    create_ik_config,
    remove_ik_config,
    has_ik_constraint,
    get_ik_constraint,
    update_pole_axis,
    setup_ik_stretch,
    setup_ik_fk_switch,
)
from .com import (
    calculate_com,
    enable_com_visualization,
    update_com_visualization,
    is_com_visualization_enabled,
    is_com_for_armature,
    get_com_armature_name,
    register_frame_handler,
    unregister_frame_handler,
)
from .physics import (
    cleanup_physics,
)

__all__ = [
    # Creation
    "create_rig",
    "load_rigbone",
    "autoname_parts",
    # Constraints
    "link_object_to_bone_rigid",
    "auto_constraint_parts",
    "manual_constraint_parts",
    # IK
    "create_ik_config",
    "remove_ik_config",
    "has_ik_constraint",
    "get_ik_constraint",
    "update_pole_axis",
    "setup_ik_stretch",
    "setup_ik_fk_switch",
    # Center of Mass
    "calculate_com",
    "enable_com_visualization",
    "update_com_visualization",
    "is_com_visualization_enabled",
    "is_com_for_armature",
    "get_com_armature_name",
    "register_frame_handler",
    "unregister_frame_handler",
    # Physics
    "cleanup_physics",
]
