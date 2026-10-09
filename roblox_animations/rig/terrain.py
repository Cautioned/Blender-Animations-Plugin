"""Terrain support: decode the SmoothGrid blob and build Blender meshes.

Roblox stores smooth terrain as an RLE stream of 32x32x32-cell chunks.
The chunk ids are delta-encoded as three interleaved big-endian int32s
(byte-triples from most significant to least), and chunk cell data runs
y-major then z then x.  Each RLE run is:

    lead byte:  [run-present 0x80][occ-present 0x40][material 6 bits]
    optional occupancy byte (present => value; absent => 255 solid)
    optional run byte (present => run length - 1, max 256)

A cell is air when occupancy == 0.  Material values index the Terrain
instance's MaterialColors RGB table.

Imports mesh terrain as ONE object: every material's built-in PBR maps are
blended by per-vertex splat weights (texture splatting) instead of hard
per-material seams.  Water stays a separate transmission-shaded surface.
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

_STUDS_PER_VOXEL = 4.0
_SOLID_OCCUPANCY_MIN = 128

# The smooth-grid RLE stores a 6-bit material INDEX into the engine's fixed
# terrain material table (the same order as the Terrain.MaterialColors
# palette).  Slot -> modern Enum.Material value, verified against
# Terrain:GetMaterialColor defaults in Studio.  0 is air (never stored),
# 1 is water (colour comes from the WaterColor property, not the palette).
# Note the order is NOT the modern enum order: Brick sits next to Concrete,
# CrackedLava owns the red slot, WoodPlanks is a terrain material while
# Pebble is not, and Limestone/Pavement sit at the very end.
_TERRAIN_MATERIAL_ENUM = {
    1: 2048,    # Water
    2: 1280,    # Grass
    3: 800,     # Slate
    4: 816,     # Concrete
    5: 848,     # Brick
    6: 1296,    # Sand
    7: 528,     # WoodPlanks
    8: 896,     # Rock
    9: 1552,    # Glacier
    10: 1328,   # Snow
    11: 912,    # Sandstone
    12: 1344,   # Mud
    13: 788,    # Basalt
    14: 1360,   # Ground
    15: 804,    # CrackedLava
    16: 1376,   # Asphalt
    17: 880,    # Cobblestone
    18: 1536,   # Ice
    19: 1284,   # LeafyGrass
    20: 1392,   # Salt
    21: 820,    # Limestone
    22: 836,    # Pavement
}

# Per-face quad corner order used during face extraction.  For each axis the
# ordering was chosen so the winding (cross of the first two edges) points
# along +axis for the plus faces and -axis for the minus faces.
_FACE_AXIS_ORDER = {
    # axis: (corner axis b, corner axis d)
    0: (1, 2),
    1: (2, 0),
    2: (0, 1),
}

# Corner colour attribute holding each vertex's splat weight for material
# column k: the splat shader reads RBXSplat0..N via Vertex Color nodes.
# FLOAT_COLOR (not BYTE_COLOR) so the shader sees raw linear weights; byte
# attributes would be sRGB-decoded on read and stop summing to 1.
_SPLAT_WEIGHT_LAYER = "RBXSplat"

# How far a Voronoi cell may rotate its texture samples (radians).  The
# shared field's cell colour drives the Mapping rotation directly, so 1.0
# caps the spin at ~57 degrees — the classic organic-tiling strength that
# breaks up repeating tiles.  Rotation applies everywhere (not border
# gated): every material shares ONE field, so crossfaded textures stay in
# the same frame, and the swizzled triplanar normals are excluded so
# their tangent frame is never corrupted.
_SPLAT_ROTATION_RANGE = 1.0

# Border dither: amplitude and Voronoi cell size (the shared rotation
# field reuses the same cell size).  Position is in tile units (world/10),
# so Scale 1.0 gives ~10-stud cells.  The cell-colour dither is bounded
# and centred, so the amplitude is the peak border excursion in weight
# units; 1.0 is the maximum the bounded source can deliver and shifts the
# crossfade by ~2 studs (with ~5-stud cells) — anything lower was
# invisible next to the 10-stud texture tiles.
_SPLAT_DITHER_AMPLITUDE = 1.0
_SPLAT_DITHER_SCALE = 2.0

# Crossfade ramp window on the dithered weight.  A narrow window (e.g.
# 0.55-0.65) collapses the crossfade to a razor-thin near-binary edge —
# the two textures hard-switch with noise instead of blending.  A wide
# window turns the border into a smooth gradient, with the dither only
# gently wiggling it.
_SPLAT_RAMP_LO = 0.25
_SPLAT_RAMP_HI = 0.75

# Every material's texture chain collapses into a per-material node group
# (maps bake inside: group image sockets only exist in newer Blender
# builds).  The key stamps the exact visual contents so a re-import reuses
# the group only while tint and map refs still match.
_SPLAT_SLOT_VERSION = 5
_SPLAT_SLOT_INPUTS = (
    ("Weight", "NodeSocketFloat"),
    ("Cell Color", "NodeSocketVector"),
    ("Position", "NodeSocketVector"),
    ("Border", "NodeSocketFloat"),
    ("Blend X", "NodeSocketFloat"),
    ("Blend Y", "NodeSocketFloat"),
    ("Blend Z", "NodeSocketFloat"),
)
_SPLAT_SLOT_OUTPUTS = (
    ("Color", "NodeSocketColor"),
    ("Roughness", "NodeSocketFloat"),
    ("Metalness", "NodeSocketFloat"),
    ("Normal", "NodeSocketVector"),
)


def smooth_grid_material_enums(data: bytes) -> tuple[int, ...]:
    """Read only the used terrain materials without expanding cell arrays."""
    if isinstance(data, str):
        data = data.encode("latin-1")
    if not data or len(data) < 2:
        return ()
    version, log2 = data[0], data[1]
    if version != 1 or log2 > 8:
        return ()
    offset = 2
    cells_per_chunk = (1 << log2) ** 3
    used = set()
    while offset < len(data):
        if offset + 12 > len(data):
            return ()
        offset += 12
        consumed = 0
        while consumed < cells_per_chunk:
            if offset >= len(data):
                return ()
            lead = data[offset]
            offset += 1
            material_index = lead & 0x3F
            if lead & 0x40:
                if offset >= len(data):
                    return ()
                offset += 1
            run = 1
            if lead & 0x80:
                if offset >= len(data):
                    return ()
                run = data[offset] + 1
                offset += 1
            if run > cells_per_chunk - consumed:
                return ()
            material_enum = _TERRAIN_MATERIAL_ENUM.get(material_index)
            if material_enum is not None:
                used.add(material_enum)
            consumed += run
    return tuple(sorted(used))


def decode_smooth_grid_arrays(data: bytes):
    """Decode SmoothGrid bytes into five numpy arrays (x, y, z, mat, occ).

    Requires numpy (available inside Blender).  Any cell with a material is
    kept, including occupancy 0: solidity is material-based (like Mesher.cpp)
    and the occupancy field only steers vertex placement.  Runs are expanded
    with vectorized numpy slices into growable preallocated buffers, so the
    only python-loop work is one iteration per RLE run, not per cell.
    """
    import numpy as np

    if isinstance(data, str):
        data = data.encode("latin-1")
    if not data:
        return (
            np.empty(0, dtype=np.int32),
            np.empty(0, dtype=np.int32),
            np.empty(0, dtype=np.int32),
            np.empty(0, dtype=np.uint8),
            np.empty(0, dtype=np.uint8),
        )
    offset = 0
    version = data[offset]
    offset += 1
    log2 = data[offset]
    offset += 1
    if version != 1:
        raise ValueError(f"Unsupported SmoothGrid version {version}")
    if log2 > 8:
        raise ValueError(f"Unsupported SmoothGrid chunk size 2^{log2}")
    size = 1 << log2
    total = size * size * size
    capacity = 1 << 12
    xs = np.empty(capacity, dtype=np.int32)
    ys = np.empty(capacity, dtype=np.int32)
    zs = np.empty(capacity, dtype=np.int32)
    mats = np.empty(capacity, dtype=np.uint8)
    occs = np.empty(capacity, dtype=np.uint8)
    count = 0

    def _grow(old, new_capacity, keep):
        grown = np.empty(new_capacity, dtype=old.dtype)
        grown[:keep] = old[:keep]
        return grown

    last = [0, 0, 0]
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError(f"SmoothGrid chunk id overrun at byte {offset}")
        block = data[offset:offset + 12]
        offset += 12
        # Three int32 deltas, byte-interleaved big-endian.
        comps = []
        for c in range(3):
            comps.append(
                int.from_bytes(
                    bytes((block[c], block[c + 3], block[c + 6], block[c + 9])),
                    "big", signed=True,
                )
            )
        last = [last[0] + comps[0], last[1] + comps[1], last[2] + comps[2]]
        chunk_x, chunk_y, chunk_z = last
        consumed = 0
        while consumed < total:
            if offset >= len(data):
                raise ValueError(f"SmoothGrid chunk data overrun at byte {offset}")
            lead = data[offset]
            offset += 1
            material = lead & 0x3F
            occupancy = 255
            if lead & 0x40:
                occupancy = data[offset]
                offset += 1
            run = 1
            if lead & 0x80:
                run = data[offset] + 1
                offset += 1
            if run > total - consumed:
                raise ValueError(
                    f"SmoothGrid chunk overrun at chunk {tuple(last)} cell {consumed}"
                )
            if material != 0:
                needed = count + run
                if needed > capacity:
                    while capacity < needed:
                        capacity <<= 1
                    xs = _grow(xs, capacity, count)
                    ys = _grow(ys, capacity, count)
                    zs = _grow(zs, capacity, count)
                    mats = _grow(mats, capacity, count)
                    occs = _grow(occs, capacity, count)
                inds = np.arange(consumed, consumed + run, dtype=np.int64)
                xs[count:needed] = (chunk_x * size + (inds & (size - 1))).astype(np.int32)
                zs[count:needed] = (chunk_z * size + ((inds >> log2) & (size - 1))).astype(np.int32)
                ys[count:needed] = (chunk_y * size + (inds >> (2 * log2))).astype(np.int32)
                mats[count:needed] = material
                occs[count:needed] = occupancy
                count = needed
            consumed += run
    return (
        xs[:count],
        ys[:count],
        zs[:count],
        mats[:count],
        occs[:count],
    )


def decode_smooth_grid(data: bytes) -> List[Tuple[int, int, int, int, int]]:
    """Decode SmoothGrid bytes into (x, y, z, material, occupancy) cells."""
    xs, ys, zs, mats, occs = decode_smooth_grid_arrays(data)
    return list(zip(
        (int(v) for v in xs),
        (int(v) for v in ys),
        (int(v) for v in zs),
        (int(v) for v in mats),
        (int(v) for v in occs),
    ))


def decode_material_colors(data: Optional[bytes]) -> List[Tuple[float, float, float]]:
    """Decode the Terrain MaterialColors blob into a list of RGB floats."""
    if not data:
        return []
    colors = []
    for k in range(len(data) // 3):
        colors.append((
            data[3 * k] / 255.0,
            data[3 * k + 1] / 255.0,
            data[3 * k + 2] / 255.0,
        ))
    return colors


def _solid_mask(occs):
    """Studio treats occupancy <= 0.5 ((value+1)/256 <= 0.5) as non-solid."""
    return occs >= _SOLID_OCCUPANCY_MIN


def _downsample_grid(occ, mat, used, lod):
    """Halve the grid per axis `lod` times (2x2x2 cell blocks).

    A block is solid when any of its cells is solid (conservative: thin
    walls survive); its material is the block's majority material.
    """
    import numpy as np

    step = 1 << lod
    nx, ny, nz = occ.shape
    cx, cy, cz = nx - (nx % step), ny - (ny % step), nz - (nz % step)
    blocks = occ[:cx, :cy, :cz].reshape(
        cx // step, step, cy // step, step, cz // step, step
    )
    occ_down = blocks.max(axis=(1, 3, 5))
    # Material vote weighted by occupancy + 1, mirroring reduceMaterials.
    # Counts are accumulated per material into a running-best buffer instead
    # of stacking one int32 plane per material across the whole grid.
    occ_weights = blocks.astype(np.uint16) + 1
    del blocks
    mat_crop = mat[:cx, :cy, :cz]
    best = np.zeros(occ_down.shape, dtype=np.int32)
    mat_down = np.zeros(occ_down.shape, dtype=np.uint8)
    for material in used:
        mat_blocks = (mat_crop == material).reshape(
            cx // step, step, cy // step, step, cz // step, step
        )
        counts = (
            mat_blocks * occ_weights
        ).sum(axis=(1, 3, 5), dtype=np.int32)
        better = counts > best
        mat_down[better] = material
        best[better] = counts[better]
        del counts, better, mat_blocks
    # Majority material per block, but blocks with no material cells at all
    # must stay air: a zero best count means every vote was zero.
    mat_down[best == 0] = 0
    del best, occ_weights
    return occ_down, mat_down


# Surface-nets edge table: the 12 cube edges as (corner a, corner b) offsets.
# Corner order matches Roblox's kVertexIndexTable (x fastest, then y, then z).
_EDGE_TABLE = (
    ((0, 0, 0), (1, 0, 0)),
    ((1, 0, 0), (1, 1, 0)),
    ((1, 1, 0), (0, 1, 0)),
    ((0, 1, 0), (0, 0, 0)),
    ((0, 0, 1), (1, 0, 1)),
    ((1, 0, 1), (1, 1, 1)),
    ((1, 1, 1), (0, 1, 1)),
    ((0, 1, 1), (0, 0, 1)),
    ((0, 0, 0), (0, 0, 1)),
    ((1, 0, 0), (1, 0, 1)),
    ((1, 1, 0), (1, 1, 1)),
    ((0, 1, 0), (0, 1, 1)),
)

# Quad corner order around each face direction, so the cross of the first
# two edges points toward the solid side (mirrors pushQuad in Mesher.cpp).
_FACE_CORNERS = {
    0: ((0, 0, 0), (0, -1, 0), (0, -1, -1), (0, 0, -1)),
    1: ((0, 0, 0), (-1, 0, 0), (-1, 0, -1), (0, 0, -1)),
    2: ((0, 0, 0), (-1, 0, 0), (-1, -1, 0), (0, -1, 0)),
}


def _terrain_grid(xs, ys, zs, mats, occs, decimate):
    """Build padded occ/mat grids; return (occ, mat, used, min_xyz, step)."""
    import numpy as np

    min_x, max_x = int(xs.min()), int(xs.max())
    min_y, max_y = int(ys.min()), int(ys.max())
    min_z, max_z = int(zs.min()), int(zs.max())
    step = 1 << decimate
    nx = max_x - min_x + 1 + 2 * step
    ny = max_y - min_y + 1 + 2 * step
    nz = max_z - min_z + 1 + 2 * step
    if decimate > 0:
        nx += (-nx) % step
        ny += (-ny) % step
        nz += (-nz) % step
    total = nx * ny * nz
    if total > 512 * 1024 * 1024:
        raise ValueError(
            f"Terrain grid too large ({total} cells); skipping mesh build"
        )
    occ = np.zeros((nx, ny, nz), dtype=np.uint8)
    mat = np.zeros((nx, ny, nz), dtype=np.uint8)
    idx = ((xs - min_x + step) * (ny * nz)
           + (ys - min_y + step) * nz
           + (zs - min_z + step))
    occ.flat[idx] = occs
    mat.flat[idx] = np.minimum(mats, 255).astype(np.uint8)
    del idx
    used = sorted(int(m) for m in np.unique(mats))
    if decimate > 0:
        occ, mat = _downsample_grid(occ, mat, used, decimate)
        nx, ny, nz = occ.shape
    return occ, mat, used, (min_x - step, min_y - step, min_z - step), step


def _corner_views(grid, cell_dims):
    """Views of `grid` at the 8 cell corners, for every cell in the grid."""
    cx, cy, cz = cell_dims
    return tuple(
        grid[o0:o0 + cx, o1:o1 + cy, o2:o2 + cz]
        for o0 in (0, 1) for o1 in (0, 1) for o2 in (0, 1)
    )


def _surface_nets(xs, ys, zs, mats, occs, decimate):
    """Surface-nets pass for a single dense region (see _surface_nets_core)."""
    import numpy as np

    if xs.size == 0:
        return None
    occ, mat, used, origin, step = _terrain_grid(
        xs, ys, zs, mats, occs, decimate
    )
    return _surface_nets_core(occ, mat, used, origin, step)


def _surface_nets_core(occ, mat, used, origin, step, face_filter=None):
    """Shared surface-nets pass (port of Voxel2::Mesher).

    One vertex per boundary cell, positioned along the cell's crossing
    edges weighted by occupancy, quads between cells of differing
    solidity.  Returns the full vertex/quad tables, the per-material face
    chunks, and per-vertex material weights so both the legacy per-material
    groups and the single splat mesh derive from one meshing pass.

    ``face_filter`` (optional) receives the GLOBAL cell coordinate of each
    candidate face's left cell and returns keep/drop; the chunked terrain
    path uses it to assign boundary faces to exactly one chunk.
    """
    import numpy as np

    nx, ny, nz = occ.shape
    cx, cy, cz = nx - 1, ny - 1, nz - 1
    cell_size = float(step)
    # Solidity is material-based (Mesher.cpp's GridVertex.tag), never an
    # occupancy threshold: low-occupancy surface cells stay solid so the
    # occupancy field can pull vertices smoothly toward the surface instead
    # of being culled into stair-steps.
    tags = (mat > 0).astype(np.uint8)
    occ_views = _corner_views(occ, (cx, cy, cz))
    tag_views = _corner_views(tags, (cx, cy, cz))

    # Vertex placement happens in two passes so no full-grid position buffer
    # ever exists: pass one counts crossing edges per cell (one uint8 per
    # cell, no floats), pass two accumulates the occupancy-weighted crossing
    # points directly into vertex-sized arrays via flat gathered indices.
    ecount = np.zeros((cx, cy, cz), dtype=np.uint8)
    for (pa, pb) in _EDGE_TABLE:
        ta = tag_views[pa[0] * 4 + pa[1] * 2 + pa[2]]
        tb = tag_views[pb[0] * 4 + pb[1] * 2 + pb[2]]
        ecount += (ta != tb).astype(np.uint8)
    vertex_flat = np.flatnonzero(ecount.ravel())
    vertex_count = int(vertex_flat.size)
    vertex_ids = np.full((cx, cy, cz), -1, dtype=np.int32)
    vertex_ids.ravel()[vertex_flat] = np.arange(vertex_count, dtype=np.int32)
    ids_flat = vertex_ids.ravel()
    eavg_vertex = np.zeros((vertex_count, 3), dtype=np.float32)
    occ_scale = 1.0 / 256.0
    for (pa, pb) in _EDGE_TABLE:
        ta = tag_views[pa[0] * 4 + pa[1] * 2 + pa[2]]
        tb = tag_views[pb[0] * 4 + pb[1] * 2 + pb[2]]
        crossing = ta != tb
        oa = occ_views[pa[0] * 4 + pa[1] * 2 + pa[2]]
        ob = occ_views[pb[0] * 4 + pb[1] * 2 + pb[2]]
        f = np.flatnonzero(crossing.ravel())
        if f.size == 0:
            continue
        ta_f = ta.ravel()[f].astype(np.float32)
        tb_f = tb.ravel()[f].astype(np.float32)
        oa_f = oa.ravel()[f].astype(np.float32)
        ob_f = ob.ravel()[f].astype(np.float32)
        t = np.where(
            ta_f > tb_f,
            (oa_f + 1.0) * occ_scale,
            1.0 - (ob_f + 1.0) * occ_scale,
        )
        delta = np.asarray(pb, dtype=np.float32) - np.asarray(pa, dtype=np.float32)
        point = np.asarray(pa, dtype=np.float32) + delta * t[:, None]
        np.add.at(eavg_vertex, ids_flat[f], point)
    del occ_views, tag_views
    fx = vertex_flat // (cy * cz)
    fy = (vertex_flat // cz) % cy
    fz = vertex_flat % cz
    voxel = _STUDS_PER_VOXEL
    cell_size = float(step)
    ecount_vertex = ecount.ravel()[vertex_flat].astype(np.float32)[:, None]
    point = np.clip(
        eavg_vertex / np.maximum(ecount_vertex, 1.0), 0.0, cell_size
    )
    del ecount, eavg_vertex, ecount_vertex
    positions = np.empty((vertex_count, 3), dtype=np.float32)
    positions[:, 0] = (origin[0] + fx * cell_size + point[:, 0] + 0.5) * voxel
    positions[:, 1] = (origin[1] + fy * cell_size + point[:, 1] + 0.5) * voxel
    positions[:, 2] = (origin[2] + fz * cell_size + point[:, 2] + 0.5) * voxel
    del point, fx, fy, fz

    # Vertex materials: majority of the cell's 8 corners, occupancy-weighted.
    # Materials are voted sequentially into a running-best pair of buffers
    # (2 bytes + 1 byte per cell) instead of stacking all 8 corners x all
    # materials at once (the old layout reached hundreds of bytes per cell).
    # The vote must cover every cell, not just vertex cells: face extraction
    # reads node cells that can lack a vertex of their own.
    # The same pass normalises the corner votes into per-vertex splat
    # weights (one column per material) so the splat mesh never re-scans
    # the grids.
    mat_views = _corner_views(mat, (cx, cy, cz))
    occ_views = _corner_views(occ, (cx, cy, cz))
    best = np.zeros((cx, cy, cz), dtype=np.int16)
    mat_of_vertex = np.zeros((cx, cy, cz), dtype=np.uint8)
    totals = np.zeros((cx, cy, cz), dtype=np.int16)
    splat_weights = np.zeros((vertex_count, len(used)), dtype=np.float32)
    for k, material in enumerate(used):
        counts = np.zeros((cx, cy, cz), dtype=np.int16)
        for i in range(8):
            weighted = occ_views[i].astype(np.int16) + 1
            counts += weighted * (mat_views[i] == material)
        better = counts > best
        mat_of_vertex[better] = material
        best[better] = counts[better]
        totals += counts
        splat_weights[:, k] = counts.ravel()[vertex_flat]
        del counts, better
    del best, mat_views, occ_views
    denominator = np.maximum(totals.ravel()[vertex_flat], 1).astype(np.float32)
    splat_weights /= denominator[:, None]
    del totals, denominator

    # Quads between grid nodes whose tags differ (generateIndices in
    # Mesher.cpp): a face at node n references the cell vertices at n and
    # its three -1-offset neighbors.
    node_tags = tags
    face_slots = {m: [] for m in used}
    for axis in range(3):
        inner = (slice(1, -1),) * 3
        cur = node_tags[inner]
        shifted = np.roll(node_tags, -1, axis=axis)
        shifted[(slice(None),) * axis + (node_tags.shape[axis] - 1,)] = 0
        nxt = shifted[inner]
        del shifted
        differs = cur != nxt
        # Mirror pushQuad in Mesher.cpp: the axis-1 (y) face flips on the
        # OPPOSITE comparison, because its corner ordering runs the other
        # way around the quad.
        keep = cur < nxt if axis == 1 else cur > nxt
        fi, fj, fk = np.nonzero(differs)
        if fi.size == 0:
            continue
        # Node coordinates (inner slices start at 1).
        nx_n = fi + 1
        ny_n = fj + 1
        nz_n = fk + 1
        if face_filter is not None:
            origin_arr = np.asarray(origin, dtype=np.int64)
            keep_face = face_filter(
                origin_arr[0] + nx_n - 1,
                origin_arr[1] + ny_n - 1,
                origin_arr[2] + nz_n - 1,
            )
            if not np.any(keep_face):
                continue
            fi = fi[keep_face]
            fj = fj[keep_face]
            fk = fk[keep_face]
            nx_n = fi + 1
            ny_n = fj + 1
            nz_n = fk + 1
        quad = np.empty((fi.size, 4), dtype=np.int32)
        for corner_index, (ox, oy, oz) in enumerate(_FACE_CORNERS[axis]):
            quad[:, corner_index] = vertex_ids[nx_n + ox, ny_n + oy, nz_n + oz]
        # Reverse order when the solid node is on the -axis side.
        keep_flat = keep[fi, fj, fk]
        quad[~keep_flat] = quad[~keep_flat][:, ::-1]
        face_mats = mat_of_vertex[nx_n, ny_n, nz_n]
        for material in used:
            mask = face_mats == material
            if not np.any(mask):
                continue
            face_slots[material].append(quad[mask])

    # Soft normals accumulated over the FULL vertex buffer before any
    # per-material split (same as Mesher.cpp, which computes softnormals
    # for the whole mesh): material seams then stay smooth instead of
    # creasing at the group boundaries.  Chunks are processed in place so
    # no second copy of the quad table is ever materialized.
    del vertex_ids, node_tags, tags, mat_of_vertex, occ, mat
    verts_full = positions
    normals_full = np.zeros_like(verts_full)
    for chunks in face_slots.values():
        for chunk in chunks:
            v0 = verts_full[chunk[:, 0]]
            v1 = verts_full[chunk[:, 1]]
            v3 = verts_full[chunk[:, 3]]
            face_n = np.cross(v1 - v0, v3 - v0)
            for k in range(4):
                np.add.at(normals_full, chunk[:, k], face_n)
    lengths = np.linalg.norm(normals_full, axis=1, keepdims=True)
    lengths[lengths == 0] = 1.0
    normals_full /= lengths
    del lengths

    return {
        "used": used,
        "vertex_count": vertex_count,
        "verts": verts_full,
        "normals": normals_full,
        "splat_weights": splat_weights,
        "face_slots": face_slots,
    }


def terrain_smooth_mesh_groups(xs, ys, zs, mats, occs, colors, decimate=0):
    """Smooth terrain as per-material (vertices, quads, normals, color).

    Legacy API: each material's quads are compacted to the vertices they
    reference.  ``terrain_smooth_mesh_splat`` supersedes this for import,
    but callers that want one mesh per material still work.
    """
    import numpy as np

    if xs.size == 0:
        return []
    core = _surface_nets(xs, ys, zs, mats, occs, decimate)
    if core is None:
        return []
    used = core["used"]
    vertex_count = core["vertex_count"]
    groups = []
    for material in used:
        chunks = core["face_slots"][material]
        if not chunks:
            continue
        quads = np.concatenate(chunks, axis=0)
        referenced = np.unique(quads)
        remap = np.zeros(vertex_count, dtype=np.int32)
        remap[referenced] = np.arange(referenced.size, dtype=np.int32)
        quads = remap[quads]
        color = colors[material] if material < len(colors) else (0.8, 0.8, 0.8)
        groups.append((
            material,
            np.ascontiguousarray(core["verts"][referenced]),
            np.ascontiguousarray(quads),
            np.ascontiguousarray(core["normals"][referenced]),
            color,
        ))
    return groups


def terrain_smooth_mesh_splat(xs, ys, zs, mats, occs, colors, decimate=0):
    """One merged mesh plus per-vertex material weights for splatting.

    Returns (splat, water).  ``splat`` is
    (materials, verts, quads, normals, weights, tints): every material
    except water welded into one vertex table with one normalised float
    weight per (vertex, material).  ``water`` is a legacy-style group for
    the transmission-shaded water surface, or None.
    """
    import numpy as np

    if xs.size == 0:
        return None, None
    core = _surface_nets(xs, ys, zs, mats, occs, decimate)
    if core is None:
        return None, None
    used = core["used"]
    face_slots = core["face_slots"]
    vertex_count = core["vertex_count"]

    def _legacy_group(material):
        chunks = face_slots.get(material) or []
        if not chunks:
            return None
        quads = np.concatenate(chunks, axis=0)
        referenced = np.unique(quads)
        remap = np.zeros(vertex_count, dtype=np.int32)
        remap[referenced] = np.arange(referenced.size, dtype=np.int32)
        quads = remap[quads]
        color = colors[material] if material < len(colors) else (0.8, 0.8, 0.8)
        return (
            material,
            np.ascontiguousarray(core["verts"][referenced]),
            np.ascontiguousarray(quads),
            np.ascontiguousarray(core["normals"][referenced]),
            color,
        )

    splat_materials = [m for m in used if m != 1]
    splat = None
    if splat_materials:
        parts = [
            chunk for m in splat_materials for chunk in face_slots[m]
        ]
        if parts:
            quads = np.concatenate(parts, axis=0)
            referenced = np.unique(quads)
            remap = np.zeros(vertex_count, dtype=np.int32)
            remap[referenced] = np.arange(referenced.size, dtype=np.int32)
            quads = remap[quads]
            columns = [used.index(m) for m in splat_materials]
            weights = core["splat_weights"][referenced][:, columns]
            tints = tuple(
                tuple(colors[m]) if m < len(colors) else (0.8, 0.8, 0.8)
                for m in splat_materials
            )
            splat = (
                tuple(splat_materials),
                np.ascontiguousarray(core["verts"][referenced]),
                np.ascontiguousarray(quads),
                np.ascontiguousarray(core["normals"][referenced]),
                np.ascontiguousarray(weights),
                tints,
            )
    water = _legacy_group(1) if 1 in used else None
    return splat, water


def terrain_mesh_groups(xs, ys, zs, mats, colors, voxel=_STUDS_PER_VOXEL, decimate=0):
    """Convert solid terrain cells into per-material (vertices, quads, color).

    Vertices are deduplicated on shared grid corners and everything is kept
    as numpy arrays so the Blender side can use foreach_set directly.
    `decimate` levels halve the grid per axis (2x2x2 cells per block) before
    meshing, like Roblox's own chunk mips.
    """
    import numpy as np

    if xs.size == 0:
        return []

    # Bounding box + air padding on every side so boundary faces are emitted
    # correctly.  Pad by the downsample block size so the crop inside
    # _downsample_grid never trims a real cell column.
    min_x, max_x = int(xs.min()), int(xs.max())
    min_y, max_y = int(ys.min()), int(ys.max())
    min_z, max_z = int(zs.min()), int(zs.max())
    step = 1 << decimate
    nx = max_x - min_x + 1 + 2 * step
    ny = max_y - min_y + 1 + 2 * step
    nz = max_z - min_z + 1 + 2 * step
    if decimate > 0:
        nx += (-nx) % step
        ny += (-ny) % step
        nz += (-nz) % step
    total = nx * ny * nz
    if total > 512 * 1024 * 1024:
        raise ValueError(
            f"Terrain grid too large ({total} cells); skipping mesh build"
        )

    occ = np.zeros((nx, ny, nz), dtype=np.uint8)
    mat = np.zeros((nx, ny, nz), dtype=np.uint8)
    idx = ((xs - min_x + 1) * (ny * nz)
           + (ys - min_y + 1) * nz
           + (zs - min_z + 1))
    occ.flat[idx] = 1
    mat.flat[idx] = np.minimum(mats, 255).astype(np.uint8)
    del idx
    used = sorted(int(m) for m in np.unique(mats))
    if decimate > 0:
        occ, mat = _downsample_grid(occ, mat, used, decimate)
        nx, ny, nz = occ.shape
        voxel *= (1 << decimate)
    dims = (nx, ny, nz)

    # Axis multipliers for packing grid indices into one int64 key.
    axis_mult = (ny * nz, nz, 1)
    key_chunks = {m: [] for m in used}
    for axis in range(3):
        b_axis, d_axis = _FACE_AXIS_ORDER[axis]
        # Plus side: cell solid, next cell along axis is air.
        shifted = np.roll(occ, -1, axis=axis)
        # Only the wrap plane is fake; everything else is a true neighbor.
        shifted[(slice(None),) * axis + (dims[axis] - 1,)] = 0
        plus = occ & ~shifted
        # Minus side: cell solid, previous cell along axis is air.
        shifted = np.roll(occ, 1, axis=axis)
        shifted[(slice(None),) * axis + (0,)] = 0
        minus = occ & ~shifted

        mb = axis_mult[b_axis]
        md = axis_mult[d_axis]
        ma = axis_mult[axis]
        for side, plane_sign in ((plus, 1), (minus, -1)):
            axis_arrays = np.nonzero(side)
            if axis_arrays[0].size == 0:
                continue
            # Packed grid index of each solid cell's own corner.
            base_key = (
                axis_arrays[0] * (ny * nz)
                + axis_arrays[1] * nz
                + axis_arrays[2]
            ).astype(np.int64)
            # The quad plane sits one step further along axis for plus faces.
            plane_key = base_key + (ma if plane_sign > 0 else 0)
            if plane_sign > 0:
                offsets = np.asarray([0, mb, mb + md, md], dtype=np.int64)
            else:
                offsets = np.asarray([0, md, mb + md, mb], dtype=np.int64)
            face_mats = mat[axis_arrays]
            for material in used:
                mask = face_mats == material
                if not np.any(mask):
                    continue
                corners = plane_key[mask, None] + offsets[None, :]
                key_chunks[material].append(corners)

    groups = []
    for material in used:
        chunks = key_chunks[material]
        if not chunks:
            continue
        keys = np.concatenate(chunks, axis=0).ravel()
        del chunks
        unique_keys, inverse = np.unique(keys, return_inverse=True)
        del keys
        # Grid indices are shifted by (min - 1) relative to world cells;
        # undo that so vertex positions sit on the true cell grid.
        ux = (unique_keys // (ny * nz) + min_x - 1) * voxel
        uy = ((unique_keys // nz) % ny + min_y - 1) * voxel
        uz = (unique_keys % nz + min_z - 1) * voxel
        del unique_keys
        verts = np.stack(
            (ux.astype(np.float32), uy.astype(np.float32), uz.astype(np.float32)),
            axis=1,
        )
        quads = inverse.reshape(-1, 4).astype(np.int32)
        del inverse
        color = colors[material] if material < len(colors) else (0.8, 0.8, 0.8)
        groups.append((material, np.ascontiguousarray(verts), quads, color))
    return groups


def _terrain_chunked_groups(xs, ys, zs, mats, occs, colors, log2):
    """Per-chunk surface nets for sparse maps too large for one dense grid.

    Maps like freebuilds scatter material across a bounding box whose dense
    representation would need tens of gigabytes.  Each 2^log2 chunk is
    meshed independently with a one-cell overlap from its 26 neighbours;
    boundary faces are assigned to the chunk owning the face's left cell,
    so no face is duplicated and none is lost.  Returns legacy-style
    per-material groups (material, verts, quads, normals, color).
    """
    import numpy as np

    size = 1 << log2
    cx = np.floor_divide(xs, size).astype(np.int64)
    cy = np.floor_divide(ys, size).astype(np.int64)
    cz = np.floor_divide(zs, size).astype(np.int64)
    key = ((cx & 0x1FFFFF) << 42) | ((cy & 0x1FFFFF) << 21) | (cz & 0x1FFFFF)
    order = np.argsort(key, kind="stable")
    del cx, cy, cz
    xs_s = xs[order]
    ys_s = ys[order]
    zs_s = zs[order]
    mats_s = mats[order]
    occs_s = occs[order]
    key_s = key[order]
    del xs, ys, zs, mats, occs, order, key
    unique_keys, run_starts = np.unique(key_s, return_index=True)
    run_ends = np.append(run_starts[1:], key_s.size)
    del key_s
    total_runs = int(run_ends.size)

    merged_verts = {}
    merged_normals = {}
    merged_quads = {}
    base = {}

    for run_index in range(total_runs):
        if run_index and run_index % 500 == 0:
            print(f"[Terrain] chunked meshing {run_index}/{total_runs} chunks...")
        packed = int(unique_keys[run_index])
        raw = ((packed >> 42) & 0x1FFFFF, (packed >> 21) & 0x1FFFFF, packed & 0x1FFFFF)
        chunk = tuple(int(v - (1 << 21)) if v >= (1 << 20) else int(v) for v in raw)
        slices = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    neighbor = (
                        ((chunk[0] + dx) & 0x1FFFFF) << 42
                    ) | (
                        ((chunk[1] + dy) & 0x1FFFFF) << 21
                    ) | ((chunk[2] + dz) & 0x1FFFFF)
                    j = np.searchsorted(unique_keys, neighbor)
                    if j < total_runs and int(unique_keys[j]) == neighbor:
                        slices.append((int(run_starts[j]), int(run_ends[j])))
        if not slices:
            continue
        sel = np.concatenate([
            np.arange(start, stop, dtype=np.int64)
            for start, stop in slices
        ])
        occ, mat, used, origin, step = _terrain_grid(
            xs_s[sel], ys_s[sel], zs_s[sel], mats_s[sel], occs_s[sel], 0
        )

        def keep(gx0, gy0, gz0):
            return (
                (np.floor_divide(gx0, size) == chunk[0])
                & (np.floor_divide(gy0, size) == chunk[1])
                & (np.floor_divide(gz0, size) == chunk[2])
            )

        core = _surface_nets_core(
            occ, mat, used, origin, step, face_filter=keep
        )
        if core is None:
            continue
        vertex_count = core["vertex_count"]
        for material in used:
            chunks = core["face_slots"].get(material)
            if not chunks:
                continue
            quads = np.concatenate(chunks, axis=0)
            referenced = np.unique(quads)
            remap = np.zeros(vertex_count, dtype=np.int32)
            remap[referenced] = np.arange(referenced.size, dtype=np.int32)
            offset = base.get(material, 0)
            merged_quads.setdefault(material, []).append(
                (remap[quads] + offset).astype(np.int32)
            )
            merged_verts.setdefault(material, []).append(
                np.ascontiguousarray(core["verts"][referenced])
            )
            merged_normals.setdefault(material, []).append(
                np.ascontiguousarray(core["normals"][referenced])
            )
            base[material] = offset + referenced.size

    groups = []
    for material, quad_list in merged_quads.items():
        color = colors[material] if material < len(colors) else (0.8, 0.8, 0.8)
        groups.append((
            material,
            np.concatenate(merged_verts[material], axis=0),
            np.concatenate(quad_list, axis=0),
            np.concatenate(merged_normals[material], axis=0),
            color,
        ))
    return groups


def prepare_terrain(terrain_meta):
    """Decode and mesh terrain without touching Blender RNA.

    This is deliberately safe for a worker thread. The returned numpy arrays
    are consumed by ``build_prepared_terrain`` on Blender's main thread.
    """

    smoothgrid = terrain_meta.get("smoothgrid") or b""
    colors_raw = terrain_meta.get("colors") or b""
    decimate = int(terrain_meta.get("decimate") or 0)
    smooth = bool(terrain_meta.get("smooth", True))
    blend = bool(terrain_meta.get("blend", True))
    water_props = terrain_meta.get("water") or {}
    if len(smoothgrid) < 3:
        return None
    try:
        xs, ys, zs, mats, occs = decode_smooth_grid_arrays(smoothgrid)
    except ValueError as exc:
        print(f"[Terrain] Skipping terrain: {exc}")
        return None
    if xs.size == 0:
        return None
    # No occupancy thresholding here: solidity is material-based inside the
    # mesher, and the occupancy field steers vertex placement.
    solid_count = int(xs.size)
    colors = decode_material_colors(colors_raw)
    try:
        log2 = int(smoothgrid[1]) if isinstance(smoothgrid, (bytes, bytearray)) else 5
    except (IndexError, TypeError, ValueError):
        log2 = 5
    # The dense meshing path builds one grid over the material bounding box.
    # Sparse freebuilds scatter cells across an enormous box, so estimate
    # that box first and fall back to per-chunk meshing when it would not
    # fit in memory.
    dense_total = (
        int(xs.max() - xs.min() + 3)
        * int(ys.max() - ys.min() + 3)
        * int(zs.max() - zs.min() + 3)
    )
    try:
        if smooth and dense_total > 512 * 1024 * 1024:
            print(
                f"[Terrain] Sparse map ({solid_count} cells over "
                f"{dense_total} bounding cells); meshing per chunk."
            )
            splat = None
            groups = _terrain_chunked_groups(
                xs, ys, zs, mats, occs, colors, log2
            )
        elif smooth and blend:
            splat, water_group = terrain_smooth_mesh_splat(
                xs, ys, zs, mats, occs, colors, decimate=decimate
            )
            groups = [water_group] if water_group is not None else []
        else:
            splat = None
            groups = terrain_smooth_mesh_groups(
                xs, ys, zs, mats, occs, colors, decimate=decimate
            ) if smooth else terrain_mesh_groups(
                xs, ys, zs, mats, colors, decimate=decimate
            )
    except ValueError as exc:
        print(f"[Terrain] {exc}")
        return None
    if splat is None and not groups:
        return None
    # The decoded cell arrays are dead weight once the mesh owns the
    # vertices and quads; drop them before Blender duplicates the geometry.
    del xs, ys, zs, mats, occs

    return {
        "splat": splat,
        "groups": groups,
        "solid_count": solid_count,
        "water_props": water_props,
    }


def build_prepared_terrain(collection, prepared, model_tag=None):
    """Create Blender terrain meshes from ``prepare_terrain`` output."""
    if not prepared:
        return 0
    import bpy
    import numpy as np

    from ..core.constants import get_transform_to_blender

    t2b = np.asarray(get_transform_to_blender(), dtype=np.float64)
    groups = prepared.get("groups") or []
    splat = prepared.get("splat")
    solid_count = prepared["solid_count"]
    water_props = prepared["water_props"]
    built = 0
    timings = {
        "transform": 0.0,
        "rna geometry": 0.0,
        "mesh update": 0.0,
        "material": 0.0,
        "link": 0.0,
    }
    material_timings = {}
    if splat is not None:
        built += _build_splat_terrain(
            collection, splat, model_tag, t2b, timings
        )
    for group_index in range(len(groups)):
        group = groups[group_index]
        groups[group_index] = None
        material, verts, quads, color = group[0], group[1], group[2], group[4]
        # Roblox Y-up -> Blender Z-up in one vectorized pass.
        started = time.perf_counter()
        blender_verts = verts.astype(np.float64) @ t2b[:3, :3].T + t2b[:3, 3]
        blender_verts = np.ascontiguousarray(blender_verts, dtype=np.float32)
        timings["transform"] += time.perf_counter() - started
        del verts
        quad_count = quads.shape[0]
        started = time.perf_counter()
        mesh = bpy.data.meshes.new(f"Terrain_{material}")
        mesh.vertices.add(blender_verts.shape[0])
        mesh.vertices.foreach_set("co", blender_verts.ravel())
        mesh.loops.add(4 * quad_count)
        mesh.loops.foreach_set("vertex_index", quads.ravel())
        del quads
        mesh.polygons.add(quad_count)
        mesh.polygons.foreach_set(
            "loop_start", np.arange(quad_count, dtype=np.int32) * 4
        )
        mesh.polygons.foreach_set(
            "loop_total", np.full(quad_count, 4, dtype=np.int32)
        )
        mesh.polygons.foreach_set(
            "use_smooth", np.ones(quad_count, dtype=np.int32)
        )
        timings["rna geometry"] += time.perf_counter() - started
        # No custom split normals and no auto-smooth: per-polygon use_smooth
        # lets Blender derive smooth normals from the geometry on its own.
        started = time.perf_counter()
        mesh.update()
        timings["mesh update"] += time.perf_counter() - started
        tint = (color[0], color[1], color[2])
        started = time.perf_counter()
        if material == 1:
            mat = _terrain_water_material(water_props)
        else:
            mat = _terrain_material(material, tint, mesh, material_timings)
        timings["material"] += time.perf_counter() - started
        mesh.materials.append(mat)
        started = time.perf_counter()
        obj = bpy.data.objects.new(
            f"{model_tag + '/' if model_tag else ''}Terrain_{material}", mesh
        )
        # Terrain groups are one material/tint per object. Feeding that tint
        # through Object Info avoids creating and filling an RBXColor CORNER
        # attribute for every terrain loop (millions of values in large maps).
        if material != 1:
            try:
                from .textures import set_object_rbx_tint

                set_object_rbx_tint(obj, {"color": tint})
            except Exception:
                pass
        collection.objects.link(obj)
        timings["link"] += time.perf_counter() - started
        built += 1
    print(f"[Terrain] Imported {solid_count} terrain cells as {built} object(s).")
    print(
        "[Terrain] Build detail: "
        + ", ".join(f"{name} {seconds:.2f}s" for name, seconds in timings.items())
    )
    if material_timings:
        print(
            "[Terrain] Material detail: "
            + ", ".join(
                f"{name} {seconds:.2f}s" for name, seconds in material_timings.items()
            )
        )
    return built


def import_terrain(collection, terrain_meta, model_tag=None):
    """Decode and build the terrain (one splat mesh plus water)."""
    return build_prepared_terrain(
        collection, prepare_terrain(terrain_meta), model_tag=model_tag
    )


def _build_splat_terrain(collection, splat, model_tag, t2b, timings):
    """Build the merged terrain mesh: splat weights + one splat shader."""
    import bpy
    import numpy as np

    materials, verts, quads, _normals, weights, tints = splat
    started = time.perf_counter()
    blender_verts = verts.astype(np.float64) @ t2b[:3, :3].T + t2b[:3, 3]
    blender_verts = np.ascontiguousarray(blender_verts, dtype=np.float32)
    timings["transform"] += time.perf_counter() - started
    del verts
    quad_count = quads.shape[0]
    started = time.perf_counter()
    mesh = bpy.data.meshes.new("Terrain_Splat")
    mesh.vertices.add(blender_verts.shape[0])
    mesh.vertices.foreach_set("co", blender_verts.ravel())
    del blender_verts
    mesh.loops.add(4 * quad_count)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(quad_count)
    mesh.polygons.foreach_set(
        "loop_start", np.arange(quad_count, dtype=np.int32) * 4
    )
    mesh.polygons.foreach_set(
        "loop_total", np.full(quad_count, 4, dtype=np.int32)
    )
    mesh.polygons.foreach_set(
        "use_smooth", np.ones(quad_count, dtype=np.int32)
    )
    # One FLOAT_COLOR corner attribute per material; the shader reads each
    # red channel through a Vertex Color node.  (Byte colours would be
    # sRGB-decoded by the shader and no longer linear weights.)
    loop_count = 4 * quad_count
    for k in range(len(materials)):
        attribute = None
        try:
            attribute = mesh.color_attributes.new(
                f"{_SPLAT_WEIGHT_LAYER}{k}", "FLOAT_COLOR", "CORNER"
            )
        except Exception:
            pass
        if attribute is None:
            continue
        try:
            data = np.empty((loop_count, 4), dtype=np.float32)
            data[:, 0] = weights[quads.ravel(), k]
            data[:, 1:3] = 0.0
            data[:, 3] = 1.0
            attribute.data.foreach_set("color", data.ravel())
        except Exception:
            pass
        del data
    del weights, quads
    timings["rna geometry"] += time.perf_counter() - started
    started = time.perf_counter()
    mesh.update()
    timings["mesh update"] += time.perf_counter() - started
    started = time.perf_counter()
    entries = _terrain_splat_entries(materials, tints)
    mat = _terrain_splat_material(entries)
    timings["material"] += time.perf_counter() - started
    mesh.materials.append(mat)
    started = time.perf_counter()
    obj = bpy.data.objects.new(
        f"{model_tag + '/' if model_tag else ''}Terrain", mesh
    )
    collection.objects.link(obj)
    timings["link"] += time.perf_counter() - started
    print(
        f"[Terrain] Splat shader blends {len(materials)} materials "
        f"across {quad_count} quads."
    )
    return 1


def _terrain_splat_entries(materials, tints):
    """Per-material metadata (enum, tint, built-in map refs) for splatting."""
    from .textures import _builtin_map_ref, _builtin_material_for_entry

    entries = []
    for material, tint in zip(materials, tints):
        enum = _TERRAIN_MATERIAL_ENUM.get(int(material))
        try:
            tint = tuple(max(0.0, min(1.0, float(c))) for c in tint[:3])
        except (TypeError, ValueError, IndexError):
            tint = (0.8, 0.8, 0.8)
        entry = {
            "material": enum,
            "tint": tint,
            "name": f"Terrain_{material}",
        }
        builtin = _builtin_material_for_entry(entry)
        maps = builtin["maps"] if builtin else ("", "", "", "")
        entry["color_ref"] = _builtin_map_ref(maps[0]) if builtin else ""
        entry["normal_ref"] = _builtin_map_ref(maps[1]) if builtin else ""
        entry["metal_ref"] = _builtin_map_ref(maps[2]) if builtin else ""
        entry["rough_ref"] = _builtin_map_ref(maps[3]) if builtin else ""
        entries.append(entry)
    return entries


def _splat_constant_image(name, rgba):
    """1x1 non-colour image used as a placeholder map (white/black/flat)."""
    import bpy

    image = bpy.data.images.get(name)
    if image is None:
        image = bpy.data.images.new(name, 1, 1)
        image.colorspace_settings.name = "Non-Color"
        image.pixels = [float(c) for c in rgba]
        image.update()
    return image


def _build_slot_group(entry, images):
    """Build (or reuse) the per-material splat node group.

    One group per material: triplanar colour/roughness/metalness/normal
    sampling with per-cell rotation, all multiplied by the slot's
    normalised weight.  The normal map is sampled triplanar too (axis
    swizzles keep it upright on every slope), so no mesh UV layer is
    needed anywhere.  Maps bake into the group because group image
    sockets only exist in newer Blender builds; the group is reused
    across imports whenever its visual key (graph version + tint + map
    refs) still matches.
    """
    import bpy

    from .textures import (
        _color_image_view,
        _data_image_view,
        fetch_texture_image,
    )

    white, black, flat_normal = images
    color_map = white
    rough_map = white
    metal_map = black
    normal_map = flat_normal
    if entry.get("color_ref"):
        fetched = fetch_texture_image(
            entry["color_ref"], name=f"{entry['name']}_matcolor"
        )
        fetched = _color_image_view(fetched)
        if fetched is not None:
            color_map = fetched
    if entry.get("rough_ref"):
        fetched = fetch_texture_image(
            entry["rough_ref"], name=f"{entry['name']}_roughness", non_color=True
        )
        fetched = _data_image_view(fetched)
        if fetched is not None:
            rough_map = fetched
    if entry.get("metal_ref"):
        fetched = fetch_texture_image(
            entry["metal_ref"], name=f"{entry['name']}_metalness", non_color=True
        )
        fetched = _data_image_view(fetched)
        if fetched is not None:
            metal_map = fetched
    if entry.get("normal_ref"):
        fetched = fetch_texture_image(
            entry["normal_ref"], name=f"{entry['name']}_normal", non_color=True
        )
        fetched = _data_image_view(fetched)
        if fetched is not None:
            normal_map = fetched

    tint = entry["tint"]
    key = "|".join((
        str(_SPLAT_SLOT_VERSION),
        str(entry.get("material")),
        ",".join(f"{c:.4f}" for c in tint),
        str(entry.get("color_ref")),
        str(entry.get("rough_ref")),
        str(entry.get("metal_ref")),
        str(entry.get("normal_ref")),
    ))
    group_name = f"RBX Terrain Splat {entry['name']}"
    group = bpy.data.node_groups.get(group_name)
    if group is not None and group.get("RBXSplatSlotKey") == key:
        return group
    if group is None:
        group = bpy.data.node_groups.new(group_name, "ShaderNodeTree")
    else:
        group.nodes.clear()
    # The group interface API changed in 4.0 (interface.new_socket replaced
    # inputs/outputs.new); support both.
    try:
        group.interface.clear()
        for name, socket_type in _SPLAT_SLOT_INPUTS:
            group.interface.new_socket(name, in_out="INPUT", socket_type=socket_type)
        for name, socket_type in _SPLAT_SLOT_OUTPUTS:
            group.interface.new_socket(name, in_out="OUTPUT", socket_type=socket_type)
    except AttributeError:
        group.inputs.clear()
        group.outputs.clear()
        for name, socket_type in _SPLAT_SLOT_INPUTS:
            group.inputs.new(socket_type, name)
        for name, socket_type in _SPLAT_SLOT_OUTPUTS:
            group.outputs.new(socket_type, name)

    gt = group.nodes
    gl = group.links
    gi = gt.new("NodeGroupInput")
    gi.location = (-2400, 0)
    go = gt.new("NodeGroupOutput")
    go.location = (0, -300)

    _g = [0.0, 0.0]

    def _glane(x, y):
        _g[0] = float(x)
        _g[1] = float(y)

    def _gplace(node, dx=200.0):
        node.location = (_g[0], _g[1])
        _g[0] += dx
        return node

    def _gnew(bl_type, dx=200.0):
        return _gplace(gt.new(bl_type), dx)

    def _gvec(operation, *ins):
        node = _gnew("ShaderNodeVectorMath")
        node.operation = operation
        for index, socket in enumerate(ins):
            gl.new(socket, node.inputs[index])
        return node.outputs[0]

    def _gscalar(operation, *ins):
        node = _gnew("ShaderNodeMath")
        node.operation = operation
        for index, socket in enumerate(ins):
            gl.new(socket, node.inputs[index])
        return node.outputs[0]

    def _gvalue(value):
        node = _gnew("ShaderNodeValue")
        node.outputs[0].default_value = float(value)
        return node.outputs[0]

    def _gsep(vector, component):
        node = _gnew("ShaderNodeSeparateXYZ")
        gl.new(vector, node.inputs["Vector"])
        return node.outputs[component]

    def _gtexture(image, label):
        node = _gnew("ShaderNodeTexImage")
        node.image = image
        node.label = label
        try:
            node.interpolation = "Linear"
        except Exception:
            pass
        return node

    def _gtriplanar(image, label, rotation):
        separate = _gnew("ShaderNodeSeparateXYZ")
        gl.new(gi.outputs["Position"], separate.inputs["Vector"])
        x, y, z = separate.outputs["X"], separate.outputs["Y"], separate.outputs["Z"]
        samples = []
        for axis, (first, second) in enumerate(((y, z), (x, z), (x, y))):
            combine = _gnew("ShaderNodeCombineXYZ")
            gl.new(first, combine.inputs["X"])
            gl.new(second, combine.inputs["Y"])
            vector = combine.outputs["Vector"]
            mapping = _gnew("ShaderNodeMapping")
            try:
                mapping.vector_type = "POINT"
            except Exception:
                pass
            gl.new(vector, mapping.inputs["Vector"])
            gl.new(rotation, mapping.inputs["Rotation"])
            vector = mapping.outputs["Vector"]
            texture = _gtexture(image, f"{label} {'XYZ'[axis]}")
            gl.new(vector, texture.inputs["Vector"])
            samples.append(texture.outputs["Color"])
        blended = _gvec("MULTIPLY", samples[0], gi.outputs["Blend X"])
        blended = _gvec("MULTIPLY_ADD", samples[1], gi.outputs["Blend Y"], blended)
        blended = _gvec("MULTIPLY_ADD", samples[2], gi.outputs["Blend Z"], blended)
        return blended

    # Rotation from the shared Voronoi cell colour, scaled by the spin
    # range.  It rotates colour/roughness/metalness only: the swizzled
    # triplanar normals must stay axis-aligned or their tangent frame
    # corrupts and lighting glitches.  The Border input (1 from the main
    # tree) keeps rotation full-strength everywhere.
    _glane(-2400, -240)
    rotation_node = _gnew("ShaderNodeVectorMath")
    rotation_node.operation = "MULTIPLY"
    rotation_node.inputs[1].default_value = (
        _SPLAT_ROTATION_RANGE, _SPLAT_ROTATION_RANGE, _SPLAT_ROTATION_RANGE
    )
    gl.new(gi.outputs["Cell Color"], rotation_node.inputs[0])
    rotation_gate = _gnew("ShaderNodeVectorMath")
    rotation_gate.operation = "MULTIPLY"
    gl.new(rotation_node.outputs[0], rotation_gate.inputs[0])
    gl.new(gi.outputs["Border"], rotation_gate.inputs[1])
    rotation = rotation_gate.outputs[0]

    # Colour: triplanar map x tint x weight.
    _glane(-2400, -420)
    color = _gtriplanar(color_map, "Color", rotation)
    tint_node = _gnew("ShaderNodeRGB")
    tint_node.outputs[0].default_value = (tint[0], tint[1], tint[2], 1.0)
    color = _gvec("MULTIPLY", color, tint_node.outputs[0])
    gl.new(_gvec("MULTIPLY", color, gi.outputs["Weight"]), go.inputs["Color"])

    # Roughness.
    _glane(-2400, -1040)
    rough = _gsep(_gtriplanar(rough_map, "Rough", rotation), "X")
    gl.new(
        _gscalar("MULTIPLY", rough, gi.outputs["Weight"]),
        go.inputs["Roughness"],
    )

    # Metalness (the black placeholder map yields zero for most terrain).
    _glane(-2400, -1660)
    metal = _gsep(_gtriplanar(metal_map, "Metal", rotation), "X")
    gl.new(
        _gscalar("MULTIPLY", metal, gi.outputs["Weight"]),
        go.inputs["Metalness"],
    )

    # Triplanar normal: sample per axis, swizzle each sample into the
    # axis tangent frame (X:(b,r,g) Y:(r,b,g) Z:(r,g,b)), decode 0..1 to
    # -1..1, blend by the same renormalised triplanar weights.  No uv
    # layer: its per-face axis projection flipped the tangent basis
    # between quads and tore the normals exactly where slopes change.
    _glane(-2400, -2280)
    separate = _gnew("ShaderNodeSeparateXYZ")
    gl.new(gi.outputs["Position"], separate.inputs["Vector"])
    px, py, pz = separate.outputs["X"], separate.outputs["Y"], separate.outputs["Z"]
    axis_normals = []
    for first, second, swizzle in (
        (py, pz, ("B", "R", "G")),
        (px, pz, ("R", "B", "G")),
        (px, py, ("R", "G", "B")),
    ):
        combine = _gnew("ShaderNodeCombineXYZ")
        gl.new(first, combine.inputs["X"])
        gl.new(second, combine.inputs["Y"])
        mapping = _gnew("ShaderNodeMapping")
        try:
            mapping.vector_type = "POINT"
        except Exception:
            pass
        gl.new(combine.outputs["Vector"], mapping.inputs["Vector"])
        texture = _gtexture(normal_map, "Normal")
        gl.new(mapping.outputs["Vector"], texture.inputs["Vector"])
        sample = _gnew("ShaderNodeSeparateXYZ")
        gl.new(texture.outputs["Color"], sample.inputs["Vector"])
        channels = {
            "R": sample.outputs["X"],
            "G": sample.outputs["Y"],
            "B": sample.outputs["Z"],
        }
        swizzled = _gnew("ShaderNodeCombineXYZ")
        for target, source in zip(("X", "Y", "Z"), swizzle):
            gl.new(channels[source], swizzled.inputs[target])
        decoded = _gvec("SUBTRACT", _gvec("MULTIPLY", swizzled.outputs["Vector"], _gvalue(2.0)), _gvalue(1.0))
        axis_normals.append(decoded)
    blended = _gvec("MULTIPLY", axis_normals[0], gi.outputs["Blend X"])
    blended = _gvec("MULTIPLY_ADD", axis_normals[1], gi.outputs["Blend Y"], blended)
    blended = _gvec("MULTIPLY_ADD", axis_normals[2], gi.outputs["Blend Z"], blended)
    gl.new(
        _gvec("MULTIPLY", blended, gi.outputs["Weight"]),
        go.inputs["Normal"],
    )

    group["RBXSplatSlotKey"] = key
    return group


def _terrain_splat_material(entries):
    """One shader splatting every terrain material by vertex weight.

    The game-engine approach: each material's colour map is sampled with a
    world-space triplanar projection (the same 10 studs/tile as parts, so
    slopes never stretch) and blended by the per-vertex weights in the
    RBXSplatN corner attributes.  Weights are dithered by a per-material
    Voronoi cell field (gated to material borders), run through a wide
    crossfade ramp, then renormalised, so borders read as smooth organic
    gradients with a gentle wiggle.  One shared Voronoi field rotates
    every material's texture samples (colour/roughness/metalness only,
    never the swizzled normals), so repeating tiles break up organically
    while crossfaded materials stay in one frame.  Normal maps ride the
    same triplanar projection with per-axis swizzles, so they stay
    upright on every slope.

    Every material's texture chain collapses into one per-material node
    group (the maps bake inside: group image sockets only exist in newer
    Blender builds), so the main tree stays at ~a dozen visible nodes per
    material instead of ~fifty.
    """
    import bpy

    from .textures import (
        _BUILTIN_MATERIAL_STUDS_PER_TILE,
    )

    mat = bpy.data.materials.new("Terrain_Splat")
    mat.use_nodes = True
    tree = mat.node_tree
    nodes = tree.nodes
    links = tree.links
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        return mat
    bsdf.location = (0, -300)
    output = nodes.get("Material Output")
    if output is not None:
        output.location = (280, -300)

    # Lane cursor: every node lands where the cursor sits, then the cursor
    # advances.  Callers re-lane before each chain so the graph lays out in
    # columns instead of Blender's default stack at the origin.
    _cursor = [0.0, 0.0]

    def _lane(x, y):
        _cursor[0] = float(x)
        _cursor[1] = float(y)

    def _place(node, dx=200.0, dy=0.0):
        node.location = (_cursor[0], _cursor[1])
        _cursor[0] += dx
        _cursor[1] += dy
        return node

    def _new(bl_type, dx=200.0, dy=0.0):
        return _place(nodes.new(bl_type), dx, dy)

    def _vec(operation, *ins):
        node = _new("ShaderNodeVectorMath")
        node.operation = operation
        for index, socket in enumerate(ins):
            links.new(socket, node.inputs[index])
        return node.outputs[0]

    def _scalar(operation, *ins):
        node = _new("ShaderNodeMath")
        node.operation = operation
        for index, socket in enumerate(ins):
            links.new(socket, node.inputs[index])
        return node.outputs[0]

    def _value(value):
        node = _new("ShaderNodeValue")
        node.outputs[0].default_value = float(value)
        return node.outputs[0]

    def _component(vector, component):
        node = _new("ShaderNodeSeparateXYZ")
        links.new(vector, node.inputs["Vector"])
        return node.outputs[component]

    # World-anchored position (the terrain object sits at the origin) in
    # tile units: 1/studs-per-tile matches the part material density.
    _lane(500, 700)
    texcoord = _new("ShaderNodeTexCoord")
    texcoord.name = "RBX Splat TexCoord"
    pos_scale = _new("ShaderNodeVectorMath")
    pos_scale.operation = "SCALE"
    pos_scale.name = "RBX Splat Position"
    pos_scale.inputs["Scale"].default_value = 1.0 / _BUILTIN_MATERIAL_STUDS_PER_TILE
    links.new(texcoord.outputs["Object"], pos_scale.inputs[0])
    pos = pos_scale.outputs[0]

    # Triplanar blend weights from the shading normal, renormalised to 1.
    _lane(500, 460)
    geometry = _new("ShaderNodeNewGeometry")
    geometry.name = "RBX Splat Geometry"
    abs_normal = _vec("ABSOLUTE", geometry.outputs["Normal"])
    n_x = _component(abs_normal, "X")
    n_y = _component(abs_normal, "Y")
    n_z = _component(abs_normal, "Z")
    normal_total = _scalar("ADD", _scalar("ADD", n_x, n_y), n_z)
    wx = _scalar("DIVIDE", n_x, normal_total)
    wy = _scalar("DIVIDE", n_y, normal_total)
    wz = _scalar("DIVIDE", n_z, normal_total)

    white = _splat_constant_image("RBX Splat White", (1.0, 1.0, 1.0, 1.0))
    black = _splat_constant_image("RBX Splat Black", (0.0, 0.0, 0.0, 1.0))
    flat_normal = _splat_constant_image(
        "RBX Splat Flat Normal", (0.5, 0.5, 1.0, 1.0)
    )

    # One shared Voronoi field rotates every material's texture samples
    # by the same amount at any point, breaking up repeating tiles
    # without letting crossfaded materials clash.  (The swizzled normals
    # are not rotated.)
    _lane(500, 220)
    rot_voronoi = _new("ShaderNodeTexVoronoi")
    rot_voronoi.name = "RBX Splat Rotation Field"
    try:
        rot_voronoi.feature = "F1"
        rot_voronoi.distance = "EUCLIDEAN"
        rot_voronoi.inputs["Scale"].default_value = _SPLAT_DITHER_SCALE
        rot_voronoi.inputs["Randomness"].default_value = 0.95
    except Exception:
        pass
    links.new(pos, rot_voronoi.inputs["Vector"])
    shared_cell_color = rot_voronoi.outputs["Color"]
    # Rotation applies everywhere at full strength: all materials share
    # one field, so crossfaded textures stay in one frame.
    rotation_on = _value(1.0)

    # Per-material weight chains (vertex weight -> Voronoi dither -> sharp
    # ramp -> floor) plus one slot group per material carrying its maps.
    sharpened = []
    slots = []
    weight_lane_ends = []
    for k, entry in enumerate(entries):
        column_x = -2000.0 - 3400.0 * k
        _lane(column_x, 0)
        attribute = _new("ShaderNodeVertexColor")
        attribute.layer_name = f"{_SPLAT_WEIGHT_LAYER}{k}"
        attribute.name = f"RBX Splat Weight {k}"
        raw = _component(attribute.outputs["Color"], "X")
        voronoi = _new("ShaderNodeTexVoronoi")
        try:
            voronoi.feature = "F1"
            voronoi.distance = "EUCLIDEAN"
            voronoi.inputs["Scale"].default_value = _SPLAT_DITHER_SCALE
            # Max randomness: below ~0.6 the jittered lattice stays
            # visibly regular and the border wiggle reads as repeating
            # cells.
            voronoi.inputs["Randomness"].default_value = 0.95 + 0.05 * k
        except Exception:
            pass
        offset = _new("ShaderNodeVectorMath")
        offset.operation = "ADD"
        offset.inputs[1].default_value = (k * 13.0, k * 29.0, 0.0)
        links.new(pos, offset.inputs[0])
        links.new(offset.outputs[0], voronoi.inputs["Vector"])
        # Bounded, centred dither source: the Voronoi CELL COLOUR is
        # constant within each cell and always in [0, 1], unlike the raw
        # F1 distance which is unbounded and strongly biased at fine cell
        # scales (the distance never reaches 0.5, so the old
        # (distance - 0.5) dither was nearly constant and pushed the
        # border one way).
        voronoi_fac = _component(voronoi.outputs["Color"], "X")
        dither = _scalar(
            "MULTIPLY",
            _scalar("SUBTRACT", voronoi_fac, _value(0.5)),
            _value(_SPLAT_DITHER_AMPLITUDE),
        )
        # Dither only near material borders: the interior of a single
        # material (flat ground) must stay uniform, or the Voronoi cells
        # read as blotches across large flat areas.  4*w*(1-w) peaks at 1
        # on a border (w=0.5) and falls to 0 inside either material.  The
        # voronoi runs at max randomness so the cell lattice is irregular
        # and the border never reads as a repeating pattern.
        boundary = _scalar(
            "MULTIPLY",
            _scalar("MULTIPLY", raw, _scalar("SUBTRACT", _value(1.0), raw)),
            _value(4.0),
        )
        dither = _scalar("MULTIPLY", dither, boundary)
        dithered = _scalar("ADD", raw, dither)
        ramp = _new("ShaderNodeValToRGB")
        ramp.color_ramp.interpolation = "LINEAR"
        stops = ramp.color_ramp.elements
        stops[0].position = _SPLAT_RAMP_LO
        stops[0].color = (0.0, 0.0, 0.0, 1.0)
        stops[1].position = _SPLAT_RAMP_HI
        stops[1].color = (1.0, 1.0, 1.0, 1.0)
        links.new(dithered, ramp.inputs["Fac"])
        sharp = _component(ramp.outputs["Color"], "X")
        # A sliver of the raw weight guarantees no vertex ever goes black.
        sharp = _scalar("ADD", sharp, _scalar("MULTIPLY", raw, _value(0.12)))
        sharpened.append(sharp)
        weight_lane_ends.append(tuple(_cursor))
        slot = _new("ShaderNodeGroup", dx=260.0)
        slot.node_tree = _build_slot_group(entry, (white, black, flat_normal))
        slot.name = f"RBX Terrain Splat {k}"
        slot.label = entry["name"]
        slots.append(slot)
        links.new(shared_cell_color, slot.inputs["Cell Color"])
        links.new(pos, slot.inputs["Position"])
        links.new(rotation_on, slot.inputs["Border"])
        links.new(wx, slot.inputs["Blend X"])
        links.new(wy, slot.inputs["Blend Y"])
        links.new(wz, slot.inputs["Blend Z"])

    # Shared weight total, right of the shared coordinate block.
    _lane(2500, -240)
    weight_total = None
    for sharp in sharpened:
        weight_total = (
            sharp if weight_total is None else _scalar("ADD", weight_total, sharp)
        )
    weight_total = _scalar("MAXIMUM", weight_total, _value(0.0001))

    # Sum chains converge on the Principled BSDF: each chain's add nodes
    # stack downward at a fixed x, between the material columns and the
    # shader.  The cursor drops 80 px per add so long chains stay tidy.
    sum_cursors = {
        "color": (-360.0, -240.0),
        "rough": (-360.0, -740.0),
        "normal": (-360.0, -1240.0),
        "metal": (-360.0, -1720.0),
    }

    def _sum_node(chain, node):
        x, y = sum_cursors[chain]
        node.location = (x, y)
        sum_cursors[chain] = (x, y - 80.0)
        return node

    def _vec_sum(chain, *ins):
        node = nodes.new("ShaderNodeVectorMath")
        node.operation = "ADD"
        _sum_node(chain, node)
        for index, socket in enumerate(ins):
            links.new(socket, node.inputs[index])
        return node.outputs[0]

    def _scalar_sum(chain, *ins):
        node = nodes.new("ShaderNodeMath")
        node.operation = "ADD"
        _sum_node(chain, node)
        for index, socket in enumerate(ins):
            links.new(socket, node.inputs[index])
        return node.outputs[0]

    color_sum = None
    rough_sum = None
    metal_sum = None
    normal_sum = None
    for k, entry in enumerate(entries):
        _lane(*weight_lane_ends[k])
        weight = _scalar("DIVIDE", sharpened[k], weight_total)
        slot = slots[k]
        links.new(weight, slot.inputs["Weight"])
        color_sum = (
            slot.outputs["Color"]
            if color_sum is None
            else _vec_sum("color", color_sum, slot.outputs["Color"])
        )
        rough_sum = (
            slot.outputs["Roughness"]
            if rough_sum is None
            else _scalar_sum("rough", rough_sum, slot.outputs["Roughness"])
        )
        metal_sum = (
            slot.outputs["Metalness"]
            if metal_sum is None
            else _scalar_sum("metal", metal_sum, slot.outputs["Metalness"])
        )
        normal_sum = (
            slot.outputs["Normal"]
            if normal_sum is None
            else _vec_sum("normal", normal_sum, slot.outputs["Normal"])
        )

    links.new(color_sum, bsdf.inputs["Base Color"])
    links.new(rough_sum, bsdf.inputs["Roughness"])
    links.new(metal_sum, bsdf.inputs["Metallic"])
    _lane(-180, -1240)
    links.new(_vec("NORMALIZE", normal_sum), bsdf.inputs["Normal"])
    try:
        bsdf.inputs["Specular IOR Level"].default_value = 0.5
    except Exception:
        pass
    return mat


def _terrain_water_material(water_props):
    """Translucent water for terrain slot 1 (WaterColor/WaterTransparency).

    Simple and pretty: water colour + alpha from the Terrain properties,
    full transmission with a glassy IOR, and a low-frequency noise bump so
    the surface catches light instead of reading as flat plastic.
    """
    import bpy

    mat = bpy.data.materials.new("Terrain_Water")
    mat.use_nodes = True
    tree = mat.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if bsdf is None:
        return mat
    color = water_props.get("color") or (0.047, 0.329, 0.361)
    try:
        color = tuple(max(0.0, min(1.0, float(c))) for c in color[:3])
    except (TypeError, ValueError):
        color = (0.047, 0.329, 0.361)
    try:
        transparency = max(0.0, min(1.0, float(water_props.get("transparency", 0.3))))
    except (TypeError, ValueError):
        transparency = 0.3
    alpha = 1.0 - transparency
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Alpha"].default_value = alpha
    bsdf.inputs["Roughness"].default_value = 0.1
    # Transmission got its own weight socket in Blender 4.0; the older
    # transmission socket covers both roles before that, and very old
    # builds lack either (water then just reads as translucent colour).
    try:
        bsdf.inputs["Transmission Weight"].default_value = 1.0
    except KeyError:
        try:
            bsdf.inputs["Transmission"].default_value = 1.0
        except KeyError:
            pass
    try:
        bsdf.inputs["IOR"].default_value = 1.33
    except KeyError:
        pass
    try:
        bsdf.inputs["Specular IOR Level"].default_value = 0.5
    except Exception:
        pass
    try:
        coords = tree.nodes.new("ShaderNodeTexCoord")
        noise = tree.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 0.22
        noise.inputs["Detail"].default_value = 4.0
        bump = tree.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.08
        bump.inputs["Distance"].default_value = 0.5
        tree.links.new(coords.outputs["Object"], noise.inputs["Vector"])
        tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
        tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    except Exception:
        pass
    try:
        mat.blend_method = "HASHED"
        mat.shadow_method = "HASHED"
        mat.show_transparent_back = True
    except Exception:
        pass
    return mat


def _terrain_material(material, tint, _mesh, timings=None):
    """Material for one terrain material, triplanar like the splat path.

    ``material`` is the smooth-grid's 6-bit material index; it maps through
    ``_TERRAIN_MATERIAL_ENUM`` to the same Enum.Material values BaseParts
    use.  Colour/roughness/metalness/swizzled normals sample through the
    world-position triplanar projection, so texel density stays uniform on
    slopes instead of stretching under a fixed-plane UV projection.
    Unknown slots (Water etc.) fall back to the flat colour.
    """
    enum = _TERRAIN_MATERIAL_ENUM.get(int(material))
    if enum is None:
        return _plain_terrain_material(material, tint)

    def timed(name, func):
        started = time.perf_counter()
        result = func()
        if timings is not None:
            timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started)
        return result

    try:
        from .textures import _builtin_material_for_entry

        entry = {
            "material": enum,
            "color": tint,
            "name": f"Terrain_{material}",
        }
        if _builtin_material_for_entry(entry) is not None:
            return timed(
                "triplanar material",
                lambda: _terrain_triplanar_material(entry, tint),
            )
    except Exception as exc:
        print(f"[Terrain] Builtin material skipped: {exc}")
    return _plain_terrain_material(material, tint)


def _terrain_triplanar_material(entry, tint):
    """Triplanar builtin material for the solid terrain path.

    The same world-position triplanar chain as the splat slots (colour,
    roughness, metalness and swizzled normals), but a single material at
    weight 1 with no rotation.  Any fixed-plane UV projection stretches
    texels on slopes (density scales with the slope angle), while the
    triplanar projection keeps texel density uniform everywhere.
    """
    import bpy

    from .textures import (
        _BUILTIN_MATERIAL_STUDS_PER_TILE,
        _builtin_map_ref,
        _builtin_material_for_entry,
    )

    try:
        tint = tuple(max(0.0, min(1.0, float(c))) for c in tint[:3])
    except (TypeError, ValueError, IndexError):
        tint = (0.8, 0.8, 0.8)
    builtin = _builtin_material_for_entry(entry)
    maps = builtin["maps"] if builtin else ("", "", "", "")
    slot_entry = dict(entry)
    slot_entry["tint"] = tint
    slot_entry["color_ref"] = _builtin_map_ref(maps[0]) if builtin else ""
    slot_entry["normal_ref"] = _builtin_map_ref(maps[1]) if builtin else ""
    slot_entry["metal_ref"] = _builtin_map_ref(maps[2]) if builtin else ""
    slot_entry["rough_ref"] = _builtin_map_ref(maps[3]) if builtin else ""

    mat = bpy.data.materials.new(entry["name"])
    mat.use_nodes = True
    tree = mat.node_tree
    nodes = tree.nodes
    links = tree.links
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        return mat
    bsdf.location = (0, -300)
    output = nodes.get("Material Output")
    if output is not None:
        output.location = (280, -300)

    def _scalar(operation, *ins):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        for index, socket in enumerate(ins):
            links.new(socket, node.inputs[index])
        return node.outputs[0]

    # World-anchored position in tile units, same density as parts/splat.
    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.name = "RBX Terrain TexCoord"
    texcoord.location = (500, 700)
    pos_scale = nodes.new("ShaderNodeVectorMath")
    pos_scale.operation = "SCALE"
    pos_scale.inputs["Scale"].default_value = 1.0 / _BUILTIN_MATERIAL_STUDS_PER_TILE
    pos_scale.location = (700, 700)
    links.new(texcoord.outputs["Object"], pos_scale.inputs[0])
    pos = pos_scale.outputs[0]

    # Triplanar blend weights from the shading normal, renormalised to 1.
    geometry = nodes.new("ShaderNodeNewGeometry")
    geometry.location = (500, 460)
    abs_normal = nodes.new("ShaderNodeVectorMath")
    abs_normal.operation = "ABSOLUTE"
    abs_normal.location = (700, 460)
    links.new(geometry.outputs["Normal"], abs_normal.inputs[0])
    separate = nodes.new("ShaderNodeSeparateXYZ")
    separate.location = (900, 460)
    links.new(abs_normal.outputs[0], separate.inputs["Vector"])
    nx, ny, nz = separate.outputs["X"], separate.outputs["Y"], separate.outputs["Z"]
    total = _scalar("ADD", _scalar("ADD", nx, ny), nz)
    wx = _scalar("DIVIDE", nx, total)
    wy = _scalar("DIVIDE", ny, total)
    wz = _scalar("DIVIDE", nz, total)

    images = (
        _splat_constant_image("RBX Splat White", (1.0, 1.0, 1.0, 1.0)),
        _splat_constant_image("RBX Splat Black", (0.0, 0.0, 0.0, 1.0)),
        _splat_constant_image("RBX Splat Flat Normal", (0.5, 0.5, 1.0, 1.0)),
    )
    slot = nodes.new("ShaderNodeGroup")
    slot.node_tree = _build_slot_group(slot_entry, images)
    slot.name = f"RBX Terrain Material {entry['name']}"
    slot.label = entry["name"]
    slot.location = (-600, 0)
    one = nodes.new("ShaderNodeValue")
    one.outputs[0].default_value = 1.0
    zero = nodes.new("ShaderNodeValue")
    zero.outputs[0].default_value = 0.0
    links.new(one.outputs[0], slot.inputs["Weight"])
    links.new(zero.outputs[0], slot.inputs["Border"])
    links.new(pos, slot.inputs["Position"])
    links.new(wx, slot.inputs["Blend X"])
    links.new(wy, slot.inputs["Blend Y"])
    links.new(wz, slot.inputs["Blend Z"])
    # Cell Color left unlinked: zero rotation, clean upright tiling.

    links.new(slot.outputs["Color"], bsdf.inputs["Base Color"])
    links.new(slot.outputs["Roughness"], bsdf.inputs["Roughness"])
    links.new(slot.outputs["Metalness"], bsdf.inputs["Metallic"])
    normalize = nodes.new("ShaderNodeVectorMath")
    normalize.operation = "NORMALIZE"
    links.new(slot.outputs["Normal"], normalize.inputs[0])
    links.new(normalize.outputs[0], bsdf.inputs["Normal"])
    try:
        bsdf.inputs["Specular IOR Level"].default_value = 0.5
    except Exception:
        pass
    return mat


def _plain_terrain_material(material, color):
    """Flat-colour fallback for terrain materials without a builtin map."""
    import bpy

    mat = bpy.data.materials.new(f"Terrain_{material}")
    mat.use_nodes = True
    tree = mat.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if bsdf is None:
        return mat
    bsdf.inputs["Base Color"].default_value = (
        color[0], color[1], color[2], 1.0
    )
    bsdf.inputs["Roughness"].default_value = 1.0
    try:
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
    except Exception:
        pass
    # Subtle large-scale tonal variation so flat material colors read
    # like ground instead of plastic.  Object coordinates align across
    # all terrain objects (they all sit at the world origin).
    try:
        coords = tree.nodes.new("ShaderNodeTexCoord")
        noise = tree.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 0.12
        noise.inputs["Detail"].default_value = 6.0
        tree.links.new(coords.outputs["Object"], noise.inputs["Vector"])
        mix = tree.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MIX"
        dark = (
            color[0] * 0.72, color[1] * 0.72, color[2] * 0.72, 1.0
        )
        light = (
            min(1.0, color[0] * 1.12 + 0.04),
            min(1.0, color[1] * 1.12 + 0.04),
            min(1.0, color[2] * 1.12 + 0.04),
            1.0,
        )
        # Blender 5.1 exposes A/B/Factor/Result once per data type, so the
        # sockets must be picked by TYPE or the float variants swallow the
        # colour assignments and the mix collapses to grey.
        from .textures import _socket_by_type

        _socket_by_type(mix, "input", "A", "RGBA").default_value = dark
        _socket_by_type(mix, "input", "B", "RGBA").default_value = light
        damp = tree.nodes.new("ShaderNodeMath")
        damp.operation = "MULTIPLY"
        damp.inputs[1].default_value = 0.35
        tree.links.new(noise.outputs["Fac"], damp.inputs[0])
        tree.links.new(
            damp.outputs["Value"],
            _socket_by_type(mix, "input", "Factor", "VALUE"),
        )
        tree.links.new(
            _socket_by_type(mix, "output", "Result", "RGBA"),
            bsdf.inputs["Base Color"],
        )
    except Exception:
        pass
    return mat


def terrain_meta_from_instances(instances) -> Optional[dict]:
    """Extract the raw terrain payload from parsed rbxm instances."""
    for instance in instances.values():
        if instance.class_name != "Terrain":
            continue
        grid = instance.props.get("SmoothGrid")
        if grid is None:
            return None
        if isinstance(grid, str):
            grid = grid.encode("latin-1")
        colors = instance.props.get("MaterialColors")
        if isinstance(colors, str):
            colors = colors.encode("latin-1")
        water_color = instance.props.get("WaterColor") or [0.047, 0.329, 0.361]
        try:
            water_color = [float(c) for c in water_color[:3]]
        except (TypeError, ValueError):
            water_color = [0.047, 0.329, 0.361]
        return {
            "smoothgrid": bytes(grid),
            "colors": bytes(colors or b""),
            "water": {
                "color": water_color,
                "transparency": float(instance.props.get("WaterTransparency") or 0.3),
                "reflectance": float(instance.props.get("WaterReflectance") or 1.0),
            },
        }
    return None
