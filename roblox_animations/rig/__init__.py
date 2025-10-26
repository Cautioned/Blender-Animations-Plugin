"""
Rig module for the Roblox Animations Blender Addon.

This module handles rig creation, bone management, and constraint operations.
"""

from .creation import *
from .constraints import *
from .ik import *

__all__ = [
    # Creation
    'create_rig', 'load_rigbone', 'autoname_parts',
    
    # Constraints
    'link_object_to_bone_rigid', 'auto_constraint_parts', 'manual_constraint_parts',
    
    # IK
    'create_ik_config', 'remove_ik_config'
]
