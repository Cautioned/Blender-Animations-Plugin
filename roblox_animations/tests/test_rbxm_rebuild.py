"""RBXM import -> all rebuild modes -> independently reconstructed Roblox poses.

Uses binary fixtures generated from Studio's R6/R15 capture and a skewed Bone
chain. No assets or network are required.

Run tests: blender --background --factory-startup --python <this_file>
Generate Studio cases: append -- <output_directory> to that command.
Serve the output: python -m http.server 18764 --bind 127.0.0.1 --directory <output_directory>
Then run rebuild_playback_batch.lua in the Studio command bar.
"""

import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import bpy
from mathutils import Matrix

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "roblox_animations.tests"

from . import test_rbxm as binary
from .test_studio_template_roundtrip import templates
from ..animation.serialization import serialize
from ..core.constants import get_transform_to_blender
from ..core.utils import (
    cf_to_mat,
    mat_to_cf,
    to_matrix,
    get_action_fcurves,
    invalidate_armature_cache,
)
from ..operators import rig_ops
from ..server import requests

MODES = ("RAW", "LOCAL_AXIS_EXTEND", "LOCAL_YAXIS_EXTEND", "CONNECT")


def template_binary(name):
    fixture = templates()[name]
    names = list(fixture["parts"])
    refs = {name: i + 1 for i, name in enumerate(names)}
    joints = fixture["joints"]
    joint_refs = list(range(1000, 1000 + len(joints)))
    chunks = [
        binary._inst_chunk(1, "Model", [0]),
        binary._inst_chunk(2, "Part", list(refs.values())),
        binary._inst_chunk(3, "Motor6D", joint_refs),
    ]
    chunks += [
        binary._prop_chunk(1, "Name", 1, binary._enc_string(name)),
        binary._prop_chunk(
            2, "Name", 1, b"".join(binary._enc_string(n) for n in names)
        ),
        binary._prop_chunk(
            2, "CFrame", 0x10, binary._enc_cframes([fixture["parts"][n] for n in names])
        ),
        binary._prop_chunk(
            2, "Size", 0x0E, binary._enc_vector3s([(1, 1, 1)] * len(names))
        ),
        binary._prop_chunk(
            3, "Name", 1, b"".join(binary._enc_string(j["part1"]) for j in joints)
        ),
    ]
    for key in ("Part0", "Part1"):
        chunks.append(
            binary._prop_chunk(
                3,
                key,
                0x13,
                binary._enc_referents([refs[j[key.lower()]] for j in joints]),
            )
        )
    for key in ("C0", "C1"):
        chunks.append(
            binary._prop_chunk(
                3, key, 0x10, binary._enc_cframes([j[key.lower()] for j in joints])
            )
        )
    chunks.append(
        binary._prnt_chunk(
            [0] + list(refs.values()) + joint_refs,
            [-1] + [0] * len(names) + [refs[j["part0"]] for j in joints],
        )
    )
    return binary._rbxm(chunks, 3, 1 + len(names) + len(joints))


def bone_binary():
    chunks = [
        binary._inst_chunk(1, "Model", [0]),
        binary._inst_chunk(2, "Part", [1]),
        binary._inst_chunk(3, "Bone", [2, 3, 4]),
    ]
    rest = [
        Matrix.Translation((0, 1, 0)) @ Matrix.Rotation(0.3, 4, "Y"),
        Matrix.Translation((2, 0.4, 0.3)) @ Matrix.Rotation(-0.4, 4, "X"),
        Matrix.Translation((0.2, 1.3, 0.4)) @ Matrix.Rotation(0.2, 4, "Z"),
    ]
    chunks += [
        binary._prop_chunk(1, "Name", 1, binary._enc_string("SkewedBones")),
        binary._prop_chunk(2, "Name", 1, binary._enc_string("Body")),
        binary._prop_chunk(
            2,
            "CFrame",
            0x10,
            binary._enc_cframes([mat_to_cf(Matrix.Translation((3, 2, 1)))]),
        ),
        binary._prop_chunk(2, "Size", 0x0E, binary._enc_vector3s([(1, 1, 1)])),
        binary._prop_chunk(
            3,
            "Name",
            1,
            b"".join(binary._enc_string(n) for n in ("Root", "Middle", "Tip")),
        ),
        binary._prop_chunk(
            3, "CFrame", 0x10, binary._enc_cframes([mat_to_cf(m) for m in rest])
        ),
        binary._prnt_chunk([0, 1, 2, 3, 4], [-1, 0, 1, 2, 3]),
    ]
    return binary._rbxm(chunks, 3, 5)


