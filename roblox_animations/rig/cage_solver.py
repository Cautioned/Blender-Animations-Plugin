"""Utilities for layered-clothing cage deformation and wrap correspondence."""

from __future__ import annotations

import heapq
from typing import Dict, List, Optional, Sequence, Tuple


Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]


def _vec_add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vec_sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vec_scale(a: Vec3, scalar: float) -> Vec3:
    return (a[0] * scalar, a[1] * scalar, a[2] * scalar)


def _dist2(a: Vec3, b: Vec3) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return (dx * dx) + (dy * dy) + (dz * dz)


def _dist(a: Vec3, b: Vec3) -> float:
    return _dist2(a, b) ** 0.5


def _round_uv_key(uv: Optional[Sequence[float]], precision: int = 4) -> Optional[Vec2]:
    if uv is None:
        return None
    return (round(float(uv[0]), precision), round(float(uv[1]), precision))


def _round_pos_key(position: Sequence[float], precision: int = 6) -> Vec3:
    return tuple(round(float(component), precision) for component in position)


def _build_vertex_adjacency(faces: Optional[Sequence[Sequence[int]]], vertex_count: int) -> List[set]:
    adjacency = [set() for _ in range(max(vertex_count, 0))]
    if not faces:
        return adjacency

    for face in faces:
        if face is None or len(face) < 3:
            continue
        try:
            a, b, c = int(face[0]), int(face[1]), int(face[2])
        except Exception:
            continue
        if min(a, b, c) < 0 or max(a, b, c) >= vertex_count:
            continue
        adjacency[a].update((b, c))
        adjacency[b].update((a, c))
        adjacency[c].update((a, b))

    return adjacency


def _sequence_distance(left: Sequence[float], right: Sequence[float], missing_cost: float = 1.0) -> float:
    overlap = min(len(left), len(right))
    distance = sum(abs(float(left[index]) - float(right[index])) for index in range(overlap))
    if len(left) > overlap:
        distance += sum(abs(float(value)) for value in left[overlap:]) + (len(left) - overlap) * missing_cost
    if len(right) > overlap:
        distance += sum(abs(float(value)) for value in right[overlap:]) + (len(right) - overlap) * missing_cost
    return distance


def _uv_sequence_distance(left: Sequence[Optional[Vec2]], right: Sequence[Optional[Vec2]]) -> float:
    overlap = min(len(left), len(right))
    distance = 0.0
    for index in range(overlap):
        left_uv = left[index]
        right_uv = right[index]
        if left_uv == right_uv:
            continue
        if left_uv is None or right_uv is None:
            distance += 1.0
        else:
            distance += abs(left_uv[0] - right_uv[0]) + abs(left_uv[1] - right_uv[1])

    distance += abs(len(left) - len(right))
    return distance


def _build_vertex_topology(vertices: Sequence[dict], faces: Optional[Sequence[Sequence[int]]], precision: int = 4) -> Dict[int, dict]:
    adjacency = _build_vertex_adjacency(faces, len(vertices))
    topology: Dict[int, dict] = {}
    for vertex_index, neighbors in enumerate(adjacency):
        neighbor_indices = sorted(neighbors)
        neighbor_uvs = sorted(
            (_round_uv_key(vertices[neighbor_index].get("uv"), precision=precision) for neighbor_index in neighbor_indices),
            key=lambda value: (value is None, value),
        )
        edge_lengths = sorted(
            _dist(vertices[vertex_index]["position"], vertices[neighbor_index]["position"])
            for neighbor_index in neighbor_indices
        )
        topology[vertex_index] = {
            "valence": len(neighbor_indices),
            "neighbor_uvs": neighbor_uvs,
            "edge_lengths": edge_lengths,
        }
    return topology


