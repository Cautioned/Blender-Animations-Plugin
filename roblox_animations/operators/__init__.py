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
    OBJECT_OT_ModifyIK,
    OBJECT_OT_RemoveIK,
    OBJECT_OT_SetIKFK,
    OBJECT_OT_ToggleCOM,
    OBJECT_OT_ToggleCOMGrid,
    OBJECT_OT_SetCOMPivot,
    OBJECT_OT_EditCOMWeights,
    OBJECT_OT_ResetBoneWeight,
    OBJECT_OT_ApplyDefaultWeights,
    OBJECT_OT_ClearCOMWeights,
    OBJECT_OT_SetSelectedBoneWeight,
    OBJECT_OT_ToggleAutoPhysics,
    OBJECT_OT_AnalyzePhysics,
    OBJECT_OT_TogglePhysicsGhost,
    OBJECT_OT_ToggleRotationMomentum,
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
    "OBJECT_OT_ModifyIK",
    "OBJECT_OT_RemoveIK",
    "OBJECT_OT_SetIKFK",
    "OBJECT_OT_ToggleCOM",
    "OBJECT_OT_ToggleCOMGrid",
    "OBJECT_OT_SetCOMPivot",
    "OBJECT_OT_EditCOMWeights",
    "OBJECT_OT_ResetBoneWeight",
    "OBJECT_OT_ApplyDefaultWeights",
    "OBJECT_OT_ClearCOMWeights",
    "OBJECT_OT_SetSelectedBoneWeight",
    "OBJECT_OT_ToggleAutoPhysics",
    "OBJECT_OT_AnalyzePhysics",
    "OBJECT_OT_TogglePhysicsGhost",
    "OBJECT_OT_ToggleRotationMomentum",
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
