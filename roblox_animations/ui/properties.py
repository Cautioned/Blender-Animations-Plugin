"""
Scene properties and property registration for the addon.
"""

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
)
from bpy.types import PropertyGroup
from ..core.utils import armature_items
from ..core.constants import DEFAULT_SERVER_PORT


class RobloxAnimationSettings(PropertyGroup):
    rbx_anim_armature: EnumProperty(
        items=armature_items,
        name="Armature",
        description="Select an armature",
    )

    rbx_server_port: IntProperty(
        name="Server Port",
        description="Port for the animation server",
        default=DEFAULT_SERVER_PORT,
        min=1024,
        max=65535,
    )

    rbx_deform_rig_scale: FloatProperty(
        name="Deform Rig Scale",
        description=(
            "Enter the scale you exported your rig at for proper animation export. "
            "Usually 0.1 or 0.2. You can also adjust in Roblox Studio."
        ),
        default=0.1,
        min=0.0,
    )

    force_deform_bone_serialization: BoolProperty(
        name="Force Deform Bone Serialization",
        description=(
            "Force the use of deform bone serialization even if the rig is not detected "
            "as a deform bone rig (for testing)"
        ),
        default=False,
    )

    rbx_max_studs_per_frame: FloatProperty(
        name="Max studs/frame",
        description="maximum allowed displacement per frame (studs)",
        default=1.0,
        min=0.0,
    )

    rbx_show_motionpath_validation: BoolProperty(
        name="Show validation overlay",
        description="toggle drawing of violation overlays in 3d view",
        default=False,
    )

    rbx_full_range_bake: BoolProperty(
        name="Full range bake",
        description=(
            "when disabled, animations without cyclic extrapolation hold the final pose instead of baking every frame"
        ),
        default=True,
    )


def register_properties():
    bpy.utils.register_class(RobloxAnimationSettings)
    bpy.types.Scene.rbx_anim_settings = bpy.props.PointerProperty(
        type=RobloxAnimationSettings,
        name="Roblox Animations Settings",
    )


def unregister_properties():
    if hasattr(bpy.types.Scene, "rbx_anim_settings"):
        del bpy.types.Scene.rbx_anim_settings
    bpy.utils.unregister_class(RobloxAnimationSettings)