def _normal_match_penalty(source_normal: Optional[Sequence[float]], target_normal: Optional[Sequence[float]]) -> float:
    if source_normal is None or target_normal is None:
        return 0.0

    dot = 0.0
    source_length = 0.0
    target_length = 0.0
    for source_component, target_component in zip(source_normal, target_normal):
        source_value = float(source_component)
        target_value = float(target_component)
        dot += source_value * target_value
        source_length += source_value * source_value
        target_length += target_value * target_value

    if source_length <= 1e-12 or target_length <= 1e-12:
        return 0.0

    normalized_dot = dot / ((source_length ** 0.5) * (target_length ** 0.5))
    normalized_dot = max(-1.0, min(1.0, normalized_dot))
    return 1.0 - normalized_dot


def _vertex_match_score(source_vertex: dict, target_vertex: dict) -> Tuple[float, float]:
    position_score = _dist2(source_vertex["position"], target_vertex["position"])
    normal_score = _normal_match_penalty(source_vertex.get("normal"), target_vertex.get("normal"))
    return (position_score, normal_score)


def _select_non_conflicting_pairs(pair_scores: Sequence[Tuple[tuple, int, int]]) -> List[Tuple[int, int]]:
    heap = [(score, source_index, target_index) for score, source_index, target_index in pair_scores]
    heapq.heapify(heap)

    used_sources = set()
    used_targets = set()
    links: List[Tuple[int, int]] = []
    while heap:
        _, source_index, target_index = heapq.heappop(heap)
        if source_index in used_sources or target_index in used_targets:
            continue
        used_sources.add(source_index)
        used_targets.add(target_index)
        links.append((source_index, target_index))

    return links


def _match_uv_bucket(
    source_indices: Sequence[int],
    target_indices: Sequence[int],
    source_vertices: Sequence[dict],
    target_vertices: Sequence[dict],
    source_topology: Optional[Dict[int, dict]] = None,
    target_topology: Optional[Dict[int, dict]] = None,
    use_position_score: bool = True,
) -> List[Tuple[int, int]]:
    pair_scores = []
    for source_index in source_indices:
        source_vertex = source_vertices[source_index]
        source_info = (source_topology or {}).get(source_index)
        for target_index in target_indices:
            target_vertex = target_vertices[target_index]
            target_info = (target_topology or {}).get(target_index)
            topology_score = 0.0
            valence_score = 0.0
            edge_score = 0.0
            if source_info and target_info:
                valence_score = abs(source_info["valence"] - target_info["valence"])
                topology_score = _uv_sequence_distance(source_info["neighbor_uvs"], target_info["neighbor_uvs"])
                edge_score = _sequence_distance(source_info["edge_lengths"], target_info["edge_lengths"], missing_cost=0.25)
            position_score, normal_score = _vertex_match_score(source_vertex, target_vertex)
            score = (topology_score, valence_score, edge_score, normal_score)
            if use_position_score:
                score = score + (position_score,)
            score = score + (source_index, target_index)
            pair_scores.append((score, source_index, target_index))

    return _select_non_conflicting_pairs(pair_scores)


def _match_position_bucket(
    source_indices: Sequence[int],
    target_indices: Sequence[int],
    source_vertices: Sequence[dict],
    target_vertices: Sequence[dict],
    source_topology: Optional[Dict[int, dict]] = None,
    target_topology: Optional[Dict[int, dict]] = None,
) -> List[Tuple[int, int]]:
    pair_scores = []
    for source_index in source_indices:
        source_vertex = source_vertices[source_index]
        source_info = (source_topology or {}).get(source_index)
        for target_index in target_indices:
            target_vertex = target_vertices[target_index]
            target_info = (target_topology or {}).get(target_index)
            valence_score = 0.0
            edge_score = 0.0
            if source_info and target_info:
                valence_score = abs(source_info["valence"] - target_info["valence"])
                edge_score = _sequence_distance(source_info["edge_lengths"], target_info["edge_lengths"], missing_cost=0.25)
            _, normal_score = _vertex_match_score(source_vertex, target_vertex)
            score = (valence_score, edge_score, normal_score, source_index, target_index)
            pair_scores.append((score, source_index, target_index))

    return _select_non_conflicting_pairs(pair_scores)


