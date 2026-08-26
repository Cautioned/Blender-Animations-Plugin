"""
Part name matching, fingerprinting, and mesh lookup for rig creation.

Name canonicalization (Studio prefixes, class suffixes, Blender .001
suffixes), centroid/position fingerprinting, the match-context builder,
and the two-pass AUX rename flow.  Leaf module: no dependencies on the
rest of the rig package beyond core constants/utils.
"""

import re
import bpy
from mathutils import Matrix

from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat


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


def _get_wrap_layer_metadata(entry):
    wrap_layer = entry.get("wrap_layer") if isinstance(entry, dict) else None
    return wrap_layer if isinstance(wrap_layer, dict) else None


def _get_wrap_target_metadata(entry):
    wrap_target = entry.get("wrap_target") if isinstance(entry, dict) else None
    return wrap_target if isinstance(wrap_target, dict) else None


def _strip_model_prefix(name: str) -> str:
    """Drop the Studio-style "<Model>.model/" prefix from an object name."""
    if name and "/" in name:
        return name.rsplit("/", 1)[1]
    return name or ""


def _strip_class_suffix(name: str) -> str:
    """Drop the exact-class suffix (".basepart"/".meshpart"/...) from a name."""
    base = _strip_suffix(name)
    for suffix in (".basepart", ".meshpart", ".unionoperation", ".wedgepart",
                   ".cornerwedgepart", ".model", ".accessory", ".folder", ".part"):
        if base.lower().endswith(suffix):
            return base[: -len(suffix)]
    return base


def _match_stem(name: str) -> str:
    """Canonical comparison key: no Blender suffix, model prefix, or class tag."""
    return _strip_class_suffix(_strip_model_prefix(name or "")).lower()


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
    # Studio-style names ("Roblox.model/X.unionoperation") still match the
    # bare part name ("X") used by the rig tree and metadata.
    stem = _match_stem(part_name)
    for candidate in parts_collection.objects:
        if _match_stem(candidate.name) == stem:
            return candidate
    return None


def _entry_object_name(entry):
    """Object name an entry was built under (wrap layers use the layer name)."""
    wrap_layer = _get_wrap_layer_metadata(entry)
    if wrap_layer and wrap_layer.get("name"):
        return wrap_layer["name"]
    return (entry or {}).get("name")


def _find_parts_object_for_entry(parts_collection, entry):
    """Find the built object for an entry by its rbxm instance referent,
    falling back to the parse index and then to name matching."""
    if not isinstance(entry, dict):
        return None
    inst_ref = entry.get("inst_ref")
    if inst_ref is not None:
        inst_ref = int(inst_ref)
        for candidate in parts_collection.objects:
            try:
                if candidate.get("RBXInstRef") == inst_ref:
                    return candidate
            except Exception:
                continue
    idx = entry.get("idx")
    if idx is not None:
        idx = int(idx)
        for candidate in parts_collection.objects:
            try:
                if candidate.get("RBXPartIdx") == idx:
                    return candidate
            except Exception:
                continue
    return _find_parts_object(parts_collection, _entry_object_name(entry))


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


