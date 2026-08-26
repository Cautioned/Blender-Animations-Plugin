"""SmoothGrid terrain decode regression tests.

Ground truth comes from Studio: terrain_probe.rbxl holds 40 cells
(32 Grass at x -2..1 / y -1..0 / z -2..1, 8 Sand at x 7..8 / y 0..1 /
z -1..0) and terrain_probe_2.rbxl holds 20 occupancy-128 cells.  Both
files live in the addon repository root, next to this package's parent.
"""

from __future__ import annotations

import os
import unittest

from roblox_animations.rig.terrain import (
    decode_material_colors,
    decode_smooth_grid,
    smooth_grid_material_enums,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _load_grid(filename):
    from roblox_animations.core.rbxm import _parse_chunks

    with open(os.path.join(_REPO_ROOT, filename), "rb") as handle:
        instances, _roots = _parse_chunks(handle.read())
    terrain = next(
        instance for instance in instances.values()
        if instance.class_name == "Terrain"
    )
    grid = terrain.props.get("SmoothGrid")
    if isinstance(grid, str):
        grid = grid.encode("latin-1")
    colors = terrain.props.get("MaterialColors")
    if isinstance(colors, str):
        colors = colors.encode("latin-1")
    return bytes(grid), bytes(colors or b"")


class TestTerrainDecode(unittest.TestCase):
    def test_probe1_forty_cells(self):
        grid, colors = _load_grid("terrain_probe.rbxl")
        cells = decode_smooth_grid(grid)
        self.assertEqual(len(cells), 40)
        grass = {(x, y, z) for x, y, z, m, o in cells if m == 2}
        sand = {(x, y, z) for x, y, z, m, o in cells if m == 6}
        self.assertEqual(len(grass), 32)
        self.assertEqual(len(sand), 8)
        for x in range(-2, 2):
            for y in range(-1, 1):
                for z in range(-2, 2):
                    self.assertIn((x, y, z), grass)
        for x in (7, 8):
            for y in (0, 1):
                for z in (-1, 0):
                    self.assertIn((x, y, z), sand)
        # FillBlock cells are fully solid: no occupancy byte was stored.
        self.assertTrue(all(o == 255 for _x, _y, _z, _m, o in cells))

    def test_probe2_partial_occupancy(self):
        grid, colors = _load_grid("terrain_probe_2.rbxl")
        cells = decode_smooth_grid(grid)
        self.assertEqual(len(cells), 20)
        self.assertTrue(all(o == 128 for _x, _y, _z, _m, o in cells))
        mats = {m for _x, _y, _z, m, _o in cells}
        self.assertEqual(mats, {2, 6, 8, 10})

    def test_material_scan_does_not_expand_cells(self):
        grid, _colors = _load_grid("terrain_probe_2.rbxl")
        self.assertEqual(
            smooth_grid_material_enums(grid),
            (896, 1280, 1296, 1328),
        )

    def test_material_colors(self):
        _grid, colors = _load_grid("terrain_probe.rbxl")
        palette = decode_material_colors(colors)
        self.assertEqual(len(palette), 23)
        # Material 2 is grass green in the stock palette.
        self.assertEqual(
            palette[2], (111 / 255.0, 126 / 255.0, 62 / 255.0)
        )

    def test_empty_grid(self):
        self.assertEqual(decode_smooth_grid(b""), [])
        self.assertEqual(decode_smooth_grid(b"\x01\x05"), [])

    def test_truncated_stream_raises(self):
        with self.assertRaises(ValueError):
            decode_smooth_grid(b"\x01\x05\xff\xff")


class TestTerrainSplat(unittest.TestCase):
    """Surface-nets splat mesh: one merged surface + water split out."""

    def test_splat_weights_normalise_and_water_splits(self):
        import numpy as np

        from roblox_animations.rig.terrain import terrain_smooth_mesh_splat

        xs = []
        ys = []
        zs = []
        mats = []
        for x in range(3):
            for y in range(2):
                for z in range(3):
                    xs.append(x)
                    ys.append(y)
                    zs.append(z)
                    mats.append(2)
        for x in range(3, 5):
            for y in range(2):
                for z in range(3):
                    xs.append(x)
                    ys.append(y)
                    zs.append(z)
                    mats.append(6)
        for x in range(7, 9):
            for y in range(2):
                for z in range(3):
                    xs.append(x)
                    ys.append(y)
                    zs.append(z)
                    mats.append(1)
        xs = np.asarray(xs, dtype=np.int32)
        ys = np.asarray(ys, dtype=np.int32)
        zs = np.asarray(zs, dtype=np.int32)
        mats = np.asarray(mats, dtype=np.uint8)
        occs = np.full(xs.shape, 255, dtype=np.uint8)
        colors = [(0.5, 0.5, 0.5)] * 8
        splat, water = terrain_smooth_mesh_splat(xs, ys, zs, mats, occs, colors)
        self.assertIsNotNone(splat)
        self.assertIsNotNone(water)
        materials, verts, quads, normals, weights, tints = splat
        self.assertEqual(materials, (2, 6))
        self.assertEqual(quads.shape[1], 4)
        self.assertTrue(quads.min() >= 0)
        self.assertTrue(quads.max() < verts.shape[0])
        self.assertEqual(weights.shape, (verts.shape[0], 2))
        self.assertTrue(np.all(weights >= 0.0))
        np.testing.assert_allclose(weights.sum(axis=1), 1.0, atol=1e-5)
        self.assertEqual(len(tints), 2)
        self.assertEqual(water[0], 1)
        self.assertGreater(water[2].shape[0], 0)


if __name__ == "__main__":
    unittest.main()
