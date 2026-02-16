import unittest
import math
import importlib
from mathutils import Matrix

from ..core import utils, constants

importlib.reload(utils)
importlib.reload(constants)

from ..core.utils import cf_to_mat, mat_to_cf
from ..core.constants import get_transform_to_blender


def _make_cframe(x, y, z, rot_y_deg=0):
    """Build a 12-element CFrame at (x,y,z) with a Y-axis rotation."""
    a = math.radians(rot_y_deg)
    c, s = math.cos(a), math.sin(a)
    # Roblox CFrame: [x,y,z, R00,R01,R02, R10,R11,R12, R20,R21,R22]
    # Y-rotation:  R00=cos, R02=sin, R11=1, R20=-sin, R22=cos
    return [x, y, z, c, 0, s, 0, 1, 0, -s, 0, c]


def _identity_cf():
    return [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]


def _apply_relocation(joints_tree, relocation_mat):
    """Mirror of the relocation logic in import_ops._relocate_joint_transforms."""
    tf = joints_tree.get("transform")
    if tf and len(tf) >= 12:
        old_mat = cf_to_mat(tf)
        new_mat = relocation_mat @ old_mat
        new_cf = mat_to_cf(new_mat)
        for i in range(len(new_cf)):
            tf[i] = new_cf[i]
    for child in joints_tree.get("children", []):
        _apply_relocation(child, relocation_mat)