def _find_matching_part(aux_name, aux_cf, match_ctx, inst_ref=None):
    """Resolve an aux entry to a mesh.

    Priority order:
    1. rbxm instance referent (RBXInstRef) when available
    2. Fingerprint object map (authoritative, from index-based matching)
    3. Name-based lookup with side + position tiebreaking (for duplicates)
    4. Position fingerprint (last resort)
    """
    used = match_ctx["used"]

    # Authoritative pass: the rbxm tells us exactly which instance this is.
    if inst_ref is not None:
        parts_collection = match_ctx.get("parts_collection")
        if parts_collection is not None:
            for candidate in parts_collection.objects:
                if candidate.type != "MESH":
                    continue
                if candidate in used:
                    continue
                try:
                    if candidate.get("RBXInstRef") == int(inst_ref):
                        print(
                            f"[_find_matching_part] INST_REF HIT: '{aux_name}' (ref={inst_ref}) -> mesh '{candidate.name}'")
                        return candidate
                except Exception:
                    continue

    t2b = match_ctx.get("t2b") or get_transform_to_blender()
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
    # .001/.002 suffix for duplicates). Exact suffixed names are authoritative;
    # unsuffixed names still use base-name matching plus position tiebreaking.
    fp_map = match_ctx.get("fingerprint_object_map", {})
    if aux_name and fp_map:
        aux_key = aux_name or ""
        aux_key_lower = aux_key.lower()
        aux_base_lower = _strip_suffix(aux_key).lower()
        aux_has_suffix = aux_base_lower != aux_key_lower
        exact_fp_exists = any((k or "").lower() == aux_key_lower for k in fp_map.keys())
        exact_fp_candidates = []
        base_fp_candidates = []
        for obj_name, obj in fp_map.items():
            if obj in used:
                continue
            obj_key = obj_name or ""
            obj_key_lower = obj_key.lower()
            if obj_key_lower == aux_key_lower:
                exact_fp_candidates.append(obj)
            elif _strip_suffix(obj_key).lower() == aux_base_lower:
                base_fp_candidates.append(obj)

        if aux_has_suffix and exact_fp_exists:
            # Suffixed query whose exact key exists in fp_map — exact only.
            # (e.g. "S26_low.007" should only match "S26_low.007", not
            # fall back to "S26_low.008" through base-name matching.)
            fp_candidates = exact_fp_candidates
        elif not aux_has_suffix and not exact_fp_exists:
            # Unsuffixed query with no exact key in fp_map — do NOT match via
            # base-name fallback here.  Handled by name_index instead, which
            # includes position gating and side checks.
            # (prevents "Cylinder" matching "Cylinder.001" in fp_map)
            fp_candidates = []
        else:
            # Remaining cases:
            #   a) unsuffixed + exact exists → exact + base for position disambig
            #      (e.g. "Hand" matches both "Hand" and "Hand.001" in fp_map)
            #   b) suffixed + no exact → exact + base fallback
            #      (e.g. "Hand.001" when fp_map only has "Hand.002")
            fp_candidates = exact_fp_candidates + base_fp_candidates

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
                print(
                    f"[_find_matching_part] FINGERPRINT HIT (pos disambig, {len(fp_candidates)} cands): '{aux_name}' -> mesh '{obj.name}' (dist={_fp_dist(obj):.4f})")
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
            has_any = any(
                (k or "").lower() == aux_key_lower
                or _strip_suffix(k or "").lower() == aux_base_lower
                for k in fp_map.keys()
            )
            if has_any:
                print(f"[_find_matching_part] FINGERPRINT found but all used: '{aux_name}'")
                if aux_has_suffix and exact_fp_exists:
                    return None
            else:
                print(
                    f"[_find_matching_part] FINGERPRINT MISS: '{aux_name}' not in map (map has {len(fp_map)} entries)")

    # Fallback: Name-based candidates (base name match, ignoring suffixes)
    # WITH SIDE CHECK + POSITION TIEBREAKING for multiple candidates
    name_index = match_ctx.get("name_index", {})
    candidates = []
    if base_name and base_name in name_index:
        for obj in name_index[base_name]:
            if obj not in used:
                candidates.append(obj)

    if candidates:
        if len(candidates) == 1:
            obj = candidates[0]
            if not _side_ok(obj):
                print(
                    f"[_find_matching_part] NAME MATCH '{aux_name}' -> '{obj.name}' BUT WRONG SIDE (using anyway, only candidate)")
            return obj
        # Multiple candidates — filter by side first, then distance
        side_ok_cands = [o for o in candidates if _side_ok(o)]
        pool = side_ok_cands if side_ok_cands else candidates
        if len(pool) == 1:
            print(
                f"[_find_matching_part] NAME+SIDE: '{aux_name}' -> '{pool[0].name}' (1 on correct side of {len(candidates)})")
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
                print(
                    f"[_find_matching_part] NAME+SIDE+POS: '{aux_name}' -> '{best_obj.name}' (dist={best_dist:.4f}, {len(candidates)} candidates)")
                return best_obj
            else:
                print(
                    f"[_find_matching_part] NAME+SIDE+POS REJECTED: '{aux_name}' best '{best_obj.name}' too far ({best_dist:.4f})")
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
                        print(
                            f"[_find_matching_part] POS FINGERPRINT '{aux_name}' -> '{obj.name}' WRONG SIDE, skipping")
                        continue
                    return obj
        except Exception:
            pass
    return None


def _apply_fingerprint_renames(rig_def, match_ctx, allow_aux_renames=True, all_bone_names=None):
    """Rename meshes by comparing position fingerprints from rig metadata.

    Collects all renames first, then applies via two-pass temp-name approach
    to avoid blender's auto-suffixing (.001) corrupting other objects' names.
    """
    if not allow_aux_renames:
        return

    name_index = match_ctx["name_index"]
    pending = []  # (obj, aux_name)
    all_bone_names = all_bone_names or set()

    def walk(node):
        # Collect child names to skip overlapping AUX renames
        child_part_names = set()
        for child in node.get("children") or []:
            jname = child.get("jname")
            pname = child.get("pname")
            if jname:
                child_part_names.add(jname.lower())
            if pname:
                child_part_names.add(pname.lower())

        aux_list = node.get("aux") or []
        aux_tf = node.get("auxTransform") or []
        for idx, aux_name in enumerate(aux_list):
            if not aux_name:
                continue
            aux_lower = aux_name.lower()
            if aux_lower in child_part_names or aux_lower in all_bone_names:
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
