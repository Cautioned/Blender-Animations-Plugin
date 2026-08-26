"""
FileMesh transform helpers: scaling, axis conversion, and vertex building.

Converts Roblox-space FileMesh payloads into Blender world space with the
correct part scale, plus the small shared vector/matrix utilities used by
the wrap solver and the skin-binding pipeline.  Leaf module: depends only
on core constants/utils and part_matching.
"""

from mathutils import Vector, Matrix

from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat
from .part_matching import _get_wrap_layer_metadata


def _normalize_vector(vector):
    if vector is None:
        return None
    normalized = vector.copy()
    if normalized.length_squared > 0:
        normalized.normalize()
        return normalized
    return None


def _compute_mesh_scale(part_size, mesh_size):
    scale = []
    for idx in range(3):
        mesh_component = float(mesh_size[idx]) if mesh_size and idx < len(mesh_size) else 0.0
        part_component = float(part_size[idx]) if part_size and idx < len(part_size) else 1.0
        scale.append(part_component / mesh_component if abs(mesh_component) > 1e-8 else 1.0)
    return scale


def _compute_filemesh_mesh_size(mesh_data):
    """Natural MeshSize inferred from raw FileMesh vertex positions."""
    if isinstance(mesh_data, dict) and "_rbx_computed_mesh_size" in mesh_data:
        return mesh_data["_rbx_computed_mesh_size"]
    positions = mesh_data.get("positions") if isinstance(mesh_data, dict) else None
    if not positions:
        return None
    xs = [float(p[0]) for p in positions]
    ys = [float(p[1]) for p in positions]
    zs = [float(p[2]) for p in positions]
    size = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
    if isinstance(mesh_data, dict):
        mesh_data["_rbx_computed_mesh_size"] = size
    return size


def _get_effective_mesh_size(entry, mesh_data):
    """Return the mesh natural size to use for part scaling.

    Authoritative rbxm MeshSize is used when present. When it is missing we
    infer it from the FileMesh bounding box so rigid meshes (body parts and
    accessories) scale to their Part.Size. WrapLayer clothing is left at its
    authored scale (fallback to part_size, i.e. unity scale) because the cage
    deformer expects reference-body proportions.
    """
    if isinstance(entry, dict):
        mesh_size = entry.get("mesh_size")
        if mesh_size is not None:
            return mesh_size
        if _get_wrap_layer_metadata(entry):
            return entry.get("part_size")
    computed = _compute_filemesh_mesh_size(mesh_data)
    if computed is not None:
        return computed
    return entry.get("part_size") if isinstance(entry, dict) else None


def _coerce_cf_matrix(value):
    if value is None:
        return None
    if isinstance(value, Matrix):
        return value.copy()
    return cf_to_mat(value)


