"""
Server module for the Roblox Animations Blender Addon.

This module handles the HTTP server for live sync with Roblox Studio.
"""

from .server import *
from .handler import *
from .requests import *

def load_handler(dummy):
    """Handler for addon loading - checks for updates"""
    from ..operators.update_ops import UpdateOperator
    UpdateOperator.check_for_updates()

__all__ = [
    # Server
    'start_server', 'stop_server', 'is_server_running', 'get_server_status',
    
    # Handler
    'AnimationHandler',
    
    # Requests
    'process_pending_requests', 'execute_list_armatures', 'execute_in_main_thread',
    
    # Load handler
    'load_handler'
]
