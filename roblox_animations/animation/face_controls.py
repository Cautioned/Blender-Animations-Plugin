"""Face-control helpers, decoded facs payloads, and solver utilities."""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Mapping, Optional


FACE_CONTROL_PROP_NAME = "rbx_face_control_name"
FACE_CONTROL_HELPER_PROP = "rbx_face_control_helper"
FACE_CONTROL_BONE_PREFIX = "FaceControl_"
FACE_DEFORM_BONE_PROP = "rbx_face_deform_bone"
FACE_FACS_DATA_PROP = "rbx_facs_data_json"
FACE_FACS_CONTROLS_PROP = "rbx_facs_controls_json"
FACE_FACS_UI_SYNC_PROP = "_rbx_facs_ui_syncing"

_FACS_ARMATURE_RUNTIME_CACHE = {}
_FACS_ACTIVE_ARMATURES = {}

FACE_CONTROL_ORDER = (
    "LeftEyeClosed",
    "RightEyeClosed",
    "EyesLookDown",
    "EyesLookLeft",
    "EyesLookRight",
    "EyesLookUp",
    "LeftEyeUpperLidRaiser",
    "RightEyeUpperLidRaiser",
    "LeftCheekRaiser",
    "RightCheekRaiser",
    "Corrugator",
    "LeftBrowLowerer",
    "RightBrowLowerer",
    "LeftOuterBrowRaiser",
    "RightOuterBrowRaiser",
    "LeftInnerBrowRaiser",
    "RightInnerBrowRaiser",
    "LeftNoseWrinkler",
    "RightNoseWrinkler",
    "ChinRaiser",
    "ChinRaiserUpperLip",
    "FlatPucker",
    "Funneler",
    "LipPresser",
    "LipsTogether",
    "LowerLipSuck",
    "UpperLipSuck",
    "Pucker",
    "MouthLeft",
    "MouthRight",
    "LeftCheekPuff",
    "RightCheekPuff",
    "LeftDimpler",
    "RightDimpler",
    "LeftLipCornerDown",
    "RightLipCornerDown",
    "LeftLowerLipDepressor",
    "RightLowerLipDepressor",
    "LeftLipCornerPuller",
    "RightLipCornerPuller",
    "LeftLipStretcher",
    "RightLipStretcher",
    "LeftUpperLipRaiser",
    "RightUpperLipRaiser",
    "JawDrop",
    "JawLeft",
    "JawRight",
    "TongueOut",
    "TongueUp",
    "TongueDown",
)

FACE_CONTROL_UI_GROUPS = (
    (
        "Eyes",
        (
            "LeftEyeClosed",
            "RightEyeClosed",
            "EyesLookDown",
            "EyesLookLeft",
            "EyesLookRight",
            "EyesLookUp",
            "LeftEyeUpperLidRaiser",
            "RightEyeUpperLidRaiser",
            "LeftCheekRaiser",
            "RightCheekRaiser",
        ),
    ),
    (
        "Brows and Nose",
        (
            "Corrugator",
            "LeftBrowLowerer",
            "RightBrowLowerer",
            "LeftOuterBrowRaiser",
            "RightOuterBrowRaiser",
            "LeftInnerBrowRaiser",
            "RightInnerBrowRaiser",
            "LeftNoseWrinkler",
            "RightNoseWrinkler",
        ),
    ),
    (
        "Mouth",
        (
            "ChinRaiser",
            "ChinRaiserUpperLip",
            "FlatPucker",
            "Funneler",
            "LipPresser",
            "LipsTogether",
            "LowerLipSuck",
            "UpperLipSuck",
            "Pucker",
            "MouthLeft",
            "MouthRight",
            "LeftCheekPuff",
            "RightCheekPuff",
            "LeftDimpler",
            "RightDimpler",
            "LeftLipCornerDown",
            "RightLipCornerDown",
            "LeftLowerLipDepressor",
            "RightLowerLipDepressor",
            "LeftLipCornerPuller",
            "RightLipCornerPuller",
            "LeftLipStretcher",
            "RightLipStretcher",
            "LeftUpperLipRaiser",
            "RightUpperLipRaiser",
        ),
    ),
    (
        "Jaw and Tongue",
        (
            "JawDrop",
            "JawLeft",
            "JawRight",
            "TongueOut",
            "TongueUp",
            "TongueDown",
        ),
    ),
)


