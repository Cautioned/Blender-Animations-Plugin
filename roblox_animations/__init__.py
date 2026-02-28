"""
Roblox Animations Blender Addon - Modular Version

This addon provides tools for importing Roblox rigs and exporting animations
with live sync capabilities to Roblox Studio.
"""

# Import modules once at module level
from . import operators, ui, server


# Define bl_info directly to avoid import issues
bl_info = {
    "name": "Roblox Animations Importer/Exporter",
    "description": "Plugin for importing roblox rigs and exporting animations.",
    "author": "Cautioned",
    "version": (2, 4, 1),
    "blender": (2, 80, 0),
    "location": "View3D > Toolbar",
}


def _resolve_operator_class(attr_name, fallback_module=None):
    """Resolve an operator class from operators package with optional fallback module."""
    cls = getattr(operators, attr_name, None)
    if cls is not None:
        return cls

    if fallback_module:
        try:
            module = __import__(
                f"{__package__}.operators.{fallback_module}",
                fromlist=[attr_name],
            )
            return getattr(module, attr_name, None)
        except Exception:
            return None

    return None


_classes = [
    # Import operators
    _resolve_operator_class(
        "OBJECT_OT_ConfirmWeaponTarget", fallback_module="import_ops"
    ),  # must register before ImportModel uses it
    _resolve_operator_class("OBJECT_OT_ApplyWeaponImport", fallback_module="import_ops"),
    _resolve_operator_class("OBJECT_OT_ImportModel", fallback_module="import_ops"),
    _resolve_operator_class("OBJECT_OT_ImportFbxAnimation", fallback_module="import_ops"),
    # Rig operators
    _resolve_operator_class("OBJECT_OT_GenRig"),
    _resolve_operator_class("OBJECT_OT_GenIK"),
    _resolve_operator_class("OBJECT_OT_ModifyIK"),
    _resolve_operator_class("OBJECT_OT_RemoveIK"),
    _resolve_operator_class("OBJECT_OT_SetIKFK"),
    _resolve_operator_class("OBJECT_OT_ToggleCOM"),
    _resolve_operator_class("OBJECT_OT_ToggleCOMGrid"),
    _resolve_operator_class("OBJECT_OT_EditCOMWeights"),
    _resolve_operator_class("OBJECT_OT_ResetBoneWeight"),
    _resolve_operator_class("OBJECT_OT_ApplyDefaultWeights"),
    _resolve_operator_class("OBJECT_OT_ClearCOMWeights"),
    _resolve_operator_class("OBJECT_OT_SetSelectedBoneWeight"),
    # AutoPhysics operators
    _resolve_operator_class("OBJECT_OT_ToggleAutoPhysics"),
    _resolve_operator_class("OBJECT_OT_AnalyzePhysics"),
    _resolve_operator_class("OBJECT_OT_TogglePhysicsGhost"),
    _resolve_operator_class("OBJECT_OT_ToggleRotationMomentum"),
    # Weld bone visibility
    _resolve_operator_class("OBJECT_OT_ToggleWeldBones"),
    # World-space unparent
    _resolve_operator_class("OBJECT_OT_WorldSpaceUnparent"),
    _resolve_operator_class("OBJECT_OT_WorldSpaceReparent"),
    # Animation operators
    _resolve_operator_class("OBJECT_OT_ApplyTransform"),
    _resolve_operator_class("OBJECT_OT_MapKeyframes"),
    _resolve_operator_class("OBJECT_OT_Bake"),
    _resolve_operator_class("OBJECT_OT_Bake_File"),
    _resolve_operator_class("OBJECT_OT_ValidateMotionPaths"),
    _resolve_operator_class("OBJECT_OT_ClearMotionPathValidation"),
    _resolve_operator_class("OBJECT_OT_RunTests"),
    # Constraint operators
    _resolve_operator_class("OBJECT_OT_AutoConstraint"),
    _resolve_operator_class("OBJECT_OT_ManualConstraint"),
    # Weapon/accessory operators
    _resolve_operator_class("OBJECT_OT_AttachMeshToBone"),
    _resolve_operator_class("OBJECT_OT_ImportAndAttach"),
    # Server operators
    _resolve_operator_class("StartServerOperator"),
    _resolve_operator_class("StopServerOperator"),
    # UI panels
    getattr(ui, "OBJECT_PT_RbxAnimations", None),
    getattr(ui, "OBJECT_PT_RbxAnimations_Tool", None),
]
CLASSES = tuple(cls for cls in _classes if cls is not None)


