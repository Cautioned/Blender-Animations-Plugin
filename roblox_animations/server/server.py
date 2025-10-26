"""
HTTP server setup and management for live sync functionality.
"""

import socket
import threading
import socketserver
from .handler import AnimationHandler


# Global server state
server_thread = None
server_instance = None
is_server_running = False


def get_server_status():
    """Get the current server running status"""
    return is_server_running


class SafeThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Thread-safe TCP server with keepalive settings"""
    allow_reuse_address = True
    daemon_threads = True
    timeout = None  # Remove timeout to keep connection alive
    
    def __init__(self, server_address, RequestHandlerClass):
        self.address_family = socket.AF_INET
        super().__init__(server_address, RequestHandlerClass)
        
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Add TCP keepalive settings
        if hasattr(socket, 'TCP_KEEPIDLE'):
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        if hasattr(socket, 'TCP_KEEPINTVL'):
            self.socket.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 60)
        if hasattr(socket, 'TCP_KEEPCNT'):
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
        super().server_bind()


def run_server_safely(server):
    """Run server with error handling"""
    global is_server_running
    print("Blender Addon: Server thread is now running.")
    try:
        server.timeout = 1  # Set a timeout so we can check is_server_running
        while is_server_running:
            try:
                server.handle_request()
            except socket.timeout:
                continue  # Just check is_server_running again
            except Exception as e:
                if is_server_running:
                    print(
                        f"Blender Addon: Error handling request: {e}, continuing...")
                    import traceback
                    traceback.print_exc()
    except Exception as e:
        print(
            f"Blender Addon: Server thread encountered a fatal error: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        print("Blender Addon: Server thread is shutting down.")
        is_server_running = False


def start_server(port=31337):
    """Start the server with improved error handling"""
    global server_thread, server_instance, is_server_running
    
    # Stop existing server if running
    if server_thread:
        stop_server()
    
    try:
        # Create server without timeout
        server_instance = SafeThreadedTCPServer(
            ("127.0.0.1", port), AnimationHandler)
        is_server_running = True
        print(f"Blender Addon: Server instance created for 127.0.0.1:{port}.")
        
        # Start server in thread
        server_thread = threading.Thread(
            target=lambda: run_server_safely(server_instance))
        server_thread.daemon = True
        server_thread.start()
        print("Blender Addon: Server thread started.")
        
        # Test if server is actually running
        try:
            test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_socket.settimeout(1)
            test_socket.connect(('127.0.0.1', port))
            test_socket.close()
            print(
                f"Blender Addon: Server started and listening on port {port}")
            return True
        except Exception as e:
            print(f"Blender Addon: Server connection test failed: {e}")
            print("Blender Addon: Server failed to start properly")
            stop_server()
            return False
            
    except Exception as e:
        print(f"Blender Addon: Failed to start server: {str(e)}")
        server_thread = None
        server_instance = None
        is_server_running = False
        return False


def stop_server():
    """Stop the server safely by sending a dummy request."""
    global server_thread, server_instance, is_server_running

    if not is_server_running:
        return

    print("Blender Addon: Stop server called.")
    
    try:
        # Get server address to connect to for the dummy request
        host, port = "127.0.0.1", server_instance.server_address[1]

        # Signal the server thread to stop
        is_server_running = False
        
        # Send a dummy connection to unblock handle_request()
        try:
            print(
                f"Blender Addon: Sending dummy connection to {host}:{port} to unblock server...")
            with socket.create_connection((host, port), timeout=1) as sock:
                # The connection itself is enough, we don't need to send data
                pass 
            print("Blender Addon: Dummy connection sent.")
        except Exception as e:
            print(
                f"Blender Addon: Could not send dummy connection, waiting for timeout: {e}")

        # Wait for the thread to finish
        if server_thread and server_thread.is_alive():
            print("Blender Addon: Waiting for server thread to join...")
            server_thread.join(timeout=2)
            if server_thread.is_alive():
                print("Blender Addon: Server thread did not join in time.")
        
        # Now that the thread is stopped, clean up the server instance
        if server_instance:
            try:
                print("Blender Addon: Closing server socket...")
                server_instance.server_close()
                print("Blender Addon: Server socket closed.")
            except Exception as e:
                print(f"Blender Addon: Error closing server socket: {e}")
            
    except Exception as e:
        print(f"Blender Addon: Error stopping server: {str(e)}")
    finally:
        # Ensure all globals are reset
        print("Blender Addon: Finalizing server shutdown.")
        server_thread = None
        server_instance = None
        is_server_running = False
