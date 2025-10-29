"""
Server management operators.
"""

import bpy
from ..server.server import start_server, stop_server


class StartServerOperator(bpy.types.Operator):
    bl_idname = "object.start_server"
    bl_label = "Start Animation Server"

    def execute(self, context):
        try:
            settings = getattr(context.scene, "rbx_anim_settings", None)
            port = settings.rbx_server_port if settings else None
            if not port:
                self.report({"ERROR"}, "Invalid server port")
                return {"CANCELLED"}
            if start_server(port):
                self.report({"INFO"}, f"Server started on port {port}")
                # Force UI refresh
                for area in context.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
            else:
                self.report({"ERROR"}, "Failed to start server - port may be in use")
        except Exception as e:
            self.report({"ERROR"}, f"Error starting server: {str(e)}")
        return {"FINISHED"}


class StopServerOperator(bpy.types.Operator):
    bl_idname = "object.stop_server"
    bl_label = "Stop Animation Server"

    def execute(self, context):
        try:
            stop_server()
            self.report({"INFO"}, "Server stopped")
            # Force UI refresh
            for area in context.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
        except Exception as e:
            self.report({"ERROR"}, f"Error stopping server: {str(e)}")
        return {"FINISHED"}