def _build_position_lookup(vertices: Sequence[dict], precisions: Sequence[int]) -> Dict[int, Dict[Vec3, List[int]]]:
    lookups: Dict[int, Dict[Vec3, List[int]]] = {}
    for precision in precisions:
        lookup: Dict[Vec3, List[int]] = {}
        for vertex_index, vertex in enumerate(vertices):
            key = _round_pos_key(vertex.get("position"), precision=precision)
            lookup.setdefault(key, []).append(vertex_index)
        lookups[precision] = lookup
    return lookups


def _find_position_candidates(
    vertex: dict,
    source_lookup: Dict[int, Dict[Vec3, List[int]]],
    precisions: Sequence[int],
) -> Optional[List[int]]:
    position = vertex.get("position")
    if position is None:
        return None

    for precision in precisions:
        candidate_indices = source_lookup.get(precision, {}).get(_round_pos_key(position, precision=precision))
        if candidate_indices:
            return candidate_indices

    return None


def build_mesh_vertices(
    positions: Optional[Sequence[Sequence[float]]],
    normals: Optional[Sequence[Optional[Sequence[float]]]] = None,
    uvs: Optional[Sequence[Optional[Sequence[float]]]] = None,
) -> List[dict]:
    vertices = []
    if not positions:
        return vertices

    normals = normals or []
    uvs = uvs or []
    normals_len = len(normals)
    uvs_len = len(uvs)
    vertices_append = vertices.append
    for index, position in enumerate(positions):
        normal = normals[index] if index < normals_len else None
        uv = uvs[index] if index < uvs_len else None
        vertices_append(
            {
                "index": index,
                "position": (float(position[0]), float(position[1]), float(position[2])),
                "normal": (float(normal[0]), float(normal[1]), float(normal[2])) if normal is not None else None,
                "uv": (float(uv[0]), float(uv[1])) if uv is not None else None,
            }
        )
    return vertices


def link_vertices_by_uv(
    source_vertices: Sequence[dict],
    target_vertices: Sequence[dict],
    precision: int = 4,
    source_faces: Optional[Sequence[Sequence[int]]] = None,
    target_faces: Optional[Sequence[Sequence[int]]] = None,
    use_position_score: bool = True,
) -> List[Tuple[int, int]]:
    source_uv_map = {}
    target_uv_map = {}

    for source_index, vertex in enumerate(source_vertices):
        key = _round_uv_key(vertex.get("uv"), precision=precision)
        if key is not None:
            source_uv_map.setdefault(key, []).append(source_index)

    for target_index, vertex in enumerate(target_vertices):
        key = _round_uv_key(vertex.get("uv"), precision=precision)
        if key is not None:
            target_uv_map.setdefault(key, []).append(target_index)

    links = []
    source_topology = None
    target_topology = None
    for key, source_indices in source_uv_map.items():
        target_indices = target_uv_map.get(key)
        if not target_indices:
            continue
        if len(source_indices) == 1 and len(target_indices) == 1:
            links.append((source_indices[0], target_indices[0]))
            continue
        if source_topology is None and source_faces:
            source_topology = _build_vertex_topology(source_vertices, source_faces, precision=precision)
        if target_topology is None and target_faces:
            target_topology = _build_vertex_topology(target_vertices, target_faces, precision=precision)
        links.extend(
            _match_uv_bucket(
                source_indices,
                target_indices,
                source_vertices,
                target_vertices,
                source_topology=source_topology,
                target_topology=target_topology,
                use_position_score=use_position_score,
            )
        )

    return links


