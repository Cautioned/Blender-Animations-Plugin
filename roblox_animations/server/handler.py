"""
HTTP request handler for the animation server.
"""

import json
import time
import urllib.parse
import http.server
import traceback
import bpy
from ..core.utils import get_cached_armatures, get_cached_armature_hash
from .requests import (
    pending_requests,
    pending_responses,
)


class AnimationHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for animation server endpoints"""

    def get_available_armatures(self):
        """Get list of available armatures and their properties"""
        # Use cached armature list for better performance
        cached_armatures = get_cached_armatures()
        armatures = []
        for armature_name in cached_armatures:
            obj = bpy.data.objects.get(armature_name)
            if obj:  # Double-check object still exists
                armature_info = {
                    "name": obj.name,
                    "bones": [bone.name for bone in obj.data.bones],
                    "num_bones": len(obj.data.bones),
                    "has_animation": bool(
                        obj.animation_data and obj.animation_data.action
                    ),
                    "frame_range": [
                        bpy.context.scene.frame_start,
                        bpy.context.scene.frame_end,
                    ]
                    if obj.animation_data
                    else None,
                }
                armatures.append(armature_info)
        return armatures

    def do_GET(self):
        """Handle GET requests"""
        if self.path.startswith("/animation_status"):
            query_components = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query
            )
            armature_name = query_components.get("armature", [None])[0]
            # Use unquote_plus to handle both %20 and + for spaces consistently
            if armature_name:
                armature_name = urllib.parse.unquote_plus(armature_name)
            last_known_hash = query_components.get("last_known_hash", [""])[0]

            if not armature_name:
                self.send_detailed_error(
                    400, "Bad Request", "Armature name not provided"
                )
                return

            obj = bpy.data.objects.get(armature_name)
            if not (obj and obj.type == "ARMATURE"):
                self.send_detailed_error(404, "Not Found", "Armature not found")
                return

            # Always use cached combined hash (action + timeline) - the caching mechanism handles staleness
            current_hash = get_cached_armature_hash(armature_name)

            has_update = current_hash != last_known_hash

            response_data = {
                "has_update": has_update,
                "hash": current_hash,
            }

            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response_data).encode("utf-8"))
            return

        elif self.path == "/list_armatures":
            print("Blender Addon: Received GET request for /list_armatures")
            try:
                task_id = str(time.time())
                # Add a specific request for listing armatures
                pending_requests.append(("list_armatures", task_id))
                print(f"Blender Addon: Queued task {('list_armatures', task_id)}")

                # Process immediately to avoid timer delay
                from .requests import process_pending_requests

                process_pending_requests()

                start_time = time.time()
                while task_id not in pending_responses:
                    if time.time() - start_time > 5:  # 5 second timeout
                        print("Blender Addon: Request for /list_armatures timed out.")
                        self.send_detailed_error(408, "Request timeout")
                        return
                    time.sleep(0.01)

                print(f"Blender Addon: Found response for task {task_id}")
                success, data = pending_responses.pop(task_id)

                if not success:
                    print(f"Blender Addon: Task {task_id} failed. Sending 500 error.")
                    self.send_detailed_error(500, data)
                    return

                # Send response from main thread
                print(
                    f"Blender Addon: Sending success response for /list_armatures. Data length: {len(data)}"
                )
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)  # data is already encoded
            except Exception as e:
                error_msg = f"Server error: {str(e)}"
                print(error_msg)
                traceback.print_exc()
                self.send_detailed_error(500, error_msg)

            return

        elif self.path.startswith("/get_bone_rest/"):
            armature_name_encoded = self.path.split("/")[-1]
            # Use quote_plus to handle both %20 and + for spaces consistently
            armature_name = urllib.parse.unquote_plus(armature_name_encoded)
            try:
                task_id = str(time.time())
                pending_requests.append(("get_bone_rest", task_id, armature_name))

                # Process immediately to avoid timer delay
                from .requests import process_pending_requests

                process_pending_requests()

                start_time = time.time()
                while task_id not in pending_responses:
                    if time.time() - start_time > 5:
                        self.send_detailed_error(408, "Request timeout")
                        return
                    time.sleep(0.01)

                success, data = pending_responses.pop(task_id)

                if not success:
                    self.send_detailed_error(500, data)
                    return

                # Send JSON data
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            except Exception as e:
                error_msg = f"Server error: {str(e)}"
                print(error_msg)
                traceback.print_exc()
                self.send_detailed_error(500, error_msg)

            return

        elif self.path.startswith("/export_animation/"):
            armature_name_encoded = self.path.split("/")[-1]
            # Use quote_plus to handle both %20 and + for spaces consistently
            armature_name = urllib.parse.unquote_plus(armature_name_encoded)
            try:
                task_id = str(time.time())
                pending_requests.append(("export_animation", task_id, armature_name))

                # Process immediately to avoid timer delay
                from .requests import process_pending_requests

                process_pending_requests()

                start_time = time.time()
                while task_id not in pending_responses:
                    if time.time() - start_time > 5:
                        self.send_detailed_error(408, "Request timeout")
                        return
                    time.sleep(0.01)

                success, data = pending_responses.pop(task_id)

                if not success:
                    self.send_detailed_error(500, data)
                    return

                # Send binary data directly without base64 encoding
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)  # data should already be bytes

            except Exception as e:
                error_msg = f"Server error: {str(e)}"
                print(error_msg)
                traceback.print_exc()
                self.send_detailed_error(500, error_msg)

        else:
            self.send_detailed_error(404, "Not Found", "Invalid endpoint")

    def do_POST(self):
        """Handle POST requests for importing animations from Roblox"""
        if self.path.startswith("/import_animation"):
            # Parse target armature from query parameters
            query_components = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query
            )
            target_armature = query_components.get("armature", [None])[0]
            # Use unquote_plus to handle both %20 and + for spaces consistently
            if target_armature:
                target_armature = urllib.parse.unquote_plus(target_armature)

            content_length = int(self.headers["Content-Length"])
            post_data = self.rfile.read(content_length)

            try:
                import zlib

                # Try to decompress, assuming zlib compressed data first
                try:
                    decompressed = zlib.decompress(post_data, 16 + zlib.MAX_WBITS)
                except zlib.error:
                    # If decompression fails, assume it's raw JSON
                    decompressed = post_data

                animation_data = json.loads(decompressed.decode("utf-8"))

                from .requests import pending_requests, pending_responses

                # Queue import operation for main thread with target armature
                task_id = str(time.time())
                pending_requests.append(
                    ("import", task_id, animation_data, target_armature)
                )

                # Process immediately to avoid timer delay
                from .requests import process_pending_requests

                process_pending_requests()

                # Wait for response
                start_time = time.time()
                while task_id not in pending_responses:
                    if time.time() - start_time > 5:
                        self.send_detailed_error(408, "Request timeout")
                        return
                    time.sleep(0.01)

                success, message = pending_responses.pop(task_id)

                if success:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True}).encode())
                else:
                    self.send_detailed_error(500, message)

            except Exception as e:
                self.send_detailed_error(500, str(e))
                traceback.print_exc()
        else:
            self.send_detailed_error(404, "Not Found")

    def do_OPTIONS(self):
        """Handle preflight requests"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def send_detailed_error(self, code, message, details=None):
        """Send an error with detailed information"""
        print(
            f"Blender Addon: Sending error - Code: {code}, Message: {message}, Details: {details}"
        )
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        error_data = {
            "error": True,
            "code": code,
            "message": message,
            "details": details,
        }

        self.wfile.write(json.dumps(error_data).encode("utf-8"))
