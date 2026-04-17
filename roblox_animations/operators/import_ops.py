"""
Import operators for rig and animation data.
"""

import json
import base64
import re
import bpy
from bpy_extras.io_utils import ImportHelper
from ..core.utils import get_unique_name, get_object_by_name, iter_scene_objects
from ..core.utils import cf_to_mat, mat_to_cf
from ..core.constants import get_transform_to_blender
from ..rig.creation import create_rig, get_unique_collection_name
from contextlib import contextmanager


@contextmanager
def _ensure_all_bone_collections_visible(armature):
    """Temporarily unhide all bone collections on an armature so that
    edit_bones can access bones in hidden collections.  Restores original
    visibility on exit.  Safe on pre-4.0 builds that lack bone collections."""
    saved = {}
    collections = getattr(armature.data, "collections", None)
    if collections is not None:
        for bc in collections:
            saved[bc.name] = bc.is_visible
            bc.is_visible = True
    try:
        yield
    finally:
        if collections is not None:
            for bc in collections:
                if bc.name in saved:
                    bc.is_visible = saved[bc.name]


def _strip_suffix(name: str) -> str:
    return re.sub(r"\.\d+$", "", name or "")


def _resolve_imported_obj_name(name: str, known_names=None) -> str:
    """Resolve Blender OBJ-import numeric suffixes against known target names.

    Blender's OBJ importer often appends a trailing digit when duplicate object
    names collide, e.g. "Sword" -> "Sword1" or "AccessoryDW3" ->
    "Accessorydw31". Only collapse that suffix when doing so matches a known
    metadata target name.
    """
    base_name = _strip_suffix(name).lower()
    if not known_names or base_name in known_names:
        return base_name

    match = re.match(r"^(.*?)(\d+)$", base_name)
    if not match:
        return base_name

    prefix, digits = match.groups()
    for trim_count in range(1, len(digits) + 1):
        candidate = prefix + digits[:-trim_count]
        if candidate in known_names:
            return candidate

    return base_name


def _dict_get_any(data, keys):
    """Get first present non-empty value for any key (case/underscore-insensitive)."""
    if not isinstance(data, dict):
        return None

    for key in keys:
        if key in data:
            value = data.get(key)
            if value not in (None, ""):
                return value

    normalized = {}
    for k, v in data.items():
        if not isinstance(k, str):
            continue
        nk = k.replace("_", "").lower()
        if nk not in normalized:
            normalized[nk] = v

    for key in keys:
        nk = key.replace("_", "").lower()
        if nk in normalized:
            value = normalized[nk]
            if value not in (None, ""):
                return value
    return None


def _coerce_cf12(value):
    """Best-effort coercion to a 12-number CFrame array."""
    if value is None:
        return None
    try:
        if hasattr(value, "to_list"):
            value = value.to_list()
        elif not isinstance(value, (list, tuple)):
            value = list(value)
    except Exception:
        return None

    if len(value) < 12:
        return None
    return [value[i] for i in range(12)]


def _joint_transform_key(node, *keys):
    return _coerce_cf12(_dict_get_any(node, keys))


def _get_joint_part_world_matrix(node):
    transform = _joint_transform_key(node, "transform")
    if not transform:
        return None
    return cf_to_mat(transform)


def _get_joint_anchor_world_matrix(node):
    part_world = _get_joint_part_world_matrix(node)
    if part_world is None:
        return None

    child_offset = _joint_transform_key(node, "jointtransform1", "jointTransform1")
    if child_offset:
        return part_world @ cf_to_mat(child_offset)
    return part_world


def _matrix_difference_score(left, right):
    if left is None or right is None:
        return float("inf")

    translation_error = (left.to_translation() - right.to_translation()).length
    rotation_error = 0.0
    for row in range(3):
        for col in range(3):
            rotation_error += abs(left[row][col] - right[row][col])

    return translation_error + rotation_error


def _annotate_weapon_original_parents(joints_tree, attachment_parent_name, attachment_parent_transform):
    """Recover the original Motor6D parent for imported weapon bones."""
    assignments = {}

    if not isinstance(joints_tree, dict) or not attachment_parent_name or attachment_parent_transform is None:
        return assignments

    def recurse(node, candidates):
        if not isinstance(node, dict):
            return

        joint_name = node.get("jname")
        node["originalParentBone"] = attachment_parent_name
        if joint_name:
            assignments[joint_name] = attachment_parent_name

        child_anchor_world = _get_joint_anchor_world_matrix(node)
        parent_offset = _joint_transform_key(node, "jointtransform0", "jointTransform0")
        if child_anchor_world is not None and parent_offset and candidates:
            parent_offset_mat = cf_to_mat(parent_offset)
            best_parent_name = attachment_parent_name
            best_score = float("inf")

            for candidate_name, candidate_part_world in candidates:
                predicted_child_anchor = candidate_part_world @ parent_offset_mat
                score = _matrix_difference_score(predicted_child_anchor, child_anchor_world)
                if score < best_score:
                    best_score = score
                    best_parent_name = candidate_name

            node["originalParentBone"] = best_parent_name
            if joint_name:
                assignments[joint_name] = best_parent_name

        for child in node.get("children", []) or []:
            recurse(child, candidates)

    recurse(joints_tree, [(attachment_parent_name, attachment_parent_transform)])
    return assignments


def _norm_name(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s_]+", "", value).lower()


def _iter_dicts_recursive(node, depth=0, max_depth=24):
    """Yield (dict_node, depth) for nested dict/list payloads."""
    if depth > max_depth:
        return
    if isinstance(node, dict):
        yield node, depth
        for child in node.values():
            if isinstance(child, (dict, list, tuple)):
                yield from _iter_dicts_recursive(child, depth + 1, max_depth)
    elif isinstance(node, (list, tuple)):
        for child in node:
            if isinstance(child, (dict, list, tuple)):
                yield from _iter_dicts_recursive(child, depth + 1, max_depth)


def _iter_part_aux_entries(meta_loaded):
    if not isinstance(meta_loaded, dict):
        return []
    part_aux = meta_loaded.get("partAux") or []
    if isinstance(part_aux, dict):
        return list(part_aux.values())
    return list(part_aux)


def _normalize_accessory_handle_jnames(meta_loaded):
    """Rewrite stale accessory Handle joint names to their exported part names.

    Older Studio exports often encode accessory weld nodes with joint names like
    Handle/Handle1 while the actual exported part name lives in pname. That leaks
    into Blender bone creation and fallback matching. Normalize those nodes early
    so the importer consistently uses the exported accessory part name.
    """
    if not isinstance(meta_loaded, dict):
        return 0

    renamed = 0

    def recurse(node):
        nonlocal renamed

        if not isinstance(node, dict):
            return

        joint_type = str(node.get("jointType") or "")
        jname = node.get("jname")
        pname = node.get("pname")
        if (
            joint_type in {"Weld", "WeldConstraint"}
            and isinstance(jname, str)
            and isinstance(pname, str)
            and jname.lower().startswith("handle")
            and not pname.lower().startswith("handle")
            and pname.strip()
        ):
            node["jname"] = pname
            renamed += 1

        for child in node.get("children") or []:
            recurse(child)

    recurse(meta_loaded.get("rig"))
    recurse(meta_loaded.get("joints"))
    for attachment in meta_loaded.get("weaponAttachments") or []:
        if isinstance(attachment, dict):
            recurse(attachment.get("joints"))

    return renamed


def _meta_has_skinned_meshes(meta_loaded):
    """Return True when metadata explicitly marks any skinned mesh part entries."""
    for entry in _iter_part_aux_entries(meta_loaded):
        if isinstance(entry, dict) and entry.get("has_skinning") and entry.get("mesh_id"):
            return True
    return False


def _meta_is_majority_skinned(meta_loaded):
    """Return True when most body-part MeshParts have explicit skinning data.

    Distinguishes proper skinned rigs (most limbs skinned -> CONNECT) from
    hybrid rigs (only a few parts skinned, e.g. head -> LOCAL_YAXIS_EXTEND).
    """
    total = 0
    skinned = 0
    for entry in _iter_part_aux_entries(meta_loaded):
        if not isinstance(entry, dict):
            continue
        if not entry.get("mesh_id"):
            continue
        mesh_class = entry.get("mesh_class")
        if mesh_class not in (None, "", "MeshPart"):
            continue
        # skip accessories (wrap layers) — only count body parts
        if entry.get("wrap_layer"):
            continue
        total += 1
        if entry.get("has_skinning"):
            skinned += 1
    return total > 0 and skinned > total / 2


def _meta_has_filemesh_candidates(meta_loaded):
    """Return True when metadata includes any MeshPart filemesh candidates.

    Studio does not always expose skinning via Bone descendants, so Blender must
    sometimes inspect the FileMesh directly to determine whether weights exist.
    """
    for entry in _iter_part_aux_entries(meta_loaded):
        if not isinstance(entry, dict):
            continue
        if not entry.get("mesh_id"):
            continue
        mesh_class = entry.get("mesh_class")
        if mesh_class in (None, "", "MeshPart"):
            return True
    return False


def _rig_contains_deform_bones(node):
    """Return True when the exported rig tree contains deform bone nodes."""
    if not isinstance(node, dict):
        return False
    if node.get("isDeformBone") or node.get("jointType") == "Bone":
        return True
    for child in node.get("children", []):
        if _rig_contains_deform_bones(child):
            return True
    return False