class TestWeaponRelocation(unittest.TestCase):
    """Test the weapon relocation math used during weapon import."""

    def _assert_cf_pos(self, cf, x, y, z, tol=0.001):
        self.assertAlmostEqual(cf[0], x, delta=tol, msg=f"X: {cf[0]} != {x}")
        self.assertAlmostEqual(cf[1], y, delta=tol, msg=f"Y: {cf[1]} != {y}")
        self.assertAlmostEqual(cf[2], z, delta=tol, msg=f"Z: {cf[2]} != {z}")

    def _assert_mat_pos(self, mat, x, y, z, tol=0.001):
        pos = mat.to_translation()
        self.assertAlmostEqual(pos.x, x, delta=tol, msg=f"X: {pos.x} != {x}")
        self.assertAlmostEqual(pos.y, y, delta=tol, msg=f"Y: {pos.y} != {y}")
        self.assertAlmostEqual(pos.z, z, delta=tol, msg=f"Z: {pos.z} != {z}")

    # ---- cf_to_mat / mat_to_cf roundtrip ----

    def test_identity_roundtrip(self):
        cf = _identity_cf()
        mat = cf_to_mat(cf)
        self.assertEqual(mat, Matrix.Identity(4))
        rt = mat_to_cf(mat)
        for i, (a, b) in enumerate(zip(cf, rt)):
            self.assertAlmostEqual(a, b, places=6, msg=f"elem {i}")

    def test_translation_roundtrip(self):
        cf = _make_cframe(10, 20, 30)
        mat = cf_to_mat(cf)
        self._assert_mat_pos(mat, 10, 20, 30)
        rt = mat_to_cf(mat)
        for i, (a, b) in enumerate(zip(cf, rt)):
            self.assertAlmostEqual(a, b, places=6, msg=f"elem {i}")

    def test_rotation_roundtrip(self):
        cf = _make_cframe(5, 0, 5, rot_y_deg=90)
        mat = cf_to_mat(cf)
        rt = mat_to_cf(mat)
        for i, (a, b) in enumerate(zip(cf, rt)):
            self.assertAlmostEqual(a, b, places=5, msg=f"elem {i}")

    # ---- relocation: translation only ----

    def test_translate_only(self):
        """Weapon at (10,0,0), parent at (0,5,0), no rotation.
        Equipped = parent * C0 * C1^-1. With identity C0/C1, equipped = parent.
        relocation = equipped @ weapon^-1 should shift by (-10, 5, 0)."""
        weapon_root_cf = _make_cframe(10, 0, 0)
        equipped_cf = _make_cframe(0, 5, 0)

        weapon_root_mat = cf_to_mat(weapon_root_cf)
        equipped_mat = cf_to_mat(equipped_cf)
        relocation = equipped_mat @ weapon_root_mat.inverted()

        tree = {
            "transform": list(weapon_root_cf),
            "children": [
                {"transform": _make_cframe(11, 0, 0), "children": []},
                {"transform": _make_cframe(12, 0, 0), "children": []},
            ],
        }
        _apply_relocation(tree, relocation)

        # root should now be at (0, 5, 0)
        self._assert_cf_pos(tree["transform"], 0, 5, 0)
        # children shifted by same delta
        self._assert_cf_pos(tree["children"][0]["transform"], 1, 5, 0)
        self._assert_cf_pos(tree["children"][1]["transform"], 2, 5, 0)

    # ---- relocation: rotation ----

    def test_rotate_90_degrees(self):
        """Weapon at origin facing +Z, equipped position facing +X (90° Y rot).
        All child transforms should rotate accordingly."""
        weapon_root_cf = _make_cframe(0, 0, 0, rot_y_deg=0)
        equipped_cf = _make_cframe(0, 0, 0, rot_y_deg=90)

        weapon_root_mat = cf_to_mat(weapon_root_cf)
        equipped_mat = cf_to_mat(equipped_cf)
        relocation = equipped_mat @ weapon_root_mat.inverted()

        # child at (0, 0, 5) — 5 studs in front
        tree = {
            "transform": list(weapon_root_cf),
            "children": [
                {"transform": _make_cframe(0, 0, 5), "children": []},
            ],
        }
        _apply_relocation(tree, relocation)

        # root stays at origin
        self._assert_cf_pos(tree["transform"], 0, 0, 0)
        # child that was at (0,0,5) should now be at (5,0,0) after 90° Y rotation
        self._assert_cf_pos(tree["children"][0]["transform"], 5, 0, 0, tol=0.01)

    def test_translate_and_rotate(self):
        """Weapon at (10, 0, 0) facing +Z, should end up at (0, 5, 0) facing -Z (180°)."""
        weapon_root_cf = _make_cframe(10, 0, 0, rot_y_deg=0)
        equipped_cf = _make_cframe(0, 5, 0, rot_y_deg=180)

        weapon_root_mat = cf_to_mat(weapon_root_cf)
        equipped_mat = cf_to_mat(equipped_cf)
        relocation = equipped_mat @ weapon_root_mat.inverted()

        # child 2 studs in front of weapon root (Z+2)
        tree = {
            "transform": list(weapon_root_cf),
            "children": [
                {"transform": _make_cframe(10, 0, 2, rot_y_deg=0), "children": []},
            ],
        }
        _apply_relocation(tree, relocation)

        # root at equipped position
        self._assert_cf_pos(tree["transform"], 0, 5, 0)
        # child was +2 Z from root, after 180° it should be -2 Z from new root
        self._assert_cf_pos(tree["children"][0]["transform"], 0, 5, -2, tol=0.01)

    # ---- relocation: no-op when already at target ----

    def test_no_op_when_coincident(self):
        """If weapon is already at the equipped position, relocation should be identity."""
        cf = _make_cframe(3, 7, -2, rot_y_deg=45)
        weapon_root_mat = cf_to_mat(cf)
        equipped_mat = cf_to_mat(cf)
        relocation = equipped_mat @ weapon_root_mat.inverted()

        # relocation should be identity
        for r in range(4):
            for c in range(4):
                expected = 1.0 if r == c else 0.0
                self.assertAlmostEqual(
                    relocation[r][c], expected, places=5,
                    msg=f"relocation[{r}][{c}]"
                )

    # ---- relocation preserves child relative positions ----

    def test_child_relative_position_preserved(self):
        """After relocation, the relative transform between parent and child
        should be identical to before."""
        weapon_root_cf = _make_cframe(5, 0, 5, rot_y_deg=30)
        child_cf = _make_cframe(6, 1, 7, rot_y_deg=30)
        equipped_cf = _make_cframe(-3, 10, 0, rot_y_deg=120)

        weapon_root_mat = cf_to_mat(weapon_root_cf)
        child_mat_before = cf_to_mat(child_cf)
        equipped_mat = cf_to_mat(equipped_cf)
        relocation = equipped_mat @ weapon_root_mat.inverted()

        # relative transform before
        rel_before = weapon_root_mat.inverted() @ child_mat_before

        tree = {
            "transform": list(weapon_root_cf),
            "children": [
                {"transform": list(child_cf), "children": []},
            ],
        }
        _apply_relocation(tree, relocation)

        # relative transform after
        new_root_mat = cf_to_mat(tree["transform"])
        new_child_mat = cf_to_mat(tree["children"][0]["transform"])
        rel_after = new_root_mat.inverted() @ new_child_mat

        for r in range(4):
            for c in range(4):
                self.assertAlmostEqual(
                    rel_before[r][c], rel_after[r][c], places=4,
                    msg=f"relative[{r}][{c}] changed"
                )

    # ---- deep tree ----

    def test_deep_tree(self):
        """Relocation works recursively on a 3-level tree."""
        weapon_cf = _make_cframe(0, 0, 0)
        equipped_cf = _make_cframe(10, 10, 10)
        relocation = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        tree = {
            "transform": _make_cframe(0, 0, 0),
            "children": [{
                "transform": _make_cframe(1, 0, 0),
                "children": [{
                    "transform": _make_cframe(2, 0, 0),
                    "children": [],
                }],
            }],
        }
        _apply_relocation(tree, relocation)

        self._assert_cf_pos(tree["transform"], 10, 10, 10)
        self._assert_cf_pos(tree["children"][0]["transform"], 11, 10, 10)
        self._assert_cf_pos(tree["children"][0]["children"][0]["transform"], 12, 10, 10)

    # ---- equipped position formula ----

    def test_equipped_position_formula(self):
        """Verify parent * C0 * C1^-1 gives the correct equipped CFrame.
        Motor6D equation: Part0.CFrame * C0 = Part1.CFrame * C1
        => Part1.CFrame = Part0.CFrame * C0 * C1^-1"""
        parent_cf = _make_cframe(0, 5, 0, rot_y_deg=0)
        # C0 = offset 2 studs to the right (X)
        c0_cf = _make_cframe(2, 0, 0)
        # C1 = offset 1 stud forward (Z)
        c1_cf = _make_cframe(0, 0, 1)

        parent_mat = cf_to_mat(parent_cf)
        c0_mat = cf_to_mat(c0_cf)
        c1_mat = cf_to_mat(c1_cf)

        equipped_mat = parent_mat @ c0_mat @ c1_mat.inverted()
        equipped_pos = equipped_mat.to_translation()

        # expected: parent at (0,5,0), C0 shifts X+2, C1^-1 shifts Z-1
        self.assertAlmostEqual(equipped_pos.x, 2, delta=0.001)
        self.assertAlmostEqual(equipped_pos.y, 5, delta=0.001)
        self.assertAlmostEqual(equipped_pos.z, -1, delta=0.001)

    def test_equipped_position_with_rotation(self):
        """Equipped position when parent is rotated 90° around Y."""
        parent_cf = _make_cframe(0, 5, 0, rot_y_deg=90)
        c0_cf = _make_cframe(2, 0, 0)  # 2 studs X in parent space
        c1_cf = _identity_cf()

        parent_mat = cf_to_mat(parent_cf)
        c0_mat = cf_to_mat(c0_cf)
        c1_mat = cf_to_mat(c1_cf)

        equipped_mat = parent_mat @ c0_mat @ c1_mat.inverted()
        equipped_pos = equipped_mat.to_translation()

        # parent rotated 90° Y: local X (2,0,0) becomes world (0,0,-2)
        self.assertAlmostEqual(equipped_pos.x, 0, delta=0.01)
        self.assertAlmostEqual(equipped_pos.y, 5, delta=0.01)
        self.assertAlmostEqual(equipped_pos.z, -2, delta=0.01)