def _ordered_unique(values):
    ordered = []
    seen = set()
    for value in values or []:
        if value in seen:
            continue
        ordered.append(value)
        seen.add(value)
    return ordered


def _compact_json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _coerce_facs_payload(payload) -> Optional[dict]:
    if payload is None:
        return None
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise TypeError("facs payload must be a dict or json string")
    return payload


def _armature_runtime_cache_key(armature_obj) -> int:
    try:
        return int(armature_obj.as_pointer())
    except Exception:
        return id(armature_obj)


def _state_signature_from_holder(control_holder, runtime: dict) -> tuple[float, ...]:
    signature = []
    for property_name in runtime.get("control_prop_names") or ():
        try:
            signature.append(float(getattr(control_holder, property_name, 0.0) or 0.0))
        except Exception:
            signature.append(0.0)
    return tuple(signature)


def _normalized_state_from_signature(runtime: dict, state_signature: tuple[float, ...]) -> dict:
    return {
        control_name: state_signature[index]
        for index, control_name in enumerate(runtime.get("control_names") or ())
    }


def _apply_runtime_solution(
    armature_obj,
    payload: dict,
    runtime_entry: dict,
    state_signature: tuple[float, ...],
    persist_state: bool = True,
    apply_token=None,
) -> dict:
    if (
        runtime_entry.get("last_signature") == state_signature
        and runtime_entry.get("last_apply_token") == apply_token
    ):
        return runtime_entry.get("last_solved") or {}

    runtime = runtime_entry["compiled"]
    solved = runtime_entry.get("last_solved") or {}
    if runtime_entry.get("last_signature") != state_signature:
        pose_weights = _compute_runtime_state_weights(runtime, state_signature)
        solved = _solve_runtime_facs_bone_transforms(runtime, pose_weights)
    pose = getattr(armature_obj, "pose", None)
    pose_bones = getattr(pose, "bones", None)
    if pose_bones is not None:
        for bone_name in payload.get("face_bone_names") or []:
            pose_bone = pose_bones.get(bone_name)
            if pose_bone is None:
                continue
            transform = solved.get(bone_name) or {
                "position": (0.0, 0.0, 0.0),
                "rotation": (0.0, 0.0, 0.0),
            }
            if pose_bone.rotation_mode != "XYZ":
                pose_bone.rotation_mode = "XYZ"
            pose_bone.location = transform["position"]
            pose_bone.rotation_euler = transform["rotation"]

    if persist_state:
        state_json = _compact_json(_normalized_state_from_signature(runtime, state_signature))
        if runtime_entry.get("last_state_json") != state_json:
            armature_obj[FACE_FACS_CONTROLS_PROP] = state_json
            runtime_entry["last_state_json"] = state_json
    runtime_entry["last_signature"] = state_signature
    runtime_entry["last_solved"] = solved
    runtime_entry["last_apply_token"] = apply_token
    return solved


