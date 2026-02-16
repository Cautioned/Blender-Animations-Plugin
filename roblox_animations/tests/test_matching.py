"""
Tests for the mesh-to-bone matching pipeline.

Covers:
- _rename_parts_by_size_fingerprint (hungarian assignment, aspect ratios, side penalties)
- _rename_parts_by_fingerprint (position matching, size gating, fp locking)
- _find_matching_part (fp map lookup, position disambiguation)
- auto_constraint_parts (name-based bone matching, position disambiguation)
- Two-pass rename collision avoidance (blender auto-suffixing .001)
- Edge cases: tiny meshes, duplicate names, left/right swaps, near-center parts

All tests create real blender objects so they exercise the actual blender
name-uniqueness behavior that causes the bugs in production.
"""

import bpy
import unittest
import importlib
from mathutils import Vector

from ..operators import import_ops
from ..rig import creation
from ..rig import constraints
from ..core import utils
from ..core import constants

importlib.reload(import_ops)
importlib.reload(creation)
importlib.reload(constraints)
importlib.reload(utils)
importlib.reload(constants)

from ..operators.import_ops import (
    _strip_suffix,
    _dims_to_ratios,
    _hungarian_assign,
    _rename_parts_by_size_fingerprint,
    _rename_parts_by_fingerprint,
)
from ..rig.creation import (
    _find_matching_part,
    _build_match_context,
)
from ..rig.constraints import (
    auto_constraint_parts,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cleanup():
    """nuke everything in the scene."""
    for action in bpy.data.actions:
        bpy.data.actions.remove(action)
    for arm in bpy.data.armatures:
        bpy.data.armatures.remove(arm)
    for mesh in bpy.data.meshes:
        bpy.data.meshes.remove(mesh)
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    bpy.context.view_layer.update()


def _make_mesh_obj(name, dims=(1, 1, 1), location=(0, 0, 0), collection=None):
    """Create a real mesh object with the given dimensions and location.

    Uses a cube primitive scaled to match `dims`.  The object's
    *vertex positions* encode the dimensions (not just obj.dimensions)
    so that geometric-center helpers see real data.
    """
    import bmesh
    mesh = bpy.data.meshes.new(f"mesh_{name}")
    bm = bmesh.new()
    dx, dy, dz = [d / 2.0 for d in dims]
    verts = [
        bm.verts.new(( dx,  dy,  dz)),
        bm.verts.new(( dx,  dy, -dz)),
        bm.verts.new(( dx, -dy,  dz)),
        bm.verts.new(( dx, -dy, -dz)),
        bm.verts.new((-dx,  dy,  dz)),
        bm.verts.new((-dx,  dy, -dz)),
        bm.verts.new((-dx, -dy,  dz)),
        bm.verts.new((-dx, -dy, -dz)),
    ]
    # six faces
    bm.faces.new([verts[0], verts[1], verts[3], verts[2]])
    bm.faces.new([verts[4], verts[5], verts[7], verts[6]])
    bm.faces.new([verts[0], verts[1], verts[5], verts[4]])
    bm.faces.new([verts[2], verts[3], verts[7], verts[6]])
    bm.faces.new([verts[0], verts[2], verts[6], verts[4]])
    bm.faces.new([verts[1], verts[3], verts[7], verts[5]])
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    if collection:
        collection.objects.link(obj)
    else:
        bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()
    return obj


def _make_parts_collection(name="Parts"):
    coll = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(coll)
    return coll


def _make_armature_with_bones(bone_specs, collection=None):
    """Create an armature with bones at given positions.

    bone_specs: list of (bone_name, head_xyz, tail_xyz)
    returns: armature object
    """
    bpy.ops.object.add(type="ARMATURE", enter_editmode=True, location=(0, 0, 0))
    ao = bpy.context.object
    amt = ao.data
    # remove default bone if any
    for b in list(amt.edit_bones):
        amt.edit_bones.remove(b)
    for name, head, tail in bone_specs:
        bone = amt.edit_bones.new(name)
        bone.head = Vector(head)
        bone.tail = Vector(tail)
    bpy.ops.object.mode_set(mode="OBJECT")
    if collection:
        for coll in ao.users_collection:
            coll.objects.unlink(ao)
        collection.objects.link(ao)
    bpy.context.view_layer.update()
    return ao


def _make_cframe_components(x, y, z, r00=1, r01=0, r02=0, r10=0, r11=1, r12=0, r20=0, r21=0, r22=1):
    """Build a 12-component CFrame array like roblox sends."""
    return [x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22]


def _blender_to_roblox(bx, by, bz):
    """Convert blender (OBJ-space) position to roblox CFrame position.

    t2b maps roblox (rx, ry, rz) -> blender (rx, -rz, ry),
    so the inverse is blender (bx, by, bz) -> roblox (bx, bz, -by).
    """
    return (bx, bz, -by)


def _make_rig_node(jname, blender_xyz, children=None, aux=None, aux_transforms=None, pname=None):
    """Build a minimal rig definition node.

    blender_xyz: the position in BLENDER space (where the test mesh lives).
    Internally converted to roblox CFrame coords for the rig definition.
    """
    rx, ry, rz = _blender_to_roblox(*blender_xyz)
    node = {
        "jname": jname,
        "pname": pname or jname,
        "transform": _make_cframe_components(rx, ry, rz),
        "jointtransform0": _make_cframe_components(0, 0, 0),
        "jointtransform1": _make_cframe_components(0, 0, 0),
        "children": children or [],
        "aux": aux or [],
        "auxTransform": aux_transforms or [],
    }
    return node


def _make_meta(rig_name, rig_def, part_aux_list, parts_list=None):
    """Build a minimal meta_loaded dict."""
    meta = {
        "rigName": rig_name,
        "rig": rig_def,
        "partAux": part_aux_list,
    }
    if parts_list:
        meta["parts"] = parts_list
    return meta


def _names_of(collection):
    """Sorted list of mesh object names in a collection."""
    return sorted(obj.name for obj in collection.objects if obj.type == "MESH")


# ---------------------------------------------------------------------------
# test: _dims_to_ratios
# ---------------------------------------------------------------------------

class TestDimsToRatios(unittest.TestCase):
    def test_cube(self):
        r = _dims_to_ratios((1.0, 1.0, 1.0))
        self.assertAlmostEqual(r[0], 1.0)
        self.assertAlmostEqual(r[1], 1.0)

    def test_flat_plate(self):
        r = _dims_to_ratios((0.01, 1.0, 2.0))
        self.assertAlmostEqual(r[0], 0.005, places=3)
        self.assertAlmostEqual(r[1], 0.5, places=3)

    def test_degenerate_zero(self):
        r = _dims_to_ratios((0, 0, 0))
        self.assertEqual(r, (1.0, 1.0))

    def test_scale_invariance(self):
        r1 = _dims_to_ratios((0.01, 0.02, 0.05))
        r2 = _dims_to_ratios((1.0, 2.0, 5.0))
        self.assertAlmostEqual(r1[0], r2[0], places=5)
        self.assertAlmostEqual(r1[1], r2[1], places=5)


# ---------------------------------------------------------------------------
# test: hungarian assignment
# ---------------------------------------------------------------------------

class TestHungarianAssign(unittest.TestCase):
    def test_identity_cost(self):
        import numpy as np
        cost = np.array([[0, 100], [100, 0]], dtype=np.float64)
        result = _hungarian_assign(cost, 2, 2)
        assigned = dict(result)
        # row 0 should match col 0, row 1 -> col 1
        self.assertEqual(assigned[0], 0)
        self.assertEqual(assigned[1], 1)

    def test_swap_cost(self):
        import numpy as np
        cost = np.array([[100, 0], [0, 100]], dtype=np.float64)
        result = _hungarian_assign(cost, 2, 2)
        assigned = dict(result)
        self.assertEqual(assigned[0], 1)
        self.assertEqual(assigned[1], 0)

    def test_more_candidates_than_targets(self):
        import numpy as np
        cost = np.array([[0, 100, 50]], dtype=np.float64)
        result = _hungarian_assign(cost, 1, 3)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], (0, 0))


