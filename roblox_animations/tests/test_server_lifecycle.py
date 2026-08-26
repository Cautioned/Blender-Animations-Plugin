import importlib
import unittest
from unittest import mock

from ..server import server as server_module

importlib.reload(server_module)


class TestServerLifecycle(unittest.TestCase):
    def tearDown(self):
        server_module.server_instance = None
        server_module.server_should_run = False
        server_module.server_port = None
        server_module._timer_registered = False

    def test_status_reregisters_request_timer_after_file_load(self):
        server_module.server_instance = object()
        server_module.server_should_run = True
        server_module._timer_registered = True

        with mock.patch.object(
            server_module.bpy.app.timers,
            "is_registered",
            return_value=False,
        ):
            with mock.patch.object(server_module.bpy.app.timers, "register") as register:
                self.assertTrue(server_module.get_server_status())

        register.assert_called_once_with(
            server_module._server_tick,
            first_interval=0.0,
            persistent=True,
        )
        self.assertTrue(server_module._timer_registered)


if __name__ == "__main__":
    unittest.main()
