"""
Constants and configuration for the Roblox Animations Blender Addon.
"""

from typing import List, Optional

from mathutils import Matrix


# Blender version (will be set when needed)
blender_version = None


def get_blender_version():
    """Get the blender version, initializing it if needed"""
    global blender_version
    if blender_version is None:
        import bpy

        blender_version = bpy.app.version
    return blender_version


# coordinate system transformation matrix (y-up to z-up)
transform_to_blender: Optional[Matrix] = None


def get_transform_to_blender() -> Matrix:
    """Get the transform matrix, initializing it if needed"""
    global transform_to_blender
    if transform_to_blender is None:
        try:
            import bpy_extras

            converted = bpy_extras.io_utils.axis_conversion(
                from_forward="Z", from_up="Y", to_forward="-Y", to_up="Z"
            )
            transform_to_blender = (
                converted.to_4x4() if converted is not None else Matrix.Identity(4)
            )
        except ImportError:
            transform_to_blender = Matrix.Identity(4)
    return transform_to_blender


# Identity CFrame components matrix
identity_cf: List[float] = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

# CFrame rounding settings
cf_round = False  # round cframes before exporting? (reduce size)
cf_round_fac = 4  # round to how many decimals?

# Cache settings
CACHE_DURATION = 1.0  # Cache for 1 second
HASH_CACHE_DURATION = 0.5  # Cache for 0.5 seconds

# Server settings
DEFAULT_SERVER_PORT = 31337
