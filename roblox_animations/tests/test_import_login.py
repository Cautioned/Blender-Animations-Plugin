"""Login is mandatory before the RBXM/RBXL importer opens or changes a scene."""

import unittest
from types import SimpleNamespace
from unittest import mock

from ..core import auth
from ..operators import import_ops


class TestImportLogin(unittest.TestCase):
    def setUp(self):
        self.online = self.enterContext(
            mock.patch.object(auth, "is_online_access_allowed", return_value=True)
        )
        self.logging_in = self.enterContext(
            mock.patch.object(auth, "is_login_in_progress", return_value=False)
        )
        self.headers = self.enterContext(
            mock.patch.object(auth, "get_auth_headers", return_value={})
        )
        self.operator = SimpleNamespace(
            report=mock.Mock(),
            files=[],
            properties=SimpleNamespace(filepath="fixture.rbxm"),
            _import_one=mock.Mock(return_value="FINISHED"),
        )
        self.context = SimpleNamespace(
            window_manager=SimpleNamespace(fileselect_add=mock.Mock())
        )
        self.enterContext(
            mock.patch("roblox_animations.rig.filemesh.release_import_cache")
        )
        self.enterContext(
            mock.patch("roblox_animations.rig.textures.release_import_byte_cache")
        )

    def invoke(self):
        return import_ops.OBJECT_OT_ImportRbxm.invoke(self.operator, self.context, None)

    def execute(self):
        return import_ops.OBJECT_OT_ImportRbxm.execute(self.operator, self.context)

    def test_logged_out_cannot_open_file_browser(self):
        self.assertEqual(self.invoke(), {"CANCELLED"})
        self.context.window_manager.fileselect_add.assert_not_called()
        self.assertIn("Log In to Roblox", self.operator.report.call_args.args[1])

    def test_direct_execute_cannot_import_without_login(self):
        self.assertEqual(self.execute(), {"CANCELLED"})
        self.operator._import_one.assert_not_called()

    def test_online_access_disabled_explains_required_setting(self):
        self.online.return_value = False
        self.assertEqual(self.execute(), {"CANCELLED"})
        self.headers.assert_not_called()
        self.operator._import_one.assert_not_called()
        self.assertIn("Online Access", self.operator.report.call_args.args[1])

    def test_incomplete_login_does_not_start_import(self):
        self.logging_in.return_value = True
        self.assertEqual(self.execute(), {"CANCELLED"})
        self.headers.assert_not_called()
        self.operator._import_one.assert_not_called()

    def test_valid_or_refreshed_login_allows_import(self):
        # get_auth_headers refreshes expired saved sessions. A stale local
        # is_logged_in flag must not reject a session that refreshes successfully.
        with mock.patch.object(auth, "is_logged_in", return_value=False):
            self.headers.return_value = {"Authorization": "Bearer test-only"}
            self.assertEqual(self.invoke(), {"RUNNING_MODAL"})
            self.context.window_manager.fileselect_add.assert_called_once_with(
                self.operator
            )
            self.assertEqual(self.execute(), {"FINISHED"})
            self.operator._import_one.assert_called_once_with(
                self.context, "fixture.rbxm"
            )

    def test_logout_while_file_browser_is_open_blocks_import(self):
        self.headers.side_effect = [{"Authorization": "Bearer test-only"}, {}]
        self.assertEqual(self.invoke(), {"RUNNING_MODAL"})
        self.assertEqual(self.execute(), {"CANCELLED"})
        self.operator._import_one.assert_not_called()

    def test_saved_login_that_cannot_refresh_is_rejected(self):
        with mock.patch.object(auth, "has_saved_login", return_value=True):
            self.assertEqual(self.execute(), {"CANCELLED"})
        self.operator._import_one.assert_not_called()

    def test_multi_file_import_is_blocked_before_first_file(self):
        self.operator.files = [
            SimpleNamespace(name="one.rbxm"),
            SimpleNamespace(name="two.rbxl"),
        ]
        self.operator.directory = "fixtures"
        self.assertEqual(self.execute(), {"CANCELLED"})
        self.operator._import_one.assert_not_called()
