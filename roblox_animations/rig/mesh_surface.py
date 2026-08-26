"""
Mesh surface and geometry helpers for synthesized Roblox parts.

Bulk geometry upload, custom normals, loop UVs, corner colors, and the
display configuration used by the mesh builders in creation.py and
primitive_shapes.py.  Kept dependency-free of the rest of the rig package
on purpose: these are leaf utilities.
"""

import math
import bpy


def _set_mesh_smooth_shading(mesh):
    polygons = getattr(mesh, "polygons", None)
    if not polygons:
        return
    try:
        polygons.foreach_set("use_smooth", [True] * len(polygons))
    except Exception:
        for polygon in polygons:
            polygon.use_smooth = True


def _bulk_set(attribute, name, values):
    """foreach_set with a list conversion fallback for older Blender builds."""
    try:
        attribute.foreach_set(name, values)
    except TypeError:
        attribute.foreach_set(name, list(values))


def _populate_mesh_geometry(mesh, positions, faces):
    """Fill a new mesh through Blender's bulk RNA arrays.

    This avoids ``from_pydata`` creating Python-side vertex/face wrappers for
    every item, which is material on place-size imports. Buffer flattening is
    numpy-first: a place asset can carry a million loops, and a python list
    comprehension per vertex/loop is a real slice-budget cost.
    """
    if positions is None or faces is None:
        return False
    if len(positions) == 0 or len(faces) == 0:
        return False

    try:
        import numpy as np  # bundled with Blender

        from itertools import chain

        pos_arr = np.asarray(positions, dtype=np.float32)
        if pos_arr.ndim != 2 or pos_arr.shape[0] != len(positions) or pos_arr.shape[1] < 3:
            raise ValueError("positions must be a flat list of vec3 rows")
        co = pos_arr[:, :3].ravel()
        if isinstance(faces, np.ndarray) and faces.ndim == 2:
            loop_total = int(faces.shape[0] * faces.shape[1])
            if loop_total <= 0:
                return False
            vertex_index_buffer = faces.ravel()
            loop_starts = np.arange(0, loop_total, faces.shape[1], dtype=np.int64)
            loop_totals = np.full(faces.shape[0], faces.shape[1], dtype=np.int64)
        else:
            face_lens = np.fromiter((len(face) for face in faces), dtype=np.int64, count=len(faces))
            loop_total = int(face_lens.sum())
            if loop_total <= 0:
                return False
            vertex_index_buffer = np.fromiter(
                chain.from_iterable(faces), dtype=np.int64, count=loop_total
            )
            loop_starts = np.cumsum(face_lens) - face_lens
            loop_totals = face_lens
    except Exception:
        loop_total = sum(len(face) for face in faces)
        if loop_total <= 0:
            return False
        co = [component for position in positions for component in position[:3]]
        vertex_index_buffer = [index for face in faces for index in face]
        loop_starts = []
        cursor = 0
        for face in faces:
            loop_starts.append(cursor)
            cursor += len(face)
        loop_totals = [len(face) for face in faces]

    try:
        mesh.vertices.add(len(positions))
        _bulk_set(mesh.vertices, "co", co)
        mesh.loops.add(loop_total)
        _bulk_set(mesh.loops, "vertex_index", vertex_index_buffer)
        mesh.polygons.add(len(faces))
        _bulk_set(mesh.polygons, "loop_start", loop_starts)
        _bulk_set(mesh.polygons, "loop_total", loop_totals)
        mesh.update()
        return True
    except Exception:
        return False


def _apply_mesh_custom_normals(mesh, vertices):
    # Blender 5.1's ``normals_split_custom_set`` access-violated on some very
    # large FileMesh data (see Blender's mesh_normals_corner_custom_set).
    # That is process-fatal and cannot be caught in Python.  With the strict
    # finite-nonzero validation below it applies safely to normal-sized
    # meshes (verified on 5.1.0); only pathological very-large meshes keep
    # the derived-smooth-normals fallback.
    if tuple(getattr(bpy.app, "version", (0, 0, 0))) >= (5, 1, 0):
        if len(mesh.vertices) > 100_000:
            return False
    if not hasattr(mesh, "normals_split_custom_set_from_vertices") and not hasattr(mesh, "normals_split_custom_set"):
        return False
    if not vertices or len(vertices) != len(mesh.vertices):
        return False

    # ``normals_split_custom_set`` enters Blender's C mesh-normal code, where
    # malformed source data can cause an access violation instead of a Python
    # exception. FileMesh payloads are external, so reject anything other than
    # a complete set of finite, non-zero vec3 normals before crossing that API
    # boundary. Blender will derive safe normals from the mesh winding instead.
    normals = []
    for vertex in vertices:
        normal = vertex.get("normal") if isinstance(vertex, dict) else None
        if normal is None:
            return False
        try:
            if len(normal) != 3:
                return False
            normal = tuple(float(component) for component in normal)
        except (TypeError, ValueError, OverflowError):
            return False
        if not all(math.isfinite(component) for component in normal):
            return False
        length_squared = sum(component * component for component in normal)
        if length_squared <= 1e-20:
            return False
        inverse_length = 1.0 / math.sqrt(length_squared)
        normals.append(tuple(component * inverse_length for component in normal))

    try:
        if hasattr(mesh, "use_auto_smooth"):
            mesh.use_auto_smooth = True
        if hasattr(mesh, "normals_split_custom_set"):
            loop_normals = [normals[int(loop.vertex_index)] for loop in mesh.loops]
            if len(loop_normals) != len(mesh.loops):
                return False
            mesh.normals_split_custom_set(loop_normals)
        else:
            mesh.normals_split_custom_set_from_vertices(normals)
        return True
    except Exception:
        return False


