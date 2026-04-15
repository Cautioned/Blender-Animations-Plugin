import unittest

from ..rig.cage_solver import build_mesh_vertices, link_targets_to_sources_by_position, link_vertices_by_position, link_vertices_by_uv, solve_two_stage_cage_deformation


class TestCageSolver(unittest.TestCase):
    def test_links_vertices_by_uv_even_when_order_changes(self):
        source = build_mesh_vertices(
            [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],
            uvs=[(0, 0), (1, 0), (0, 1), (1, 1)],
        )
        target = build_mesh_vertices(
            [(1, 1, 2), (0, 1, 2), (1, 0, 2), (0, 0, 2)],
            uvs=[(1, 1), (0, 1), (1, 0), (0, 0)],
        )

        links = link_vertices_by_uv(source, target)
        self.assertEqual(sorted(links), [(0, 3), (1, 2), (2, 1), (3, 0)])

    def test_two_stage_solver_propagates_reference_cage_motion(self):
        reference_inner = build_mesh_vertices(
            [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],
            uvs=[(0, 0), (1, 0), (0, 1), (1, 1)],
        )
        current_inner = build_mesh_vertices(
            [(0, 0, 0), (1, 0, 1), (0, 1, 0), (1, 1, 1)],
            uvs=[(0, 0), (1, 0), (0, 1), (1, 1)],
        )
        source_outer = build_mesh_vertices(
            [(0, 0, 0.5), (1, 0, 0.5), (0, 1, 0.5), (1, 1, 0.5)],
            uvs=[(0, 0), (1, 0), (0, 1), (1, 1)],
        )
        source_mesh = build_mesh_vertices(
            [(0.25, 0.25, 1.0), (0.75, 0.25, 1.0), (0.25, 0.75, 1.0), (0.75, 0.75, 1.0)],
            uvs=[(0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)],
        )

        solved = solve_two_stage_cage_deformation(reference_inner, current_inner, source_outer, source_mesh)

        self.assertIsNotNone(solved)
        self.assertEqual(solved["inner_link_count"], 4)
        self.assertEqual(solved["inner_solver_mode"], "global-rbf")
        self.assertEqual(solved["outer_solver_mode"], "global-rbf")
        predicted = solved["predicted_mesh_positions"]
        self.assertEqual(len(predicted), 4)
        self.assertLess(predicted[0][2], predicted[1][2])
        self.assertLess(predicted[2][2], predicted[3][2])
        self.assertGreater(predicted[1][2], 0.75)
        self.assertGreater(predicted[1][2], predicted[0][2] + 0.2)

    def test_two_stage_solver_uses_local_rbf_for_large_control_sets(self):
        count = 128
        reference_inner = build_mesh_vertices(
            [(float(index), 0.0, 0.0) for index in range(count)],
            uvs=[(float(index), 0.0) for index in range(count)],
        )
        current_inner = build_mesh_vertices(
            [(float(index), 0.0, 0.25 * float(index % 3)) for index in range(count)],
            uvs=[(float(index), 0.0) for index in range(count)],
        )
        source_outer = build_mesh_vertices(
            [(float(index), 1.0, 0.0) for index in range(count)],
            uvs=[(float(index), 1.0) for index in range(count)],
        )
        source_mesh = build_mesh_vertices(
            [(float(index) + 0.5, 1.5, 0.0) for index in range(32)],
            uvs=[(float(index) + 0.5, 1.5) for index in range(32)],
        )

        solved = solve_two_stage_cage_deformation(reference_inner, current_inner, source_outer, source_mesh)

        self.assertIsNotNone(solved)
        self.assertEqual(solved["inner_solver_mode"], "local-rbf")
        self.assertEqual(solved["outer_solver_mode"], "local-rbf")
        self.assertEqual(len(solved["predicted_mesh_positions"]), 32)

    def test_links_duplicate_uv_vertices_by_geometric_proximity(self):
        source = build_mesh_vertices(
            [(0.0, 0.0, 10.0), (0.0, 1.0, 0.0)],
            normals=[(0.0, 0.0, 1.0), (0.0, 1.0, 0.0)],
            uvs=[(0.5, 0.5), (0.5, 0.5)],
        )
        target = build_mesh_vertices(
            [(0.0, 0.0, 0.0), (0.0, 1.0, 10.0)],
            normals=[(0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
            uvs=[(0.5, 0.5), (0.5, 0.5)],
        )

        links = link_vertices_by_uv(source, target)

        self.assertEqual(sorted(links), [(0, 1), (1, 0)])

    def test_links_vertices_by_position_even_when_order_changes(self):
        source = build_mesh_vertices(
            [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],
            normals=[(0, 0, 1)] * 4,
        )
        target = build_mesh_vertices(
            [(1, 1, 0), (0, 1, 0), (1, 0, 0), (0, 0, 0)],
            normals=[(0, 0, 1)] * 4,
        )

        links = link_vertices_by_position(
            source,
            target,
            source_faces=[(0, 1, 2), (1, 3, 2)],
            target_faces=[(0, 1, 2), (1, 3, 2)],
        )

        self.assertEqual(sorted(links), [(0, 3), (1, 2), (2, 1), (3, 0)])

    def test_links_targets_to_sources_by_position_allows_source_reuse(self):
        source = build_mesh_vertices(
            [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],
            normals=[(0, 0, 1)] * 4,
        )
        target = build_mesh_vertices(
            [(0, 0, 0), (1, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],
            normals=[(0, 0, 1)] * 5,
        )

        links = link_targets_to_sources_by_position(
            source,
            target,
            source_faces=[(0, 1, 2), (1, 3, 2)],
            target_faces=[(0, 1, 3), (2, 4, 3)],
        )

        self.assertEqual(len(links), 5)
        self.assertEqual(links[1][0], links[2][0])

    def test_links_targets_to_sources_by_position_uses_coarse_shortlist_when_positions_are_close(self):
        source = build_mesh_vertices(
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0)],
            normals=[(0, 0, 1)] * 4,
        )
        target = build_mesh_vertices(
            [(0.0002, -0.0002, 0.0), (0.9998, 0.0002, 0.0), (-0.0002, 1.0001, 0.0), (1.0002, 0.9999, 0.0)],
            normals=[(0, 0, 1)] * 4,
        )

        links = link_targets_to_sources_by_position(
            source,
            target,
            precision=6,
            source_faces=[(0, 1, 2), (1, 3, 2)],
            target_faces=[(0, 1, 2), (1, 3, 2)],
        )

        self.assertEqual(sorted(links), [(0, 0), (1, 1), (2, 2), (3, 3)])


if __name__ == "__main__":
    unittest.main()