# ---------------------------------------------------------------------------
# helpers for mesh-positioning tests
# ---------------------------------------------------------------------------

def _get_t2b():
    """Return the roblox-to-blender axis conversion matrix.
    Resets the cached global so we always get the real matrix from bpy_extras."""
    constants.transform_to_blender = None  # force re-init
    return get_transform_to_blender()


def _bone_blender_pos(cf):
    """Given a roblox CFrame, return where the bone HEAD would end up in blender space."""
    t2b = _get_t2b()
    return (t2b @ cf_to_mat(cf)).to_translation()


def _mesh_blender_pos_after_reloc(initial_roblox_cf, relocation_mat):
    """Simulate applying reloc_blender to a mesh whose matrix_world was set
    from a roblox CFrame.  Returns the new blender-space position.

    In the real code:
        mesh.matrix_world = initial_blender  (set during rig/mesh creation)
        mesh.matrix_world = reloc_blender @ mesh.matrix_world
    where reloc_blender = t2b @ relocation_mat @ t2b^-1
    """
    t2b = _get_t2b()
    initial_blender = t2b @ cf_to_mat(initial_roblox_cf)
    reloc_blender = t2b @ relocation_mat @ t2b.inverted()
    result = reloc_blender @ initial_blender
    return result.to_translation()