def _build_facs_runtime(payload: dict) -> dict:
    bone_pose_transforms = payload.get("bone_pose_transforms") or {}
    control_names = tuple(payload.get("face_control_names") or ())
    facs_pose_names = list(payload.get("facs_pose_names") or [])
    if bone_pose_transforms:
        facs_pose_names.extend(
            pose_name
            for pose_transforms in bone_pose_transforms.values()
            for pose_name in pose_transforms.keys()
        )
    pose_names = tuple(_ordered_unique(list(control_names) + facs_pose_names))
    pose_index_by_name = {pose_name: pose_index for pose_index, pose_name in enumerate(pose_names)}

    control_indices = tuple(pose_index_by_name[control_name] for control_name in control_names)

    two_pose_correctives = []
    for corrective in payload.get("two_pose_correctives") or []:
        control_names_pair = tuple(corrective.get("control_names") or ())
        if len(control_names_pair) != 2:
            continue
        name_a, name_b = control_names_pair
        pose_name = corrective.get("name") or f"x2_{name_a}_{name_b}"
        if pose_name not in pose_index_by_name or name_a not in pose_index_by_name or name_b not in pose_index_by_name:
            continue
        two_pose_correctives.append(
            (pose_index_by_name[pose_name], pose_index_by_name[name_a], pose_index_by_name[name_b])
        )

    three_pose_correctives = []
    for corrective in payload.get("three_pose_correctives") or []:
        control_names_triplet = tuple(corrective.get("control_names") or ())
        if len(control_names_triplet) != 3:
            continue
        name_a, name_b, name_c = control_names_triplet
        pose_name = corrective.get("name") or f"x3_{name_a}_{name_b}_{name_c}"
        if (
            pose_name not in pose_index_by_name
            or name_a not in pose_index_by_name
            or name_b not in pose_index_by_name
            or name_c not in pose_index_by_name
        ):
            continue
        three_pose_correctives.append(
            (
                pose_index_by_name[pose_name],
                pose_index_by_name[name_a],
                pose_index_by_name[name_b],
                pose_index_by_name[name_c],
            )
        )

    bone_entries = []
    for bone_name in tuple(payload.get("face_bone_names") or bone_pose_transforms.keys()):
        pose_entries = []
        for pose_name, transform in (bone_pose_transforms.get(bone_name) or {}).items():
            pose_index = pose_index_by_name.get(pose_name)
            if pose_index is None:
                continue
            position = transform.get("position") or (0.0, 0.0, 0.0)
            rotation = transform.get("rotation") or (0.0, 0.0, 0.0)
            pose_entries.append(
                (
                    pose_index,
                    float(position[0]),
                    float(position[1]),
                    float(position[2]),
                    math.radians(float(rotation[0])),
                    math.radians(float(rotation[1])),
                    math.radians(float(rotation[2])),
                )
            )
        bone_entries.append((bone_name, tuple(pose_entries)))

    return {
        "control_names": control_names,
        "control_prop_names": tuple(face_control_property_name(control_name) for control_name in control_names),
        "pose_names": pose_names,
        "control_indices": control_indices,
        "two_pose_correctives": tuple(two_pose_correctives),
        "three_pose_correctives": tuple(three_pose_correctives),
        "bone_entries": tuple(bone_entries),
    }


def _get_armature_facs_runtime(armature_obj, payload=None) -> Optional[dict]:
    if armature_obj is None:
        return None

    cache_key = _armature_runtime_cache_key(armature_obj)
    raw_payload = None
    if payload is None:
        try:
            raw_payload = armature_obj.get(FACE_FACS_DATA_PROP)
        except Exception:
            raw_payload = None
        if not raw_payload:
            _FACS_ARMATURE_RUNTIME_CACHE.pop(cache_key, None)
            _FACS_ACTIVE_ARMATURES.pop(cache_key, None)
            return None

    cached = _FACS_ARMATURE_RUNTIME_CACHE.get(cache_key)
    if cached is not None:
        if raw_payload is not None and cached.get("raw_payload") == raw_payload:
            return cached
        if payload is not None and cached.get("payload") is payload:
            return cached

    payload = _coerce_facs_payload(raw_payload if payload is None else payload)
    if not payload:
        _FACS_ARMATURE_RUNTIME_CACHE.pop(cache_key, None)
        _FACS_ACTIVE_ARMATURES.pop(cache_key, None)
        return None

    runtime = {
        "raw_payload": raw_payload,
        "payload": payload,
        "compiled": _build_facs_runtime(payload),
        "last_signature": None,
        "last_apply_token": None,
        "last_state_json": None,
        "last_solved": None,
    }
    _FACS_ARMATURE_RUNTIME_CACHE[cache_key] = runtime
    _FACS_ACTIVE_ARMATURES[cache_key] = armature_obj
    return runtime


def _compute_runtime_state_weights(runtime: dict, state_signature: tuple[float, ...]) -> list[float]:
    pose_weights = [0.0] * len(runtime.get("pose_names") or ())
    for value, pose_index in zip(state_signature, runtime.get("control_indices") or ()):
        pose_weights[pose_index] = value
    for pose_index, name_a_index, name_b_index in runtime.get("two_pose_correctives") or ():
        pose_weights[pose_index] = pose_weights[name_a_index] * pose_weights[name_b_index]
    for pose_index, name_a_index, name_b_index, name_c_index in runtime.get("three_pose_correctives") or ():
        pose_weights[pose_index] = (
            pose_weights[name_a_index] * pose_weights[name_b_index] * pose_weights[name_c_index]
        )
    return pose_weights


