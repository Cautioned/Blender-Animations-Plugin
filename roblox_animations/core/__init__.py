"""
Core module for the Roblox Animations Blender Addon.

This module contains shared utilities, constants, and data structures
used throughout the addon.
"""

from .constants import (
    version, blender_version, get_blender_version, get_transform_to_blender,
    identity_cf, cf_round, cf_round_fac, CACHE_DURATION, HASH_CACHE_DURATION, DEFAULT_SERVER_PORT
)
from .utils import *
from .types import *

__all__ = [
    # Constants
    'version', 'blender_version', 'get_blender_version',
    'get_transform_to_blender', 'identity_cf', 'cf_round', 'cf_round_fac',
    'CACHE_DURATION', 'HASH_CACHE_DURATION', 'DEFAULT_SERVER_PORT',
    
    # Utilities
    'get_action_hash', 'get_timeline_hash', 'get_armature_timeline_hash', 
    'get_cached_armature_hash', 'has_animation_changed', 'on_animation_update',
    'get_cached_armatures', 'invalidate_armature_cache', 'armature_items',
    'cf_to_mat', 'mat_to_cf', 'get_unique_name', 'find_master_collection_for_object',
    'find_parts_collection_in_master', 'set_scene_fps', 'get_scene_fps',
    'armature_anim_hashes', 'armature_update_timestamps', 'last_known_synced_armature',
    
    # Types
    'AnimationState', 'RigInfo', 'BoneInfo', 'ExportInfo', 'AnimationData'
]
