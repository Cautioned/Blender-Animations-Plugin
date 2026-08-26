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
from .cage_solver import build_mesh_vertices, link_targets_to_sources_by_position, link_vertices_by_uv, numpy_available, solve_two_stage_cage_deformation
from .filemesh import fetch_and_parse_filemesh
from ..core.utils import (
    cf_to_mat,
    get_unique_name,
    get_object_by_name,
    find_master_collection_for_object,
    find_parts_collection_in_master,
)

# Leaf helpers moved out of this module; re-imported here so the public
# surface of creation.py stays unchanged for other modules and tests.
from .mesh_surface import (
    _apply_mesh_custom_normals,
    _apply_mesh_loop_tangents,
    _apply_mesh_loop_uvs,
    _apply_mesh_vertex_colors,
    _bulk_set,
    _configure_synthesized_mesh_display,
    _configure_synthesized_mesh_surface,
    _ensure_synthesized_display_modifier,
    _iter_mesh_loop_vertex_indices,
    _mesh_loop_color_values,
    _mesh_loop_uv_values,
    _new_mesh_attribute,
    _new_mesh_color_attribute,
    _populate_mesh_geometry,
    _set_mesh_smooth_shading,
    _vertex_corner_value,
)
from .filemesh_geometry import (
    _build_transformed_filemesh_geometry,
    _build_transformed_filemesh_vertices,
    _coerce_cf_matrix,
    _compute_filemesh_mesh_size,
    _compute_filemesh_world_positions,
    _compute_mesh_scale,
    _copy_position,
    _get_effective_mesh_size,
    _normalize_vector,
    _normalize_wrap_auto_skin,
)
from .part_matching import (
    _apply_fingerprint_renames,
    _build_match_context,
    _entry_object_name,
    _find_matching_part,
    _find_parts_object,
    _find_parts_object_for_entry,
    _fingerprint_position,
    _get_mesh_world_center,
    _get_wrap_layer_metadata,
    _get_wrap_target_metadata,
    _match_stem,
    _mesh_center_in_t2b_space,
    _refresh_match_context,
    _strip_class_suffix,
    _strip_model_prefix,
    _strip_suffix,
    get_unique_collection_name,
)
from .skin_binding import (
    _apply_direct_source_transfer_weights,
    _apply_index_bound_weights,
    _apply_inherited_weight_transfer,
    _apply_position_bound_weights,
    _apply_rigid_bone_binding,
    _apply_skinned_mesh_bindings,
    _apply_uv_map_bound_weights,
    _apply_weight_data_transfer,
    _blend_weight_dicts,
    _build_component_centers,
    _build_mesh_object_faces,
    _build_mesh_object_vertices,
    _build_position_sample_lookup,
    _build_transfer_source_object,
    _build_vertex_component_ids,
    _canonical_triangle_face,
    _clear_child_of_constraints,
    _closest_point_on_triangle,
    _collect_vertex_group_weights,
    _compute_mesh_vertex_all_uvs,
    _compute_mesh_vertex_normal,
    _compute_mesh_vertex_uvs,
    _determine_binding_fallback_bone,
    _ensure_armature_modifier,
    _ensure_vertex_groups,
    _estimate_index_alignment,
    _find_position_candidate_indices,
    _format_binding_context,
    _get_position_transfer_vertices,
    _has_meaningful_vertex_weights,
    _index_alignment_is_tight,
    _index_alignment_limits,
    _limit_weight_dict,
    _log_binding_apply,
    _log_binding_inspect,
    _log_binding_mode,
    _map_target_components_to_source_components,
    _measure_transfer_coverage,
    _mesh_face_count,
    _pick_best_sample,
    _pick_closest_sample,
    _rebase_mesh_to_predicted_positions,
    _remove_all_vertex_groups,
    _remove_object_and_data,
    _resolve_binding_bone_name,
    _round_uv_key,
    _round_vector_key,
    _run_weight_transfer_sequence,
    _sample_match_score,
    _short_content_id,
    _sorted_triangle_topology,
)
from .primitive_shapes import (
    _BLOCK_FACE_NORMALS,
    _block_canonical_stud_uvs,
    _build_primitive_mesh_data,
    _create_batched_static_primitives,
    _create_batched_static_primitives_np,
    _face_surface_types,
    _generate_face_uvs,
    _generate_primitive_loop_uvs,
    _primitive_face_projection_axes,
    _primitive_shape_template,
    _static_primitive_part_arrays,
)


def _matrix_to_idprop(value):
    """Convert Matrix values to list-of-lists so IDProperties accept them."""
    if isinstance(value, Matrix):
        return [list(row) for row in value]
    return value


# Name/stem helpers live in part_matching.py (imported above).


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


# Binding logging, wrap metadata accessors, and part lookup helpers live in
# skin_binding.py / part_matching.py (imported above).


# Constraint/vertex-group utilities live in skin_binding.py (imported above).


# Mesh geometry/surface helpers live in mesh_surface.py (imported above).

def _iter_rig_node_names(node):
    if not isinstance(node, dict):
        return
    jname = node.get("jname")
    if jname:
        yield jname
    for child in node.get("children", []):
        yield from _iter_rig_node_names(child)


# Vector-key helpers and filemesh scale/cf coercion live in skin_binding.py /
# filemesh_geometry.py (imported above).


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


# Wrap AutoSkin normalization lives in filemesh_geometry.py (imported above).


# Transformed-filemesh vertex building lives in filemesh_geometry.py (imported above).


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
                "position": _copy_position(position),
                "position_key": _round_vector_key(position),
                "normal": normal,
                "normal_key": _round_vector_key(normal, precision=4),
                "uv": uv,
                "uv_key": _round_uv_key(uv, precision=4),
                "weights": resolved_weights,
            }
        )
    return samples


# Vertex-weight predicate lives in skin_binding.py (imported above).


def _build_position_samples(binding):
    from . import avatar_scale  # noqa: PLC0415

    entry = binding["entry"]
    mesh_data = binding["mesh_data"]
    vertices = _build_transformed_filemesh_vertices(
        mesh_data,
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=_get_effective_mesh_size(entry, mesh_data),
        limb_scale=avatar_scale.entry_limb_scale(entry),
    )
    if not vertices:
        return None
    return _build_position_samples_from_vertices(vertices, mesh_data.get("vertex_weights") or [])


def _is_wrap_binding(binding):
    return bool(binding.get("wrap_solver") or _get_wrap_layer_metadata(binding.get("entry") or {}))


# Mesh vertex/UV analysis helpers live in skin_binding.py (imported above).


def _build_wrap_topology_index_binding(binding, target_faces=None, index_alignment=None):
    if not _get_wrap_layer_metadata(binding.get("entry") or {}):
        return None, None

    mesh_obj = binding["object"]
    mesh_data = binding["mesh_data"]
    vertex_weights = mesh_data.get("vertex_weights") or []
    if not _has_meaningful_vertex_weights(vertex_weights):
        return None, None

    target_vertex_count = len(mesh_obj.data.vertices) if mesh_obj and mesh_obj.data is not None else 0
    if target_vertex_count <= 0 or len(vertex_weights) != target_vertex_count:
        return None, None

    source_faces = mesh_data.get("faces") or []
    if target_faces is None:
        target_faces = _build_mesh_object_faces(mesh_obj)
    if not source_faces or not target_faces or len(source_faces) != len(target_faces):
        return None, None

    source_topology = _sorted_triangle_topology(source_faces)
    target_topology = _sorted_triangle_topology(target_faces)
    if not source_topology or len(source_topology) != len(target_topology):
        return None, None
    if source_topology != target_topology:
        return None, None

    if not _index_alignment_is_tight(mesh_obj, index_alignment):
        if index_alignment:
            max_distance_limit, avg_distance_limit = _index_alignment_limits(mesh_obj)
            print(
                f"[RigCreate] Wrap topology index rejected for '{mesh_obj.name}' "
                f"(avg={index_alignment['avg']:.6f}>{avg_distance_limit:.6f} "
                f"or max={index_alignment['max']:.6f}>{max_distance_limit:.6f})"
            )
        return None, None

    return {
        "mesh_data": mesh_data,
        "mode": "index",
        "topology_index_face_count": len(target_topology),
    }, (
        f"wrap topology index (faces={len(target_topology)}, vertices={target_vertex_count}, "
        f"avg={index_alignment['avg']:.6f}, max={index_alignment['max']:.6f})"
    )