def _solve_runtime_facs_bone_transforms(runtime: dict, pose_weights: list[float]) -> dict:
    solved = {}
    for bone_name, pose_entries in runtime.get("bone_entries") or ():
        position_x = 0.0
        position_y = 0.0
        position_z = 0.0
        rotation_x = 0.0
        rotation_y = 0.0
        rotation_z = 0.0
        for pose_index, pos_x, pos_y, pos_z, rot_x, rot_y, rot_z in pose_entries:
            weight = pose_weights[pose_index]
            if weight <= 0.0:
                continue
            position_x += pos_x * weight
            position_y += pos_y * weight
            position_z += pos_z * weight
            rotation_x += rot_x * weight
            rotation_y += rot_y * weight
            rotation_z += rot_z * weight
        solved[bone_name] = {
            "position": (position_x, position_y, position_z),
            "rotation": (rotation_x, rotation_y, rotation_z),
        }
    return solved


def face_control_property_name(control_name: str) -> str:
    return "rbx_facs_" + re.sub(r"(?<!^)(?=[A-Z])", "_", control_name).lower()


def ordered_face_controls(control_names) -> list[str]:
    available = set(control_names or [])
    ordered = [control_name for control_name in FACE_CONTROL_ORDER if control_name in available]
    extras = sorted(available.difference(ordered))
    ordered.extend(extras)
    return ordered


def grouped_face_controls(control_names) -> list[tuple[str, list[str]]]:
    available = set(control_names or [])
    grouped = []
    covered = set()
    for label, group_names in FACE_CONTROL_UI_GROUPS:
        visible = [control_name for control_name in group_names if control_name in available]
        if visible:
            grouped.append((label, visible))
            covered.update(visible)

    remaining = [control_name for control_name in ordered_face_controls(control_names) if control_name not in covered]
    if remaining:
        grouped.append(("Other", remaining))
    return grouped


def property_group_control_state(control_holder, control_names) -> dict:
    state = {}
    for control_name in control_names or []:
        try:
            state[control_name] = float(getattr(control_holder, face_control_property_name(control_name), 0.0))
        except Exception:
            state[control_name] = 0.0
    return state


def facs_payload_from_mesh_data(mesh_data) -> Optional[dict]:
    mesh_data = mesh_data or {}
    facs_data = mesh_data.get("facs_data") or {}
    bone_pose_transforms = facs_data.get("bone_pose_transforms")
    if not bone_pose_transforms:
        return None

    return copy.deepcopy(
        {
            "face_bone_names": list(mesh_data.get("face_bone_names") or bone_pose_transforms.keys()),
            "face_control_names": list(mesh_data.get("face_control_names") or []),
            "facs_pose_names": list(facs_data.get("facs_pose_names") or []),
            "two_pose_correctives": list(facs_data.get("two_pose_correctives") or []),
            "three_pose_correctives": list(facs_data.get("three_pose_correctives") or []),
            "bone_pose_transforms": bone_pose_transforms,
        }
    )


def merge_facs_payloads(payloads) -> Optional[dict]:
    payloads = [facs_payload_from_mesh_data(payload) if payload and "facs_data" in payload else _coerce_facs_payload(payload) for payload in payloads or []]
    payloads = [payload for payload in payloads if payload]
    if not payloads:
        return None

    merged = copy.deepcopy(payloads[0])
    merged["face_bone_names"] = _ordered_unique(merged.get("face_bone_names") or merged.get("bone_pose_transforms", {}).keys())

    for payload in payloads[1:]:
        for field in ("face_control_names", "facs_pose_names", "two_pose_correctives", "three_pose_correctives"):
            current = merged.get(field) or []
            incoming = payload.get(field) or []
            if current and incoming and current != incoming:
                raise ValueError(f"conflicting facs payload field: {field}")
            if not current and incoming:
                merged[field] = copy.deepcopy(incoming)

        merged["face_bone_names"] = _ordered_unique(
            list(merged.get("face_bone_names") or []) + list(payload.get("face_bone_names") or [])
        )
        merged_bones = merged.setdefault("bone_pose_transforms", {})
        for bone_name, transforms in (payload.get("bone_pose_transforms") or {}).items():
            existing = merged_bones.get(bone_name)
            if existing is not None and existing != transforms:
                raise ValueError(f"conflicting facs pose transforms for bone: {bone_name}")
            merged_bones[bone_name] = copy.deepcopy(transforms)

    if not merged.get("face_bone_names"):
        merged["face_bone_names"] = list((merged.get("bone_pose_transforms") or {}).keys())
    return merged


