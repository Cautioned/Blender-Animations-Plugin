"""Weld graphs must become unique, cycle-free Blender bone trees."""
import unittest
import json

from ..core.rbxm import (
    _Instance, _build_rig_tree, _derive_mesh_to_bone, _derive_mesh_to_bone_refs,
    _joint_parts, parse_rbxm,
)


def welded_clothes_binary():
    """Sparse referents, repeated Part names, reversed chains and shared owners."""
    from . import test_rbxm as binary
    refs = [1, 2, 20, 42, 101, 305, 700, 1200, 1300]
    names = ["Root", "UpperDoor", "Beak"] + ["Part"] * 6
    identity = [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]
    chunks = [binary._inst_chunk(1, "Model", [0]),
              binary._inst_chunk(2, "Part", refs),
              binary._inst_chunk(3, "Motor6D", [2000, 2001, 2002, 2003]),
              binary._inst_chunk(4, "WeldConstraint", [3000, 3001, 3002, 3003])]
    chunks += [binary._prop_chunk(1, "Name", 1, binary._enc_string("ClothesRig")),
               binary._prop_chunk(2, "Name", 1, b"".join(binary._enc_string(n) for n in names)),
               binary._prop_chunk(2, "CFrame", 0x10,
                                  binary._enc_cframes([[i, i + 1, 0] + identity[3:] for i in range(len(refs))])),
               binary._prop_chunk(2, "Size", 0x0E, binary._enc_vector3s([[1, 1, 1]] * len(refs)))]
    for cls, key, endpoints in [(3, "Part0", [1, 1, 1, 1]), (3, "Part1", [2, 20, 1200, 1300]),
                                (4, "Part0Internal", [42, 101, 305, 700]),
                                (4, "Part1Internal", [2, 20, 101, 2])]:
        chunks.append(binary._prop_chunk(cls, key, 0x13, binary._enc_referents(endpoints)))
    for key in ("C0", "C1"):
        chunks.append(binary._prop_chunk(3, key, 0x10, binary._enc_cframes([identity] * 4)))
    all_refs = [0] + refs + [2000, 2001, 2002, 2003, 3000, 3001, 3002, 3003]
    chunks.append(binary._prnt_chunk(all_refs, [-1] + [0] * (len(all_refs) - 1)))
    return binary._rbxm(chunks, 4, len(all_refs))


class TestWeldedClothesReferences(unittest.TestCase):
    def test_public_internal_and_attachment_endpoint_precedence(self):
        joint = _Instance(9000, "WeldConstraint")
        for props, expected in [
            ({"Part0Internal": 42, "Part1Internal": 700}, (42, 700)),
            ({"Part0": 0, "Part1": 20, "Part0Internal": 42, "Part1Internal": 700}, (0, 20)),
            ({"Part0": None, "Part1": None, "Part0Internal": 42, "Part1Internal": 700}, (42, 700)),
            ({"Attachment0": 101, "Attachment1": 305}, (101, 305)),
            ({}, (None, None)),
        ]:
            with self.subTest(props=props):
                joint.props = props
                self.assertEqual(_joint_parts(joint), expected)

    def test_duplicate_clothes_names_and_reversed_weld_chain(self):
        meta = json.loads(json.dumps(parse_rbxm(welded_clothes_binary())))
        expected = {"1": 1, "2": 2, "20": 20, "42": 2, "101": 20,
                    "305": 20, "700": 2, "1200": 1200, "1300": 1300}
        self.assertEqual(meta["meshToBoneRefs"], expected)
        self.assertEqual(meta["scene_rigs"][0]["meshToBoneRefs"], expected)
        self.assertNotEqual(next(p["idx"] for p in meta["partAux"] if p["inst_ref"] == 42), 42)
        nodes = []
        stack = [meta["rig"]]
        while stack:
            node = stack.pop()
            nodes.append(node)
            stack.extend(node["children"])
        self.assertEqual(len(nodes), len(expected))
        self.assertEqual(len({n["inst_ref"] for n in nodes}), len(expected))
        self.assertEqual(_derive_mesh_to_bone_refs(meta["rig"]), expected)

    def test_non_weld_hierarchy_retains_independent_targets(self):
        rig = {"jname": "Root", "inst_ref": 10, "children": [
            {"jname": "Body", "inst_ref": 90, "children": [
                {"jname": "SkinBone", "inst_ref": 120, "jointType": "Bone", "isDeformBone": True}]}]}
        self.assertEqual(_derive_mesh_to_bone_refs(rig), {"10": 10, "90": 90, "120": 120})

    def test_attachment_joints_keep_resolved_part_identity(self):
        identity = [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]
        instances = {}
        for ref, name in ((10, "Root"), (90, "Beak"), (305, "Part")):
            part = _Instance(ref, "Part")
            part.name = name
            part.props["CFrame"] = identity[:]
            instances[ref] = part
        for ref, parent in ((1000, 10), (1001, 90), (1002, 90), (1003, 305)):
            attachment = _Instance(ref, "Attachment")
            attachment.parent = parent
            attachment.props["CFrame"] = identity[:]
            instances[ref] = attachment
        for ref, kind, a, b in ((2000, "AnimationConstraint", 1000, 1001),
                                (2001, "RigidConstraint", 1002, 1003)):
            joint = _Instance(ref, kind)
            joint.props = {"Attachment0": a, "Attachment1": b}
            instances[ref] = joint
        rig = _build_rig_tree(instances, {})
        self.assertEqual(_derive_mesh_to_bone_refs(rig), {"10": 10, "90": 90, "305": 90})
        self.assertEqual(rig["children"][0]["jointType"], "AnimationConstraint")
        self.assertEqual(rig["children"][0]["children"][0]["jointType"], "RigidConstraint")


