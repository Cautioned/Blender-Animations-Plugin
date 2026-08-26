"""
HTTP server setup and management for live sync functionality.
"""

import socket
import socketserver
import time
import traceback
from typing import Optional

import bpy

from .handler import AnimationHandler
from .websocket import LiveSyncWebSocketServer
from .requests import execute_in_main_thread, pending_responses
from ..core.utils import get_armature_timeline_hash

# Global server state managed via Blender timers
server_instance: Optional["SafeTCPServer"] = None
server_should_run: bool = False
server_port: Optional[int] = None
_timer_registered: bool = False
websocket_server: Optional[LiveSyncWebSocketServer] = None
_last_websocket_publish: float = 0.0
_dirty_armatures: dict[str, float] = {}
_processing_websocket_sync: bool = False


def _live_sync_depsgraph_handler(_scene, depsgraph) -> None:
    """Mark subscribed actions dirty without hashing them on every graph update."""
    if websocket_server is None or _processing_websocket_sync:
        return
    subscribed = websocket_server.subscribed_armatures()
    if not subscribed:
        return
    try:
        action_changed = any(isinstance(update.id, bpy.types.Action) for update in depsgraph.updates)
    except Exception:
        action_changed = True
    if action_changed:
        dirty_at = time.monotonic()
        for armature in subscribed:
            _dirty_armatures[armature] = dirty_at


def _register_live_sync_handler() -> None:
    if _live_sync_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_live_sync_depsgraph_handler)


def _unregister_live_sync_handler() -> None:
    if _live_sync_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_live_sync_depsgraph_handler)


def _process_websocket_sync_requests() -> None:
    global _processing_websocket_sync
    if websocket_server is None:
        return
    for client, request in websocket_server.drain_sync_requests():
        request_id = request.get("request_id", "")
        trigger_hash = request.get("trigger_hash", "")
        armature = request.get("armature", "")
        task_id = f"ws:{request_id}:{time.time_ns()}"
        try:
            _processing_websocket_sync = True
            execute_in_main_thread(
                task_id,
                armature,
                request.get("target_bone_rest"),
                False,
                request.get("base_hash", ""),
            )
            success, payload = pending_responses.pop(task_id)
        except Exception as exc:
            success, payload = False, str(exc)
            traceback.print_exc()
        finally:
            _processing_websocket_sync = False
        websocket_server.publish_sync_response(
            client,
            request_id,
            trigger_hash,
            success,
            payload,
        )


def get_server_status() -> bool:
    """Return whether the live sync server is currently running."""
    running = bool(server_should_run and server_instance is not None)
    if running:
        ensure_server_timer_running()
    return running


def is_server_running() -> bool:
    """Backward-compatible alias for get_server_status."""
    return get_server_status()


class SafeTCPServer(socketserver.TCPServer):
    """TCP server with keepalive settings suitable for polling."""

    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass):
        self.address_family = socket.AF_INET
        super().__init__(server_address, RequestHandlerClass)

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        if hasattr(socket, "TCP_KEEPINTVL"):
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 60)
        if hasattr(socket, "TCP_KEEPCNT"):
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
        super().server_bind()


def _server_tick() -> Optional[float]:
    """Timer callback that services pending HTTP requests."""
    global _timer_registered

    if not server_should_run or server_instance is None:
        _timer_registered = False
        return None

    try:
        # handle_request respects the timeout set on the server instance
        server_instance.handle_request()
    except socket.timeout:
        pass
    except Exception as exc:
        if server_should_run:
            print(f"Blender Addon: Error in server request loop: {exc}")
            traceback.print_exc()

    _process_websocket_sync_requests()

    global _last_websocket_publish
    if websocket_server is not None:
        now = time.monotonic()
        for armature in websocket_server.drain_new_subscriptions():
            _dirty_armatures[armature] = now - 0.025
        ready = [name for name, dirty_at in _dirty_armatures.items() if now - dirty_at >= 0.025]
        for armature in ready:
            _dirty_armatures.pop(armature, None)
            websocket_server.publish_updates(
                lambda name, expected=armature: get_armature_timeline_hash(name) if name == expected else ""
            )
        # Slow fallback covers initial subscriptions, timeline settings, and
        # Blender builds that omit an Action dependency update.
        if now - _last_websocket_publish >= 2.0:
            _last_websocket_publish = now
            websocket_server.publish_updates(get_armature_timeline_hash)

    # Re-run quickly so we remain responsive without blocking Blender
    return 0.01


def ensure_server_timer_running() -> bool:
    """Ensure the request-loop timer is registered for an active server."""
    global _timer_registered

    if not server_should_run or server_instance is None:
        return False

    try:
        if bpy.app.timers.is_registered(_server_tick):
            _timer_registered = True
            return True
    except Exception:
        if _timer_registered:
            return True

    try:
        bpy.app.timers.register(_server_tick, first_interval=0.0, persistent=True)
        _timer_registered = True
        return True
    except Exception as exc:
        _timer_registered = False
        print(f"Blender Addon: Failed to register server request timer: {exc}")
        traceback.print_exc()
        return False


def _unregister_server_timer() -> None:
    global _timer_registered

    if not _timer_registered:
        return

    try:
        if bpy.app.timers.is_registered(_server_tick):
            bpy.app.timers.unregister(_server_tick)
    except ValueError:
        pass
    except Exception:
        pass
    finally:
        _timer_registered = False


def handle_blend_file_loaded(_dummy=None) -> None:
    """Restore the server request timer after Blender opens another file."""
    if server_should_run and server_instance is not None:
        _register_live_sync_handler()
        ensure_server_timer_running()


def start_server(port: int = 31337) -> bool:
    """Start the live sync server using Blender timers instead of threads."""
    global server_instance, server_should_run, server_port, _timer_registered, websocket_server, _last_websocket_publish

    if server_instance is not None:
        stop_server()

    try:
        _dirty_armatures.clear()
        server_instance = SafeTCPServer(("127.0.0.1", port), AnimationHandler)
        server_instance.timeout = 0  # Non-blocking select inside handle_request
        websocket_server = LiveSyncWebSocketServer(port + 1)
        websocket_server.start()
        _register_live_sync_handler()
        _last_websocket_publish = 0.0
        server_should_run = True
        server_port = port

        ensure_server_timer_running()

        print(f"Blender Addon: Server started and listening on port {port}")
        return True
    except Exception as exc:
        print(f"Blender Addon: Failed to start server: {exc}")
        traceback.print_exc()

        if server_instance is not None:
            try:
                server_instance.server_close()
            except Exception:
                pass

        if websocket_server is not None:
            websocket_server.stop()
            websocket_server = None
        _unregister_live_sync_handler()

        server_instance = None
        server_should_run = False
        server_port = None

        _unregister_server_timer()

        return False


def stop_server() -> None:
    """Stop the live sync server and clean up resources."""
    global server_instance, server_should_run, server_port, _timer_registered, websocket_server

    if server_instance is None:
        server_should_run = False
        server_port = None
        _unregister_live_sync_handler()
        if websocket_server is not None:
            websocket_server.stop()
            websocket_server = None
        _unregister_server_timer()
        return

    print("Blender Addon: Stop server called.")

    server_should_run = False
    _dirty_armatures.clear()
    _unregister_live_sync_handler()
    if websocket_server is not None:
        websocket_server.stop()
        websocket_server = None
    try:
        try:
            server_instance.server_close()
        except Exception as exc:
            print(f"Blender Addon: Error closing server socket: {exc}")
            traceback.print_exc()
    finally:
        server_instance = None
        server_port = None

    _unregister_server_timer()

    print("Blender Addon: Server shutdown complete.")
