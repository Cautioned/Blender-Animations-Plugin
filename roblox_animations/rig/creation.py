"""
Rig creation and bone management utilities.
"""

import json
import re
import bpy
from mathutils import Vector, Matrix
from ..animation.face_controls import (
    FACE_DEFORM_BONE_PROP,
    facs_payload_from_mesh_data,
    merge_facs_payloads,
    store_facs_payload_on_armature,
)
from ..core.constants import get_transform_to_blender
from .cage_solver import build_mesh_vertices, link_targets_to_sources_by_position, link_vertices_by_uv, solve_two_stage_cage_deformation
from .filemesh import fetch_and_parse_filemesh
from ..core.utils import (
    cf_to_mat,
    get_unique_name,
    get_object_by_name,
    find_master_collection_for_object,
    find_parts_collection_in_master,
)


def _matrix_to_idprop(value):
    """Convert Matrix values to list-of-lists so IDProperties accept them."""
    if isinstance(value, Matrix):
        return [list(row) for row in value]
    return value


def _strip_suffix(name: str) -> str:
    """Strip .001/.002 style suffixes for stable matching."""
    return re.sub(r"\.\d+$", "", name or "")


def _get_mesh_world_center(obj):
    """Vertex centroid in world space.

    OBJ-imported meshes have matrix_world == Identity, so
    matrix_world.to_translation() returns (0,0,0) for ALL of them.
    This function computes the actual geometric center from vertex data.
    """
    if obj.type != "MESH" or not obj.data.vertices:
        return obj.matrix_world.to_translation()
    from mathutils import Vector as _Vec
    verts = obj.data.vertices
    n = len(verts)
    sx = sy = sz = 0.0
    for v in verts:
        sx += v.co.x
        sy += v.co.y
        sz += v.co.z
    return obj.matrix_world @ _Vec((sx / n, sy / n, sz / n))


def _mesh_center_in_t2b_space(obj):
    """Vertex centroid in world space.

    The OBJ importer and t2b produce identical blender-space positions
    (confirmed empirically). No axis correction is needed.
    """
    return _get_mesh_world_center(obj)


def _safe_mode_set(mode, obj=None):
    ctx = bpy.context
    if obj:
        try:
            ctx.view_layer.objects.active = obj
            obj.select_set(True)
        except Exception:
            pass

    try:
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode=mode)
            return True
    except Exception:
        pass

    if hasattr(ctx, "temp_override") and obj:
        try:
            with ctx.temp_override(active_object=obj, object=obj, selected_objects=[obj], selected_editable_objects=[obj]):
                if bpy.ops.object.mode_set.poll():
                    bpy.ops.object.mode_set(mode=mode)
                    return True
        except Exception:
            pass

    try:
        bpy.ops.object.mode_set(mode=mode)
        return True
    except Exception:
        return False


def _iter_part_aux_entries(meta_loaded):
    part_aux = meta_loaded.get("partAux") or []
    if isinstance(part_aux, dict):
        return list(part_aux.values())
    return list(part_aux)


def _build_part_to_bone_map(rig_node, result=None):
    """Return {part_name: bone_name} from pname->jname pairs in the rig tree.

    Clothing filemeshes are authored against the Roblox FBX skeleton which uses
    PART names ("Head", "UpperTorso" ...) as bone names, while the armature built
    from the export metadata uses JOINT names (jname: "Neck", "Waist" ...).  This
    map lets us remap clothing vertex-weight keys to the correct armature bone.
    """
    if result is None:
        result = {}
    if not isinstance(rig_node, dict):
        return result
    pname = rig_node.get("pname")
    jname = rig_node.get("jname")
    if pname and jname and pname not in result:
        result[pname] = jname
    for child in rig_node.get("children") or []:
        _build_part_to_bone_map(child, result)
    return result


def _mesh_bones_overlap_rig(mesh_bone_names, rig_names, part_to_bone_map=None):
    if not mesh_bone_names or not rig_names:
        return False

    if rig_names.intersection(mesh_bone_names):
        return True

    if not part_to_bone_map:
        return False

    for bone_name in mesh_bone_names:
        if part_to_bone_map.get(bone_name) in rig_names:
            return True

    return False


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
    print(f"[RigCreate] Applying {stage} for '{mesh_obj.name}': {_format_binding_context(binding)}")


def _get_wrap_layer_metadata(entry):
    wrap_layer = entry.get("wrap_layer") if isinstance(entry, dict) else None
    return wrap_layer if isinstance(wrap_layer, dict) else None


def _get_wrap_target_metadata(entry):
    wrap_target = entry.get("wrap_target") if isinstance(entry, dict) else None
    return wrap_target if isinstance(wrap_target, dict) else None


def _find_parts_object(parts_collection, part_name):
    if not part_name:
        return None
    obj = parts_collection.objects.get(part_name)
    if obj:
        return obj

    stripped = _strip_suffix(part_name)
    for candidate in parts_collection.objects:
        if _strip_suffix(candidate.name) == stripped:
            return candidate
    return None


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


def _set_mesh_smooth_shading(mesh):
    polygons = getattr(mesh, "polygons", None)
    if not polygons:
        return
    try:
        polygons.foreach_set("use_smooth", [True] * len(polygons))
    except Exception:
        for polygon in polygons:
            polygon.use_smooth = True


def _apply_mesh_custom_normals(mesh, vertices):
    if not hasattr(mesh, "normals_split_custom_set_from_vertices"):
        return False
    if not vertices or len(vertices) != len(mesh.vertices):
        return False

    normals = []
    for vertex in vertices:
        normal = vertex.get("normal") if isinstance(vertex, dict) else None
        if normal is None:
            return False
        normals.append(tuple(float(component) for component in normal))

    try:
        if hasattr(mesh, "use_auto_smooth"):
            mesh.use_auto_smooth = True
        mesh.normals_split_custom_set_from_vertices(normals)
        return True
    except Exception:
        return False


def _configure_synthesized_mesh_surface(mesh_obj, vertices):
    mesh = getattr(mesh_obj, "data", None)
    if mesh is None:
        return

    _set_mesh_smooth_shading(mesh)
    mesh_obj["RBXSynthesizedCustomNormals"] = bool(_apply_mesh_custom_normals(mesh, vertices))


def _configure_synthesized_mesh_display(mesh_obj, entry):
    helper_display = bool(_get_wrap_target_metadata(entry) if isinstance(entry, dict) else None)
    mesh_obj["RBXDisplayHelper"] = helper_display
    if not helper_display:
        return

    mesh_obj.hide_render = True
    if hasattr(mesh_obj, "display_type"):
        mesh_obj.display_type = "WIRE"
    elif hasattr(mesh_obj, "show_wire"):
        mesh_obj.show_wire = True


def _ensure_synthesized_display_modifier(mesh_obj):
    if getattr(mesh_obj, "type", None) != "MESH":
        return None
    if not bool(mesh_obj.get("RBXSynthesizedPart")):
        return None
    if not bool(mesh_obj.get("RBXDisplayHelper")):
        return None

    modifier = None
    for existing in mesh_obj.modifiers:
        if existing.type == "WELD" and existing.name == "RBXSynthDisplayWeld":
            modifier = existing
            break

    if modifier is None:
        try:
            modifier = mesh_obj.modifiers.new(name="RBXSynthDisplayWeld", type="WELD")
        except Exception:
            return None

    threshold = max(max(mesh_obj.dimensions), 1.0) * 1e-6
    if hasattr(modifier, "merge_threshold"):
        modifier.merge_threshold = threshold
    elif hasattr(modifier, "merge_distance"):
        modifier.merge_distance = threshold
    return modifier


def _iter_rig_node_names(node):
    if not isinstance(node, dict):
        return
    jname = node.get("jname")
    if jname:
        yield jname
    for child in node.get("children", []):
        yield from _iter_rig_node_names(child)


def _normalize_vector(vector):
    if vector is None:
        return None
    normalized = vector.copy()
    if normalized.length_squared > 0:
        normalized.normalize()
        return normalized
    return None


def _round_vector_key(vector, precision=5):
    if vector is None:
        return None
    return tuple(round(float(component), precision) for component in vector)


def _round_uv_key(uv, precision=5):
    if uv is None:
        return None
    return tuple(round(float(component), precision) for component in uv)


def _compute_mesh_scale(part_size, mesh_size):
    scale = []
    for idx in range(3):
        mesh_component = float(mesh_size[idx]) if mesh_size and idx < len(mesh_size) else 0.0
        part_component = float(part_size[idx]) if part_size and idx < len(part_size) else 1.0
        scale.append(part_component / mesh_component if abs(mesh_component) > 1e-8 else 1.0)
    return scale


def _coerce_cf_matrix(value):
    if value is None:
        return None
    if isinstance(value, Matrix):
        return value.copy()
    return cf_to_mat(value)


def _compose_wrap_local_matrix(origin=None, import_origin=None, bind_offset=None):
    local_matrix = Matrix.Identity(4)

    origin_matrix = _coerce_cf_matrix(origin)
    if origin_matrix is not None:
        local_matrix = local_matrix @ origin_matrix

    import_matrix = _coerce_cf_matrix(import_origin)
    if import_matrix is not None:
        local_matrix = local_matrix @ import_matrix.inverted_safe()

    bind_offset_matrix = _coerce_cf_matrix(bind_offset)
    if bind_offset_matrix is not None:
        local_matrix = local_matrix @ bind_offset_matrix

    return local_matrix


def _compose_wrap_geometry_matrix(origin=None, import_origin=None, bind_offset=None):
    """Wrap cages are placed from the mesh part cframe plus the explicit origin only."""
    return _compose_wrap_local_matrix(origin=origin)


def _normalize_wrap_auto_skin(value):
    if not value:
        return None
    text = str(value)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def _build_transformed_filemesh_vertices(mesh_data, part_cf=None, part_size=None, mesh_size=None, local_cf=None):
    positions = mesh_data.get("positions") or []
    if not positions:
        return []

    t2b = get_transform_to_blender()
    world_matrix = Matrix.Identity(4)
    if part_cf:
        try:
            world_matrix = t2b @ cf_to_mat(part_cf)
        except Exception:
            return []

    local_matrix = Matrix.Identity(4)
    if local_cf:
        try:
            local_matrix = _coerce_cf_matrix(local_cf) or Matrix.Identity(4)
        except Exception:
            return []

    transform_matrix = world_matrix @ local_matrix
    normal_matrix = transform_matrix.to_3x3()
    scale = _compute_mesh_scale(part_size, mesh_size)
    scale_x, scale_y, scale_z = scale

    normals = mesh_data.get("normals") or []
    uvs = mesh_data.get("uvs") or []
    normals_len = len(normals)
    uvs_len = len(uvs)
    transformed = []
    transformed_append = transformed.append
    for vertex_index, position in enumerate(positions):
        local_vec = Vector((position[0] * scale_x, position[1] * scale_y, position[2] * scale_z))
        world_vec = transform_matrix @ local_vec
        normal = normals[vertex_index] if vertex_index < normals_len else None
        uv = uvs[vertex_index] if vertex_index < uvs_len else None
        world_normal = None
        if normal is not None:
            world_normal = _normalize_vector(normal_matrix @ Vector(normal))
        transformed_append(
            {
                "index": vertex_index,
                "position": world_vec,
                "normal": world_normal,
                "uv": (float(uv[0]), float(uv[1])) if uv is not None else None,
            }
        )

    return transformed


