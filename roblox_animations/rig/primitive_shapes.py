"""
Roblox primitive shape generation and static-part batching.

Geometry for Block/Ball/Cylinder/Wedge/CornerWedge parts matching Studio's
OBJ export, canonical per-face UVs, and the numpy batch path that merges
compatible static place primitives into one mesh object.
"""

import bpy
from mathutils import Matrix

from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat
from .mesh_surface import (
    _bulk_set,
    _populate_mesh_geometry,
    _set_mesh_smooth_shading,
)


def _build_primitive_mesh_data(entry, include_surface_data=True):
    """Generate synthetic mesh_data from a Roblox primitive Part shape.

    Shape geometry matches Roblox Studio's OBJ export (Block / Ball / Cylinder /
    Wedge / CornerWedge).  Returns a filemesh-style dict with positions, faces,
    normals, uvs, bone_names, and vertex_weights.
    """
    import math as _math
    if include_surface_data:
        from mathutils import Vector

    shape = entry.get("shape", "block")
    size = entry.get("part_size") or [4.0, 1.0, 2.0]
    hx, hy, hz = float(size[0]) / 2.0, float(size[1]) / 2.0, float(size[2]) / 2.0

    positions = []
    faces = []

    def _emit_quad(v0, v1, v2, v3):
        i = len(positions)
        positions.extend([v0, v1, v2, v3])
        faces.append((i, i+1, i+2))
        faces.append((i, i+2, i+3))

    # ── Block (Roblox Shape 1, default Part) ──────────────────────────
    if shape in ("block", 1):
        # 6 faces × 4 verts = 24 verts, 12 tris (matches OBJ).  Winding is
        # OUTWARD (Blender derives face normals from the vertex order, so an
        # inward-wound quad renders with an inverted normal — and inverts the
        # stud bevel's normal-map shading along with it).
        _emit_quad((-hx, -hy, -hz), (-hx,  hy, -hz), (hx,  hy, -hz), (hx, -hy, -hz))  # -Z
        _emit_quad((-hx, -hy,  hz), (hx, -hy,  hz), (hx,  hy,  hz), (-hx,  hy,  hz))  # +Z
        _emit_quad((-hx,  hy, -hz), (-hx,  hy,  hz), (hx,  hy,  hz), (hx,  hy, -hz))  # +Y
        _emit_quad((-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy,  hz), (-hx, -hy,  hz))  # -Y
        _emit_quad((hx, -hy, -hz), (hx,  hy, -hz), (hx,  hy,  hz), (hx, -hy,  hz))  # +X
        _emit_quad((-hx, -hy, -hz), (-hx, -hy,  hz), (-hx,  hy,  hz), (-hx,  hy, -hz))  # -X

    # ── Cylinder (Roblox Shape 2) ─────────────────────────────────────
    elif shape in ("cylinder", 2):
        # Caps at ±X, circular cross-section in YZ.  Circle is always 1:1
        # (Roblox does not stretch it); radius is the smaller cross-section
        # dimension.  16 segments = 2 + 2×16 verts, 16×2 + 16×2×2 = 96 tris.
        segs = 16
        r = min(hy, hz)  # circular cross-section, not elliptical
        # +X cap center
        pc = len(positions)
        positions.append((hx, 0, 0))
        # +X cap ring
        for i in range(segs):
            a = 2.0 * _math.pi * i / segs
            positions.append((hx, r * _math.cos(a), r * _math.sin(a)))
        # +X cap fans
        for i in range(segs):
            ni = (i + 1) % segs
            faces.append((pc, pc + 1 + i, pc + 1 + ni))
        # -X cap center
        nc = len(positions)
        positions.append((-hx, 0, 0))
        # -X cap ring
        for i in range(segs):
            a = 2.0 * _math.pi * i / segs
            positions.append((-hx, r * _math.cos(a), r * _math.sin(a)))
        # -X cap fans (reverse winding)
        for i in range(segs):
            ni = (i + 1) % segs
            faces.append((nc, nc + 1 + ni, nc + 1 + i))
        # Side quads (wound outward: +X ring vertex -> -X ring vertex -> next -X).
        for i in range(segs):
            ni = (i + 1) % segs
            t0 = pc + 1 + i
            t1 = pc + 1 + ni
            b0 = nc + 1 + i
            b1 = nc + 1 + ni
            faces.append((t0, b0, b1))
            faces.append((t0, b1, t1))

    # ── Ball / Sphere (Roblox Shape 0) ────────────────────────────────
    elif shape in ("ball", 0):
        # Sphere is always 1:1:1 — Roblox does not stretch it.  Radius is
        # the smallest half-extent so the sphere is inscribed in the bounding
        # box. The latitude rings intentionally stop short of the poles, so
        # add explicit pole vertices and triangle fans; without them balls
        # have two open caps.
        rings, segs = 14, 21
        r = min(hx, hy, hz)
        south_pole = len(positions)
        positions.append((-r, 0.0, 0.0))
        for ring in range(rings):
            phi = _math.pi * (ring + 0.5) / rings - _math.pi / 2.0
            cos_phi = _math.cos(phi)
            sin_phi = _math.sin(phi)
            for s in range(segs):
                theta = 2.0 * _math.pi * s / segs
                x = r * sin_phi
                y = r * cos_phi * _math.cos(theta)
                z = r * cos_phi * _math.sin(theta)
                positions.append((x, y, z))
        north_pole = len(positions)
        positions.append((r, 0.0, 0.0))
        # South pole faces wind outward along -X.
        for s in range(segs):
            current = 1 + s
            next_index = 1 + (s + 1) % segs
            faces.append((south_pole, next_index, current))
        for ring in range(rings - 1):
            for s in range(segs):
                a = 1 + ring * segs + s
                b = 1 + ring * segs + (s + 1) % segs
                c = 1 + (ring + 1) * segs + (s + 1) % segs
                d = 1 + (ring + 1) * segs + s
                faces.append((a, b, c))
                faces.append((a, c, d))
        # North pole faces wind outward along +X.
        last_ring = 1 + (rings - 1) * segs
        for s in range(segs):
            current = last_ring + s
            next_index = last_ring + (s + 1) % segs
            faces.append((north_pole, current, next_index))

    # ── Wedge (Shape 3 / WedgePart) ────────────────────────────────────
    elif shape == "wedge":
        # Right triangular prism.  5 faces = 20 verts, 10 tris (matches OBJ).
        # Cross-section in YZ: triangle at y=-hy spans z=-hz..+hz;
        # at y=+hy only exists at z=+hz.  Extruded along X.
        # +Z face (full rectangle)
        _emit_quad((-hx, -hy,  hz), (hx, -hy,  hz), (hx,  hy,  hz), (-hx,  hy,  hz))
        # +X face (triangle: z=-hz..+hz along y=-hy, then up to y=+hy at z=+hz),
        # wound so the triangle normal points +X.
        i = len(positions)
        positions.extend([(hx, hy, hz), (hx, hy, hz), (hx, -hy, hz), (hx, -hy, -hz)])
        faces.append((i, i+2, i+3))  # tri 1
        faces.append((i, i+2, i+3))  # tri 2 (degenerate, same as OBJ)
        # -X face (same triangle, mirrored), wound so the normal points -X.
        i = len(positions)
        positions.extend([(-hx, hy, hz), (-hx, hy, hz), (-hx, -hy, -hz), (-hx, -hy, hz)])
        faces.append((i, i+2, i+3))
        faces.append((i, i+2, i+3))
        # Bottom face (y=-hy, full rectangle)
        _emit_quad((-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy,  hz), (-hx, -hy,  hz))
        # Slope face (connects +Z top edge to -Z bottom edge)
        _emit_quad((-hx,  hy,  hz), (hx,  hy,  hz), (hx, -hy, -hz), (-hx, -hy, -hz))

    # ── CornerWedge (Shape 4 / CornerWedgePart) ────────────────────────
    elif shape == "corner_wedge":
        # Tetrahedron-like shape with a tall corner at (+hx, +hy, -hz)
        # and a rectangular base at y=-hy.  5 faces = 20 verts, 10 tris.
        # Unique vertices:
        #   A = ( hx,  hy, -hz)  — tall corner
        #   B = ( hx, -hy, -hz)
        #   C = ( hx, -hy,  hz)
        #   D = (-hx, -hy,  hz)
        #   E = (-hx, -hy, -hz)
        A = (hx,  hy, -hz)
        B = (hx, -hy, -hz)
        C = (hx, -hy,  hz)
        D = (-hx, -hy,  hz)
        E = (-hx, -hy, -hz)
        # Each triangle keeps the OBJ layout (A duplicated for the degenerate
        # tri) but is wound so its normal points AWAY from the solid.
        # +X face: triangle A-C-B (normal +X)
        i = len(positions)
        positions.extend([A, A, C, B])
        faces.append((i, i+2, i+3))  # A-C-B  (skips duplicate)
        faces.append((i, i+2, i+3))  # degenerate
        # Slope face 1: A-D-C (normal up/away from the base)
        i = len(positions)
        positions.extend([A, A, D, C])
        faces.append((i, i+2, i+3))  # A-D-C
        faces.append((i, i+2, i+3))
        # Slope face 2: A-E-D (normal away from the solid)
        i = len(positions)
        positions.extend([A, A, E, D])
        faces.append((i, i+2, i+3))  # A-E-D
        faces.append((i, i+2, i+3))
        # Bottom face (y=-hy): quad E-B-C-D
        _emit_quad(E, B, C, D)
        # -Z face: triangle A-B-E (normal -Z)
        i = len(positions)
        positions.extend([A, A, B, E])
        faces.append((i, i+2, i+3))  # A-B-E
        faces.append((i, i+2, i+3))

    else:
        return None

    n = len(positions)
    normals = []
    uvs = []
    loop_uvs = []
    face_surface_types = []
    if include_surface_data:
        # Compute per-face normals (flat shading, matches OBJ split-vertex layout)
        normals = [(0.0, 0.0, 1.0)] * n
        for face in faces:
            if len(face) < 3:
                continue
            i0, i1, i2 = int(face[0]), int(face[1]), int(face[2])
            if i0 >= n or i1 >= n or i2 >= n:
                continue
            p0 = Vector(positions[i0])
            p1 = Vector(positions[i1])
            p2 = Vector(positions[i2])
            edge1 = p1 - p0
            edge2 = p2 - p0
            normal = edge1.cross(edge2)
            if normal.length_squared > 1e-12:
                normal.normalize()
            else:
                normal = Vector((0.0, 0.0, 1.0))
            fn = (float(normal.x), float(normal.y), float(normal.z))
            normals[i0] = fn
            normals[i1] = fn
            normals[i2] = fn
            if len(face) >= 4:
                i3 = int(face[3])
                if i3 < n:
                    normals[i3] = fn

        if shape in ("block", 1):
            # Blocks carry Roblox's canonical per-face UVs (its OBJ export),
            # not a derived projection: u/v in band space, 64px/stud.
            loop_uvs = []
            uvs = [(0.0, 0.0)] * n
            for face_index, face in enumerate(faces):
                nx, ny, nz = _BLOCK_FACE_NORMALS[face_index]
                for index in face:
                    u_studs, v_studs = _block_canonical_stud_uvs(
                        positions[index], nx, ny, nz, hx, hy, hz
                    )
                    loop_uvs.append((u_studs * 0.5, v_studs * 0.125))
                    uvs[index] = (u_studs * 0.5, v_studs * 0.125)
        else:
            uv_entry = dict(entry)
            uv_entry["_primitive_positions"] = positions
            loop_uvs = _generate_primitive_loop_uvs(faces, uv_entry)
            uvs = _generate_face_uvs(faces, n, uv_entry)
        face_surface_types = _face_surface_types(entry, faces, shape)
    return {
        "positions": positions,
        "faces": faces,
        "normals": normals,
        "uvs": uvs,
        "loop_uvs": loop_uvs,
        # Per-triangle Roblox surface types, for textures.apply_part_material's
        # per-face material assignment.
        "face_surface_types": face_surface_types,
        "bone_names": [],
        "vertex_weights": [{} for _ in range(n)],
    }


