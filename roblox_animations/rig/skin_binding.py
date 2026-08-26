"""
Skin binding: weight application and the vertex/UV matching machinery.

Turns prepared bindings (direct, uv-map, vertex-map, position, index, rigid)
into Blender vertex groups and armature modifiers.  Also hosts the shared
mesh-analysis helpers (component ids, closest-point-on-triangle, UV buckets,
sample scoring) used by the binding builders in creation.py.
"""

import re
import bpy
from mathutils import Vector

from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat, get_unique_name
from .filemesh_geometry import (
    _build_transformed_filemesh_vertices,
    _copy_position,
    _get_effective_mesh_size,
    _normalize_vector,
    _normalize_wrap_auto_skin,
)
from .mesh_surface import _ensure_synthesized_display_modifier, _populate_mesh_geometry
from .part_matching import (
    _get_mesh_world_center,
    _get_wrap_layer_metadata,
    _get_wrap_target_metadata,
)


def _short_content_id(value):
    if not value:
        return "none"
    text = str(value)
    match = re.search(r"id=(\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"(\d+)$", text)
    if match:
        return match.group(1)
    return text


def _format_binding_context(binding):
    entry = binding.get("entry") or {}
    mesh_data = binding.get("mesh_data") or {}
    wrap_layer_metadata = _get_wrap_layer_metadata(entry) or {}
    wrap_target_metadata = binding.get("wrap_target") or _get_wrap_target_metadata(entry) or {}
    parts = [
        f"mesh_id={_short_content_id(entry.get('mesh_id'))}",
        f"has_skinning={bool(entry.get('has_skinning'))}",
        f"bone_names={len(mesh_data.get('bone_names') or [])}",
        f"weights={len(mesh_data.get('vertex_weights') or [])}",
    ]

    if wrap_layer_metadata:
        parts.extend(
            [
                f"wrap_ref={_short_content_id(wrap_layer_metadata.get('reference_mesh_id'))}",
                f"wrap_cage={_short_content_id(wrap_layer_metadata.get('cage_mesh_id'))}",
                f"auto_skin={_normalize_wrap_auto_skin(wrap_layer_metadata.get('auto_skin')) or 'none'}",
            ]
        )

    if wrap_target_metadata:
        parts.append(f"target_cage={_short_content_id(wrap_target_metadata.get('cage_mesh_id'))}")

    mode = binding.get("mode")
    if mode:
        parts.append(f"mode={mode}")

    return ", ".join(parts)


def _log_binding_inspect(mesh_obj, binding, has_weights, bone_overlap):
    print(
        f"[RigCreate] Skin bind inspect for '{mesh_obj.name}': "
        f"{_format_binding_context(binding)}, has_weights={has_weights}, bone_overlap={bone_overlap}"
    )


def _log_binding_mode(mesh_obj, binding, label):
    print(f"[RigCreate] Skin bind mode for '{mesh_obj.name}': {label}; {_format_binding_context(binding)}")


def _log_binding_apply(mesh_obj, binding, stage):
    context = _format_binding_context(binding)
    if binding.get("predicted_mesh_positions"):
        fit_avg = binding.get("fit_avg_distance")
        fit_max = binding.get("fit_max_distance")
        delta_avg = binding.get("cage_delta_avg")
        delta_max = binding.get("cage_delta_max")
        if fit_avg is not None and fit_max is not None:
            context += f", cage fit avg={fit_avg:.4f} max={fit_max:.4f} (delta={delta_avg:.4f}/{delta_max:.4f})"
    print(f"[RigCreate] Applying {stage} for '{mesh_obj.name}': {context}")


def _clear_child_of_constraints(obj):
    for constraint in [c for c in obj.constraints if c.type == "CHILD_OF"]:
        obj.constraints.remove(constraint)


def _ensure_armature_modifier(obj, armature_obj):
    modifier = None
    for existing in obj.modifiers:
        if existing.type == "ARMATURE":
            modifier = existing
            break
    if modifier is None:
        modifier = obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = armature_obj
    # Deformation must come from the authored vertex groups alone — Roblox
    # has no envelope concept, and envelope influence double-weights nearby
    # geometry (e.g. a child bone dragging its parent's mesh). Newer Blender
    # versions removed the property (envelopes already off/gone), hence the
    # attribute check.
    if hasattr(modifier, "use_envelope"):
        modifier.use_envelope = False
    if hasattr(modifier, "use_vertex_groups"):
        modifier.use_vertex_groups = True
    # The Armature modifier deforms in the MESH's local space: moving the
    # armature OBJECT (the usual way to relocate a rig) does not carry the
    # vertices, only bone poses do. Parent the mesh to the armature with a
    # plain object parent so object-mode moves drag the mesh along. Bone
    # motion still comes from the modifier, so nothing double-transforms.
    # Preserve the current world matrix through the parent inverse.
    if obj.parent != armature_obj:
        obj.parent_type = "OBJECT"
        obj.parent = armature_obj
        obj.matrix_parent_inverse = (
            armature_obj.matrix_world.inverted_safe() @ obj.matrix_world
        )
    _ensure_synthesized_display_modifier(obj)
    return modifier


def _remove_all_vertex_groups(obj):
    while obj.vertex_groups:
        obj.vertex_groups.remove(obj.vertex_groups[0])


def _remove_object_and_data(obj):
    if obj is None:
        return

    mesh = obj.data if getattr(obj, "type", None) == "MESH" else None
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _round_vector_key(vector, precision=5):
    if vector is None:
        return None
    return tuple(round(float(component), precision) for component in vector)


def _round_uv_key(uv, precision=5):
    if uv is None:
        return None
    return tuple(round(float(component), precision) for component in uv)


def _has_meaningful_vertex_weights(vertex_weights):
    for weights in vertex_weights or []:
        if not weights:
            continue
        for value in weights.values():
            if float(value) > 0.0:
                return True
    return False


def _compute_mesh_vertex_uvs(mesh_obj):
    """Return {vertex_index: (u, v)} using the first-encountered loop UV per vertex.
    Used by existing callers that expect a single UV per vertex."""
    mesh = mesh_obj.data
    if mesh is None or not mesh.uv_layers:
        return {}

    uv_layer = mesh.uv_layers.active or mesh.uv_layers[0]
    result = {}
    for loop in mesh.loops:
        vertex_index = loop.vertex_index
        if vertex_index in result:
            continue
        uv = uv_layer.data[loop.index].uv
        result[vertex_index] = (float(uv.x), float(uv.y))

    return result


def _compute_mesh_vertex_all_uvs(mesh_obj):
    """Return {vertex_index: list[(u, v)]} collecting ALL distinct loop UVs per vertex.
    Seam vertices have multiple loops with different UV coordinates; using only the
    first-encountered loop causes UV-match misses for those vertices."""
    mesh = mesh_obj.data
    if mesh is None or not mesh.uv_layers:
        return {}

    uv_layer = mesh.uv_layers.active or mesh.uv_layers[0]
    result = {}
    for loop in mesh.loops:
        vertex_index = loop.vertex_index
        uv = uv_layer.data[loop.index].uv
        uv_tuple = (float(uv.x), float(uv.y))
        uvs = result.get(vertex_index)
        if uvs is None:
            result[vertex_index] = [uv_tuple]
        elif uv_tuple not in uvs:
            uvs.append(uv_tuple)

    return result


def _build_mesh_object_vertices(mesh_obj, world_space=False):
    if mesh_obj.type != "MESH" or mesh_obj.data is None:
        return []

    vertex_uvs = _compute_mesh_vertex_uvs(mesh_obj)
    vertices = []
    for vertex in mesh_obj.data.vertices:
        if world_space:
            position = mesh_obj.matrix_world @ vertex.co
            normal = _compute_mesh_vertex_normal(mesh_obj, vertex)
            if normal is not None:
                normal = (float(normal.x), float(normal.y), float(normal.z))
            position = (float(position.x), float(position.y), float(position.z))
        else:
            position = (float(vertex.co.x), float(vertex.co.y), float(vertex.co.z))
            normal = (float(vertex.normal.x), float(vertex.normal.y), float(vertex.normal.z))
        vertices.append(
            {
                "index": vertex.index,
                "position": position,
                "normal": normal,
                "uv": vertex_uvs.get(vertex.index),
            }
        )
    return vertices


def _build_mesh_object_faces(mesh_obj):
    if mesh_obj.type != "MESH" or mesh_obj.data is None:
        return []

    mesh = mesh_obj.data
    try:
        mesh.calc_loop_triangles()
        return [tuple(int(index) for index in triangle.vertices) for triangle in mesh.loop_triangles]
    except Exception:
        faces = []
        for polygon in mesh.polygons:
            vertices = [int(index) for index in polygon.vertices]
            if len(vertices) < 3:
                continue
            anchor = vertices[0]
            for index in range(1, len(vertices) - 1):
                faces.append((anchor, vertices[index], vertices[index + 1]))
        return faces


def _canonical_triangle_face(face):
    if face is None or len(face) < 3:
        return None
    try:
        indices = tuple(int(index) for index in face[:3])
    except Exception:
        return None
    if min(indices) < 0:
        return None
    return tuple(sorted(indices))


def _sorted_triangle_topology(faces):
    topology = []
    for face in faces or []:
        canonical = _canonical_triangle_face(face)
        if canonical is not None:
            topology.append(canonical)
    topology.sort()
    return topology


def _index_alignment_limits(mesh_obj):
    max_dimension = max(max(mesh_obj.dimensions), 1.0)
    return max_dimension * 0.0025, max_dimension * 0.001


def _index_alignment_is_tight(mesh_obj, alignment):
    if not alignment:
        return False
    max_distance_limit, avg_distance_limit = _index_alignment_limits(mesh_obj)
    return alignment["max"] <= max_distance_limit and alignment["avg"] <= avg_distance_limit


def _compute_mesh_vertex_normal(mesh_obj, vertex):
    try:
        normal_matrix = mesh_obj.matrix_world.to_3x3().inverted_safe().transposed()
    except Exception:
        normal_matrix = mesh_obj.matrix_world.to_3x3()
    return _normalize_vector(normal_matrix @ vertex.normal)


def _sample_match_score(vertex_position, vertex_normal, vertex_uv, sample):
    position_distance = (vertex_position - sample["position"]).length

    normal_penalty = 1.0
    sample_normal = sample.get("normal")
    if vertex_normal is not None and sample_normal is not None:
        dot = max(-1.0, min(1.0, vertex_normal.dot(sample_normal)))
        normal_penalty = 1.0 - dot

    uv_penalty = 1.0
    sample_uv = sample.get("uv")
    if vertex_uv is not None and sample_uv is not None:
        uv_penalty = abs(vertex_uv[0] - sample_uv[0]) + abs(vertex_uv[1] - sample_uv[1])

    return (round(position_distance, 8), round(normal_penalty, 8), round(uv_penalty, 8), sample.get("index", -1))


def _pick_best_sample(candidate_indices, used_indices, vertex_position, vertex_normal, vertex_uv, samples):
    best_unused = None
    best_used = None
    for sample_index in candidate_indices:
        sample = samples[sample_index]
        score = _sample_match_score(vertex_position, vertex_normal, vertex_uv, sample)
        if sample_index in used_indices:
            if best_used is None or score < best_used[0]:
                best_used = (score, sample_index)
        else:
            if best_unused is None or score < best_unused[0]:
                best_unused = (score, sample_index)
    if best_unused is not None:
        return best_unused[1]
    if best_used is not None:
        return best_used[1]
    return None


def _pick_closest_sample(samples, used_indices, vertex_position, vertex_normal, vertex_uv, max_distance=None):
    best_unused = None
    best_used = None
    best_distance_unused = None
    best_distance_used = None

    for sample_index, sample in enumerate(samples):
        sample_distance = (vertex_position - sample["position"]).length
        if max_distance is not None and sample_distance > max_distance:
            continue

        score = _sample_match_score(vertex_position, vertex_normal, vertex_uv, sample)
        candidate = (score, sample_index, sample_distance)
        if sample_index in used_indices:
            if best_used is None or candidate[0] < best_used[0]:
                best_used = candidate
                best_distance_used = sample_distance
        else:
            if best_unused is None or candidate[0] < best_unused[0]:
                best_unused = candidate
                best_distance_unused = sample_distance

    if best_unused is not None:
        return best_unused[1], best_distance_unused
    if best_used is not None:
        return best_used[1], best_distance_used
    return None, None


def _build_position_sample_lookup(samples):
    sample_lookup = {}
    sample_signature_lookup = {}
    coarse_lookups = {precision: {} for precision in (4, 3, 2, 1)}

    for sample_index, sample in enumerate(samples):
        position_key = sample.get("position_key")
        sample_lookup.setdefault(position_key, []).append(sample_index)

        signature_key = (position_key, sample.get("normal_key"), sample.get("uv_key"))
        sample_signature_lookup.setdefault(signature_key, []).append(sample_index)

        position = sample.get("position")
        if position is None:
            continue

        for precision, lookup in coarse_lookups.items():
            coarse_key = _round_vector_key(position, precision=precision)
            lookup.setdefault(coarse_key, []).append(sample_index)

    return sample_lookup, sample_signature_lookup, coarse_lookups


def _find_position_candidate_indices(world_position, normal_key, uv_key,
                                     sample_lookup, sample_signature_lookup, coarse_lookups):
    position_key = _round_vector_key(world_position)

    candidate_indices = sample_signature_lookup.get((position_key, normal_key, uv_key))
    if candidate_indices:
        return candidate_indices, "exact-signature"

    candidate_indices = sample_lookup.get(position_key)
    if candidate_indices:
        return candidate_indices, "exact-position"

    for precision in (4, 3, 2, 1):
        coarse_key = _round_vector_key(world_position, precision=precision)
        candidate_indices = coarse_lookups[precision].get(coarse_key)
        if candidate_indices:
            return candidate_indices, f"coarse-p{precision}"

    return None, None


def _estimate_index_alignment(mesh_obj, filemesh_world_positions):
    if not filemesh_world_positions:
        return None

    vertex_count = len(mesh_obj.data.vertices)
    if vertex_count != len(filemesh_world_positions):
        return None

    if vertex_count <= 0:
        return None

    if vertex_count <= 5000:
        sample_indices = range(vertex_count)
    else:
        sample_count = min(vertex_count, 512)
        step = max((vertex_count - 1) / max(sample_count - 1, 1), 1.0)
        sample_indices = {min(int(round(index * step)), vertex_count - 1) for index in range(sample_count)}

    max_distance = 0.0
    total_distance = 0.0
    compared = 0
    for vertex_index in sample_indices:
        mesh_world_position = mesh_obj.matrix_world @ mesh_obj.data.vertices[vertex_index].co
        # Transformed FileMesh positions are stored as plain tuples, so
        # coerce before Vector arithmetic (Vector - tuple raises).
        target_position = filemesh_world_positions[vertex_index]
        if not isinstance(target_position, Vector):
            target_position = Vector(target_position)
        distance = (mesh_world_position - target_position).length
        total_distance += distance
        max_distance = max(max_distance, distance)
        compared += 1

    if compared <= 0:
        return None

    return {
        "avg": total_distance / compared,
        "max": max_distance,
        "count": compared,
    }


def _mesh_face_count(mesh_obj):
    return len(_build_mesh_object_faces(mesh_obj)) if mesh_obj is not None else 0


def _limit_weight_dict(weights, max_influences=4):
    filtered = [(bone_name, float(weight)) for bone_name, weight in weights.items() if weight > 0]
    if not filtered:
        return {}

    filtered.sort(key=lambda item: item[1], reverse=True)
    limited = filtered[:max_influences]
    total = sum(weight for _, weight in limited)
    if total <= 0:
        return {}

    return {bone_name: weight / total for bone_name, weight in limited}


def _collect_vertex_group_weights(mesh_obj, available_bones):
    group_names = {
        group.index: group.name
        for group in mesh_obj.vertex_groups
        if group.name in available_bones
    }
    if not group_names:
        return []

    weights_per_vertex = []
    for vertex in mesh_obj.data.vertices:
        vertex_weights = {}
        total = 0.0
        for group_ref in vertex.groups:
            bone_name = group_names.get(group_ref.group)
            if not bone_name or group_ref.weight <= 0:
                continue
            vertex_weights[bone_name] = vertex_weights.get(bone_name, 0.0) + float(group_ref.weight)
            total += float(group_ref.weight)

        if total > 0:
            weights_per_vertex.append({
                bone_name: weight / total for bone_name, weight in vertex_weights.items()
            })
        else:
            weights_per_vertex.append({})

    return weights_per_vertex


def _measure_transfer_coverage(mesh_obj, available_bones):
    assigned_weights = _collect_vertex_group_weights(mesh_obj, available_bones)
    assigned_vertices = sum(1 for weights in assigned_weights if weights)
    total_vertices = len(mesh_obj.data.vertices)
    coverage = assigned_vertices / max(total_vertices, 1)
    return assigned_vertices, total_vertices, coverage


def _ensure_vertex_groups(mesh_obj, bone_names):
    groups = {}
    existing = {group.name: group for group in mesh_obj.vertex_groups}
    for bone_name in bone_names or []:
        if not bone_name:
            continue
        group = existing.get(bone_name)
        if group is None:
            group = mesh_obj.vertex_groups.new(name=bone_name)
            existing[bone_name] = group
        groups[bone_name] = group
    return groups


def _run_weight_transfer_sequence(mesh_obj, source_obj, available_bones,
                                  initial_max_distance, label, preferred_mapping=None):
    if preferred_mapping is None:
        preferred_mapping = "POLYINTERP_NEAREST" if source_obj.data.polygons else "NEAREST"

    mapping, max_distance = _apply_weight_data_transfer(
        mesh_obj,
        source_obj,
        max_distance=initial_max_distance,
        mapping=preferred_mapping,
    )
    assigned_vertices, total_vertices, coverage = _measure_transfer_coverage(mesh_obj, available_bones)

    if coverage < 0.98 and max_distance is not None:
        mapping, max_distance = _apply_weight_data_transfer(
            mesh_obj,
            source_obj,
            max_distance=None,
            mapping=preferred_mapping,
        )
        assigned_vertices, total_vertices, coverage = _measure_transfer_coverage(mesh_obj, available_bones)
        print(
            f"[RigCreate] {label} retried without distance limit for '{mesh_obj.name}' "
            f"(assigned={assigned_vertices}/{total_vertices}, mapping={mapping})"
        )

    if preferred_mapping != "NEAREST" and assigned_vertices <= 0:
        mapping, max_distance = _apply_weight_data_transfer(
            mesh_obj,
            source_obj,
            max_distance=None,
            mapping="NEAREST",
        )
        assigned_vertices, total_vertices, coverage = _measure_transfer_coverage(mesh_obj, available_bones)
        print(
            f"[RigCreate] {label} retried with nearest-vertex mapping for '{mesh_obj.name}' "
            f"(assigned={assigned_vertices}/{total_vertices})"
        )

    return assigned_vertices, total_vertices, coverage, mapping, max_distance


def _determine_binding_fallback_bone(binding, available_bones):
    part_to_bone = binding.get("part_to_bone_map") or {}
    entry = binding.get("entry") or {}

    entry_name = entry.get("name")
    resolved_entry_name = part_to_bone.get(entry_name, entry_name)
    if resolved_entry_name in available_bones:
        return resolved_entry_name

    resolved_weight_totals = {}
    for weights in binding.get("mesh_data", {}).get("vertex_weights") or []:
        for bone_name, weight in (weights or {}).items():
            resolved = part_to_bone.get(bone_name, bone_name)
            if resolved in available_bones and weight > 0:
                resolved_weight_totals[resolved] = resolved_weight_totals.get(resolved, 0.0) + float(weight)

    if resolved_weight_totals:
        return max(resolved_weight_totals.items(), key=lambda item: item[1])[0]

    return None


def _resolve_binding_bone_name(bone_name, part_to_bone, available_bones, fallback_bone=None):
    resolved = part_to_bone.get(bone_name, bone_name)
    if resolved in available_bones:
        return resolved
    if fallback_bone in available_bones:
        return fallback_bone
    return None


def _get_position_transfer_vertices(binding):
    predicted_mesh_positions = binding.get("predicted_mesh_positions") or []
    if predicted_mesh_positions:
        return [_copy_position(position) for position in predicted_mesh_positions]

    entry = binding["entry"]
    from . import avatar_scale  # noqa: PLC0415
    limb_scale = avatar_scale.entry_limb_scale(entry)
    vertices = _build_transformed_filemesh_vertices(
        binding["mesh_data"],
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=_get_effective_mesh_size(entry, binding["mesh_data"]),
        limb_scale=limb_scale,
    )
    return [_copy_position(vertex["position"]) for vertex in vertices]


def _rebase_mesh_to_predicted_positions(mesh_obj, binding):
    """Rebase a wrap layer's bind geometry to the cage-deformer's predicted
    positions — this is the actual deformation Roblox's WrapLayer applies
    before skinning. Weights (authored) are untouched; only positions move."""
    predicted_positions = binding.get("predicted_mesh_positions") or []
    if not predicted_positions or mesh_obj.type != "MESH" or mesh_obj.data is None:
        return False

    mesh = mesh_obj.data
    vertex_count = len(mesh.vertices)
    if vertex_count <= 0:
        return False

    local_matrix = mesh_obj.matrix_world.inverted_safe()
    target_positions = {}
    vertex_links = binding.get("vertex_links") or []
    if vertex_links:
        for source_index, target_index in vertex_links:
            if 0 <= source_index < len(predicted_positions) and 0 <= target_index < vertex_count:
                target_positions[target_index] = local_matrix @ predicted_positions[source_index]
    elif len(predicted_positions) == vertex_count and (
        binding.get("mode") == "index" or _index_alignment_is_tight(mesh_obj, binding.get("index_alignment"))
    ):
        for vertex_index in range(vertex_count):
            position = predicted_positions[vertex_index]
            if not isinstance(position, Vector):
                position = Vector(position)
            target_positions[vertex_index] = local_matrix @ position
    if not target_positions:
        return False

    for vertex_index, position in target_positions.items():
        mesh.vertices[vertex_index].co = position
    mesh.update()
    print(
        f"[RigCreate] Cage-fit rebased {len(target_positions)}/{vertex_count} vertices for "
        f"'{mesh_obj.name}' (wrap deformer refit)"
    )
    return True


def _build_transfer_source_object(mesh_obj, armature_obj, binding):
    part_to_bone = binding.get("part_to_bone_map") or {}
    available_bones = {bone.name for bone in armature_obj.data.bones}
    fallback_bone = _determine_binding_fallback_bone(binding, available_bones)
    vertex_weights = binding.get("binding_vertex_weights") or binding["mesh_data"].get("vertex_weights") or []
    vertices_world = _get_position_transfer_vertices(binding)
    if not vertices_world or len(vertices_world) != len(vertex_weights):
        return None

    faces = []
    for face in binding["mesh_data"].get("faces") or []:
        if face is None or len(face) < 3:
            continue
        try:
            indices = (int(face[0]), int(face[1]), int(face[2]))
        except Exception:
            continue
        if min(indices) < 0 or max(indices) >= len(vertices_world):
            continue
        faces.append(indices)

    local_matrix = mesh_obj.matrix_world.inverted_safe()
    local_vertices = [
        tuple(
            local_matrix @ (position if isinstance(position, Vector) else Vector(position))
        )
        for position in vertices_world
    ]

    source_mesh = bpy.data.meshes.new(get_unique_name(f"__rbxskin_mesh_{mesh_obj.name}"))
    # Bulk RNA arrays instead of from_pydata (no per-vertex python wrappers).
    if not _populate_mesh_geometry(source_mesh, local_vertices, faces):
        bpy.data.meshes.remove(source_mesh)
        return None
    source_mesh.update()

    source_obj = bpy.data.objects.new(get_unique_name(f"__rbxskin_{mesh_obj.name}"), source_mesh)
    source_obj.matrix_world = mesh_obj.matrix_world.copy()

    target_collection = mesh_obj.users_collection[0] if mesh_obj.users_collection else bpy.context.scene.collection
    target_collection.objects.link(source_obj)
    source_obj.hide_viewport = True
    source_obj.hide_render = True

    groups = {}
    for weights in vertex_weights:
        for bone_name in (weights or {}).keys():
            resolved = _resolve_binding_bone_name(
                bone_name,
                part_to_bone,
                available_bones,
                fallback_bone=fallback_bone,
            )
            if resolved and resolved not in groups:
                groups[resolved] = source_obj.vertex_groups.new(name=resolved)

    if not groups:
        _remove_object_and_data(source_obj)
        return None

    matched = 0
    for vertex_index, weights in enumerate(vertex_weights):
        for bone_name, weight in (weights or {}).items():
            resolved = _resolve_binding_bone_name(
                bone_name,
                part_to_bone,
                available_bones,
                fallback_bone=fallback_bone,
            )
            group = groups.get(resolved)
            if group and weight > 0:
                group.add([vertex_index], float(weight), "REPLACE")
                matched += 1

    if matched <= 0:
        _remove_object_and_data(source_obj)
        return None

    return source_obj


def _apply_weight_data_transfer(mesh_obj, source_obj, max_distance=None, mapping=None):
    mapping = mapping or ("POLYINTERP_NEAREST" if source_obj.data.polygons else "NEAREST")

    modifier = mesh_obj.modifiers.new(name="RBXWeightTransfer", type="DATA_TRANSFER")
    modifier.object = source_obj
    modifier.use_vert_data = True
    modifier.data_types_verts = {"VGROUP_WEIGHTS"}
    modifier.vert_mapping = mapping
    modifier.layers_vgroup_select_src = "ALL"
    modifier.layers_vgroup_select_dst = "NAME"
    modifier.mix_mode = "REPLACE"
    modifier.mix_factor = 1.0
    modifier.use_max_distance = max_distance is not None
    if max_distance is not None:
        modifier.max_distance = max_distance

    try:
        if hasattr(bpy.context, "temp_override"):
            with bpy.context.temp_override(
                active_object=mesh_obj,
                object=mesh_obj,
                selected_objects=[mesh_obj],
                selected_editable_objects=[mesh_obj],
            ):
                bpy.ops.object.modifier_apply(modifier=modifier.name)
        else:
            bpy.context.view_layer.objects.active = mesh_obj
            mesh_obj.select_set(True)
            bpy.ops.object.modifier_apply(modifier=modifier.name)
    except Exception:
        try:
            mesh_obj.modifiers.remove(modifier)
        except Exception:
            pass
        raise

    return mapping, max_distance


def _apply_inherited_weight_transfer(mesh_obj, armature_obj, source_meshes):
    available_bones = {bone.name for bone in armature_obj.data.bones}
    if not available_bones:
        return False

    target_center = _get_mesh_world_center(mesh_obj)
    candidates = []
    for source_mesh in source_meshes:
        if source_mesh == mesh_obj or source_mesh.type != "MESH" or source_mesh.data is None:
            continue
        bone_names = [group.name for group in source_mesh.vertex_groups if group.name in available_bones]
        if not bone_names:
            continue
        distance = (_get_mesh_world_center(source_mesh) - target_center).length
        candidates.append((distance, source_mesh, bone_names))

    if not candidates:
        return False

    candidates.sort(key=lambda item: item[0])
    max_dimension = max(max(mesh_obj.dimensions), 1.0)
    initial_max_distance = max_dimension * 0.05

    for distance, source_mesh, bone_names in candidates[:3]:
        _remove_all_vertex_groups(mesh_obj)
        _ensure_vertex_groups(mesh_obj, bone_names)
        try:
            assigned_vertices, total_vertices, coverage, mapping, max_distance = _run_weight_transfer_sequence(
                mesh_obj,
                source_mesh,
                available_bones,
                initial_max_distance,
                label="Inherited weight transfer",
            )
        except Exception as exc:
            _remove_all_vertex_groups(mesh_obj)
            print(
                f"[RigCreate] Inherited weight transfer failed for '{mesh_obj.name}' from '{source_mesh.name}': {exc}"
            )
            continue

        if assigned_vertices <= 0:
            continue

        print(
            f"[RigCreate] Inherited weights used for '{mesh_obj.name}' from '{source_mesh.name}' "
            f"(assigned={assigned_vertices}/{total_vertices}, coverage={coverage:.3f}, mapping={mapping}, "
            f"max_distance={'none' if max_distance is None else f'{max_distance:.6f}'}, distance={distance:.6f})"
        )
        _clear_child_of_constraints(mesh_obj)
        _ensure_armature_modifier(mesh_obj, armature_obj)
        return True

    _remove_all_vertex_groups(mesh_obj)
    return False


def _build_vertex_component_ids(vertex_count, faces):
    if vertex_count <= 0:
        return [], 0

    parents = list(range(vertex_count))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for face in faces or []:
        if face is None or len(face) < 2:
            continue
        try:
            indices = [int(index) for index in face if 0 <= int(index) < vertex_count]
        except Exception:
            continue
        if len(indices) < 2:
            continue
        anchor = indices[0]
        for vertex_index in indices[1:]:
            union(anchor, vertex_index)

    component_by_root = {}
    component_ids = []
    for vertex_index in range(vertex_count):
        root = find(vertex_index)
        component_id = component_by_root.get(root)
        if component_id is None:
            component_id = len(component_by_root)
            component_by_root[root] = component_id
        component_ids.append(component_id)

    return component_ids, len(component_by_root)


def _build_component_centers(component_ids, positions):
    sums = {}
    for vertex_index, component_id in enumerate(component_ids or []):
        if vertex_index >= len(positions):
            continue
        position = positions[vertex_index]
        if position is None:
            continue
        vec = position if isinstance(position, Vector) else Vector(position)
        current = sums.setdefault(component_id, [0.0, 0.0, 0.0, 0])
        current[0] += float(vec.x)
        current[1] += float(vec.y)
        current[2] += float(vec.z)
        current[3] += 1

    centers = {}
    for component_id, values in sums.items():
        count = max(int(values[3]), 1)
        centers[component_id] = Vector((values[0] / count, values[1] / count, values[2] / count))
    return centers


def _map_target_components_to_source_components(target_centers, source_centers):
    mapping = {}
    if not target_centers or not source_centers:
        return mapping

    pair_budget = 250_000
    if len(target_centers) * len(source_centers) <= pair_budget:
        pairs = []
        for target_component, target_center in target_centers.items():
            for source_component, source_center in source_centers.items():
                pairs.append(((target_center - source_center).length_squared, target_component, source_component))

        used_targets = set()
        used_sources = set()
        for _distance, target_component, source_component in sorted(pairs, key=lambda item: item[0]):
            if target_component in used_targets or source_component in used_sources:
                continue
            mapping[target_component] = source_component
            used_targets.add(target_component)
            used_sources.add(source_component)
            if len(used_targets) == len(target_centers) or len(used_sources) == len(source_centers):
                break

    for target_component, target_center in target_centers.items():
        if target_component in mapping:
            continue
        best_source_component = None
        best_distance = None
        for source_component, source_center in source_centers.items():
            distance = (target_center - source_center).length_squared
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_source_component = source_component
        if best_source_component is not None:
            mapping[target_component] = best_source_component

    return mapping


def _closest_point_on_triangle(point, first, second, third):
    edge_ab = second - first
    edge_ac = third - first
    point_a = point - first
    d1 = edge_ab.dot(point_a)
    d2 = edge_ac.dot(point_a)
    if d1 <= 0.0 and d2 <= 0.0:
        return first, (1.0, 0.0, 0.0)

    point_b = point - second
    d3 = edge_ab.dot(point_b)
    d4 = edge_ac.dot(point_b)
    if d3 >= 0.0 and d4 <= d3:
        return second, (0.0, 1.0, 0.0)

    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        blend = d1 / max(d1 - d3, 1e-12)
        return first + edge_ab * blend, (1.0 - blend, blend, 0.0)

    point_c = point - third
    d5 = edge_ab.dot(point_c)
    d6 = edge_ac.dot(point_c)
    if d6 >= 0.0 and d5 <= d6:
        return third, (0.0, 0.0, 1.0)

    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        blend = d2 / max(d2 - d6, 1e-12)
        return first + edge_ac * blend, (1.0 - blend, 0.0, blend)

    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        blend = (d4 - d3) / max((d4 - d3) + (d5 - d6), 1e-12)
        return second + (third - second) * blend, (0.0, 1.0 - blend, blend)

    denom = max(va + vb + vc, 1e-12)
    v_weight = vb / denom
    w_weight = vc / denom
    u_weight = 1.0 - v_weight - w_weight
    return first + edge_ab * v_weight + edge_ac * w_weight, (u_weight, v_weight, w_weight)


def _blend_weight_dicts(weighted_sources):
    blended = {}
    total_factor = 0.0
    for weights, factor in weighted_sources or []:
        if not weights or factor <= 0.0:
            continue
        for bone_name, weight in weights.items():
            if weight > 0:
                blended[bone_name] = blended.get(bone_name, 0.0) + (float(weight) * float(factor))
        total_factor += float(factor)

    if total_factor <= 0.0 or not blended:
        return None
    return _limit_weight_dict({bone_name: weight / total_factor for bone_name, weight in blended.items()})


def _apply_position_bound_weights(mesh_obj, armature_obj, binding):
    """Assign bone weights from filemesh vertex data via UV-first, position-fallback matching.

    The old Blender data-transfer approach (POLYINTERP_NEAREST / NEAREST) fails on
    garments with two close-together geometry regions (e.g. shorts inner thigh) because:
    - POLYINTERP_NEAREST barycentric-interpolates across polygon faces, blending
      bone assignments across zone boundaries.
    - NEAREST still relies on cage-predicted positions (avg ~10 cm off), which can
      place inner-thigh verts from opposite legs closer to each other than to their
      own side, causing topology inversion.

    This implementation uses UV coordinates as the primary matching key (which are
    topology-stable and cleanly divide e.g. left vs right leg), with cage-predicted
    world-position as a tie-breaker when multiple filemesh verts share a UV, and a
    pure nearest-position fallback for any target verts whose UV has no filemesh match.
    """
    part_to_bone = binding.get("part_to_bone_map") or {}
    available_bones = {bone.name for bone in armature_obj.data.bones}
    fallback_bone = _determine_binding_fallback_bone(binding, available_bones)

    mesh_data = binding["mesh_data"]
    vertex_weights = binding.get("binding_vertex_weights") or mesh_data.get("vertex_weights") or []
    if not vertex_weights:
        return False

    # Build resolved bone groups
    bone_names = mesh_data.get("bone_names") or []
    groups = {}
    for bone_name in bone_names:
        resolved = _resolve_binding_bone_name(bone_name, part_to_bone, available_bones, fallback_bone)
        if resolved and resolved not in groups:
            groups[resolved] = mesh_obj.vertex_groups.new(name=resolved)
    if not groups:
        return False

    # Source positions in world space (cage-predicted or raw filemesh)
    source_positions_world = _get_position_transfer_vertices(binding)
    if not source_positions_world or len(source_positions_world) != len(vertex_weights):
        _remove_all_vertex_groups(mesh_obj)
        print(f"[RigCreate] Position bind could not build source positions for '{mesh_obj.name}'")
        return False

    # Build filemesh UV bucket: (round(u,4), 1-round(v,4)) → [source_indices]
    # Note: match both raw and V-flipped filemesh UVs against Blender UVs (Blender
    # stores V from bottom, Roblox stores V from top; the OBJ exporter may or may not flip).
    filemesh_uvs = mesh_data.get("uvs") or []
    uv_bucket_raw = {}
    uv_bucket_flip = {}
    for src_idx, uv in enumerate(filemesh_uvs):
        if uv is None:
            continue
        u = round(float(uv[0]), 4)
        v = round(float(uv[1]), 4)
        uv_bucket_raw.setdefault((u, v), []).append(src_idx)
        uv_bucket_flip.setdefault((u, round(1.0 - float(uv[1]), 4)), []).append(src_idx)

    # Also build precision-3 fallback buckets for float rounding differences between
    # OBJ export and the binary filemesh (a p4 miss can match at p3).
    uv_bucket_raw3 = {}
    uv_bucket_flip3 = {}
    for src_idx, uv in enumerate(filemesh_uvs):
        if uv is None:
            continue
        u3 = round(float(uv[0]), 3)
        v3 = round(float(uv[1]), 3)
        uv_bucket_raw3.setdefault((u3, v3), []).append(src_idx)
        uv_bucket_flip3.setdefault((u3, round(1.0 - float(uv[1]), 3)), []).append(src_idx)

    # Target Blender vertex UVs — collect ALL loop UVs per vertex so seam vertices
    # (which have multiple loops with different UV coordinates) don't miss their match.
    target_all_uvs = _compute_mesh_vertex_all_uvs(mesh_obj)

    # Pick the UV bucket (raw or v-flipped) that gives more matches
    uv_match_raw = 0
    uv_match_flip = 0
    for uvs in target_all_uvs.values():
        for tgt_uv in uvs:
            u = round(float(tgt_uv[0]), 4)
            v = round(float(tgt_uv[1]), 4)
            if (u, v) in uv_bucket_raw:
                uv_match_raw += 1
                break
        for tgt_uv in uvs:
            u = round(float(tgt_uv[0]), 4)
            v = round(float(tgt_uv[1]), 4)
            if (u, v) in uv_bucket_flip:
                uv_match_flip += 1
                break
    uv_bucket = uv_bucket_flip if uv_match_flip > uv_match_raw else uv_bucket_raw
    uv_bucket3 = uv_bucket_flip3 if uv_match_flip > uv_match_raw else uv_bucket_raw3
    uv_method = "flip" if uv_match_flip > uv_match_raw else "raw"
    uv_matched_count = max(uv_match_raw, uv_match_flip)

    # Pre-compute target world positions and normals.
    # Normals are the key discriminator for symmetric garments: left/right legs
    # share identical UV coordinates but face opposite directions.  Position
    # alone is unreliable (cage avg error ~10 cm can exceed the inter-leg gap),
    # but normal direction is barely affected by cage translation error.
    matrix_world = mesh_obj.matrix_world
    normal_matrix = matrix_world.to_3x3().inverted_safe().transposed()
    target_world_positions = [matrix_world @ v.co for v in mesh_obj.data.vertices]
    target_world_normals = [
        (normal_matrix @ v.normal).normalized()
        for v in mesh_obj.data.vertices
    ]

    source_component_ids, source_component_count = _build_vertex_component_ids(
        len(vertex_weights),
        mesh_data.get("faces") or [],
    )
    target_faces = _build_mesh_object_faces(mesh_obj)
    target_component_ids, target_component_count = _build_vertex_component_ids(
        len(mesh_obj.data.vertices),
        target_faces,
    )
    source_component_centers = _build_component_centers(source_component_ids, source_positions_world)
    target_component_centers = _build_component_centers(target_component_ids, target_world_positions)
    target_to_source_component = _map_target_components_to_source_components(
        target_component_centers,
        source_component_centers,
    )
    use_component_filter = source_component_count > 1 and target_component_count > 1 and bool(
        target_to_source_component)
    source_indices_by_component = {}
    for source_index, component_id in enumerate(source_component_ids):
        source_indices_by_component.setdefault(component_id, []).append(source_index)
    source_face_records_by_component = {}
    for face in mesh_data.get("faces") or []:
        if face is None or len(face) < 3:
            continue
        try:
            face_indices = tuple(int(index) for index in face[:3])
        except Exception:
            continue
        if min(face_indices) < 0 or max(face_indices) >= len(source_positions_world):
            continue
        face_components = {
            source_component_ids[index]
            for index in face_indices
            if 0 <= index < len(source_component_ids)
        }
        if len(face_components) != 1:
            continue
        face_positions = tuple(source_positions_world[index] for index in face_indices)
        face_normal = _normalize_vector(
            (face_positions[1] - face_positions[0]).cross(face_positions[2] - face_positions[0]))
        component_id = next(iter(face_components))
        source_face_records_by_component.setdefault(component_id, []).append(
            {
                "indices": face_indices,
                "positions": face_positions,
                "normal": face_normal,
            }
        )
    source_face_records = [
        face_record
        for component_records in source_face_records_by_component.values()
        for face_record in component_records
    ]

    # Filemesh source normals transformed into Blender world space for tie-breaking.
    # Raw filemesh normals are in Roblox space (Y-up); applying t2b + part_cf rotation
    # gives the correct Blender-space direction so the dot product with target_world_normals
    # is meaningful.  This is esp. important for front/back disambiguation where Y and Z
    # are swapped between the two coordinate systems.
    entry = binding.get("entry") or {}
    _t2b = get_transform_to_blender()
    _source_normal_matrix = _t2b.to_3x3()
    _part_cf = entry.get("part_cf")
    if _part_cf is not None:
        try:
            _source_normal_matrix = (_t2b @ cf_to_mat(_part_cf)).to_3x3()
        except Exception:
            pass

    filemesh_normals_raw = mesh_data.get("normals") or []
    source_normals_world = []
    for src_idx in range(len(vertex_weights)):
        n = filemesh_normals_raw[src_idx] if src_idx < len(filemesh_normals_raw) else None
        if n is not None:
            wn = _normalize_vector(_source_normal_matrix @ Vector(n))
            source_normals_world.append((float(wn.x), float(wn.y), float(wn.z)) if wn else None)
        else:
            source_normals_world.append(None)

    def _preferred_source_component(target_index):
        if not use_component_filter or target_index >= len(target_component_ids):
            return None
        return target_to_source_component.get(target_component_ids[target_index])

    def _filter_candidates_to_component(candidates, preferred_component):
        if preferred_component is None:
            return candidates, False
        filtered = [
            candidate
            for candidate in candidates
            if 0 <= candidate < len(source_component_ids) and source_component_ids[candidate] == preferred_component
        ]
        return (filtered, True) if filtered else (candidates, False)

    def _uv_tie_break_score(src_idx, tgt_world, tgt_normal, position_primary=False):
        """Lower = better. Components prevent side swaps; normals help only inside an island."""
        sp = source_positions_world[src_idx]
        dx = sp.x - tgt_world.x
        dy = sp.y - tgt_world.y
        dz = sp.z - tgt_world.z
        dist2 = dx * dx + dy * dy + dz * dz

        sn = source_normals_world[src_idx] if src_idx < len(source_normals_world) else None
        if sn is not None and tgt_normal is not None:
            # dot product: 1.0 = same direction, -1.0 = opposite.
            # Negate so lower score = better agreement
            dot = tgt_normal.x * sn[0] + tgt_normal.y * sn[1] + tgt_normal.z * sn[2]
            normal_cost = 1.0 - max(-1.0, min(1.0, dot))  # 0..2
        else:
            normal_cost = 1.0  # neutral when data missing

        if position_primary:
            return (dist2, normal_cost)
        return (normal_cost, dist2)

    matched = 0
    uv_assigned = 0
    pos_assigned = 0
    # vertex_index → source index/weights for already-assigned local matches
    assigned_source = {}
    assigned_weights = {}
    component_filtered_uv = 0
    component_nearest_assigned = 0
    neighbor_assigned = 0
    blended_nearest_assigned = 0
    face_project_assigned = 0

    def _candidate_distance_normal_score(src_idx, tgt_world, tgt_normal):
        sp = source_positions_world[src_idx]
        dx = sp.x - tgt_world.x
        dy = sp.y - tgt_world.y
        dz = sp.z - tgt_world.z
        dist2 = dx * dx + dy * dy + dz * dz

        sn = source_normals_world[src_idx] if src_idx < len(source_normals_world) else None
        if sn is not None and tgt_normal is not None:
            dot = tgt_normal.x * sn[0] + tgt_normal.y * sn[1] + tgt_normal.z * sn[2]
            normal_cost = 1.0 - max(-1.0, min(1.0, dot))
        else:
            normal_cost = 1.0

        return dist2, normal_cost

    def _blend_nearest_candidate_weights(candidate_indices, tgt_world, tgt_normal, max_samples=4):
        best_entries = []
        for candidate in candidate_indices or []:
            if candidate < 0 or candidate >= len(vertex_weights):
                continue
            candidate_weights = vertex_weights[candidate] or {}
            if not candidate_weights:
                continue
            dist2, normal_cost = _candidate_distance_normal_score(candidate, tgt_world, tgt_normal)
            entry = (dist2, normal_cost, candidate)
            best_entries.append(entry)
            best_entries.sort(key=lambda item: (item[0], item[1]))
            if len(best_entries) > max_samples:
                best_entries.pop()

        if not best_entries:
            return None, None, False

        best_entries.sort(key=lambda item: (item[0], item[1]))
        representative = best_entries[0][2]
        if len(best_entries) == 1 or best_entries[0][0] <= 1e-12:
            return _limit_weight_dict(vertex_weights[representative] or {}), representative, False

        blended = {}
        total_factor = 0.0
        for dist2, normal_cost, candidate in best_entries:
            distance = max(dist2 ** 0.5, 1e-6)
            normal_factor = 1.0 / max(0.25 + normal_cost, 0.25)
            factor = (1.0 / distance) * normal_factor
            if factor <= 0.0:
                continue
            for bone_name, weight in (vertex_weights[candidate] or {}).items():
                if weight > 0:
                    blended[bone_name] = blended.get(bone_name, 0.0) + (float(weight) * factor)
            total_factor += factor

        if total_factor <= 0.0 or not blended:
            return _limit_weight_dict(vertex_weights[representative] or {}), representative, False

        return _limit_weight_dict({bone_name: weight / total_factor for bone_name,
                                  weight in blended.items()}), representative, True

    def _blend_face_projected_weights(face_records, tgt_world, tgt_normal):
        best_record = None
        best_score = None
        best_barycentric = None
        for face_record in face_records or []:
            face_positions = face_record["positions"]
            closest_point, barycentric = _closest_point_on_triangle(
                tgt_world,
                face_positions[0],
                face_positions[1],
                face_positions[2],
            )
            distance_squared = (closest_point - tgt_world).length_squared
            face_normal = face_record.get("normal")
            if face_normal is not None and tgt_normal is not None:
                dot = tgt_normal.dot(face_normal)
                normal_cost = 1.0 - max(-1.0, min(1.0, dot))
            else:
                normal_cost = 1.0
            score = (distance_squared, normal_cost)
            if best_score is None or score < best_score:
                best_score = score
                best_record = face_record
                best_barycentric = barycentric

        if best_record is None or best_barycentric is None:
            return None, None

        weighted_sources = []
        representative = None
        representative_factor = -1.0
        for source_index, factor in zip(best_record["indices"], best_barycentric):
            if factor <= 0.0:
                continue
            if factor > representative_factor:
                representative = source_index
                representative_factor = factor
            weighted_sources.append((vertex_weights[source_index] or {}, factor))

        blended = _blend_weight_dicts(weighted_sources)
        if blended is None:
            return None, None
        return blended, representative

    for blender_vertex in mesh_obj.data.vertices:
        tgt_idx = blender_vertex.index
        tgt_world = target_world_positions[tgt_idx]
        tgt_normal = target_world_normals[tgt_idx] if tgt_idx < len(target_world_normals) else None
        target_component = target_component_ids[tgt_idx] if tgt_idx < len(target_component_ids) else None
        preferred_source_component = _preferred_source_component(tgt_idx)
        best_src = None
        best_weights = None

        # 1. UV match — try all loop UVs for this vertex, tie-break with normal then position
        tgt_uvs_list = target_all_uvs.get(tgt_idx) or []
        for tgt_uv in tgt_uvs_list:
            u = round(float(tgt_uv[0]), 4)
            v = round(float(tgt_uv[1]), 4)
            candidates = uv_bucket.get((u, v))
            if not candidates:
                # Precision-3 fallback for float rounding mismatches
                u3 = round(float(tgt_uv[0]), 3)
                v3 = round(float(tgt_uv[1]), 3)
                candidates = uv_bucket3.get((u3, v3))
            if candidates:
                candidates, component_filtered = _filter_candidates_to_component(candidates, preferred_source_component)
                if component_filtered:
                    component_filtered_uv += 1
                if len(candidates) == 1:
                    best_src = candidates[0]
                else:
                    best_score = None
                    for c in candidates:
                        score = _uv_tie_break_score(c, tgt_world, tgt_normal, position_primary=component_filtered)
                        if best_score is None or score < best_score:
                            best_score = score
                            best_src = c
                uv_assigned += 1
                assigned_source[tgt_idx] = best_src
                best_weights = vertex_weights[best_src] or {}
                assigned_weights[tgt_idx] = best_weights
                break  # stop once any loop UV matched

        # If the UV exists elsewhere but not on the mapped component, stay on the
        # mapped island and use nearest local position. Mirrored accessories often
        # reuse UVs across wrists/ankles; accepting a global UV candidate here swaps sides.
        if best_src is not None and preferred_source_component is not None:
            if 0 <= best_src < len(
                    source_component_ids) and source_component_ids[best_src] != preferred_source_component:
                assigned_source.pop(tgt_idx, None)
                assigned_weights.pop(tgt_idx, None)
                uv_assigned = max(0, uv_assigned - 1)
                best_src = None
                best_weights = None

        if best_src is None and preferred_source_component is not None:
            component_faces = source_face_records_by_component.get(preferred_source_component) or []
            if component_faces:
                best_weights, best_src = _blend_face_projected_weights(
                    component_faces,
                    tgt_world,
                    tgt_normal,
                )
                if best_src is not None:
                    component_nearest_assigned += 1
                    face_project_assigned += 1
                    assigned_source[tgt_idx] = best_src
                    assigned_weights[tgt_idx] = best_weights

            if best_src is None:
                component_candidates = source_indices_by_component.get(preferred_source_component) or []
                if component_candidates:
                    best_weights, best_src, blended = _blend_nearest_candidate_weights(
                        component_candidates,
                        tgt_world,
                        tgt_normal,
                    )
                if best_src is not None:
                    component_nearest_assigned += 1
                    if blended:
                        blended_nearest_assigned += 1
                    assigned_source[tgt_idx] = best_src
                    assigned_weights[tgt_idx] = best_weights

        # 2. Fallback: propagate from nearest already-UV-matched Blender neighbour
        #    (avoids using cage-predicted positions directly for seam/border verts).
        if best_src is None:
            best_dist2 = float("inf")
            for matched_tgt, matched_src in assigned_source.items():
                if use_component_filter and matched_tgt < len(
                        target_component_ids) and target_component_ids[matched_tgt] != target_component:
                    continue
                mp = target_world_positions[matched_tgt]
                dx = mp.x - tgt_world.x
                dy = mp.y - tgt_world.y
                dz = mp.z - tgt_world.z
                d2 = dx * dx + dy * dy + dz * dz
                if d2 < best_dist2:
                    best_dist2 = d2
                    best_src = matched_src
                    best_weights = assigned_weights.get(matched_tgt)
            if best_src is not None:
                neighbor_assigned += 1

        # 3. Last resort: nearest cage-predicted filemesh position
        if best_src is None:
            fallback_source_indices = source_indices_by_component.get(
                preferred_source_component) if preferred_source_component is not None else None
            if not fallback_source_indices:
                fallback_source_indices = range(len(source_positions_world))
            fallback_faces = (
                source_face_records_by_component.get(preferred_source_component)
                if preferred_source_component is not None
                else source_face_records
            )
            if fallback_faces:
                best_weights, best_src = _blend_face_projected_weights(
                    fallback_faces,
                    tgt_world,
                    tgt_normal,
                )
                if best_src is not None:
                    face_project_assigned += 1
            if best_src is None:
                best_weights, best_src, blended = _blend_nearest_candidate_weights(
                    fallback_source_indices,
                    tgt_world,
                    tgt_normal,
                )
                if blended:
                    blended_nearest_assigned += 1
            pos_assigned += 1

        if best_src is None:
            continue

        weights_src = best_weights if best_weights is not None else (vertex_weights[best_src] or {})
        for bone_name, weight in weights_src.items():
            resolved = _resolve_binding_bone_name(bone_name, part_to_bone, available_bones, fallback_bone)
            group = groups.get(resolved)
            if group and weight > 0:
                group.add([tgt_idx], float(weight), "REPLACE")
                matched += 1

    if matched <= 0:
        _remove_all_vertex_groups(mesh_obj)
        print(f"[RigCreate] Position bind produced no weights for '{mesh_obj.name}'")
        return False

    total_vertices = len(mesh_obj.data.vertices)
    uv_ratio = uv_assigned / max(total_vertices, 1)
    local_transfer_count = component_nearest_assigned + pos_assigned
    nearest_transfer_count = max(0, local_transfer_count - face_project_assigned)
    nearest_ratio = nearest_transfer_count / max(total_vertices, 1)
    face_project_ratio = face_project_assigned / max(total_vertices, 1)
    strong_face_projection = face_project_ratio >= 0.90 and nearest_ratio <= 0.05
    island_ratio = target_component_count / max(source_component_count, 1)
    confidence_notes = []
    if uv_assigned <= 0:
        confidence_notes.append("no uv links")
    elif uv_ratio < 0.75:
        confidence_notes.append(f"partial uv coverage {uv_ratio:.3f}")
    if face_project_ratio > 0.50:
        confidence_notes.append(f"mostly face projection {face_project_ratio:.3f}")
    if nearest_ratio > 0.50:
        confidence_notes.append(f"mostly nearest transfer {nearest_ratio:.3f}")
    elif nearest_ratio > 0.20:
        confidence_notes.append(f"substantial nearest transfer {nearest_ratio:.3f}")
    if island_ratio > 4.0:
        confidence_notes.append(f"fragmented target islands {target_component_count}->{source_component_count}")

    if not confidence_notes:
        bind_confidence = "high"
    elif nearest_ratio > 0.50 or (uv_assigned <= 0 and not strong_face_projection) or (island_ratio > 4.0 and not strong_face_projection):
        bind_confidence = "low"
    else:
        bind_confidence = "medium"

    print(
        f"[RigCreate] Position bind used for '{mesh_obj.name}' "
        f"(uv={uv_assigned}/{total_vertices} [{uv_method}, candidates={uv_matched_count}], "
        f"islands={target_component_count}->{source_component_count}, island_uv={component_filtered_uv}/{uv_assigned}, "
        f"island_nearest={component_nearest_assigned}/{total_vertices}, "
        f"face_project={face_project_assigned}/{total_vertices}, "
        f"nearest_blend={blended_nearest_assigned}/{total_vertices}, "
        f"neighbor_propagate={neighbor_assigned}/{total_vertices}, "
        f"pos_fallback={pos_assigned}/{total_vertices}, confidence={bind_confidence})"
    )
    if bind_confidence == "low":
        print(
            f"[RigCreate] Position bind low-confidence for '{mesh_obj.name}': "
            f"{'; '.join(confidence_notes)}"
        )
    _clear_child_of_constraints(mesh_obj)
    _ensure_armature_modifier(mesh_obj, armature_obj)
    return True


def _apply_index_bound_weights(mesh_obj, armature_obj, binding):
    part_to_bone = binding.get("part_to_bone_map") or {}
    groups = {}
    available_bones = {bone.name for bone in armature_obj.data.bones}
    fallback_bone = _determine_binding_fallback_bone(binding, available_bones)
    for bone_name in binding["mesh_data"].get("bone_names") or []:
        resolved = _resolve_binding_bone_name(
            bone_name,
            part_to_bone,
            available_bones,
            fallback_bone=fallback_bone,
        )
        if resolved and resolved not in groups:
            groups[resolved] = mesh_obj.vertex_groups.new(name=resolved)

    if not groups:
        return False

    matched = 0
    for vertex, weights in zip(mesh_obj.data.vertices, binding["mesh_data"].get("vertex_weights") or []):
        for bone_name, weight in weights.items():
            resolved = _resolve_binding_bone_name(
                bone_name,
                part_to_bone,
                available_bones,
                fallback_bone=fallback_bone,
            )
            group = groups.get(resolved)
            if group and weight > 0:
                group.add([vertex.index], weight, "REPLACE")
                matched += 1

    if matched <= 0:
        return False

    _clear_child_of_constraints(mesh_obj)
    _ensure_armature_modifier(mesh_obj, armature_obj)
    return True


def _apply_uv_map_bound_weights(mesh_obj, armature_obj, binding):
    part_to_bone = binding.get("part_to_bone_map") or {}
    groups = {}
    available_bones = {bone.name for bone in armature_obj.data.bones}
    fallback_bone = _determine_binding_fallback_bone(binding, available_bones)
    for bone_name in binding["mesh_data"].get("bone_names") or []:
        resolved = _resolve_binding_bone_name(
            bone_name,
            part_to_bone,
            available_bones,
            fallback_bone=fallback_bone,
        )
        if resolved and resolved not in groups:
            groups[resolved] = mesh_obj.vertex_groups.new(name=resolved)

    if not groups:
        return False

    # vertex-map links index into the collapsed vertex array (binding_vertex_weights),
    # NOT the original filemesh vertex_weights. Using the wrong array causes arbitrary
    # bone assignments (e.g. LowerTorso bleeding into clothing fronts).
    vertex_weights = (
        binding.get("binding_vertex_weights")
        or binding["mesh_data"].get("vertex_weights")
        or []
    )
    matched_vertices = set()
    matched = 0
    for source_index, target_index in binding.get("vertex_links") or []:
        if target_index in matched_vertices:
            continue
        if source_index < 0 or source_index >= len(vertex_weights):
            continue
        if target_index < 0 or target_index >= len(mesh_obj.data.vertices):
            continue
        matched_vertices.add(target_index)
        for bone_name, weight in (vertex_weights[source_index] or {}).items():
            resolved = _resolve_binding_bone_name(
                bone_name,
                part_to_bone,
                available_bones,
                fallback_bone=fallback_bone,
            )
            group = groups.get(resolved)
            if group and weight > 0:
                group.add([target_index], weight, "REPLACE")
                matched += 1

    if matched <= 0:
        return False

    mode = binding.get("mode")
    if mode == "vertex-map":
        label = "Triangulated vertex bind"
        coverage = binding.get("vertex_link_coverage", 0.0)
    else:
        label = "Source uv bind"
        coverage = binding.get("uv_link_coverage", 0.0)

    print(
        f"[RigCreate] {label} used for '{mesh_obj.name}' "
        f"(links={len(binding.get('vertex_links') or [])}, coverage={coverage:.3f})"
    )
    _clear_child_of_constraints(mesh_obj)
    _ensure_armature_modifier(mesh_obj, armature_obj)
    return True


def _apply_direct_source_transfer_weights(mesh_obj, armature_obj, binding):
    """Robust weight path for body parts: transfer vertex groups straight from
    the exact source geometry using Blender's data-transfer, matching target
    verts to source verts by position. Reads the RAW authored vertex weights
    (1:1 with source positions) — bypasses the hand-rolled collapse/resolve
    that bled child-limb weights into parent geometry at every joint."""
    available_bones = {bone.name for bone in armature_obj.data.bones}
    mesh_data = binding.get("mesh_data") or {}
    raw_weights = mesh_data.get("vertex_weights") or []
    if not _has_meaningful_vertex_weights(raw_weights):
        return False

    # Use raw authored weights, NOT binding_vertex_weights (the resolution
    # output). Positions come from the (cage-predicted or raw) source verts.
    transfer_binding = dict(binding)
    transfer_binding.pop("binding_vertex_weights", None)
    source_obj = _build_transfer_source_object(mesh_obj, armature_obj, transfer_binding)
    if source_obj is None:
        return False

    try:
        # The data-transfer modifier only writes to existing destination groups
        # when layers_vgroup_select_dst is "NAME". Pre-create matching groups so
        # the transfer actually lands.
        existing_groups = {g.name for g in mesh_obj.vertex_groups}
        for group in source_obj.vertex_groups:
            if group.name not in existing_groups:
                mesh_obj.vertex_groups.new(name=group.name)

        max_dimension = max(max(mesh_obj.dimensions), 1.0)
        initial_max_distance = max_dimension * 0.02
        # NEAREST (not POLYINTERP): each vert takes its closest source vert's
        # authored weights verbatim. Interpolation would blend arm+torso at the
        # shoulder — the very bleeding we're removing. The source mesh carries
        # the correct authored weight at every position, so nearest is exact.
        assigned_vertices, total_vertices, coverage, mapping, _max_distance = _run_weight_transfer_sequence(
            mesh_obj,
            source_obj,
            available_bones,
            initial_max_distance,
            label="Direct source weight transfer",
            preferred_mapping="NEAREST",
        )
        if assigned_vertices <= 0:
            return False
        print(
            f"[RigCreate] Direct source weights used for '{mesh_obj.name}' "
            f"(assigned={assigned_vertices}/{total_vertices}, coverage={coverage:.3f}, mapping={mapping})"
        )
        _clear_child_of_constraints(mesh_obj)
        _ensure_armature_modifier(mesh_obj, armature_obj)
        return True
    except Exception as exc:
        print(f"[RigCreate] Direct source weight transfer failed for '{mesh_obj.name}': {exc}")
        return False
    finally:
        _remove_object_and_data(source_obj)


def _apply_skinned_mesh_bindings(armature_obj, bindings):
    applied = 0
    wrap_bindings = []
    weighted_meshes = []

    for mesh_obj, binding in bindings.items():
        if mesh_obj.type != "MESH" or mesh_obj.data is None:
            continue

        _remove_all_vertex_groups(mesh_obj)
        if _get_wrap_layer_metadata(binding.get("entry") or {}):
            wrap_bindings.append((mesh_obj, binding))
            continue

        _log_binding_apply(mesh_obj, binding, "skin bind")

        if binding.get("mode") == "vertex-map":
            # Body parts: bypass the collapse/resolve and pull weights straight
            # from exact source geometry via Blender's transfer.
            success = _apply_direct_source_transfer_weights(mesh_obj, armature_obj, binding)
            if not success:
                success = _apply_uv_map_bound_weights(mesh_obj, armature_obj, binding)
        elif binding.get("mode") == "uv-map":
            success = _apply_uv_map_bound_weights(mesh_obj, armature_obj, binding)
        elif binding.get("mode") == "position":
            success = _apply_position_bound_weights(mesh_obj, armature_obj, binding)
        elif binding.get("mode") == "rigid":
            success = _apply_rigid_bone_binding(mesh_obj, armature_obj, binding)
        else:
            success = _apply_index_bound_weights(mesh_obj, armature_obj, binding)

        if success:
            applied += 1
            weighted_meshes.append(mesh_obj)
        else:
            print(f"[RigCreate] Failed to apply skinned weights to '{mesh_obj.name}'")

    for mesh_obj, binding in wrap_bindings:
        _remove_all_vertex_groups(mesh_obj)
        success = False
        _log_binding_apply(mesh_obj, binding, "layered clothing bind")

        if binding.get("mode") in ("uv-map", "vertex-map"):
            success = _apply_uv_map_bound_weights(mesh_obj, armature_obj, binding)
        elif binding.get("mode") == "position":
            success = _apply_position_bound_weights(mesh_obj, armature_obj, binding)
        elif binding.get("mode") == "index":
            success = _apply_index_bound_weights(mesh_obj, armature_obj, binding)

        if not success:
            success = _apply_inherited_weight_transfer(mesh_obj, armature_obj, weighted_meshes)

        if success:
            applied += 1
            weighted_meshes.append(mesh_obj)
            _rebase_mesh_to_predicted_positions(mesh_obj, binding)
        else:
            print(f"[RigCreate] Failed to apply layered clothing weights to '{mesh_obj.name}' (no deterministic bind)")

    return applied


def _apply_rigid_bone_binding(mesh_obj, armature_obj, binding):
    """Bind an unweighted accessory rigidly to one bone (full weight)."""
    bone_name = binding.get("rigid_bone")
    bone = armature_obj.data.bones.get(bone_name) if bone_name else None
    if bone is None:
        print(
            f"[RigCreate] Rigid bone '{bone_name}' missing for '{mesh_obj.name}'; "
            f"leaving unbound"
        )
        return False
    if not bone.use_deform:
        # Non-deform bones cannot influence vertex groups, so a rigid link
        # must go through a CHILD_OF constraint instead.
        from .constraints import link_object_to_bone_rigid

        link_object_to_bone_rigid(mesh_obj, armature_obj, bone)
        return True
    group = mesh_obj.vertex_groups.new(name=bone_name)
    # Blender 4.x+ requires the explicit type argument on VertexGroup.add.
    group.add(list(range(len(mesh_obj.data.vertices))), 1.0, "REPLACE")
    _clear_child_of_constraints(mesh_obj)
    _ensure_armature_modifier(mesh_obj, armature_obj)
    return True