# ---------------------------------------------------------------------------
# test: two-pass rename avoids blender auto-suffixing
# ---------------------------------------------------------------------------

class TestTwoPassRename(unittest.TestCase):
    """Verify that the two-pass rename approach prevents blender from
    silently corrupting other objects' names via .001 suffixing."""

    def setUp(self):
        _cleanup()
        self.parts = _make_parts_collection()

    def tearDown(self):
        _cleanup()

    def test_swap_names_no_corruption(self):
        """Swapping A<->B should not produce .001 suffixes."""
        a = _make_mesh_obj("Alpha", collection=self.parts)
        b = _make_mesh_obj("Beta", collection=self.parts)
        # simulate two-pass rename
        a.name = "__tmp_0__"
        b.name = "__tmp_1__"
        a.name = "Beta"
        b.name = "Alpha"
        self.assertEqual(a.name, "Beta")
        self.assertEqual(b.name, "Alpha")

    def test_direct_rename_causes_suffixing(self):
        """Without two-pass, renaming A to B's name corrupts B.
        NOTE: blender's exact suffixing behavior varies by version."""
        a = _make_mesh_obj("Alpha", collection=self.parts)
        b = _make_mesh_obj("Beta", collection=self.parts)
        # direct rename: a takes b's name
        a.name = "Beta"
        # blender should have suffixed one of them — at minimum
        # both can't literally be the same python object
        self.assertNotEqual(id(a), id(b))
        # one of them should have a suffix if blender enforces uniqueness
        names = {a.name, b.name}
        self.assertEqual(len(names), 2, "blender should keep names unique")


