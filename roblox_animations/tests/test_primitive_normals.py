"""Regression tests for primitive Part face winding.

Every triangle synthesized by ``creation._build_primitive_mesh_data`` must be
wound so its geometric normal points AWAY from the solid's centroid.  Blender
derives the render normal from the winding order, and the classic stud bevel's
normal map is applied on top of that normal — an inward-wound face inverts
both, which is what made part tops/bottoms render light-when-dark (the
"viewed at an angle" artifact).
"""

import math
import unittest

from ..rig import creation


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(v):
    return math.sqrt(_dot(v, v))


# Rough solid centres: the shapes are convex, so any interior point works for
# the outward test.  Blocks/cylinders/balls are centred on the origin; the
# wedge/corner-wedge are offset.
_CENTROIDS = {
    "block": (0.0, 0.0, 0.0),
    "cylinder": (0.0, 0.0, 0.0),
    "ball": (0.0, 0.0, 0.0),
    "wedge": (0.0, -0.2, 0.1),
    "corner_wedge": (0.0, -0.3, 0.0),
}


def _entry(shape):
    return {
        "shape": shape,
        "part_size": [4.0, 1.0, 2.0],
        "surface_types": [0] * 6,
    }


class TestPrimitiveNormals(unittest.TestCase):
    def _assert_outward(self, shape):
        data = creation._build_primitive_mesh_data(_entry(shape))
        self.assertIsNotNone(data, f"no mesh data for shape {shape}")
        positions = data["positions"]
        faces = data["faces"]
        normals = data["normals"]
        centroid = _CENTROIDS[shape]

        degenerate = 0
        for face in faces:
            i0, i1, i2 = (int(face[0]), int(face[1]), int(face[2]))
            p0, p1, p2 = positions[i0], positions[i1], positions[i2]
            n = _cross(_sub(p1, p0), _sub(p2, p0))
            if _length(n) < 1e-9:
                degenerate += 1  # OBJ-parity degenerate tris carry no area
                continue
            fc = (
                (p0[0] + p1[0] + p2[0]) / 3.0,
                (p0[1] + p1[1] + p2[1]) / 3.0,
                (p0[2] + p1[2] + p2[2]) / 3.0,
            )
            out = _dot(n, _sub(fc, centroid))
            self.assertGreater(
                out,
                0.0,
                f"{shape} face {face} wound inward: normal={n}, face centre={fc}",
            )
            # The stored per-vertex normal must agree with the winding.  Only
            # checked on the block: its faces own their vertices exclusively.
            # (The cylinder shares ring vertices between caps and sides, so a
            # cap normal legitimately lands on a side triangle's vertex — a
            # shading nuance, not a winding bug.)
            if shape == "block":
                for idx in (i0, i1, i2):
                    stored = normals[idx]
                    self.assertGreater(
                        _dot(stored, n),
                        0.0,
                        f"{shape} vertex {idx} stored normal {stored} opposes winding {n}",
                    )

        # Every shape must have far more real triangles than degenerate ones.
        self.assertLess(degenerate, len(faces) // 2, f"{shape}: too many degenerate faces")

    def test_block_normals_outward(self):
        self._assert_outward("block")

    def test_cylinder_normals_outward(self):
        self._assert_outward("cylinder")

    def test_ball_normals_outward(self):
        self._assert_outward("ball")

    def test_wedge_normals_outward(self):
        self._assert_outward("wedge")

    def test_corner_wedge_normals_outward(self):
        self._assert_outward("corner_wedge")

    def test_block_face_uv_direction_wraps_continuously(self):
        # Block UVs must match Roblox's canonical OBJ export (parts.obj):
        # +Z U=+X, +X U=-Z, -Z U=-X, -X U=+Z, V up on side faces but anchored
        # at the TOP edge; top/bottom faces use U=-X, V=-Z.  Values are
        # band-space (u 0.5/stud, v 0.125/stud).
        entry = _entry("block")
        data = creation._build_primitive_mesh_data(entry)
        loop_uvs = data["loop_uvs"]

        # Faces are emitted as quads (2 tris each) in the order
        # -Z, +Z, +Y, -Y, +X, -X; each tri contributes 3 loop uvs.
        def quad_uvs(quad_index):
            return loop_uvs[quad_index * 6:quad_index * 6 + 6]

        hx, hy, hz = 2.0, 0.5, 1.0

        # +Z face: U=+X, anchored at x=-hx; V top-anchored.
        uvs = quad_uvs(1)
        self.assertAlmostEqual(min(u for u, _ in uvs), 0.0, places=5)
        self.assertAlmostEqual(max(u for u, _ in uvs), 2 * hx * 0.5, places=5)
        self.assertAlmostEqual(min(v for _, v in uvs), 0.0, places=5)
        self.assertAlmostEqual(max(v for _, v in uvs), 2 * hy * 0.125, places=5)
        # +X face: U=-Z, anchored at z=+hz.
        uvs = quad_uvs(4)
        self.assertAlmostEqual(min(u for u, _ in uvs), 0.0, places=5)
        self.assertAlmostEqual(max(u for u, _ in uvs), 2 * hz * 0.5, places=5)
        # -Z face: U=-X, anchored at x=+hx.
        uvs = quad_uvs(0)
        self.assertAlmostEqual(min(u for u, _ in uvs), 0.0, places=5)
        self.assertAlmostEqual(max(u for u, _ in uvs), 2 * hx * 0.5, places=5)
        # -X face: U=+Z, anchored at z=-hz.
        uvs = quad_uvs(5)
        self.assertAlmostEqual(min(u for u, _ in uvs), 0.0, places=5)
        self.assertAlmostEqual(max(u for u, _ in uvs), 2 * hz * 0.5, places=5)
        # Side-face V is anchored at the TOP: y=+hy samples v=0.
        for quad_index in (0, 1, 4, 5):
            uvs = quad_uvs(quad_index)
            self.assertAlmostEqual(min(v for _, v in uvs), 0.0, places=5)
            self.assertAlmostEqual(max(v for _, v in uvs), 2 * hy * 0.125, places=5)
        # Top/bottom faces: U=-X (anchored at x=+hx), V=-Z (anchored at z=+hz).
        for quad_index in (2, 3):
            uvs = quad_uvs(quad_index)
            self.assertAlmostEqual(min(u for u, _ in uvs), 0.0, places=5)
            self.assertAlmostEqual(max(u for u, _ in uvs), 2 * hx * 0.5, places=5)
            self.assertAlmostEqual(min(v for _, v in uvs), 0.0, places=5)
            self.assertAlmostEqual(max(v for _, v in uvs), 2 * hz * 0.125, places=5)


if __name__ == "__main__":
    unittest.main()