def _build_position_samples_from_vertices(vertices, vertex_weights):
    samples = []
    weight_count = len(vertex_weights)
    samples_append = samples.append
    for vertex_index, vertex in enumerate(vertices):
        weights = vertex_weights[vertex_index] if vertex_index < weight_count else None
        if not weights:
            continue

        resolved_weights = {}
        for bone_name, value in weights.items():
            weight = float(value)
            if weight > 0.0:
                resolved_weights[bone_name] = weight
        if not resolved_weights:
            continue

        position = vertex["position"]
        normal = vertex.get("normal")
        uv = vertex.get("uv")
        samples_append(
            {
                "index": vertex.get("index", vertex_index),
                "position": position.copy(),
                "position_key": _round_vector_key(position),
                "normal": normal,
                "normal_key": _round_vector_key(normal, precision=4),
                "uv": uv,
                "uv_key": _round_uv_key(uv, precision=4),
                "weights": resolved_weights,
            }
        )
    return samples


def _has_meaningful_vertex_weights(vertex_weights):
    for weights in vertex_weights or []:
        if not weights:
            continue
        for value in weights.values():
            if float(value) > 0.0:
                return True
    return False


def _build_position_samples(binding):
    entry = binding["entry"]
    mesh_data = binding["mesh_data"]
    vertices = _build_transformed_filemesh_vertices(
        mesh_data,
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=entry.get("mesh_size") or entry.get("part_size"),
    )
    if not vertices:
        return None
    return _build_position_samples_from_vertices(vertices, mesh_data.get("vertex_weights") or [])


def _is_wrap_binding(binding):
    return bool(binding.get("wrap_solver") or _get_wrap_layer_metadata(binding.get("entry") or {}))


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


def _find_position_candidate_indices(world_position, normal_key, uv_key, sample_lookup, sample_signature_lookup, coarse_lookups):
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


def _compute_filemesh_world_positions(binding):
    entry = binding["entry"]
    vertices = _build_transformed_filemesh_vertices(
        binding["mesh_data"],
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=entry.get("mesh_size") or entry.get("part_size"),
    )
    if not vertices:
        return None
    return [vertex["position"] for vertex in vertices]


def _build_transformed_filemesh_geometry(mesh_data, part_cf=None, part_size=None, mesh_size=None, local_cf=None):
    vertices = _build_transformed_filemesh_vertices(
        mesh_data,
        part_cf=part_cf,
        part_size=part_size,
        mesh_size=mesh_size,
        local_cf=local_cf,
    )
    if not vertices:
        return None, []

    faces = []
    for face in mesh_data.get("faces") or []:
        if face is None or len(face) < 3:
            continue
        try:
            faces.append((int(face[0]), int(face[1]), int(face[2])))
        except Exception:
            continue

    return vertices, faces


def _collapse_weighted_source_geometry(vertices, vertex_weights, faces, precision=6):
    collapsed_vertices = []
    collapsed_weights = []
    collapsed_faces = []
    # representative_original_indices[collapsed_index] = one representative original
    # vertex index. Used so callers can read the ORIGINAL per-vertex weight rather than
    # a blended aggregate, which would introduce bone contamination across zone boundaries.
    representative_original_indices = []
    sums_by_index = []
    index_by_key = {}
    remap = {}
    vertex_weight_count = len(vertex_weights)

    for source_index, vertex in enumerate(vertices or []):
        position = vertex.get("position")
        if position is None:
            continue

        key = _round_vector_key(position, precision=precision)
        collapsed_index = index_by_key.get(key)
        position_x = float(position[0])
        position_y = float(position[1])
        position_z = float(position[2])
        normal = vertex.get("normal")
        if collapsed_index is None:
            collapsed_index = len(collapsed_vertices)
            index_by_key[key] = collapsed_index
            collapsed_vertices.append(
                {
                    "index": collapsed_index,
                    "position": (position_x, position_y, position_z),
                    "normal": normal,
                    "uv": None,
                }
            )
            collapsed_weights.append({})
            representative_original_indices.append(source_index)
            if normal is None:
                sums_by_index.append([position_x, position_y, position_z, 0.0, 0.0, 0.0, 1, False])
            else:
                sums_by_index.append(
                    [
                        position_x,
                        position_y,
                        position_z,
                        float(normal[0]),
                        float(normal[1]),
                        float(normal[2]),
                        1,
                        True,
                    ]
                )
        else:
            sums = sums_by_index[collapsed_index]
            sums[0] += position_x
            sums[1] += position_y
            sums[2] += position_z
            sums[6] += 1
            if normal is not None:
                normal_x = float(normal[0])
                normal_y = float(normal[1])
                normal_z = float(normal[2])
                if sums[7]:
                    sums[3] += normal_x
                    sums[4] += normal_y
                    sums[5] += normal_z
                else:
                    sums[3] = normal_x
                    sums[4] = normal_y
                    sums[5] = normal_z
                    sums[7] = True

        remap[source_index] = collapsed_index
        weights = vertex_weights[source_index] if source_index < vertex_weight_count else None
        if not weights:
            continue
        merged = collapsed_weights[collapsed_index]
        for bone_name, weight in weights.items():
            merged[bone_name] = merged.get(bone_name, 0.0) + float(weight)

    for collapsed_index, sums in enumerate(sums_by_index):
        count = max(int(sums[6]), 1)
        collapsed_vertices[collapsed_index]["position"] = (
            sums[0] / count,
            sums[1] / count,
            sums[2] / count,
        )
        avg_normal = None
        if sums[7]:
            normal_length_squared = (sums[3] * sums[3]) + (sums[4] * sums[4]) + (sums[5] * sums[5])
            if normal_length_squared > 0.0:
                normal_scale = normal_length_squared ** -0.5
                avg_normal = (
                    sums[3] * normal_scale,
                    sums[4] * normal_scale,
                    sums[5] * normal_scale,
                )
        collapsed_vertices[collapsed_index]["normal"] = avg_normal
        collapsed_weights[collapsed_index] = _limit_weight_dict(collapsed_weights[collapsed_index])

    for face in faces or []:
        if face is None or len(face) < 3:
            continue
        try:
            remapped = tuple(remap[int(index)] for index in face[:3])
        except Exception:
            continue
        if len(set(remapped)) < 3:
            continue
        collapsed_faces.append(remapped)

    return collapsed_vertices, collapsed_weights, collapsed_faces, representative_original_indices


def _create_mesh_object_from_filemesh(parts_collection, part_name, mesh_data, entry, local_cf=None):
    vertices, faces = _build_transformed_filemesh_geometry(
        mesh_data,
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=entry.get("mesh_size") or entry.get("part_size"),
        local_cf=local_cf,
    )
    if not vertices:
        return None

    mesh = bpy.data.meshes.new(get_unique_name(f"mesh_{part_name or 'Part'}"))
    mesh.from_pydata([tuple(vertex["position"]) for vertex in vertices], [], faces)

    uvs = mesh_data.get("uvs") or []
    if uvs:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for polygon in mesh.polygons:
            for loop_index, vertex_index in zip(range(polygon.loop_start, polygon.loop_start + polygon.loop_total), polygon.vertices):
                if 0 <= vertex_index < len(uvs) and uvs[vertex_index] is not None:
                    uv = uvs[vertex_index]
                    uv_layer.data[loop_index].uv = (float(uv[0]), float(uv[1]))

    mesh.update()

    object_name = part_name or get_unique_name("Part")
    mesh_obj = bpy.data.objects.new(object_name, mesh)
    mesh_obj["RBXSynthesizedPart"] = True
    _configure_synthesized_mesh_surface(mesh_obj, vertices)
    _configure_synthesized_mesh_display(mesh_obj, entry)
    parts_collection.objects.link(mesh_obj)
    return mesh_obj


def _replace_object_with_synthesized_filemesh(parts_collection, mesh_obj, mesh_data, entry):
    if mesh_obj is None:
        return None

    object_name = mesh_obj.name
    _remove_object_and_data(mesh_obj)
    replacement = _create_mesh_object_from_filemesh(parts_collection, object_name, mesh_data, entry)
    if replacement is not None:
        print(f"[RigCreate] Replaced imported mesh '{object_name}' with synthesized FileMesh geometry")
    return replacement


def _binding_quality_score(binding):
    if not binding:
        return 0.0

    mode = binding.get("mode")
    if mode == "uv-map":
        return float(binding.get("uv_link_coverage", 0.0) or 0.0)
    if mode == "vertex-map":
        return float(binding.get("vertex_link_coverage", 0.0) or 0.0)
    if mode == "index":
        return 1.0
    return 0.0


def _collect_intentionally_missing_wrap_target_parts(meta_loaded, parts_collection):
    missing = set()

    for entry in _iter_part_aux_entries(meta_loaded):
        if not isinstance(entry, dict):
            continue

        part_name = _strip_suffix(entry.get("name") or "")
        if not part_name:
            continue
        if not _get_wrap_target_metadata(entry):
            continue
        if _find_parts_object(parts_collection, part_name) is not None:
            continue

        missing.add(part_name.lower())

    if missing:
        print(f"[RigCreate] Wrap target body parts intentionally absent from import: {sorted(missing)}")

    return missing