def _normalize_wrap_auto_skin(value):
    if value is None:
        return None
    # Roblox WrapLayer.AutoSkin enum: 0=Disabled, 1=EnabledOverride,
    # 2=EnabledPreserve. rbxm stores the raw int; the server export sends
    # the enum item name.
    if isinstance(value, (int, float)):
        return {0: "disabled", 1: "enabledoverride", 2: "enabledpreserve"}.get(int(value))
    text = str(value)
    if not text:
        return None
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def _build_transformed_filemesh_vertices(mesh_data, part_cf=None, part_size=None,
                                         mesh_size=None, local_cf=None, limb_scale=None):
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
    direction_matrix = transform_matrix.to_3x3()
    try:
        normal_matrix = direction_matrix.inverted_safe().transposed()
    except Exception:
        normal_matrix = direction_matrix
    scale = _compute_mesh_scale(part_size, mesh_size)
    if limb_scale is not None:
        scale = [scale[i] * float(limb_scale[i]) for i in range(3)]
    scale_x, scale_y, scale_z = scale

    normals = mesh_data.get("normals") or []
    uvs = mesh_data.get("uvs") or []
    tangent_signs = mesh_data.get("tangent_signs") or []
    tangent_sign_bytes = mesh_data.get("tangent_sign_bytes") or []
    colors = mesh_data.get("colors") or []
    normals_len = len(normals)
    uvs_len = len(uvs)
    tangent_signs_len = len(tangent_signs)
    tangent_sign_bytes_len = len(tangent_sign_bytes)
    colors_len = len(colors)
    transformed = []
    transformed_append = transformed.append
    m00, m01, m02, m03 = transform_matrix[0]
    m10, m11, m12, m13 = transform_matrix[1]
    m20, m21, m22, m23 = transform_matrix[2]
    for vertex_index, position in enumerate(positions):
        local_x = position[0] * scale_x
        local_y = position[1] * scale_y
        local_z = position[2] * scale_z
        # Vector, not tuple: every downstream consumer (cage solver,
        # alignment, weight transfer) does Vector arithmetic on this.
        world_vec = Vector((
            m00 * local_x + m01 * local_y + m02 * local_z + m03,
            m10 * local_x + m11 * local_y + m12 * local_z + m13,
            m20 * local_x + m21 * local_y + m22 * local_z + m23,
        ))
        normal = normals[vertex_index] if vertex_index < normals_len else None
        uv = uvs[vertex_index] if vertex_index < uvs_len else None
        # Tangent attributes are intentionally not synthesized on any supported
        # Blender version, so transforming them here is dead per-vertex work.
        tangent = None
        tangent_sign = tangent_signs[vertex_index] if vertex_index < tangent_signs_len else None
        tangent_sign_byte = tangent_sign_bytes[vertex_index] if vertex_index < tangent_sign_bytes_len else None
        color = colors[vertex_index] if vertex_index < colors_len else None
        world_normal = None
        if normal is not None:
            world_normal = _normalize_vector(normal_matrix @ Vector(normal))
        world_tangent = None
        if tangent is not None and len(tangent) >= 4:
            tangent_vec = _normalize_vector(direction_matrix @ Vector((tangent[0], tangent[1], tangent[2])))
            if tangent_vec is not None:
                if tangent_sign is None:
                    tangent_sign = tangent[3]
                world_tangent = (float(tangent_vec.x), float(tangent_vec.y), float(tangent_vec.z), float(tangent_sign))
        transformed_append(
            {
                "index": vertex_index,
                "position": world_vec,
                "normal": world_normal,
                "uv": (float(uv[0]), float(uv[1])) if uv is not None else None,
                "tangent": world_tangent,
                "tangent_sign": float(tangent_sign) if tangent_sign is not None else None,
                "tangent_sign_byte": int(tangent_sign_byte) if tangent_sign_byte is not None else None,
                "color": tuple(float(component) for component in color[:4]) if color is not None else None,
            }
        )

    return transformed


def _copy_position(position):
    """Copy a Vector or tuple position; plain tuples have no ``.copy``."""
    if position is None:
        return None
    return position.copy() if hasattr(position, "copy") else position


def _compute_filemesh_world_positions(binding):
    from . import avatar_scale  # noqa: PLC0415

    entry = binding["entry"]
    vertices = _build_transformed_filemesh_vertices(
        binding["mesh_data"],
        part_cf=entry.get("part_cf"),
        part_size=entry.get("part_size"),
        mesh_size=_get_effective_mesh_size(entry, binding["mesh_data"]),
        limb_scale=avatar_scale.entry_limb_scale(entry),
    )
    if not vertices:
        return None
    return [vertex["position"] for vertex in vertices]


def _build_transformed_filemesh_geometry(mesh_data, part_cf=None, part_size=None,
                                         mesh_size=None, local_cf=None, limb_scale=None):
    vertices = _build_transformed_filemesh_vertices(
        mesh_data,
        part_cf=part_cf,
        part_size=part_size,
        mesh_size=mesh_size,
        local_cf=local_cf,
        limb_scale=limb_scale,
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
