"""Round-trip fixtures captured from Studio's TemplateR6 and TemplateR15.

build_export_case also produces expected world poses for replay in Studio.
"""

import bpy
import json
import pathlib
import unittest
from mathutils import Matrix
from ..core.utils import (
    cf_to_mat,
    mat_to_cf,
    to_matrix,
    get_action_fcurves,
    invalidate_armature_cache,
)
from ..core.constants import get_transform_to_blender
from ..rig.creation import create_joint_bone
from ..animation.serialization import serialize
from ..operators import import_ops


def build_export_case(rig_name, fixture_rig):
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    to_blender = get_transform_to_blender()
    identity_cf = mat_to_cf(Matrix.Identity(4))
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    parts = fixture_rig["parts"]
    joints = fixture_rig["joints"]
    data = bpy.data.armatures.new(rig_name)
    source = bpy.data.objects.new(rig_name, data)
    bpy.context.collection.objects.link(source)
    bpy.context.view_layer.objects.active = source
    source.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    b = data.edit_bones.new("HumanoidRootPart")
    b.matrix = to_blender @ cf_to_mat(parts[b.name])
    b.length = 0.3
    b["transform"] = [list(r) for r in cf_to_mat(parts[b.name])]
    b["transform1"] = [list(r) for r in Matrix.Identity(4)]
    b["nicetransform"] = [list(r) for r in Matrix.Identity(4)]
    bpy.ops.object.mode_set(mode="OBJECT")
    pending = list(joints)
    while pending:
        for j in list(pending):
            if j["part0"] in data.bones:
                create_joint_bone(
                    source, j["part0"], parts[j["part1"]], j["c0"], j["c1"], j["part1"]
                )
                pending.remove(j)
    control = source.copy()
    control.data = source.data.copy()
    control.name = rig_name + "_Controls"
    bpy.context.collection.objects.link(control)
    for b in control.data.bones:
        for k in list(b.keys()):
            del b[k]
        b.name = "CTRL_" + b.name
    for b in source.pose.bones:
        c = b.constraints.new("COPY_TRANSFORMS")
        c.target = control
        c.subtarget = "CTRL_" + b.name
    hand = "RightHand" if "RightHand" in data.bones else "Right Arm"
    mesh = bpy.data.meshes.new("HandleMesh")
    mesh.from_pydata([(0, 0, 0), (0, 1, 0), (1, 0, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new("Handle", mesh)
    bpy.context.collection.objects.link(obj)
    obj["RBXPartIdx"] = 1
    meta = {
        "rigName": "VerificationWeapon",
        "partAux": [{"idx": 1, "inst_ref": 1, "name": "Handle", "part_cf": identity_cf}],
        "weaponGrip": [
            {
                "root": "Handle",
                "bone": hand,
                "jointName": "VerificationWeapon",
                "connectionC0": identity_cf,
                "connectionC1": identity_cf,
            }
        ],
    }
    import_ops._pending_weapon_import.update(
        mode="rbxm",
        data={"schema": 1, "meta_loaded": meta, "rig_part_obj_names": [obj.name]},
    )
    invalidate_armature_cache()
    result = bpy.ops.object.rbxanims_apply_weapon_import(target_rig=control.name)
    assert result == {"FINISHED"}, result
    assert bpy.context.scene.rbx_anim_settings.rbx_anim_armature == source.name
    weapon = source.pose.bones["VerificationWeapon"]
    cw = weapon.constraints[-1]
    assert cw.target == control
    for name in ("CTRL_" + hand, cw.subtarget):
        pb = control.pose.bones[name]
        pb.rotation_mode = "XYZ"
        for frame, x in ((1, 0), (6, 0.35), (12, -0.2)):
            pb.rotation_euler.z = x
            pb.location.x = x * 0.25
            pb.keyframe_insert(data_path="rotation_euler", frame=frame)
            pb.keyframe_insert(data_path="location", frame=frame)
    for fc in get_action_fcurves(control.animation_data.action):
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = 12
    baked = serialize(source)
    expected = []
    for row in baked["kfs"]:
        bpy.context.scene.frame_set(round(row["t"] * bpy.context.scene.render.fps) + 1)
        ev = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
        world = {}
        for pb in ev.pose.bones:
            world[pb.name] = mat_to_cf(
                to_blender.inverted()
                @ pb.matrix
                @ to_matrix(pb.bone["nicetransform"]).inverted()
                @ to_matrix(pb.bone["transform1"]).inverted()
            )
        expected.append(world)
    return {"baked": baked, "expected": expected, "weaponParent": hand}


def templates():
    return json.loads(
        (
            pathlib.Path(__file__).parent / "fixtures" / "studio_templates.json"
        ).read_text()
    )


class TestStudioTemplateRoundtrip(unittest.TestCase):
    def check_template(self, name):
        fixture = templates()[name]
        case = build_export_case(name, fixture)
        joints = fixture["joints"] + [
            {
                "part0": case["weaponParent"],
                "part1": "VerificationWeapon",
                "c0": mat_to_cf(Matrix.Identity(4)),
                "c1": mat_to_cf(Matrix.Identity(4)),
            }
        ]
        for row, expected in zip(case["baked"]["kfs"], case["expected"]):
            world = {
                "HumanoidRootPart": cf_to_mat(fixture["parts"]["HumanoidRootPart"])
            }
            pending = list(joints)
            while pending:
                ready = [j for j in pending if j["part0"] in world]
                self.assertTrue(ready, "Disconnected joint graph")
                for joint in ready:
                    pose = row["kf"].get(joint["part1"])
                    delta = cf_to_mat(pose[0]) if pose else Matrix.Identity(4)
                    world[joint["part1"]] = (
                        world[joint["part0"]]
                        @ cf_to_mat(joint["c0"])
                        @ delta
                        @ cf_to_mat(joint["c1"]).inverted()
                    )
                    pending.remove(joint)
            for bone, components in expected.items():
                error = max(
                    abs(a - b) for a, b in zip(mat_to_cf(world[bone]), components)
                )
                self.assertLess(error, 0.001, (name, row["t"], bone))

    def test_r6_weapon_import_and_animation_roundtrip(self):
        self.check_template("TemplateR6")

    def test_r15_weapon_import_and_animation_roundtrip(self):
        self.check_template("TemplateR15")
