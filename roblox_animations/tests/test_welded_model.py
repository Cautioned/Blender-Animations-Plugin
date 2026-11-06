"""Optional private-model integration check; set RBX_WELD_TEST_MODEL to an RBXM.

Imports native primitive geometry and the complete rig graph. A second check
uses unweighted proxy geometry for MeshParts to exercise rigid binding without
remote FileMesh assets. Materials are excluded; no network or login is needed.
"""

import json
import os
from pathlib import Path
import unittest
from unittest import mock

import bpy

from ..core.rbxm import parse_rbxm
from ..operators.import_ops import _create_rbxl_scene_rig_scaffolds
from ..rig import creation, textures
from .test_rbxm_rebuild import clean_scene


@unittest.skipUnless(os.environ.get("RBX_WELD_TEST_MODEL"), "Set RBX_WELD_TEST_MODEL for private-model validation")
class TestWeldedModel(unittest.TestCase):
    def tearDown(self):
        clean_scene()

    def assertMatrixClose(self, actual, expected):
        self.assertLess(max(abs(actual[r][c] - expected[r][c]) for r in range(4) for c in range(4)), 0.001)

    def test_native_clothes_keep_placement_and_follow_animation_owners(self):
        self.check_model(include_meshpart_proxies=False)

    def test_rigid_meshpart_proxies_follow_animation_owners(self):
        self.check_model(include_meshpart_proxies=True)

    def check_model(self, include_meshpart_proxies):
        meta = parse_rbxm(Path(os.environ["RBX_WELD_TEST_MODEL"]).read_bytes())
        tested = 0
        for mode in ("RAW", "LOCAL_AXIS_EXTEND", "LOCAL_YAXIS_EXTEND", "CONNECT"):
            with self.subTest(mode=mode):
                clean_scene()
                scaffolds = _create_rbxl_scene_rig_scaffolds(
                    meta["scene_rigs"], meta["partAux"], bpy.context.scene.collection)
                for scaffold in scaffolds:
                    entries = [p for p in meta["partAux"] if p["inst_ref"] in scaffold["refs"]]
                    with mock.patch.object(textures, "apply_part_material"):
                        for entry in entries:
                            if entry.get("mesh_id") and not include_meshpart_proxies:
                                continue
                            geometry_entry = dict(entry)
                            if entry.get("mesh_id"):
                                geometry_entry["shape"] = "block"
                            if not geometry_entry.get("shape"):
                                continue
                            mesh_data = creation._build_primitive_mesh_data(geometry_entry)
                            self.assertIsNotNone(mesh_data)
                            creation._create_mesh_object_from_filemesh(
                                scaffold["parts"], entry["name"], mesh_data, entry)
                    bpy.context.view_layer.update()
                    objects = list(scaffold["parts"].objects)
                    self.assertTrue(objects)
                    # Invisible root plumbing is normally hidden by the mesh
                    # builder. Enable evaluation for this pose assertion too.
                    for obj in objects:
                        obj.hide_viewport = False
                        obj.hide_set(False)
                    bpy.context.view_layer.update()
                    before = {obj: obj.matrix_world.copy() for obj in objects}
                    # Model MeshParts keep their original names, referents and
                    # weld graph; only remote geometry/weights are substituted.
                    with mock.patch.object(creation, "fetch_and_parse_filemesh", return_value={"vertex_weights": []}):
                        creation.create_rig(mode, scaffold["meta_name"])
                    armature = next(obj for obj in bpy.data.objects
                                    if obj.type == "ARMATURE" and obj.get("rbx_rig_meta_name") == scaffold["meta_name"])
                    saved_meta = json.loads(bpy.data.objects[scaffold["meta_name"]]["RigMeta"])
                    mapping = saved_meta["meshToBoneRefs"]
                    names = {int(b["rbx_inst_ref"]): b.name for b in armature.data.bones if "rbx_inst_ref" in b}
                    bpy.context.view_layer.update()
                    target_before = {}
                    for obj in objects:
                        ref = int(obj["RBXInstRef"])
                        owner = mapping[str(ref)]
                        constraints = [c for c in obj.constraints if c.type == "CHILD_OF"]
                        self.assertEqual(len(constraints), 1, f"mesh #{ref}")
                        self.assertEqual(constraints[0].target, armature)
                        self.assertEqual(constraints[0].subtarget, names[owner])
                        self.assertMatrixClose(obj.matrix_world, before[obj])
                        target_before[owner] = armature.matrix_world @ armature.pose.bones[names[owner]].matrix
                        tested += 1
                    for owner in target_before:
                        bone = armature.pose.bones[names[owner]]
                        bone.rotation_mode = "XYZ"
                        bone.rotation_euler.z = 0.2
                    bpy.context.view_layer.update()
                    for obj in objects:
                        owner = mapping[str(obj["RBXInstRef"])]
                        target_now = armature.matrix_world @ armature.pose.bones[names[owner]].matrix
                        with self.subTest(ref=obj["RBXInstRef"], owner=owner, bone=names[owner]):
                            self.assertMatrixClose(obj.matrix_world, target_now @ target_before[owner].inverted() @ before[obj])
        self.assertGreater(tested, 0)