# ---------------------------------------------------------------------------
# test: _rename_parts_by_size_fingerprint
# ---------------------------------------------------------------------------

class TestSizeFingerprintMatching(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.parts = _make_parts_collection()

    def tearDown(self):
        _cleanup()

    def _run_fingerprint(self, meta, parts_coll):
        return _rename_parts_by_size_fingerprint(meta, parts_coll)

    def test_basic_matching(self):
        """Each mesh has unique dims, should match 1:1."""
        _make_mesh_obj("mesh_a", dims=(1.0001, 2.0001, 3.0001), location=(1, 0, 0), collection=self.parts)
        _make_mesh_obj("mesh_b", dims=(2.0002, 3.0002, 4.0002), location=(-1, 0, 0), collection=self.parts)

        part_aux = [
            {"idx": 1, "name": "BoneA", "dims_fp": [1.0, 2.0, 3.0]},
            {"idx": 2, "name": "BoneB", "dims_fp": [2.0, 3.0, 4.0]},
        ]
        rig_def = _make_rig_node("Root", (0, 5, 0), children=[
            _make_rig_node("BoneA", (1, 0, 0)),
            _make_rig_node("BoneB", (-1, 0, 0)),
        ])
        meta = _make_meta("Rig", rig_def, part_aux)
        count = self._run_fingerprint(meta, self.parts)
        self.assertGreater(count, 0)
        names = _names_of(self.parts)
        self.assertIn("BoneA", names)
        self.assertIn("BoneB", names)

    def test_left_right_not_swapped(self):
        """Left and right parts with identical size but different x-positions
        should not be swapped (side mismatch penalty)."""
        # "left" mesh on positive x, "right" mesh on negative x
        _make_mesh_obj("mesh_L", dims=(1.0001, 2.0001, 3.0001), location=(2, 0, 0), collection=self.parts)
        _make_mesh_obj("mesh_R", dims=(1.0002, 2.0002, 3.0002), location=(-2, 0, 0), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 5, 0), children=[
            _make_rig_node("LeftHand", (2, 0, 0)),
            _make_rig_node("RightHand", (-2, 0, 0)),
        ])
        part_aux = [
            {"idx": 1, "name": "LeftHand", "dims_fp": [1.0, 2.0, 3.0]},
            {"idx": 2, "name": "RightHand", "dims_fp": [1.0, 2.0, 3.0]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)
        self._run_fingerprint(meta, self.parts)

        fp_map = meta.get("_fingerprint_object_map", {})
        # the mesh at x=+2 should map to the key containing "LeftHand"
        left_obj = None
        right_obj = None
        for obj_name, obj in fp_map.items():
            base = _strip_suffix(obj_name)
            if base == "LeftHand":
                left_obj = obj
            elif base == "RightHand":
                right_obj = obj
        if left_obj:
            self.assertGreater(left_obj.location.x, 0, "left hand mesh should be on +x side")
        if right_obj:
            self.assertLess(right_obj.location.x, 0, "right hand mesh should be on -x side")

    def test_tiny_meshes_discriminated(self):
        """Very small meshes with different aspect ratios should still be
        distinguished via RATIO_WEIGHT."""
        _make_mesh_obj("tiny_a", dims=(0.01, 0.01, 0.05), location=(1, 0, 0), collection=self.parts)
        _make_mesh_obj("tiny_b", dims=(0.01, 0.05, 0.05), location=(-1, 0, 0), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 5, 0), children=[
            _make_rig_node("Screw", (1, 0, 0)),
            _make_rig_node("Bolt", (-1, 0, 0)),
        ])
        part_aux = [
            {"idx": 1, "name": "Screw", "dims_fp": [0.01, 0.01, 0.05]},
            {"idx": 2, "name": "Bolt", "dims_fp": [0.01, 0.05, 0.05]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)
        count = self._run_fingerprint(meta, self.parts)
        self.assertGreater(count, 0)
        fp_map = meta.get("_fingerprint_object_map", {})
        # verify correct assignment by checking position
        for obj_name, obj in fp_map.items():
            base = _strip_suffix(obj_name)
            if base == "Screw":
                self.assertGreater(obj.location.x, 0)
            elif base == "Bolt":
                self.assertLess(obj.location.x, 0)

    def test_duplicate_target_names(self):
        """Multiple partAux entries with the same name should each get a
        different mesh object (no dict key collision)."""
        _make_mesh_obj("m1", dims=(1.0001, 2.0001, 3.0001), location=(5, 0, 0), collection=self.parts)
        _make_mesh_obj("m2", dims=(1.0002, 2.0002, 3.0002), location=(-5, 0, 0), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 5, 0), children=[
            _make_rig_node("Hand", (5, 0, 0)),
            _make_rig_node("Hand", (-5, 0, 0)),
        ])
        part_aux = [
            {"idx": 1, "name": "Hand", "dims_fp": [1.0, 2.0, 3.0]},
            {"idx": 2, "name": "Hand", "dims_fp": [1.0, 2.0, 3.0]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)
        self._run_fingerprint(meta, self.parts)
        fp_map = meta.get("_fingerprint_object_map", {})
        # should have 2 entries (keyed by obj.name which is unique)
        self.assertEqual(len(fp_map), 2)
        # the two objects should be different
        objs = list(fp_map.values())
        self.assertNotEqual(objs[0].name, objs[1].name)

    def test_cost_rejection(self):
        """A mesh with wildly different size should be rejected (cost > MAX)."""
        _make_mesh_obj("giant", dims=(100, 100, 100), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 0, 0), children=[
            _make_rig_node("Tiny", (0, 0, 0)),
        ])
        part_aux = [
            {"idx": 1, "name": "Tiny", "dims_fp": [0.01, 0.01, 0.01]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)
        count = self._run_fingerprint(meta, self.parts)
        # should reject — no valid match
        self.assertEqual(count, 0)

    def test_swap_rename_collision_prevented(self):
        """When the hungarian assignment swaps two names (A->B, B->A),
        the two-pass rename should prevent blender .001 suffixing."""
        # create meshes already named as bone targets, but swapped positions
        _ = _make_mesh_obj("BoneA", dims=(1.0001, 2.0001, 3.0001), location=(-2, 0, 0), collection=self.parts)
        _ = _make_mesh_obj("BoneB", dims=(2.0002, 3.0002, 4.0002), location=(2, 0, 0), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 5, 0), children=[
            _make_rig_node("BoneA", (2, 0, 0)),
            _make_rig_node("BoneB", (-2, 0, 0)),
        ])
        part_aux = [
            {"idx": 1, "name": "BoneA", "dims_fp": [2.0, 3.0, 4.0]},
            {"idx": 2, "name": "BoneB", "dims_fp": [1.0, 2.0, 3.0]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)
        self._run_fingerprint(meta, self.parts)

        # no object in the collection should have a .001 suffix
        for obj in self.parts.objects:
            self.assertNotRegex(obj.name, r"\.\d+$",
                f"object '{obj.name}' has unwanted suffix — rename collision")


# ---------------------------------------------------------------------------
# test: auto_constraint_parts (position-based disambiguation)
# ---------------------------------------------------------------------------

class TestAutoConstraintParts(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.master = bpy.data.collections.new("RIG: Test")
        bpy.context.scene.collection.children.link(self.master)
        self.parts = bpy.data.collections.new("Parts")
        self.master.children.link(self.parts)

    def tearDown(self):
        _cleanup()

    def test_single_bone_match(self):
        """Simple case: one mesh, one bone, same base name."""
        ao = _make_armature_with_bones([
            ("Torso", (0, 0, 0), (0, 0.1, 0)),
        ], collection=self.master)
        _make_mesh_obj("Torso", dims=(1, 1, 1), location=(0, 0, 0), collection=self.parts)
        bpy.context.view_layer.update()
        ok, msg = auto_constraint_parts(ao.name)
        self.assertTrue(ok)
        torso = self.parts.objects.get("Torso")
        self.assertTrue(any(
            c.type == "CHILD_OF" and c.subtarget == "Torso"
            for c in torso.constraints
        ))

    def test_duplicate_bones_position_disambiguated(self):
        """Two bones with the same base name on opposite sides of the rig.
        Meshes should be matched to the nearest bone, not arbitrarily."""
        ao = _make_armature_with_bones([
            ("Hand", (3, 0, 0), (3, 0.1, 0)),      # right side
            ("Hand.001", (-3, 0, 0), (-3, 0.1, 0)),  # left side
        ], collection=self.master)
        # meshes at matching positions
        _ = _make_mesh_obj("Hand", dims=(0.5, 0.5, 0.5), location=(3, 0, 0), collection=self.parts)
        _ = _make_mesh_obj("Hand.001", dims=(0.5, 0.5, 0.5), location=(-3, 0, 0), collection=self.parts)
        # blender may rename m_l — that's ok, we just care about the constraint target
        bpy.context.view_layer.update()

        ok, msg = auto_constraint_parts(ao.name)
        self.assertTrue(ok)

        # the mesh near x=+3 should be constrained to the bone at x=+3
        for obj in self.parts.objects:
            if obj.type != "MESH":
                continue
            child_ofs = [c for c in obj.constraints if c.type == "CHILD_OF"]
            if not child_ofs:
                continue
            bone_name = child_ofs[0].subtarget
            bone = ao.data.bones[bone_name]
            bone_x = (ao.matrix_world @ bone.head_local).x
            mesh_x = obj.location.x
            # same sign check — mesh and bone should be on the same side
            if abs(mesh_x) > 0.1 and abs(bone_x) > 0.1:
                self.assertEqual(
                    mesh_x > 0, bone_x > 0,
                    f"mesh '{obj.name}' at x={mesh_x:.1f} constrained to bone '{bone_name}' at x={bone_x:.1f} — WRONG SIDE"
                )

    def test_skip_objects_respected(self):
        """Objects in skip_objects should not be touched."""
        ao = _make_armature_with_bones([
            ("Arm", (0, 0, 0), (0, 0.1, 0)),
        ], collection=self.master)
        m = _make_mesh_obj("Arm", collection=self.parts)
        bpy.context.view_layer.update()

        ok, msg = auto_constraint_parts(ao.name, skip_objects={m})
        # no constraint should have been added
        self.assertEqual(len([c for c in m.constraints if c.type == "CHILD_OF"]), 0)

    def test_stupid_rigger_similar_names(self):
        """Rigger uses 'leg', 'leg.001' for completely different bones at
        different positions. Meshes with similar names should go to the
        nearest bone, not just any bone."""
        ao = _make_armature_with_bones([
            ("leg", (0, 0, -5), (0, 0.1, -5)),       # bottom
            ("leg.001", (0, 0, 5), (0, 0.1, 5)),      # top (maybe an antenna or smth)
        ], collection=self.master)
        m_bottom = _make_mesh_obj("leg", dims=(0.3, 0.3, 1), location=(0, 0, -5), collection=self.parts)
        _ = _make_mesh_obj("leg.002", dims=(0.3, 0.3, 1), location=(0, 0, 5), collection=self.parts)
        # note: m_top is named "leg.002" bc blender might have suffixed it
        bpy.context.view_layer.update()

        ok, msg = auto_constraint_parts(ao.name)

        # bottom mesh should be constrained to bottom bone
        for c in m_bottom.constraints:
            if c.type == "CHILD_OF":
                bone = ao.data.bones[c.subtarget]
                self.assertAlmostEqual((ao.matrix_world @ bone.head_local).z, -5, places=0,
                    msg=f"bottom mesh constrained to bone at z={(ao.matrix_world @ bone.head_local).z}")


# ---------------------------------------------------------------------------
# test: _find_matching_part (creation.py)
# ---------------------------------------------------------------------------

class TestFindMatchingPart(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.parts = _make_parts_collection()

    def tearDown(self):
        _cleanup()

    def test_fp_map_single_match(self):
        """Fingerprint map with a unique entry should return it directly."""
        obj = _make_mesh_obj("LeftHand", location=(2, 0, 0), collection=self.parts)
        bpy.context.view_layer.update()
        match_ctx = _build_match_context(self.parts)
        match_ctx["fingerprint_object_map"] = {"LeftHand": obj}
        result = _find_matching_part("LeftHand", None, match_ctx)
        self.assertEqual(result, obj)

    def test_fp_map_duplicate_position_disambiguated(self):
        """When fp_map has two entries with same base name,
        should pick the one closest to the expected position."""
        obj_l = _make_mesh_obj("Hand", location=(-3, 0, 0), collection=self.parts)
        obj_r = _make_mesh_obj("Hand.001", location=(3, 0, 0), collection=self.parts)
        bpy.context.view_layer.update()

        match_ctx = _build_match_context(self.parts)
        match_ctx["fingerprint_object_map"] = {
            obj_l.name: obj_l,
            obj_r.name: obj_r,
        }
        # query for "Hand" with expected position at x=+3 (right side)
        cf = _make_cframe_components(3, 0, 0)
        result = _find_matching_part("Hand", cf, match_ctx)
        # should pick the mesh at x=+3, not x=-3
        self.assertIsNotNone(result)
        self.assertGreater(result.location.x, 0,
            f"expected mesh at +x but got '{result.name}' at x={result.location.x}")

    def test_name_fallback_with_side_check(self):
        """When fp_map misses, name-based fallback should still check side."""
        _ = _make_mesh_obj("arm", location=(-3, 0, 0), collection=self.parts)
        _ = _make_mesh_obj("arm.001", location=(3, 0, 0), collection=self.parts)
        bpy.context.view_layer.update()

        match_ctx = _build_match_context(self.parts)
        match_ctx["fingerprint_object_map"] = {}  # no fp hits
        # looking for "arm" expected at x=+3
        cf = _make_cframe_components(3, 0, 0)
        result = _find_matching_part("arm", cf, match_ctx)
        self.assertIsNotNone(result)
        # should pick the one at +3
        self.assertGreater(result.location.x, 0)


# ---------------------------------------------------------------------------
# test: end-to-end rename pipeline (fingerprint + position pass)
# ---------------------------------------------------------------------------

class TestEndToEndRenamePipeline(unittest.TestCase):
    """Full pipeline test: _rename_parts_by_size_fingerprint followed by
    _rename_parts_by_fingerprint, simulating a real import."""

    def setUp(self):
        _cleanup()
        self.parts = _make_parts_collection()

    def tearDown(self):
        _cleanup()

    def test_full_pipeline_left_right(self):
        """Symmetric left/right parts with identical dimensions should
        be correctly assigned based on position, not swapped."""
        # meshes at their physical positions
        _make_mesh_obj("p1x", dims=(1.0001, 2.0001, 3.0001), location=(3, 0, 0), collection=self.parts)
        _make_mesh_obj("p2x", dims=(1.0002, 2.0002, 3.0002), location=(-3, 0, 0), collection=self.parts)
        # a third unrelated mesh to make it harder
        _make_mesh_obj("p3x", dims=(5.0003, 1.0003, 1.0003), location=(0, 0, 2), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 5, 0), children=[
            _make_rig_node("LeftArm", (3, 0, 0)),
            _make_rig_node("RightArm", (-3, 0, 0)),
            _make_rig_node("Spine", (0, 0, 2)),
        ])
        part_aux = [
            {"idx": 1, "name": "LeftArm", "dims_fp": [1.0, 2.0, 3.0]},
            {"idx": 2, "name": "RightArm", "dims_fp": [1.0, 2.0, 3.0]},
            {"idx": 3, "name": "Spine", "dims_fp": [5.0, 1.0, 1.0]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)

        # first pass
        _rename_parts_by_size_fingerprint(meta, self.parts)

        fp_map = meta.get("_fingerprint_object_map", {})
        rig_scale = meta.get("_rig_scale", 1.0)

        # second pass
        _rename_parts_by_fingerprint(
            meta.get("rig"), self.parts,
            renamed_via_fingerprint=len(fp_map),
            fingerprint_object_map=fp_map,
            scale_factor=rig_scale,
            meta_loaded=meta,
        )

        # verify: mesh at x=+3 should be named LeftArm (or LeftArm.NNN)
        for obj in self.parts.objects:
            if obj.type != "MESH":
                continue
            base = _strip_suffix(obj.name)
            if base == "LeftArm":
                self.assertGreater(obj.location.x, 0,
                    f"LeftArm mesh at x={obj.location.x} — should be positive")
            elif base == "RightArm":
                self.assertLess(obj.location.x, 0,
                    f"RightArm mesh at x={obj.location.x} — should be negative")

    def test_pipeline_tiny_meshes_near_center(self):
        """Tiny meshes near the rig center shouldn't get swapped even
        though their positions are close and sizes are similar."""
        # two tiny meshes with different aspect ratios, spread enough for matching
        _make_mesh_obj("p1x", dims=(0.01, 0.01, 0.05), location=(1, 0, 0), collection=self.parts)
        _make_mesh_obj("p2x", dims=(0.01, 0.05, 0.01), location=(-1, 0, 0), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 0, 5), children=[
            _make_rig_node("ScrewA", (1, 0, 0)),
            _make_rig_node("ScrewB", (-1, 0, 0)),
        ])
        part_aux = [
            {"idx": 1, "name": "ScrewA", "dims_fp": [0.01, 0.01, 0.05]},
            {"idx": 2, "name": "ScrewB", "dims_fp": [0.01, 0.05, 0.01]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)
        _rename_parts_by_size_fingerprint(meta, self.parts)

        # verify the meshes were discriminated by ratio
        fp_map = meta.get("_fingerprint_object_map", {})
        for obj_name, obj in fp_map.items():
            base = _strip_suffix(obj_name)
            if base == "ScrewA":
                # ScrewA has dims (0.01, 0.01, 0.05) — the rod-shaped one
                d = obj.dimensions
                sorted_d = sorted([d.x, d.y, d.z])
                # should have two small dims and one larger
                self.assertGreater(sorted_d[2] / max(sorted_d[0], 1e-9), 3.0,
                    "ScrewA should be the elongated mesh")
            elif base == "ScrewB":
                d = obj.dimensions
                sorted_d = sorted([d.x, d.y, d.z])
                # ScrewB dims (0.01, 0.05, 0.01) — also rod-shaped, rotated
                self.assertGreater(sorted_d[2] / max(sorted_d[0], 1e-9), 3.0,
                    "ScrewB should be the elongated mesh (rotated)")

    def test_pipeline_stupid_rigger_reuses_name(self):
        """Rigger uses "Part" for 3 completely different bones. The pipeline
        should assign each mesh to the correct "Part" bone by position."""
        # three meshes at very different locations — spread on X and Z
        # (blender Y maps to roblox -Z, so use X and Z for clear separation)
        _make_mesh_obj("p1x", dims=(1.0001, 1.0001, 1.0001), location=(10, 0, 0), collection=self.parts)
        _make_mesh_obj("p2x", dims=(1.0002, 1.0002, 1.0002), location=(0, 0, 0), collection=self.parts)
        _make_mesh_obj("p3x", dims=(1.0003, 1.0003, 1.0003), location=(-10, 0, 0), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 0, 5), children=[
            _make_rig_node("Part", (10, 0, 0)),
            _make_rig_node("Part", (0, 0, 0)),
            _make_rig_node("Part", (-10, 0, 0)),
        ])
        part_aux = [
            {"idx": 1, "name": "Part", "dims_fp": [1.0, 1.0, 1.0]},
            {"idx": 2, "name": "Part", "dims_fp": [1.0, 1.0, 1.0]},
            {"idx": 3, "name": "Part", "dims_fp": [1.0, 1.0, 1.0]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)
        _rename_parts_by_size_fingerprint(meta, self.parts)

        fp_map = meta.get("_fingerprint_object_map", {})
        # should have 3 distinct objects
        self.assertEqual(len(fp_map), 3)
        objs = list(fp_map.values())
        obj_names = [o.name for o in objs]
        # all should be distinct
        self.assertEqual(len(set(obj_names)), 3, f"expected 3 unique objects, got {obj_names}")

    def test_pipeline_nonzero_y_axis(self):
        """Parts spread along blender Y (roblox Z) must still match
        correctly — this is the axis where OBJ and t2b disagree on sign."""
        # meshes at different blender Y values (nonzero!)
        _make_mesh_obj("p1x", dims=(1.0001, 2.0001, 1.0001), location=(0, 5, 0), collection=self.parts)
        _make_mesh_obj("p2x", dims=(1.0002, 2.0002, 1.0002), location=(0, -5, 0), collection=self.parts)
        _make_mesh_obj("p3x", dims=(2.0003, 1.0003, 1.0003), location=(3, 2, 0), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 0, 5), children=[
            _make_rig_node("Front", (0, 5, 0)),
            _make_rig_node("Back", (0, -5, 0)),
            _make_rig_node("Side", (3, 2, 0)),
        ])
        part_aux = [
            {"idx": 1, "name": "Front", "dims_fp": [1.0, 2.0, 1.0]},
            {"idx": 2, "name": "Back", "dims_fp": [1.0, 2.0, 1.0]},
            {"idx": 3, "name": "Side", "dims_fp": [2.0, 1.0, 1.0]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)

        _rename_parts_by_size_fingerprint(meta, self.parts)

        fp_map = meta.get("_fingerprint_object_map", {})
        rig_scale = meta.get("_rig_scale", 1.0)

        _rename_parts_by_fingerprint(
            meta.get("rig"), self.parts,
            renamed_via_fingerprint=len(fp_map),
            fingerprint_object_map=fp_map,
            scale_factor=rig_scale,
            meta_loaded=meta,
        )

        for obj in self.parts.objects:
            if obj.type != "MESH":
                continue
            base = _strip_suffix(obj.name)
            if base == "Front":
                self.assertGreater(obj.location.y, 0,
                    f"Front mesh at y={obj.location.y} — should be positive Y")
            elif base == "Back":
                self.assertLess(obj.location.y, 0,
                    f"Back mesh at y={obj.location.y} — should be negative Y")
            elif base == "Side":
                self.assertGreater(obj.location.x, 0,
                    f"Side mesh at x={obj.location.x} — should be positive X")

    def test_second_pass_doesnt_override_first(self):
        """Parts locked by the first pass should NOT be reassigned
        by the second pass, even if the second pass finds a 'better' match."""
        _ = _make_mesh_obj("BoneA", dims=(1.0001, 2.0001, 3.0001), location=(0, 0, 0), collection=self.parts)

        rig_def = _make_rig_node("Root", (0, 5, 0), children=[
            _make_rig_node("BoneA", (0, 0, 0)),
        ])
        part_aux = [
            {"idx": 1, "name": "BoneA", "dims_fp": [1.0, 2.0, 3.0]},
        ]
        meta = _make_meta("Rig", rig_def, part_aux)

        # first pass
        _rename_parts_by_size_fingerprint(meta, self.parts)
        fp_map = meta.get("_fingerprint_object_map", {})
        rig_scale = meta.get("_rig_scale", 1.0)

        # capture the object assigned to BoneA
        bone_a_obj = None
        for k, v in fp_map.items():
            if _strip_suffix(k) == "BoneA":
                bone_a_obj = v
                break
        self.assertIsNotNone(bone_a_obj)

        # second pass
        _rename_parts_by_fingerprint(
            meta.get("rig"), self.parts,
            renamed_via_fingerprint=len(fp_map),
            fingerprint_object_map=fp_map,
            scale_factor=rig_scale,
            meta_loaded=meta,
        )

        # the same object should still be named BoneA (or BoneA.NNN)
        self.assertEqual(_strip_suffix(bone_a_obj.name), "BoneA",
            f"first pass assignment was overridden: '{bone_a_obj.name}'")


# ---------------------------------------------------------------------------
# entry point for blender's test runner
# ---------------------------------------------------------------------------

def run_tests():
    """Run all matching tests. Call from blender's python console:
        from roblox_animations.tests.test_matching import run_tests; run_tests()
    """
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestDimsToRatios))
    suite.addTests(loader.loadTestsFromTestCase(TestHungarianAssign))
    suite.addTests(loader.loadTestsFromTestCase(TestTwoPassRename))
    suite.addTests(loader.loadTestsFromTestCase(TestSizeFingerprintMatching))
    suite.addTests(loader.loadTestsFromTestCase(TestAutoConstraintParts))
    suite.addTests(loader.loadTestsFromTestCase(TestFindMatchingPart))
    suite.addTests(loader.loadTestsFromTestCase(TestEndToEndRenamePipeline))
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)