def _vertex_corner_value(vertices, vertex_index, key, default=None):
    try:
        vertex_index = int(vertex_index)
    except Exception:
        return default
    if vertex_index < 0 or vertex_index >= len(vertices):
        return default
    vertex = vertices[vertex_index]
    if not isinstance(vertex, dict):
        return default
    value = vertex.get(key)
    return default if value is None else value


def _iter_mesh_loop_vertex_indices(mesh):
    for polygon in mesh.polygons:
        for loop_index, vertex_index in zip(
                range(polygon.loop_start, polygon.loop_start + polygon.loop_total), polygon.vertices):
            yield loop_index, int(vertex_index)


def _mesh_loop_uv_values(mesh, vertices, loop_uvs):
    """Loop-corner UV pairs (numpy fast paths, python fallback)."""
    loop_count = len(mesh.loops)
    if loop_uvs is not None:
        # Worker-produced flat float arrays copy straight into the bulk
        # buffer — no per-loop python objects on either side.
        try:
            import numpy as np  # bundled with Blender

            if isinstance(loop_uvs, np.ndarray):
                flat = loop_uvs.reshape(-1).astype(np.float32)
                if len(flat) >= 2 * loop_count:
                    flat = flat[: 2 * loop_count]
                uv_values = np.zeros(2 * loop_count, dtype=np.float32)
                filled = min(len(flat), 2 * loop_count)
                uv_values[:filled] = flat[:filled]
                uv_values[1::2] = 1.0 - uv_values[1::2]
                return uv_values
        except Exception:
            pass
        try:
            import numpy as np  # bundled with Blender

            arr = np.asarray(loop_uvs[:loop_count], dtype=object)
            valid = arr is not None
            uv_values = np.zeros(2 * loop_count, dtype=np.float32)
            filled = np.flatnonzero(valid)
            if filled.size:
                uv = np.stack(arr[filled].tolist())
                uv_values[2 * filled] = uv[:, 0].astype(np.float32)
                uv_values[2 * filled + 1] = 1.0 - uv[:, 1].astype(np.float32)
            return uv_values
        except Exception:
            pass
        uv_values = [0.0] * (2 * loop_count)
        for loop_index, uv in enumerate(loop_uvs[:loop_count]):
            if uv is not None:
                uv_values[2 * loop_index] = float(uv[0])
                uv_values[2 * loop_index + 1] = 1.0 - float(uv[1])
        return uv_values

    # Avoid millions of ``_vertex_corner_value`` calls on large maps.
    # Mesh loop order matches the bulk-populated face loop order.
    vertex_uvs = [
        vertex.get("uv") if isinstance(vertex, dict) else None
        for vertex in vertices
    ]
    if not vertex_uvs:
        return [0.0] * (2 * loop_count)
    try:
        import numpy as np  # bundled with Blender

        idx_arr = np.empty(loop_count, dtype=np.int64)
        mesh.loops.foreach_get("vertex_index", idx_arr)
        in_range = (idx_arr >= 0) & (idx_arr < len(vertex_uvs))
        clip = np.clip(idx_arr, 0, len(vertex_uvs) - 1)
        uva = np.asarray(vertex_uvs, dtype=object)
        valid = in_range & (uva[clip] is not None)
        uv_values = np.zeros(2 * loop_count, dtype=np.float32)
        filled = np.flatnonzero(valid)
        if filled.size:
            uv = np.stack(uva[clip][filled].tolist())
            uv_values[2 * filled] = uv[:, 0].astype(np.float32)
            uv_values[2 * filled + 1] = 1.0 - uv[:, 1].astype(np.float32)
        return uv_values
    except Exception:
        pass
    uv_values = [0.0] * (2 * loop_count)
    loop_vertex_indices = [0] * loop_count
    mesh.loops.foreach_get("vertex_index", loop_vertex_indices)
    for loop_index, vertex_index in enumerate(loop_vertex_indices):
        uv = vertex_uvs[vertex_index] if 0 <= vertex_index < len(vertex_uvs) else None
        if uv is not None:
            uv_values[2 * loop_index] = float(uv[0])
            uv_values[2 * loop_index + 1] = 1.0 - float(uv[1])
    return uv_values