def clean_scene():
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    invalidate_armature_cache()


def import_binary(name, data):
    clean_scene()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / (name + ".rbxm")
        path.write_bytes(data)
        # These offline binary/geometry fixtures do not need real credentials.
        # The importer login boundary has separate tests in test_import_login.
        with mock.patch(
            "roblox_animations.operators.import_ops._require_rbxm_login",
            return_value=True,
        ):
            assert bpy.ops.object.rbxanims_import_rbxm(filepath=str(path)) == {
                "FINISHED"
            }
    armature = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    meta = next(o for o in bpy.data.objects if "RigMeta" in o)
    return armature, meta


def rebuild(armature, meta, mode, source="armature"):
    source_name = armature.name if source == "armature" else meta.name
    rig_ops.OBJECT_OT_GenRig.rig_meta_items_cache[:] = [(source_name, source_name, "")]
    assert bpy.ops.object.rbxanims_genrig(
        pr_rig_meta_name=source_name, pr_rigging_type=mode
    ) == {"FINISHED"}
    return bpy.context.object


def animate_and_export(armature, meta):
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.render.fps_base = 1
    scene.frame_start, scene.frame_end = 1, 13
    scene.rbx_anim_settings.rbx_full_range_bake = True
    for index, bone in enumerate(armature.pose.bones):
        if not bone.bone.get("is_transformable"):
            continue
        bone.rotation_mode = "XYZ"
        for frame, amount in ((1, 0), (7, 0.13), (13, 0)):
            bone.location = (amount * (1 + index / 20), amount / 4, -amount / 3)
            bone.rotation_euler = (0, 0, amount)
            bone.keyframe_insert(data_path="location", frame=frame)
            bone.keyframe_insert(data_path="rotation_euler", frame=frame)
    for curve in get_action_fcurves(armature.animation_data.action):
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"
    invalidate_armature_cache()
    baked = serialize(armature)
    back = get_transform_to_blender().inverted()
    expected = []
    for frame in range(1, 14):
        scene.frame_set(frame)
        evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
        poses = {
            b.name: mat_to_cf(
                back
                @ b.matrix
                @ to_matrix(b.bone["nicetransform"]).inverted()
                @ to_matrix(b.bone["transform1"]).inverted()
            )
            for b in evaluated.pose.bones
        }
        expected.append({"t": (frame - 1) / 30, "poses": poses, "onFrame": True})
    return {
        "baked": baked,
        "expected": expected,
        "metadata": json.loads(meta["RigMeta"]),
    }