def link_vertices_by_position(
    source_vertices: Sequence[dict],
    target_vertices: Sequence[dict],
    precision: int = 6,
    source_faces: Optional[Sequence[Sequence[int]]] = None,
    target_faces: Optional[Sequence[Sequence[int]]] = None,
) -> List[Tuple[int, int]]:
    source_position_map = {}
    target_position_map = {}

    for source_index, vertex in enumerate(source_vertices):
        key = _round_pos_key(vertex.get("position"), precision=precision)
        source_position_map.setdefault(key, []).append(source_index)

    for target_index, vertex in enumerate(target_vertices):
        key = _round_pos_key(vertex.get("position"), precision=precision)
        target_position_map.setdefault(key, []).append(target_index)

    links = []
    source_topology = None
    target_topology = None
    for key, source_indices in source_position_map.items():
        target_indices = target_position_map.get(key)
        if not target_indices:
            continue
        if len(source_indices) == 1 and len(target_indices) == 1:
            links.append((source_indices[0], target_indices[0]))
            continue
        if source_topology is None and source_faces:
            source_topology = _build_vertex_topology(source_vertices, source_faces, precision=precision)
        if target_topology is None and target_faces:
            target_topology = _build_vertex_topology(target_vertices, target_faces, precision=precision)
        links.extend(
            _match_position_bucket(
                source_indices,
                target_indices,
                source_vertices,
                target_vertices,
                source_topology=source_topology,
                target_topology=target_topology,
            )
        )

    return links


def link_targets_to_sources_by_position(
    source_vertices: Sequence[dict],
    target_vertices: Sequence[dict],
    precision: int = 6,
    source_faces: Optional[Sequence[Sequence[int]]] = None,
    target_faces: Optional[Sequence[Sequence[int]]] = None,
) -> List[Tuple[int, int]]:
    candidate_precisions = tuple(range(max(2, precision), 1, -1))
    source_lookup = _build_position_lookup(source_vertices, candidate_precisions)
    source_topology = None
    target_topology = None
    fallback_candidates = range(len(source_vertices))

    links: List[Tuple[int, int]] = []
    for target_index, target_vertex in enumerate(target_vertices):
        best_source = None
        best_score = None
        candidate_indices = _find_position_candidates(target_vertex, source_lookup, candidate_precisions)
        if not candidate_indices:
            candidate_indices = fallback_candidates
        elif len(candidate_indices) == 1:
            links.append((candidate_indices[0], target_index))
            continue

        target_info = None
        if source_faces and target_faces:
            if source_topology is None:
                source_topology = _build_vertex_topology(source_vertices, source_faces, precision=precision)
            if target_topology is None:
                target_topology = _build_vertex_topology(target_vertices, target_faces, precision=precision)
            target_info = target_topology.get(target_index)

        for source_index in candidate_indices:
            source_vertex = source_vertices[source_index]
            source_info = source_topology.get(source_index) if source_topology is not None else None
            valence_score = 0.0
            edge_score = 0.0
            if source_info and target_info:
                valence_score = abs(source_info["valence"] - target_info["valence"])
                edge_score = _sequence_distance(source_info["edge_lengths"], target_info["edge_lengths"], missing_cost=0.25)

            position_score, normal_score = _vertex_match_score(source_vertex, target_vertex)
            score = (position_score, valence_score, edge_score, normal_score, source_index)
            if best_score is None or score < best_score:
                best_score = score
                best_source = source_index

        if best_source is not None:
            links.append((best_source, target_index))

    return links


def _deduplicate_controls(
    positions: Sequence[Vec3],
    targets: Sequence[Vec3],
    precision: int = 6,
) -> Tuple[List[Vec3], List[Vec3]]:
    deduped_positions = []
    deduped_targets = []
    dedupe_counts = []
    index_by_key = {}

    for position, target in zip(positions, targets):
        key = _round_pos_key(position, precision=precision)
        control_index = index_by_key.get(key)
        if control_index is None:
            index_by_key[key] = len(deduped_positions)
            deduped_positions.append(position)
            deduped_targets.append(target)
            dedupe_counts.append(1)
            continue

        count = dedupe_counts[control_index] + 1
        previous = deduped_targets[control_index]
        merged = (
            ((previous[0] * dedupe_counts[control_index]) + target[0]) / count,
            ((previous[1] * dedupe_counts[control_index]) + target[1]) / count,
            ((previous[2] * dedupe_counts[control_index]) + target[2]) / count,
        )
        deduped_targets[control_index] = merged
        dedupe_counts[control_index] = count

    return deduped_positions, deduped_targets


