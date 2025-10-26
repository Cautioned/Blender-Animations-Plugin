"""
Type definitions and data structures for the Roblox Animations Blender Addon.
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass


@dataclass
class AnimationState:
    """Represents the animation state of a bone."""
    bone_name: str
    transform: List[float]  # CFrame data
    easing_style: str
    easing_direction: str


@dataclass
class RigInfo:
    """Information about a rig/armature."""
    name: str
    bones: List[str]
    num_bones: int
    has_animation: bool
    frame_range: Optional[Tuple[int, int]]
    is_deform_rig: bool = False


@dataclass
class BoneInfo:
    """Information about a bone."""
    name: str
    parent: Optional[str]
    use_deform: bool
    has_constraints: bool


@dataclass
class ExportInfo:
    """Metadata for exported animations."""
    rig_type: str  # "deform_bone" or "motor6d"
    fps: float
    plugin_version: float
    export_time: float


@dataclass
class AnimationData:
    """Complete animation data structure."""
    duration: float
    keyframes: List[Dict[str, Any]]
    is_deform_bone_rig: bool = False
    bone_hierarchy: Optional[Dict[str, Optional[str]]] = None
    export_info: Optional[ExportInfo] = None