def _apply_mesh_loop_uvs(mesh, vertices, loop_uvs=None):
    has_vertex_uvs = bool(
        vertices and any(vertex.get("uv") is not None for vertex in vertices if isinstance(vertex, dict))
    )
    # loop_uvs may be a numpy array here — truthiness is not defined for it.
    if loop_uvs is None and not has_vertex_uvs:
        return False

    try:
        uv_values = _mesh_loop_uv_values(mesh, vertices, loop_uvs)
        uv_layer = mesh.uv_layers.get("UVMap") or mesh.uv_layers.new(name="UVMap")
        _bulk_set(uv_layer.data, "uv", uv_values)
        return True
    except Exception:
        return False


def _new_mesh_attribute(mesh, name, data_type, domain="CORNER"):
    attributes = getattr(mesh, "attributes", None)
    if attributes is None:
        return None

    try:
        existing = attributes.get(name)
        if existing is not None:
            return existing
        return attributes.new(name=name, type=data_type, domain=domain)
    except Exception:
        return None


def _apply_mesh_loop_tangents(mesh, vertices):
    if not vertices:
        return False
    if not any(
        isinstance(vertex, dict) and (vertex.get("tangent") is not None or vertex.get("tangent_sign_byte") is not None)
        for vertex in vertices
    ):
        return False

    tangent_attr = _new_mesh_attribute(mesh, "RBXTangent", "FLOAT_VECTOR")
    sign_attr = _new_mesh_attribute(mesh, "RBXTangentSign", "FLOAT")
    sign_byte_attr = _new_mesh_attribute(mesh, "RBXTangentSignByte", "INT")
    if tangent_attr is None and sign_attr is None and sign_byte_attr is None:
        return False

    try:
        for loop_index, vertex_index in _iter_mesh_loop_vertex_indices(mesh):
            tangent = _vertex_corner_value(vertices, vertex_index, "tangent")
            sign = _vertex_corner_value(vertices, vertex_index, "tangent_sign")
            sign_byte = _vertex_corner_value(vertices, vertex_index, "tangent_sign_byte")
            if tangent is not None:
                if tangent_attr is not None:
                    tangent_attr.data[loop_index].vector = (float(tangent[0]), float(tangent[1]), float(tangent[2]))
                if sign is None and len(tangent) >= 4:
                    sign = tangent[3]
            if sign is not None and sign_attr is not None:
                sign_attr.data[loop_index].value = float(sign)
            if sign_byte is not None and sign_byte_attr is not None:
                sign_byte_attr.data[loop_index].value = int(sign_byte)
        return True
    except Exception:
        return False


def _new_mesh_color_attribute(mesh, name="RBXColor"):
    color_attributes = getattr(mesh, "color_attributes", None)
    if color_attributes is not None:
        try:
            existing = color_attributes.get(name)
            layer = existing
            if layer is None:
                layer = color_attributes.new(name=name, type="BYTE_COLOR", domain="CORNER")
            # Mark it active AND the render color: Solid/Workbench "Attributes"
            # viewport shading samples the ACTIVE color attribute, so a union's
            # per-vertex colors are invisible in Solid mode without this (they
            # only showed in Material Preview/Rendered, which read the shader).
            try:
                color_attributes.active_color = layer
                idx = list(color_attributes).index(layer)
                if idx >= 0:
                    mesh.color_attributes.render_color_index = idx
            except Exception:
                pass
            return layer
        except Exception:
            pass

    vertex_colors = getattr(mesh, "vertex_colors", None)
    if vertex_colors is not None:
        try:
            existing = vertex_colors.get(name)
            if existing is not None:
                return existing
            return vertex_colors.new(name=name)
        except Exception:
            pass

    return None


