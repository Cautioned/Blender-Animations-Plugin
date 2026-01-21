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
    "version": (2, 3, 1),
    "blender": (2, 80, 0),
    "location": "View3D > Toolbar",
}


# Define classes once - used for both registration and unregistration
CLASSES = (
    # Import operators
    operators.OBJECT_OT_ImportModel,
    operators.OBJECT_OT_ImportFbxAnimation,
    # Rig operators
    operators.OBJECT_OT_GenRig,
    operators.OBJECT_OT_GenIK,
    operators.OBJECT_OT_ModifyIK,
    operators.OBJECT_OT_RemoveIK,
    operators.OBJECT_OT_SetIKFK,
    operators.OBJECT_OT_ToggleCOM,
    operators.OBJECT_OT_ToggleCOMGrid,
    operators.OBJECT_OT_EditCOMWeights,
    operators.OBJECT_OT_ResetBoneWeight,
    operators.OBJECT_OT_ApplyDefaultWeights,
    operators.OBJECT_OT_ClearCOMWeights,
    operators.OBJECT_OT_SetSelectedBoneWeight,
    # AutoPhysics operators
    operators.OBJECT_OT_ToggleAutoPhysics,
    operators.OBJECT_OT_AnalyzePhysics,
    operators.OBJECT_OT_TogglePhysicsGhost,
    operators.OBJECT_OT_ToggleRotationMomentum,
    # Animation operators
    operators.OBJECT_OT_ApplyTransform,
    operators.OBJECT_OT_MapKeyframes,
    operators.OBJECT_OT_Bake,
    operators.OBJECT_OT_Bake_File,
    operators.OBJECT_OT_ValidateMotionPaths,
    operators.OBJECT_OT_ClearMotionPathValidation,
    operators.OBJECT_OT_RunTests,
    # Constraint operators
    operators.OBJECT_OT_AutoConstraint,
    operators.OBJECT_OT_ManualConstraint,
    # Server operators
    operators.StartServerOperator,
    operators.StopServerOperator,
    # UI panels
    ui.OBJECT_PT_RbxAnimations,
    ui.OBJECT_PT_RbxAnimations_Tool,
)


def _safe_unregister_class(cls):
    import bpy

    try:
        bpy.utils.unregister_class(cls)
        return
    except Exception:
        pass

    try:
        existing = getattr(bpy.types, cls.__name__, None)
        if existing:
            bpy.utils.unregister_class(existing)
    except Exception:
        pass


def _safe_register_class(cls):
    import bpy

    try:
        bpy.utils.register_class(cls)
    except ValueError:
        _safe_unregister_class(cls)
        bpy.utils.register_class(cls)


def file_import_extend(self, context):
    """Add import options to the file menu"""
    self.layout.operator(
        operators.OBJECT_OT_ImportModel.bl_idname, text="Roblox Rig (.obj)"
    )
    self.layout.operator(
        operators.OBJECT_OT_ImportFbxAnimation.bl_idname,
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
            from .rig.com import enable_com_visualization, unregister_frame_handler
            enable_com_visualization(False)
            unregister_frame_handler()
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