def _extract_motor6d_connection(meta_loaded, weapon_root_name, preferred_parent_name=None):
    """Find Motor6D-like connection data anywhere in metadata payload.

    Returns dict with parent_name/connectionC0/connectionC1 when found.
    """
    if not isinstance(meta_loaded, dict):
        return None

    root_norm = _norm_name(weapon_root_name)
    pref_norm = _norm_name(preferred_parent_name)
    if not root_norm:
        return None

    part0_keys = ("part0", "Part0", "parentPart", "parent_part", "parent", "from")
    part1_keys = ("part1", "Part1", "childPart", "child_part", "child", "to")

    c0_keys = (
        "connectionC0",
        "connection_c0",
        "C0",
        "c0",
        "jointtransform0",
        "jointTransform0",
    )
    c1_keys = (
        "connectionC1",
        "connection_c1",
        "C1",
        "c1",
        "jointtransform1",
        "jointTransform1",
    )

    best = None

    for node, depth in _iter_dicts_recursive(meta_loaded):
        c0 = _coerce_cf12(_dict_get_any(node, c0_keys))
        c1 = _coerce_cf12(_dict_get_any(node, c1_keys))
        if not (c0 and c1):
            continue

        part0 = _dict_get_any(node, part0_keys)
        part1 = _dict_get_any(node, part1_keys)
        if not (isinstance(part0, str) and isinstance(part1, str)):
            continue

        p0 = _norm_name(part0)
        p1 = _norm_name(part1)

        reverse = False
        score = 0

        if p1 == root_norm:
            score += 8
        elif p0 == root_norm:
            score += 6
            reverse = True
        else:
            continue

        parent_name = part0 if not reverse else part1
        parent_norm = p0 if not reverse else p1
        if pref_norm and parent_norm == pref_norm:
            score += 4

        jt = _dict_get_any(node, ("jointType", "joint_type", "type", "ClassName", "className"))
        if isinstance(jt, str) and "motor6d" in jt.lower():
            score += 1

        # Prefer shallower nodes when score ties.
        score -= depth * 0.01

        if reverse:
            # Swapped relation: root*C0 = parent*C1  -> parent*C1 = root*C0
            use_c0, use_c1 = c1, c0
        else:
            use_c0, use_c1 = c0, c1

        candidate = {
            "parent_name": parent_name,
            "connectionC0": use_c0,
            "connectionC1": use_c1,
            "jointType": jt or "Motor6D",
            "score": score,
            "depth": depth,
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate

    return best


def _collect_weapon_suggested_bones(meta_loaded):
    """Collect unique suggested attachment bones from weapon metadata."""
    if not isinstance(meta_loaded, dict):
        return []

    suggested = []
    top = _dict_get_any(
        meta_loaded,
        (
            "suggestedBone",
            "suggested_bone",
            "attachmentBone",
            "attachBone",
            "parentBone",
            "parent_bone",
        ),
    ) or ""
    if isinstance(top, str) and top:
        suggested.append(top)

    attachments = meta_loaded.get("weaponAttachments")
    if isinstance(attachments, list):
        for att in attachments:
            if not isinstance(att, dict):
                continue
            sb = _dict_get_any(
                att,
                (
                    "suggestedBone",
                    "suggested_bone",
                    "attachmentBone",
                    "attachBone",
                    "parentBone",
                    "parent_bone",
                ),
            ) or ""
            if isinstance(sb, str) and sb:
                suggested.append(sb)

    unique = []
    seen = set()
    for name in suggested:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique


def _find_bone_case_insensitive(armature, suggested):
    """Return matched bone name from armature or None."""
    if not armature or armature.type != "ARMATURE" or not suggested:
        return None
    if suggested in armature.data.bones:
        return suggested
    suggested_lower = suggested.lower()
    for bone in armature.data.bones:
        if bone.name.lower() == suggested_lower:
            return bone.name
    return None


def _infer_weapon_parent_bone_from_transform(armature, joints_tree):
    """Infer likely parent bone by nearest rest-transform position.

    Uses Roblox transform props when available (preferred), falls back to
    armature-space bone heads for older/partial rigs.
    """
    from mathutils import Matrix

    if (
        not armature
        or armature.type != "ARMATURE"
        or not isinstance(joints_tree, dict)
        or not joints_tree.get("transform")
    ):
        return None, None

    try:
        t2b = get_transform_to_blender()
        weapon_root_pos = (t2b @ cf_to_mat(joints_tree["transform"])).to_translation()
    except Exception:
        return None, None

    best_name = None
    best_dist = None

    for bone in armature.data.bones:
        bone_pos = None
        tf_prop = bone.get("transform")
        if tf_prop:
            try:
                bone_mat = Matrix([list(row) for row in tf_prop])
                bone_pos = (t2b @ bone_mat).to_translation()
            except Exception:
                bone_pos = None

        if bone_pos is None:
            try:
                bone_pos = armature.matrix_world @ bone.head_local
            except Exception:
                continue

        dist = (weapon_root_pos - bone_pos).length
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_name = bone.name

    return best_name, best_dist


def _should_redirect_weapon_import(selected_armature, source_armature, meta_loaded):
    """Decide whether to redirect weapon import from selected rig to source rig.
    Redirect when the selected rig looks like a proxy/control rig, or when it
    is missing suggested attach bones that exist on the detected source rig."""
    if not selected_armature or not source_armature:
        return False

    # Prefer armatures that carry imported Roblox bone transforms.
    # Proxy/control rigs typically do not have these custom props.
    selected_has_transform = any(
        "transform" in b for b in selected_armature.data.bones
    )
    source_has_transform = any(
        "transform" in b for b in source_armature.data.bones
    )
    if source_has_transform and not selected_has_transform:
        return True

    suggested_bones = _collect_weapon_suggested_bones(meta_loaded)
    if not suggested_bones:
        return False

    missing_on_selected = [
        bone_name for bone_name in suggested_bones
        if not _find_bone_case_insensitive(selected_armature, bone_name)
    ]
    if not missing_on_selected:
        return False

    return all(
        _find_bone_case_insensitive(source_armature, bone_name)
        for bone_name in missing_on_selected
    )


def _dims_to_ratios(sorted_dims):
    """Compute scale-invariant aspect ratios from sorted dimensions.
    
    Returns (r1, r2) where r1 = dim[0]/dim[2], r2 = dim[1]/dim[2].
    These are invariant to uniform scaling and highly discriminating
    for small parts that share similar absolute sizes.
    """
    if len(sorted_dims) < 3 or sorted_dims[2] < 1e-9:
        return (1.0, 1.0)
    return (sorted_dims[0] / sorted_dims[2], sorted_dims[1] / sorted_dims[2])


def _hungarian_assign(cost_matrix, n_targets, n_cands):
    """Optimal assignment via hungarian algorithm with fallback.
    
    Tries scipy first, falls back to a pure-python implementation
    bc blender's bundled python may not have scipy.
    """
    try:
        from scipy.optimize import linear_sum_assignment
        row_ind, col_ind = linear_sum_assignment(cost_matrix)
        return list(zip(row_ind.tolist(), col_ind.tolist()))
    except ImportError:
        pass
    
    # fallback: greedy assignment by ascending cost (not optimal but
    # still globally-aware — much better than per-target greedy)
    import numpy as np
    n_rows, n_cols = cost_matrix.shape
    flat_indices = np.argsort(cost_matrix, axis=None)
    used_rows = set()
    used_cols = set()
    assignments = []
    for flat_idx in flat_indices:
        r = int(flat_idx // n_cols)
        c = int(flat_idx % n_cols)
        if r in used_rows or c in used_cols:
            continue
        assignments.append((r, c))
        used_rows.add(r)
        used_cols.add(c)
        if len(assignments) >= min(n_rows, n_cols):
            break
    return assignments


def _rename_parts_by_size_fingerprint(meta_loaded, parts_collection):
    """Rename parts using size fingerprints, aspect ratios, and position data.
    
    Uses global optimal assignment (hungarian/munkres) instead of greedy
    matching, with scale-invariant aspect ratios for better discrimination
    of small parts.
    """
    import bmesh
    import numpy as np
    from collections import defaultdict
    
    part_aux_raw = meta_loaded.get("partAux")
    if not part_aux_raw:
        return 0

    # lua arrays with numeric keys come through as dicts {"1": ..., "2": ...}
    if isinstance(part_aux_raw, dict):
        part_aux_list = list(part_aux_raw.values())
    else:
        part_aux_list = part_aux_raw

    rig_name = meta_loaded.get("rigName", "Rig")
    num_targets = len(part_aux_list)
    print(f"[RigImport] Fingerprint matching {num_targets} targets...")
    
    # Build expected position map from rig definition.
    # Maps lowercase name -> list[Vector] to handle duplicate names
    # (e.g. multiple joints named "Part").
    expected_loc_by_name = defaultdict(list)
    rig_def = meta_loaded.get("rig")
    t2b = get_transform_to_blender()
    if rig_def:
        def collect_expected_positions(node, depth=0):
            if not node:
                return
            jname = node.get("jname") or node.get("pname") or ""
            node_transform = node.get("transform")
            if jname and node_transform:
                try:
                    expected_loc = (t2b @ cf_to_mat(node_transform)).to_translation()
                    expected_loc_by_name[jname.lower()].append(expected_loc)
                except Exception:
                    pass

            aux_names = node.get("aux") or []
            aux_transforms = node.get("auxTransform") or []
            for idx, aux_name in enumerate(aux_names):
                if not aux_name:
                    continue
                cf = aux_transforms[idx] if idx < len(aux_transforms) else None
                if not cf:
                    continue
                try:
                    expected_loc = (t2b @ cf_to_mat(cf)).to_translation()
                    expected_loc_by_name[aux_name.lower()].append(expected_loc)
                except Exception:
                    pass

            for child in (node.get("children") or []):
                collect_expected_positions(child, depth + 1)

        collect_expected_positions(rig_def)

    # Pre-process targets — now includes aspect ratios
    fp_targets = []
    for item in part_aux_list:
        if not item or not isinstance(item, dict) or "idx" not in item:
            continue
        
        idx = item["idx"]
        target_name = item.get("name", f"{rig_name}{idx}")
        target_lower = str(target_name).lower()
        target_family = "accessory" if (target_lower.startswith("handle") or item.get("wrap_layer")) else "body"
        
        dims = item.get("dims_fp")
        if dims and len(dims) == 3:
            sorted_dims = tuple(sorted([float(x) for x in dims]))
            ratios = _dims_to_ratios(sorted_dims)
            fp_targets.append({
                "target": target_name,
                "family": target_family,
                "dims": sorted_dims,
                "ratios": ratios,
                "sig": sum(sorted_dims),
                "is_vol": False,
                "is_wrap_layer": bool(item.get("wrap_layer")),
            })
        elif "vol_fp" in item:
            vol = float(item["vol_fp"])
            fp_targets.append({
                "target": target_name,
                "family": target_family,
                "dims": (vol,),
                "ratios": (1.0, 1.0),
                "sig": vol,
                "is_vol": True,
                "is_wrap_layer": bool(item.get("wrap_layer")),
            })

    if not fp_targets:
        return 0

    known_target_names = {item["target"].lower(): item.get("family") or "body" for item in fp_targets}

    mesh_objects = [o for o in parts_collection.objects if o.type == "MESH"]
    mesh_centers = {obj: _get_mesh_world_center(obj) for obj in mesh_objects}

    # Build candidate data — now includes aspect ratios
    all_candidates = []
    
    for obj in mesh_objects:
        base_name = _resolve_imported_obj_name(obj.name, known_target_names)
        candidate_family = known_target_names.get(
            base_name,
            "accessory" if base_name.startswith("handle") else "body",
        )
        d = obj.dimensions
        sorted_dims = tuple(sorted([d.x, d.y, d.z]))
        sig = sum(sorted_dims)
        ratios = _dims_to_ratios(sorted_dims)
        
        cand = {
            "obj": obj,
            "family": candidate_family,
            "dims": sorted_dims,
            "ratios": ratios,
            "sig": sig,
            "resolved_name": base_name,
        }
        all_candidates.append(cand)
    
    # Scoring parameters
    SIZE_WEIGHT = 1.0
    RATIO_WEIGHT = 4.0       # aspect ratios are scale-invariant, very reliable
    POS_WEIGHT = 5.0          # position is the most trustworthy signal
    SIDE_MISMATCH_PENALTY = 50.0  # wrong-side-of-rig penalty
    
    MAX_ACCEPTABLE_REL_DIFF = 0.08  # relaxed — size is a soft signal now
    MAX_ACCEPTABLE_DIFF_VOL = 0.02
    MIN_SCALE = 0.1
    MAX_SCALE = 10.0
    
    PROHIBITIVE_COST = 1e6   # "impossible" assignment cost
    MAX_ACCEPTABLE_COST = 3.0  # reject matches above this (good matches are <1.0)
    
    n_targets = len(fp_targets)
    n_cands = len(all_candidates)
    
    if n_cands == 0:
        return 0
    
    # --- estimate rig scale BEFORE scoring ---
    # compare each target sig against ALL candidate sigs to find the
    # most common scale factor. this lets us scale position comparisons
    # correctly without chicken-and-egg problems.
    scale_votes = []
    for target in fp_targets:
        if target.get("is_vol", False) or target["sig"] <= 0:
            continue
        for cand in all_candidates:
            s = cand["sig"] / target["sig"]
            if MIN_SCALE <= s <= MAX_SCALE:
                # only vote if shape roughly matches (quick aspect ratio check)
                t_ratios = target["ratios"]
                c_ratios = cand["ratios"]
                if abs(t_ratios[0] - c_ratios[0]) + abs(t_ratios[1] - c_ratios[1]) < 0.5:
                    scale_votes.append(s)
    
    from statistics import median
    if scale_votes:
        estimated_rig_scale = median(scale_votes)
    else:
        estimated_rig_scale = 1.0
    
    # position distance threshold scales with rig scale
    MAX_POS_DIST = max(0.5, 0.5 * estimated_rig_scale)
    
    print(f"[RigImport] Pre-estimated rig scale: {estimated_rig_scale:.4f}, pos threshold: {MAX_POS_DIST:.3f}")
    
    # Build full cost matrix: targets (rows) x candidates (cols)
    cost_matrix = np.full((n_targets, n_cands), PROHIBITIVE_COST, dtype=np.float64)
    scale_matrix = np.ones((n_targets, n_cands), dtype=np.float64)
    
    for ti, target in enumerate(fp_targets):
        target_name = target["target"]
        target_lower = target_name.lower()
        target_family = target.get("family") or "body"
        target_dims = target["dims"]
        target_sig = target["sig"]
        target_ratios = target["ratios"]
        is_vol = target.get("is_vol", False)
        is_wrap_layer = bool(target.get("is_wrap_layer"))
        expected_locs = expected_loc_by_name.get(target_lower)
        
        for ci, cand in enumerate(all_candidates):
            obj = cand["obj"]
            mesh_center = mesh_centers[obj]
            name_confirmed_wrap = is_wrap_layer and cand.get("resolved_name") == target_lower

            if cand.get("family") != target_family:
                continue
            
            # --- size compatibility check ---
            if is_vol:
                if "vol" not in cand:
                    bm = bmesh.new()
                    try:
                        bm.from_mesh(obj.data)
                        cand["vol"] = abs(bm.calc_volume())
                    except Exception:
                        dd = cand["dims"]
                        cand["vol"] = dd[0] * dd[1] * dd[2]
                    finally:
                        bm.free()
                vol_diff = abs(target_dims[0] - cand["vol"])
                if vol_diff > MAX_ACCEPTABLE_DIFF_VOL:
                    continue
                size_norm = vol_diff / max(MAX_ACCEPTABLE_DIFF_VOL, 1e-9)
                scale = 1.0
                ratio_norm = 0.0  # no ratio info for volume-only
            else:
                if target_sig <= 0:
                    continue
                scale = cand["sig"] / target_sig
                if scale < MIN_SCALE or scale > MAX_SCALE:
                    continue
                scaled_target = [d * scale for d in target_dims]
                size_diff = sum(abs(a - b) for a, b in zip(scaled_target, cand["dims"]))
                size_diff = size_diff / max(cand["sig"], 1e-6)
                if size_diff > MAX_ACCEPTABLE_REL_DIFF:
                    continue
                size_norm = size_diff / max(MAX_ACCEPTABLE_REL_DIFF, 1e-9)
                
                # aspect ratio difference — scale-invariant, crucial for small parts
                cand_ratios = cand["ratios"]
                ratio_diff = abs(target_ratios[0] - cand_ratios[0]) + abs(target_ratios[1] - cand_ratios[1])
                ratio_norm = ratio_diff  # already 0-based, typically 0-2 range
            
            # --- position component ---
            # use global estimated_rig_scale for expected positions, NOT
            # per-candidate scale (which would distort world positions)
            pos_norm = 1.0  # neutral if no position data
            side_penalty = 0.0
            
            if expected_locs:
                # find the nearest expected position for this name
                best_dist = float('inf')
                best_scaled = None
                for eloc in expected_locs:
                    es = eloc * estimated_rig_scale
                    d = (mesh_center - es).length
                    if d < best_dist:
                        best_dist = d
                        best_scaled = es
                pos_dist = best_dist
                if name_confirmed_wrap:
                    pos_norm = 0.0
                else:
                    pos_norm = min(pos_dist / max(MAX_POS_DIST, 1e-6), 3.0)
                
                # side mismatch: penalize when mesh and expected position
                # disagree on which side of the rig they're on (x-sign).
                expected_x = best_scaled.x
                mesh_x = mesh_center.x
                tolerance = max(0.02, 0.05 * estimated_rig_scale)
                if (not name_confirmed_wrap
                        and abs(expected_x) >= tolerance
                        and abs(mesh_x) >= tolerance):
                    if (expected_x > 0) != (mesh_x > 0):
                        side_penalty = SIDE_MISMATCH_PENALTY
            
            score = (SIZE_WEIGHT * size_norm
                     + RATIO_WEIGHT * ratio_norm
                     + POS_WEIGHT * pos_norm
                     + side_penalty)
            
            cost_matrix[ti, ci] = score
            scale_matrix[ti, ci] = scale
    
    # --- global optimal assignment ---
    assignments = _hungarian_assign(cost_matrix, n_targets, n_cands)
    
    # fingerprint_object_map maps FINAL blender object name -> obj ref
    # We build it AFTER renames so that blender's auto-suffixes (.001 etc)
    # are captured correctly. This is critical for duplicate target names
    # (e.g. multiple parts all called "bonnie left hand").
    fingerprint_object_map = {}
    renamed_count = 0
    rejected_count = 0
    skipped_count = 0
    scale_samples = []
    
    # Collect all renames first, then apply in two passes to avoid
    # blender's auto-suffixing (.001) when a target name already exists.
    # Without this, renaming obj_A to "LeftHand" when "LeftHand" already
    # exists causes blender to silently rename the EXISTING "LeftHand"
    # to "LeftHand.001", corrupting downstream name-based matching.
    pending_fp_renames = []  # (obj, target_name)
    accepted_assignments = []  # (obj, target_name, pos_confirmed) — all accepted, incl. already-correct
    
    # Position-lock threshold: only FP-lock matches whose mesh center
    # is within this distance of the expected position. matches with
    # poor position agreement are still renamed but left unlocked so
    # pass 2 can override them via position matching.
    FP_LOCK_POS_THRESHOLD = MAX_POS_DIST * 1.5
    
    for ti, ci in assignments:
        cost = cost_matrix[ti, ci]
        if cost >= PROHIBITIVE_COST:
            continue  # no valid match for this target
        if cost > MAX_ACCEPTABLE_COST:
            target_name = fp_targets[ti]["target"]
            obj_name = all_candidates[ci]["obj"].name
            print(f"[RigImport]   rejected '{target_name}' -> '{obj_name}' (cost={cost:.3f} > {MAX_ACCEPTABLE_COST})")
            rejected_count += 1
            continue
        
        target = fp_targets[ti]
        cand = all_candidates[ci]
        obj = cand["obj"]
        target_name = target["target"]
        scale = scale_matrix[ti, ci]
        wrap_name_confirmed = bool(target.get("is_wrap_layer")) and cand.get("resolved_name") == target_name.lower()
        
        current_name = _strip_suffix(obj.name)
        if not target.get("is_vol", False) and scale > 0:
            scale_samples.append(scale)
        
        # check position agreement for lock decision
        expected_locs = expected_loc_by_name.get(target_name.lower())
        pos_dist = float('inf')
        if expected_locs:
            mc = mesh_centers[obj]
            for eloc in expected_locs:
                es = eloc * estimated_rig_scale
                d = (mc - es).length
                if d < pos_dist:
                    pos_dist = d
            pos_info = f"pos_dist={pos_dist:.3f}"
        else:
            pos_info = "no_pos_data"
        
        pos_confirmed = wrap_name_confirmed or pos_dist < FP_LOCK_POS_THRESHOLD
        lock_tag = "LOCK" if pos_confirmed else "TENTATIVE"
        
        if current_name == target_name:
            skipped_count += 1
            accepted_assignments.append((obj, target_name, pos_confirmed))
        else:
            print(f"[RigImport]   matched '{target_name}' -> '{obj.name}' (cost={cost:.3f}, scale={scale:.3f}, {pos_info}, {lock_tag})")
            pending_fp_renames.append((obj, target_name))
            accepted_assignments.append((obj, target_name, pos_confirmed))
            renamed_count += 1
    
    # Two-pass rename: temp names first, then final names.
    # Only rename position-confirmed matches. Tentative matches keep
    # their original OBJ names so pass 2 can match them by position.
    confirmed_objs = {id(obj) for obj, _, pc in accepted_assignments if pc}
    confirmed_renames = [(obj, tgt) for obj, tgt in pending_fp_renames
                         if id(obj) in confirmed_objs]
    
    for i, (obj, _) in enumerate(confirmed_renames):
        obj.name = f"__rbxfp_{i}__"
    for obj, target_name in confirmed_renames:
        obj.name = target_name
    
    # Build the fingerprint map AFTER renames. Only FP-lock matches
    # where position was confirmed — tentative matches stay unlocked
    # so pass 2 can reassign them if it finds a better position match.
    tentative_count = 0
    for obj, target_name, pos_confirmed in accepted_assignments:
        if pos_confirmed:
            fingerprint_object_map[obj.name] = obj
        else:
            tentative_count += 1
            print(f"[RigImport]   TENTATIVE (not locked): '{target_name}' -> '{obj.name}' — poor position agreement")
    if tentative_count:
        print(f"[RigImport] {tentative_count} matches left tentative (unlocked for pass 2 override)")
    
    # count targets that got no candidate at all (prohibitive cost)
    assigned_targets = {ti for ti, ci in assignments if cost_matrix[ti, ci] < PROHIBITIVE_COST}
    for ti, target in enumerate(fp_targets):
        if ti not in assigned_targets:
            print(f"[RigImport]   '{target['target']}' unmatched: no size-compatible candidate")
            rejected_count += 1
    
    if scale_samples:
        rig_scale = median(scale_samples)
        meta_loaded["_rig_scale"] = rig_scale
        print(f"[RigImport] Estimated rig scale: {rig_scale:.4f}")

    print(f"[RigImport] Fingerprinting: {renamed_count} renamed, {skipped_count} already correct, {rejected_count} rejected")

    # --- axis debug: compare expected vs actual positions ---
    print("[RigImport] === POSITION COMPARISON (pass 1) ===")
    for obj, target_name, pos_confirmed in accepted_assignments:
        mesh_c = mesh_centers.get(obj)
        if mesh_c is None:
            mesh_c = _get_mesh_world_center(obj)
        exp_list = expected_loc_by_name.get(target_name.lower())
        if exp_list:
            best_dist = float('inf')
            best_exp_s = None
            for eloc in exp_list:
                es = eloc * estimated_rig_scale
                d = (mesh_c - es).length
                if d < best_dist:
                    best_dist = d
                    best_exp_s = es
            print(f"[RigImport]   {target_name:30s}  mesh=({mesh_c.x:+8.3f}, {mesh_c.y:+8.3f}, {mesh_c.z:+8.3f})  "
                  f"expected=({best_exp_s.x:+8.3f}, {best_exp_s.y:+8.3f}, {best_exp_s.z:+8.3f})  dist={best_dist:.4f}")
        else:
            print(f"[RigImport]   {target_name:30s}  mesh=({mesh_c.x:+8.3f}, {mesh_c.y:+8.3f}, {mesh_c.z:+8.3f})  expected=N/A")
    print("[RigImport] === END POSITION COMPARISON ===")

    meta_loaded["_fingerprint_object_map"] = fingerprint_object_map
    return renamed_count + skipped_count



def _get_mesh_world_center(obj):
    """Get the geometric center of a mesh in world space (from actual vertices)."""
    if obj.type != "MESH" or not obj.data.vertices:
        return obj.matrix_world.to_translation()
    
    # Calculate bounding box center in local space
    verts = obj.data.vertices
    min_co = [float('inf')] * 3
    max_co = [float('-inf')] * 3
    
    for v in verts:
        for i in range(3):
            min_co[i] = min(min_co[i], v.co[i])
            max_co[i] = max(max_co[i], v.co[i])
    
    # Local center
    local_center = [(min_co[i] + max_co[i]) / 2.0 for i in range(3)]
    
    # Transform to world space
    from mathutils import Vector
    world_center = obj.matrix_world @ Vector(local_center)
    return world_center


# Grid cell size for spatial hashing (in blender units).
# 0.1 keeps buckets small enough that the 27-neighbor query stays fast,
# but large enough to absorb typical OBJ precision loss.
_GRID_CELL = 0.1


def _grid_key(loc):
    """Integer grid cell for a world-space location."""
    from math import floor
    return (
        floor(loc.x / _GRID_CELL),
        floor(loc.y / _GRID_CELL),
        floor(loc.z / _GRID_CELL),
    )


class _SpatialHash:
    """Simple 3D spatial hash for O(1)-amortized nearest-neighbor queries."""

    def __init__(self):
        self._buckets: dict[tuple, list] = {}

    def insert(self, obj, loc):
        key = _grid_key(loc)
        self._buckets.setdefault(key, []).append((obj, loc))

    def query_nearest(self, target_loc, exclude, max_distance=0.5):
        """Return (obj, dist) for the nearest non-excluded object, or (None, inf).
        
        Searches the 27 neighboring cells (3³) around the target, which
        guarantees finding anything within one cell width. If max_distance
        exceeds the cell size we also check an expanded shell.
        """
        cx, cy, cz = _grid_key(target_loc)
        # How many extra rings of cells to check beyond the immediate 27
        extra = max(0, int(max_distance / _GRID_CELL))
        r = 1 + extra

        best_obj = None
        best_dist = max_distance

        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    bucket = self._buckets.get((cx + dx, cy + dy, cz + dz))
                    if not bucket:
                        continue
                    for obj, loc in bucket:
                        if obj in exclude:
                            continue
                        dist = (loc - target_loc).length
                        if dist < best_dist:
                            best_dist = dist
                            best_obj = obj

        return best_obj, best_dist


def _rename_parts_by_fingerprint(rig_def, parts_collection, renamed_via_fingerprint=0, fingerprint_object_map=None, scale_factor=1.0, meta_loaded=None):
    """Rename meshes using transform position matching from rig metadata.
    
    Uses name matching first, then spatial-hash position lookup.
    Size data from partAux (when available) gates position matches so tiny
    meshes aren't grabbed by distant bones.
    """
    if not rig_def:
        print("[RigImport] No rig definition provided")
        return False

    allow_aux_renames = not bool(meta_loaded and meta_loaded.get("partAux"))
    if not allow_aux_renames:
        print("[RigImport] partAux present - skipping aux-name rename targets")

    t2b = get_transform_to_blender()
    used = set()
    
    # Build a set of all bone/part names in the rig definition (case-insensitive)
    all_rig_names = set()
    def collect_names(node):
        if not node:
            return
        jname = node.get("jname") or node.get("pname") or ""
        if jname:
            all_rig_names.add(jname.lower())
        if allow_aux_renames:
            for aux_name in (node.get("aux") or []):
                if aux_name:
                    all_rig_names.add(aux_name.lower())
        for child in (node.get("children") or []):
            collect_names(child)
    collect_names(rig_def)
    print(f"[RigImport] Rig contains {len(all_rig_names)} named parts")
    
    mesh_objects = [obj for obj in parts_collection.objects if obj.type == "MESH"]
    print(f"[RigImport] Building spatial index for {len(mesh_objects)} mesh objects")
    
    # Parts already matched by size fingerprinting are authoritative —
    # don't let this pass reassign them.
    fp_matched_objs = set()
    fp_matched_names = set()  # base bone names (lowered) that are fully covered by fp
    if fingerprint_object_map:
        for obj_name, obj in fingerprint_object_map.items():
            fp_matched_objs.add(obj)
            # The object was renamed to the target bone name (possibly with .001 suffix)
            # so strip suffix to get the base bone name
            fp_matched_names.add(_strip_suffix(obj_name).lower())
        print(f"[RigImport] {len(fp_matched_objs)} parts locked from fingerprint pass")
    
    # Build name index for direct name matching (case-insensitive)
    known_target_names = set(all_rig_names)
    part_aux_raw = meta_loaded.get("partAux") if meta_loaded else None
    if part_aux_raw:
        pa_list = list(part_aux_raw.values()) if isinstance(part_aux_raw, dict) else part_aux_raw
        for item in (pa_list or []):
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    known_target_names.add(str(name).lower())

    name_index = {}
    for obj in mesh_objects:
        base_name = _resolve_imported_obj_name(obj.name, known_target_names)
        name_index.setdefault(base_name, []).append(obj)
    
    # Precompute geometric centers and build spatial hash
    mesh_centers = {}
    spatial = _SpatialHash()
    for obj in mesh_objects:
        center = _get_mesh_world_center(obj)
        mesh_centers[obj] = center
        spatial.insert(obj, center)
    
    # Build expected-size map from partAux so position matching can gate
    # on size compatibility — prevents tiny meshes from being grabbed by
    # distant or wrong-sized bones.
    expected_dims_by_name = {}  # target_name_lower -> sorted dims tuple
    synth_preferred_targets = set()
    if meta_loaded:
        part_aux_raw = meta_loaded.get("partAux")
        if part_aux_raw:
            pa_list = list(part_aux_raw.values()) if isinstance(part_aux_raw, dict) else part_aux_raw
            for item in (pa_list or []):
                if not item or not isinstance(item, dict):
                    continue
                name = item.get("name", "")
                dims = item.get("dims_fp")
                if name and dims and len(dims) == 3:
                    sd = tuple(sorted([float(x) for x in dims]))
                    expected_dims_by_name[name.lower()] = sd
                mesh_class = item.get("mesh_class")
                if (
                    name
                    and item.get("mesh_id")
                    and mesh_class in (None, "", "MeshPart")
                    and item.get("wrap_target")
                ):
                    synth_preferred_targets.add(name.lower())
    if expected_dims_by_name:
        print(f"[RigImport] Loaded expected sizes for {len(expected_dims_by_name)} parts")

    # Precompute mesh sizes for size gating
    mesh_dims = {}  # obj -> sorted dims tuple
    for obj in mesh_objects:
        d = obj.dimensions
        mesh_dims[obj] = tuple(sorted([d.x, d.y, d.z]))

    # Precompute reserved-name exclusion: for each target name, which objects
    # are "reserved" (already named for a DIFFERENT rig bone)?
    # This replaces the per-query O(n) scan with an O(1) lookup.
    _obj_rig_name = {}  # obj -> lowered rig name it matches (if any)
    for obj in mesh_objects:
        base = _resolve_imported_obj_name(obj.name, known_target_names)
        if base in all_rig_names:
            _obj_rig_name[obj] = base

    reserved_by_name: dict[str, set] = {}  # target_lower -> set of excluded objs
    for target_lower in all_rig_names:
        excluded = set()
        for obj, obj_name in _obj_rig_name.items():
            if obj_name != target_lower:
                excluded.add(obj)
        reserved_by_name[target_lower] = excluded
    
    def match_by_name(target_name):
        candidates = name_index.get(target_name.lower(), [])
        available = [o for o in candidates if o not in used]
        return available[0] if available else None
    
    def match_by_position(cf, target_name, allow_reserved_override=False, max_distance_override=None):
        """Match by spatial-hash nearest-neighbor lookup with size-aware tolerance.
        
        Tolerance scales with the expected part size — tiny parts need to be
        very close to their expected position, large parts get more slack.
        Also rejects matches where mesh dims diverge wildly from expected.
        """
        if not cf:
            return None
        
        try:
            expected_loc = (t2b @ cf_to_mat(cf)).to_translation()
            if scale_factor and scale_factor != 1.0:
                expected_loc = expected_loc * scale_factor
        except Exception as e:
            print(f"[RigImport]   '{target_name}' Failed to convert CFrame: {e}")
            return None
        
        # Build exclude set: already-used + fingerprint-locked + reserved names
        exclude = set(used) | fp_matched_objs
        if not allow_reserved_override:
            exclude.update(reserved_by_name.get(target_name.lower(), frozenset()))
        
        # Adaptive tolerance: scale by expected part size.
        # A part 2 studs across can be 0.5 units away; a part 0.01 studs across
        # should be within ~0.05 units.
        target_lower = target_name.lower()
        exp_dims = expected_dims_by_name.get(target_lower)
        if exp_dims:
            exp_size = max(exp_dims) * (scale_factor if scale_factor else 1.0)
            # tolerance = clamp(exp_size * 0.5, 0.05, 0.5) scaled by rig scale
            pos_tolerance = max(0.05, min(0.5, exp_size * 0.5)) * max(1.0, scale_factor if scale_factor else 1.0)
        else:
            pos_tolerance = max(0.5, 0.5 * scale_factor) if scale_factor else 0.5
        
        query_max_distance = max_distance_override if max_distance_override is not None else pos_tolerance
        best, dist = spatial.query_nearest(expected_loc, exclude, max_distance=query_max_distance)
        if best:
            # Size gate: reject if mesh dims are wildly incompatible with expected
            if exp_dims:
                m_dims = mesh_dims.get(best)
                if m_dims:
                    exp_sig = sum(exp_dims) * (scale_factor if scale_factor else 1.0)
                    mesh_sig = sum(m_dims)
                    if exp_sig > 1e-6 and mesh_sig > 1e-6:
                        ratio = mesh_sig / exp_sig
                        if ratio < 0.2 or ratio > 5.0:
                            print(f"[RigImport]   '{target_name}' REJECTED '{best.name}' (size ratio={ratio:.2f}, dist={dist:.4f})")
                            return None
            
            print(f"[RigImport]   '{target_name}' MATCHED (dist={dist:.4f}, tol={query_max_distance:.3f}) -> '{best.name}'")
            return best

        print(f"[RigImport]   '{target_name}' NO POSITION MATCH at ({expected_loc.x:.4f}, {expected_loc.y:.4f}, {expected_loc.z:.4f}) tol={query_max_distance:.3f}")
        return None

    def match_wrap_target_by_strong_position(cf, target_name):
        target_lower = target_name.lower()
        exp_dims = expected_dims_by_name.get(target_lower)
        if exp_dims:
            exp_size = max(exp_dims) * (scale_factor if scale_factor else 1.0)
            strong_tolerance = max(0.03, min(0.16, exp_size * 0.12))
        else:
            strong_tolerance = 0.08
        return match_by_position(
            cf,
            target_name,
            allow_reserved_override=False,
            max_distance_override=strong_tolerance,
        )

    # Pre-mark fingerprint-matched objects as used so they don't get stolen
    for tname, obj in (fingerprint_object_map or {}).items():
        used.add(obj)
    
    matched_count = 0
    locked_match_count = 0
    unmatched_names = []
    pending_renames = []  # List of (obj, target_name)
    matched_objects = []  # List of (obj, target_name)
    
    # Collect all nodes that need matching (excluding root)
    nodes_to_match = []  # List of (jname, transform, is_aux)
    
    def collect_nodes(node, depth=0):
        """First pass: collect all bone/part names and their transforms."""
        jname = node.get("jname") or node.get("pname") or ""
        children = node.get("children") or []
        node_transform = node.get("transform")
        aux_transforms = node.get("auxTransform") or []
        aux_names = node.get("aux") or []
        
        is_root = (depth == 0)
        
        if jname and not is_root:
            nodes_to_match.append((jname, node_transform, False))
        
        if allow_aux_renames and not is_root:
            for idx, aux_name in enumerate(aux_names):
                if aux_name:
                    cf = aux_transforms[idx] if idx < len(aux_transforms) else None
                    nodes_to_match.append((aux_name, cf, True))
        
        for child in children:
            collect_nodes(child, depth + 1)
    
    collect_nodes(rig_def)
    print(f"[RigImport] Collected {len(nodes_to_match)} nodes to match")

    if synth_preferred_targets:
        print(f"[RigImport] Metadata-only hidden wrap targets detected for {len(synth_preferred_targets)} body parts")
    
    # Check if meshes already have names matching the rig bones
    # If so, use name-based matching. If not, use position-based matching.
    meshes_with_rig_names = 0
    # for obj in mesh_objects:
    #     base_name = _strip_suffix(obj.name).lower()
    #     if base_name in all_rig_names:
    #         meshes_with_rig_names += 1
    
    # Force use of rename map if fingerprints were used
    # If we successfully renamed parts via fingerprints, we should trust those names
    if renamed_via_fingerprint > 0:
        use_name_matching = True
        print("[RigImport] Fingerprinting successful - running NAME matching on corrected parts")
    else:
        for obj in mesh_objects:
            base_name = _resolve_imported_obj_name(obj.name, known_target_names)
            if base_name in all_rig_names:
                meshes_with_rig_names += 1
        use_name_matching = meshes_with_rig_names > 0
        print(f"[RigImport] Found {meshes_with_rig_names} meshes with rig bone names - using {'NAME' if use_name_matching else 'POSITION'} matching")
    
    for target_name, transform, is_aux in nodes_to_match:
        # Skip parts already locked by fingerprint pass
        target_lower = target_name.lower()
        if target_lower in fp_matched_names:
            locked_match_count += 1
            continue
        
        obj = None
        prefix = "AUX " if is_aux else ""
        is_hidden_wrap_target = target_lower in synth_preferred_targets and not is_aux
        
        if use_name_matching:
            # Use name matching
            obj = match_by_name(target_name)
            if obj:
                print(f"[RigImport] {prefix}'{target_name}' matched by NAME -> '{obj.name}'")
            elif transform and not is_hidden_wrap_target:
                obj = match_by_position(transform, target_name, allow_reserved_override=True)
                if obj:
                    print(f"[RigImport] {prefix}'{target_name}' matched by POSITION -> '{obj.name}'")
            elif transform and is_hidden_wrap_target:
                # Two-tier: try tight tolerance first, then normal tolerance.
                # Truly absent parts (no nearby mesh) fail both tiers.
                obj = match_wrap_target_by_strong_position(transform, target_name)
                if obj:
                    print(f"[RigImport] {prefix}'{target_name}' matched by STRONG POSITION -> '{obj.name}'")
                else:
                    obj = match_by_position(transform, target_name, allow_reserved_override=True)
                    if obj:
                        print(f"[RigImport] {prefix}'{target_name}' matched by POSITION (wrap target fallback) -> '{obj.name}'")
        else:
            # Use position matching
            if transform and not is_hidden_wrap_target:
                obj = match_by_position(transform, target_name)
                if obj:
                    print(f"[RigImport] {prefix}'{target_name}' matched by POSITION -> '{obj.name}'")
            elif transform and is_hidden_wrap_target:
                obj = match_wrap_target_by_strong_position(transform, target_name)
                if obj:
                    print(f"[RigImport] {prefix}'{target_name}' matched by STRONG POSITION -> '{obj.name}'")
                else:
                    obj = match_by_position(transform, target_name)
                    if obj:
                        print(f"[RigImport] {prefix}'{target_name}' matched by POSITION (wrap target fallback) -> '{obj.name}'")

        if obj is None and transform and is_hidden_wrap_target:
            print(f"[RigImport] {prefix}'{target_name}' no mesh nearby -> keep hidden wrap target absent")
        
        if obj:
            current_base = _strip_suffix(obj.name)
            if current_base != target_name:
                pending_renames.append((obj, target_name))
                matched_count += 1
                matched_objects.append((obj, target_name))
            else:
                matched_objects.append((obj, current_base))
            used.add(obj)
        else:
            unmatched_names.append(target_name)
    
    # Two-pass rename to avoid name collisions (e.g., Handle2->Handle1 when Handle1 exists)
    # Pass 1: Rename all to temporary unique names
    print(f"[RigImport] Applying {len(pending_renames)} renames (two-pass to avoid collisions)")
    temp_names = []
    for i, (obj, _) in enumerate(pending_renames):
        temp_name = f"__rbxtemp_{i}__"
        temp_names.append((obj, temp_name))
        obj.name = temp_name
    
    # Pass 2: Rename to final target names
    for i, (obj, target_name) in enumerate(pending_renames):
        print(f"[RigImport]   RENAME: '{temp_names[i][1]}' -> '{target_name}'")
        obj.name = target_name
    
    print("[RigImport] " + "="*50)
    print(f"[RigImport] SUMMARY: {matched_count} parts renamed, {locked_match_count} prelocked, {len(unmatched_names)} unmatched")
    if unmatched_names:
        print(f"[RigImport] Unmatched parts: {unmatched_names}")

    # --- axis debug: compare expected vs actual positions (pass 2) ---
    print("[RigImport] === POSITION COMPARISON (pass 2) ===")
    for target_name, transform, is_aux in nodes_to_match:
        if not transform:
            continue
        try:
            exp = (t2b @ cf_to_mat(transform)).to_translation()
            if scale_factor and scale_factor != 1.0:
                exp = exp * scale_factor
        except Exception:
            continue
        # find the mesh currently named target_name (or target_name.NNN)
        mesh_obj = None
        tl = target_name.lower()
        for obj in mesh_objects:
            if _strip_suffix(obj.name).lower() == tl:
                mesh_obj = obj
                break
        if mesh_obj:
            mc = _get_mesh_world_center(mesh_obj)
            dist = (mc - exp).length
            tag = "FP-LOCKED" if tl in fp_matched_names else "pass2"
            print(f"[RigImport]   [{tag}] {target_name:30s}  mesh=({mc.x:+8.3f}, {mc.y:+8.3f}, {mc.z:+8.3f})  "
                  f"expected=({exp.x:+8.3f}, {exp.y:+8.3f}, {exp.z:+8.3f})  dist={dist:.4f}")
        else:
            print(f"[RigImport]   [MISSING] {target_name:30s}  expected=({exp.x:+8.3f}, {exp.y:+8.3f}, {exp.z:+8.3f})")
    print("[RigImport] === END POSITION COMPARISON ===")

    print("[RigImport] " + "="*50)

    if meta_loaded is not None:
        updated_fp_map = dict(fingerprint_object_map or {})
        for obj, _target_name in matched_objects:
            stale_keys = [key for key, mapped_obj in updated_fp_map.items() if mapped_obj is obj and key != obj.name]
            for stale_key in stale_keys:
                updated_fp_map.pop(stale_key, None)
            updated_fp_map[obj.name] = obj
        meta_loaded["_fingerprint_object_map"] = updated_fp_map

    return bool(matched_objects or locked_match_count)


def _parts_list_from_rig_def(rig_def):
    """Derive a parts list from rig metadata in depth-first traversal order.
    
    Must match the order that lua's GetDescendants() produces, which is
    depth-first. Alphabetical sorting would mismatch the p<N>x indices.
    """
    if not rig_def:
        return []
    parts = []
    seen = set()

    def walk(node):
        if not node:
            return
        local_pname = node.get("pname") or node.get("jname")
        if local_pname and local_pname not in seen:
            parts.append(local_pname)
            seen.add(local_pname)

        aux = node.get("aux") or []
        for aux_name in aux:
            if aux_name and aux_name not in seen:
                parts.append(aux_name)
                seen.add(aux_name)

        for child in node.get("children") or []:
            walk(child)

    walk(rig_def)

    return parts


def _rename_indexed_parts(meta_loaded, parts_collection):
    """Rename OBJ-exported meshes from indexed placeholders to real part names.
    
    Tries two naming schemes:
    1. New unambiguous: 'p<N>x' (+ optional OBJ group suffix '1' + optional dedup '.001')
    2. Legacy: '<rigName><N>' (+ optional OBJ suffix '1' + optional dedup '.001')
    """
    parts_list = None
    
    # Best source: partAux has authoritative idx→name mapping from the export.
    # This is always correct regardless of how 'parts' is serialized.
    part_aux_raw = meta_loaded.get("partAux")
    if part_aux_raw:
        if isinstance(part_aux_raw, dict):
            aux_items = list(part_aux_raw.values())
        else:
            aux_items = list(part_aux_raw)
        # sort by idx to get correct export order
        aux_with_idx = []
        for item in aux_items:
            if isinstance(item, dict) and "idx" in item and "name" in item:
                aux_with_idx.append((int(item["idx"]), item["name"]))
        if aux_with_idx:
            aux_with_idx.sort(key=lambda t: t[0])
            parts_list = [name for _, name in aux_with_idx]
    
    # Fallback: try the 'parts' payload directly
    if not parts_list:
        parts_payload = meta_loaded.get("parts")
        if isinstance(parts_payload, list):
            parts_list = parts_payload
        elif isinstance(parts_payload, dict):
            first_key = next(iter(parts_payload), "")
            if first_key.isdigit():
                parts_list = [parts_payload[k] for k in sorted(parts_payload.keys(), key=lambda k: int(k))]
    
    # Last resort: derive from rig tree (may not match GetDescendants order)
    if not parts_list:
        parts_list = _parts_list_from_rig_def(meta_loaded.get("rig"))

    if not parts_list:
        return False, "Missing 'parts' in rig metadata"

    print(f"[RigImport] parts_list ({len(parts_list)} entries):")
    for i, name in enumerate(parts_list):
        print(f"[RigImport]   [{i+1}] = {name!r}")

    # New unambiguous pattern: p<N>x (with optional OBJ group suffix "1" and dedup suffix)
    # This is the ONLY pattern we trust for indexed rename, because both the
    # naming (p<N>x) and the partAux idx are assigned by the same partCount
    # in the same GetDescendants loop on the lua side.
    #
    # Legacy <rigName><N> patterns are NOT used — Roblox's OBJ exporter
    # assigns its own index order which doesn't match GetDescendants order,
    # so the mapping would be wrong. Those rigs fall through to fingerprint
    # matching instead.
    new_pattern = re.compile(r"^p(\d+)x1?(\.\d+)?$", re.IGNORECASE)
    new_indexed = [
        obj for obj in parts_collection.objects
        if obj.type == "MESH" and new_pattern.match(obj.name)
    ]
    if new_indexed:
        _autoname_from_pattern(parts_list, new_pattern, new_indexed)
        return True, None

    return False, None


def _parts_already_named(parts_list, parts_collection):
    if not isinstance(parts_list, list) or not parts_list:
        return False

    mesh_names = {obj.name for obj in parts_collection.objects if obj.type == "MESH"}
    expected_names = {name for name in parts_list if isinstance(name, str) and name}
    if not expected_names:
        return False

    return expected_names.issubset(mesh_names)


def _autoname_from_pattern(partnames, pattern, objects_to_rename):
    """Rename objects whose names match `pattern` (group 1 = index) to partnames[index-1].
    
    Uses two-pass temp-name approach to avoid blender's auto-suffixing
    when a target name already exists as another object's name.
    """
    
    pending = []
    print(f"[RigImport] _autoname mapping ({len(objects_to_rename)} meshes):")
    for obj in objects_to_rename:
        match = pattern.match(obj.name)
        if match:
            try:
                index = int(match.group(1))
                if 0 < index <= len(partnames):
                    target = partnames[index - 1]
                    print(f"[RigImport]   '{obj.name}' → idx={index} → '{target}'")
                    pending.append((obj, target))
                else:
                    print(
                        f"Warning: Index {index} out of range for partnames list (length: {len(partnames)})"
                    )
            except Exception as e:
                print(f"Error renaming part {obj.name}: {str(e)}")
    
    # Pass 1: temp names to clear the namespace
    for i, (obj, _) in enumerate(pending):
        obj.name = f"__rbxidx_{i}__"
    # Pass 2: final names
    for obj, target_name in pending:
        obj.name = target_name
    
    # --- axis debug: compare expected vs actual positions (indexed import) ---
    print("[RigImport] === POSITION COMPARISON (indexed import) ===")
    for obj, target_name in pending:
        mesh_c = _get_mesh_world_center(obj)
        print(f"[RigImport]   {target_name:30s}  mesh=({mesh_c.x:+8.3f}, {mesh_c.y:+8.3f}, {mesh_c.z:+8.3f})")
    print("[RigImport] === END POSITION COMPARISON ===")


# ---------------------------------------------------------------------------
# weapon import confirmation popup
# ---------------------------------------------------------------------------

# stash dict for passing data between ImportModel and the confirm dialog
_pending_weapon_import = {}


class _Reporter:
    """Lightweight proxy so _import_weapon can call self.report()
    without holding a reference to the (now-dead) ImportModel instance."""
    def __init__(self, report_fn):
        self.report = report_fn


def _weapon_target_rig_items(self, context):
    """Enum items callback listing all armatures in the scene.
    The currently-selected armature (from settings) is first."""
    from ..core.utils import get_cached_armatures
    items = []
    settings = getattr(context.scene, "rbx_anim_settings", None)
    current = settings.rbx_anim_armature if settings else ""
    seen = set()
    # put current rig first so it's the default
    if current:
        items.append((current, current, "Currently active rig"))
        seen.add(current)
    for name in get_cached_armatures():
        if name not in seen:
            items.append((name, name, ""))
            seen.add(name)
    if not items:
        items.append(("NONE", "(no armatures)", ""))
    return items


class OBJECT_OT_ConfirmWeaponTarget(bpy.types.Operator):
    bl_idname = "object.rbxanims_confirm_weapon_target"
    bl_label = "Import Weapon"
    bl_description = "Confirm target rig for weapon import"
    bl_options = {"REGISTER", "INTERNAL"}

    target_rig: bpy.props.EnumProperty(
        name="Target Rig",
        description="Armature to attach the weapon to",
        items=_weapon_target_rig_items,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=350)

    @staticmethod
    def _find_source_armature(armature):
        """Detect if this armature is a proxy/control rig by scanning for
        Copy Transform/Location/Rotation constraints pointing to another armature.
        Returns (source_armature, constraint_map) or (None, {}).
        constraint_map: {bone_name: (target_armature, subtarget_bone)} for bones
        that have copy constraints."""
        COPY_TYPES = {"COPY_TRANSFORMS", "COPY_LOCATION", "COPY_ROTATION"}
        target_counts = {}  # armature_obj -> count
        constraint_map = {}

        for pb in armature.pose.bones:
            for c in pb.constraints:
                if c.type in COPY_TYPES and c.target and c.target.type == "ARMATURE" and c.target != armature:
                    target_counts[c.target] = target_counts.get(c.target, 0) + 1
                    constraint_map[pb.name] = (c.target, c.subtarget or pb.name)
                    break  # one copy constraint per bone is enough

        if not target_counts:
            return None, {}

        # the source is whichever armature has the most copy constraints pointing at it
        source = max(target_counts, key=target_counts.get)
        # filter map to only constraints pointing at the winner
        filtered = {k: v for k, v in constraint_map.items() if v[0] == source}
        return source, filtered

    def _get_suggested_bones(self):
        data = _pending_weapon_import.get("data")
        if not data:
            return []
        return _collect_weapon_suggested_bones(data["meta_loaded"])

    def _find_bone_case_insensitive(self, armature, suggested):
        return _find_bone_case_insensitive(armature, suggested)

    def _check_bone_matches(self, context):
        """Return (armature, matches) where matches is:
        [{"suggested": str, "found": str|None, "source_arm": Object|None}]"""
        suggested_bones = self._get_suggested_bones()
        armature = None
        if self.target_rig and self.target_rig != "NONE":
            armature = get_object_by_name(self.target_rig, context.scene)
        if not armature or armature.type != "ARMATURE":
            return armature, [{"suggested": s, "found": None, "source_arm": None} for s in suggested_bones]

        source_arm, _ = self._find_source_armature(armature)
        matches = []
        for suggested in suggested_bones:
            found = self._find_bone_case_insensitive(armature, suggested)
            if found:
                matches.append({"suggested": suggested, "found": found, "source_arm": None})
                continue
            source_found = self._find_bone_case_insensitive(source_arm, suggested) if source_arm else None
            if source_found:
                matches.append({"suggested": suggested, "found": source_found, "source_arm": source_arm})
            else:
                matches.append({"suggested": suggested, "found": None, "source_arm": None})
        return armature, matches

    def draw(self, context):
        layout = self.layout
        weapon_name = _pending_weapon_import.get("weapon_name", "Weapon")
        layout.label(text=f"Importing: {weapon_name}", icon="OBJECT_DATA")
        layout.separator()
        layout.prop(self, "target_rig", icon="ARMATURE_DATA")

        armature, matches = self._check_bone_matches(context)
        if matches:
            any_source = any(m["source_arm"] for m in matches)
            if any_source:
                src = next((m["source_arm"] for m in matches if m["source_arm"]), None)
                box = layout.box()
                col = box.column(align=True)
                col.label(text="Proxy rig detected", icon="INFO")
                if src:
                    col.label(text=f"Source rig: {src.name}")
                col.label(text="Weapon bones will be created on the source rig")
                col.label(text="with copy constraints mirrored to the proxy")

            box = layout.box()
            col = box.column(align=True)
            col.label(text=f"Suggested attachment bones ({len(matches)}):", icon="BONE_DATA")
            for m in matches:
                suggested = m["suggested"]
                found = m["found"]
                source_arm = m["source_arm"]
                if found and not source_arm:
                    col.label(text=f"{suggested} -> {found}", icon="CHECKMARK")
                elif found and source_arm:
                    col.label(text=f"{suggested} -> {found} (source rig)", icon="INFO")
                else:
                    row = col.row()
                    row.alert = True
                    row.label(text=f"{suggested} (missing)", icon="ERROR")

            missing = [m for m in matches if not m["found"]]
            if missing and armature:
                col.separator()
                warn = col.row()
                warn.alert = True
                warn.label(text="Some suggested bones were not found on this rig", icon="ERROR")
                bone_names = [b.name for b in armature.data.bones]
                if bone_names:
                    preview = bone_names[:8]
                    col.alert = False
                    col.label(text=f"Available bones ({len(bone_names)}):", icon="BONE_DATA")
                    for bn in preview:
                        col.label(text=f"  - {bn}")
                    if len(bone_names) > 8:
                        col.label(text=f"  ... and {len(bone_names) - 8} more")

    def execute(self, context):
        armature, matches = self._check_bone_matches(context)
        if armature and matches:
            missing = [m["suggested"] for m in matches if not m["found"]]
            if missing:
                self.report(
                    {"ERROR"},
                    f"Missing suggested bone(s) on \"{self.target_rig}\": {', '.join(missing)}. Pick a different rig."
                )
                return {"CANCELLED"}

        return bpy.ops.object.rbxanims_apply_weapon_import(target_rig=self.target_rig)

class OBJECT_OT_ApplyWeaponImport(bpy.types.Operator):
    bl_idname = "object.rbxanims_apply_weapon_import"
    bl_label = "Apply Weapon Import"
    bl_description = "Apply the selected target rig for weapon import"
    bl_options = {"REGISTER", "INTERNAL", "UNDO"}

    target_rig: bpy.props.StringProperty(name="Target Rig", default="NONE")

    def execute(self, context):
        # Undo safety: normalize to object mode before any ID/collection edits.
        try:
            from ..rig.creation import _safe_mode_set
            _safe_mode_set("OBJECT")
        except Exception:
            pass

        data = _pending_weapon_import.pop("data", None)
        if not data:
            self.report({"ERROR"}, "No pending weapon import data")
            return {"CANCELLED"}

        meta_loaded = data["meta_loaded"]
        # re-fetch objects by name — the live refs stored earlier are
        # potentially stale bc they crossed an operator / undo boundary.
        rig_part_obj_names = data.get("rig_part_obj_names", [])
        rig_part_objs = [
            bpy.data.objects[n] for n in rig_part_obj_names
            if n in bpy.data.objects
        ]

        # detect proxy rig — if so, import onto the source armature
        actual_rig_name = self.target_rig
        proxy_armature = None
        if actual_rig_name and actual_rig_name != "NONE":
            selected_arm = get_object_by_name(actual_rig_name, context.scene)
            if selected_arm and selected_arm.type == "ARMATURE":
                source_arm, constraint_map = OBJECT_OT_ConfirmWeaponTarget._find_source_armature(selected_arm)
                if source_arm and _should_redirect_weapon_import(selected_arm, source_arm, meta_loaded):
                    print(
                        f"[WeaponImport] Proxy rig redirect: {selected_arm.name} -> {source_arm.name} "
                        f"({len(constraint_map)} constrained bones)"
                    )
                    proxy_armature = selected_arm
                    actual_rig_name = source_arm.name
                elif source_arm:
                    print(
                        f"[WeaponImport] Keeping selected rig '{selected_arm.name}' "
                        "for import (suggested bones resolved on selected rig)"
                    )

        # override the armature setting so _import_weapon picks it up
        settings = getattr(context.scene, "rbx_anim_settings", None)
        old_arm = settings.rbx_anim_armature if settings else None
        if settings and actual_rig_name != "NONE":
            settings.rbx_anim_armature = actual_rig_name

        # use a lightweight proxy so _import_weapon can call self.report()
        # (the original ImportModel instance is already dead)
        proxy = _Reporter(self.report)

        # bl_options already includes 'UNDO', so blender handles the undo
        # step automatically.  do NOT call undo_push manually — doubling up
        # corrupts the undo stack and causes a build_materials crash on
        # ctrl+z (null material pointer after partial undo restore).

        try:
            # call as unbound method — proxy duck-types as `self`
            result = OBJECT_OT_ImportModel._import_weapon(proxy, context, meta_loaded, rig_part_objs)

            # if proxy rig detected, clone weapon bones onto the proxy
            # with copy constraints mirroring the source
            if result == {"FINISHED"} and proxy_armature:
                source_arm_obj = get_object_by_name(actual_rig_name, context.scene)
                if source_arm_obj:
                    OBJECT_OT_ApplyWeaponImport._clone_weapon_bones_to_proxy(
                        context, source_arm_obj, proxy_armature, meta_loaded
                    )
        finally:
            # restore original setting
            if settings and old_arm is not None:
                settings.rbx_anim_armature = old_arm

        return result

    @staticmethod
    def _clone_weapon_bones_to_proxy(context, source_armature, proxy_armature, meta_loaded):
        """Create matching weapon bones on the proxy armature with COPY_TRANSFORMS
        constraints pointing back to the source armature's weapon bones."""
        from ..rig.creation import _safe_mode_set

        def _ensure_object_in_view_layer(ctx, obj):
            """Ensure object is available in current window view layer.

            Returns True if available (or switched to a view layer that has it),
            else False.
            """
            if not obj:
                return False
            try:
                if ctx.view_layer.objects.get(obj.name) == obj:
                    return True
            except Exception:
                pass

            scene = getattr(ctx, "scene", None)
            win = getattr(ctx, "window", None)
            if scene is None:
                return False

            for vl in scene.view_layers:
                try:
                    if vl.objects.get(obj.name) == obj:
                        if win is not None:
                            try:
                                win.view_layer = vl
                            except Exception:
                                pass
                        return True
                except Exception:
                    continue
            return False

        # find weapon bones on source (they were just created by _import_weapon)
        # weapon bones are parented under the suggested bone
        suggested = meta_loaded.get("suggestedBone", "")
        suggested_bones = set()
        if suggested:
            suggested_bones.add(suggested.lower())
        attachments = meta_loaded.get("weaponAttachments")
        if isinstance(attachments, list):
            for att in attachments:
                if isinstance(att, dict):
                    sb = att.get("suggestedBone")
                    if isinstance(sb, str) and sb:
                        suggested_bones.add(sb.lower())
        weapon_bones = []
        for bone in source_armature.data.bones:
            # weapon bones are typically named after the weapon parts
            # and are children (direct or indirect) of the suggested bone
            parent = bone.parent
            while parent:
                if parent.name.lower() in suggested_bones:
                    weapon_bones.append(bone.name)
                    break
                parent = parent.parent

        if not weapon_bones:
            print(f"[WeaponImport] No weapon bones found under suggested roots {sorted(suggested_bones)} to clone to proxy")
            return

        print(f"[WeaponImport] Cloning {len(weapon_bones)} weapon bones to proxy '{proxy_armature.name}': {weapon_bones}")

        if not _ensure_object_in_view_layer(context, source_armature):
            print(
                f"[WeaponImport] Cannot clone weapon bones: source armature "
                f"'{source_armature.name}' is not in any accessible view layer."
            )
            return
        if not _ensure_object_in_view_layer(context, proxy_armature):
            print(
                f"[WeaponImport] Skipping proxy clone: armature "
                f"'{proxy_armature.name}' is not in any accessible view layer."
            )
            return

        # collect bone data from SOURCE in edit mode (only way to get real roll)
        context.view_layer.objects.active = source_armature
        source_armature.select_set(True)
        with _ensure_all_bone_collections_visible(source_armature):
            _safe_mode_set("EDIT", source_armature)

            bone_data = {}  # name -> {head, tail, roll, parent_name}
            for bone_name in weapon_bones:
                eb = source_armature.data.edit_bones.get(bone_name)
                if eb:
                    bone_data[bone_name] = {
                        "head": eb.head.copy(),
                        "tail": eb.tail.copy(),
                        "roll": eb.roll,
                        "parent": eb.parent.name if eb.parent else None,
                    }

            _safe_mode_set("OBJECT", source_armature)

        # collect custom properties from source bones (object mode)
        # these are critical for serialization (transform, transform0/1, nicetransform, etc.)
        def _deep_convert_idprop(val):
            """recursively convert IDPropertyArray/IDPropertyGroup to plain python types
            so blender can re-create them as new IDProperties without crashing."""
            if hasattr(val, "to_dict"):
                # IDPropertyGroup → dict with recursively converted values
                return {k: _deep_convert_idprop(v) for k, v in val.items()}
            if hasattr(val, "to_list"):
                # IDPropertyArray → list with recursively converted elements
                return [_deep_convert_idprop(x) for x in val.to_list()]
            if isinstance(val, (list, tuple)):
                return [_deep_convert_idprop(x) for x in val]
            # scalar: int, float, str, bool — pass through
            return val

        bone_props = {}  # name -> dict of custom props
        for bone_name in weapon_bones:
            src_bone = source_armature.data.bones.get(bone_name)
            if src_bone:
                props = {}
                for key in src_bone.keys():
                    if key.startswith("_"):
                        continue  # skip internal blender props
                    props[key] = _deep_convert_idprop(src_bone[key])
                bone_props[bone_name] = props

        # create matching bones on PROXY in edit mode
        context.view_layer.objects.active = proxy_armature
        proxy_armature.select_set(True)
        with _ensure_all_bone_collections_visible(proxy_armature):
            _safe_mode_set("EDIT", proxy_armature)

            for bone_name in weapon_bones:
                bd = bone_data.get(bone_name)
                if not bd:
                    continue
                if bone_name not in proxy_armature.data.edit_bones:
                    new_bone = proxy_armature.data.edit_bones.new(bone_name)
                    new_bone.head = bd["head"]
                    new_bone.tail = bd["tail"]
                    new_bone.roll = bd["roll"]
                    # parent to the matching parent if it exists on proxy
                    if bd["parent"] and bd["parent"] in proxy_armature.data.edit_bones:
                        new_bone.parent = proxy_armature.data.edit_bones[bd["parent"]]

            _safe_mode_set("POSE", proxy_armature)

        # copy custom properties to proxy bones (must be done after edit mode)
        # values are already deep-converted to plain python types
        for bone_name, props in bone_props.items():
            proxy_bone = proxy_armature.data.bones.get(bone_name)
            if proxy_bone:
                for key, value in props.items():
                    try:
                        proxy_bone[key] = value
                    except Exception as e:
                        print(f"[WeaponImport] Failed to copy prop '{key}' to proxy bone '{bone_name}': {type(value).__name__} = {value!r}: {e}")

        # add copy transforms constraints
        for bone_name in weapon_bones:
            if bone_name in proxy_armature.pose.bones:
                pose_bone = proxy_armature.pose.bones[bone_name]
                # skip if already has a copy constraint for this bone
                has_copy = any(
                    c.type == "COPY_TRANSFORMS" and c.target == source_armature and c.subtarget == bone_name
                    for c in pose_bone.constraints
                )
                if not has_copy:
                    c = pose_bone.constraints.new(type="COPY_TRANSFORMS")
                    c.target = source_armature
                    c.subtarget = bone_name
                    c.name = f"WeaponCopy_{bone_name}"

        _safe_mode_set("OBJECT", proxy_armature)
        print("[WeaponImport] Cloned weapon bones to proxy with COPY_TRANSFORMS constraints")


class OBJECT_OT_ImportModel(bpy.types.Operator, ImportHelper):
    bl_label = "Import rig data (.obj)"
    bl_idname = "object.rbxanims_importmodel"
    bl_description = "Import rig data (.obj)"

    filename_ext = ".obj"
    filter_glob: bpy.props.StringProperty(default="*.obj", options={"HIDDEN"})
    filepath: bpy.props.StringProperty(name="File Path", maxlen=1024, default="")

    def execute(self, context):
        # Do not clear objects
        objnames_before_import = {obj.name for obj in iter_scene_objects(context.scene)}
        if bpy.app.version >= (5, 0, 0):
            bpy.ops.wm.obj_import(
                filepath=self.properties.filepath,
                use_split_groups=True,
                forward_axis="NEGATIVE_Z",
                up_axis="Y",
            )
        elif bpy.app.version >= (4, 0, 0):
            bpy.ops.wm.obj_import(
                filepath=self.properties.filepath,
                use_split_groups=True,
            )
        else:
            bpy.ops.import_scene.obj(
                filepath=self.properties.filepath, use_split_groups=True
            )

        # Get the actual newly imported OBJECTS
        imported_objs = [
            obj for obj in iter_scene_objects(context.scene) if obj.name not in objnames_before_import
        ]

        # Extract meta...
        encodedmeta = ""
        partial = {}
        meta_objs_to_delete = []
        for obj in imported_objs:
            # Case-insensitive match for Meta part names (Roblox/OBJ idiosyncrasies)
            match = re.search(r"^meta(\d+)q1(.*?)q1\d*(\.\d+)?$", obj.name, re.IGNORECASE)
            if match:
                partial[int(match.group(1))] = match.group(2)
                meta_objs_to_delete.append(obj)

        # Check if this is actually a rig file (has metadata)
        if not meta_objs_to_delete:
            self.report(
                {"ERROR"},
                "This OBJ file does not contain Roblox rig metadata. "
                "Please use Blender's standard OBJ importer for regular 3D models, "
                "or export the rig from Roblox Studio using the Roblox Animations plugin.",
            )
            return {"CANCELLED"}

        # The rig parts are simply the imported objects that are not meta objects.
        # This is done before deleting, ensuring we have valid object references.
        meta_set = set(meta_objs_to_delete)
        # capture names BEFORE removal — live refs become stale after
        # bpy.data.objects.remove() re-allocates the container.
        rig_part_names = [obj.name for obj in imported_objs if obj not in meta_set]

        # Batch-remove meta objects: collect orphan mesh data, then purge once.
        # Calling bpy.data.objects.remove() in a loop is O(n²) bc each call
        # triggers depsgraph invalidation. Instead we unlink + batch purge.
        orphan_meshes = []
        for obj in meta_objs_to_delete:
            mesh = obj.data if obj.type == "MESH" else None
            for coll in list(obj.users_collection):
                coll.objects.unlink(obj)
            # Use do_unlink=True to handle edge-cases where Blender still
            # tracks a hidden user after manual collection unlinks.
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh and mesh.users == 0:
                orphan_meshes.append(mesh)
        for mesh in orphan_meshes:
            bpy.data.meshes.remove(mesh)

        # re-fetch by name now that the container is stable
        rig_part_objs = [bpy.data.objects[n] for n in rig_part_names if n in bpy.data.objects]

        try:
            for i in range(1, len(partial) + 1):
                if i in partial:  # Check if the key exists
                    encodedmeta += partial[i]
                else:
                    self.report(
                        {"ERROR"},
                        f"Missing metadata part {i}. The rig file may be corrupted.",
                    )
                    return {"CANCELLED"}

            encodedmeta = encodedmeta.replace("0", "=")

            # Validate encoded metadata is not empty
            if not encodedmeta.strip():
                self.report(
                    {"ERROR"},
                    "Rig metadata is empty or corrupted. The rig file may be corrupted.",
                )
                return {"CANCELLED"}

            try:
                meta = base64.b32decode(encodedmeta, True).decode("utf-8")
            except Exception as e:
                self.report(
                    {"ERROR"},
                    f"Failed to decode rig metadata: {str(e)}. The rig file may be corrupted.",
                )
                return {"CANCELLED"}

            try:
                meta_loaded = json.loads(meta)
            except Exception as e:
                self.report(
                    {"ERROR"},
                    f"Failed to parse rig metadata JSON: {str(e)}. The rig file may be corrupted.",
                )
                return {"CANCELLED"}

            normalized_handle_count = _normalize_accessory_handle_jnames(meta_loaded)
            if normalized_handle_count:
                print(
                    f"[RigImport] normalized {normalized_handle_count} accessory Handle joint name(s) to pname"
                )
                meta = json.dumps(meta_loaded, separators=(",", ":"))

            print(
                f"[RigImport] import_ops build=2.4.6 export_version={meta_loaded.get('version', 'unknown')} "
                f"has_skinned_mesh_metadata={_meta_has_skinned_meshes(meta_loaded)} "
                f"has_filemesh_candidates={_meta_has_filemesh_candidates(meta_loaded)}"
            )

            # --- WEAPON IMPORT PATH ---
            if meta_loaded.get("exportType") == "weapon":
                weapon_name = meta_loaded.get("weaponName", "Weapon")
                _pending_weapon_import.clear()
                _pending_weapon_import["weapon_name"] = weapon_name
                # store object NAMES, not live refs — these cross an
                # operator / undo boundary and live bpy.data refs become
                # stale after undo-step creation (dangling C pointers →
                # build_materials null deref on ctrl+z).
                _pending_weapon_import["data"] = {
                    "meta_loaded": meta_loaded,
                    "rig_part_obj_names": [obj.name for obj in rig_part_objs],
                }
                bpy.ops.object.rbxanims_confirm_weapon_target("INVOKE_DEFAULT")
                return {"FINISHED"}

            # Store meta in an empty
            bpy.ops.object.add(type="EMPTY", location=(0, 0, 0))
            ob = bpy.context.object
            rig_name = meta_loaded.get("rigName", "Rig")
            ob.name = get_unique_name(f"__{rig_name}Meta")
            ob["RigMeta"] = meta

            # Create a unique master collection for this rig
            master_collection_name = get_unique_collection_name(f"RIG: {rig_name}")
            master_collection = bpy.data.collections.new(master_collection_name)
            context.scene.collection.children.link(master_collection)

            # Create a sub-collection for the parts
            parts_collection = bpy.data.collections.new("Parts")
            master_collection.children.link(parts_collection)

            # Move the meta object to the master collection
            for coll in list(ob.users_collection):
                coll.objects.unlink(ob)
            master_collection.objects.link(ob)

            # Move all imported parts to the rig's parts collection
            for obj in rig_part_objs:
                if obj:  # Check if object still exists
                    for coll in list(obj.users_collection):
                        coll.objects.unlink(obj)
                    parts_collection.objects.link(obj)

            renamed_by_index, index_warn = _rename_indexed_parts(meta_loaded, parts_collection)
            if index_warn:
                self.report({"WARNING"}, index_warn)

            if not renamed_by_index:
                # Indexed rename didn't fire (old export or non-standard OBJ names),
                # fall back to size/position fingerprinting.
                renamed_via_fp = _rename_parts_by_size_fingerprint(meta_loaded, parts_collection)
                
                fp_map = meta_loaded.get("_fingerprint_object_map", {})
                rig_scale = meta_loaded.get("_rig_scale", 1.0)
                _rename_parts_by_fingerprint(meta_loaded.get("rig"), parts_collection, renamed_via_fp, fp_map, rig_scale, meta_loaded=meta_loaded)

                fp_map = meta_loaded.get("_fingerprint_object_map", {})
                if fp_map:
                    fp_map_names = {obj_name: obj_name for obj_name in fp_map.keys()}
                    ob["_FingerprintMap"] = json.dumps(fp_map_names)
                    print(f"[RigImport] Stored {len(fp_map_names)} authoritative part mappings")

            else:
                print("[RigImport] Indexed rename succeeded, skipping fingerprint passes")

            has_skinned_mesh_metadata = _meta_has_skinned_meshes(meta_loaded)
            has_filemesh_candidates = _meta_has_filemesh_candidates(meta_loaded)
            has_deform_bones = _rig_contains_deform_bones(meta_loaded.get("rig"))

            print(
                f"[RigImport] deform-detect has_deform_bones={has_deform_bones} "
                f"has_skinned_mesh_metadata={has_skinned_mesh_metadata} "
                f"has_filemesh_candidates={has_filemesh_candidates}"
            )

            if has_deform_bones or has_filemesh_candidates:
                try:
                    majority_skinned = _meta_is_majority_skinned(meta_loaded)
                    bone_mode = "CONNECT" if majority_skinned else "LOCAL_YAXIS_EXTEND"
                    print(f"[RigImport] auto-generating armature for deform/filemesh-candidate rig (mode={bone_mode}, majority_skinned={majority_skinned})")
                    create_rig(bone_mode, ob.name)
                    if has_skinned_mesh_metadata:
                        print("[RigImport] automatic skinning path completed")
                        self.report({"INFO"}, "skinned rig detected: armature generated and skinning applied")
                    elif has_filemesh_candidates:
                        print(
                            "[RigImport] mesh file candidates detected without explicit Studio skinning signal; "
                            "FileMesh parsing will determine whether weights can be reconstructed"
                        )
                    else:
                        export_version = meta_loaded.get("version", "unknown")
                        self.report(
                            {"WARNING"},
                            "deform rig detected and armature generated, but this export is missing skin metadata; "
                            f"re-export from the updated studio plugin to reconstruct weights (export version: {export_version})",
                        )
                        print(
                            "[RigImport] deform bones detected, but partAux has no mesh_id/has_skinning data; "
                            f"skinning cannot be rebuilt from this export (version={export_version})"
                        )
                except Exception as exc:
                    self.report(
                        {"WARNING"},
                        f"imported deform/skinned rig, but automatic armature generation failed: {exc}",
                    )

            return {"FINISHED"}
        except KeyError as e:
            self.report(
                {"ERROR"},
                f"KeyError: {str(e)} - The rig file may be corrupted or incompatible.",
            )
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Error importing rig: {str(e)}")
            return {"CANCELLED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def _import_weapon(self, context, meta_loaded, rig_part_objs):
        """Handle weapon/accessory OBJ import (exportType == 'weapon').

        Two paths:
          1. Motor6D weapon (meta has 'joints'):  Build a bone sub-tree on the
             existing armature mirroring the weapon's Motor6D hierarchy, then
             constrain each mesh to its corresponding bone.
          2. Simple weapon (no Motor6Ds):  Rename meshes, select them, and
             invoke the single-bone attach dialog.
        """
        from mathutils import Matrix
        from ..core.constants import get_transform_to_blender
        from ..core.utils import cf_to_mat
        from ..rig.creation import (
            load_rigbone,
            _build_match_context,
            _safe_mode_set,
        )

        weapon_name = meta_loaded.get("weaponName", "Weapon")
        parts_map = meta_loaded.get("parts", {})
        suggested_bone = _dict_get_any(
            meta_loaded,
            (
                "suggestedBone",
                "suggested_bone",
                "attachmentBone",
                "attachBone",
                "parentBone",
                "parent_bone",
            ),
        ) or ""
        joints_tree = _dict_get_any(meta_loaded, ("joints", "jointsTree", "jointTree"))
        # present only for Motor6D weapons
        weapon_attachments = meta_loaded.get("weaponAttachments")

        print(f"[WeaponImport] Starting import: weapon='{weapon_name}', "
              f"parts_map type={type(parts_map).__name__} len={len(parts_map) if parts_map else 0}, "
              f"has_joints={joints_tree is not None}, suggested_bone='{suggested_bone}'")
        print(f"[WeaponImport] Imported objects: {[o.name for o in rig_part_objs]}")

        # Single-attachment exports may place authoritative attach metadata under
        # weaponAttachments[0] while top-level fields are empty. Normalize here
        # so parent selection and relocation math use the same data path.
        if (
            not meta_loaded.get("_split_import_pass")
            and isinstance(weapon_attachments, list)
            and len(weapon_attachments) == 1
            and isinstance(weapon_attachments[0], dict)
        ):
            attachment = weapon_attachments[0]

            att_joints = _dict_get_any(attachment, ("joints", "jointsTree", "jointTree"))
            if not isinstance(joints_tree, dict) and isinstance(att_joints, dict):
                joints_tree = att_joints
                meta_loaded["joints"] = joints_tree

            if not suggested_bone:
                suggested_bone = _dict_get_any(
                    attachment,
                    (
                        "suggestedBone",
                        "suggested_bone",
                        "attachmentBone",
                        "attachBone",
                        "parentBone",
                        "parent_bone",
                    ),
                ) or ""
                if not suggested_bone and isinstance(joints_tree, dict):
                    suggested_bone = _dict_get_any(
                        joints_tree,
                        (
                            "parentBone",
                            "parent_bone",
                            "parentName",
                            "parentPart",
                            "parentPartName",
                            "attachTo",
                            "attachedTo",
                        ),
                    ) or ""
                if suggested_bone:
                    meta_loaded["suggestedBone"] = suggested_bone

            conn_c0 = _coerce_cf12(
                meta_loaded.get("connectionC0")
                or _dict_get_any(meta_loaded, ("connection_c0", "c0", "C0"))
                or _dict_get_any(attachment, ("connectionC0", "connection_c0", "c0", "C0"))
                or _dict_get_any(joints_tree, ("connectionC0", "connection_c0", "c0", "C0", "jointtransform0", "jointTransform0"))
            )
            conn_c1 = _coerce_cf12(
                meta_loaded.get("connectionC1")
                or _dict_get_any(meta_loaded, ("connection_c1", "c1", "C1"))
                or _dict_get_any(attachment, ("connectionC1", "connection_c1", "c1", "C1"))
                or _dict_get_any(joints_tree, ("connectionC1", "connection_c1", "c1", "C1", "jointtransform1", "jointTransform1"))
            )
            if conn_c0 and not meta_loaded.get("connectionC0"):
                meta_loaded["connectionC0"] = conn_c0
            if conn_c1 and not meta_loaded.get("connectionC1"):
                meta_loaded["connectionC1"] = conn_c1
            if not meta_loaded.get("connectionJointType"):
                joint_type = _dict_get_any(
                    attachment,
                    ("connectionJointType", "connection_joint_type", "jointType", "joint_type"),
                ) or _dict_get_any(joints_tree, ("jointType", "joint_type"))
                if joint_type:
                    meta_loaded["connectionJointType"] = joint_type

            print(
                f"[WeaponImport] Normalized single attachment metadata: "
                f"has_joints={joints_tree is not None}, suggested_bone='{suggested_bone}', "
                f"has_connection={bool(meta_loaded.get('connectionC0') and meta_loaded.get('connectionC1'))}"
            )

        # Additional compatibility pass: some exporters store Motor6D data in
        # nested arrays/tables not covered by the top-level/attachment schema.
        if isinstance(joints_tree, dict):
            root_lookup = joints_tree.get("pname") or joints_tree.get("jname") or weapon_name
            if not isinstance(root_lookup, str):
                root_lookup = weapon_name
            extracted_conn = _extract_motor6d_connection(
                meta_loaded,
                root_lookup,
                suggested_bone or None,
            )
            if extracted_conn:
                if not suggested_bone and extracted_conn.get("parent_name"):
                    suggested_bone = extracted_conn["parent_name"]
                    meta_loaded["suggestedBone"] = suggested_bone
                if not meta_loaded.get("connectionC0") and extracted_conn.get("connectionC0"):
                    meta_loaded["connectionC0"] = extracted_conn["connectionC0"]
                if not meta_loaded.get("connectionC1") and extracted_conn.get("connectionC1"):
                    meta_loaded["connectionC1"] = extracted_conn["connectionC1"]
                if not meta_loaded.get("connectionJointType") and extracted_conn.get("jointType"):
                    meta_loaded["connectionJointType"] = extracted_conn["jointType"]
                print(
                    f"[WeaponImport] Extracted Motor6D connection from nested metadata: "
                    f"parent='{meta_loaded.get('suggestedBone', suggested_bone)}', "
                    f"score={extracted_conn['score']:.2f}, depth={extracted_conn['depth']}"
                )

        # New multi-piece weapon payload (v2.5+): split into independent
        # single-root imports so each piece can attach to a different rig bone.
        if (
            not meta_loaded.get("_split_import_pass")
            and isinstance(weapon_attachments, list)
            and len(weapon_attachments) > 1
        ):
            print(f"[WeaponImport] Multi-attachment import: {len(weapon_attachments)} attachment roots")

            # name -> idx map from partAux for p<idx>x object lookup
            part_name_to_idx = {}
            part_aux = meta_loaded.get("partAux")
            if isinstance(part_aux, dict):
                part_aux = list(part_aux.values())
            if isinstance(part_aux, list):
                for entry in part_aux:
                    if isinstance(entry, dict):
                        idx = entry.get("idx")
                        name = entry.get("name")
                        if isinstance(idx, str) and idx.isdigit():
                            idx = int(idx)
                        if isinstance(idx, int) and isinstance(name, str):
                            part_name_to_idx[name] = idx
                            part_name_to_idx[name.lower()] = idx

            def _extract_import_part_index(obj_name: str):
                """Extract export part index from importer-renamed object names.
                Supports patterns like p12x, p12x.001, P12x1, P12x1.001."""
                base = _strip_suffix(obj_name or "")
                m = re.match(r"(?i)^p(\d+)x(?:\d+)?$", base)
                if m:
                    return int(m.group(1))
                # very defensive fallback for odd importer variants
                m = re.match(r"(?i)^p(\d+)x", base)
                if m:
                    return int(m.group(1))
                return None

            # parse imported object name -> idx
            obj_by_idx = {}
            for obj in rig_part_objs:
                idx = _extract_import_part_index(obj.name)
                if idx is not None:
                    obj_by_idx[idx] = obj

            def _collect_part_names(node, out):
                if not isinstance(node, dict):
                    return
                pname = node.get("pname")
                if isinstance(pname, str):
                    out.add(pname)
                jname = node.get("jname")
                if isinstance(jname, str):
                    out.add(jname)
                for ch in node.get("children", []):
                    _collect_part_names(ch, out)

            imported_count = 0
            for i, attachment in enumerate(weapon_attachments, start=1):
                if not isinstance(attachment, dict):
                    continue
                att_joints = attachment.get("joints")
                if not isinstance(att_joints, dict):
                    continue

                part_names = set()
                _collect_part_names(att_joints, part_names)

                subset_objs = []
                for part_name in part_names:
                    idx = part_name_to_idx.get(part_name)
                    if idx is None and isinstance(part_name, str):
                        idx = part_name_to_idx.get(part_name.lower())
                    if idx is None:
                        continue
                    obj = obj_by_idx.get(idx)
                    if obj:
                        subset_objs.append(obj)

                if not subset_objs:
                    print(f"[WeaponImport] Attachment #{i} skipped: no matching imported objs")
                    continue

                sub_meta = dict(meta_loaded)
                sub_meta["_split_import_pass"] = True
                sub_meta["weaponAttachments"] = None
                sub_meta["joints"] = att_joints
                sub_meta["suggestedBone"] = _dict_get_any(
                    attachment,
                    (
                        "suggestedBone",
                        "suggested_bone",
                        "attachmentBone",
                        "attachBone",
                        "parentBone",
                        "parent_bone",
                    ),
                ) or suggested_bone
                sub_meta["connectionC0"] = _coerce_cf12(
                    _dict_get_any(attachment, ("connectionC0", "connection_c0", "c0", "C0"))
                    or _dict_get_any(att_joints, ("connectionC0", "connection_c0", "c0", "C0", "jointtransform0", "jointTransform0"))
                )
                sub_meta["connectionC1"] = _coerce_cf12(
                    _dict_get_any(attachment, ("connectionC1", "connection_c1", "c1", "C1"))
                    or _dict_get_any(att_joints, ("connectionC1", "connection_c1", "c1", "C1", "jointtransform1", "jointTransform1"))
                )
                sub_meta["connectionJointType"] = _dict_get_any(
                    attachment,
                    ("connectionJointType", "connection_joint_type", "jointType", "joint_type"),
                ) or _dict_get_any(att_joints, ("jointType", "joint_type"))

                # self may be a lightweight reporter proxy (no bound method),
                # so recurse via the class method explicitly.
                result = OBJECT_OT_ImportModel._import_weapon(
                    self, context, sub_meta, subset_objs
                )
                if result == {"CANCELLED"}:
                    return {"CANCELLED"}
                if result == {"FINISHED"}:
                    imported_count += 1

            if imported_count > 0:
                self.report({"INFO"}, f"Imported {imported_count} weapon attachment root(s).")
                return {"FINISHED"}
            return {"CANCELLED"}

        # ---- Early bone validation (before any scene mutations) ----
        # For Motor6D weapons we need the suggested bone to exist on the
        # target armature.  Validate NOW so we can bail cleanly without
        # leaving orphaned collections/objects that crash on undo.
        settings_early = getattr(context.scene, "rbx_anim_settings", None)
        arm_name_early = settings_early.rbx_anim_armature if settings_early else None
        armature_early = None
        if arm_name_early:
            armature_early = get_object_by_name(arm_name_early, context.scene)
            if armature_early and armature_early.type != "ARMATURE":
                armature_early = None

        if joints_tree and armature_early and suggested_bone:
            resolved = None
            if suggested_bone in armature_early.data.bones:
                resolved = suggested_bone
            else:
                for b in armature_early.data.bones:
                    if b.name.lower() == suggested_bone.lower():
                        resolved = b.name
                        break
            if not resolved:
                self.report(
                    {"ERROR"},
                    f"Bone \"{suggested_bone}\" not found on \"{armature_early.name}\".",
                )
                return {"CANCELLED"}

        # ---- Create a temporary collection for weapon parts ----
        # (mirrors the rig import flow: collection → indexed rename → fingerprint)
        from ..rig.creation import get_unique_collection_name
        weapon_coll_name = get_unique_collection_name(f"WEAPON: {weapon_name}")
        weapon_coll = bpy.data.collections.new(weapon_coll_name)
        context.scene.collection.children.link(weapon_coll)

        parts_coll = bpy.data.collections.new("Parts")
        weapon_coll.children.link(parts_coll)

        # move all imported mesh objects into the parts collection
        for obj in rig_part_objs:
            if obj:
                for coll in list(obj.users_collection):
                    coll.objects.unlink(obj)
                parts_coll.objects.link(obj)

        # ---- Rename indexed exports when needed; otherwise accept direct names ----
        renamed_by_index, index_warn = _rename_indexed_parts(meta_loaded, parts_coll)
        if index_warn:
            print(f"[WeaponImport] Index rename warning: {index_warn}")

        if renamed_by_index:
            print("[WeaponImport] Indexed rename succeeded")
        elif _parts_already_named(meta_loaded.get("parts", {}), parts_coll):
            print("[WeaponImport] Imported meshes already use exported part names")
        else:
            # fallback to fingerprint matching (same as rig)
            renamed_via_fp = _rename_parts_by_size_fingerprint(meta_loaded, parts_coll)
            fp_map = meta_loaded.get("_fingerprint_object_map", {})
            # weapon has no "rig" key, so skip tree-based fingerprinting
            print(f"[WeaponImport] Indexed rename failed, fingerprint renamed {renamed_via_fp} parts")

        # collect weapon meshes after rename
        weapon_meshes = [obj for obj in parts_coll.objects if obj.type == "MESH"]
        mesh_by_name = {obj.name: obj for obj in weapon_meshes}

        print(f"[WeaponImport] Renamed {len(weapon_meshes)} weapon meshes: {[o.name for o in weapon_meshes]}")

        if not weapon_meshes:
            self.report({"WARNING"}, "No weapon meshes found in import")
            return {"CANCELLED"}

        # ---- locate the target armature ----
        settings = getattr(context.scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        armature = None
        if arm_name:
            armature = get_object_by_name(arm_name, context.scene)
            if armature and armature.type != "ARMATURE":
                armature = None

        if not armature:
            print(f"[WeaponImport] WARNING: No armature found (arm_name={arm_name!r}). "
                  f"Weapon bones cannot be created without an active rig.")

        # ==================================================================
        # PATH 1:  Motor6D weapon — build bone sub-tree
        # ==================================================================
        if joints_tree and armature:
            print(f"[WeaponImport] Motor6D weapon '{weapon_name}' — building bone sub-tree")

            # find the parent bone on the existing armature
            # (already validated in early check above, this is just resolution)
            parent_bone_name = suggested_bone
            if parent_bone_name and parent_bone_name not in armature.data.bones:
                # try case-insensitive lookup
                for b in armature.data.bones:
                    if b.name.lower() == parent_bone_name.lower():
                        parent_bone_name = b.name
                        break
                else:
                    # should never happen — early validation catches this
                    print(f"[WeaponImport] BUG: bone '{suggested_bone}' passed "
                          f"early check but not found now")
                    parent_bone_name = None

            if not parent_bone_name:
                # Metadata from some exporter variants omits suggestedBone.
                # Infer the best parent from transform proximity before any
                # blind fallback to the first armature bone.
                inferred_parent, inferred_dist = _infer_weapon_parent_bone_from_transform(
                    armature, joints_tree
                )
                if inferred_parent:
                    parent_bone_name = inferred_parent
                    print(
                        f"[WeaponImport] Inferred parent bone '{parent_bone_name}' "
                        f"from weapon root transform (distance={inferred_dist:.4f})"
                    )
                elif armature.data.bones:
                    parent_bone_name = armature.data.bones[0].name
                    print(f"[WeaponImport] WARNING: falling back to "
                          f"'{parent_bone_name}' (unexpected)")
                else:
                    self.report({"ERROR"}, "Target armature has no bones")
                    return {"CANCELLED"}

            print(f"[WeaponImport] Parent bone: '{parent_bone_name}'")

            # try to move weapon meshes into the rig's existing Parts collection
            from ..core.utils import (
                find_master_collection_for_object,
                find_parts_collection_in_master,
            )
            master_coll = find_master_collection_for_object(armature)
            rig_parts_coll = find_parts_collection_in_master(master_coll, create_if_missing=False)
            target_coll = rig_parts_coll or parts_coll  # use rig's if available, else weapon's own
            if rig_parts_coll and rig_parts_coll != parts_coll:
                for obj in weapon_meshes:
                    for coll in list(obj.users_collection):
                        coll.objects.unlink(obj)
                    rig_parts_coll.objects.link(obj)
                # remove the now-empty weapon parts collection
                weapon_coll.children.unlink(parts_coll)
                bpy.data.collections.remove(parts_coll)
                # move the weapon_coll under the rig master instead of scene root
                if master_coll:
                    context.scene.collection.children.unlink(weapon_coll)
                    master_coll.children.link(weapon_coll)

            # build a match_ctx so load_rigbone can link meshes to bones
            match_ctx = _build_match_context(target_coll)

            # populate fingerprint_object_map so load_rigbone finds our meshes
            fp_map = {}
            for name, obj in mesh_by_name.items():
                fp_map[name] = obj
            match_ctx["fingerprint_object_map"] = fp_map
            print(f"[WeaponImport] fp_map keys: {list(fp_map.keys())}")

            # deselect everything and ensure object mode before switching
            _safe_mode_set("OBJECT")
            try:
                bpy.ops.object.select_all(action="DESELECT")
            except Exception:
                pass

            # enter edit mode on the armature
            prev_active = context.view_layer.objects.active
            context.view_layer.objects.active = armature
            armature.select_set(True)
            # unhide all bone collections so edit_bones can see hidden bones
            with _ensure_all_bone_collections_visible(armature):
                entered = _safe_mode_set("EDIT", armature)
                if not entered:
                    self.report({"ERROR"}, "Failed to enter edit mode on armature")
                    return {"CANCELLED"}

                parent_edit_bone = armature.data.edit_bones.get(parent_bone_name)
                if not parent_edit_bone:
                    _safe_mode_set("OBJECT", armature)
                    self.report({"ERROR"}, f"Bone '{parent_bone_name}' not found on armature in edit mode")
                    return {"CANCELLED"}

            # ---- Weapon bone strategy ----
            # Use the SAME load_rigbone flow as normal rig import so that all
            # position / rotation math is handled identically.  The weapon
            # root gets its own bone (e.g. "Handle") parented to the existing
            # parent bone (e.g. "RightHand").  Child weapon parts get bones
            # parented to the weapon root bone.
            #
            # The weapon root needs jointtransform0/1 so load_rigbone can
            # compute its offset from the parent.  If the exporter provided
            # connectionC0/C1, those ARE the Motor6D transforms.  Otherwise
            # we use identity (weapon root lands on parent bone head).

            from ..rig.constraints import link_object_to_bone_rigid
            t2b = get_transform_to_blender()

            root_jname = joints_tree.get("jname", weapon_name)
            weapon_children = joints_tree.get("children", [])
            parent_transform_prop = parent_edit_bone.get("transform")
            if parent_transform_prop:
                parent_part_world_mat = Matrix([list(row) for row in parent_transform_prop])
            else:
                parent_part_world_mat = t2b.inverted() @ Matrix.Translation(parent_edit_bone.head)
                print("[WeaponImport] WARNING: no stored transform on parent bone, using bone head")

            print(f"[WeaponImport] Weapon root '{root_jname}' will be parented "
                  f"to bone '{parent_bone_name}'")
            print(f"[WeaponImport] {len(weapon_children)} child joint(s)")

            # ---- Relocate weapon transforms to parent bone's coordinate space ----
            # The weapon's `transform` fields are absolute Roblox world CFrames.
            # The rig bones are also at their Roblox world positions.  If the
            # weapon was not co-located with the rig (common — the weapon sits
            # in Workspace or StarterPack, not equipped on the character), we
            # need to shift all weapon transforms so the weapon root aligns
            # with where it WOULD be if it were attached to the parent bone.
            #
            # We compute the delta in Roblox space and apply it to every
            # `transform` field in the joint tree.  This way load_rigbone
            # (which reads `transform` to compute bone head) places everything
            # in the right spot — same as if the weapon had been at that
            # position during export.
            weapon_root_cf = joints_tree.get("transform")
            if weapon_root_cf:
                # Where the weapon root CFrame IS (roblox world → blender)
                weapon_root_mat = cf_to_mat(weapon_root_cf)
                weapon_root_pos_blender = (t2b @ weapon_root_mat).to_translation()

                # Where we WANT the weapon root: its EQUIPPED position.
                #
                # Roblox joint equation:
                #   ParentPart.CFrame * C0 = WeaponRoot.CFrame * C1
                #   WeaponRoot.CFrame = ParentPart.CFrame * C0 * C1^-1
                #
                # After relocation, load_rigbone applies C1 (jointtransform1):
                #   bone.head = equipped_cf * C1
                #             = parent * C0 * C1^-1 * C1
                #             = parent * C0   (correct joint position)
                #
                # The parent bone stores its Roblox CFrame in the "transform"
                # custom property (set by load_rigbone during rig import).
                parent_cf_mat = parent_part_world_mat

                conn_c0 = _coerce_cf12(
                    meta_loaded.get("connectionC0")
                    or _dict_get_any(meta_loaded, ("connection_c0", "c0", "C0"))
                    or _dict_get_any(joints_tree, ("connectionC0", "connection_c0", "c0", "C0", "jointtransform0", "jointTransform0"))
                )
                conn_c1 = _coerce_cf12(
                    meta_loaded.get("connectionC1")
                    or _dict_get_any(meta_loaded, ("connection_c1", "c1", "C1"))
                    or _dict_get_any(joints_tree, ("connectionC1", "connection_c1", "c1", "C1", "jointtransform1", "jointTransform1"))
                )
                inferred_conn = False
                # When exporter metadata omits connectionC0/C1, derive a stable
                # local joint from current parent/world transforms.
                # Prefer a parent bone endpoint (head/tail) nearest the weapon
                # root so the inferred pivot is at the limb/hand contact point,
                # not the weapon center.
                if not (conn_c0 and conn_c1):
                    try:
                        weapon_root_pos_bl = (t2b @ weapon_root_mat).to_translation()
                        cand_head = parent_edit_bone.head.copy()
                        cand_tail = parent_edit_bone.tail.copy()
                        if (cand_tail - cand_head).length > 1e-5:
                            if (weapon_root_pos_bl - cand_tail).length <= (weapon_root_pos_bl - cand_head).length:
                                joint_anchor_bl = cand_tail
                                anchor_name = "tail"
                            else:
                                joint_anchor_bl = cand_head
                                anchor_name = "head"
                        else:
                            joint_anchor_bl = cand_head
                            anchor_name = "head"

                        joint_anchor_rb = (t2b.inverted() @ Matrix.Translation(joint_anchor_bl)).to_translation()
                        joint_world_mat = weapon_root_mat.copy()
                        joint_world_mat.translation = joint_anchor_rb

                        inferred_c0_mat = parent_cf_mat.inverted() @ joint_world_mat
                        inferred_c1_mat = weapon_root_mat.inverted() @ joint_world_mat
                        conn_c0 = mat_to_cf(inferred_c0_mat)
                        conn_c1 = mat_to_cf(inferred_c1_mat)
                        if not meta_loaded.get("connectionC0"):
                            meta_loaded["connectionC0"] = conn_c0
                        if not meta_loaded.get("connectionC1"):
                            meta_loaded["connectionC1"] = conn_c1
                        if not meta_loaded.get("connectionJointType"):
                            meta_loaded["connectionJointType"] = "Motor6D"
                        inferred_conn = True
                        print(
                            f"[WeaponImport] Inferred joint anchor from parent bone {anchor_name} "
                            f"for missing C0/C1"
                        )
                    except Exception:
                        conn_c0 = None
                        conn_c1 = None

                equipped_cf = weapon_root_mat
                if conn_c0 and conn_c1:
                    c0_mat = cf_to_mat(conn_c0)
                    c1_mat = cf_to_mat(conn_c1)
                    equipped_cf = parent_cf_mat @ c0_mat @ c1_mat.inverted()
                    if inferred_conn:
                        print("[WeaponImport] Inferred missing C0/C1 from parent and weapon root transform")
                    print("[WeaponImport] Target = parent * C0 * C1^-1 "
                          "(equipped position)")
                else:
                    print("[WeaponImport] No C0/C1 — keeping exported weapon root transform")

                target_pos = (t2b @ equipped_cf).to_translation()

                # Delta for BONE TRANSFORMS (CFrame-based, in roblox space)
                delta_blender = target_pos - weapon_root_pos_blender

                # Delta for MESHES — use the actual mesh vertex center rather
                # than the CFrame, since OBJ axis handling may produce a
                # slightly different position than t2b @ CFrame.
                root_pname_lookup = joints_tree.get("pname") or root_jname
                root_mesh_for_delta = mesh_by_name.get(root_pname_lookup)
                if not root_mesh_for_delta:
                    for n, o in mesh_by_name.items():
                        if n.lower() == (root_pname_lookup or "").lower():
                            root_mesh_for_delta = o
                            break
                if root_mesh_for_delta:
                    from ..rig.creation import _get_mesh_world_center
                    actual_mesh_center = _get_mesh_world_center(root_mesh_for_delta)
                    _ = target_pos - actual_mesh_center
                else:
                    _ = delta_blender

                if delta_blender.length > 0.0001:
                    # Compute the FULL rigid relocation matrix in roblox space.
                    # This handles both translation AND rotation so the weapon
                    # aligns to the rig no matter which direction it faces.
                    #   relocation = equipped_cf @ weapon_root_cf^-1
                    # Applied to each node:
                    #   new_transform = relocation @ old_transform
                    relocation_mat = equipped_cf @ weapon_root_mat.inverted()

                    def _relocate_joint_transforms(node, reloc):
                        tf = node.get("transform")
                        if tf and len(tf) >= 12:
                            old_mat = cf_to_mat(tf)
                            new_mat = reloc @ old_mat
                            new_cf = mat_to_cf(new_mat)
                            for i in range(len(new_cf)):
                                tf[i] = new_cf[i]
                        for child in node.get("children", []):
                            _relocate_joint_transforms(child, reloc)

                    _relocate_joint_transforms(joints_tree, relocation_mat)

                    # Move + rotate mesh objects in blender space
                    reloc_blender = t2b @ relocation_mat @ t2b.inverted()
                    _safe_mode_set("OBJECT", armature)
                    for obj in weapon_meshes:
                        # Apply the full rigid transform to each mesh
                        obj.matrix_world = reloc_blender @ obj.matrix_world
                    print(f"[WeaponImport] Relocated weapon: delta={delta_blender.length:.4f}")

                    # re-enter edit mode
                    context.view_layer.objects.active = armature
                    armature.select_set(True)
                    _safe_mode_set("EDIT", armature)
                    parent_edit_bone = armature.data.edit_bones.get(parent_bone_name)

            # Inject connection joint transforms into the weapon root node
            # so load_rigbone knows how to offset it from the parent bone.
            if "jointtransform0" not in joints_tree:
                conn_c0 = meta_loaded.get("connectionC0")
                conn_c1 = meta_loaded.get("connectionC1")
                if conn_c0 and conn_c1:
                    joints_tree["jointtransform0"] = conn_c0
                    joints_tree["jointtransform1"] = conn_c1
                    joints_tree["jointType"] = meta_loaded.get(
                        "connectionJointType", "Motor6D")
                    print("[WeaponImport] Using connectionC0/C1 for root joint")
                else:
                    # Identity — weapon root bone sits exactly on parent bone
                    joints_tree["jointtransform0"] = [
                        0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]
                    joints_tree["jointtransform1"] = [
                        0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]
                    print("[WeaponImport] No connection data — identity joint")

            original_parent_map = _annotate_weapon_original_parents(
                joints_tree,
                parent_bone_name,
                parent_part_world_mat,
            )
            if original_parent_map:
                print(
                    f"[WeaponImport] Preserved original Motor6D parents for {len(original_parent_map)} weapon bone(s)"
                )

            # ---- Build weapon bones via load_rigbone (same as rig import) ----
            rigging_type = "RAW"
            try:
                load_rigbone(
                    armature, rigging_type, joints_tree,
                    parent_edit_bone, target_coll, match_ctx,
                )
            except Exception as e:
                import traceback
                traceback.print_exc()
                _safe_mode_set("OBJECT", armature)
                self.report({"ERROR"}, f"Failed to build weapon bones: {e}")
                return {"CANCELLED"}

            _safe_mode_set("OBJECT", armature)

            # Verify root bone was created
            if root_jname not in armature.data.bones:
                print(f"[WeaponImport] WARNING: root bone '{root_jname}' not "
                      f"found. Bones: {[b.name for b in armature.data.bones]}")
            else:
                print(f"[WeaponImport] Root bone '{root_jname}' created ok")

            for bone_name, original_parent_name in original_parent_map.items():
                data_bone = armature.data.bones.get(bone_name)
                if data_bone and original_parent_name:
                    data_bone["rbx_original_parent"] = original_parent_name

            # ---- Apply pending constraints (mesh → bone CHILD_OF) ----
            pending = match_ctx.get("pending_constraints", [])
            applied = 0
            for obj, bone_name in pending:
                bone = armature.data.bones.get(bone_name)
                if bone:
                    link_object_to_bone_rigid(obj, armature, bone)
                    applied += 1
                    print(f"[WeaponImport] Constrained '{obj.name}' -> "
                          f"bone '{bone_name}'")
                else:
                    print(f"[WeaponImport] WARNING: bone '{bone_name}' not "
                          f"found for '{obj.name}'")

            # Constrain any remaining orphan meshes to the weapon root bone
            constrained_objs = {obj for obj, _ in pending}
            root_bone_obj = armature.data.bones.get(root_jname)
            for obj in weapon_meshes:
                if obj not in constrained_objs:
                    target_bone = root_bone_obj or (
                        armature.data.bones.get(parent_bone_name))
                    if target_bone:
                        link_object_to_bone_rigid(obj, armature, target_bone)
                        applied += 1
                        print(f"[WeaponImport] Constrained orphan "
                              f"'{obj.name}' -> '{target_bone.name}'")

            if prev_active:
                context.view_layer.objects.active = prev_active

            self.report(
                {"INFO"},
                f"Imported weapon '{weapon_name}': root '{root_jname}' → bone "
                f"'{parent_bone_name}', {len(weapon_children)} sub-bones, "
                f"{applied} mesh(es) constrained.",
            )
            return {"FINISHED"}

        # ==================================================================
        # PATH 2:  Simple weapon (no Motor6Ds) — single-bone attach
        # ==================================================================
        bpy.ops.object.select_all(action="DESELECT")
        for obj in weapon_meshes:
            obj.select_set(True)
        context.view_layer.objects.active = weapon_meshes[0]

        if armature:
            return bpy.ops.object.rbxanims_attach_to_bone(
                "INVOKE_DEFAULT",
                bone_name=suggested_bone,
                weapon_bone_name=weapon_name,
            )

        names = ", ".join(o.name for o in weapon_meshes)
        hint = f" (suggested bone: {suggested_bone})" if suggested_bone else ""
        self.report(
            {"INFO"},
            f"Imported weapon '{weapon_name}': {names}{hint}. "
            "Select an armature and use Attach to Bone to finish.",
        )
        return {"FINISHED"}


class OBJECT_OT_ImportFbxAnimation(bpy.types.Operator, ImportHelper):
    bl_label = "Import animation data (.fbx)"
    bl_idname = "object.rbxanims_importfbxanimation"
    bl_description = "Import animation data (.fbx) --- FBX file should contain an armature, which will be mapped onto the generated rig by bone names."

    filename_ext = ".fbx"
    filter_glob: bpy.props.StringProperty(default="*.fbx", options={"HIDDEN"})
    filepath: bpy.props.StringProperty(name="File Path", maxlen=1024, default="")

    @classmethod
    def poll(cls, context):
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        return get_object_by_name(armature_name)

    def execute(self, context):
        from ..animation.import_export import (
            get_mapping_error_bones,
            prepare_for_kf_map,
            copy_anim_state,
            apply_ao_transform,
        )
        from ..core.utils import get_action_fcurves
        import math

        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        
        # Get target armature early to fail fast
        armature = get_object_by_name(armature_name)
        if not armature:
            self.report(
                {"ERROR"},
                f"No armature named '{armature_name}' found. Please ensure the correct rig is selected.",
            )
            return {"CANCELLED"}

        # Ensure active keying set exists, create one if needed
        if not bpy.context.scene.keying_sets.active:
            bpy.ops.anim.keying_set_add()
            self.report({"INFO"}, "Created new keying set for animation import.")

        # Import and keep track of what is imported (use set for faster lookup)
        objnames_before_import = {obj.name for obj in iter_scene_objects(context.scene)}
        bpy.ops.import_scene.fbx(filepath=self.properties.filepath)
        objnames_imported = [
            obj.name for obj in iter_scene_objects(context.scene) if obj.name not in objnames_before_import
        ]

        def clear_imported():
            """Clean up all objects imported from the FBX file."""
            for obj_name in objnames_imported:
                obj = get_object_by_name(obj_name)
                if obj:
                    bpy.data.objects.remove(obj)

        # Check that there's exactly 1 armature in the imported file
        armatures_imported = [
            obj for obj in iter_scene_objects(context.scene)
            if obj.type == "ARMATURE" and obj.name in objnames_imported
        ]
        if len(armatures_imported) == 0:
            self.report({"ERROR"}, "Imported FBX file contains no armature.")
            clear_imported()
            return {"CANCELLED"}
        if len(armatures_imported) > 1:
            self.report(
                {"ERROR"},
                f"Imported FBX file contains {len(armatures_imported)} armatures, expected 1.",
            )
            clear_imported()
            return {"CANCELLED"}

        ao_imp = armatures_imported[0]

        # Validate bone mapping between source and target
        err_mappings = get_mapping_error_bones(armature, ao_imp)
        if err_mappings:
            self.report(
                {"ERROR"},
                f"Cannot map rig, the following bones are missing from the source rig: {', '.join(err_mappings)}.",
            )
            clear_imported()
            return {"CANCELLED"}

        # Validate imported armature has animation data
        if not ao_imp.animation_data or not ao_imp.animation_data.action:
            self.report({"ERROR"}, "Imported FBX armature contains no animation data.")
            clear_imported()
            return {"CANCELLED"}

        fcurves = get_action_fcurves(ao_imp.animation_data.action)
        if not fcurves:
            self.report({"ERROR"}, "Imported FBX armature contains no animation curves.")
            clear_imported()
            return {"CANCELLED"}

        # Get keyframes and set frame range
        kp_frames = [kp.co.x for fcurve in fcurves for kp in fcurve.keyframe_points]
        if not kp_frames:
            self.report({"ERROR"}, "Imported FBX armature contains no keyframes.")
            clear_imported()
            return {"CANCELLED"}

        bpy.context.scene.frame_start = math.floor(min(kp_frames))
        bpy.context.scene.frame_end = math.ceil(max(kp_frames))

        # Apply transforms and prepare for keyframe mapping
        bpy.context.view_layer.objects.active = ao_imp
        apply_ao_transform(ao_imp)
        prepare_for_kf_map()

        # Ensure the target armature has animation_data initialized
        if armature.animation_data is None:
            armature.animation_data_create()

        # Copy animation state from imported armature to target
        copy_anim_state(armature, ao_imp)

        clear_imported()
        self.report({"INFO"}, f"Successfully imported animation with {len(kp_frames)} keyframes.")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

