"""
Scene properties and property registration for the addon.
"""

import bpy
from ..core.utils import armature_items
from ..core.constants import DEFAULT_SERVER_PORT


def register_properties():
    """Register all scene properties"""
    # Armature selection property
    bpy.types.Scene.rbx_anim_armature = bpy.props.EnumProperty(
        items=armature_items, name="Armature", description="Select an armature"
    )
    
    # Server port property
    bpy.types.Scene.rbx_server_port = bpy.props.IntProperty(
        name="Server Port",
        description="Port for the animation server",
        default=DEFAULT_SERVER_PORT,
        min=1024,
        max=65535
    )
    
    # Deform rig scale property
    bpy.types.Scene.rbx_deform_rig_scale = bpy.props.FloatProperty(
        name="Deform Rig Scale",
        description="Enter the scale you exported your rig at for proper animation export. Usually 0.1 or 0.2. You can also adjust in Roblox Studio.",
        default=0.1,
        min=0.0
    )
    
    # Force deform bone serialization property
    bpy.types.Scene.force_deform_bone_serialization = bpy.props.BoolProperty(
        name="Force Deform Bone Serialization",
        description="Force the use of deform bone serialization even if the rig is not detected as a deform bone rig (for testing)",
        default=False
    )

    # Motion path validation properties
    bpy.types.Scene.rbx_max_studs_per_frame = bpy.props.FloatProperty(
        name="Max studs/frame",
        description="maximum allowed displacement per frame (studs)",
        default=1.0,
        min=0.0
    )
    bpy.types.Scene.rbx_show_motionpath_validation = bpy.props.BoolProperty(
        name="Show validation overlay",
        description="toggle drawing of violation overlays in 3d view",
        default=False
    )


def unregister_properties():
    """Unregister all scene properties"""
    # Safely unregister properties
    if hasattr(bpy.types.Scene, 'rbx_anim_armature'):
        del bpy.types.Scene.rbx_anim_armature
    if hasattr(bpy.types.Scene, 'rbx_server_port'):
        del bpy.types.Scene.rbx_server_port
    if hasattr(bpy.types.Scene, 'rbx_deform_rig_scale'):
        del bpy.types.Scene.rbx_deform_rig_scale
    if hasattr(bpy.types.Scene, 'rbx_max_studs_per_frame'):
        del bpy.types.Scene.rbx_max_studs_per_frame
    if hasattr(bpy.types.Scene, 'rbx_show_motionpath_validation'):
        del bpy.types.Scene.rbx_show_motionpath_validation
    if hasattr(bpy.types.Scene, 'force_deform_bone_serialization'):
        del bpy.types.Scene.force_deform_bone_serialization