# Roblox's classic-surface atlas (Part1_diff.png / Part1_nmap.png) is split by
# textures.py into per-band 128x512 tileable images (Studs/Glue/Inlet/
# Universal).  The stud pattern runs at 64px per stud in BOTH axes (verified
# against the atlas cells: one 64x64 cell == one stud), so as fractions of a
# 128x512 band image:
_U_UNITS_PER_STUD = 64.0 / 128.0    # 0.5  — one stud spans half the band width
_V_UNITS_PER_STUD = 64.0 / 512.0    # 0.125 — one stud spans 1/8 of the band height
# The band images wrap with REPEAT, so values beyond [0,1] tile seamlessly.


def _generate_face_uvs(faces, vertex_count, entry):
    """Generate per-face UVs for Roblox's classic surface textures.

    Each face is planar-projected onto its two dominant in-plane axes at an
    isotropic 64px/stud, matching the stud cells of the surface bands.  Which
    band (Studs/Inlet/Glue/Universal) a face samples is decided by the
    per-face material assignment in textures.apply_part_material, so the UVs
    themselves are band-agnostic — they only need the correct stud density.
    """
    uvs = [(0.0, 0.0)] * vertex_count
    positions = entry.get("_primitive_positions") or []

    for face in faces:
        idxs = [int(i) for i in face if 0 <= int(i) < vertex_count]
        if len(idxs) < 3:
            continue

        # Planar-project this face onto its two dominant in-plane axes.
        pts = [positions[i] for i in idxs]
        ax_u, ax_v, u_sign, _ = _primitive_face_projection_axes(pts)

        # Anchor each face at a texture-cell boundary.  Using the centred local
        # coordinates directly starts odd-sized faces halfway through a stud:
        # a 27-stud face then shows 28 fragments (two half studs at its ends)
        # instead of Roblox's 27 complete studs.
        origin_u = min(point[ax_u] * u_sign for point in pts)
        origin_v = min(point[ax_v] for point in pts)
        for i, p in zip(idxs, pts):
            uvs[i] = (
                (p[ax_u] * u_sign - origin_u) * _U_UNITS_PER_STUD,
                (p[ax_v] - origin_v) * _V_UNITS_PER_STUD,
            )

    return uvs