# Sample scoring/candidate lookup live in skin_binding.py (imported above).


# Filemesh world-position/geometry builders live in filemesh_geometry.py
# (imported above).


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


# Primitive shape generation and UV helpers live in primitive_shapes.py
# (imported above).

def _get_filemesh_native_transform(entry, mesh_data, limb_scale=None):
    """Return the object transform for unbaked FileMesh geometry."""
    from . import avatar_scale  # noqa: PLC0415

    limb_scale = limb_scale or avatar_scale.entry_limb_scale(entry)
    scale = _compute_mesh_scale(
        entry.get("part_size"), _get_effective_mesh_size(entry, mesh_data)
    )
    scale = [scale[index] * limb_scale[index] for index in range(3)]
    return (
        get_transform_to_blender()
        @ cf_to_mat(entry.get("part_cf"))
        @ Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))
    )


def _create_mesh_object_from_filemesh(parts_collection, part_name, mesh_data, entry, local_cf=None,
                                      lod_index=0, native_transform=False, native_object_transform=False, skip_custom_normals=False):
    from . import avatar_scale  # noqa: PLC0415

    limb_scale = avatar_scale.entry_limb_scale(entry)
    if any(abs(component - 1.0) > 1e-4 for component in limb_scale):
        print(
            f"[RigCreate] HD limb scale for '{part_name}': "
            f"({limb_scale[0]:.3f}, {limb_scale[1]:.3f}, {limb_scale[2]:.3f})"
        )
    # Build the render mesh from LOD0 only — FileMesh faces span every LOD
    # concatenated, and using them all stacks LOD shells and mismatches the
    # (LOD-sliced) skin binding data. Embedded union (CSG) meshes have no LOD
    # table — slicing those would mangle the face buffer, so leave them be.
    if not (isinstance(mesh_data, dict) and mesh_data.get("embedded_union")):
        mesh_data = _slice_mesh_data_to_lod(mesh_data, lod_index)
    native_matrix = None
    native_surface = False
    if native_transform:
        positions = mesh_data.get("positions") or []
        native_matrix = _get_filemesh_native_transform(entry, mesh_data, limb_scale)
        vertices = positions
        faces = mesh_data.get("faces") or []
        native_surface = True
    else:
        vertices, faces = _build_transformed_filemesh_geometry(
            mesh_data, part_cf=entry.get("part_cf"), part_size=entry.get("part_size"),
            mesh_size=_get_effective_mesh_size(entry, mesh_data), local_cf=local_cf, limb_scale=limb_scale,
        )
    if not vertices:
        return None

    # Classic clothing keeps the mesh's native UVs; the composite texture is
    # baked per limb group in clothing.get_limb_texture (see textures.py).

    # Blender datablock creation performs its own efficient suffixing. The
    # old helper scanned every scene object for each mesh, even though object
    # names cannot collide with mesh datablock names in the first place.
    mesh = bpy.data.meshes.new(f"mesh_{part_name or 'Part'}")
    mesh_positions = vertices if native_surface else [tuple(vertex["position"]) for vertex in vertices]
    if not _populate_mesh_geometry(mesh, mesh_positions, faces):
        bpy.data.meshes.remove(mesh)
        return None
    if native_matrix is not None and not native_object_transform:
        mesh.transform(native_matrix)

    # Wrap-layer accessories are typically all named "Handle"; name them from
    # their WrapLayer so the outliner stays readable. Association is NOT by
    # name: the object is stamped with the entry's parse index (RBXPartIdx),
    # which the bind phase uses to find it exactly.
    wrap_layer = entry.get("wrap_layer") if isinstance(entry, dict) else None
    if isinstance(wrap_layer, dict) and wrap_layer.get("name"):
        base_name = wrap_layer["name"]
    else:
        base_name = part_name or "Part"

    # Studio-style naming for multi-model scenes: when the entry carries a
    # model tag (set by the rbxm importer), name the object
    # "<Model>.model/<Part>.<Class>" so parts group under their source model
    # and never collide across models. The class suffix uses Roblox's exact
    # PascalCase class name (Part / MeshPart / UnionOperation) so it reads
    # identically to Studio's class panel. Bind-phase lookups strip the prefix
    # and class suffix, so name matching still works.
    model_tag = entry.get("model_tag") if isinstance(entry, dict) else None
    if model_tag:
        class_name = entry.get("class_name") or "Part"
        object_name = f"{model_tag}/{base_name}.{class_name}"
    else:
        object_name = base_name
    mesh_obj = bpy.data.objects.new(object_name, mesh)
    if native_matrix is not None and native_object_transform:
        mesh_obj.matrix_world = native_matrix
    mesh_obj["RBXSynthesizedPart"] = True
    if isinstance(entry, dict) and entry.get("class_name") == "Part" and not entry.get("mesh_id"):
        # Primitive Parts carry Roblox's canonical local per-face UVs.  Write
        # the built-in material layer from those local loop UVs directly: the
        # material pass must never re-project in world space, where the
        # Y-up -> Z-up axis remap would put U along the world's vertical on
        # some faces (brick courses running sideways).
        mesh["RBXPrimitiveShape"] = str(entry.get("shape", "block") or "block")
        try:
            from .textures import _BUILTIN_MATERIAL_UV_LAYER, _BUILTIN_MATERIAL_STUDS_PER_TILE

            loop_uvs = mesh_data.get("loop_uvs") or []
            if len(loop_uvs) == len(mesh.loops):
                uv_layer = mesh.uv_layers.get(_BUILTIN_MATERIAL_UV_LAYER)
                if uv_layer is None:
                    uv_layer = mesh.uv_layers.new(name=_BUILTIN_MATERIAL_UV_LAYER)
                units = 1.0 / float(_BUILTIN_MATERIAL_STUDS_PER_TILE)
                # loop_uvs are band units (0.5/stud u, 0.125/stud v).
                values = [0.0] * (2 * len(loop_uvs))
                for index, (band_u, band_v) in enumerate(loop_uvs):
                    values[2 * index] = (band_u / 0.5) * units
                    values[2 * index + 1] = (band_v / 0.125) * units
                _bulk_set(uv_layer.data, "uv", values)
        except Exception:
            pass
    if isinstance(entry, dict):
        if entry.get("idx") is not None:
            mesh_obj["RBXPartIdx"] = int(entry["idx"])
        if entry.get("inst_ref") is not None:
            mesh_obj["RBXInstRef"] = int(entry["inst_ref"])
    if native_surface:
        _set_mesh_smooth_shading(mesh)
        # Built-in materials tile through the RBXMaterialUV layer; their
        # shader never reads the mesh's native UVs.  Uploading both layers
        # for every built-in part doubles UV memory and loop traffic for
        # data nothing samples.  TextureID/SurfaceAppearance/clothing parts
        # keep the native layer.
        needs_native_uvs = True
        if isinstance(entry, dict):
            try:
                from .textures import builtin_material_texture_refs

                if builtin_material_texture_refs(entry):
                    needs_native_uvs = False
            except Exception:
                pass
        mesh_obj["RBXSynthesizedUVs"] = bool(
            _apply_mesh_loop_uvs(mesh, (), mesh_data.get("loop_uvs"))
            if needs_native_uvs
            else False
        )
        mesh_obj["RBXSynthesizedCustomNormals"] = False
        mesh_obj["RBXSynthesizedVertexColors"] = False
        mesh_obj["RBXSynthesizedTangents"] = False
    else:
        _configure_synthesized_mesh_surface(mesh_obj, vertices, mesh_data.get(
            "loop_uvs"), apply_custom_normals=not skip_custom_normals)
    _configure_synthesized_mesh_display(mesh_obj, entry)
    try:
        from .textures import apply_part_material
        if isinstance(entry, dict) and mesh_data.get("face_surface_types"):
            entry["_face_surface_types"] = mesh_data["face_surface_types"]
        apply_part_material(mesh_obj, entry)
    except Exception as exc:
        print(f"[RigCreate] Material build failed for '{object_name}': {exc}")
    parts_collection.objects.link(mesh_obj)
    return mesh_obj


