"""Weld graphs must become unique, cycle-free Blender bone trees."""
import unittest

from ..core.rbxm import _Instance, _build_rig_tree, _derive_mesh_to_bone


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