def _build_wrap_target_snapshot(meta_loaded, parts_collection):
    snapshot_vertices = []
    snapshot_faces = []
    snapshot_sources = []

    for entry in _iter_part_aux_entries(meta_loaded):
        if not isinstance(entry, dict):
            continue

        wrap_target_metadata = _get_wrap_target_metadata(entry)
        if not wrap_target_metadata:
            continue

        mesh_obj = _find_parts_object(parts_collection, entry.get("name"))
        if mesh_obj is not None and mesh_obj.type != "MESH":
            continue

        cage_mesh_id = wrap_target_metadata.get("cage_mesh_id")
        if not cage_mesh_id:
            continue

        try:
            cage_mesh_data = fetch_and_parse_filemesh(cage_mesh_id)
        except Exception as exc:
            source_name = mesh_obj.name if mesh_obj is not None else (entry.get("name") or "unknown")
            print(f"[RigCreate] Wrap target cage fetch failed for '{source_name}': {exc}")
            continue

        cage_local_matrix = _compose_wrap_geometry_matrix(
            origin=wrap_target_metadata.get("cage_origin"),
            import_origin=wrap_target_metadata.get("import_origin"),
        )

        cage_vertices, cage_faces = _build_transformed_filemesh_geometry(
            cage_mesh_data,
            part_cf=entry.get("part_cf"),
            part_size=entry.get("part_size"),
            mesh_size=entry.get("mesh_size") or entry.get("part_size"),
            local_cf=cage_local_matrix,
        )
        if not cage_vertices:
            continue

        vertex_offset = len(snapshot_vertices)
        snapshot_vertices.extend(cage_vertices)
        snapshot_faces.extend(
            (face[0] + vertex_offset, face[1] + vertex_offset, face[2] + vertex_offset)
            for face in cage_faces
        )
        source_name = mesh_obj.name if mesh_obj is not None else (entry.get("name") or "unknown")
        snapshot_sources.append(f"{source_name}:{len(cage_vertices)}")

    if snapshot_sources:
        print(f"[RigCreate] Built wrap target cage snapshot from {snapshot_sources}")

    return {
        "vertices": snapshot_vertices,
        "faces": snapshot_faces,
    }


def _build_wrap_solver_binding(binding, current_wrap_snapshot):
    if not current_wrap_snapshot or not current_wrap_snapshot.get("vertices"):
        return None, None

    wrap_layer_metadata = _get_wrap_layer_metadata(binding.get("entry") or {})
    if not wrap_layer_metadata:
        return None, None

    reference_mesh_id = wrap_layer_metadata.get("reference_mesh_id")
    cage_mesh_id = wrap_layer_metadata.get("cage_mesh_id")
    auto_skin = _normalize_wrap_auto_skin(wrap_layer_metadata.get("auto_skin"))
    if not reference_mesh_id or not cage_mesh_id:
        return None, "missing wrap layer cage ids"

    mesh_data = binding.get("mesh_data") or {}
    vertex_weights = mesh_data.get("vertex_weights") or []
    if not mesh_data.get("positions") or not _has_meaningful_vertex_weights(vertex_weights):
        return None, "missing source skinned mesh data"

    entry = binding["entry"]
    try:
        reference_mesh_data = fetch_and_parse_filemesh(reference_mesh_id)
        outer_cage_mesh_data = fetch_and_parse_filemesh(cage_mesh_id)
    except Exception as exc:
        return None, f"cage fetch failed: {exc}"

    mesh_size = entry.get("mesh_size") or entry.get("part_size")
    reference_local_matrix = _compose_wrap_geometry_matrix(
        origin=wrap_layer_metadata.get("reference_origin"),
        import_origin=wrap_layer_metadata.get("import_origin"),
        bind_offset=wrap_layer_metadata.get("bind_offset"),
    )
    cage_local_matrix = _compose_wrap_geometry_matrix(
        origin=wrap_layer_metadata.get("cage_origin"),
        import_origin=wrap_layer_metadata.get("import_origin"),
        bind_offset=wrap_layer_metadata.get("bind_offset"),
    )
    source_local_matrix = Matrix.Identity(4)

    reference_vertices, reference_faces = _build_transformed_filemesh_geometry(
        reference_mesh_data,
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=mesh_size,
        local_cf=reference_local_matrix,
    )
    outer_cage_vertices = _build_transformed_filemesh_vertices(
        outer_cage_mesh_data,
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=mesh_size,
        local_cf=cage_local_matrix,
    )
    source_mesh_vertices = _build_transformed_filemesh_vertices(
        mesh_data,
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=mesh_size,
        local_cf=source_local_matrix,
    )
    if not reference_vertices or not outer_cage_vertices or not source_mesh_vertices:
        return None, "incomplete cage geometry"

    solved = solve_two_stage_cage_deformation(
        build_mesh_vertices(
            [vertex["position"] for vertex in reference_vertices],
            normals=[vertex.get("normal") for vertex in reference_vertices],
            uvs=[vertex.get("uv") for vertex in reference_vertices],
        ),
        build_mesh_vertices(
            [vertex["position"] for vertex in current_wrap_snapshot["vertices"]],
            normals=[vertex.get("normal") for vertex in current_wrap_snapshot["vertices"]],
            uvs=[vertex.get("uv") for vertex in current_wrap_snapshot["vertices"]],
        ),
        build_mesh_vertices(
            [vertex["position"] for vertex in outer_cage_vertices],
            normals=[vertex.get("normal") for vertex in outer_cage_vertices],
            uvs=[vertex.get("uv") for vertex in outer_cage_vertices],
        ),
        build_mesh_vertices(
            [vertex["position"] for vertex in source_mesh_vertices],
            normals=[vertex.get("normal") for vertex in source_mesh_vertices],
            uvs=[vertex.get("uv") for vertex in source_mesh_vertices],
        ),
        reference_inner_faces=reference_faces,
        current_inner_faces=current_wrap_snapshot.get("faces"),
    )
    if not solved:
        return None, "insufficient cage links"

    predicted_mesh_positions = [Vector(position) for position in solved.get("predicted_mesh_positions") or []]
    if len(predicted_mesh_positions) != len(vertex_weights):
        return None, "predicted mesh vertex count mismatch"

    predicted_vertices = []
    for vertex_index, vertex in enumerate(source_mesh_vertices):
        predicted_vertices.append(
            {
                "index": vertex.get("index", vertex_index),
                "position": predicted_mesh_positions[vertex_index].copy(),
                "normal": vertex.get("normal"),
                "uv": vertex.get("uv"),
            }
        )

    alignment = _estimate_index_alignment(binding["object"], predicted_mesh_positions)
    max_dimension = max(max(binding["object"].dimensions), 1.0)
    max_distance_limit = max_dimension * 0.0025
    avg_distance_limit = max_dimension * 0.001

    result = {
        "mesh_data": mesh_data,
        "wrap_solver": solved,
        "predicted_mesh_positions": predicted_mesh_positions,
        "wrap_auto_skin": auto_skin,
    }
    if alignment:
        result["index_alignment"] = alignment

    if (
        len(predicted_mesh_positions) == len(binding["object"].data.vertices)
        and alignment
        and alignment["max"] <= max_distance_limit
        and alignment["avg"] <= avg_distance_limit
    ):
        result["mode"] = "index"
        return result, (
            "cage index "
            f"(auto_skin={auto_skin or 'unknown'}, links={solved['inner_link_count']}, inner={solved.get('inner_solver_mode')}, "
            f"outer={solved.get('outer_solver_mode')}, avg={alignment['avg']:.6f}, max={alignment['max']:.6f})"
        )

    result["mode"] = "position"
    result["position_samples"] = _build_position_samples_from_vertices(predicted_vertices, vertex_weights)
    if alignment:
        return result, (
            "cage position "
            f"(auto_skin={auto_skin or 'unknown'}, links={solved['inner_link_count']}, inner={solved.get('inner_solver_mode')}, "
            f"outer={solved.get('outer_solver_mode')}, avg={alignment['avg']:.6f}, max={alignment['max']:.6f})"
        )
    return result, (
        "cage position "
        f"(auto_skin={auto_skin or 'unknown'}, links={solved['inner_link_count']}, inner={solved.get('inner_solver_mode')}, "
        f"outer={solved.get('outer_solver_mode')})"
    )


def _estimate_index_alignment(mesh_obj, filemesh_world_positions):
    if not filemesh_world_positions:
        return None

    vertex_count = len(mesh_obj.data.vertices)
    if vertex_count != len(filemesh_world_positions):
        return None

    sample_count = min(vertex_count, 64)
    if sample_count <= 0:
        return None

    if sample_count == vertex_count:
        sample_indices = range(vertex_count)
    else:
        step = max((vertex_count - 1) / max(sample_count - 1, 1), 1.0)
        sample_indices = {min(int(round(index * step)), vertex_count - 1) for index in range(sample_count)}

    max_distance = 0.0
    total_distance = 0.0
    compared = 0
    for vertex_index in sample_indices:
        mesh_world_position = mesh_obj.matrix_world @ mesh_obj.data.vertices[vertex_index].co
        distance = (mesh_world_position - filemesh_world_positions[vertex_index]).length
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


def _select_bind_mesh_data_for_target_mesh(mesh_data, mesh_obj):
    if not isinstance(mesh_data, dict):
        return mesh_data

    lod_offsets = mesh_data.get("lod_offsets") or []
    all_faces = mesh_data.get("faces") or []
    if len(lod_offsets) <= 1 or not all_faces:
        return mesh_data

    target_face_count = _mesh_face_count(mesh_obj)
    if target_face_count <= 0:
        return mesh_data

    candidates = []
    for index, start in enumerate(lod_offsets):
        end = lod_offsets[index + 1] if index + 1 < len(lod_offsets) else len(all_faces)
        start = max(0, min(int(start), len(all_faces)))
        end = max(start, min(int(end), len(all_faces)))
        face_count = end - start
        if face_count <= 0:
            continue
        candidates.append(
            {
                "index": index,
                "start": start,
                "end": end,
                "face_count": face_count,
                "high_quality": index < int(mesh_data.get("num_high_quality_lods") or 0),
            }
        )

    if not candidates:
        return mesh_data

    best = min(
        candidates,
        key=lambda item: (
            abs(item["face_count"] - target_face_count),
            abs(item["face_count"] - target_face_count) / max(target_face_count, 1),
            item["index"],
        ),
    )

    selected_faces = list(all_faces[best["start"] : best["end"]])
    used_vertex_indices = sorted(
        {
            int(vertex_index)
            for face in selected_faces
            for vertex_index in face[:3]
            if vertex_index is not None
        }
    )
    if used_vertex_indices:
        remap = {source_index: remapped_index for remapped_index, source_index in enumerate(used_vertex_indices)}
        remapped_faces = [
            tuple(remap[int(vertex_index)] for vertex_index in face[:3])
            for face in selected_faces
        ]

        def _select_vertex_array(values, default=None):
            if not isinstance(values, list):
                return values if values is not None else default
            return [values[index] if 0 <= index < len(values) else default for index in used_vertex_indices]

        selected_mesh_data = dict(mesh_data)
        selected_mesh_data["positions"] = _select_vertex_array(mesh_data.get("positions"), default=None)
        selected_mesh_data["normals"] = _select_vertex_array(mesh_data.get("normals"), default=None)
        selected_mesh_data["uvs"] = _select_vertex_array(mesh_data.get("uvs"), default=None)
        selected_mesh_data["vertex_weights"] = _select_vertex_array(mesh_data.get("vertex_weights"), default={})
        selected_mesh_data["faces"] = remapped_faces
    else:
        selected_mesh_data = dict(mesh_data)
        selected_mesh_data["faces"] = selected_faces

    selected_mesh_data["lod_selection"] = {
        "index": best["index"],
        "start": best["start"],
        "end": best["end"],
        "face_count": best["face_count"],
        "target_face_count": target_face_count,
        "high_quality": best["high_quality"],
        "vertex_count": len(selected_mesh_data.get("positions") or []),
    }
    return selected_mesh_data


