"""
Roblox Animations Blender Addon.

Import Roblox rigs/places (.rbxm/.rbxl) and export animations back to
Roblox Studio, with optional live sync and OAuth-based Studio auth.
"""

import bpy
from bpy.types import AddonPreferences

# Import modules once at module level
from . import operators, ui, server


class RbxAnimationsPreferences(AddonPreferences):
    """Addon preferences for non-secret addon settings."""

    bl_idname = __name__

    roblox_content_path: bpy.props.StringProperty(
        name="Roblox Content Folder",
        description=(
            "Path to Roblox's 'content' folder (or a version/Versions folder "
            "containing it). Used for builtin meshes, fonts, and clothing "
            "compositing guides. Leave empty to derive it from the Roblox "
            "Studio install location automatically, or press Auto-Detect to "
            "fill it in."
        ),
        subtype="DIR_PATH",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, "roblox_content_path")
        row.operator(
            "preferences.rbxanims_auto_detect_content",
            text="Auto-Detect",
        )
        if not self.roblox_content_path:
            try:
                from .rig.filemesh import detect_roblox_content_dir

                detected = detect_roblox_content_dir()
            except Exception:
                detected = None
            if detected:
                layout.label(
                    text=f"Detected: {detected}", icon="CHECKMARK"
                )
            else:
                layout.label(
                    text="No Roblox Studio install found; the import falls "
                    "back to install-derived detection automatically.",
                    icon="INFO",
                )


class RbxAnimationsAutoDetectContent(bpy.types.Operator):
    """Derive the Roblox content folder from the Studio install location."""

    bl_idname = "preferences.rbxanims_auto_detect_content"
    bl_label = "Auto-Detect Roblox Content Folder"
    bl_description = (
        "Fill the Roblox content folder from the Roblox Studio install "
        "location (newest version wins)"
    )
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        from .rig.filemesh import detect_roblox_content_dir

        found = detect_roblox_content_dir()
        prefs = None
        package = (__name__ or "").split(".")
        for end in range(len(package), 0, -1):
            addon = context.preferences.addons.get(".".join(package[:end]))
            prefs = getattr(addon, "preferences", None) if addon else None
            if prefs is not None:
                break
        if prefs is None:
            self.report({"ERROR"}, "Could not locate the addon preferences.")
            return {"CANCELLED"}
        if not found:
            self.report(
                {"ERROR"},
                "No Roblox Studio install found; set the folder manually.",
            )
            return {"CANCELLED"}
        prefs.roblox_content_path = found
        self.report({"INFO"}, f"Roblox content folder: {found}")
        return {"FINISHED"}


# Define bl_info directly to avoid import issues
bl_info = {
    "name": "Roblox Animations Importer/Exporter",
    "description": "Plugin for importing roblox rigs and exporting animations.",
    "author": "Cautioned",
    "version": (3, 0, 1),
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
    _resolve_operator_class("OBJECT_OT_ImportRbxm", fallback_module="import_ops"),
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
    # _resolve_operator_class("OBJECT_OT_ToggleAutoPhysics"),
    # _resolve_operator_class("OBJECT_OT_AnalyzePhysics"),
    # _resolve_operator_class("OBJECT_OT_TogglePhysicsGhost"),
    # _resolve_operator_class("OBJECT_OT_ToggleRotationMomentum"),
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
    # Constraint operators
    _resolve_operator_class("OBJECT_OT_AutoConstraint"),
    _resolve_operator_class("OBJECT_OT_ManualConstraint"),
    # Weapon/accessory operators
    _resolve_operator_class("OBJECT_OT_AttachMeshToBone"),
    _resolve_operator_class("OBJECT_OT_ImportAndAttach"),
    # Server operators
    _resolve_operator_class("StartServerOperator"),
    _resolve_operator_class("StopServerOperator"),
    # OAuth operators
    _resolve_operator_class("OBJECT_OT_RbxOAuthLogin", fallback_module="auth_ops"),
    _resolve_operator_class("OBJECT_OT_RbxOAuthCancelLogin", fallback_module="auth_ops"),
    _resolve_operator_class("OBJECT_OT_RbxOAuthLogout", fallback_module="auth_ops"),
    # UI panels
    getattr(ui, "OBJECT_PT_RbxAnimations", None),
  
    # Addon preferences (must be last so bl_idname resolves correctly)
    RbxAnimationsAutoDetectContent,
    RbxAnimationsPreferences,
]
CLASSES = tuple(cls for cls in _classes if cls is not None)


def _safe_unregister_class(cls):

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


@bpy.app.handlers.persistent
def _on_blend_file_loaded(dummy):
    server.load_handler(dummy)


def _remove_blend_file_load_handlers():
    for handler in list(bpy.app.handlers.load_post):
        if (
            getattr(handler, "__name__", "") == _on_blend_file_loaded.__name__
            and getattr(handler, "__module__", "") == __name__
        ):
            try:
                bpy.app.handlers.load_post.remove(handler)
            except ValueError:
                pass


def file_import_extend(self, context):
    """Add import options to the file menu"""
    import_model_op = _resolve_operator_class("OBJECT_OT_ImportModel", fallback_module="import_ops")
    import_anim_op = _resolve_operator_class("OBJECT_OT_ImportFbxAnimation", fallback_module="import_ops")
    import_rbxm_op = _resolve_operator_class("OBJECT_OT_ImportRbxm", fallback_module="import_ops")
    if import_model_op is not None:
        self.layout.operator(import_model_op.bl_idname, text="Roblox Rig (.obj)")
    if import_anim_op is not None:
        self.layout.operator(
            import_anim_op.bl_idname,
            text="Animation for Roblox Rig (.fbx)",
        )
    if import_rbxm_op is not None:
        self.layout.operator(
            import_rbxm_op.bl_idname,
            text="Roblox Model/Place (.rbxm/.rbxl)",
        )


def register():
    """Register the addon"""

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

        _remove_blend_file_load_handlers()
        bpy.app.handlers.load_post.append(_on_blend_file_loaded)

    except Exception as e:
        print(f"Error registering Roblox Animations addon: {e}")
        import traceback

        traceback.print_exc()


def unregister():
    """Unregister the addon"""

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

        try:
            _remove_blend_file_load_handlers()
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
