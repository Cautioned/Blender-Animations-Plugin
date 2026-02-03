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
from ..core.utils import armature_items, get_object_by_name
from ..core.constants import DEFAULT_SERVER_PORT


def _on_gravity_update(self, context):
    """Callback when gravity is changed - re-analyze physics if enabled."""
    try:
        from ..rig.physics import is_physics_enabled, analyze_animation
        
        if is_physics_enabled():
            # Find the armature being analyzed
            from ..rig.physics import _physics_data
            armature_name = _physics_data.get("armature_name")
            if armature_name:
                armature = get_object_by_name(armature_name)
                if armature:
                    analyze_animation(armature)
    except Exception:
        pass


def _on_physics_param_update(self, context):
    """Generic callback for physics parameter updates that re-runs analysis if enabled."""
    try:
        from ..rig.physics import is_physics_enabled, analyze_animation
        if is_physics_enabled():
            from ..rig.physics import _physics_data
            armature_name = _physics_data.get("armature_name")
            if armature_name:
                armature = get_object_by_name(armature_name)
                if armature:
                    analyze_animation(armature)
    except Exception:
        pass


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

    rbx_physics_gravity: FloatProperty(
        name="Physics Gravity",
        description=(
            "Gravity for AutoPhysics simulation. "
            "Default 50 works well for typical Roblox-scale rigs."
        ),
        default=50.0,
        min=0.1,
        max=500.0,
        update=_on_gravity_update,
    )

    rbx_physics_landing_steer: FloatProperty(
        name="Landing Steer",
        description=(
            "How aggressively the ghost will try to re-orient to an upright pose at landing."
        ),
        default=0.6,
        update=_on_physics_param_update,
        min=0.0,
        max=1.0,
    )

    rbx_physics_landing_window: FloatProperty(
        name="Landing Stick Window (s)",
        description=(
            "Time (seconds) before landing during which the ghost will blend towards an upright pose."
        ),
        default=0.25,
        update=_on_physics_param_update,
        min=0.0,
        max=3.0,
    )

    rbx_full_range_bake: BoolProperty(
        name="Full range bake",
        description=(
            "when disabled, animations without cyclic extrapolation hold the final pose instead of baking every frame"
        ),
        default=True,
    )

    rbx_hide_weld_bones: BoolProperty(
        name="Hide Weld Bones",
        description="hide weld/weldconstraint bones in the viewport (they're still there, just invisible)",
        default=False,
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
