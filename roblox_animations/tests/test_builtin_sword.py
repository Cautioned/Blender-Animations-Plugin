"""The supplied classic sword RBXM imports real geometry and attaches to a rig."""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bpy

from ..core.rbxm import parse_rbxm
from ..operators import import_ops
from ..rig import filemesh
from ..animation.serialization import serialize
from .test_rbxm_rebuild import import_binary, template_binary, clean_scene


class TestBuiltinSword(unittest.TestCase):
    def tearDown(self):
        import_ops._pending_weapon_import.clear()
        clean_scene()

    def test_exact_weapon_file_imports_and_attaches(self):
        raw = base64.b64decode(
            (Path(__file__).parent / "fixtures/sword_handle.rbxm.b64").read_text()
        )
        meta = parse_rbxm(raw)
        self.assertEqual(meta["weaponGrip"][0]["bone"], "Right Arm")
        self.assertEqual(meta["partAux"][0]["mesh_id"], "rbxasset://fonts/sword.mesh")
        arm, _ = import_binary("TemplateR6", template_binary("TemplateR6"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sword_handle.rbxm"
            path.write_bytes(raw)
            with mock.patch.object(
                import_ops, "_require_rbxm_login", return_value=True
            ):
                self.assertEqual(
                    bpy.ops.object.rbxanims_import_rbxm(filepath=str(path)),
                    {"FINISHED"},
                )
        swords = [
            obj
            for obj in bpy.context.scene.objects
            if obj.type == "MESH" and len(obj.data.polygons) == 198
        ]
        self.assertEqual(len(swords), 1)
        sword = swords[0]
        self.assertEqual(len(sword.data.polygons), 198)
        self.assertEqual(len(sword.data.uv_layers), 1)
        images = [
            n.image
            for mat in sword.data.materials
            if mat and mat.use_nodes
            for n in mat.node_tree.nodes
            if n.type == "TEX_IMAGE" and n.image
        ]
        self.assertTrue(any(tuple(image.size) == (128, 128) for image in images))
        bone = arm.pose.bones["Sword Mesh"]
        self.assertEqual(bone.parent.name, "Right Arm")
        self.assertTrue(
            any(
                c.type == "CHILD_OF" and c.target == arm and c.subtarget == "Sword Mesh"
                for c in sword.constraints
            )
        )
        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_end = 2
        for frame, x in ((1, 0), (2, 0.25)):
            bone.location.x = x
            bone.keyframe_insert(data_path="location", frame=frame)
        result = serialize(arm)
        self.assertIn("Sword Mesh", result["kfs"][-1]["kf"])

    def test_builtin_assets_need_no_local_install_or_network(self):
        filemesh.release_import_cache()
        with (
            mock.patch.object(filemesh, "_roblox_content_dirs", return_value=[]),
            mock.patch.object(
                filemesh,
                "_fetch_url_bytes",
                side_effect=AssertionError("Unexpected network request"),
            ),
        ):
            mesh = filemesh.fetch_and_parse_filemesh(
                "rbxasset://fonts/sword.mesh", allow_local_paths=False
            )
            self.assertEqual(len(mesh["faces"]), 198)
            self.assertEqual(len(mesh["uvs"]), 594)
            texture = filemesh._resolve_rbxasset_path(
                "rbxasset://textures/SwordTexture.png"
            )
            self.assertTrue(texture.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