def _apply_batched_mesh_vertex_colors(mesh, colors, loop_vidx):
    """Per-corner colors from a per-vertex numpy matrix (batch path)."""
    if colors is None or len(colors) == 0 or len(loop_vidx) == 0:
        return False
    try:
        import numpy as np  # bundled with Blender

        first = colors[0]
        if not np.any(np.abs(colors - first) > 1e-6):
            return False
        layer = _new_mesh_color_attribute(mesh)
        if layer is None:
            return False
        valid = (loop_vidx >= 0) & (loop_vidx < len(colors))
        clip = np.clip(loop_vidx, 0, len(colors) - 1)
        width = min(colors.shape[1], 4)
        values = np.ones((len(loop_vidx), 4), dtype=np.float32)
        values[valid, :width] = colors[clip[valid], :width]
        _bulk_set(layer.data, "color", values.ravel())
        return True
    except Exception:
        return False


def _create_batched_filemesh_instances_np(np, parts_collection, batch_name, mesh_data, entries):
    """numpy batch path: one matrix multiply per instance, bulk corner data.

    Raises on malformed payloads so the caller falls back to the per-vertex
    python builder.
    """
    src_positions = mesh_data.get("positions") or []
    src_faces = mesh_data.get("faces") or []
    if not src_positions or not src_faces:
        return None
    pos_np = np.asarray(src_positions, dtype=np.float32)
    if pos_np.ndim != 2 or pos_np.shape[0] != len(src_positions) or pos_np.shape[1] < 3:
        raise ValueError("positions must be a flat list of vec3 rows")
    face_np = np.asarray(src_faces, dtype=np.int64)
    if face_np.ndim != 2:
        raise ValueError("faces must be a flat list of index rows")
    uv_np = None
    src_uvs = mesh_data.get("uvs") or []
    if src_uvs:
        uv_np = np.asarray(src_uvs, dtype=np.float32)
        if uv_np.ndim != 2 or uv_np.shape[1] < 2:
            raise ValueError("uvs must be a flat list of vec2 rows")
    col_np = None
    src_colors = mesh_data.get("colors") or []
    if src_colors:
        col_np = np.asarray(src_colors, dtype=np.float32)
        if col_np.ndim != 2 or col_np.shape[1] < 3:
            raise ValueError("colors must be a flat list of vec3/vec4 rows")

    t2b = get_transform_to_blender()
    parts_pos = []
    parts_faces = []
    parts_vidx = []
    vertex_offset = 0
    for entry in entries:
        transform = Matrix.Identity(4)
        if entry.get("part_cf"):
            try:
                transform = t2b @ cf_to_mat(entry["part_cf"])
            except Exception:
                continue
        m3 = np.array(
            [
                [transform[0][0], transform[0][1], transform[0][2]],
                [transform[1][0], transform[1][1], transform[1][2]],
                [transform[2][0], transform[2][1], transform[2][2]],
            ],
            dtype=np.float32,
        )
        translation = np.array(
            [transform[0][3], transform[1][3], transform[2][3]], dtype=np.float32
        )
        scale = _compute_mesh_scale(
            entry.get("part_size"), _get_effective_mesh_size(entry, mesh_data)
        )
        scale_arr = np.array([float(scale[0]), float(scale[1]), float(scale[2])], dtype=np.float32)
        parts_pos.append((pos_np * scale_arr) @ m3.T + translation)
        parts_faces.append(face_np + vertex_offset)
        parts_vidx.append(face_np.ravel())
        vertex_offset += len(pos_np)
    if not parts_pos:
        return None

    positions = np.concatenate(parts_pos)
    faces = np.concatenate(parts_faces)
    loop_vidx = np.concatenate(parts_vidx)

    mesh = bpy.data.meshes.new(f"mesh_{batch_name}")
    mesh_obj = None
    try:
        if not _populate_mesh_geometry(mesh, positions, faces):
            raise ValueError("geometry upload failed")
        if uv_np is not None:
            uv_layer = mesh.uv_layers.new(name="UVMap")
            valid = loop_vidx < len(uv_np)
            uv_values = np.zeros(2 * len(loop_vidx), dtype=np.float32)
            filled = np.flatnonzero(valid)
            if filled.size:
                uv = uv_np[loop_vidx[filled]]
                uv_values[2 * filled] = uv[:, 0]
                uv_values[2 * filled + 1] = 1.0 - uv[:, 1]
            _bulk_set(uv_layer.data, "uv", uv_values)
        mesh_obj = bpy.data.objects.new(batch_name, mesh)
        mesh_obj["RBXSynthesizedPart"] = True
        mesh_obj["RBXMeshPartBatch"] = True
        mesh_obj["RBXStaticPartCount"] = len(entries)
        _set_mesh_smooth_shading(mesh)
        mesh_obj["RBXSynthesizedUVs"] = uv_np is not None
        # Batched place geometry joins the same quality path as every other
        # lazy mesh: smooth generated normals instead of a per-vertex custom
        # normal transform (the latter needs a python pass per vertex).
        mesh_obj["RBXSynthesizedCustomNormals"] = False
        mesh_obj["RBXSynthesizedVertexColors"] = _apply_batched_mesh_vertex_colors(
            mesh, col_np, loop_vidx
        )
        mesh_obj["RBXSynthesizedTangents"] = False
        mesh.update()
        try:
            from .textures import apply_part_material
            apply_part_material(mesh_obj, entries[0])
        except Exception as exc:
            print(f"[RigCreate] Batched FileMesh material failed for '{batch_name}': {exc}")
        parts_collection.objects.link(mesh_obj)
        return mesh_obj
    except Exception:
        if mesh_obj is not None and mesh_obj.name in bpy.data.objects:
            bpy.data.objects.remove(mesh_obj, do_unlink=True)
        elif mesh.name in bpy.data.meshes:
            bpy.data.meshes.remove(mesh)
        raise


