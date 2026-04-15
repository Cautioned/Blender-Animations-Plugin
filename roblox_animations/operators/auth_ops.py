"""
Operators for Roblox OAuth 2.0 authentication.
"""

import bpy
from bpy.types import Operator


class OBJECT_OT_RbxOAuthLogin(Operator):
    bl_idname = "rbx.oauth_login"
    bl_label = "Log In to Roblox"
    bl_description = (
        "Authenticate with your Roblox account via the browser "
        "to allow importing private meshes"
    )

    def execute(self, context):
        from ..core import auth

        try:
            auth.start_login_async()
            self.report(
                {"INFO"},
                "Browser opened for Roblox login — waiting for callback…",
            )
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
        except Exception as exc:
            self.report({"ERROR"}, f"Login error: {exc}")

        return {"FINISHED"}

    @classmethod
    def poll(cls, context):
        from ..core import auth

        return not auth.is_login_in_progress() and not auth.is_logged_in()


class OBJECT_OT_RbxOAuthCancelLogin(Operator):
    bl_idname = "rbx.oauth_cancel_login"
    bl_label = "Cancel Login"
    bl_description = "Cancel the ongoing Roblox authentication"

    def execute(self, context):
        from ..core import auth

        auth.cancel_login()
        return {"FINISHED"}

    @classmethod
    def poll(cls, context):
        from ..core import auth

        return auth.is_login_in_progress()


class OBJECT_OT_RbxOAuthLogout(Operator):
    bl_idname = "rbx.oauth_logout"
    bl_label = "Log Out"
    bl_description = "Revoke stored Roblox authentication tokens"

    def execute(self, context):
        from ..core import auth

        try:
            auth.logout()
            self.report({"INFO"}, "Logged out of Roblox.")
        except Exception as exc:
            self.report({"ERROR"}, f"Logout error: {exc}")

        return {"FINISHED"}

    @classmethod
    def poll(cls, context):
        from ..core import auth

        return auth.is_logged_in()