def _log_lod_bind_selection(mesh_obj, raw_mesh_data, binding_mesh_data):
    lod_offsets = raw_mesh_data.get("lod_offsets") or []
    if not lod_offsets:
        return

    selection = binding_mesh_data.get("lod_selection") or {}
    print(
        f"[RigCreate] LOD bind selection for '{mesh_obj.name}': "
        f"lod_type={raw_mesh_data.get('lod_type')}, "
        f"hq_lods={raw_mesh_data.get('num_high_quality_lods', 0)}, "
        f"lod_offsets={lod_offsets}, "
        f"selected={selection.get('index', 0)}, "
        f"faces={selection.get('face_count', len(binding_mesh_data.get('faces') or []))}, "
        f"target_faces={selection.get('target_face_count', _mesh_face_count(mesh_obj))}, "
        f"vertices={selection.get('vertex_count', len(binding_mesh_data.get('positions') or []))}"
    )


def _build_direct_skin_binding(binding, prefer_source_uv=False):
    mesh_obj = binding["object"]
    mesh_data = binding["mesh_data"]
    vertex_weights = mesh_data.get("vertex_weights") or []
    if not _has_meaningful_vertex_weights(vertex_weights):
        return None, None

    direct_binding = {
        "mesh_data": mesh_data,
    }
    target_faces = _build_mesh_object_faces(mesh_obj)

    filemesh_world_positions = _compute_filemesh_world_positions(binding)
    if filemesh_world_positions:
        direct_binding["filemesh_world_positions"] = filemesh_world_positions

    source_uv_binding, source_uv_message = _build_source_uv_binding(binding, target_faces=target_faces)
    if source_uv_binding and source_uv_binding.get("uv_link_coverage", 0.0) >= 0.95:
        direct_binding.update(source_uv_binding)
        return direct_binding, source_uv_message

    source_topology_binding, source_topology_message = _build_source_topology_binding(binding, target_faces=target_faces)
    if source_topology_binding:
        direct_binding.update(source_topology_binding)
        return direct_binding, source_topology_message

    position_samples = _build_position_samples(binding)
    if len(vertex_weights) == len(mesh_obj.data.vertices):
        alignment = _estimate_index_alignment(mesh_obj, filemesh_world_positions)
        if alignment:
            direct_binding["index_alignment"] = alignment

        max_dimension = max(max(mesh_obj.dimensions), 1.0)
        max_distance_limit = max_dimension * 0.0025
        avg_distance_limit = max_dimension * 0.001

        if alignment and alignment["max"] <= max_distance_limit and alignment["avg"] <= avg_distance_limit:
            direct_binding["mode"] = "index"
            return direct_binding, (
                f"index (avg={alignment['avg']:.6f}, max={alignment['max']:.6f}, samples={alignment['count']})"
            )
        if position_samples:
            direct_binding["mode"] = "position"
            direct_binding["position_samples"] = position_samples
            if alignment:
                return direct_binding, (
                    f"position (index mismatch avg={alignment['avg']:.6f}, max={alignment['max']:.6f})"
                )
            return direct_binding, "position"

        direct_binding["mode"] = "index"
        return direct_binding, "index-only"

    if position_samples:
        direct_binding["mode"] = "position"
        direct_binding["position_samples"] = position_samples
        return direct_binding, "position"

    if source_uv_binding:
        direct_binding.update(source_uv_binding)
        return direct_binding, source_uv_message

    return None, None


def _build_source_uv_binding(binding, target_faces=None):
    mesh_obj = binding["object"]
    mesh_data = binding["mesh_data"]
    vertex_weights = mesh_data.get("vertex_weights") or []
    if not _has_meaningful_vertex_weights(vertex_weights):
        return None, None

    source_vertices = build_mesh_vertices(
        mesh_data.get("positions") or [],
        normals=mesh_data.get("normals") or [],
        uvs=mesh_data.get("uvs") or [],
    )
    target_vertices = _build_mesh_object_vertices(mesh_obj)
    if not source_vertices or not target_vertices:
        return None, None
    if target_faces is None:
        target_faces = _build_mesh_object_faces(mesh_obj)

    links = link_vertices_by_uv(
        source_vertices,
        target_vertices,
        source_faces=mesh_data.get("faces") or [],
        target_faces=target_faces,
        use_position_score=False,
    )
    if not links:
        return None, None

    coverage = len(links) / max(len(target_vertices), 1)
    binding_data = {
        "mesh_data": mesh_data,
        "mode": "uv-map",
        "vertex_links": links,
        "uv_link_coverage": coverage,
        "uv_link_count": len(links),
    }
    return binding_data, f"source uv (links={len(links)}, coverage={coverage:.3f})"


def _build_source_topology_binding(binding, target_faces=None):
    mesh_obj = binding["object"]
    entry = binding["entry"]
    mesh_data = binding["mesh_data"]
    vertex_weights = mesh_data.get("vertex_weights") or []
    if not _has_meaningful_vertex_weights(vertex_weights):
        return None, None

    source_vertices = _build_transformed_filemesh_vertices(
        mesh_data,
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=entry.get("mesh_size") or entry.get("part_size"),
    )
    source_faces = mesh_data.get("faces") or []
    target_vertices = _build_mesh_object_vertices(mesh_obj, world_space=True)
    if not source_vertices or not target_vertices:
        return None, None

    if target_faces is None:
        target_faces = _build_mesh_object_faces(mesh_obj)
    reject_notes = []
    max_dimension = max(max(mesh_obj.dimensions), 1.0)
    max_distance_limit = max_dimension * 0.0625
    avg_distance_limit = max_dimension * 0.025
    topology_pair_budget = 2_000_000

    if len(source_vertices) * len(target_vertices) > topology_pair_budget:
        print(
            f"[RigCreate] Triangulated vertex map skipped for '{mesh_obj.name}' "
            f"(source_verts={len(source_vertices)}, target_verts={len(target_vertices)}, "
            f"pair_budget={topology_pair_budget})"
        )
        return None, None

    # Build a position → [original_index, ...] map so we can resolve each
    # collapsed vertex back to the best individual original vertex (by normal
    # similarity).  Using the representative-original or blended collapsed
    # weights introduces cross-zone contamination when seam vertices from
    # different bone regions collapse into one bucket.
    position_to_original_indices = {}
    for orig_index, vertex in enumerate(source_vertices):
        pos = vertex.get("position")
        if pos is None:
            continue
        for prec in (6, 5, 4, 3, 2):
            k = _round_vector_key(pos, precision=prec)
            position_to_original_indices.setdefault((prec, k), []).append(orig_index)

    for precision in (6, 5, 4, 3, 2):
        collapsed_vertices, collapsed_weights, collapsed_faces, representative_original_indices = _collapse_weighted_source_geometry(
            source_vertices,
            vertex_weights,
            source_faces,
            precision=precision,
        )

        links = link_targets_to_sources_by_position(
            collapsed_vertices,
            target_vertices,
            precision=precision,
            source_faces=collapsed_faces,
            target_faces=target_faces,
        )
        if not links or len(links) < len(target_vertices):
            reject_notes.append(
                f"p{precision}:links={len(links) if links else 0}/{len(target_vertices)},collapsed={len(collapsed_vertices)}"
            )
            continue

        coverage = len(links) / max(len(target_vertices), 1)
        if coverage < 1.0:
            reject_notes.append(
                f"p{precision}:coverage={coverage:.3f},collapsed={len(collapsed_vertices)}"
            )
            continue

        total_distance = 0.0
        max_distance = 0.0
        for source_index, target_index in links:
            source_position = Vector(collapsed_vertices[source_index]["position"])
            target_position = Vector(target_vertices[target_index]["position"])
            distance = (source_position - target_position).length
            total_distance += distance
            max_distance = max(max_distance, distance)

        avg_distance = total_distance / max(len(links), 1)
        if max_distance > max_distance_limit or avg_distance > avg_distance_limit:
            reject_notes.append(
                f"p{precision}:dist(avg={avg_distance:.6f},max={max_distance:.6f}),collapsed={len(collapsed_vertices)}"
            )
            continue

        # For each collapsed vertex, resolve to the best individual original
        # vertex (most similar normal to the collapsed average normal).  Using
        # original per-vertex weights avoids the blending artifact where
        # merging bone-boundary seam vertices gives incorrect LowerTorso/
        # UpperTorso contamination.
        resolved_weights = []
        for collapsed_index, collapsed_vertex in enumerate(collapsed_vertices):
            collapsed_normal = collapsed_vertex.get("normal")
            collapsed_pos = collapsed_vertex.get("position")
            # gather original candidates at this collapsed position
            cand_key = _round_vector_key(collapsed_pos, precision=precision) if collapsed_pos else None
            cand_indices = position_to_original_indices.get((precision, cand_key), [])
            if not cand_indices:
                cand_indices = [representative_original_indices[collapsed_index]]
            best_orig = cand_indices[0]
            if collapsed_normal is not None and len(cand_indices) > 1:
                cn = Vector(collapsed_normal)
                best_dot = -2.0
                for orig_index in cand_indices:
                    on = source_vertices[orig_index].get("normal")
                    if on is None:
                        continue
                    dot = cn.dot(Vector(on))
                    if dot > best_dot:
                        best_dot = dot
                        best_orig = orig_index
            orig_w = vertex_weights[best_orig] if best_orig < len(vertex_weights) else {}
            resolved_weights.append(_limit_weight_dict(orig_w) if orig_w else {})

        best_result = {
            "mesh_data": mesh_data,
            "mode": "vertex-map",
            "vertex_links": links,
            "vertex_link_coverage": coverage,
            "vertex_link_count": len(links),
            "binding_vertex_weights": resolved_weights,
        }
        return best_result, (
            f"triangulated vertex map (links={len(links)}, coverage={coverage:.3f}, "
            f"precision={precision}, collapsed={len(collapsed_vertices)}->{len(target_vertices)}, "
            f"avg={avg_distance:.6f}, max={max_distance:.6f})"
        )

    if reject_notes:
        print(
            f"[RigCreate] Triangulated vertex map rejected for '{mesh_obj.name}' "
            f"({'; '.join(reject_notes)})"
        )

    return None, None


