"""
Scene properties and property registration for the addon.
"""

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
)
from bpy.types import PropertyGroup
from ..animation.face_controls import (
    FACE_CONTROL_ORDER,
    FACE_FACS_UI_SYNC_PROP,
    apply_facs_properties_to_armature,
    face_control_property_name,
    iter_active_facs_armatures,
    load_facs_payload_from_armature,
)
from ..core.utils import armature_items, get_object_by_name
from ..core.constants import DEFAULT_SERVER_PORT


_FACE_CONTROL_DEPSGRAPH_SEQUENCE = 0
_FACE_CONTROL_DEPSGRAPH_APPLYING = False


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


def _apply_face_controls_from_properties(armature_obj):
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return
    try:
        if armature_obj.get(FACE_FACS_UI_SYNC_PROP):
            return
    except Exception:
        pass

    payload = load_facs_payload_from_armature(armature_obj)
    if not payload:
        return

    control_holder = getattr(armature_obj, "rbx_face_controls", None)
    if control_holder is None:
        return

    apply_facs_properties_to_armature(armature_obj, payload=payload, persist_state=True)


def _on_face_control_update(self, context):
    _apply_face_controls_from_properties(getattr(self, "id_data", None))


def _face_control_frame_apply_token(scene):
    if scene is None:
        return None
    try:
        return ("frame", float(scene.frame_current_final))
    except Exception:
        pass
    try:
        return ("frame", int(scene.frame_current), float(getattr(scene, "frame_subframe", 0.0)))
    except Exception:
        return None


def _face_control_depsgraph_apply_token(scene):
    global _FACE_CONTROL_DEPSGRAPH_SEQUENCE
    _FACE_CONTROL_DEPSGRAPH_SEQUENCE += 1
    return ("depsgraph", _FACE_CONTROL_DEPSGRAPH_SEQUENCE, _face_control_frame_apply_token(scene))


@persistent
def _frame_change_face_controls_handler(scene):
    apply_token = _face_control_frame_apply_token(scene)
    for obj in iter_active_facs_armatures():
        apply_facs_properties_to_armature(obj, persist_state=False, apply_token=apply_token)


@persistent
def _depsgraph_face_controls_handler(scene, depsgraph):
    global _FACE_CONTROL_DEPSGRAPH_APPLYING

    if _FACE_CONTROL_DEPSGRAPH_APPLYING:
        return

    active_armatures = list(iter_active_facs_armatures())
    if not active_armatures:
        return

    apply_token = _face_control_depsgraph_apply_token(scene)
    _FACE_CONTROL_DEPSGRAPH_APPLYING = True
    try:
        for obj in active_armatures:
            apply_facs_properties_to_armature(obj, persist_state=False, apply_token=apply_token)
    finally:
        _FACE_CONTROL_DEPSGRAPH_APPLYING = False


class RobloxFaceControlState(PropertyGroup):
    pass


if not hasattr(RobloxFaceControlState, "__annotations__"):
    RobloxFaceControlState.__annotations__ = {}

for _control_name in FACE_CONTROL_ORDER:
    RobloxFaceControlState.__annotations__[face_control_property_name(_control_name)] = FloatProperty(
        name=_control_name,
        description=f"Drive Roblox FaceControls '{_control_name}'",
        default=0.0,
        min=0.0,
        max=1.0,
        soft_min=0.0,
        soft_max=1.0,
        update=_on_face_control_update,
    )


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

    rbx_face_controls_expanded: BoolProperty(
        name="Show Face Controls",
        description="show or hide the roblox face control sliders",
        default=False,
    )


def register_properties():
    bpy.utils.register_class(RobloxFaceControlState)
    bpy.utils.register_class(RobloxAnimationSettings)
    bpy.types.Scene.rbx_anim_settings = bpy.props.PointerProperty(
        type=RobloxAnimationSettings,
        name="Roblox Animations Settings",
    )
    bpy.types.Object.rbx_face_controls = bpy.props.PointerProperty(
        type=RobloxFaceControlState,
        name="Roblox Face Controls",
    )
    if _frame_change_face_controls_handler not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_frame_change_face_controls_handler)
    if _depsgraph_face_controls_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_face_controls_handler)


def unregister_properties():
    if _frame_change_face_controls_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_frame_change_face_controls_handler)
    if _depsgraph_face_controls_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_face_controls_handler)
    if hasattr(bpy.types.Object, "rbx_face_controls"):
        del bpy.types.Object.rbx_face_controls
    if hasattr(bpy.types.Scene, "rbx_anim_settings"):
        del bpy.types.Scene.rbx_anim_settings
    bpy.utils.unregister_class(RobloxAnimationSettings)
    bpy.utils.unregister_class(RobloxFaceControlState)
