"""
Operators module for the Roblox Animations Blender Addon.

This module contains all Blender operators (actions) for the addon.
"""

from .import_ops import (
    OBJECT_OT_ImportModel,
    OBJECT_OT_ImportFbxAnimation,
)
from .rig_ops import (
    OBJECT_OT_GenRig,
    OBJECT_OT_GenIK,
    OBJECT_OT_RemoveIK,
)
from .animation_ops import (
    OBJECT_OT_ApplyTransform,
    OBJECT_OT_MapKeyframes,
    OBJECT_OT_Bake,
    OBJECT_OT_Bake_File,
)
from .constraint_ops import (
    OBJECT_OT_AutoConstraint,
    OBJECT_OT_ManualConstraint,
)
from .server_ops import (
    StartServerOperator,
    StopServerOperator,
)
from .validation_ops import (
    OBJECT_OT_ValidateMotionPaths,
    OBJECT_OT_ClearMotionPathValidation,
)
from .test_ops import (
    OBJECT_OT_RunTests,
)

__all__ = [
    # Import operators
    "OBJECT_OT_ImportModel",
    "OBJECT_OT_ImportFbxAnimation",
    # Rig operators
    "OBJECT_OT_GenRig",
    "OBJECT_OT_GenIK",
    "OBJECT_OT_RemoveIK",
    # Animation operators
    "OBJECT_OT_ApplyTransform",
    "OBJECT_OT_MapKeyframes",
    "OBJECT_OT_Bake",
    "OBJECT_OT_Bake_File",
    "OBJECT_OT_ValidateMotionPaths",
    "OBJECT_OT_ClearMotionPathValidation",
    # Constraint operators
    "OBJECT_OT_AutoConstraint",
    "OBJECT_OT_ManualConstraint",
    # Server operators
    "StartServerOperator",
    "StopServerOperator",
    # Test operators
    "OBJECT_OT_RunTests",
]