def build_neutral_facs_control_state(control_names) -> dict:
    return {control_name: 0.0 for control_name in control_names or []}


def normalize_facs_control_state(control_names, control_state=None) -> dict:
    state = build_neutral_facs_control_state(control_names)
    if control_state is None:
        return state
    if isinstance(control_state, str):
        control_state = json.loads(control_state)
    if not isinstance(control_state, Mapping):
        raise TypeError("facs control state must be a mapping or json string")

    for control_name in state:
        value = control_state.get(control_name, 0.0)
        try:
            state[control_name] = float(value)
        except (TypeError, ValueError):
            state[control_name] = 0.0
    return state


def compute_facs_state_weights(payload, control_state=None) -> dict:
    payload = _coerce_facs_payload(payload) or {}
    state = normalize_facs_control_state(payload.get("face_control_names") or [], control_state)

    for corrective in payload.get("two_pose_correctives") or []:
        name_a, name_b = corrective.get("control_names") or (None, None)
        if not name_a or not name_b:
            continue
        pose_name = corrective.get("name") or f"x2_{name_a}_{name_b}"
        state[pose_name] = (state.get(name_a, 0.0) or 0.0) * (state.get(name_b, 0.0) or 0.0)

    for corrective in payload.get("three_pose_correctives") or []:
        name_a, name_b, name_c = corrective.get("control_names") or (None, None, None)
        if not name_a or not name_b or not name_c:
            continue
        pose_name = corrective.get("name") or f"x3_{name_a}_{name_b}_{name_c}"
        state[pose_name] = (
            (state.get(name_a, 0.0) or 0.0)
            * (state.get(name_b, 0.0) or 0.0)
            * (state.get(name_c, 0.0) or 0.0)
        )

    return state


def compute_facs_bone_transforms(payload, control_state=None) -> dict:
    payload = _coerce_facs_payload(payload) or {}
    state = compute_facs_state_weights(payload, control_state)
    bone_pose_transforms = payload.get("bone_pose_transforms") or {}
    bone_names = payload.get("face_bone_names") or list(bone_pose_transforms.keys())

    solved = {}
    for bone_name in bone_names:
        pose_transforms = bone_pose_transforms.get(bone_name) or {}
        position = [0.0, 0.0, 0.0]
        rotation = [0.0, 0.0, 0.0]
        for pose_name, weight in state.items():
            if weight <= 0.0:
                continue
            transform = pose_transforms.get(pose_name)
            if not transform:
                continue
            for axis_index, value in enumerate(transform.get("position") or (0.0, 0.0, 0.0)):
                position[axis_index] += float(value) * weight
            for axis_index, value in enumerate(transform.get("rotation") or (0.0, 0.0, 0.0)):
                rotation[axis_index] += float(value) * weight
        solved[bone_name] = {
            "position": tuple(position),
            "rotation": tuple(rotation),
        }
    return solved


def store_facs_payload_on_armature(armature_obj, payload) -> Optional[dict]:
    payload = merge_facs_payloads([payload])
    if not payload or armature_obj is None:
        return None

    armature_obj[FACE_FACS_DATA_PROP] = _compact_json(payload)
    armature_obj[FACE_FACS_CONTROLS_PROP] = _compact_json(
        build_neutral_facs_control_state(payload.get("face_control_names") or [])
    )
    cache_key = _armature_runtime_cache_key(armature_obj)
    _FACS_ARMATURE_RUNTIME_CACHE.pop(cache_key, None)
    _FACS_ACTIVE_ARMATURES.pop(cache_key, None)
    sync_facs_property_group_from_armature(armature_obj, payload=payload)
    return payload


def load_facs_payload_from_armature(armature_obj) -> Optional[dict]:
    runtime = _get_armature_facs_runtime(armature_obj)
    return runtime.get("payload") if runtime else None