def _prepare_skinned_mesh_bindings(meta_loaded, parts_collection):
    rig_names = set(_iter_rig_node_names(meta_loaded.get("rig") or {}))
    part_to_bone_map = _build_part_to_bone_map(meta_loaded.get("rig") or {})
    bindings = {}
    wrap_target_snapshot = _build_wrap_target_snapshot(meta_loaded, parts_collection)

    for entry in _iter_part_aux_entries(meta_loaded):
        if not isinstance(entry, dict):
            continue
        mesh_id = entry.get("mesh_id")
        if not mesh_id:
            continue
        mesh_class = entry.get("mesh_class")
        if mesh_class not in (None, "", "MeshPart"):
            continue

        mesh_obj = _find_parts_object(parts_collection, entry.get("name"))
        if mesh_obj is None or mesh_obj.type != "MESH" or mesh_obj.data is None:
            continue

        wrap_layer_metadata = _get_wrap_layer_metadata(entry)
        wrap_target_metadata = _get_wrap_target_metadata(entry)
        auto_skin = _normalize_wrap_auto_skin(wrap_layer_metadata.get("auto_skin")) if wrap_layer_metadata else None

        binding = {
            "object": mesh_obj,
            "entry": entry,
            "mesh_data": {},
            "part_to_bone_map": part_to_bone_map,
        }

        direct_binding = None
        direct_mode_message = None
        try:
            mesh_data = fetch_and_parse_filemesh(mesh_id)
        except Exception as exc:
            if not wrap_layer_metadata:
                print(f"[RigCreate] Skipping skin bind for '{mesh_obj.name}': {exc}")
                continue
            print(f"[RigCreate] Wrap layer '{mesh_obj.name}' has no deterministic FileMesh bind: {exc}")
        else:
            binding_mesh_data = _select_bind_mesh_data_for_target_mesh(mesh_data, mesh_obj)
            binding["mesh_data"] = binding_mesh_data
            vertex_weights = binding_mesh_data.get("vertex_weights") or []
            bone_overlap = _mesh_bones_overlap_rig(
                binding_mesh_data.get("bone_names") or [],
                rig_names,
                part_to_bone_map,
            )
            has_weights = _has_meaningful_vertex_weights(vertex_weights)
            if wrap_layer_metadata:
                _log_lod_bind_selection(mesh_obj, mesh_data, binding_mesh_data)
            _log_binding_inspect(mesh_obj, binding, has_weights, bone_overlap)
            if has_weights and bone_overlap:
                direct_binding, direct_mode_message = _build_direct_skin_binding(
                    binding,
                    prefer_source_uv=bool(wrap_layer_metadata),
                )

                # Imported OBJ meshes for wrap layers can carry the right part name
                # while still being the wrong render mesh. If the deterministic direct
                # bind quality is extremely low, replace that mesh with the exact
                # synthesized FileMesh and rebuild the binding on the replacement.
                if (
                    wrap_layer_metadata
                    and direct_binding
                    and not bool(mesh_obj.get("RBXSynthesizedPart"))
                    and direct_binding.get("mode") == "uv-map"
                    and _binding_quality_score(direct_binding) < 0.95
                ):
                    print(
                        f"[RigCreate] Low-quality wrap direct bind for '{mesh_obj.name}': "
                        f"{direct_mode_message or direct_binding.get('mode')}; synthesizing selected FileMesh geometry"
                    )
                    replacement = _replace_object_with_synthesized_filemesh(
                        parts_collection,
                        mesh_obj,
                        binding["mesh_data"],
                        entry,
                    )
                    if replacement is not None:
                        mesh_obj = replacement
                        binding["object"] = mesh_obj
                        binding["mesh_data"] = _select_bind_mesh_data_for_target_mesh(mesh_data, mesh_obj)
                        if wrap_layer_metadata:
                            _log_lod_bind_selection(mesh_obj, mesh_data, binding["mesh_data"])
                        _log_binding_inspect(mesh_obj, binding, has_weights, bone_overlap)
                        direct_binding, direct_mode_message = _build_direct_skin_binding(
                            binding,
                            prefer_source_uv=True,
                        )

        if wrap_layer_metadata:
            if auto_skin == "disabled" and direct_binding:
                direct_mode = direct_binding.get("mode")
                if direct_mode in ("uv-map", "vertex-map", "index"):
                    binding.update(direct_binding)
                    if wrap_target_metadata:
                        binding["wrap_target"] = wrap_target_metadata
                    label = direct_mode_message or direct_mode
                    _log_binding_mode(mesh_obj, binding, f"{label}, auto_skin=disabled, prefer direct")
                    bindings[mesh_obj] = binding
                    continue

            wrap_solver_binding = None
            wrap_solver_message = None
            if direct_binding:
                wrap_solver_binding, wrap_solver_message = _build_wrap_solver_binding(binding, wrap_target_snapshot)

            if wrap_solver_binding:
                binding.update(wrap_solver_binding)
                _log_binding_mode(mesh_obj, binding, wrap_solver_message)
                bindings[mesh_obj] = binding
                continue

            binding["mode"] = "wrap"
            if wrap_target_metadata:
                binding["wrap_target"] = wrap_target_metadata
            wrap_label = "wrap"
            if wrap_solver_message:
                wrap_label = f"wrap ({wrap_solver_message})"
            if direct_mode_message and not wrap_solver_message:
                wrap_label = f"wrap (no deterministic wrap bind; direct={direct_mode_message})"
            _log_binding_mode(mesh_obj, binding, wrap_label)
            bindings[mesh_obj] = binding
            continue

        if not direct_binding:
            print(
                f"[RigCreate] Skipping skin bind for '{mesh_obj.name}': no usable direct skin binding; "
                f"{_format_binding_context(binding)}"
            )
            continue

        binding.update(direct_binding)
        if wrap_target_metadata:
            binding["wrap_target"] = wrap_target_metadata
        if direct_mode_message:
            _log_binding_mode(mesh_obj, binding, direct_mode_message)

        bindings[mesh_obj] = binding

    return bindings


def _collect_face_bone_records_from_bindings(bindings):
    collected = {}
    for binding in (bindings or {}).values():
        mesh_data = binding.get("mesh_data") or {}
        face_bone_names = set(mesh_data.get("face_bone_names") or [])
        if not face_bone_names:
            continue
        for record in mesh_data.get("bones") or []:
            bone_name = record.get("name") or record.get("resolved_name")
            if bone_name in face_bone_names and bone_name not in collected:
                collected[bone_name] = (binding, record)
    return collected


