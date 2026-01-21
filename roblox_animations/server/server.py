"""
HTTP server setup and management for live sync functionality.
"""

import socket
import socketserver
import traceback
from typing import Optional

import bpy

from .handler import AnimationHandler

# Global server state managed via Blender timers
server_instance: Optional["SafeTCPServer"] = None
server_should_run: bool = False
server_port: Optional[int] = None
_timer_registered: bool = False


def get_server_status() -> bool:
    """Return whether the live sync server is currently running."""
    return bool(server_should_run and server_instance is not None)


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

    # Re-run quickly so we remain responsive without blocking Blender
    return 0.01


def start_server(port: int = 31337) -> bool:
    """Start the live sync server using Blender timers instead of threads."""
    global server_instance, server_should_run, server_port, _timer_registered

    if server_instance is not None:
        stop_server()

    try:
        server_instance = SafeTCPServer(("127.0.0.1", port), AnimationHandler)
        server_instance.timeout = 0  # Non-blocking select inside handle_request
        server_should_run = True
        server_port = port

        if not _timer_registered:
            bpy.app.timers.register(_server_tick, first_interval=0.0)
            _timer_registered = True

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

        server_instance = None
        server_should_run = False
        server_port = None

        if _timer_registered:
            try:
                bpy.app.timers.unregister(_server_tick)
            except ValueError:
                pass
            _timer_registered = False

        return False


def stop_server() -> None:
    """Stop the live sync server and clean up resources."""
    global server_instance, server_should_run, server_port, _timer_registered

    if server_instance is None:
        return

    print("Blender Addon: Stop server called.")

    server_should_run = False
    try:
        try:
            server_instance.server_close()
        except Exception as exc:
            print(f"Blender Addon: Error closing server socket: {exc}")
            traceback.print_exc()
    finally:
        server_instance = None
        server_port = None

    if _timer_registered:
        try:
            bpy.app.timers.unregister(_server_tick)
        except ValueError:
            pass
        _timer_registered = False

    print("Blender Addon: Server shutdown complete.")