def load_facs_control_state_from_armature(armature_obj, payload=None) -> dict:
    payload = merge_facs_payloads([payload]) if payload is not None else load_facs_payload_from_armature(armature_obj)
    if not payload:
        return {}
    try:
        raw_state = armature_obj.get(FACE_FACS_CONTROLS_PROP)
    except Exception:
        raw_state = None
    return normalize_facs_control_state(payload.get("face_control_names") or [], raw_state)


def sync_facs_property_group_from_armature(armature_obj, payload=None, control_state=None) -> bool:
    if armature_obj is None:
        return False

    payload = merge_facs_payloads([payload]) if payload is not None else load_facs_payload_from_armature(armature_obj)
    if not payload:
        return False

    control_holder = getattr(armature_obj, "rbx_face_controls", None)
    if control_holder is None:
        return False

    normalized_state = normalize_facs_control_state(
        payload.get("face_control_names") or [],
        control_state if control_state is not None else load_facs_control_state_from_armature(armature_obj, payload),
    )

    armature_obj[FACE_FACS_UI_SYNC_PROP] = True
    try:
        for control_name, value in normalized_state.items():
            setattr(control_holder, face_control_property_name(control_name), float(value))
    finally:
        try:
            del armature_obj[FACE_FACS_UI_SYNC_PROP]
        except Exception:
            pass

    return True


def apply_facs_snapshot_to_armature(armature_obj, control_state=None, payload=None, apply_token=None) -> dict:
    runtime_entry = _get_armature_facs_runtime(armature_obj, payload=payload)
    if not runtime_entry:
        return {}

    payload = runtime_entry["payload"]
    runtime = runtime_entry["compiled"]

    normalized_state = normalize_facs_control_state(
        runtime.get("control_names") or [],
        control_state if control_state is not None else load_facs_control_state_from_armature(armature_obj, payload),
    )
    state_signature = tuple(normalized_state.get(control_name, 0.0) for control_name in (runtime.get("control_names") or ()))
    return _apply_runtime_solution(
        armature_obj,
        payload,
        runtime_entry,
        state_signature,
        persist_state=True,
        apply_token=apply_token,
    )


def apply_facs_properties_to_armature(
    armature_obj,
    payload=None,
    persist_state: bool = False,
    apply_token=None,
) -> dict:
    runtime_entry = _get_armature_facs_runtime(armature_obj, payload=payload)
    if not runtime_entry:
        return {}

    control_holder = getattr(armature_obj, "rbx_face_controls", None)
    if control_holder is None:
        return {}

    runtime = runtime_entry["compiled"]
    state_signature = _state_signature_from_holder(control_holder, runtime)
    return _apply_runtime_solution(
        armature_obj,
        runtime_entry["payload"],
        runtime_entry,
        state_signature,
        persist_state=persist_state,
        apply_token=apply_token,
    )


def iter_active_facs_armatures():
    stale_keys = []
    active_armatures = []
    for cache_key, armature_obj in list(_FACS_ACTIVE_ARMATURES.items()):
        try:
            if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
                stale_keys.append(cache_key)
                continue
            if not armature_obj.get(FACE_FACS_DATA_PROP):
                stale_keys.append(cache_key)
                continue
        except Exception:
            stale_keys.append(cache_key)
            continue
        active_armatures.append(armature_obj)

    for cache_key in stale_keys:
        _FACS_ACTIVE_ARMATURES.pop(cache_key, None)
        _FACS_ARMATURE_RUNTIME_CACHE.pop(cache_key, None)

    return active_armatures


def is_face_control_bone(bone_like) -> bool:
    data_bone = getattr(bone_like, "bone", bone_like)
    try:
        if data_bone.get(FACE_CONTROL_HELPER_PROP):
            return True
        if data_bone.get(FACE_CONTROL_PROP_NAME):
            return True
    except Exception:
        pass

    bone_name = getattr(data_bone, "name", "")
    return isinstance(bone_name, str) and bone_name.startswith(FACE_CONTROL_BONE_PREFIX)


def is_face_deform_bone(bone_like) -> bool:
    data_bone = getattr(bone_like, "bone", bone_like)
    try:
        return bool(data_bone.get(FACE_DEFORM_BONE_PROP))
    except Exception:
        return False