class TestRbxmJointGraph(unittest.TestCase):
    def build(self, edges):
        identity = [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]
        names = list(dict.fromkeys(name for _, a, b in edges for name in (a, b)))
        parts = {}
        instances = {}
        for ref, name in enumerate(names, 1):
            part = _Instance(ref, "Part")
            part.name = name
            part.props["CFrame"] = identity[:]
            parts[name] = part
            instances[ref] = part
        for index, (kind, a, b) in enumerate(edges, len(parts) + 1):
            joint = _Instance(index, kind)
            joint.props = {"Part0": parts[a].referent, "Part1": parts[b].referent,
                           "C0": identity[:], "C1": identity[:]}
            instances[index] = joint
        rig = _build_rig_tree(instances, {})
        nodes = {}
        stack = [rig]
        while stack:
            node = stack.pop()
            self.assertNotIn(node["jname"], nodes, "A multiply connected part must only produce one bone")
            nodes[node["jname"]] = node
            stack.extend(node["children"])
        self.assertEqual(set(nodes), set(names))
        return rig, nodes

    def test_default_ponytail_weld_cycle(self):
        rig, nodes = self.build([
            ("Motor6D", "Root", "Head"),
            ("Motor6D", "Head", "HairThing"),
            ("Weld", "Circle.010", "Circle.039"),
            ("Weld", "Circle.039", "Circle.010"),
            ("Weld", "Circle.010", "HairThing"),
        ])
        self.assertEqual(nodes["Circle.010"]["pname"], "HairThing")
        self.assertEqual(nodes["Circle.039"]["pname"], "Circle.010")
        self.assertEqual(_derive_mesh_to_bone(rig)["Circle.039"], "HairThing")

    def test_duplicate_weld_and_diamond_emit_one_bone(self):
        self.build([
            ("Motor6D", "Root", "Head"),
            ("Weld", "Head", "A"), ("WeldConstraint", "Head", "A"),
            ("Weld", "Head", "B"),
            ("Weld", "A", "C"), ("Weld", "B", "C"),
        ])

    def test_weld_cross_link_cannot_steal_motor_parent(self):
        _, nodes = self.build([
            ("Motor6D", "Root", "Coat"),
            ("WeldConstraint", "Coat", "Arm"),
            ("Motor6D", "Root", "Torso"),
            ("Motor6D", "Torso", "Arm"),
        ])
        self.assertEqual(nodes["Arm"]["pname"], "Torso")
        self.assertEqual(nodes["Arm"]["jointType"], "Motor6D")

    def test_closed_graph_without_a_root_terminates(self):
        rig, _ = self.build([("Weld", "A", "B"), ("Weld", "B", "C"), ("Weld", "C", "A")])
        self.assertEqual(rig["jname"], "A")

    def test_self_weld_does_not_create_a_bone(self):
        self.build([("Motor6D", "Root", "Arm"), ("WeldConstraint", "Arm", "Arm")])

    def test_ambiguous_clothes_link_is_reported(self):
        with self.assertLogs("roblox_animations.core.rbxm", level="WARNING") as logs:
            self.build([("Motor6D", "Root", "UpperDoor"), ("Motor6D", "Root", "Beak"),
                        ("WeldConstraint", "UpperDoor", "Part"), ("WeldConstraint", "Part", "Beak")])
        self.assertTrue(any("Ambiguous rigid link" in line for line in logs.output))