class TestRbxmRebuild(unittest.TestCase):
    def tearDown(self):
        clean_scene()

    def assert_matrix(self, a, b):
        self.assertLess(
            max(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb)), 0.0003
        )

    def check_roundtrip(self, case):
        root = case["metadata"]["rig"]
        channels = {}
        for row in case["baked"]["kfs"]:
            for name, pose in row["kf"].items():
                channels.setdefault(name, []).append((row["t"], cf_to_mat(pose[0])))

        def sample_channel(name, time):
            keys = channels.get(name)
            if not keys:
                return Matrix.Identity(4)
            if time <= keys[0][0]:
                return keys[0][1]
            for (start, a), (end, b) in zip(keys, keys[1:]):
                if time <= end:
                    return a.lerp(b, (time - start) / (end - start))
            return keys[-1][1]

        for sample in case["expected"]:

            def check(node, parent=None):
                name = node["jname"]
                delta = sample_channel(name, sample["t"])
                if parent is None:
                    world = cf_to_mat(node["transform"]) @ delta
                else:
                    world = (
                        parent
                        @ cf_to_mat(node["jointtransform0"])
                        @ delta
                        @ cf_to_mat(node["jointtransform1"]).inverted()
                    )
                self.assert_matrix(world, cf_to_mat(sample["poses"][name]))
                for child in node.get("children", []):
                    check(child, world)

            check(root)

    def check_modes(self, name, data):
        armature, meta = import_binary(name, data)
        metadata = meta["RigMeta"]
        names = set(armature.data.bones.keys())
        meshes = {
            o.name: o.matrix_world.copy() for o in bpy.data.objects if o.type == "MESH"
        }
        for source in ("armature", "metadata"):
            for mode in MODES:
                with self.subTest(rig=name, source=source, mode=mode):
                    armature = rebuild(armature, meta, mode, source)
                    self.assertEqual(set(armature.data.bones.keys()), names)
                    self.assertEqual(meta["RigMeta"], metadata)
                    self.assertEqual(armature["rbx_rigging_type"], mode)
                    self.assertEqual(
                        bpy.context.scene.rbx_anim_settings.rbx_anim_armature,
                        armature.name,
                    )
                    bpy.context.view_layer.update()
                    for mesh_name, rest in meshes.items():
                        mesh = bpy.data.objects[mesh_name]
                        self.assert_matrix(mesh.matrix_world, rest)
                        self.assertTrue(
                            all(
                                c.target == armature
                                for c in mesh.constraints
                                if c.type == "CHILD_OF"
                            )
                        )
                    for bone in armature.data.bones:
                        self.assertGreater(bone.length, 0.009)
                        if (
                            mode == "CONNECT"
                            and bone.parent
                            and len(bone.children) == 1
                        ):
                            self.assertLess(
                                (bone.tail_local - bone.children[0].head_local).length,
                                0.0001,
                            )
                    self.check_roundtrip(animate_and_export(armature, meta))
                    bpy.context.scene.frame_set(1)

    def test_r6_all_modes(self):
        self.check_modes("TemplateR6", template_binary("TemplateR6"))

    def test_r15_all_modes(self):
        self.check_modes("TemplateR15", template_binary("TemplateR15"))

    def test_bone_all_modes(self):
        self.check_modes("SkewedBones", bone_binary())

    def test_deform_animation_import_preserves_rebuilt_axes(self):
        armature, meta = import_binary("SkewedBones", bone_binary())
        delta = Matrix.Translation((0.15, -0.1, 0.2)) @ Matrix.Rotation(0.35, 4, "Z")
        animation = {
            "t": 1,
            "is_deform_bone_rig": True,
            "export_info": {"fps": 30},
            "kfs": [
                {
                    "t": 0,
                    "kf": {
                        name: [mat_to_cf(Matrix.Identity(4)), "Linear", "Out"]
                        for name in ("Root", "Middle", "Tip")
                    },
                },
                {
                    "t": 1,
                    "kf": {
                        name: [mat_to_cf(delta), "Linear", "Out"]
                        for name in ("Root", "Middle", "Tip")
                    },
                },
            ],
        }
        for mode in MODES:
            with self.subTest(mode=mode):
                armature = rebuild(armature, meta, mode)
                requests.execute_import_animation(
                    "rebuild-test", animation, armature.name
                )
                self.assertTrue(requests.pending_responses.pop("rebuild-test")[0])
                bpy.context.scene.frame_set(30)
                result = serialize(armature)
                final = result["kfs"][-1]["kf"]
                for name in ("Root", "Middle", "Tip"):
                    self.assert_matrix(cf_to_mat(final[name][0]), delta)

    def test_local_axis_uses_dominant_axis(self):
        armature, meta = import_binary("SkewedBones", bone_binary())
        armature = rebuild(armature, meta, "RAW")
        bone = armature.data.bones["Root"]
        original = bone.matrix_local.copy()
        target = armature.data.bones["Middle"].head_local.copy()
        local = original.inverted() @ target
        self.assertGreater(abs(local.x), abs(local.y))
        armature = rebuild(armature, meta, "LOCAL_AXIS_EXTEND")
        expected = original.translation + original.to_3x3().col[0] * local.x
        self.assertLess(
            (armature.data.bones["Root"].tail_local - expected).length, 0.0001
        )
        armature = rebuild(armature, meta, "LOCAL_YAXIS_EXTEND")
        expected = original.translation + original.to_3x3().col[1] * local.y
        self.assertLess(
            (armature.data.bones["Root"].tail_local - expected).length, 0.0001
        )

    def test_older_import_resolves_renamed_metadata(self):
        armature, meta = import_binary("TemplateR6", template_binary("TemplateR6"))
        del armature["rbx_rig_meta_name"]
        armature.name = "My renamed armature"
        meta.name = "__RenamedMeta"
        armature = rebuild(armature, meta, "CONNECT")
        self.assertEqual(len(armature.data.bones), 7)
        self.assertEqual(armature["rbx_rig_meta_name"], meta.name)

    def test_missing_metadata_does_not_create_dummy_or_delete_armature(self):
        armature, meta = import_binary("TemplateR6", template_binary("TemplateR6"))
        bpy.data.objects.remove(meta, do_unlink=True)
        before = set(bpy.data.objects.keys())
        with self.assertRaisesRegex(RuntimeError, "Original rig metadata"):
            rebuild(armature, None, "CONNECT")
        self.assertEqual(set(bpy.data.objects.keys()), before)
        self.assertEqual(len(armature.data.bones), 7)

    def test_deform_skin_motion_matches_in_every_mode(self):
        armature, meta = import_binary("SkewedBones", bone_binary())
        delta = Matrix.Translation((0.2, -0.1, 0.3)) @ Matrix.Rotation(0.45, 4, "X")
        reference = None
        for mode in MODES:
            with self.subTest(mode=mode):
                armature = rebuild(armature, meta, mode)
                mesh = bpy.data.meshes.new("SkinVerification")
                mesh.from_pydata([(3, -1, 2), (4, -1, 2), (3, -2, 2)], [], [(0, 1, 2)])
                obj = bpy.data.objects.new("SkinVerification", mesh)
                bpy.context.scene.collection.objects.link(obj)
                group = obj.vertex_groups.new(name="Root")
                group.add([0, 1, 2], 1, "REPLACE")
                modifier = obj.modifiers.new("Skin", "ARMATURE")
                modifier.object = armature
                bone = armature.pose.bones["Root"]
                nice = to_matrix(bone.bone["nicetransform"])
                bone.matrix_basis = nice.inverted() @ delta @ nice
                bpy.context.view_layer.update()
                evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
                vertices = [v.co.copy() for v in evaluated.data.vertices]
                if reference is None:
                    reference = vertices
                    self.assertGreater((vertices[0] - mesh.vertices[0].co).length, 0.01)
                else:
                    for a, b in zip(vertices, reference):
                        self.assertLess((a - b).length, 0.0001)
                bpy.data.objects.remove(obj, do_unlink=True)

    def test_selected_armature_rebuild_keeps_sibling_controls(self):
        armature, meta = import_binary("TemplateR6", template_binary("TemplateR6"))
        control = bpy.data.objects.new("Controls", bpy.data.armatures.new("Controls"))
        armature.users_collection[0].objects.link(control)
        armature.name = "ZZZ Selected rig"
        rebuilt = rebuild(armature, meta, "CONNECT")
        self.assertEqual(len(rebuilt.data.bones), 7)
        self.assertIs(bpy.data.objects.get(control.name), control)
        rebuilt = rebuild(rebuilt, meta, "RAW", source="metadata")
        self.assertEqual(len(rebuilt.data.bones), 7)
        self.assertIs(bpy.data.objects.get(control.name), control)