def _create_batched_filemesh_instances(parts_collection, batch_name, mesh_data, entries, lod_index=0):
    """Merge visually identical, non-rigged FileMesh instances into one object."""
    if not entries:
        return None
    mesh_data = _slice_mesh_data_to_lod(mesh_data, lod_index)
    try:
        import numpy as np  # bundled with Blender

        return _create_batched_filemesh_instances_np(
            np, parts_collection, batch_name, mesh_data, entries
        )
    except Exception:
        pass
    positions = []
    faces = []
    surface_vertices = []
    for entry in entries:
        vertices, entry_faces = _build_transformed_filemesh_geometry(
            mesh_data,
            part_cf=entry.get("part_cf"),
            part_size=entry.get("part_size"),
            mesh_size=_get_effective_mesh_size(entry, mesh_data),
        )
        if not vertices:
            continue
        offset = len(positions)
        positions.extend(tuple(vertex["position"]) for vertex in vertices)
        surface_vertices.extend(vertices)
        faces.extend(tuple(offset + index for index in face) for face in entry_faces)
    if not positions or not faces:
        return None
    mesh = bpy.data.meshes.new(f"mesh_{batch_name}")
    if not _populate_mesh_geometry(mesh, positions, faces):
        bpy.data.meshes.remove(mesh)
        return None
    mesh_obj = bpy.data.objects.new(batch_name, mesh)
    mesh_obj["RBXSynthesizedPart"] = True
    mesh_obj["RBXMeshPartBatch"] = True
    mesh_obj["RBXStaticPartCount"] = len(entries)
    _configure_synthesized_mesh_surface(mesh_obj, surface_vertices)
    try:
        from .textures import apply_part_material
        apply_part_material(mesh_obj, entries[0])
    except Exception as exc:
        print(f"[RigCreate] Batched FileMesh material failed for '{batch_name}': {exc}")
    parts_collection.objects.link(mesh_obj)
    return mesh_obj


# Batched static-primitive builders live in primitive_shapes.py (imported above).

def _create_pending_filemesh_proxy(parts_collection, part_name, entry):
    """Create a cheap bounds proxy while a place FileMesh downloads."""
    cube_positions = [
        (-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5),
        (-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5),
    ]
    cube_faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    # Do not fetch the eventual mesh texture just to shade a temporary box.
    proxy_entry = dict(entry)
    proxy_entry.pop("texture_id", None)
    proxy_entry.pop("texture_instance", None)
    proxy_entry.pop("texture_instances", None)
    proxy_entry.pop("surface_appearance", None)
    proxy_entry.pop("surface_appearances", None)
    proxy_entry.pop("face_decal", None)
    proxy_data = {
        "positions": cube_positions,
        "faces": cube_faces,
        "normals": [],
        "uvs": [],
        "vertex_weights": [{} for _ in cube_positions],
    }
    mesh_obj = _create_mesh_object_from_filemesh(
        parts_collection, part_name, proxy_data, proxy_entry
    )
    if mesh_obj is not None:
        mesh_obj["RBXPendingFileMesh"] = True
        mesh_obj["RBXPendingMeshId"] = str(entry.get("mesh_id", ""))
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
    """Build per-body-part wrap target cage snapshots keyed by part name.

    Returns {part_name_lower: {"vertices": [...], "faces": [...]}} so the cage
    solver can match each clothing item against only the body part it wraps.
    """
    snapshots = {}
    snapshot_sources = []

    for entry in _iter_part_aux_entries(meta_loaded):
        if not isinstance(entry, dict):
            continue

        wrap_target_metadata = _get_wrap_target_metadata(entry)
        if not wrap_target_metadata:
            continue

        mesh_obj = _find_parts_object_for_entry(parts_collection, entry)
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

        part_name = (entry.get("name") or "").lower()
        snapshots[part_name] = {
            "vertices": cage_vertices,
            "faces": cage_faces,
        }
        source_name = mesh_obj.name if mesh_obj is not None else (entry.get("name") or "unknown")
        snapshot_sources.append(f"{source_name}:{len(cage_vertices)}")

    if snapshot_sources:
        print(f"[RigCreate] Built wrap target cage snapshot from {snapshot_sources}")

    return snapshots