def _apply_mesh_vertex_colors(mesh, vertices):
    if not vertices:
        return False
    colors = [vertex.get("color") for vertex in vertices if isinstance(
        vertex, dict) and vertex.get("color") is not None]
    if not colors:
        return False
    # FileMesh commonly carries a redundant all-white color stream. Our
    # ordinary MeshPart shaders do not consume it, so allocating a corner
    # attribute and copying millions of identical values is pure overhead.
    # Keep genuinely varying colors for CSG/union material paths.
    first_color = colors[0]
    if not any(color != first_color for color in colors[1:]):
        return False

    color_layer = _new_mesh_color_attribute(mesh)
    if color_layer is None:
        return False

    try:
        color_values = _mesh_loop_color_values(mesh, vertices)
        _bulk_set(color_layer.data, "color", color_values)
        return True
    except Exception:
        return False


def _mesh_loop_color_values(mesh, vertices):
    """Loop-corner RGBA values (numpy fast path, python fallback)."""
    loop_count = len(mesh.loops)
    try:
        import numpy as np  # bundled with Blender

        idx_arr = np.empty(loop_count, dtype=np.int64)
        mesh.loops.foreach_get("vertex_index", idx_arr)
        in_range = (idx_arr >= 0) & (idx_arr < len(vertices))
        clip = np.clip(idx_arr, 0, len(vertices) - 1)
        vert_colors = [
            vertex.get("color") if isinstance(vertex, dict) else None
            for vertex in vertices
        ]
        cva = np.asarray(vert_colors, dtype=object)
        valid = in_range & (cva[clip] is not None)
        color_values = np.ones((loop_count, 4), dtype=np.float32)
        filled = np.flatnonzero(valid)
        if filled.size:
            cols = np.stack(cva[clip][filled].tolist()).astype(np.float32)
            width = min(cols.shape[1], 4)
            color_values[filled, :width] = cols[:, :width]
        return color_values.ravel()
    except Exception:
        pass
    color_values = [1.0] * (4 * loop_count)
    for loop_index, loop in enumerate(mesh.loops):
        vertex_index = int(loop.vertex_index)
        color = _vertex_corner_value(vertices, vertex_index, "color", default=(1.0, 1.0, 1.0, 1.0))
        offset = 4 * loop_index
        components = [float(component) for component in color[:4]]
        color_values[offset:offset + 4] = (components + [1.0] * 4)[:4]
    return color_values


def _configure_synthesized_mesh_surface(mesh_obj, vertices, loop_uvs=None, apply_custom_normals=True):
    mesh = getattr(mesh_obj, "data", None)
    if mesh is None:
        return

    _set_mesh_smooth_shading(mesh)
    mesh_obj["RBXSynthesizedUVs"] = bool(_apply_mesh_loop_uvs(mesh, vertices, loop_uvs))
    mesh_obj["RBXSynthesizedCustomNormals"] = bool(
        _apply_mesh_custom_normals(mesh, vertices) if apply_custom_normals else False
    )
    mesh_obj["RBXSynthesizedVertexColors"] = bool(_apply_mesh_vertex_colors(mesh, vertices))
    # These custom attributes are not consumed by our materials. Avoid a
    # second Python-to-RNA pass over every mesh corner, which is prohibitively
    # slow for place files and only duplicates Blender's tangent generation.
    mesh_obj["RBXSynthesizedTangents"] = False
    try:
        mesh.update()
    except Exception:
        pass


def _is_hidden_primary_part(entry):
    """True for a Model.PrimaryPart that is fully transparent.

    Roblox characters commonly use an invisible HumanoidRootPart as
    Model.PrimaryPart. It is structural rig plumbing, not visible scene
    geometry — importers skip building a mesh object for it entirely.
    """
    try:
        if not bool((entry or {}).get("is_primary_part")):
            return False
        return float((entry or {}).get("transparency", 0.0)) >= 1.0 - 1e-6
    except (TypeError, ValueError):
        return False


def _configure_synthesized_mesh_display(mesh_obj, entry):
    # A part whose entry carries WrapTarget metadata still renders its own
    # mesh normally — the wrap target only matters for cage solving. The
    # wireframe "helper" treatment is reserved for synthesized stand-in
    # geometry (marked by the caller), never for a part's render mesh.
    helper_display = bool(mesh_obj.get("RBXDisplayHelper"))
    mesh_obj["RBXDisplayHelper"] = helper_display
    if not helper_display:
        # Defense in depth: callers normally skip hidden primaries before
        # mesh creation; hide here only if one still reaches this path
        # (e.g. shared-filemesh lazy jobs where a later instance qualifies).
        is_hidden_primary = _is_hidden_primary_part(entry)
        mesh_obj["RBXHiddenPrimaryPart"] = is_hidden_primary
        if is_hidden_primary:
            mesh_obj.hide_viewport = True
            mesh_obj.hide_render = True
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