def _safe_unregister_class(cls):
    import bpy

    try:
        existing = getattr(bpy.types, cls.__name__, None)
        if existing:
            bpy.utils.unregister_class(existing)
    except Exception:
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


def _safe_register_class(cls):
    import bpy

    existing = getattr(bpy.types, cls.__name__, None)
    if existing:
        try:
            bpy.utils.unregister_class(existing)
        except Exception:
            pass

    try:
        bpy.utils.register_class(cls)
    except Exception:
        _safe_unregister_class(cls)
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass


def file_import_extend(self, context):
    """Add import options to the file menu"""
    import_model_op = _resolve_operator_class("OBJECT_OT_ImportModel", fallback_module="import_ops")
    import_anim_op = _resolve_operator_class("OBJECT_OT_ImportFbxAnimation", fallback_module="import_ops")
    if import_model_op is not None:
        self.layout.operator(import_model_op.bl_idname, text="Roblox Rig (.obj)")
    if import_anim_op is not None:
        self.layout.operator(
            import_anim_op.bl_idname,
            text="Animation for Roblox Rig (.fbx)",
        )


def register():
    """Register the addon"""
    import bpy

    try:
        # Register all classes
        for cls in CLASSES:
            _safe_register_class(cls)

        # Register properties
        try:
            ui.unregister_properties()
        except Exception:
            pass
        ui.register_properties()

        # Add import menu items
        try:
            bpy.types.TOPBAR_MT_file_import.remove(file_import_extend)
        except Exception:
            pass
        bpy.types.TOPBAR_MT_file_import.append(file_import_extend)

        # Register request processing timer
        if not bpy.app.timers.is_registered(server.process_pending_requests):
            bpy.app.timers.register(server.process_pending_requests, persistent=True)

    except Exception as e:
        print(f"Error registering Roblox Animations addon: {e}")
        import traceback

        traceback.print_exc()


def unregister():
    """Unregister the addon"""
    import bpy

    try:
        # Clean up draw handlers first
        try:
            from .operators.validation_ops import cleanup_validation_draw_handlers

            cleanup_validation_draw_handlers()
        except Exception:
            pass
        
        # Clean up physics handlers and data
        try:
            from .rig.physics import cleanup_physics
            cleanup_physics()
        except Exception:
            pass
        
        # Clean up COM visualization
        try:
            from .rig.com import (
                enable_com_visualization,
                unregister_frame_handler,
                unregister_depsgraph_handler,
            )
            enable_com_visualization(False)
            unregister_frame_handler()
            unregister_depsgraph_handler()
        except Exception:
            pass

        # Unregister all classes in reverse order
        for cls in reversed(CLASSES):
            _safe_unregister_class(cls)

        # Unregister properties
        try:
            ui.unregister_properties()
        except Exception:
            pass

        # Remove import menu items
        try:
            bpy.types.TOPBAR_MT_file_import.remove(file_import_extend)
        except Exception:
            pass

        # Remove request processing timer
        try:
            if bpy.app.timers.is_registered(server.process_pending_requests):
                bpy.app.timers.unregister(server.process_pending_requests)
        except Exception:
            pass

        # Ensure the server is stopped when the addon is unregistered
        try:
            server.stop_server()
        except Exception:
            pass

    except Exception as e:
        print(f"Error unregistering Roblox Animations addon: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    register()