def generate_playback_cases(output_directory):
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    fixtures = [(name, template_binary(name)) for name in ("TemplateR6", "TemplateR15")]
    fixtures.append(("SkewedBones", bone_binary()))
    for name, data in fixtures:
        armature, meta = import_binary(name, data)
        for source in ("armature", "metadata"):
            for mode in MODES:
                armature = rebuild(armature, meta, mode, source)
                case = animate_and_export(armature, meta)
                case.update(id=f"{name}_{source}_{mode}", rig=name)
                (output / (case["id"] + ".json")).write_text(
                    json.dumps(case), encoding="utf-8"
                )
                manifest.append(case["id"])
    root = Path(__file__).resolve().parents[2]
    components = root / "src/ServerScriptService/BlenderAnimationsInternal/Components"
    sources = {
        name: (components / (name + ".lua")).read_text(encoding="utf-8-sig")
        for name in ("Rig", "RigPart", "Pose")
    }
    sources["Runner"] = (
        Path(__file__)
        .with_name("rebuild_playback_studio.lua")
        .read_text(encoding="utf-8")
    )
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (output / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    import roblox_animations

    roblox_animations.register()
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if args:
        if len(args) != 1:
            raise SystemExit("Pass one output directory after --")
        print("Generated rebuild cases:", len(generate_playback_cases(args[0])))
    else:
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(TestRbxmRebuild)
        )
        if not result.wasSuccessful():
            raise SystemExit(1)