def _build_filemesh_bone_world_matrix(binding, bone_record):
    entry = binding.get("entry") or {}
    rotation = bone_record.get("rotation") or ()
    translation = bone_record.get("translation") or ()
    if len(rotation) != 9 or len(translation) != 3:
        return None

    local_matrix = Matrix(
        (
            (float(rotation[0]), float(rotation[1]), float(rotation[2]), 0.0),
            (float(rotation[3]), float(rotation[4]), float(rotation[5]), 0.0),
            (float(rotation[6]), float(rotation[7]), float(rotation[8]), 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    scale = _compute_mesh_scale(
        entry.get("part_size"),
        entry.get("mesh_size") or entry.get("part_size"),
    )
    local_matrix.translation = Vector(
        (
            float(translation[0]) * scale[0],
            float(translation[1]) * scale[1],
            float(translation[2]) * scale[2],
        )
    )

    world_matrix = Matrix.Identity(4)
    part_cf = entry.get("part_cf")
    if part_cf:
        world_matrix = get_transform_to_blender() @ cf_to_mat(part_cf)
    return world_matrix @ local_matrix


def _ensure_face_deform_bones(ao, bindings):
    if not ao or getattr(ao, "type", None) != "ARMATURE":
        return []

    face_bone_records = _collect_face_bone_records_from_bindings(bindings)
    if not face_bone_records:
        return []

    edit_bones = ao.data.edit_bones
    existing_names = {bone.name for bone in edit_bones}
    pending = {
        bone_name: data
        for bone_name, data in face_bone_records.items()
        if bone_name not in existing_names
    }
    if not pending:
        return []

    created_names = []
    while pending:
        progressed = False
        for bone_name, (binding, record) in list(pending.items()):
            parent_name = None
            parent_index = record.get("parent_index")
            mesh_bones = (binding.get("mesh_data") or {}).get("bones") or []
            if isinstance(parent_index, int) and 0 <= parent_index < len(mesh_bones):
                parent_name = mesh_bones[parent_index].get("name") or mesh_bones[parent_index].get("resolved_name")

            if parent_name and parent_name in pending and parent_name not in existing_names:
                continue

            bone_matrix = _build_filemesh_bone_world_matrix(binding, record)
            if bone_matrix is None:
                del pending[bone_name]
                continue

            edit_bone = edit_bones.new(bone_name)
            head = bone_matrix.to_translation()
            tail = head + (bone_matrix.to_3x3() @ Vector((0.0, 0.02, 0.0)))
            if (tail - head).length < 0.01:
                tail = head + Vector((0.0, 0.01, 0.0))
            edit_bone.head = head
            edit_bone.tail = tail
            edit_bone.use_deform = True
            edit_bone.use_connect = False
            bone_dir = bone_matrix.to_3x3().to_4x4() @ Vector((0.0, 0.0, 1.0))
            edit_bone.align_roll(bone_dir)
            if parent_name and parent_name in edit_bones and parent_name != bone_name:
                edit_bone.parent = edit_bones[parent_name]
            elif bone_name != "Head" and "Head" in edit_bones:
                edit_bone.parent = edit_bones["Head"]

            created_names.append(bone_name)
            existing_names.add(bone_name)
            del pending[bone_name]
            progressed = True

        if not progressed:
            bone_name, (binding, record) = next(iter(pending.items()))
            bone_matrix = _build_filemesh_bone_world_matrix(binding, record)
            if bone_matrix is None:
                del pending[bone_name]
                continue
            edit_bone = edit_bones.new(bone_name)
            head = bone_matrix.to_translation()
            edit_bone.head = head
            edit_bone.tail = head + Vector((0.0, 0.01, 0.0))
            edit_bone.use_deform = True
            created_names.append(bone_name)
            existing_names.add(bone_name)
            del pending[bone_name]

    return created_names


def _mark_face_deform_bones(ao, bone_names):
    for bone_name in bone_names or []:
        bone = ao.data.bones.get(bone_name)
        if bone is None:
            continue
        bone[FACE_DEFORM_BONE_PROP] = True
        bone.use_deform = True
        bone["is_transformable"] = True


def _collect_facs_payload_from_bindings(bindings):
    payloads = []
    for binding in (bindings or {}).values():
        payload = facs_payload_from_mesh_data(binding.get("mesh_data") or {})
        if payload:
            payloads.append(payload)
    if not payloads:
        return None
    return merge_facs_payloads(payloads)


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


def _run_weight_transfer_sequence(mesh_obj, source_obj, available_bones, initial_max_distance, label, preferred_mapping=None):
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
        return [position.copy() for position in predicted_mesh_positions]

    entry = binding["entry"]
    vertices = _build_transformed_filemesh_vertices(
        binding["mesh_data"],
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=entry.get("mesh_size") or entry.get("part_size"),
    )
    return [vertex["position"].copy() for vertex in vertices]


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
    local_vertices = [tuple((local_matrix @ position)) for position in vertices_world]

    source_mesh = bpy.data.meshes.new(get_unique_name(f"__rbxskin_mesh_{mesh_obj.name}"))
    source_mesh.from_pydata(local_vertices, [], faces)
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
    # Keep backwards-compat single-UV map for callers that need it
    target_uvs = {vi: uvs[0] for vi, uvs in target_all_uvs.items() if uvs}

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

    def _uv_tie_break_score(src_idx, tgt_world, tgt_normal):
        """Lower = better.  Normal agreement is primary; position is secondary."""
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

        return (normal_cost, dist2)

    matched = 0
    uv_assigned = 0
    pos_assigned = 0
    # vertex_index → source_index for already-assigned UV-matched verts
    assigned_source = {}

    for blender_vertex in mesh_obj.data.vertices:
        tgt_idx = blender_vertex.index
        tgt_world = target_world_positions[tgt_idx]
        tgt_normal = target_world_normals[tgt_idx] if tgt_idx < len(target_world_normals) else None
        best_src = None

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
                if len(candidates) == 1:
                    best_src = candidates[0]
                else:
                    best_score = None
                    for c in candidates:
                        score = _uv_tie_break_score(c, tgt_world, tgt_normal)
                        if best_score is None or score < best_score:
                            best_score = score
                            best_src = c
                uv_assigned += 1
                assigned_source[tgt_idx] = best_src
                break  # stop once any loop UV matched

        # 2. Fallback: propagate from nearest already-UV-matched Blender neighbour
        #    (avoids using cage-predicted positions directly for seam/border verts).
        if best_src is None:
            best_dist2 = float("inf")
            for matched_tgt, matched_src in assigned_source.items():
                mp = target_world_positions[matched_tgt]
                dx = mp.x - tgt_world.x
                dy = mp.y - tgt_world.y
                dz = mp.z - tgt_world.z
                d2 = dx * dx + dy * dy + dz * dz
                if d2 < best_dist2:
                    best_dist2 = d2
                    best_src = matched_src

        # 3. Last resort: nearest cage-predicted filemesh position
        if best_src is None:
            best_dist2 = float("inf")
            for src_idx, sp in enumerate(source_positions_world):
                dx = sp.x - tgt_world.x
                dy = sp.y - tgt_world.y
                dz = sp.z - tgt_world.z
                d2 = dx * dx + dy * dy + dz * dz
                if d2 < best_dist2:
                    best_dist2 = d2
                    best_src = src_idx
            pos_assigned += 1

        if best_src is None:
            continue

        weights_src = vertex_weights[best_src] or {}
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
    print(
        f"[RigCreate] Position bind used for '{mesh_obj.name}' "
        f"(uv={uv_assigned}/{total_vertices} [{uv_method}, candidates={uv_matched_count}], "
        f"neighbor_propagate={total_vertices - uv_assigned - pos_assigned}/{total_vertices}, "
        f"pos_fallback={pos_assigned}/{total_vertices})"
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

        if binding.get("mode") in ("uv-map", "vertex-map"):
            success = _apply_uv_map_bound_weights(mesh_obj, armature_obj, binding)
        elif binding.get("mode") == "position":
            success = _apply_position_bound_weights(mesh_obj, armature_obj, binding)
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
        else:
            print(f"[RigCreate] Failed to apply layered clothing weights to '{mesh_obj.name}' (no deterministic bind)")

    return applied


def _fingerprint_position(matrix: Matrix, precision: int = 2) -> str:
    """Create a position-only fingerprint for coarse matching."""
    loc = matrix.to_translation()
    return f"{round(loc.x, precision)},{round(loc.y, precision)},{round(loc.z, precision)}"


def _build_match_context(parts_collection):
    """Precompute lookup maps for matching imported meshes to rig metadata."""
    name_index = {}
    # Position indices at multiple precision levels — use vertex centroid
    # corrected into t2b space so distances to expected positions are accurate.
    position_index_p2 = {}  # precision 2 (0.01 units)
    position_index_p1 = {}  # precision 1 (0.1 units)
    position_index_p0 = {}  # precision 0 (1 unit)

    mesh_centers = {}  # obj -> Vector (in t2b-corrected space)

    for obj in parts_collection.objects:
        if obj.type != "MESH":
            continue
        base = _strip_suffix(obj.name).lower()
        name_index.setdefault(base, []).append(obj)

        center = _mesh_center_in_t2b_space(obj)
        mesh_centers[obj] = center

        # Build a fake 4x4 from the centroid so _fingerprint_position works
        center_mat = Matrix.Translation(center)
        for prec, idx in [(2, position_index_p2), (1, position_index_p1), (0, position_index_p0)]:
            fp = _fingerprint_position(center_mat, prec)
            idx.setdefault(fp, []).append(obj)

    return {
        "name_index": name_index,
        "position_index_p2": position_index_p2,
        "position_index_p1": position_index_p1,
        "position_index_p0": position_index_p0,
        "mesh_centers": mesh_centers,
        "used": set(),
        "t2b": get_transform_to_blender(),
        "parts_collection": parts_collection,
    }


def _refresh_match_context(match_ctx):
    """Rebuild lookup indices after objects have been renamed.

    Name-based and position-based caches become stale after the two-pass
    rename flow. Recompute them while preserving the runtime state that
    create_rig accumulates around matching and constraint application.
    """
    parts_collection = match_ctx["parts_collection"]
    refreshed = _build_match_context(parts_collection)

    for key in (
        "fingerprint_object_map",
        "intentionally_missing_parts",
        "skinned_mesh_bindings",
        "pending_constraints",
    ):
        if key in match_ctx:
            refreshed[key] = match_ctx[key]

    if "used" in match_ctx:
        refreshed["used"] = match_ctx["used"]

    return refreshed


def _find_matching_part(aux_name, aux_cf, match_ctx):
    """Resolve an aux entry to a mesh.
    
    Priority order:
    1. Fingerprint object map (authoritative, from index-based matching)
    2. Name-based lookup with side + position tiebreaking (for duplicates)
    3. Position fingerprint (last resort)
    """
    used = match_ctx["used"]
    t2b = match_ctx["t2b"]
    mesh_centers = match_ctx.get("mesh_centers", {})
    base_name = _strip_suffix(aux_name or "").lower()
    intentionally_missing_parts = match_ctx.get("intentionally_missing_parts", set())

    if base_name in intentionally_missing_parts:
        return None
    
    # Pre-compute expected position if we have transform data
    expected_pos = None
    if aux_cf:
        try:
            expected_pos = (t2b @ cf_to_mat(aux_cf)).to_translation()
        except Exception:
            pass

    # Side detection from target name
    target_lower = (aux_name or "").lower()
    is_left = "left" in target_lower
    is_right = "right" in target_lower
    has_side = is_left or is_right
    expected_side_positive = None
    if has_side and expected_pos is not None and abs(expected_pos.x) >= 0.05:
        expected_side_positive = expected_pos.x > 0
    
    def _side_ok(obj):
        """Return False if mesh is on the wrong side of the rig."""
        if expected_side_positive is None:
            return True
        center = mesh_centers.get(obj)
        if center is None:
            center = _mesh_center_in_t2b_space(obj)
        return (center.x > 0) == expected_side_positive
    
    # This is the definitive mapping established during import fingerprinting.
    # Map is keyed by obj.name (which is the target bone name, possibly with
    # .001/.002 suffix for duplicates). We match by stripped base name, then
    # use position to pick the correct one when multiple candidates exist.
    fp_map = match_ctx.get("fingerprint_object_map", {})
    if aux_name and fp_map:
        # Collect ALL fp_map entries whose base name matches aux_name
        fp_candidates = []
        for obj_name, obj in fp_map.items():
            if _strip_suffix(obj_name) == aux_name and obj not in used:
                fp_candidates.append(obj)
        
        if len(fp_candidates) == 1:
            obj = fp_candidates[0]
            print(f"[_find_matching_part] FINGERPRINT HIT: '{aux_name}' -> mesh '{obj.name}'")
            return obj
        elif len(fp_candidates) > 1:
            # Multiple candidates with same base name — use position to disambiguate
            if expected_pos is not None:
                def _fp_dist(o):
                    c = mesh_centers.get(o)
                    if c is None:
                        c = _mesh_center_in_t2b_space(o)
                    return (c - expected_pos).length
                fp_candidates.sort(key=_fp_dist)
                obj = fp_candidates[0]
                print(f"[_find_matching_part] FINGERPRINT HIT (pos disambig, {len(fp_candidates)} cands): '{aux_name}' -> mesh '{obj.name}' (dist={_fp_dist(obj):.4f})")
                return obj
            else:
                # No position data — try side check
                side_ok = [o for o in fp_candidates if _side_ok(o)]
                pool = side_ok if side_ok else fp_candidates
                obj = pool[0]
                print(f"[_find_matching_part] FINGERPRINT HIT (side disambig): '{aux_name}' -> mesh '{obj.name}'")
                return obj
        else:
            # No candidates — check if they existed but were used
            has_any = any(_strip_suffix(k) == aux_name for k in fp_map.keys())
            if has_any:
                print(f"[_find_matching_part] FINGERPRINT found but all used: '{aux_name}'")
            else:
                print(f"[_find_matching_part] FINGERPRINT MISS: '{aux_name}' not in map (map has {len(fp_map)} entries)'")
    
    # Fallback: Name-based candidates (base name match, ignoring suffixes)
    # WITH SIDE CHECK + POSITION TIEBREAKING for multiple candidates
    name_index = match_ctx["name_index"]
    candidates = []
    if base_name and base_name in name_index:
        for obj in name_index[base_name]:
            if obj not in used:
                candidates.append(obj)

    if candidates:
        if len(candidates) == 1:
            obj = candidates[0]
            if not _side_ok(obj):
                print(f"[_find_matching_part] NAME MATCH '{aux_name}' -> '{obj.name}' BUT WRONG SIDE (using anyway, only candidate)")
            return obj
        # Multiple candidates — filter by side first, then distance
        side_ok_cands = [o for o in candidates if _side_ok(o)]
        pool = side_ok_cands if side_ok_cands else candidates
        if len(pool) == 1:
            print(f"[_find_matching_part] NAME+SIDE: '{aux_name}' -> '{pool[0].name}' (1 on correct side of {len(candidates)})")
            return pool[0]
        # Use vertex centroid distance to pick closest
        MAX_NAME_POS_DIST = 2.0  # generous — centroid may differ from CFrame origin
        if expected_pos is not None:
            def _pos_dist(obj):
                c = mesh_centers.get(obj)
                if c is None:
                    c = _mesh_center_in_t2b_space(obj)
                return (c - expected_pos).length
            pool.sort(key=_pos_dist)
            best_obj = pool[0]
            best_dist = _pos_dist(best_obj)
            if best_dist <= MAX_NAME_POS_DIST:
                print(f"[_find_matching_part] NAME+SIDE+POS: '{aux_name}' -> '{best_obj.name}' (dist={best_dist:.4f}, {len(candidates)} candidates)")
                return best_obj
            else:
                print(f"[_find_matching_part] NAME+SIDE+POS REJECTED: '{aux_name}' best '{best_obj.name}' too far ({best_dist:.4f})")
                return None
        # No position data — take first from side-filtered pool
        if len(pool) <= 3:
            return pool[0]
        print(f"[_find_matching_part] NAME AMBIGUOUS: '{aux_name}' has {len(pool)} candidates, no position data")
        return None

    # Position fingerprint fallback at multiple precision levels
    # Only accept unambiguous matches within a small distance threshold.
    if aux_cf:
        try:
            expected_mat = t2b @ cf_to_mat(aux_cf)
            expected_pos = expected_mat.to_translation()
            max_dist = 0.05

            for prec in [2, 1, 0]:
                fp = _fingerprint_position(expected_mat, prec)
                idx = match_ctx.get(f"position_index_p{prec}", {})
                candidates = [obj for obj in idx.get(fp, []) if obj not in used]
                if len(candidates) != 1:
                    continue

                obj = candidates[0]
                actual_pos = mesh_centers.get(obj)
                if actual_pos is None:
                    actual_pos = _mesh_center_in_t2b_space(obj)
                if (actual_pos - expected_pos).length <= max_dist:
                    if not _side_ok(obj):
                        print(f"[_find_matching_part] POS FINGERPRINT '{aux_name}' -> '{obj.name}' WRONG SIDE, skipping")
                        continue
                    return obj
        except Exception:
            pass
    return None


def _apply_fingerprint_renames(rig_def, match_ctx, allow_aux_renames=True):
    """Rename meshes by comparing position fingerprints from rig metadata.
    
    Collects all renames first, then applies via two-pass temp-name approach
    to avoid blender's auto-suffixing (.001) corrupting other objects' names.
    """
    if not allow_aux_renames:
        return

    name_index = match_ctx["name_index"]
    pending = []  # (obj, aux_name)

    def walk(node):
        aux_list = node.get("aux") or []
        aux_tf = node.get("auxTransform") or []
        for idx, aux_name in enumerate(aux_list):
            if not aux_name:
                continue
            aux_cf = aux_tf[idx] if idx < len(aux_tf) else None
            if not aux_cf:
                continue
            obj = _find_matching_part(aux_name, aux_cf, match_ctx)
            if obj and _strip_suffix(obj.name) != aux_name:
                pending.append((obj, aux_name))
        for child in node.get("children", []):
            walk(child)

    walk(rig_def)
    
    if pending:
        # Two-pass rename to avoid collisions
        for i, (obj, _) in enumerate(pending):
            obj.name = f"__rbxafr_{i}__"
        for obj, aux_name in pending:
            obj.name = aux_name
            base = _strip_suffix(obj.name).lower()
            name_index.setdefault(base, []).append(obj)


def get_unique_collection_name(basename):
    """Generate a unique collection name to avoid conflicts."""
    if basename not in bpy.data.collections:
        return basename
    i = 1
    while True:
        name = f"{basename}.{i:03d}"
        if name not in bpy.data.collections:
            return name
        i += 1


def autoname_parts(partnames, basename, objects_to_rename):
    """Rename parts to match metadata-defined names"""
    indexmatcher = re.compile(re.escape(basename) + r"_?(\d+?)1?(\.\d+)?", re.IGNORECASE)
    for object in objects_to_rename:
        match = indexmatcher.match(object.name.lower())
        if match:
            try:
                index = int(match.group(1))
                if 0 <= index - 1 < len(partnames):
                    object.name = partnames[index - 1]
                else:
                    print(
                        f"Warning: Index {index} out of range for partnames list (length: {len(partnames)})"
                    )
            except Exception as e:
                print(f"Error renaming part {object.name}: {str(e)}")


def _articulated_chain_children(rigsubdef):
    children = rigsubdef.get("children") or []
    return [
        child
        for child in children
        if (child.get("jointType") or "Motor6D") not in {"Weld", "WeldConstraint"}
    ]


def load_rigbone(ao, rigging_type, rigsubdef, parent_bone, parts_collection, match_ctx):
    """Load a single rig bone with its children"""
    amt = ao.data
    bone = amt.edit_bones.new(rigsubdef["jname"])
    joint_type = rigsubdef.get("jointType") or "Motor6D"
    original_parent_bone = rigsubdef.get("originalParentBone")

    mat = cf_to_mat(rigsubdef["transform"])
    bone["transform"] = _matrix_to_idprop(mat)
    t2b = get_transform_to_blender()
    bone_dir = (t2b @ mat).to_3x3().to_4x4() @ Vector((0, 0, 1))

    # Check if this bone is marked as a deform bone from Studio export
    is_deform_bone = rigsubdef.get("isDeformBone", False)
    if joint_type:
        # Preserve joint type for downstream serialization/diagnostics (Motor6D/Weld/WeldConstraint/Bone)
        bone["rbx_joint_type"] = joint_type
    if original_parent_bone:
        bone["rbx_original_parent"] = original_parent_bone
    if is_deform_bone:
        # Mark as a deform bone for proper animation import handling
        bone["rbx_is_deform_bone"] = True
        bone["is_transformable"] = True
        bone.use_deform = True

    if "jointtransform0" not in rigsubdef:
        # Rig root
        bone.head = (t2b @ mat).to_translation()
        bone.tail = (t2b @ mat) @ Vector((0, 0.01, 0))
        bone["transform0"] = _matrix_to_idprop(Matrix())
        bone["transform1"] = _matrix_to_idprop(Matrix())
        bone["nicetransform"] = _matrix_to_idprop(Matrix())
        bone.align_roll(bone_dir)
        bone.hide_select = True
        pre_mat = bone.matrix
        o_trans = t2b @ mat
    else:
        mat0 = cf_to_mat(rigsubdef["jointtransform0"])
        mat1 = cf_to_mat(rigsubdef["jointtransform1"])
        bone["transform0"] = _matrix_to_idprop(mat0)
        bone["transform1"] = _matrix_to_idprop(mat1)
        # Only set is_transformable for Motor6D bones if not already set for deform bones
        if not is_deform_bone:
            bone["is_transformable"] = True

        bone.parent = parent_bone
        o_trans = t2b @ (mat @ mat1)
        bone.head = o_trans.to_translation()
        real_tail = o_trans @ Vector((0, 0.25, 0))

        neutral_pos = (t2b @ mat).to_translation()
        bone.tail = real_tail
        bone.align_roll(bone_dir)

        # Store neutral matrix before any transforms (needed for all modes)
        pre_mat = bone.matrix

        # For RAW (nodes only), use original bone positions without any modifications
        # This preserves the exact bone structure from the original rig data
        if rigging_type != "RAW":
            # For other rigging types, apply "nice" transforms for better visualization/IK
            chain_children = _articulated_chain_children(rigsubdef)
            if len(chain_children) == 1:
                nextmat = cf_to_mat(chain_children[0]["transform"])
                nextmat1 = cf_to_mat(chain_children[0]["jointtransform1"])
                next_joint_pos = (t2b @ (nextmat @ nextmat1)).to_translation()

                if rigging_type == "CONNECT":  # Instantly connect
                    bone.tail = next_joint_pos
                else:
                    # For LOCAL_AXIS_EXTEND, determine best axis (calculation kept for consistency with backup.py)
                    if rigging_type == "LOCAL_AXIS_EXTEND":  # Allow non-Y too
                        invtrf = pre_mat.inverted() @ next_joint_pos
                        bestdist = abs(invtrf.y)
                        for paxis in ["x", "z"]:
                            dist = abs(getattr(invtrf, paxis))
                            if dist > bestdist:
                                bestdist = dist

                    ppd_nr_dir = real_tail - bone.head
                    ppd_nr_dir.normalize()
                    proj = ppd_nr_dir.dot(next_joint_pos - bone.head)
                    vis_world_root = ppd_nr_dir * proj
                    bone.tail = bone.head + vis_world_root

            else:
                bone.tail = bone.head + (bone.head - neutral_pos) * -2

            if (bone.tail - bone.head).length < 0.01:
                # just reset, no "nice" config can be found
                bone.tail = real_tail
                bone.align_roll(bone_dir)

    # fix roll
    bone.align_roll(bone_dir)

    post_mat = bone.matrix

    # this value stores the transform between the "proper" matrix and the "nice" matrix where bones are oriented in a more friendly way
    # For RAW mode, this should be close to identity since we're not applying nice transforms
    bone["nicetransform"] = _matrix_to_idprop(o_trans.inverted() @ post_mat)

    # link objects to bone by matching name, then fingerprint
    # Handle AUX parts (parts welded to this bone but not the primary part)
    aux_transform_list = rigsubdef.get("auxTransform") or []
    for idx, aux_name in enumerate(rigsubdef["aux"]):
        if not aux_name:
            continue

        local_cf = aux_transform_list[idx] if idx < len(aux_transform_list) else None
        found_obj = _find_matching_part(aux_name, local_cf, match_ctx)

        if found_obj:
            match_ctx["used"].add(found_obj)
            if found_obj not in match_ctx.get("skinned_mesh_bindings", {}):
                pending = match_ctx.setdefault("pending_constraints", [])
                pending.append((found_obj, bone.name))
            
    # Handle PRIMARY part (pname)
    # This was previously left to auto_constraint_parts, which guessed based on bone name.
    # Now we explicitly link 'pname' to this bone, ensuring correct constraints for duplicates.
    p_name = rigsubdef.get("pname")
    if p_name:
        # We don't have a specific transform for pname relative to bone here (it's implicit in bone head),
        # so pass None for cf. strict name matching takes priority anyway.
        found_primary = _find_matching_part(p_name, None, match_ctx)
        
        # Fallback: simple lookup in collection if _find_matching_part fails (it strips suffixes)
        if not found_primary and parts_collection:
             found_primary = parts_collection.objects.get(p_name)

        if found_primary:
            match_ctx["used"].add(found_primary)
            if found_primary not in match_ctx.get("skinned_mesh_bindings", {}):
                pending = match_ctx.setdefault("pending_constraints", [])
                pending.append((found_primary, bone.name))

    # handle child bones
    for child in rigsubdef["children"]:
        load_rigbone(ao, rigging_type, child, bone, parts_collection, match_ctx)


def _get_or_create_weld_bone_shape():
    """Get or create a simple line curve to use as custom bone shape for welds."""
    shape_name = "__WeldBoneShape"
    
    # Check if it already exists
    if shape_name in bpy.data.objects:
        return bpy.data.objects[shape_name]
    
    # Create a simple line curve
    curve_data = bpy.data.curves.new(name=shape_name, type='CURVE')
    curve_data.dimensions = '3D'
    
    # Create a simple straight line spline
    spline = curve_data.splines.new('POLY')
    spline.points.add(1)  # Start with 1 point, add 1 more = 2 points total
    spline.points[0].co = (0, 0, 0, 1)
    spline.points[1].co = (0, 1, 0, 1)  # Line along Y axis (bone direction)
    
    # Create the object
    shape_obj = bpy.data.objects.new(shape_name, curve_data)
    
    # Don't link to any collection - it's just for bone display
    shape_obj.hide_viewport = True
    shape_obj.hide_render = True
    
    return shape_obj


def _configure_weld_bones(armature_obj):
    """Configure weld bones: custom shape, lock transforms, gray color."""
    amt = armature_obj.data
    
    settings = bpy.context.scene.rbx_anim_settings
    hide_welds = getattr(settings, "rbx_hide_weld_bones", False)
    weld_shape = _get_or_create_weld_bone_shape()
    
    _safe_mode_set("POSE", armature_obj)
    
    # Blender 4.0+ uses bone collections, 3.x uses bone.hide
    try:
        collections = amt.collections
        use_collections = True
    except Exception:
        collections = None
        use_collections = False

    weld_coll = None
    if use_collections:
        weld_coll_name = "_WeldBones"
        weld_coll = collections.get(weld_coll_name)
        if weld_coll is None:
            weld_coll = collections.new(weld_coll_name)
    
    for bone in amt.bones:
        joint_type = bone.get("rbx_joint_type", "Motor6D")
        if joint_type in ("Weld", "WeldConstraint"):
            pose_bone = armature_obj.pose.bones.get(bone.name)
            if pose_bone:
                pose_bone.custom_shape = weld_shape
                pose_bone.use_custom_shape_bone_size = True
                
                pose_bone.lock_location = (True, True, True)
                pose_bone.lock_rotation = (True, True, True)
                pose_bone.lock_rotation_w = True
                pose_bone.lock_scale = (True, True, True)
                
                if hasattr(pose_bone, "color"):
                    pose_bone.color.palette = 'CUSTOM'
                    pose_bone.color.custom.normal = (0.3, 0.3, 0.3)
                    pose_bone.color.custom.select = (0.5, 0.5, 0.5)
                    pose_bone.color.custom.active = (0.6, 0.6, 0.6)
            
            if use_collections:
                weld_coll.assign(bone)
            else:
                bone.hide = hide_welds
    
    if use_collections:
        weld_coll.is_visible = not hide_welds
    
    _safe_mode_set("OBJECT", armature_obj)


def create_rig(rigging_type, rig_meta_obj_name):
    """Create a complete rig from metadata"""
    # Ensure a clean slate by deselecting everything
    if bpy.ops.object.select_all.poll():
        bpy.ops.object.select_all(action="DESELECT")

    # Ensure we are in object mode
    if bpy.context.active_object and bpy.context.mode != "OBJECT":
        _safe_mode_set("OBJECT", bpy.context.active_object)

    rig_meta_obj = get_object_by_name(rig_meta_obj_name)
    if not rig_meta_obj:
        raise ValueError(f"Rig meta object '{rig_meta_obj_name}' not found.")
        return

    # Find the master collection and parts collection for the meta object
    master_collection = find_master_collection_for_object(rig_meta_obj)
    if not master_collection:
        raise ValueError(
            f"Could not find a master collection for rig meta object '{rig_meta_obj_name}'."
        )
        return

    parts_collection = find_parts_collection_in_master(master_collection)
    if not parts_collection:
        raise ValueError(
            f"Could not find a 'Parts' collection inside '{master_collection.name}'."
        )
        return

    meta_loaded = json.loads(rig_meta_obj["RigMeta"])

    # Build a matching context so we can resolve meshes even if Roblox renames them.
    match_ctx = _build_match_context(parts_collection)
    match_ctx["intentionally_missing_parts"] = _collect_intentionally_missing_wrap_target_parts(
        meta_loaded,
        parts_collection,
    )

    # --- Deletion of old Armature ---
    # Find and delete any existing armature within this rig's master collection
    old_armature = None
    for obj in master_collection.objects:
        if obj.type == "ARMATURE":
            old_armature = obj
            break

    if old_armature:
        bpy.data.objects.remove(old_armature, do_unlink=True)

    # Set the meta object as active to provide context for subsequent operators
    bpy.context.view_layer.objects.active = rig_meta_obj

    # Load the authoritative fingerprint->object map
    # This was populated during import by _rename_parts_by_size_fingerprint
    fp_map = {}
    fp_map_json = rig_meta_obj.get("_FingerprintMap")
    if fp_map_json:
        try:
            fp_map_names = json.loads(fp_map_json)
            print(f"[RigCreate] Loading fingerprint map with {len(fp_map_names)} entries...")
            # Convert names back to object references
            for part_name, obj_name in fp_map_names.items():
                obj = parts_collection.objects.get(obj_name)
                if obj:
                    fp_map[part_name] = obj
                    print(f"[RigCreate]   '{part_name}' -> mesh '{obj.name}'")
                else:
                    print(f"[RigCreate]   WARNING: mesh '{obj_name}' not found for part '{part_name}'")
            print(f"[RigCreate] Loaded {len(fp_map)} authoritative fingerprint mappings")
        except Exception as e:
            print(f"[RigCreate] Failed to load fingerprint map: {e}")
    else:
        print("[RigCreate] WARNING: No _FingerprintMap found on meta object!")

    match_ctx["fingerprint_object_map"] = fp_map
    
    # Try to restore correct part names using fingerprinting before building constraints.
    _apply_fingerprint_renames(
        meta_loaded["rig"],
        match_ctx,
        allow_aux_renames=not bool(meta_loaded.get("partAux")),
    )

    # Rebuild lookup caches after renames so subsequent name matches and
    # skin binding preparation see the final pindex-resolved names.
    match_ctx = _refresh_match_context(match_ctx)

    skinned_mesh_bindings = _prepare_skinned_mesh_bindings(meta_loaded, parts_collection)
    match_ctx["skinned_mesh_bindings"] = skinned_mesh_bindings
    match_ctx = _refresh_match_context(match_ctx)

    bpy.ops.object.add(type="ARMATURE", enter_editmode=True, location=(0, 0, 0))
    ao = bpy.context.object
    ao.show_in_front = True

    # Move the new armature into the master collection
    for coll in ao.users_collection:
        coll.objects.unlink(ao)
    master_collection.objects.link(ao)

    # Set a unique name for the armature based on the rig name
    rig_name = meta_loaded.get("rigName", "Rig")
    ao.name = get_unique_name(f"__{rig_name}_Armature")
    amt = ao.data
    amt.name = get_unique_name(f"__{rig_name}_RigArm")
    amt.show_axes = True
    amt.show_names = True

    if bpy.context.mode != "EDIT":
        _safe_mode_set("EDIT", ao)
    # Pass the specific parts_collection to be used for constraining
    load_rigbone(ao, rigging_type, meta_loaded["rig"], None, parts_collection, match_ctx)
    created_face_deform_bones = _ensure_face_deform_bones(ao, skinned_mesh_bindings)

    if bpy.context.mode != "OBJECT":
        _safe_mode_set("OBJECT", ao)
    _mark_face_deform_bones(ao, created_face_deform_bones)
    if created_face_deform_bones:
        print(f"[RigCreate] Created {len(created_face_deform_bones)} face deform bone(s)")

    try:
        facs_payload = _collect_facs_payload_from_bindings(skinned_mesh_bindings)
    except ValueError as exc:
        facs_payload = None
        print(f"[RigCreate] Skipping facs solver payload storage: {exc}")
    if facs_payload:
        stored_payload = store_facs_payload_on_armature(ao, facs_payload)
        for bone_name in stored_payload.get("face_bone_names") or []:
            pose_bone = ao.pose.bones.get(bone_name)
            if pose_bone is not None:
                pose_bone.rotation_mode = "XYZ"
        print(
            f"[RigCreate] Stored facs solver payload for "
            f"{len(stored_payload.get('face_bone_names') or [])} face bone(s) and "
            f"{len(stored_payload.get('face_control_names') or [])} control(s)"
        )
    
    # Apply pending constraints now that we're in object mode
    from .constraints import link_object_to_bone_rigid, auto_constraint_parts
    
    # Track objects that were constrained via authoritative fingerprint mapping
    # These should NOT be touched by auto_constraint_parts
    authoritatively_constrained = set()
    
    pending = match_ctx.get("pending_constraints", [])
    print(f"[RigCreate] Applying {len(pending)} pending constraints...")
    
    for obj, bone_name in pending:
        bone = ao.data.bones.get(bone_name)
        if bone:
            link_object_to_bone_rigid(obj, ao, bone)
            authoritatively_constrained.add(obj)
            print(f"[RigCreate] AUTHORITATIVE: mesh '{obj.name}' -> bone '{bone_name}'")
        else:
            print(f"[RigCreate] WARNING: bone '{bone_name}' not found for mesh '{obj.name}'")
    
    # Auto-constraint ONLY parts that were NOT authoritatively constrained
    # This handles any parts that weren't in the fingerprint map (legacy/fallback)
    bpy.context.view_layer.update()
    skip_objects = set(authoritatively_constrained)
    skip_objects.update(skinned_mesh_bindings.keys())
    ok, msg = auto_constraint_parts(ao.name, skip_objects=skip_objects)

    # If no parts matched via fallback, retry once (but STILL skip authoritative ones)
    if ok and msg and "No matching parts found" in msg:
        # Capture the set in closure
        _skip_set = skip_objects
        _ao_name = ao.name
        def _retry_auto_constraint():
            try:
                auto_constraint_parts(_ao_name, skip_objects=_skip_set)
            except Exception:
                pass
            return None

        try:
            bpy.app.timers.register(_retry_auto_constraint, first_interval=0.0)
        except Exception:
            pass
    
    # Configure weld bones with custom display and lock them from animation
    _configure_weld_bones(ao)

    applied_skinning = _apply_skinned_mesh_bindings(ao, skinned_mesh_bindings)
    if applied_skinning:
        print(f"[RigCreate] Applied skinned mesh weights to {applied_skinning} mesh object(s)")

    return {}