def _solve_linear_system(matrix: Sequence[Sequence[float]], targets: Sequence[Vec3], regularization: float = 1e-8) -> Optional[List[Vec3]]:
    size = len(matrix)
    if size == 0:
        return []

    augmented = []
    for row_index in range(size):
        row = [float(value) for value in matrix[row_index]]
        row[row_index] += regularization
        tx, ty, tz = targets[row_index]
        augmented.append(row + [float(tx), float(ty), float(tz)])

    for pivot_index in range(size):
        pivot_row = max(range(pivot_index, size), key=lambda idx: abs(augmented[idx][pivot_index]))
        pivot_value = augmented[pivot_row][pivot_index]
        if abs(pivot_value) <= 1e-12:
            return None

        if pivot_row != pivot_index:
            augmented[pivot_index], augmented[pivot_row] = augmented[pivot_row], augmented[pivot_index]

        pivot_value = augmented[pivot_index][pivot_index]
        inverse = 1.0 / pivot_value
        for column_index in range(pivot_index, size + 3):
            augmented[pivot_index][column_index] *= inverse

        for row_index in range(size):
            if row_index == pivot_index:
                continue
            factor = augmented[row_index][pivot_index]
            if abs(factor) <= 1e-15:
                continue
            for column_index in range(pivot_index, size + 3):
                augmented[row_index][column_index] -= factor * augmented[pivot_index][column_index]

    return [
        (
            augmented[row_index][size],
            augmented[row_index][size + 1],
            augmented[row_index][size + 2],
        )
        for row_index in range(size)
    ]


def _build_rbf_matrix(control_positions: Sequence[Vec3]) -> List[List[float]]:
    size = len(control_positions)
    matrix = [[0.0] * size for _ in range(size)]
    for row_index in range(size):
        row_position = control_positions[row_index]
        for column_index in range(row_index, size):
            column_position = control_positions[column_index]
            distance = 0.0 if row_index == column_index else _dist(row_position, column_position)
            matrix[row_index][column_index] = distance
            matrix[column_index][row_index] = distance
    return matrix


def _evaluate_rbf(point: Vec3, control_positions: Sequence[Vec3], weights: Sequence[Vec3]) -> Vec3:
    result = (0.0, 0.0, 0.0)
    for control_position, weight in zip(control_positions, weights):
        basis = _dist(point, control_position)
        result = _vec_add(result, _vec_scale(weight, basis))
    return result


def _predict_points_with_rbf(
    points: Sequence[Vec3],
    control_positions: Sequence[Vec3],
    target_positions: Sequence[Vec3],
    *,
    neighbor_count: int = 24,
    global_threshold: int = 96,
    regularization: float = 1e-8,
) -> Tuple[List[Vec3], str]:
    if not points:
        return [], "empty"
    if not control_positions or not target_positions:
        return list(points), "passthrough"

    control_positions, target_positions = _deduplicate_controls(control_positions, target_positions)
    if not control_positions:
        return list(points), "passthrough"

    position_to_target = {
        _round_pos_key(position): target
        for position, target in zip(control_positions, target_positions)
    }

    if len(control_positions) <= global_threshold:
        weights = _solve_linear_system(
            _build_rbf_matrix(control_positions),
            target_positions,
            regularization=regularization,
        )
        if not weights:
            return list(points), "weighted-fallback"

        predicted = []
        for point in points:
            exact = position_to_target.get(_round_pos_key(point))
            predicted.append(exact if exact is not None else _evaluate_rbf(point, control_positions, weights))
        return predicted, "global-rbf"

    cache: Dict[Tuple[int, ...], Optional[Tuple[List[Vec3], List[Vec3]]]] = {}
    predicted = []
    local_neighbor_count = min(max(4, neighbor_count), len(control_positions))
    for point in points:
        exact = position_to_target.get(_round_pos_key(point))
        if exact is not None:
            predicted.append(exact)
            continue

        nearest = heapq.nsmallest(
            local_neighbor_count,
            enumerate(control_positions),
            key=lambda item: _dist2(point, item[1]),
        )
        neighbor_indices = tuple(index for index, _ in nearest)
        cached = cache.get(neighbor_indices)
        if cached is None and neighbor_indices not in cache:
            local_controls = [control_positions[index] for index in neighbor_indices]
            local_targets = [target_positions[index] for index in neighbor_indices]
            local_weights = _solve_linear_system(
                _build_rbf_matrix(local_controls),
                local_targets,
                regularization=regularization,
            )
            if local_weights:
                cached = (local_controls, local_weights)
            cache[neighbor_indices] = cached

        if cached:
            local_controls, local_weights = cached
            predicted.append(_evaluate_rbf(point, local_controls, local_weights))
            continue

        weighted_sum = (0.0, 0.0, 0.0)
        total_weight = 0.0
        for index, control_position in nearest:
            distance2 = _dist2(point, control_position)
            weight = 1.0 / max(distance2, 1e-12)
            weighted_sum = _vec_add(weighted_sum, _vec_scale(target_positions[index], weight))
            total_weight += weight
        if total_weight > 0.0:
            predicted.append(_vec_scale(weighted_sum, 1.0 / total_weight))
        else:
            predicted.append(point)

    return predicted, "local-rbf"