def _relocated_bone_cf(bone_cf, relocation_mat):
    """Apply relocation in roblox space (same as _apply_relocation for one node)."""
    new_mat = relocation_mat @ cf_to_mat(bone_cf)
    return mat_to_cf(new_mat)


class TestMeshRelocation(unittest.TestCase):
    """Verify that mesh matrix_world relocation (via the t2b conjugation)
    produces the same blender-space positions as the bone transforms relocated
    in roblox space then converted to blender space."""

    def _assert_vec_equal(self, a, b, tol=0.01, msg=""):
        for i, axis in enumerate("xyz"):
            self.assertAlmostEqual(
                a[i], b[i], delta=tol,
                msg=f"{msg} {axis}: {a[i]:.4f} != {b[i]:.4f}"
            )

    # ---- translation only ----

    def test_mesh_translate_only(self):
        """Mesh and bone both at weapon root, weapon shifts by pure translation.
        Mesh blender pos should match bone blender pos after relocation."""
        weapon_cf = _make_cframe(10, 0, 0)
        equipped_cf = _make_cframe(0, 5, 0)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        bone_pos = _bone_blender_pos(_relocated_bone_cf(weapon_cf, reloc))
        mesh_pos = _mesh_blender_pos_after_reloc(weapon_cf, reloc)
        self._assert_vec_equal(bone_pos, mesh_pos, msg="root")

    def test_mesh_translate_child(self):
        """Child bone + child mesh offset from root, translation-only relocation."""
        weapon_cf = _make_cframe(10, 0, 0)
        child_cf = _make_cframe(12, 0, 3)
        equipped_cf = _make_cframe(0, 5, 0)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        bone_pos = _bone_blender_pos(_relocated_bone_cf(child_cf, reloc))
        mesh_pos = _mesh_blender_pos_after_reloc(child_cf, reloc)
        self._assert_vec_equal(bone_pos, mesh_pos, msg="child")

    # ---- rotation only ----

    def test_mesh_rotate_90(self):
        """Weapon at origin, equipped position rotated 90° Y. No translation delta."""
        weapon_cf = _make_cframe(0, 0, 0, rot_y_deg=0)
        equipped_cf = _make_cframe(0, 0, 0, rot_y_deg=90)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        # test root
        bone_pos = _bone_blender_pos(_relocated_bone_cf(weapon_cf, reloc))
        mesh_pos = _mesh_blender_pos_after_reloc(weapon_cf, reloc)
        self._assert_vec_equal(bone_pos, mesh_pos, msg="root")

        # test child 5 studs forward
        child_cf = _make_cframe(0, 0, 5)
        bone_pos = _bone_blender_pos(_relocated_bone_cf(child_cf, reloc))
        mesh_pos = _mesh_blender_pos_after_reloc(child_cf, reloc)
        self._assert_vec_equal(bone_pos, mesh_pos, msg="child")

    def test_mesh_rotate_180(self):
        """180° rotation, weapon offset from origin."""
        weapon_cf = _make_cframe(5, 0, 5, rot_y_deg=0)
        equipped_cf = _make_cframe(5, 0, 5, rot_y_deg=180)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        child_cf = _make_cframe(5, 0, 8)  # 3 studs in front of root
        bone_pos = _bone_blender_pos(_relocated_bone_cf(child_cf, reloc))
        mesh_pos = _mesh_blender_pos_after_reloc(child_cf, reloc)
        self._assert_vec_equal(bone_pos, mesh_pos, msg="child 180°")

    def test_mesh_rotate_arbitrary(self):
        """Non-cardinal rotation (45°)."""
        weapon_cf = _make_cframe(0, 0, 0, rot_y_deg=0)
        equipped_cf = _make_cframe(0, 0, 0, rot_y_deg=45)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        child_cf = _make_cframe(4, 2, 0)
        bone_pos = _bone_blender_pos(_relocated_bone_cf(child_cf, reloc))
        mesh_pos = _mesh_blender_pos_after_reloc(child_cf, reloc)
        self._assert_vec_equal(bone_pos, mesh_pos, msg="child 45°")

    # ---- combined translate + rotate ----

    def test_mesh_translate_and_rotate(self):
        """Weapon at (10,0,0) facing +Z, equipped at (0,5,0) facing -Z (180°)."""
        weapon_cf = _make_cframe(10, 0, 0, rot_y_deg=0)
        equipped_cf = _make_cframe(0, 5, 0, rot_y_deg=180)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        for label, child in [
            ("root", weapon_cf),
            ("blade_tip", _make_cframe(10, 0, 5)),
            ("guard", _make_cframe(10, 1, -1, rot_y_deg=0)),
            ("pommel", _make_cframe(10, 0, -3)),
        ]:
            bone_pos = _bone_blender_pos(_relocated_bone_cf(child, reloc))
            mesh_pos = _mesh_blender_pos_after_reloc(child, reloc)
            self._assert_vec_equal(bone_pos, mesh_pos, msg=label)

    def test_mesh_translate_rotate_with_rotated_children(self):
        """Children have their OWN rotations, not just the root's."""
        weapon_cf = _make_cframe(5, 0, 0, rot_y_deg=0)
        equipped_cf = _make_cframe(-3, 10, 0, rot_y_deg=120)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        children = [
            _make_cframe(6, 1, 2, rot_y_deg=30),
            _make_cframe(7, -1, 0, rot_y_deg=-45),
            _make_cframe(5, 0, 0, rot_y_deg=90),  # coincident with root pos, different rot
        ]
        for i, child_cf in enumerate(children):
            bone_pos = _bone_blender_pos(_relocated_bone_cf(child_cf, reloc))
            mesh_pos = _mesh_blender_pos_after_reloc(child_cf, reloc)
            self._assert_vec_equal(bone_pos, mesh_pos, msg=f"child_{i}")

    # ---- no-op ----

    def test_mesh_noop_when_coincident(self):
        """When weapon is already at equipped position, mesh shouldn't move."""
        cf = _make_cframe(3, 7, -2, rot_y_deg=45)
        reloc = cf_to_mat(cf) @ cf_to_mat(cf).inverted()  # identity

        original_pos = _bone_blender_pos(cf)
        mesh_pos = _mesh_blender_pos_after_reloc(cf, reloc)
        self._assert_vec_equal(original_pos, mesh_pos, msg="no-op")

    # ---- deep tree (multiple meshes at different depths) ----

    def test_mesh_deep_tree(self):
        """3-level weapon tree: verify mesh at each level matches bone."""
        weapon_cf = _make_cframe(0, 0, 0)
        equipped_cf = _make_cframe(10, 10, 10, rot_y_deg=60)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        parts = [
            _make_cframe(0, 0, 0),       # root
            _make_cframe(1, 0, 0),       # child
            _make_cframe(2, 0, 0),       # grandchild
            _make_cframe(1, 2, 3),       # offset child
        ]
        for i, part_cf in enumerate(parts):
            bone_pos = _bone_blender_pos(_relocated_bone_cf(part_cf, reloc))
            mesh_pos = _mesh_blender_pos_after_reloc(part_cf, reloc)
            self._assert_vec_equal(bone_pos, mesh_pos, msg=f"depth_{i}")

    # ---- verify the t2b conjugation is self-consistent ----

    def test_conjugation_consistency(self):
        """The core invariant: for ANY roblox transform R and relocation L,
        (t2b @ L @ t2b^-1) @ (t2b @ R) == t2b @ (L @ R)

        i.e. relocating in blender space (left side) gives the same result
        as relocating in roblox space first then converting (right side).
        This is just the associativity of matrix multiplication + the fact
        that t2b^-1 @ t2b = I, but worth testing with actual float values."""
        t2b = _get_t2b()

        # arbitrary relocation
        weapon_cf = _make_cframe(7, -3, 12, rot_y_deg=73)
        equipped_cf = _make_cframe(-5, 8, 1, rot_y_deg=-110)
        L = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        # arbitrary mesh/bone roblox transform
        R = cf_to_mat(_make_cframe(9, 2, -4, rot_y_deg=37))

        # left side: blender-space relocation
        lhs = (t2b @ L @ t2b.inverted()) @ (t2b @ R)

        # right side: roblox-space relocation then convert
        rhs = t2b @ (L @ R)

        for r in range(4):
            for c in range(4):
                self.assertAlmostEqual(
                    lhs[r][c], rhs[r][c], places=6,
                    msg=f"conjugation [{r}][{c}]: {lhs[r][c]} != {rhs[r][c]}"
                )

    # ---- rotation matrix preservation ----

    def test_mesh_rotation_preserved(self):
        """After relocation, the mesh's rotation in blender space should match
        the bone's rotation (not just position)."""
        t2b = _get_t2b()

        weapon_cf = _make_cframe(0, 0, 0, rot_y_deg=0)
        equipped_cf = _make_cframe(5, 3, 0, rot_y_deg=90)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        child_cf = _make_cframe(0, 0, 2, rot_y_deg=30)

        # bone: relocate in roblox space, then convert to blender
        relocated_cf = _relocated_bone_cf(child_cf, reloc)
        bone_blender_mat = t2b @ cf_to_mat(relocated_cf)

        # mesh: convert to blender, then apply reloc_blender
        mesh_initial = t2b @ cf_to_mat(child_cf)
        reloc_blender = t2b @ reloc @ t2b.inverted()
        mesh_blender_mat = reloc_blender @ mesh_initial

        # compare full 4x4 matrices
        for r in range(4):
            for c in range(4):
                self.assertAlmostEqual(
                    bone_blender_mat[r][c], mesh_blender_mat[r][c], places=5,
                    msg=f"mat[{r}][{c}]: bone={bone_blender_mat[r][c]:.6f} mesh={mesh_blender_mat[r][c]:.6f}"
                )

    # ---- multiple meshes at different world positions ----

    def test_mesh_scatter(self):
        """A weapon with parts scattered around world space (not just near root).
        All should relocate correctly."""
        weapon_cf = _make_cframe(100, 0, 100, rot_y_deg=0)
        equipped_cf = _make_cframe(0, 5, 0, rot_y_deg=-90)
        reloc = cf_to_mat(equipped_cf) @ cf_to_mat(weapon_cf).inverted()

        scattered_parts = [
            _make_cframe(100, 0, 100),     # root
            _make_cframe(100, 10, 100),    # straight up
            _make_cframe(110, 0, 100),     # far right
            _make_cframe(100, 0, 110),     # far forward
            _make_cframe(95, -5, 95),      # behind and below
        ]
        for i, part_cf in enumerate(scattered_parts):
            bone_pos = _bone_blender_pos(_relocated_bone_cf(part_cf, reloc))
            mesh_pos = _mesh_blender_pos_after_reloc(part_cf, reloc)
            self._assert_vec_equal(bone_pos, mesh_pos, msg=f"scatter_{i}")