def _generate_primitive_loop_uvs(faces, entry):
    """Generate independent UVs for every primitive face corner.

    Curved primitives reuse ring vertices between caps/sides, but those faces
    have different projection axes and origins. A vertex UV array cannot
    represent that seam: whichever face is visited last wins. Blender UVs are
    per loop, so retain one value per emitted face corner instead.
    """
    positions = entry.get("_primitive_positions") or []
    loop_uvs = []
    for face in faces:
        idxs = [int(index) for index in face if 0 <= int(index) < len(positions)]
        if len(idxs) < 3:
            loop_uvs.extend([(0.0, 0.0)] * len(face))
            continue
        points = [positions[index] for index in idxs]
        ax_u, ax_v, u_sign, _ = _primitive_face_projection_axes(points)
        origin_u = min(point[ax_u] * u_sign for point in points)
        origin_v = min(point[ax_v] for point in points)
        loop_uvs.extend(
            (
                (point[ax_u] * u_sign - origin_u) * _U_UNITS_PER_STUD,
                (point[ax_v] - origin_v) * _V_UNITS_PER_STUD,
            )
            for point in points
        )
    return loop_uvs


# Roblox's per-face surface order is Back, Front, Top, Bottom, Right, Left,
# matching surface_types indices 0..5 parsed from the rbxm.
def _face_surface_types(entry, faces, shape):
    """Map each generated triangle to its Roblox surface type.

    Block faces are emitted as 6 quads (2 tris each) in the same order Roblox
    reports surfaces: -Z=Back, +Z=Front, +Y=Top, -Y=Bottom, +X=Right, -X=Left.
    Wedge/CornerWedge faces map onto their nearest Roblox surface.  Curved
    shapes (cylinder/ball) use the dominant textured type on every face.
    """
    sts = entry.get("surface_types") or [0] * 6
    n = len(faces)

    if shape in ("block", 1):
        return [sts[min(t // 2, 5)] for t in range(n)]
    if shape == "wedge":
        # +Z rect=Back, +X tri=Right, -X tri=Left, bottom=Bottom, slope=Top
        order = (0, 4, 5, 3, 2)
        return [sts[order[min(t // 2, 4)]] for t in range(n)]
    if shape == "corner_wedge":
        # +X tri=Right, slopes=Top/Front, bottom=Bottom, -Z tri=Back
        order = (4, 2, 1, 3, 0)
        return [sts[order[min(t // 2, 4)]] for t in range(n)]
    # cylinder / ball: no per-face mapping — use the dominant textured type.
    dominant = 0
    for cand in (3, 4, 1, 5):
        if cand in sts:
            dominant = cand
            break
    return [dominant] * n


# Roblox Studio's OBJ export emits block faces as quads (two triangles each)
# in the order -Z, +Z, +Y, -Y, +X, -X; this table gives each emitted
# triangle's face normal so canonical per-face UVs can be applied per loop.
_BLOCK_FACE_NORMALS = (
    (0.0, 0.0, -1.0), (0.0, 0.0, -1.0),
    (0.0, 0.0, 1.0), (0.0, 0.0, 1.0),
    (0.0, 1.0, 0.0), (0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0), (0.0, -1.0, 0.0),
    (1.0, 0.0, 0.0), (1.0, 0.0, 0.0),
    (-1.0, 0.0, 0.0), (-1.0, 0.0, 0.0),
)


def _block_canonical_stud_uvs(point, nx, ny, nz, hx, hy, hz):
    """Roblox's canonical block face UVs (parts.obj), in studs.

    U runs along the face's horizontal axis, flipped with the face normal so
    the pattern reads correctly from outside every face.  V runs along the
    face's vertical.  Anchors match the OBJ export exactly: side faces anchor
    V at the top edge, top/bottom faces anchor U at the +X edge.
    """
    if nz > 0:
        return (point[0] + hx, hy - point[1])  # +Z: U=+X
    if nz < 0:
        return (hx - point[0], hy - point[1])  # -Z: U=-X
    if ny:
        return (hx - point[0], hz - point[2])  # +/-Y: U=-X, V=-Z
    if nx > 0:
        return (hz - point[2], hy - point[1])  # +X: U=-Z
    return (hz + point[2], hy - point[1])  # -X: U=+Z


def _primitive_face_projection_axes(pts):
    """Pick the two dominant in-plane world axes for a polygonal face.

    Returns (axis_u, axis_v, u_sign, (len_u, len_v)) where axes are indices
    0/1/2 and lengths are the face's world extents along those axes.  The U
    direction flips with the face normal sign so the pattern reads correctly
    viewed from OUTSIDE every face, matching textures._face_projection_axes.
    """
    # Face normal from the first non-degenerate triangle.
    nx = ny = nz = 0.0
    for k in range(1, len(pts) - 1):
        ax, ay, az = pts[0]
        bx, by, bz = pts[k]
        cx, cy, cz = pts[k + 1]
        e1 = (bx - ax, by - ay, bz - az)
        e2 = (cx - ax, cy - ay, cz - az)
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]
        if nx * nx + ny * ny + nz * nz > 1e-12:
            break
    # Drop the dominant normal axis; project onto the other two.  V (the
    # image's vertical) always follows the part's local up on vertical
    # faces, so patterns stay upright: X walls project (Z, Y), Y faces
    # (X, Z), Z walls (X, Y).
    axn, ayn, azn = abs(nx), abs(ny), abs(nz)
    if axn >= ayn and axn >= azn:
        axes = (2, 1)  # normal ~X -> project Z (u), Y (v)
        u_sign = -1.0 if nx > 0 else 1.0  # +X: U=-Z, -X: U=+Z
    elif ayn >= axn and ayn >= azn:
        axes = (0, 2)  # normal ~Y -> project X,Z
        u_sign = 1.0
    else:
        axes = (0, 1)  # normal ~Z -> project X,Y
        u_sign = 1.0 if nz > 0 else -1.0  # +Z: U=+X, -Z: U=-X

    def _extent(axis):
        vals = [p[axis] for p in pts]
        return max(vals) - min(vals)

    a0, a1 = axes
    return a0, a1, u_sign, (_extent(a0), _extent(a1))


def _create_batched_static_primitives(parts_collection, batch_name, entries, material=None):
    """Create one mesh for compatible static place primitives.

    Their Roblox transforms are baked into the vertex positions, so batching
    has no visible transform cost. Rigged parts and textured/special-surface
    parts stay on the normal per-object path.
    """
    if not entries:
        return None
    needs_material_uv = False
    needs_color_attr = False
    units = 0.5
    uv_layer_name = "RBXMaterialUV"
    try:
        from .textures import (
            builtin_material_texture_refs,
            entry_uses_shared_builtin_tint,
            _BUILTIN_MATERIAL_STUDS_PER_TILE,
            _BUILTIN_MATERIAL_UV_LAYER,
        )

        needs_material_uv = bool(builtin_material_texture_refs(entries[0]))
        needs_color_attr = entry_uses_shared_builtin_tint(entries[0])
        units = 1.0 / _BUILTIN_MATERIAL_STUDS_PER_TILE
        uv_layer_name = _BUILTIN_MATERIAL_UV_LAYER
    except Exception:
        pass
    try:
        import numpy as np  # bundled with Blender

        return _create_batched_static_primitives_np(
            np, parts_collection, batch_name, entries, material,
            needs_material_uv=needs_material_uv,
            needs_color_attr=needs_color_attr,
            units=units, uv_layer_name=uv_layer_name,
        )
    except Exception:
        pass

    positions = []
    faces = []
    uvs = []
    t2b = get_transform_to_blender()
    for entry in entries:
        mesh_data = _build_primitive_mesh_data(entry, include_surface_data=False)
        if mesh_data is None:
            continue
        entry_faces = mesh_data.get("faces") or []
        raw_positions = mesh_data.get("positions") or []
        if not raw_positions or not entry_faces:
            continue
        transform = Matrix.Identity(4)
        if entry.get("part_cf"):
            try:
                transform = t2b @ cf_to_mat(entry["part_cf"])
            except Exception:
                continue
        m00, m01, m02, m03 = transform[0]
        m10, m11, m12, m13 = transform[1]
        m20, m21, m22, m23 = transform[2]
        offset = len(positions)
        positions.extend(
            (
                m00 * position[0] + m01 * position[1] + m02 * position[2] + m03,
                m10 * position[0] + m11 * position[1] + m12 * position[2] + m13,
                m20 * position[0] + m21 * position[1] + m22 * position[2] + m23,
            )
            for position in raw_positions
        )
        faces.extend(
            tuple(offset + int(vertex_index) for vertex_index in face[:3])
            for face in entry_faces
        )
        if needs_material_uv:
            for face in entry_faces:
                idxs = [int(index) for index in face[:3] if 0 <= int(index) < len(raw_positions)]
                pts = [raw_positions[index] for index in idxs]
                if len(pts) < 3:
                    continue
                ax_u, ax_v, u_sign, _ = _primitive_face_projection_axes(pts)
                origin_u = min(point[ax_u] * u_sign for point in pts)
                origin_v = min(point[ax_v] for point in pts)
                uvs.extend(
                    (
                        (point[ax_u] * u_sign - origin_u) * units,
                        (point[ax_v] - origin_v) * units,
                    )
                    for point in pts
                )

    if not positions or not faces:
        return None

    # Blender datablock creation suffixes names itself; scanning every object
    # for uniqueness here was O(objects) per batch.
    mesh = bpy.data.meshes.new(f"mesh_{batch_name}")
    if not _populate_mesh_geometry(mesh, positions, faces):
        bpy.data.meshes.remove(mesh)
        return None
    mesh_obj = bpy.data.objects.new(batch_name, mesh)
    mesh_obj["RBXSynthesizedPart"] = True
    mesh_obj["RBXStaticBatch"] = True
    mesh_obj["RBXStaticPartCount"] = len(entries)
    _set_mesh_smooth_shading(mesh)
    if needs_material_uv and uvs:
        try:
            uv_layer = mesh.uv_layers.new(name=uv_layer_name)
            _bulk_set(uv_layer.data, "uv", uvs)
        except Exception:
            pass
    if material is not None:
        mesh.materials.append(material)
    else:
        try:
            from .textures import apply_part_material
            apply_part_material(mesh_obj, entries[0])
        except Exception as exc:
            print(f"[RigCreate] Batched material build failed for '{batch_name}': {exc}")
    if needs_material_uv or needs_color_attr:
        # Cached-material batches never pass through apply_part_material, so
        # the RBXColor attribute (the built-in tint) must be ensured here.
        # Both helpers are no-ops when the data already exists.
        try:
            from .textures import (
                _ensure_builtin_material_uvs,
                _ensure_builtin_material_color,
            )
            if needs_material_uv:
                _ensure_builtin_material_uvs(mesh)
            _ensure_builtin_material_color(
                mesh, entries[0].get("color") or (1.0, 1.0, 1.0)
            )
        except Exception:
            pass
    parts_collection.objects.link(mesh_obj)
    return mesh_obj


_PRIMITIVE_SHAPE_TEMPLATE_CACHE = {}


def _primitive_shape_template(np, shape):
    """Return cached (unit_positions, faces, uv_coeffs, corner_axes).

    Positions use unit half-extents (a block spans -1..1), so a part's real
    local positions are ``unit_positions * part_size``.  Faces are fixed per
    shape.  ``uv_coeffs`` holds per face corner ``(u, v)`` coefficients in
    unit local space; multiplying column 0/1 by the part size along that
    face's projection axes reproduces the classic per-part planar projection
    exactly.  The projection axes are size-independent for block, cylinder,
    and ball; wedge/corner_wedge slope faces compare the normal's y/z
    magnitudes, which flip under anisotropic part sizes, so those shapes
    return None and stay on the per-part path.
    """
    if shape in ("wedge", "corner_wedge"):
        _PRIMITIVE_SHAPE_TEMPLATE_CACHE[shape] = None
        return None
    template = _PRIMITIVE_SHAPE_TEMPLATE_CACHE.get(shape)
    if template is not None:
        return template
    data = _build_primitive_mesh_data(
        {"shape": shape, "part_size": [2.0, 2.0, 2.0]},
        include_surface_data=False,
    )
    if not data or not data.get("positions") or not data.get("faces"):
        _PRIMITIVE_SHAPE_TEMPLATE_CACHE[shape] = None
        return None
    unit_positions = np.asarray(data["positions"], dtype=np.float64) * 0.5
    template_faces = np.asarray(data["faces"], dtype=np.int64)[:, :3]
    face_count = len(template_faces)
    corner_axes = np.empty((face_count, 2), dtype=np.int64)
    uv_coeffs = np.empty((face_count * 3, 2), dtype=np.float64)
    for face_index, face in enumerate(template_faces):
        points = unit_positions[face]
        ax_u, ax_v, u_sign, _ = _primitive_face_projection_axes(points)
        corner_axes[face_index] = (ax_u, ax_v)
        if shape in ("block", 1):
            # Coeffs come from the canonical unit-block loop UVs below.
            continue
        signed_u = points[:, ax_u] * u_sign
        uv_coeffs[face_index * 3:face_index * 3 + 3, 0] = (
            signed_u - signed_u.min()
        )
        uv_coeffs[face_index * 3:face_index * 3 + 3, 1] = (
            points[:, ax_v] - points[:, ax_v].min()
        )
    if shape in ("block", 1):
        # Roblox's canonical block UVs, in studs per unit-size: loop UVs of a
        # unit block in band space, divided by the band units (u 0.5/stud,
        # v 0.125/stud) so the caller's `size * units` recovers the mapping.
        unit_data = _build_primitive_mesh_data(
            {"shape": "block", "part_size": [2.0, 2.0, 2.0]},
            include_surface_data=True,
        )
        unit_uvs = np.asarray(unit_data["loop_uvs"], dtype=np.float64)
        uv_coeffs[:, 0] = unit_uvs[:, 0]
        uv_coeffs[:, 1] = unit_uvs[:, 1] / 0.25
    template = (
        unit_positions,
        template_faces,
        uv_coeffs,
        corner_axes.repeat(3, axis=0),
    )
    _PRIMITIVE_SHAPE_TEMPLATE_CACHE[shape] = template
    return template


def _static_primitive_part_arrays(
    np, entry, b_linear, b_translation, needs_material_uv, units, vertex_offset
):
    """Per-part arrays for shapes without a cached template."""
    mesh_data = _build_primitive_mesh_data(entry, include_surface_data=False)
    if mesh_data is None:
        return None
    entry_faces = mesh_data.get("faces") or []
    raw_positions = mesh_data.get("positions") or []
    if not raw_positions or not entry_faces:
        return None
    identity_cf = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    cf = entry.get("part_cf") or identity_cf
    rotation = np.asarray(cf[3:12], dtype=np.float64).reshape(3, 3)
    pos_np = np.asarray(raw_positions, dtype=np.float64)
    if pos_np.ndim != 2 or pos_np.shape[1] < 3:
        return None
    world = (
        pos_np[:, :3] @ (b_linear @ rotation).T
        + (b_linear @ np.asarray(cf[:3], dtype=np.float64) + b_translation)
    ).astype(np.float32)
    face_np = np.asarray(entry_faces, dtype=np.int64)[:, :3] + vertex_offset
    uv_flat = None
    if needs_material_uv:
        uv_flat = np.empty((len(face_np) * 3, 2), dtype=np.float32)
        for row, face in enumerate(face_np):
            points = pos_np[face - vertex_offset, :3]
            ax_u, ax_v, u_sign, _ = _primitive_face_projection_axes(points)
            signed_u = points[:, ax_u] * u_sign
            uv_flat[row * 3:row * 3 + 3, 0] = (
                signed_u - signed_u.min()
            ) * units
            uv_flat[row * 3:row * 3 + 3, 1] = (
                points[:, ax_v] - points[:, ax_v].min()
            ) * units
    return world, face_np, uv_flat


def _create_batched_static_primitives_np(
    np, parts_collection, batch_name, entries, material=None,
    needs_material_uv=False, needs_color_attr=False,
    units=0.5, uv_layer_name="RBXMaterialUV",
):
    """numpy batch path: cached templates, bulk transform per shape group."""
    t2b = np.asarray(get_transform_to_blender(), dtype=np.float64)
    b_linear = t2b[:3, :3]
    b_translation = t2b[:3, 3]
    default_size = (4.0, 1.0, 2.0)
    identity_cf = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

    grouped = {}
    for entry in entries:
        shape = entry.get("shape", "block")
        if _primitive_shape_template(np, shape) is not None:
            grouped.setdefault(shape, []).append(entry)
        else:
            grouped.setdefault(None, []).append(entry)

    parts_pos = []
    parts_faces = []
    parts_uv = []
    vertex_offset = 0
    for shape, group in grouped.items():
        if shape is None:
            # Unknown shapes keep the per-part path.
            for entry in group:
                arrays = _static_primitive_part_arrays(
                    np, entry, b_linear, b_translation,
                    needs_material_uv, units, vertex_offset,
                )
                if arrays is None:
                    continue
                pos_np, face_np, uv_flat = arrays
                parts_pos.append(pos_np)
                parts_faces.append(face_np)
                if uv_flat is not None:
                    parts_uv.append(uv_flat)
                vertex_offset += len(pos_np)
            continue
        unit_positions, template_faces, uv_coeffs, corner_axes = (
            _primitive_shape_template(np, shape)
        )
        count = len(group)
        # Extract per-entry arrays with a python loop: it is the same few
        # microseconds per part as the list comprehension, but malformed or
        # ragged part_size/part_cf values skip only their own part instead
        # of throwing and demoting the whole batch to the slow fallback.
        sizes = np.empty((count, 3), dtype=np.float64)
        cfs = np.empty((count, 12), dtype=np.float64)
        kept = 0
        for entry in group:
            size = entry.get("part_size") or default_size
            cf = entry.get("part_cf") or identity_cf
            try:
                size = tuple(size)[:3]
                cf = tuple(cf)[:12]
            except (TypeError, ValueError):
                continue
            if len(size) < 3 or len(cf) < 12:
                continue
            sizes[kept] = size
            cfs[kept] = cf
            kept += 1
        if not kept:
            continue
        sizes = sizes[:kept]
        cfs = cfs[:kept]
        count = kept
        rotations = cfs[:, 3:12].reshape(count, 3, 3)
        combined = np.einsum("ij,njk->nik", b_linear, rotations)
        translations = (
            np.einsum("ij,nj->ni", b_linear, cfs[:, :3]) + b_translation
        )
        local = unit_positions[None, :, :] * sizes[:, None, :]
        world = (
            np.einsum("nij,nvj->nvi", combined, local)
            + translations[:, None, :]
        )
        parts_pos.append(world.reshape(-1, 3).astype(np.float32))
        parts_faces.append(
            (
                template_faces[None, :, :]
                + np.arange(count, dtype=np.int64)[:, None, None] * len(unit_positions)
                + vertex_offset
            ).reshape(-1, 3)
        )
        if needs_material_uv:
            size_u = sizes[:, corner_axes[:, 0]]
            size_v = sizes[:, corner_axes[:, 1]]
            group_uvs = np.empty((count, corner_axes.shape[0], 2), dtype=np.float32)
            group_uvs[:, :, 0] = uv_coeffs[None, :, 0] * size_u * units
            group_uvs[:, :, 1] = uv_coeffs[None, :, 1] * size_v * units
            parts_uv.append(group_uvs.reshape(-1, 2))
        vertex_offset += count * len(unit_positions)

    if not parts_pos:
        return None

    positions = np.concatenate(parts_pos)
    faces = np.concatenate(parts_faces)

    mesh = bpy.data.meshes.new(f"mesh_{batch_name}")
    mesh_obj = None
    try:
        if not _populate_mesh_geometry(mesh, positions, faces):
            raise ValueError("geometry upload failed")
        mesh_obj = bpy.data.objects.new(batch_name, mesh)
        mesh_obj["RBXSynthesizedPart"] = True
        mesh_obj["RBXStaticBatch"] = True
        mesh_obj["RBXStaticPartCount"] = len(entries)
        _set_mesh_smooth_shading(mesh)
        if needs_material_uv and parts_uv:
            uv_layer = mesh.uv_layers.new(name=uv_layer_name)
            _bulk_set(uv_layer.data, "uv", np.concatenate(parts_uv).reshape(-1))
        if material is not None:
            mesh.materials.append(material)
        else:
            try:
                from .textures import apply_part_material
                apply_part_material(mesh_obj, entries[0])
            except Exception as exc:
                print(f"[RigCreate] Batched material build failed for '{batch_name}': {exc}")
        if needs_material_uv or needs_color_attr:
            # Cached-material batches never pass through apply_part_material,
            # so the RBXColor attribute (the built-in tint) must be ensured
            # here.  Both helpers are no-ops when the data already exists.
            try:
                from .textures import (
                    _ensure_builtin_material_uvs,
                    _ensure_builtin_material_color,
                )
                if needs_material_uv:
                    _ensure_builtin_material_uvs(mesh)
                _ensure_builtin_material_color(
                    mesh, entries[0].get("color") or (1.0, 1.0, 1.0)
                )
            except Exception:
                pass
        parts_collection.objects.link(mesh_obj)
        return mesh_obj
    except Exception:
        if mesh_obj is not None and mesh_obj.name in bpy.data.objects:
            bpy.data.objects.remove(mesh_obj, do_unlink=True)
        elif mesh.name in bpy.data.meshes:
            bpy.data.meshes.remove(mesh)
        raise