def _solve_displacements(
    source_vertices: Sequence[dict],
    target_vertices: Sequence[dict],
    precision: int = 4,
    source_faces: Optional[Sequence[Sequence[int]]] = None,
    target_faces: Optional[Sequence[Sequence[int]]] = None,
) -> Optional[dict]:
    links = link_vertices_by_uv(
        source_vertices,
        target_vertices,
        precision=precision,
        source_faces=source_faces,
        target_faces=target_faces,
    )
    if not links and len(source_vertices) == len(target_vertices):
        links = [(index, index) for index in range(len(source_vertices))]

    if not links:
        return None

    control_positions = []
    target_positions = []
    for source_index, target_index in links:
        source_position = source_vertices[source_index]["position"]
        target_position = target_vertices[target_index]["position"]
        control_positions.append(source_position)
        target_positions.append(target_position)

    control_positions, target_positions = _deduplicate_controls(control_positions, target_positions)
    if not control_positions:
        return None

    return {
        "links": links,
        "control_positions": control_positions,
        "target_positions": target_positions,
    }


def solve_two_stage_cage_deformation(
    reference_inner_vertices: Sequence[dict],
    current_inner_vertices: Sequence[dict],
    source_outer_vertices: Sequence[dict],
    source_mesh_vertices: Sequence[dict],
    *,
    precision: int = 4,
    inner_neighbors: int = 8,
    outer_neighbors: int = 8,
    reference_inner_faces: Optional[Sequence[Sequence[int]]] = None,
    current_inner_faces: Optional[Sequence[Sequence[int]]] = None,
) -> Optional[dict]:
    inner_solution = _solve_displacements(
        reference_inner_vertices,
        current_inner_vertices,
        precision=precision,
        source_faces=reference_inner_faces,
        target_faces=current_inner_faces,
    )
    if not inner_solution:
        return None

    predicted_outer_positions, inner_mode = _predict_points_with_rbf(
        [vertex["position"] for vertex in source_outer_vertices],
        inner_solution["control_positions"],
        inner_solution["target_positions"],
        neighbor_count=inner_neighbors,
    )

    outer_control_positions = [vertex["position"] for vertex in source_outer_vertices]
    outer_target_positions = list(predicted_outer_positions)
    outer_control_positions, outer_target_positions = _deduplicate_controls(outer_control_positions, outer_target_positions)
    if not outer_control_positions:
        return None

    predicted_mesh_positions, outer_mode = _predict_points_with_rbf(
        [vertex["position"] for vertex in source_mesh_vertices],
        outer_control_positions,
        outer_target_positions,
        neighbor_count=outer_neighbors,
    )

    return {
        "inner_link_count": len(inner_solution["links"]),
        "inner_control_count": len(inner_solution["control_positions"]),
        "outer_control_count": len(outer_control_positions),
        "inner_solver_mode": inner_mode,
        "outer_solver_mode": outer_mode,
        "predicted_outer_positions": predicted_outer_positions,
        "predicted_mesh_positions": predicted_mesh_positions,
    }