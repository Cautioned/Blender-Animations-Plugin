"""
Roblox Animations Blender Addon - Modular Version

This addon provides tools for importing Roblox rigs and exporting animations
with live sync capabilities to Roblox Studio.
"""

# Define bl_info directly to avoid import issues
bl_info = {
    "name": "Roblox Animations Importer/Exporter",
    "description": "Plugin for importing roblox rigs and exporting animations.",
    "author": "Den_S/@DennisRBLX, Updated by Cautioned/@Cautloned",
    "version": (2, 0, 0),
    "blender": (2, 80, 0),
    "location": "View3D > Toolbar",
    "warning": "Requires Python's http.server module",
}

# Classes will be defined in register() function after imports


def file_import_extend(self, context):
    """Add import options to the file menu"""
    from . import operators
    self.layout.operator(operators.OBJECT_OT_ImportModel.bl_idname,
                         text="Roblox Rig (.obj)")
    self.layout.operator(operators.OBJECT_OT_ImportFbxAnimation.bl_idname,
                         text="Animation for Roblox Rig (.fbx)")


def register():
    """Register the addon"""
    import bpy
    
    try:
        # Import modules safely
        from . import core, animation, rig, server, operators, ui
        
        # Define classes after imports
        classes = (
            # Import operators
            operators.OBJECT_OT_ImportModel,
            operators.OBJECT_OT_ImportFbxAnimation,
            
            # Rig operators
            operators.OBJECT_OT_GenRig,
            operators.OBJECT_OT_GenIK,
            operators.OBJECT_OT_RemoveIK,
            
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
            
            # Update operators
            operators.UpdateOperator,
            operators.StartServerOperator,
            operators.StopServerOperator,
            
            # UI panels
            ui.OBJECT_PT_RbxAnimations,
            ui.OBJECT_PT_RbxAnimations_Tool,
        )
        
        # Robust register: if already registered, unregister then re-register
        for cls in classes:
            try:
                bpy.utils.register_class(cls)
            except ValueError:
                try:
                    bpy.utils.unregister_class(cls)
                except Exception:
                    pass
                bpy.utils.register_class(cls)
        
        # Register properties
        try:
            ui.unregister_properties()
        except Exception:
            pass
        ui.register_properties()
        
        # Add import menu items
        # remove if already appended to avoid duplicates
        try:
            bpy.types.TOPBAR_MT_file_import.remove(file_import_extend)
        except Exception:
            pass
        bpy.types.TOPBAR_MT_file_import.append(file_import_extend)
        
        # Register animation update handler
        if core.on_animation_update not in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.append(core.on_animation_update)

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
        # Import modules safely
        from . import core, animation, rig, server, operators, ui
        # ensure any custom draw handlers are removed before we nuke classes
        try:
            from .operators.validation_ops import cleanup_validation_draw_handlers
            cleanup_validation_draw_handlers()
        except Exception:
            pass
        
        # Define classes after imports (same as register)
        classes = (
            # Import operators
            operators.OBJECT_OT_ImportModel,
            operators.OBJECT_OT_ImportFbxAnimation,
            
            # Rig operators
            operators.OBJECT_OT_GenRig,
            operators.OBJECT_OT_GenIK,
            operators.OBJECT_OT_RemoveIK,
            operators.OBJECT_OT_ToggleIKHelpers,
            
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
            
            # Update operators
            operators.UpdateOperator,
            operators.StartServerOperator,
            operators.StopServerOperator,
            
            # UI panels
            ui.OBJECT_PT_RbxAnimations,
            ui.OBJECT_PT_RbxAnimations_Tool,
        )
        
        # Unregister all classes (best-effort)
        for cls in reversed(classes):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
        
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
        
        # Remove animation update handler
        try:
            if core.on_animation_update in bpy.app.handlers.depsgraph_update_post:
                bpy.app.handlers.depsgraph_update_post.remove(core.on_animation_update)
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
