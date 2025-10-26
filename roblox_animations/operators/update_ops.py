"""
Update and server management operators.
"""

import re
import urllib.request
import bpy
from bpy.types import Operator
from ..server.server import start_server, stop_server, is_server_running
from ..core.constants import version


class UpdateOperator(bpy.types.Operator):
    bl_idname = "my_plugin.update"
    bl_label = "Check for Updates"
    bl_description = "Check for any New Updates"

    def check_for_updates(self):
        # Replace with your Pastebin link
        url = "https://pastebin.com/raw/DhTbba6C"

        try:
            response = urllib.request.urlopen(url)
            new_code = response.read().decode()

            # Extract the version number from the new code
            match = re.search(r"version = (\d+\.\d+)", new_code)
            if match:
                new_version = float(match.group(1))
                if new_version > version:
                    self.report(
                        {"INFO"},
                        "Update Available ⚠️: v"
                        + str(new_version)
                        + " https://pastebin.com/raw/DhTbba6C",
                    )
                else:
                    self.report({"INFO"}, "No Updates Available 🙁")
            else:
                self.report({"ERROR"}, "Failed to check for updates.🔌")

        except Exception as e:
            self.report({"ERROR"}, str(e))

    def execute(self, context):
        self.check_for_updates()
        return {"FINISHED"}


class StartServerOperator(bpy.types.Operator):
    bl_idname = "object.start_server"
    bl_label = "Start Animation Server"

    def execute(self, context):
        try:
            port = context.scene.rbx_server_port
            if start_server(port):
                self.report({'INFO'}, f"Server started on port {port}")
                # Force UI refresh
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
            else:
                self.report(
                    {'ERROR'}, "Failed to start server - port may be in use")
        except Exception as e:
            self.report({'ERROR'}, f"Error starting server: {str(e)}")
        return {'FINISHED'}


class StopServerOperator(bpy.types.Operator):
    bl_idname = "object.stop_server"
    bl_label = "Stop Animation Server"

    def execute(self, context):
        try:
            stop_server()
            self.report({'INFO'}, "Server stopped")
            # Force UI refresh
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        except Exception as e:
            self.report({'ERROR'}, f"Error stopping server: {str(e)}")
        return {'FINISHED'}