def _build_wrap_solver_binding(binding, current_wrap_snapshot):
    if not current_wrap_snapshot:
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

    # WrapLayer reference/cage meshes are avatar-space cages, not cages for the
    # rigid attachment part.  The attachment is useful for skinning, but using
    # it to select one WrapTarget here makes a full-body reference cage (jacket,
    # trousers, shoes, etc.) try to link against only a torso/foot snapshot.
    # That loses most UV links and gives the RBF solver nonsense controls.
    # Always reconstruct the full avatar target cage, preserving the source
    # ordering of the per-part snapshots.
    all_vertices = []
    all_faces = []
    for snap in current_wrap_snapshot.values():
        offset = len(all_vertices)
        all_vertices.extend(snap["vertices"])
        all_faces.extend(
            (face[0] + offset, face[1] + offset, face[2] + offset)
            for face in snap.get("faces", [])
        )
    target_snapshot = {"vertices": all_vertices, "faces": all_faces}
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

    def _bbox(vertices):
        # Positions may be Vectors or plain tuples; both support indexing,
        # tuples do not support .x/.y/.z.
        xs = [v["position"][0] for v in vertices]
        ys = [v["position"][1] for v in vertices]
        zs = [v["position"][2] for v in vertices]
        return (
            f"x[{min(xs):.2f},{max(xs):.2f}] "
            f"y[{min(ys):.2f},{max(ys):.2f}] "
            f"z[{min(zs):.2f},{max(zs):.2f}]"
        )

    snapshot_vertices = target_snapshot.get("vertices") or []
    print(
        f"[RigCreate] Wrap geometry for '{binding['object'].name}': "
        f"ref={_bbox(reference_vertices)} "
        f"target_cage=all "
        f"snapshot={_bbox(snapshot_vertices) if snapshot_vertices else 'empty'} "
        f"cage={_bbox(outer_cage_vertices)} "
        f"src={_bbox(source_mesh_vertices)}"
    )

    # UV link precision 3 matches MaximumADHD's hashUV (round(uv * 1e3) / 1e3);
    # the reference cage and the body cage snapshot come from different assets,
    # so 4-decimal buckets miss correspondences the reference implementation catches.
    rbf_global_threshold = 4096 if numpy_available() else 96
    solved = solve_two_stage_cage_deformation(
        build_mesh_vertices(
            [vertex["position"] for vertex in reference_vertices],
            normals=[vertex.get("normal") for vertex in reference_vertices],
            uvs=[vertex.get("uv") for vertex in reference_vertices],
        ),
        build_mesh_vertices(
            [vertex["position"] for vertex in target_snapshot["vertices"]],
            normals=[vertex.get("normal") for vertex in target_snapshot["vertices"]],
            uvs=[vertex.get("uv") for vertex in target_snapshot["vertices"]],
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
        precision=3,
        inner_global_threshold=rbf_global_threshold,
        outer_global_threshold=rbf_global_threshold,
        reference_inner_faces=reference_faces,
        current_inner_faces=target_snapshot.get("faces"),
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
                "position": _copy_position(predicted_mesh_positions[vertex_index]),
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

    # Fit delta: how far the cage deformer moved the authored bind geometry
    # (logged for diagnostics; the solution is authoritative with a true rbxm).
    fit_total = 0.0
    fit_max = 0.0
    for vertex, predicted_position in zip(source_mesh_vertices, predicted_mesh_positions):
        authored_position = vertex.get("position")
        if authored_position is None:
            continue
        delta = (Vector(predicted_position) - Vector(authored_position)).length
        fit_total += delta
        fit_max = max(fit_max, delta)
    fit_avg = fit_total / max(len(predicted_mesh_positions), 1)
    result["fit_avg_distance"] = fit_avg
    result["fit_max_distance"] = fit_max

    # Cage delta: how much the deformer believes the OUTER CAGE itself must
    # move to fit this body (reference vs avatar snapshot). The render mesh
    # should never be dragged further than the cage that wraps it — a fit
    # much larger than the cage delta means the field is extrapolating the
    # reference-vs-avatar body re-proportioning through the garment, which
    # is not a fit (Roblox anchors garments against the reference body).
    delta_total = 0.0
    delta_max = 0.0
    predicted_outer_positions = solved.get("predicted_outer_positions") or []
    for cage_vertex, predicted_cage_position in zip(outer_cage_vertices, predicted_outer_positions):
        cage_position = cage_vertex.get("position")
        if cage_position is None:
            continue
        delta = (Vector(predicted_cage_position) - Vector(cage_position)).length
        delta_total += delta
        delta_max = max(delta_max, delta)
    delta_avg = delta_total / max(len(predicted_outer_positions), 1)
    result["cage_delta_avg"] = delta_avg
    result["cage_delta_max"] = delta_max
    fit_note = f", fit(avg={fit_avg:.4f}, max={fit_max:.4f}, cage delta={delta_avg:.4f}/{delta_max:.4f})"

    if (
        len(predicted_mesh_positions) == len(binding["object"].data.vertices)
        and alignment
        and alignment["max"] <= max_distance_limit
        and alignment["avg"] <= avg_distance_limit
    ):
        result["mode"] = "index"
        return result, (
            "cage index "
            f"(auto_skin={auto_skin or 'unknown'}, links={solved['inner_link_count']}, "
            f"inner={solved.get('inner_solver_mode')}, "
            f"outer={solved.get('outer_solver_mode')}, avg={alignment['avg']:.6f}, "
            f"max={alignment['max']:.6f}{fit_note})"
        )

    result["mode"] = "position"
    result["position_samples"] = _build_position_samples_from_vertices(predicted_vertices, vertex_weights)
    if alignment:
        return result, (
            "cage position "
            f"(auto_skin={auto_skin or 'unknown'}, links={solved['inner_link_count']}, "
            f"inner={solved.get('inner_solver_mode')}, "
            f"outer={solved.get('outer_solver_mode')}, avg={alignment['avg']:.6f}, "
            f"max={alignment['max']:.6f}{fit_note})"
        )
    return result, (
        "cage position "
        f"(auto_skin={auto_skin or 'unknown'}, links={solved['inner_link_count']}, "
        f"inner={solved.get('inner_solver_mode')}, "
        f"outer={solved.get('outer_solver_mode')}{fit_note})"
    )


# Index alignment / face counting live in skin_binding.py (imported above).


def _slice_mesh_data_to_lod(mesh_data, lod_index):
    """Slice a parsed FileMesh dict to a single LOD: its face range plus a
    compacted vertex buffer containing only referenced vertices (faces are
    remapped accordingly). Returns the input unchanged when there is no LOD
    table or the index is out of range."""
    if not isinstance(mesh_data, dict):
        return mesh_data

    lod_offsets = mesh_data.get("lod_offsets") or []
    all_faces = mesh_data.get("faces") or []
    if len(lod_offsets) <= 1 or not all_faces:
        return mesh_data
    if lod_index < 0 or lod_index >= len(lod_offsets):
        return mesh_data

    start = max(0, min(int(lod_offsets[lod_index]), len(all_faces)))
    end = lod_offsets[lod_index + 1] if lod_index + 1 < len(lod_offsets) else len(all_faces)
    end = max(start, min(int(end), len(all_faces)))

    try:
        import numpy as np  # bundled with Blender

        if isinstance(all_faces, np.ndarray):
            selected_faces = all_faces[start:end]
            if selected_faces.size == 0:
                return mesh_data
            from .filemesh import _as_rows

            used_vertex_indices = np.unique(selected_faces)
            remap = np.empty(int(used_vertex_indices.max()) + 1, dtype=np.int64)
            remap[used_vertex_indices] = np.arange(len(used_vertex_indices))
            out = dict(mesh_data)
            for key in (
                "positions",
                "normals",
                "uvs",
                "tangents",
                "tangent_bytes",
                "tangent_signs",
                "tangent_sign_bytes",
                "colors",
                "color_bytes",
            ):
                values = mesh_data.get(key)
                if isinstance(values, np.ndarray):
                    out[key] = _as_rows(values[used_vertex_indices])
                else:
                    out[key] = values
            vertex_weights = mesh_data.get("vertex_weights")
            if isinstance(vertex_weights, list):
                out["vertex_weights"] = [
                    vertex_weights[index] if index < len(vertex_weights) else {}
                    for index in used_vertex_indices
                ]
            else:
                out["vertex_weights"] = vertex_weights
            out["faces"] = _as_rows(remap[selected_faces])
            return out
    except ImportError:
        pass

    selected_faces = list(all_faces[start:end])
    if not selected_faces:
        return mesh_data

    used_vertex_indices = sorted(
        {
            int(vertex_index)
            for face in selected_faces
            for vertex_index in face[:3]
            if vertex_index is not None
        }
    )
    if not used_vertex_indices:
        out = dict(mesh_data)
        out["faces"] = selected_faces
        return out

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
    selected_mesh_data["tangents"] = _select_vertex_array(mesh_data.get("tangents"), default=None)
    selected_mesh_data["tangent_bytes"] = _select_vertex_array(mesh_data.get("tangent_bytes"), default=None)
    selected_mesh_data["tangent_signs"] = _select_vertex_array(mesh_data.get("tangent_signs"), default=None)
    selected_mesh_data["tangent_sign_bytes"] = _select_vertex_array(mesh_data.get("tangent_sign_bytes"), default=None)
    selected_mesh_data["colors"] = _select_vertex_array(mesh_data.get("colors"), default=None)
    selected_mesh_data["color_bytes"] = _select_vertex_array(mesh_data.get("color_bytes"), default=None)
    selected_mesh_data["vertex_weights"] = _select_vertex_array(mesh_data.get("vertex_weights"), default={})
    selected_mesh_data["faces"] = remapped_faces
    return selected_mesh_data


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

    selected_mesh_data = _slice_mesh_data_to_lod(mesh_data, best["index"])
    selected_mesh_data["lod_selection"] = {
        "index": best["index"],
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

    wrap_layer_metadata = _get_wrap_layer_metadata(binding.get("entry") or {})
    direct_binding = {
        "mesh_data": mesh_data,
    }
    target_faces = _build_mesh_object_faces(mesh_obj)

    filemesh_world_positions = _compute_filemesh_world_positions(binding)
    if filemesh_world_positions:
        direct_binding["filemesh_world_positions"] = filemesh_world_positions
    alignment = _estimate_index_alignment(mesh_obj, filemesh_world_positions)
    if alignment:
        direct_binding["index_alignment"] = alignment

    topology_index_binding, topology_index_message = _build_wrap_topology_index_binding(
        binding,
        target_faces=target_faces,
        index_alignment=alignment,
    )
    if topology_index_binding:
        direct_binding.update(topology_index_binding)
        return direct_binding, topology_index_message

    source_uv_binding, source_uv_message = _build_source_uv_binding(binding, target_faces=target_faces)
    source_topology_binding, source_topology_message = _build_source_topology_binding(
        binding, target_faces=target_faces)
    if wrap_layer_metadata and source_topology_binding:
        direct_binding.update(source_topology_binding)
        return direct_binding, source_topology_message

    if source_uv_binding and source_uv_binding.get("uv_link_coverage", 0.0) >= 0.95:
        direct_binding.update(source_uv_binding)
        return direct_binding, source_uv_message

    if source_topology_binding:
        direct_binding.update(source_topology_binding)
        return direct_binding, source_topology_message

    position_samples = _build_position_samples(binding)
    if len(vertex_weights) == len(mesh_obj.data.vertices):
        if _index_alignment_is_tight(mesh_obj, alignment):
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

    from . import avatar_scale  # noqa: PLC0415
    limb_scale = avatar_scale.entry_limb_scale(entry)
    source_vertices = _build_transformed_filemesh_vertices(
        mesh_data,
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=_get_effective_mesh_size(entry, mesh_data),
        limb_scale=limb_scale,
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
            link_count = len(links) if links else 0
            reject_notes.append(
                f"p{precision}:links={link_count}/{len(target_vertices)},"
                f"collapsed={len(collapsed_vertices)}"
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

        # Resolve each collapsed vertex to the original vert whose authored bone
        # weights best match the bucket's blended profile. Authored weights are
        # authoritative — at a shoulder seam the arm verts are weighted to the
        # arm and torso verts to the torso, so this is data-driven and side-
        # independent, unlike normal-similarity or dedup-order guessing.
        def _dominant_bone(weight_dict):
            if not weight_dict:
                return None
            return max(weight_dict.items(), key=lambda item: item[1])[0]

        def _weight_profile_distance(left, right):
            keys = set(left) | set(right)
            return sum(abs(float(left.get(k, 0.0)) - float(right.get(k, 0.0))) for k in keys)

        resolved_weights = []
        for collapsed_index, collapsed_vertex in enumerate(collapsed_vertices):
            collapsed_pos = collapsed_vertex.get("position")
            cand_key = _round_vector_key(collapsed_pos, precision=precision) if collapsed_pos else None
            cand_indices = position_to_original_indices.get((precision, cand_key), [])
            if not cand_indices:
                cand_indices = [representative_original_indices[collapsed_index]]

            bucket_weights = collapsed_weights[collapsed_index] if collapsed_index < len(collapsed_weights) else {}
            bucket_dominant = _dominant_bone(bucket_weights)
            best_orig = None
            if bucket_dominant is not None:
                # Prefer candidates whose dominant bone matches the bucket's —
                # this is what keeps an arm seam from resolving to a torso vert.
                matching = [
                    orig_index
                    for orig_index in cand_indices
                    if _dominant_bone(vertex_weights[orig_index] if orig_index < len(vertex_weights) else {}) == bucket_dominant
                ]
                if matching:
                    best_orig = min(
                        matching,
                        key=lambda orig_index: _weight_profile_distance(
                            bucket_weights,
                            vertex_weights[orig_index] if orig_index < len(vertex_weights) else {},
                        ),
                    )
            if best_orig is None:
                best_orig = min(
                    cand_indices,
                    key=lambda orig_index: _weight_profile_distance(
                        bucket_weights,
                        vertex_weights[orig_index] if orig_index < len(vertex_weights) else {},
                    ),
                )
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
    # Prefer the authoritative meshToBone map built by rbxm.py (identity: part→bone
    # when both use the same names). Fall back to the heuristic part→bone map for
    # FBX-export rigs where part names differ from joint names.
    part_to_bone_map = meta_loaded.get("meshToBone") or _build_part_to_bone_map(meta_loaded.get("rig") or {})
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

        mesh_obj = _find_parts_object_for_entry(parts_collection, entry)
        if mesh_obj is None or mesh_obj.type != "MESH" or mesh_obj.data is None:
            continue

        wrap_layer_metadata = _get_wrap_layer_metadata(entry)
        wrap_target_metadata = _get_wrap_target_metadata(entry)

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
            # bone_overlap is advisory only: rbxm Bone instances are now part
            # of the rig tree, so authored weights bind directly by name even
            # when no Motor6D part shares the bone names.
            if has_weights:
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
            # Roblox refits EVERY wrap layer through its LinearRBF cage deformer
            # (reference body cage -> this avatar's cage snapshot) before skinning,
            # so solve it independently of the direct skin-bind result.  The
            # direct bind is a Blender vertex/weight correspondence check; it
            # can fail after a name or id change even when the FileMesh has the
            # source positions and weights the cage solver needs.  Gating the
            # cage solve on it made clothing remain in its authored shape.
            wrap_solver_binding = None
            wrap_solver_message = None
            wrap_solver_binding, wrap_solver_message = _build_wrap_solver_binding(
                binding, wrap_target_snapshot
            )

            if direct_binding:
                # The mesh's authored weights (bound directly by bone name)
                # are authoritative. AutoSkin=Disabled means Roblox itself
                # uses the authored weights verbatim.
                direct_mode = direct_binding.get("mode")
                if direct_mode in ("uv-map", "vertex-map", "index", "position"):
                    binding.update(direct_binding)
                    if wrap_target_metadata:
                        binding["wrap_target"] = wrap_target_metadata
                    cage_fit_note = None
                    if wrap_solver_binding and wrap_solver_binding.get("predicted_mesh_positions"):
                        fit_avg = wrap_solver_binding.get("fit_avg_distance")
                        fit_max = wrap_solver_binding.get("fit_max_distance")
                        delta_avg = wrap_solver_binding.get("cage_delta_avg")
                        delta_max = wrap_solver_binding.get("cage_delta_max")
                        # The cage solution is authoritative (true rbxm) — always
                        # rebase. The old size/distance/consistency gates were an
                        # OBJ-era heuristic and would leave wrap layers unfitted.
                        binding["predicted_mesh_positions"] = wrap_solver_binding["predicted_mesh_positions"]
                        binding["wrap_solver"] = wrap_solver_binding.get("wrap_solver")
                        binding["wrap_auto_skin"] = wrap_solver_binding.get("wrap_auto_skin")
                        binding["fit_avg_distance"] = fit_avg
                        binding["fit_max_distance"] = fit_max
                        binding["cage_delta_avg"] = delta_avg
                        binding["cage_delta_max"] = delta_max
                        cage_fit_note = (
                            f"cage fit avg={fit_avg:.4f} max={fit_max:.4f} "
                            f"(delta={delta_avg:.4f}/{delta_max:.4f})"
                        )
                    elif wrap_solver_message:
                        cage_fit_note = f"cage solver skipped ({wrap_solver_message})"
                    label = direct_mode_message or direct_mode
                    if cage_fit_note:
                        label = f"{label}, {cage_fit_note}"
                    _log_binding_mode(mesh_obj, binding, f"{label}, wrap direct weights")
                    bindings[mesh_obj] = binding
                    continue

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
            # Accessories that ship without per-vertex weights (classic
            # texture meshes like ears/hats) cannot be skinned; bind them
            # rigidly to the bone their part maps to instead of falling
            # through to a CHILD_OF on the whole armature, which ignores
            # bone motion entirely.
            rigid_bone = part_to_bone_map.get(entry.get("name"))
            if rigid_bone:
                binding["mode"] = "rigid"
                binding["rigid_bone"] = rigid_bone
                _log_binding_mode(mesh_obj, binding, f"rigid follow ({rigid_bone})")
                bindings[mesh_obj] = binding
                continue
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
        _get_effective_mesh_size(entry, binding.get("mesh_data") or {}),
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


# Weight limits, group collection, data-transfer sequencing, and the vertex
# component/geometry helpers all live in skin_binding.py (imported above).

# All weight-application paths and the part-matching/fingerprint machinery
# live in skin_binding.py / part_matching.py (imported above).

def _collect_all_bone_names(rig_def):
    """Walk the rig tree and collect every jname and pname into a set."""
    names = set()

    def walk(node):
        jname = node.get("jname")
        pname = node.get("pname")
        if jname:
            names.add(jname.lower())
        if pname:
            names.add(pname.lower())
        for child in node.get("children") or []:
            walk(child)
    walk(rig_def)
    return names


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
        if (child.get("jointType") or "Motor6D") not in {"Weld", "WeldConstraint", "RigidConstraint", "Snap"}
    ]


def _collect_deform_bone_names(rig_def):
    names = set()

    def walk(node):
        if node.get("isDeformBone") and node.get("jname"):
            names.add(node["jname"].casefold())
        for child in node.get("children") or []:
            walk(child)

    walk(rig_def)
    return names


def create_joint_bone(
    ao,
    parent_bone_name,
    transform_cf,
    c0_cf,
    c1_cf,
    bone_name,
    joint_type="Motor6D",
):
    """Create a Motor6D-style bone on an armature, mirroring load_rigbone's
    non-root branch.

    The bone head sits at the joint position (transform * C1 in Roblox
    space) and every prop the animation serializer/sampler expects is
    stamped (transform, transform0, transform1, nicetransform, rbx_*,
    is_transformable).  The caller is responsible for bone-collection
    visibility and object mode state.  Returns the created bone name, or
    None on failure.
    """
    t2b = get_transform_to_blender()
    mat = cf_to_mat(transform_cf)
    mat0 = cf_to_mat(c0_cf)
    mat1 = cf_to_mat(c1_cf)
    o_trans = t2b @ (mat @ mat1)
    bone_dir = (t2b @ mat).to_3x3().to_4x4() @ Vector((0, 0, 1))

    amt = ao.data
    prev_active = bpy.context.view_layer.objects.active
    prev_mode = ao.mode if ao == prev_active else None
    bpy.context.view_layer.objects.active = ao
    if ao.mode != "EDIT":
        if not _safe_mode_set("EDIT", ao):
            return None
    try:
        edit_bones = amt.edit_bones
        parent_edit = edit_bones.get(parent_bone_name)
        if parent_edit is None:
            return None
        final_name = bone_name
        counter = 1
        while final_name in edit_bones:
            final_name = f"{bone_name}.{counter:03d}"
            counter += 1
        bone = edit_bones.new(final_name)
        bone.parent = parent_edit
        bone.head = o_trans.to_translation()
        bone.tail = o_trans @ Vector((0, 0.25, 0))
        bone.align_roll(bone_dir)
        bone.use_deform = False
        post_mat = bone.matrix
        bone["transform"] = _matrix_to_idprop(mat)
        bone["transform0"] = _matrix_to_idprop(mat0)
        bone["transform1"] = _matrix_to_idprop(mat1)
        bone["nicetransform"] = _matrix_to_idprop(o_trans.inverted() @ post_mat)
        bone["rbx_joint_type"] = joint_type or "Motor6D"
        bone["rbx_original_parent"] = parent_bone_name
        bone["rbx_source_name"] = bone_name
        bone["is_transformable"] = True
        return bone.name
    finally:
        _safe_mode_set("OBJECT", ao)
        if prev_active:
            bpy.context.view_layer.objects.active = prev_active
        if prev_mode and prev_active == ao and prev_mode != "EDIT":
            _safe_mode_set(prev_mode, ao)


def load_rigbone(
    ao,
    rigging_type,
    rigsubdef,
    parent_bone,
    parts_collection,
    match_ctx,
    all_bone_names,
    deform_bone_names=None,
):
    """Load a single rig bone with its children."""
    amt = ao.data
    if deform_bone_names is None:
        deform_bone_names = _collect_deform_bone_names(rigsubdef)

    source_name = rigsubdef["jname"]
    is_deform_bone = rigsubdef.get("isDeformBone", False)
    bone_name = source_name
    if not is_deform_bone and source_name.casefold() in deform_bone_names:
        bone_name = f"__RBX_STRUCTURAL__{source_name}"

    bone = amt.edit_bones.new(bone_name)
    bone["rbx_source_name"] = source_name
    joint_type = rigsubdef.get("jointType") or "Motor6D"
    original_parent_bone = rigsubdef.get("originalParentBone")

    mat = cf_to_mat(rigsubdef["transform"])
    bone["transform"] = _matrix_to_idprop(mat)
    t2b = get_transform_to_blender()
    bone_dir = (t2b @ mat).to_3x3().to_4x4() @ Vector((0, 0, 1))

    # Check if this bone is marked as a deform bone from Studio export
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
    else:
        # Deform-capable joints (Motor6D/AnimationConstraint) must be Blender
        # deform bones ONLY when their part is a skinned mesh
        # (MeshPart.HasSkinnedMesh), or armature-modifier skinning silently
        # does nothing while non-skinned joints would wrongly deform nearby
        # geometry.  Legacy exports without the flag keep the old
        # all-deformable behavior.  Structural rigid joints and Motor6D
        # bones renamed to __RBX_STRUCTURAL__ never deform.
        skinned = bool(rigsubdef.get("hasSkinnedMesh", True))
        structural = (
            bone_name.startswith("__RBX_STRUCTURAL__")
            or joint_type in ("Weld", "WeldConstraint", "RigidConstraint", "Snap")
        )
        bone.use_deform = skinned and not structural

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

        # NOTE: do NOT repoint Bone tails at child heads. Blender forces the
        # bone Y axis along head->tail, so moving the tail rotates matrix_local
        # away from the Roblox bone's rest frame. The deform import path applies
        # the Roblox delta directly as matrix_basis and requires matrix_local to
        # equal the Roblox rest frame (modulo the axis swizzle), otherwise
        # rotations come out wrong. Keep the tail on the bone's local Z stub.

        # Deform bones need their imported local axes preserved exactly.
        # The "nice" articulated-chain adjustments are useful for Motor6D helper
        # rigs, but they skew skinned bone bases and cause imported animation
        # axes to drift.
        if rigging_type != "RAW" and not is_deform_bone:
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

    # Gather child names for AUX filtering below
    children = rigsubdef.get("children") or []
    child_part_names = set()
    for child in children:
        jname = child.get("jname")
        pname = child.get("pname")
        if jname:
            child_part_names.add(jname.lower())
        if pname:
            child_part_names.add(pname.lower())

    # Process child bones first so they claim their own meshes before
    # this bone's AUX list can steal them.
    for child in children:
        load_rigbone(
            ao,
            rigging_type,
            child,
            bone,
            parts_collection,
            match_ctx,
            all_bone_names,
            deform_bone_names,
        )

    # Process PRIMARY pname FIRST — every bone should claim its own mesh
    # before its AUX list takes leftovers.
    p_name = rigsubdef.get("pname")
    if p_name:
        found_primary = _find_matching_part(
            p_name, None, match_ctx, inst_ref=rigsubdef.get("inst_ref")
        )

        # Fallback: simple lookup in collection if _find_matching_part fails
        if not found_primary and parts_collection:
            found_primary = parts_collection.objects.get(p_name)
            if found_primary and found_primary in match_ctx["used"]:
                found_primary = None

        if found_primary:
            match_ctx["used"].add(found_primary)
            if found_primary not in match_ctx.get("skinned_mesh_bindings", {}):
                pending = match_ctx.setdefault("pending_constraints", [])
                dedup = match_ctx.setdefault("_pending_dedup", set())
                key = (id(found_primary), bone.name)
                if key not in dedup:
                    dedup.add(key)
                    pending.append((found_primary, bone.name))

    # Process AUX parts (welded to this bone but not the primary part).
    # Skip AUX entries that correspond to:
    #   a) child bones — children already claimed their meshes above.
    #   b) ANY bone in the rig — let that bone claim its own mesh via pname.
    aux_list = rigsubdef.get("aux") or []
    aux_transform_list = rigsubdef.get("auxTransform") or []
    for idx, aux_name in enumerate(aux_list):
        if not aux_name:
            continue
        aux_lower = aux_name.lower()
        if aux_lower in child_part_names or aux_lower in all_bone_names:
            continue

        local_cf = aux_transform_list[idx] if idx < len(aux_transform_list) else None
        found_obj = _find_matching_part(aux_name, local_cf, match_ctx)

        if found_obj:
            match_ctx["used"].add(found_obj)
            if found_obj not in match_ctx.get("skinned_mesh_bindings", {}):
                pending = match_ctx.setdefault("pending_constraints", [])
                dedup = match_ctx.setdefault("_pending_dedup", set())
                key = (id(found_obj), bone.name)
                if key not in dedup:
                    dedup.add(key)
                    pending.append((found_obj, bone.name))

    return


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
        if joint_type in ("Weld", "WeldConstraint", "RigidConstraint", "Snap"):
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
    # (all_objects recurses into the Rig/Parts subcollections).
    old_armature = None
    for obj in master_collection.all_objects:
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

    def _build_inst_ref_to_bone(node, result=None, jname_to_ref=None):
        """Walk the rig tree and map rbxm instance referent to deform bone name.

        Also populates jname_to_ref with the authoritative instance referent for
        each joint name so meshToBone entries can resolve to their exact mesh.
        """
        if result is None:
            result = {}
        if jname_to_ref is None:
            jname_to_ref = {}
        if not isinstance(node, dict):
            return result, jname_to_ref
        jname = node.get("jname")
        inst_ref = node.get("inst_ref")
        joint_type = node.get("jointType")
        # Deform bones are Motor6D/AnimationConstraint nodes (or the root).
        is_deform = inst_ref is not None and (
            joint_type in ("Motor6D", "AnimationConstraint") or node.get("pname") is None
        )
        if is_deform and jname:
            result[int(inst_ref)] = jname
        if jname and inst_ref is not None:
            jname_to_ref[jname] = int(inst_ref)
        for child in node.get("children") or []:
            _build_inst_ref_to_bone(child, result, jname_to_ref)
        return result, jname_to_ref

    # Pre-populate authoritative mesh->bone constraints from Studio export.
    # This bypasses all name-based matching when the Studio plugin explicitly
    # tells us which mesh belongs to which bone. Essential for duplicate-named
    # parts like two "GLOVE" accessories that would otherwise swap randomly.
    mesh_to_bone = meta_loaded.get("meshToBone") or {}
    if mesh_to_bone:
        print(f"[RigCreate] meshToBone mapping present ({len(mesh_to_bone)} entries), pre-constraining...")

        # Build an inst_ref -> bone_name map from the rig tree so rbxm-sourced
        # meshes can be matched authoritatively regardless of Blender object
        # renaming or duplicate part names.
        inst_ref_to_bone = {}
        jname_to_inst_ref = {}
        rig_def = meta_loaded.get("rig")
        if isinstance(rig_def, dict):
            inst_ref_to_bone, jname_to_inst_ref = _build_inst_ref_to_bone(rig_def)

        # Index imported meshes by their rbxm instance referent for O(1) lookup.
        inst_ref_to_obj = {}
        for candidate in parts_collection.objects:
            ref = candidate.get("RBXInstRef")
            if ref is not None:
                inst_ref_to_obj[int(ref)] = candidate

        for mesh_name, bone_name in mesh_to_bone.items():
            mesh_obj = None
            # Try authoritative inst_ref lookup first: the rig tree tells us
            # exactly which instance corresponds to this jname.
            preferred_ref = jname_to_inst_ref.get(mesh_name)
            if preferred_ref is not None:
                obj = inst_ref_to_obj.get(preferred_ref)
                if obj is not None and obj not in match_ctx["used"]:
                    mesh_obj = obj
                    print(
                        f"[RigCreate] meshToBone resolved '{mesh_name}' by inst_ref #{preferred_ref} -> mesh '{obj.name}'")

            # Fallback: any unused mesh whose inst_ref maps to the same bone.
            if mesh_obj is None:
                for ref, obj in inst_ref_to_obj.items():
                    if inst_ref_to_bone.get(ref) == bone_name and obj not in match_ctx["used"]:
                        mesh_obj = obj
                        print(f"[RigCreate] meshToBone resolved '{mesh_name}' by inst_ref #{ref} -> mesh '{obj.name}'")
                        break

            if mesh_obj is None:
                mesh_obj = parts_collection.objects.get(mesh_name)
            if mesh_obj is None:
                stripped = _strip_suffix(mesh_name)
                for candidate in parts_collection.objects:
                    if _strip_suffix(candidate.name) == stripped:
                        mesh_obj = candidate
                        break
            if mesh_obj is None:
                # Studio-style names ("<Model>.model/Head.MeshPart") — compare
                # by stem so the rig-tree part name ("Head") still resolves.
                stem = _match_stem(mesh_name)
                for candidate in parts_collection.objects:
                    if _match_stem(candidate.name) == stem:
                        mesh_obj = candidate
                        break
            if mesh_obj is None:
                print(f"[RigCreate] meshToBone WARNING: mesh '{mesh_name}' not found for bone '{bone_name}'")
                continue
            if mesh_obj in match_ctx["used"]:
                print(f"[RigCreate] meshToBone SKIP: mesh '{mesh_obj.name}' already used")
                continue
            match_ctx["used"].add(mesh_obj)
            pending = match_ctx.setdefault("pending_constraints", [])
            dedup = match_ctx.setdefault("_pending_dedup", set())
            key = (id(mesh_obj), bone_name)
            if key not in dedup:
                dedup.add(key)
                pending.append((mesh_obj, bone_name))
                print(f"[RigCreate] meshToBone PRE-CONSTRAINED: mesh '{mesh_obj.name}' -> bone '{bone_name}'")
        pending_constraints = match_ctx.get("pending_constraints") or []
        if pending_constraints:
            print(
                f"[RigCreate] meshToBone pre-constrained {len([x for x in pending_constraints if x[0] in match_ctx['used']])} meshes")

        # Stash the inst_ref -> bone map for the fallback constraint pass.
        match_ctx["inst_ref_to_bone"] = inst_ref_to_bone

    # Collect all bone names once so AUX filtering is consistent across rename + constraint passes
    all_bone_names = _collect_all_bone_names(meta_loaded["rig"])

    # Try to restore correct part names using fingerprinting before building constraints.
    _apply_fingerprint_renames(
        meta_loaded["rig"],
        match_ctx,
        allow_aux_renames=not bool(meta_loaded.get("partAux")),
        all_bone_names=all_bone_names,
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

    # Move the new armature into the rig's collection (the "<model> Rig"
    # subcollection when present, else the master collection itself).
    # Accept Blender's dedup suffixes ("xsixx.model Rig.001").
    rig_coll = None
    for child in master_collection.children:
        if re.match(r".+ Rig(?:\.\d+)?$", child.name):
            rig_coll = child
            break
    for coll in ao.users_collection:
        coll.objects.unlink(ao)
    (rig_coll or master_collection).objects.link(ao)

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
    load_rigbone(ao, rigging_type, meta_loaded["rig"], None, parts_collection, match_ctx, all_bone_names)
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
    ok, msg = auto_constraint_parts(
        ao.name,
        skip_objects=skip_objects,
        inst_ref_to_bone=match_ctx.get("inst_ref_to_bone"),
    )

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
