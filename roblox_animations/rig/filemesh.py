"""Utilities for fetching and parsing Roblox FileMesh skinning data."""

from __future__ import annotations

import ctypes
import io
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import importlib
import json
import os
from pathlib import Path
import random
import re
import struct
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple


_FILEMESH_CACHE: Dict[str, dict] = {}
_FILEMESH_BYTES_CACHE: Dict[str, bytes] = {}
_FILEMESH_PREFETCH_FAILURES: Dict[str, str] = {}
_FILEMESH_FETCH_GUARD = threading.Lock()
_FILEMESH_FETCH_LOCKS: Dict[str, threading.Lock] = {}
_HTTP_POOLING_ENABLED = False
_HTTP_SESSION_LOCAL = threading.local()
_REQUESTS_MODULE = None


def enable_http_pooling() -> bool:
    """Enable optional keep-alive sessions for high-volume place imports."""
    global _HTTP_POOLING_ENABLED, _REQUESTS_MODULE
    if _HTTP_POOLING_ENABLED:
        return True
    try:
        import requests  # Blender bundles this; urllib remains the fallback.
    except ImportError:
        return False
    _REQUESTS_MODULE = requests
    _HTTP_POOLING_ENABLED = True
    return True


def _pooled_http_session():
    session = getattr(_HTTP_SESSION_LOCAL, "session", None)
    if session is None:
        session = _REQUESTS_MODULE.Session()
        adapter = _REQUESTS_MODULE.adapters.HTTPAdapter(
            pool_connections=8,
            pool_maxsize=8,
            max_retries=0,
            pool_block=False,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _HTTP_SESSION_LOCAL.session = session
    return session


def _fetch_pooled_response(url, headers, timeout, follow_redirects, max_bytes):
    """Requests-backed equivalent of the urllib response helper."""
    try:
        response = _pooled_http_session().get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=follow_redirects,
            stream=True,
        )
    except _REQUESTS_MODULE.exceptions.RequestException as exc:
        raise urllib.error.URLError(str(exc)) from exc
    declared_length = response.headers.get("Content-Length")
    if declared_length and int(declared_length) > max_bytes:
        response.close()
        raise ValueError("asset response exceeds import safety limit")
    chunks = []
    received = 0
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        received += len(chunk)
        if received > max_bytes:
            response.close()
            raise ValueError("asset response exceeds import safety limit")
        chunks.append(chunk)
    data = b"".join(chunks)
    status_code = response.status_code
    reason = response.reason
    headers_copy = dict(response.headers)
    # Return the connection to the pool immediately: the old code leaked
    # every successful response, exhausting the 8-slot adapter and forcing
    # a fresh TLS handshake per request once the pool filled.
    response.close()
    if status_code >= 400:
        error = urllib.error.HTTPError(
            url,
            status_code,
            reason,
            headers_copy,
            io.BytesIO(data),
        )
        raise error

    class _PooledResponse:
        """Minimal response stand-in; callers only read headers/status."""

        def __init__(self):
            self.headers = headers_copy
            self.status_code = status_code
            self.reason = reason

    return _PooledResponse(), data


def _filemesh_asset_key(content_id) -> str:
    asset_id = extract_asset_id(content_id)
    return f"asset:{asset_id}" if asset_id is not None else str(content_id)


def _filemesh_fetch_lock(content_id) -> threading.Lock:
    key = _filemesh_asset_key(content_id)
    with _FILEMESH_FETCH_GUARD:
        return _FILEMESH_FETCH_LOCKS.setdefault(key, threading.Lock())


def release_import_cache() -> None:
    """Release decoded/raw mesh data retained only to speed up one import.

    The Blender meshes already created from this data own their geometry, so
    retaining the Python source buffers after an import only inflates the
    Blender process for subsequent, unrelated imports.
    """
    _FILEMESH_CACHE.clear()
    _FILEMESH_BYTES_CACHE.clear()
    _FILEMESH_PREFETCH_FAILURES.clear()
    with _FILEMESH_FETCH_GUARD:
        _FILEMESH_FETCH_LOCKS.clear()


_HTTP_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_HTTP_MAX_RETRIES = 3
_HTTP_BASE_RETRY_DELAY_SECONDS = 0.5
_HTTP_MAX_RETRY_DELAY_SECONDS = 8.0
# generous enough for production assets, bounded enough to reject bombs.
_MAX_ASSET_BYTES = 256 * 1024 * 1024
_MAX_DRACO_VERTICES = 2_000_000
_MAX_DRACO_INDICES = 6_000_000

_BONE_STRUCT = struct.Struct("<IHHf9f3f")
_SUBSET_STRUCT = struct.Struct("<IIIII26H")
_FILEMESH_FACS_HEADER_STRUCT = struct.Struct("<IIQII")
_QUANTIZED_MATRIX_HEADER_STRUCT = struct.Struct("<HII")
_TWO_POSE_CORRECTIVE_STRUCT = struct.Struct("<HH")
_THREE_POSE_CORRECTIVE_STRUCT = struct.Struct("<HHH")
_FACE_STRUCT = struct.Struct("<III")
_SKINNING_STRUCT = struct.Struct("<4B4B")
_FACS_TRANSFORM_CHANNELS = ("px", "py", "pz", "rx", "ry", "rz")
_GLTF_COMPONENT_TYPE_UINT8 = 5121
_GLTF_COMPONENT_TYPE_UINT32 = 5125
_GLTF_COMPONENT_TYPE_FLOAT32 = 5126
_DRACO_DLL_UNINITIALIZED = object()
_DRACO_DLL = _DRACO_DLL_UNINITIALIZED
_DRACO_LOAD_ERROR = None

_FILEMESH_FACS_CONTROL_MAP = {
    "c_COR": "Corrugator",
    "c_CR": "ChinRaiser",
    "c_CRUL": "ChinRaiserUpperLip",
    "c_ELD": "EyesLookDown",
    "c_ELL": "EyesLookLeft",
    "c_ELR": "EyesLookRight",
    "c_ELU": "EyesLookUp",
    "c_FN": "Funneler",
    "c_FP": "FlatPucker",
    "c_JD": "JawDrop",
    "c_JL": "JawLeft",
    "c_JR": "JawRight",
    "c_LLS": "LowerLipSuck",
    "c_LP": "LipPresser",
    "c_LPT": "LipsTogether",
    "c_ML": "MouthLeft",
    "c_MR": "MouthRight",
    "c_PK": "Pucker",
    "c_TD": "TongueDown",
    "c_TO": "TongueOut",
    "c_TU": "TongueUp",
    "c_ULS": "UpperLipSuck",
    "l_BL": "LeftBrowLowerer",
    "l_CHP": "LeftCheekPuff",
    "l_CHR": "LeftCheekRaiser",
    "l_DM": "LeftDimpler",
    "l_EC": "LeftEyeClosed",
    "l_EULR": "LeftEyeUpperLidRaiser",
    "l_IBR": "LeftInnerBrowRaiser",
    "l_LCD": "LeftLipCornerDown",
    "l_LCP": "LeftLipCornerPuller",
    "l_LLD": "LeftLowerLipDepressor",
    "l_LS": "LeftLipStretcher",
    "l_NW": "LeftNoseWrinkler",
    "l_OBR": "LeftOuterBrowRaiser",
    "l_ULR": "LeftUpperLipRaiser",
    "r_BL": "RightBrowLowerer",
    "r_CHP": "RightCheekPuff",
    "r_CHR": "RightCheekRaiser",
    "r_DM": "RightDimpler",
    "r_EC": "RightEyeClosed",
    "r_EULR": "RightEyeUpperLidRaiser",
    "r_IBR": "RightInnerBrowRaiser",
    "r_LCD": "RightLipCornerDown",
    "r_LCP": "RightLipCornerPuller",
    "r_LLD": "RightLowerLipDepressor",
    "r_LS": "RightLipStretcher",
    "r_NW": "RightNoseWrinkler",
    "r_OBR": "RightOuterBrowRaiser",
    "r_ULR": "RightUpperLipRaiser",
}


def _empty_facs_metadata() -> dict:
    return {
        "has_facs": False,
        "face_bone_names": [],
        "face_control_names": [],
        "face_control_abbreviations": [],
        "facs_data": None,
    }


def _unsupported_facs_metadata(raw_size: int, facs_format: int, message: str) -> dict:
    metadata = _empty_facs_metadata()
    metadata["facs_data"] = {
        "raw_size": raw_size,
        "format": facs_format,
        "parse_error": message,
    }
    return metadata


def _split_null_terminated_names(blob: bytes) -> List[str]:
    if not blob:
        return []
    return [
        chunk.decode("utf-8", errors="replace")
        for chunk in blob.split(b"\0")
        if chunk
    ]


def _parse_corrective_pairs(blob: bytes) -> List[Tuple[int, int]]:
    if len(blob) % _TWO_POSE_CORRECTIVE_STRUCT.size != 0:
        raise ValueError("invalid two-pose corrective payload size")
    return list(_TWO_POSE_CORRECTIVE_STRUCT.iter_unpack(blob))


def _parse_corrective_triples(blob: bytes) -> List[Tuple[int, int, int]]:
    if len(blob) % _THREE_POSE_CORRECTIVE_STRUCT.size != 0:
        raise ValueError("invalid three-pose corrective payload size")
    return list(_THREE_POSE_CORRECTIVE_STRUCT.iter_unpack(blob))


def _expand_corrective_pose_names(
    control_names: List[str],
    two_pose_pairs: List[Tuple[int, int]],
    three_pose_triples: List[Tuple[int, int, int]],
) -> Tuple[List[str], List[dict], List[dict]]:
    pose_names = list(control_names)
    two_pose_correctives = []
    three_pose_correctives = []

    def resolve_name(index: int) -> str:
        if not (0 <= index < len(pose_names)):
            raise ValueError(f"corrective control index {index} out of range")
        return pose_names[index]

    for control_index0, control_index1 in two_pose_pairs:
        control_name0 = resolve_name(control_index0)
        control_name1 = resolve_name(control_index1)
        corrective_name = f"x2_{control_name0}_{control_name1}"
        two_pose_correctives.append(
            {
                "name": corrective_name,
                "control_indices": (control_index0, control_index1),
                "control_names": (control_name0, control_name1),
            }
        )
        pose_names.append(corrective_name)

    for control_index0, control_index1, control_index2 in three_pose_triples:
        control_name0 = resolve_name(control_index0)
        control_name1 = resolve_name(control_index1)
        control_name2 = resolve_name(control_index2)
        corrective_name = f"x3_{control_name0}_{control_name1}_{control_name2}"
        three_pose_correctives.append(
            {
                "name": corrective_name,
                "control_indices": (control_index0, control_index1, control_index2),
                "control_names": (control_name0, control_name1, control_name2),
            }
        )
        pose_names.append(corrective_name)

    return pose_names, two_pose_correctives, three_pose_correctives


def _parse_quantized_matrix(blob: bytes, offset: int) -> Tuple[dict, int]:
    if offset + _QUANTIZED_MATRIX_HEADER_STRUCT.size > len(blob):
        raise ValueError("truncated quantized matrix header")

    version, rows, cols = _QUANTIZED_MATRIX_HEADER_STRUCT.unpack_from(blob, offset)
    offset += _QUANTIZED_MATRIX_HEADER_STRUCT.size
    value_count = rows * cols

    min_value = None
    max_value = None
    raw_values = None

    if version == 1:
        byte_count = value_count * 4
        if offset + byte_count > len(blob):
            raise ValueError("truncated quantized matrix v1 payload")
        flat_values = struct.unpack_from(f"<{value_count}f", blob, offset)
        offset += byte_count
    elif version == 2:
        if offset + 8 > len(blob):
            raise ValueError("truncated quantized matrix v2 bounds")
        min_value, max_value = struct.unpack_from("<ff", blob, offset)
        offset += 8
        byte_count = value_count * 2
        if offset + byte_count > len(blob):
            raise ValueError("truncated quantized matrix v2 payload")
        raw_values = struct.unpack_from(f"<{value_count}H", blob, offset)
        offset += byte_count

        if value_count == 0:
            flat_values = ()
        elif max_value == min_value:
            flat_values = [float(min_value)] * value_count
        else:
            precision = (max_value - min_value) / 65535.0
            flat_values = [
                float(min_value + (quantized_value * precision))
                for quantized_value in raw_values
            ]
    else:
        raise ValueError(f"unsupported quantized matrix version {version}")

    values = [list(flat_values[row_offset: row_offset + cols]) for row_offset in range(0, value_count, cols)]
    return (
        {
            "version": version,
            "rows": rows,
            "cols": cols,
            "min_value": min_value,
            "max_value": max_value,
            "raw_values": list(raw_values) if raw_values is not None else None,
            "values": values,
        },
        offset,
    )


def _parse_quantized_transforms(
    blob: bytes,
    expected_rows: Optional[int] = None,
    expected_cols: Optional[int] = None,
) -> dict:
    offset = 0
    matrices = {}
    rows = None
    cols = None

    for channel in _FACS_TRANSFORM_CHANNELS:
        matrix, offset = _parse_quantized_matrix(blob, offset)
        matrix_rows = matrix["rows"]
        matrix_cols = matrix["cols"]
        if rows is None:
            rows = matrix_rows
            cols = matrix_cols
        elif matrix_rows != rows or matrix_cols != cols:
            raise ValueError("mismatched quantized transform matrix dimensions")
        matrices[channel] = matrix

    if offset != len(blob):
        raise ValueError("unexpected trailing bytes in quantized transforms payload")

    if expected_rows is not None and rows != expected_rows:
        raise ValueError(
            f"quantized transform row count {rows} did not match face bone count {expected_rows}"
        )
    if expected_cols is not None and cols != expected_cols:
        raise ValueError(
            f"quantized transform column count {cols} did not match pose count {expected_cols}"
        )

    return {
        "rows": rows or 0,
        "cols": cols or 0,
        "channels": matrices,
    }


def _build_facs_bone_pose_transforms(
    face_bone_names: List[str],
    pose_names: List[str],
    quantized_transforms: dict,
) -> dict:
    bone_pose_transforms = {}
    channel_values = quantized_transforms["channels"]
    px_rows = channel_values["px"]["values"]
    py_rows = channel_values["py"]["values"]
    pz_rows = channel_values["pz"]["values"]
    rx_rows = channel_values["rx"]["values"]
    ry_rows = channel_values["ry"]["values"]
    rz_rows = channel_values["rz"]["values"]

    for bone_index, bone_name in enumerate(face_bone_names):
        px_row = px_rows[bone_index]
        py_row = py_rows[bone_index]
        pz_row = pz_rows[bone_index]
        rx_row = rx_rows[bone_index]
        ry_row = ry_rows[bone_index]
        rz_row = rz_rows[bone_index]
        pose_transforms = {}
        for pose_name, px, py, pz, rx, ry, rz in zip(
            pose_names,
            px_row,
            py_row,
            pz_row,
            rx_row,
            ry_row,
            rz_row,
        ):
            pose_transforms[pose_name] = {
                "position": (px, py, pz),
                "rotation": (rx, ry, rz),
            }
        bone_pose_transforms[bone_name] = pose_transforms

    return bone_pose_transforms


def _parse_facs_data(blob: bytes) -> dict:
    metadata = _empty_facs_metadata()
    if not blob:
        return metadata

    if len(blob) < _FILEMESH_FACS_HEADER_STRUCT.size:
        metadata["facs_data"] = {
            "raw_size": len(blob),
            "parse_error": "truncated facs header",
        }
        return metadata

    (
        face_bone_names_size,
        face_control_names_size,
        quantized_transforms_size,
        two_pose_correctives_size,
        three_pose_correctives_size,
    ) = _FILEMESH_FACS_HEADER_STRUCT.unpack_from(blob, 0)

    total_size = (
        _FILEMESH_FACS_HEADER_STRUCT.size
        + face_bone_names_size
        + face_control_names_size
        + quantized_transforms_size
        + two_pose_correctives_size
        + three_pose_correctives_size
    )
    if total_size > len(blob):
        metadata["facs_data"] = {
            "raw_size": len(blob),
            "parse_error": "truncated facs payload",
        }
        return metadata

    offset = _FILEMESH_FACS_HEADER_STRUCT.size
    face_bone_names_blob = blob[offset: offset + face_bone_names_size]
    offset += face_bone_names_size
    face_control_names_blob = blob[offset: offset + face_control_names_size]
    offset += face_control_names_size
    quantized_transforms_blob = blob[offset: offset + quantized_transforms_size]
    offset += quantized_transforms_size
    two_pose_correctives_blob = blob[offset: offset + two_pose_correctives_size]
    offset += two_pose_correctives_size
    three_pose_correctives_blob = blob[offset: offset + three_pose_correctives_size]

    control_abbreviations = _split_null_terminated_names(face_control_names_blob)
    control_names = [
        _FILEMESH_FACS_CONTROL_MAP.get(name, name)
        for name in control_abbreviations
    ]
    face_bone_names = _split_null_terminated_names(face_bone_names_blob)

    facs_data = {
        "face_bone_names_size": face_bone_names_size,
        "face_control_names_size": face_control_names_size,
        "quantized_transforms_size": quantized_transforms_size,
        "two_pose_correctives_size": two_pose_correctives_size,
        "three_pose_correctives_size": three_pose_correctives_size,
        "quantized_transforms_blob": quantized_transforms_blob,
        "two_pose_correctives_blob": two_pose_correctives_blob,
        "three_pose_correctives_blob": three_pose_correctives_blob,
    }

    try:
        two_pose_pairs = _parse_corrective_pairs(two_pose_correctives_blob)
        three_pose_triples = _parse_corrective_triples(three_pose_correctives_blob)
        facs_pose_names, two_pose_correctives, three_pose_correctives = _expand_corrective_pose_names(
            control_names,
            two_pose_pairs,
            three_pose_triples,
        )
        quantized_transforms = _parse_quantized_transforms(
            quantized_transforms_blob,
            expected_rows=len(face_bone_names),
            expected_cols=len(facs_pose_names),
        )
        bone_pose_transforms = _build_facs_bone_pose_transforms(
            face_bone_names,
            facs_pose_names,
            quantized_transforms,
        )
        facs_data.update(
            {
                "quantized_transforms": quantized_transforms,
                "two_pose_correctives": two_pose_correctives,
                "three_pose_correctives": three_pose_correctives,
                "facs_pose_names": facs_pose_names,
                "bone_pose_transforms": bone_pose_transforms,
            }
        )
    except ValueError as exc:
        facs_data["parse_error"] = str(exc)

    metadata.update(
        {
            "has_facs": bool(face_bone_names or control_names),
            "face_bone_names": face_bone_names,
            "face_control_names": control_names,
            "face_control_abbreviations": control_abbreviations,
            "facs_data": facs_data,
        }
    )
    return metadata


def _parse_facs_chunk(chunk: bytes) -> dict:
    if len(chunk) < 4:
        metadata = _empty_facs_metadata()
        metadata["facs_data"] = {
            "raw_size": len(chunk),
            "parse_error": "truncated facs chunk header",
        }
        return metadata

    facs_data_size = struct.unpack_from("<I", chunk, 0)[0]
    if 4 + facs_data_size > len(chunk):
        metadata = _empty_facs_metadata()
        metadata["facs_data"] = {
            "raw_size": len(chunk),
            "parse_error": "truncated facs chunk payload",
        }
        return metadata

    return _parse_facs_data(chunk[4: 4 + facs_data_size])


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, headers):
        return fp

    def http_error_302(self, req, fp, code, msg, headers):
        return fp

    def http_error_303(self, req, fp, code, msg, headers):
        return fp

    def http_error_307(self, req, fp, code, msg, headers):
        return fp

    def http_error_308(self, req, fp, code, msg, headers):
        return fp


def extract_asset_id(content_id) -> Optional[int]:
    """Best-effort extraction of a Roblox asset id from common content id formats."""
    if content_id is None:
        return None

    if isinstance(content_id, int):
        return content_id

    text = str(content_id).strip()
    if not text:
        return None

    if text.isdigit():
        return int(text)

    patterns = [
        r"rbxassetid://(\d+)",
        r"[?&]id=(\d+)",
        r"/asset/\?id=(\d+)",
        r"/asset/\?ID=(\d+)",
        r"/library/(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _looks_like_local_mesh_path(text: str) -> bool:
    """True for absolute filesystem paths (windows drive, unc, or posix)."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", stripped)
        or stripped.startswith("\\\\")
        or stripped.startswith("/")
        or stripped.lower().startswith("file://")
    )


def _resolve_local_mesh_path(text: str) -> Optional[Path]:
    """Resolve a file:// url or absolute path to an existing local file."""
    stripped = (text or "").strip()
    if stripped.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(stripped)
        try:
            path = urllib.request.url2pathname(parsed.path)
        except Exception:
            path = parsed.path
        candidate = Path(path)
    else:
        candidate = Path(stripped)
    candidate = candidate.expanduser()
    return candidate if candidate.is_file() else None


def _preview_bytes(data: bytes, limit: int = 64) -> str:
    snippet = data[:limit]
    text = snippet.decode("ascii", errors="replace")
    return text.replace("\r", "\\r").replace("\n", "\\n")


_RBXASSET_PREFIX = "rbxasset://"
_TRUSTED_ASSET_HOST_SUFFIXES = ("roblox.com", "rbxcdn.com")


def is_trusted_roblox_asset_url(url: str) -> bool:
    """Whether an external content URL is a Roblox-owned asset host.

    Old .rbxm files save ``http://www.roblox.com/asset/?id=...``, so plain
    http is accepted for Roblox-owned hosts (the host list stays strict).
    """
    parsed = urllib.parse.urlparse(str(url).strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme in ("https", "http")
        and bool(host)
        and any(host == suffix or host.endswith(f".{suffix}") for suffix in _TRUSTED_ASSET_HOST_SUFFIXES)
    )


# Test/headless hook: when bpy is unavailable (or for tests), this override
# is consulted instead of the addon preference. Set to a str/Path or None.
_CONTENT_PATH_OVERRIDE = None


def _user_content_dirs() -> List[Path]:
    """Content dirs derived from the addon preference (or test override).

    Accepts the preference pointing at the ``content`` dir itself, a single
    version dir, or a ``Versions`` parent folder.
    """
    raw = _CONTENT_PATH_OVERRIDE
    if raw is None:
        try:
            import bpy  # noqa: PLC0415

            # The addon's root module name varies by install method (plain
            # addon dir vs bl_ext.<repo>.<name> extension), so walk the
            # package hierarchy from most-specific to least until one of
            # them is a registered addon exposing the preference.
            package = (__package__ or "").split(".")
            raw = ""
            for end in range(len(package), 0, -1):
                addon = bpy.context.preferences.addons.get(".".join(package[:end]))
                prefs = getattr(addon, "preferences", None) if addon else None
                value = getattr(prefs, "roblox_content_path", None)
                if value is not None:
                    raw = value
                    break
        except Exception:
            raw = ""
    raw = str(raw or "").strip()
    if not raw:
        return []

    base = Path(raw).expanduser()
    out: List[Path] = []
    if (base / "avatar").is_dir() or (base / "fonts").is_dir():
        out.append(base)  # already the content dir
    if (base / "content").is_dir():
        out.append(base / "content")  # version dir or install root
    if base.is_dir():
        for child in _sorted_version_dirs(base):
            if child.is_dir() and (child / "content").is_dir():
                out.append(child / "content")  # Versions parent
    return out


def _sorted_version_dirs(versions: Path) -> List[Path]:
    """Version dirs under a Versions parent, newest install first.

    Studio version dirs are hash-named, so lexicographic order is
    meaningless; install time is the only sane recency signal.
    """
    try:
        entries = list(versions.iterdir())
    except OSError:
        return []

    def key(path: Path):
        try:
            return (path.stat().st_mtime, path.name)
        except OSError:
            return (0.0, path.name)

    return sorted(entries, key=key, reverse=True)


def _append_version_contents(candidates: List[Path], versions_parents) -> None:
    """Append <parent>/<version>/content for every version dir (newest first)."""
    for versions in versions_parents:
        for version_dir in _sorted_version_dirs(versions):
            candidates.append(version_dir / "content")


def _install_content_dirs(platform: str, home: Path, environ) -> List[Path]:
    """Install-derived content candidates for a platform (testable core)."""
    candidates: List[Path] = []
    if platform == "win32":
        # Respect redirected profile dirs instead of assuming C:.
        local_app_data = environ.get("LOCALAPPDATA")
        local = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        _append_version_contents(candidates, (local / "Roblox" / "Versions",))
        candidates.append(local / "Roblox" / "content")
        program_files = [
            p
            for p in (
                environ.get("ProgramFiles(x86)"),
                environ.get("ProgramFiles"),
            )
            if p
        ]
        _append_version_contents(
            candidates,
            (Path(p) / "Roblox" / "Versions" for p in program_files),
        )
    elif platform == "darwin":
        candidates.append(
            Path("/Applications/RobloxStudio.app/Contents/Resources/content")
        )
        candidates.append(
            home / "Applications" / "RobloxStudio.app" / "Contents" / "Resources" / "content"
        )
        # Vinegar on macOS keeps downloaded versions in the app-support dir.
        _append_version_contents(
            candidates,
            (home / "Library" / "Application Support" / "Vinegar" / "Versions",),
        )
    else:  # linux (vinegar, grapejuice, or a bare wine prefix)
        _append_version_contents(
            candidates,
            (
                home / ".vinegar" / "data" / "vinegar" / "versions",
                home / ".var" / "app" / "org.vinegarhq.Vinegar" / "data" / "vinegar" / "versions",
            ),
        )
        # Wine keeps a Windows-style install inside each prefix, so every
        # prefix contributes drive_c/users/<user>/AppData/Local/Roblox/Versions.
        prefix_roots = (
            home / ".local" / "share" / "grapejuice" / "prefixes",
            home / ".var" / "app" / "net.brinkervii.grapejuice" / "data" / "grapejuice" / "prefixes",
        )
        drive_c_roots: List[Path] = [home / ".wine" / "drive_c"]
        for prefix_root in prefix_roots:
            try:
                for prefix in prefix_root.iterdir():
                    drive_c_roots.append(prefix / "drive_c")
            except OSError:
                continue
        for drive_c in drive_c_roots:
            try:
                users = drive_c / "users"
                if not users.is_dir():
                    continue
                for user_dir in users.iterdir():
                    _append_version_contents(
                        candidates,
                        (
                            user_dir / "AppData" / "Local" / "Roblox" / "Versions",
                        ),
                    )
            except OSError:
                continue
    return candidates


def _roblox_content_dirs() -> List[Path]:
    """Candidate directories that hold Roblox's builtin content (fonts, etc.)."""
    return list(_user_content_dirs()) + _install_content_dirs(
        sys.platform, Path.home(), os.environ
    )


def detect_roblox_content_dir() -> Optional[str]:
    """Best existing Roblox content directory, derived from the install.

    Consults the addon preference first, then standard Studio install
    locations (newest version wins).  Returns a str path or None when no
    install is found.  This is what the preferences Auto-Detect button and
    the rbxasset resolver both use, so "leave the field empty" and an
    explicit detection are the same code path.
    """
    for candidate in _roblox_content_dirs():
        if candidate.is_dir() and (
            (candidate / "fonts").is_dir() or (candidate / "avatar").is_dir()
        ):
            return str(candidate.resolve())
    return None


def _bundled_asset_dir() -> Optional[Path]:
    """The addon's bundled asset directory (mirrors rbxasset:// paths)."""
    try:
        asset_dir = Path(__file__).resolve().parent.parent / "assets"
        return asset_dir if asset_dir.is_dir() else None
    except Exception:
        return None


def _resolve_bundled_rbxasset(relative: str) -> Optional[Path]:
    """Resolve an rbxasset-relative path inside the bundled asset directory."""
    asset_dir = _bundled_asset_dir()
    if asset_dir is None:
        return None
    try:
        asset_root = asset_dir.resolve()
        candidate = (asset_dir / relative.replace("/", os.sep)).resolve()
        candidate.relative_to(asset_root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _resolve_rbxasset_path(content_id: str) -> Optional[Path]:
    """Map an rbxasset:// builtin uri to a local file path.

    The copy bundled with the addon wins (deterministic, no install
    required); a local Roblox install is the fallback.

    Legacy uris like ``rbxasset://fonts/head.mesh`` no longer live under
    ``content/fonts`` in modern installs (they moved to ``content/avatar/...``),
    so after the literal path fails we fall back to a basename search of the
    avatar content tree.
    """
    text = str(content_id).strip()
    if not text.lower().startswith(_RBXASSET_PREFIX):
        return None
    relative = text[len(_RBXASSET_PREFIX):].lstrip("/").replace("/", os.sep)
    bundled = _resolve_bundled_rbxasset(relative)
    if bundled is not None:
        return bundled
    basename = os.path.basename(relative)
    for content_dir in _roblox_content_dirs():
        try:
            root = content_dir.resolve()
            candidate = (root / relative).resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            continue
        if candidate.is_file():
            return candidate
        # Fallback: search avatar subtrees for the basename.
        if basename:
            avatar_root = content_dir / "avatar"
            if avatar_root.is_dir():
                for found in avatar_root.rglob(basename):
                    if found.is_file():
                        return found
    return None


def _normalize_filemesh_bytes(data: bytes, max_bytes: int = _MAX_ASSET_BYTES) -> bytes:
    if len(data) > max_bytes:
        raise ValueError("asset exceeds import safety limit")
    if data.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("decompressed asset exceeds import safety limit")
        except OSError:
            pass

    version_index = data.find(b"version ")
    if 0 < version_index < 4096:
        data = data[version_index:]

    return data


def _looks_like_filemesh_payload(data: bytes) -> bool:
    data = _normalize_filemesh_bytes(data)
    return data.startswith(b"version ")


def _is_online_access_allowed() -> bool:
    try:
        import bpy  # noqa: PLC0415

        return bool(getattr(bpy.app, "online_access", True))
    except Exception:
        return True


def _require_online_access(action: str) -> None:
    if not _is_online_access_allowed():
        raise RuntimeError(
            f"Blender online access is disabled. Enable Online Access to {action}."
        )


def _retry_after_delay_seconds(headers) -> Optional[float]:
    if not headers:
        return None

    value = headers.get("Retry-After")
    if not value:
        return None

    value = str(value).strip()
    try:
        return max(0.0, min(float(value), _HTTP_MAX_RETRY_DELAY_SECONDS))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(delay, _HTTP_MAX_RETRY_DELAY_SECONDS))


def _http_retry_delay_seconds(headers, attempt_index: int) -> float:
    retry_after = _retry_after_delay_seconds(headers)
    if retry_after is not None:
        return retry_after

    exponential = min(
        _HTTP_BASE_RETRY_DELAY_SECONDS * (2 ** attempt_index),
        _HTTP_MAX_RETRY_DELAY_SECONDS,
    )
    jitter = random.uniform(0.0, min(0.25, exponential * 0.25))
    return min(exponential + jitter, _HTTP_MAX_RETRY_DELAY_SECONDS)


def _fetch_url_response(
    url: str,
    timeout: float = 15.0,
    follow_redirects: bool = True,
    extra_headers: Optional[Dict[str, str]] = None,
    max_bytes: int = _MAX_ASSET_BYTES,
):
    _require_online_access("fetch Roblox mesh data")
    headers: Dict[str, str] = {
        "User-Agent": "RobloxStudio/WinInet",
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    }
    if extra_headers:
        headers.update(extra_headers)
    if _HTTP_POOLING_ENABLED:
        for attempt_index in range(_HTTP_MAX_RETRIES + 1):
            try:
                return _fetch_pooled_response(url, headers, timeout, follow_redirects, max_bytes)
            except urllib.error.HTTPError as exc:
                can_retry = (
                    exc.code in _HTTP_RETRY_STATUS_CODES
                    and attempt_index < _HTTP_MAX_RETRIES
                )
                if not can_retry:
                    raise
                delay = _http_retry_delay_seconds(exc.headers, attempt_index)
                exc.close()
                time.sleep(delay)
    request = urllib.request.Request(url, headers=headers)
    opener = (
        urllib.request.build_opener()
        if follow_redirects
        else urllib.request.build_opener(_NoRedirectHandler())
    )
    for attempt_index in range(_HTTP_MAX_RETRIES + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                declared_length = response.headers.get("Content-Length")
                if declared_length and int(declared_length) > max_bytes:
                    raise ValueError("asset response exceeds import safety limit")
                try:
                    data = response.read(max_bytes + 1)
                except TypeError:  # lightweight test/Blender response shims
                    data = response.read()
                if len(data) > max_bytes:
                    raise ValueError("asset response exceeds import safety limit")
                encoding = (response.headers.get("Content-Encoding") or "").lower()
                if "gzip" in encoding or data.startswith(b"\x1f\x8b"):
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                            data = stream.read(max_bytes + 1)
                        if len(data) > max_bytes:
                            raise ValueError("decompressed asset exceeds import safety limit")
                    except OSError:
                        pass
                return response, data
        except urllib.error.HTTPError as exc:
            can_retry = (
                exc.code in _HTTP_RETRY_STATUS_CODES
                and attempt_index < _HTTP_MAX_RETRIES
            )
            if not can_retry:
                raise
            delay = _http_retry_delay_seconds(exc.headers, attempt_index)
            try:
                exc.close()
            except Exception:
                pass
            time.sleep(delay)

    raise RuntimeError(f"failed to fetch url after retries: {url}")


def _fetch_url_bytes(
    url: str,
    timeout: float = 15.0,
    extra_headers: Optional[Dict[str, str]] = None,
    max_bytes: int = _MAX_ASSET_BYTES,
) -> bytes:
    # Never let urllib carry a bearer token across an Open Cloud redirect.  The
    # delivery service returns a signed CDN location; that second request must
    # be anonymous, both because it needs no OAuth and because forwarding the
    # token to another host would be a credential leak.  requests strips auth
    # on cross-host redirects itself, so the pooled path follows in one hop.
    has_bearer = bool((extra_headers or {}).get("Authorization"))
    response, data = _fetch_url_response(
        url,
        timeout=timeout,
        follow_redirects=(not has_bearer or _HTTP_POOLING_ENABLED),
        extra_headers=extra_headers,
        max_bytes=max_bytes,
    )
    if has_bearer:
        location = response.headers.get("Location")
        locations = [location] if location else _extract_locations_from_payload(data)
        if locations:
            return _fetch_url_bytes(locations[0], timeout=timeout, max_bytes=max_bytes)
    return _normalize_filemesh_bytes(data, max_bytes=max_bytes)


def _extract_locations_from_payload(payload: bytes) -> List[str]:
    try:
        metadata = json.loads(payload.decode("utf-8"))
    except Exception:
        return []

    result = []

    # Flat singular key: {"location": "https://..."}  (asset-delivery-api v1)
    single = metadata.get("location")
    if isinstance(single, str) and single:
        result.append(single)

    # Nested array: {"locations": [{"location": "https://..."}]}  (v2 / legacy)
    for loc in metadata.get("locations") or []:
        url = loc.get("location") if isinstance(loc, dict) else None
        if url:
            result.append(url)

    return result


def _uses_opencloud_auth(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host == "apis.roblox.com"


def _describe_auth_mode(headers: Optional[Dict[str, str]]) -> str:
    if not headers:
        return "none"
    if headers.get("Authorization"):
        return "oauth-bearer"
    return "other"


def _get_auth_headers() -> Dict[str, str]:
    """Returns OAuth bearer headers if the user is logged in, else {}."""
    try:
        from ..core.auth import get_auth_headers  # noqa: PLC0415

        return get_auth_headers()
    except Exception:
        return {}


def _try_delivery_urls(
    delivery_urls: List[str],
    asset_id: int,
    timeout: float,
    errors: List[str],
    auth_headers: Optional[Dict[str, str]] = None,
) -> Optional[bytes]:
    """
    Attempt to fetch filemesh bytes from each delivery URL in order.
    Returns bytes on the first success, None if all fail.
    Auth headers are passed to the delivery endpoint only (not CDN redirect targets).
    """
    for delivery_url in delivery_urls:
        request_headers = auth_headers if _uses_opencloud_auth(delivery_url) else None
        try:
            response, delivery_payload = _fetch_url_response(
                delivery_url,
                timeout=timeout,
                follow_redirects=False,
                extra_headers=request_headers,
            )

            location_urls = []
            location = response.headers.get("Location")
            if location:
                location_urls.append(location)
            else:
                location_urls.extend(_extract_locations_from_payload(delivery_payload))

            if not location_urls and _looks_like_filemesh_payload(delivery_payload):
                return _normalize_filemesh_bytes(delivery_payload)

            if not location_urls:
                preview = _preview_bytes(delivery_payload)
                errors.append(
                    f"{delivery_url}: no delivery location in response "
                    f"(preview={preview!r})"
                )
                if _uses_opencloud_auth(delivery_url):
                    print(
                        f"[FileMesh] OpenCloud delivery returned no location for "
                        f"asset {asset_id} (auth={_describe_auth_mode(request_headers)}, "
                        f"preview={preview!r})"
                    )
                continue

            for location_url in location_urls:
                if not location_url:
                    continue
                # CDN signed URLs: no auth headers needed
                data = _fetch_url_bytes(location_url, timeout=timeout)
                if not _looks_like_filemesh_payload(data):
                    raise ValueError(
                        f"cdn payload for asset {asset_id} was not a filemesh "
                        f"(preview={_preview_bytes(data)!r})"
                    )
                return data
        except urllib.error.HTTPError as exc:  # pragma: no cover
            body = exc.read()
            preview = _preview_bytes(body) if body else ""
            errors.append(
                f"{delivery_url} http {exc.code}"
                + (f" (preview={preview!r})" if preview else "")
            )
            if _uses_opencloud_auth(delivery_url):
                print(
                    f"[FileMesh] OpenCloud delivery failed for asset {asset_id}: "
                    f"http {exc.code}, auth={_describe_auth_mode(request_headers)}, "
                    f"preview={preview!r}"
                )
        except Exception as exc:  # pragma: no cover
            errors.append(f"{delivery_url}: {exc}")
            if _uses_opencloud_auth(delivery_url):
                print(
                    f"[FileMesh] OpenCloud delivery errored for asset {asset_id}: "
                    f"{exc} (auth={_describe_auth_mode(request_headers)})"
                )
    return None


def _cache_filemesh_bytes(content_id, data) -> bytes:
    """Store raw mesh bytes under every key form callers may use."""
    _FILEMESH_BYTES_CACHE[str(content_id)] = data
    _FILEMESH_BYTES_CACHE[_filemesh_asset_key(content_id)] = data
    return data


def fetch_filemesh_bytes(
    content_id,
    timeout: float = 15.0,
    auth_headers: Optional[Dict[str, str]] = None,
    allow_local_paths: bool = True,
) -> bytes:
    """Fetch raw filemesh bytes from a content id or asset id.

    If the user is authenticated (via :mod:`roblox_animations.core.auth`) the
    OAuth bearer token is sent with the assetdelivery requests so that
    private / user-created meshes can be retrieved.
    """
    cache_key = str(content_id)
    asset_key = _filemesh_asset_key(content_id)
    cached = _FILEMESH_BYTES_CACHE.get(cache_key) or _FILEMESH_BYTES_CACHE.get(asset_key)
    if cached is not None:
        _FILEMESH_BYTES_CACHE[cache_key] = cached
        return cached

    text = str(content_id).strip() if content_id is not None else ""

    # Builtin content (rbxasset://fonts/head.mesh etc.) resolves to the local
    # Roblox install rather than the AssetDelivery API.
    if text.lower().startswith(_RBXASSET_PREFIX):
        local_path = _resolve_rbxasset_path(text)
        if local_path is not None:
            data = _normalize_filemesh_bytes(local_path.read_bytes())
            return _cache_filemesh_bytes(cache_key, data)
        raise ValueError(
            f"builtin mesh '{text}' not found in any local Roblox install "
            "(is Roblox Studio installed?)"
        )

    direct_url = None
    lower_text = text.lower()
    if lower_text.startswith(("http://", "https://")):
        if not is_trusted_roblox_asset_url(text):
            raise ValueError("refusing non-Roblox mesh URL from imported content")
        direct_url = text
    elif _looks_like_local_mesh_path(text):
        if not allow_local_paths:
            raise ValueError("refusing local mesh path from imported content")
        local_path = _resolve_local_mesh_path(text)
        if local_path is None:
            raise ValueError(f"local mesh path not found: '{text}'")
        if local_path.stat().st_size > _MAX_ASSET_BYTES:
            raise ValueError("local mesh exceeds import safety limit")
        data = _normalize_filemesh_bytes(local_path.read_bytes())
        return _cache_filemesh_bytes(cache_key, data)

    asset_id = extract_asset_id(content_id)

    errors: List[str] = []

    # Callers that fan work out to background threads provide this explicitly.
    # Refreshing OAuth tokens is main-thread-only and must never race across
    # workers (refresh tokens may rotate on successful use).
    if auth_headers is None:
        auth_headers = _get_auth_headers()

    if direct_url:
        try:
            data = _fetch_url_bytes(
                direct_url,
                timeout=timeout,
                extra_headers=auth_headers if _uses_opencloud_auth(direct_url) else None,
            )
            return _cache_filemesh_bytes(cache_key, data)
        except Exception as exc:  # pragma: no cover
            errors.append(str(exc))

    if asset_id is None:
        raise ValueError(
            f"could not determine mesh asset id from content '{content_id}'"
        )

    opencloud_url = (
        f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}"
    )
    # Try the supported authenticated endpoint exactly once.
    if auth_headers:
        data = _try_delivery_urls(
            [opencloud_url], asset_id, timeout, errors, auth_headers
        )
        if data is not None:
            return _cache_filemesh_bytes(cache_key, data)

    # Public fallback. v2 is Roblox's recommended legacy endpoint. The old
    # code put these in the authenticated pass (without auth, by host policy)
    # and then repeated both requests here, doubling every failed fetch.
    public_urls = [
        f"https://assetdelivery.roblox.com/v2/assetId/{asset_id}",
        f"https://assetdelivery.roblox.com/v1/asset/?id={asset_id}",
    ]
    data = _try_delivery_urls(public_urls, asset_id, timeout, errors)
    if data is not None:
        return _cache_filemesh_bytes(cache_key, data)

    legacy_url = f"https://www.roblox.com/asset/?id={asset_id}"
    try:
        data = _fetch_url_bytes(legacy_url, timeout=timeout)
        if not _looks_like_filemesh_payload(data):
            raise ValueError(
                f"legacy payload was not a filemesh (preview={_preview_bytes(data)!r})"
            )
        return _cache_filemesh_bytes(cache_key, data)
    except Exception as exc:  # pragma: no cover
        errors.append(str(exc))

    raise RuntimeError(
        f"failed to fetch filemesh for asset {asset_id}: "
        f"{'; '.join(errors) if errors else 'unknown error'}"
    )


def _parse_version_header(data: bytes) -> Tuple[str, int]:
    data = _normalize_filemesh_bytes(data)
    newline = data.find(b"\n")
    if newline == -1:
        raise ValueError(f"invalid filemesh header (preview={_preview_bytes(data)!r})")

    version = data[:newline].decode("ascii", errors="replace").strip()
    return version, newline + 1


def _decode_name_table(name_table: bytes, bones: List[dict]) -> List[str]:
    names: List[str] = []
    for bone in bones:
        start = bone["bone_name_index"]
        end = name_table.find(b"\0", start)
        if end == -1:
            end = len(name_table)
        names.append(name_table[start:end].decode("utf-8", errors="replace"))
    return names


def _parse_bones(data: bytes, offset: int, count: int) -> Tuple[List[dict], int]:
    bones = []
    for _ in range(count):
        unpacked = _BONE_STRUCT.unpack_from(data, offset)
        bones.append({
            "bone_name_index": unpacked[0],
            "parent_index": unpacked[1],
            "lod_parent_index": unpacked[2],
            "culling_radius": unpacked[3],
            "rotation": unpacked[4:13],
            "translation": unpacked[13:16],
        })
        offset += _BONE_STRUCT.size
    return bones, offset


def _attach_bone_names(bones: List[dict], bone_names: List[str]) -> List[dict]:
    # bone_names is already decoded by _decode_name_table in bone-array order
    # (each entry resolved via that bone's bone_name_index byte offset into the
    # raw name table). Indexing bone_names by array position is correct; using
    # bone_name_index (a byte offset) as a list index was a bug that only
    # worked by accident through the fallback path.
    for index, bone in enumerate(bones):
        name = bone_names[index] if index < len(bone_names) else None
        bone["name"] = name
        bone["resolved_name"] = name
    return bones


def _parse_subsets(data: bytes, offset: int, count: int) -> Tuple[List[dict], int]:
    subsets = []
    for _ in range(count):
        unpacked = _SUBSET_STRUCT.unpack_from(data, offset)
        subsets.append(
            {
                "faces_begin": unpacked[0],
                "faces_length": unpacked[1],
                "verts_begin": unpacked[2],
                "verts_length": unpacked[3],
                "num_bone_indices": unpacked[4],
                "bone_indices": list(unpacked[5:]),
            }
        )
        offset += _SUBSET_STRUCT.size
    return subsets, offset


def _read_vertex_records(data: bytes, offset: int, num_verts: int, vertex_size: int) -> Tuple[List[dict], int]:
    vertices = []
    vertices_append = vertices.append
    unpack_position = struct.unpack_from
    has_normal = vertex_size >= 24
    has_uv = vertex_size >= 32
    has_tangent = vertex_size >= 36
    has_color = vertex_size >= 40
    for _ in range(num_verts):
        position = unpack_position("<3f", data, offset)
        normal = unpack_position("<3f", data, offset + 12) if has_normal else None
        uv = unpack_position("<2f", data, offset + 24) if has_uv else None
        tangent_bytes = unpack_position("<4B", data, offset + 32) if has_tangent else None
        color_bytes = unpack_position("<4B", data, offset + 36) if has_color else None
        tangent_sign = _decode_tangent_sign(tangent_bytes)
        tangent = _decode_tangent_bytes(tangent_bytes, tangent_sign)
        vertices_append(
            {
                "position": position,
                "normal": normal,
                "uv": uv,
                "tangent": tangent,
                "tangent_bytes": tangent_bytes,
                "tangent_sign": tangent_sign,
                "tangent_sign_byte": tangent_bytes[3] if tangent_bytes is not None else None,
                "color": _decode_color_bytes(color_bytes),
                "color_bytes": color_bytes,
            }
        )
        offset += vertex_size
    return vertices, offset


def _decode_tangent_bytes(
    tangent_bytes,
    tangent_sign: Optional[float] = None,
) -> Optional[Tuple[float, float, float, float]]:
    if tangent_bytes is None or len(tangent_bytes) < 4:
        return None

    x = (float(tangent_bytes[0]) / 127.0) - 1.0
    y = (float(tangent_bytes[1]) / 127.0) - 1.0
    z = (float(tangent_bytes[2]) / 127.0) - 1.0
    sign = tangent_sign if tangent_sign is not None else _decode_tangent_sign(tangent_bytes)
    magnitude = ((x * x) + (y * y) + (z * z)) ** 0.5
    if magnitude <= 1e-6 or abs(magnitude - 1.0) > 0.15:
        return None

    inverse_magnitude = 1.0 / magnitude
    return (
        x * inverse_magnitude,
        y * inverse_magnitude,
        z * inverse_magnitude,
        sign if sign is not None else 1.0,
    )


def _decode_tangent_sign(tangent_bytes) -> Optional[float]:
    if tangent_bytes is None or len(tangent_bytes) < 4:
        return None
    sign = (float(tangent_bytes[3]) / 127.0) - 1.0
    return 1.0 if sign >= 0.0 else -1.0


def _decode_color_bytes(color_bytes) -> Optional[Tuple[float, float, float, float]]:
    if color_bytes is None or len(color_bytes) < 4:
        return None
    inverse_byte = 1.0 / 255.0
    return (
        color_bytes[0] * inverse_byte,
        color_bytes[1] * inverse_byte,
        color_bytes[2] * inverse_byte,
        color_bytes[3] * inverse_byte,
    )


def _read_faces(data: bytes, offset: int, num_faces: int) -> Tuple[List[Tuple[int, int, int]], int]:
    end = offset + (num_faces * _FACE_STRUCT.size)
    faces = list(_FACE_STRUCT.iter_unpack(data[offset:end]))
    offset = end
    return faces, offset


def _extract_vertex_attribute(vertices: List[dict], key: str):
    return [vertex.get(key) for vertex in vertices]


def _parse_skinning_arrays(data: bytes, offset: int, num_verts: int) -> Tuple[List[Tuple[List[int], List[int]]], int]:
    end = offset + (num_verts * _SKINNING_STRUCT.size)
    skinning = [
        (list(record[:4]), list(record[4:]))
        for record in _SKINNING_STRUCT.iter_unpack(data[offset:end])
    ]
    offset = end
    return skinning, offset


def _resolve_vertex_weights(
    num_verts: int,
    skinning: List[Tuple[List[int], List[int]]],
    subsets: List[dict],
    bone_names: List[str],
) -> List[Dict[str, float]]:
    """Resolve skinning data to vertex weights keyed by bone NAME.

    The bone array order is authoritative; the name table is decoded against
    that order by _decode_name_table (each bone's name resolved via its
    bone_name_index byte offset). Keying weights by name here means every
    downstream consumer (creation.py binding/transfer) can match weights
    directly against armature bones without a separate index->name step that
    was never implemented.
    """
    vertex_weights: List[Dict[str, float]] = [{} for _ in range(num_verts)]
    if not skinning or not subsets or not bone_names:
        return vertex_weights

    for subset in subsets:
        start = subset["verts_begin"]
        end = min(num_verts, start + subset["verts_length"])
        if start >= end:
            continue
        subset_bone_indices = []
        for bone_index in subset["bone_indices"][: subset["num_bone_indices"]]:
            if bone_index == 0xFFFF or bone_index >= len(bone_names):
                subset_bone_indices.append(None)
            else:
                subset_bone_indices.append(bone_index)
        for vertex_index in range(start, end):
            subset_indices, bone_weights = skinning[vertex_index]
            resolved: Dict[str, float] = {}
            total_weight = 0
            for subset_index, raw_weight in zip(subset_indices, bone_weights):
                if raw_weight <= 0:
                    continue

                if subset_index >= len(subset_bone_indices):
                    continue

                bone_index = subset_bone_indices[subset_index]
                if bone_index is None:
                    continue

                key = bone_names[bone_index]
                resolved[key] = resolved.get(key, 0.0) + raw_weight
                total_weight += raw_weight

            if total_weight > 0:
                inverse_total_weight = 1.0 / total_weight
                vertex_weights[vertex_index] = {
                    key: weight * inverse_total_weight for key, weight in resolved.items()
                }

    return vertex_weights


def _parse_v2_or_v3(data: bytes, version: str, offset: int) -> dict:
    if version.startswith("version 2"):
        header_size, vertex_size, face_size, num_verts, num_faces = struct.unpack_from("<HBBII", data, offset)
        offset += header_size
        num_lod_offsets = 0
        lod_offsets = []
    else:
        header_size, vertex_size, face_size, _lod_size, num_lod_offsets, num_verts, num_faces = struct.unpack_from(
            "<HBBHHII", data, offset
        )
        offset += header_size

    vertices, offset = _read_vertex_records(data, offset, num_verts, vertex_size)
    faces = []
    if face_size == 12:
        faces, offset = _read_faces(data, offset, num_faces)
    else:
        offset += num_faces * face_size
    lod_offsets = []
    if num_lod_offsets > 0:
        lod_offsets = list(struct.unpack_from(f"<{num_lod_offsets}I", data, offset))
    offset += num_lod_offsets * 4

    return {
        "version": version,
        "num_vertices": num_verts,
        "faces": faces,
        "positions": _extract_vertex_attribute(vertices, "position"),
        "normals": _extract_vertex_attribute(vertices, "normal"),
        "uvs": _extract_vertex_attribute(vertices, "uv"),
        "tangents": _extract_vertex_attribute(vertices, "tangent"),
        "tangent_bytes": _extract_vertex_attribute(vertices, "tangent_bytes"),
        "tangent_signs": _extract_vertex_attribute(vertices, "tangent_sign"),
        "tangent_sign_bytes": _extract_vertex_attribute(vertices, "tangent_sign_byte"),
        "colors": _extract_vertex_attribute(vertices, "color"),
        "color_bytes": _extract_vertex_attribute(vertices, "color_bytes"),
        "vertex_weights": [{} for _ in range(num_verts)],
        "bone_names": [],
        "has_skinning": False,
        "lod_type": None,
        "num_high_quality_lods": 0,
        "lod_offsets": lod_offsets,
        **_empty_facs_metadata(),
    }


def _infer_v4_vertex_size(total_len: int, offset: int, num_verts: int, num_faces: int, num_lod_offsets: int,
                          num_bones: int, bone_names_size: int, num_subsets: int, facs_size: int = 0) -> int:
    tail_bytes = (num_faces * 12) + (num_lod_offsets * 4) + (num_bones * _BONE_STRUCT.size) + \
        bone_names_size + (num_subsets * _SUBSET_STRUCT.size) + facs_size
    skinning_bytes = num_verts * 8 if num_bones > 0 else 0
    vertex_bytes = total_len - offset - tail_bytes - skinning_bytes
    if num_verts <= 0 or vertex_bytes <= 0:
        raise ValueError("could not infer filemesh vertex size")
    vertex_size = vertex_bytes // num_verts
    if vertex_size < 12:
        raise ValueError(f"invalid inferred vertex size {vertex_size}")
    return vertex_size


def _parse_v4_or_v5(data: bytes, version: str, offset: int) -> dict:
    facs_format = 0
    if version.startswith("version 5"):
        header = struct.unpack_from("<HHIIHHIHBBII", data, offset)
        header_size, lod_type, num_verts, num_faces, num_lod_offsets, num_bones, bone_names_size, num_subsets, hq_lods, _unused, facs_format, facs_size = header
    else:
        header = struct.unpack_from("<HHIIHHIHBB", data, offset)
        header_size, lod_type, num_verts, num_faces, num_lod_offsets, num_bones, bone_names_size, num_subsets, hq_lods, _unused = header
        facs_size = 0

    offset += header_size
    vertex_size = _infer_v4_vertex_size(
        len(data),
        offset,
        num_verts,
        num_faces,
        num_lod_offsets,
        num_bones,
        bone_names_size,
        num_subsets,
        facs_size,
    )
    vertices, offset = _read_vertex_records(data, offset, num_verts, vertex_size)

    skinning = []
    if num_bones > 0:
        skinning, offset = _parse_skinning_arrays(data, offset, num_verts)

    faces, offset = _read_faces(data, offset, num_faces)
    lod_offsets = list(struct.unpack_from(f"<{num_lod_offsets}I", data, offset)) if num_lod_offsets > 0 else []
    offset += num_lod_offsets * 4
    bones, offset = _parse_bones(data, offset, num_bones)
    name_table = data[offset: offset + bone_names_size]
    offset += bone_names_size
    bone_names = _decode_name_table(name_table, bones)
    bones = _attach_bone_names(bones, bone_names)
    subsets, offset = _parse_subsets(data, offset, num_subsets)
    vertex_weights = _resolve_vertex_weights(num_verts, skinning, subsets, bone_names)
    facs_metadata = _empty_facs_metadata()
    if facs_size > 0:
        if facs_format == 1:
            facs_metadata = _parse_facs_data(data[offset: offset + facs_size])
        elif facs_format != 0:
            facs_metadata = _unsupported_facs_metadata(
                facs_size,
                int(facs_format),
                f"unsupported facs data format {facs_format}",
            )

    return {
        "version": version,
        "num_vertices": num_verts,
        "faces": faces,
        "positions": _extract_vertex_attribute(vertices, "position"),
        "normals": _extract_vertex_attribute(vertices, "normal"),
        "uvs": _extract_vertex_attribute(vertices, "uv"),
        "tangents": _extract_vertex_attribute(vertices, "tangent"),
        "tangent_bytes": _extract_vertex_attribute(vertices, "tangent_bytes"),
        "tangent_signs": _extract_vertex_attribute(vertices, "tangent_sign"),
        "tangent_sign_bytes": _extract_vertex_attribute(vertices, "tangent_sign_byte"),
        "colors": _extract_vertex_attribute(vertices, "color"),
        "color_bytes": _extract_vertex_attribute(vertices, "color_bytes"),
        "vertex_weights": vertex_weights,
        "bone_names": bone_names,
        "bones": bones,
        "has_skinning": bool(num_bones and skinning),
        "lod_type": int(lod_type),
        "num_high_quality_lods": int(hq_lods),
        "lod_offsets": lod_offsets,
        **facs_metadata,
    }


def _parse_coremesh_v1(chunk: bytes) -> Tuple[List[dict], List[Tuple[int, int, int]], int]:
    num_verts = struct.unpack_from("<I", chunk, 0)[0]
    if num_verts <= 0:
        return [], [], 0

    vertex_size = None
    for candidate_size in (40, 36):
        vertex_block_end = 4 + (num_verts * candidate_size)
        if vertex_block_end + 4 > len(chunk):
            continue
        candidate_faces = struct.unpack_from("<I", chunk, vertex_block_end)[0]
        if vertex_block_end + 4 + (candidate_faces * 12) == len(chunk):
            vertex_size = candidate_size
            break

    if vertex_size is None:
        raise ValueError("could not infer v6 coremesh vertex size")

    offset = 4
    vertices, offset = _read_vertex_records(chunk, offset, num_verts, vertex_size)
    num_faces = struct.unpack_from("<I", chunk, offset)[0]
    offset += 4
    faces, offset = _read_faces(chunk, offset, num_faces)
    return vertices, faces, num_verts


def _get_blender_draco_dll_path() -> Optional[Path]:
    try:
        draco_module = importlib.import_module("io_scene_gltf2.io.com.draco")
        candidate = draco_module.dll_path()
        if candidate and Path(candidate).exists():
            return Path(candidate)
    except Exception:
        pass

    executable = Path(sys.executable).resolve() if sys.executable else None
    if executable:
        version_dir = executable.parent.parent.name
        for addons_dir in ("addons_core", "addons"):
            candidate = executable.parent.parent / version_dir / "scripts" / addons_dir / "io_scene_gltf2"
            if sys.platform == "win32":
                candidate = candidate / "extern_draco.dll"
            elif sys.platform == "linux":
                candidate = candidate / "libextern_draco.so"
            elif sys.platform == "darwin":
                candidate = candidate / "libextern_draco.dylib"
            else:
                candidate = None

            if candidate and candidate.exists():
                return candidate

    return None


def _load_blender_draco_dll():
    global _DRACO_DLL, _DRACO_LOAD_ERROR
    if _DRACO_DLL is not _DRACO_DLL_UNINITIALIZED:
        return _DRACO_DLL

    dll_path = _get_blender_draco_dll_path()
    if dll_path is None:
        _DRACO_LOAD_ERROR = "blender draco library was not found"
        return None

    try:
        dll = ctypes.cdll.LoadLibrary(str(dll_path.resolve()))
        dll.decoderCreate.restype = ctypes.c_void_p
        dll.decoderCreate.argtypes = []
        dll.decoderRelease.restype = None
        dll.decoderRelease.argtypes = [ctypes.c_void_p]
        dll.decoderDecode.restype = ctypes.c_bool
        dll.decoderDecode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        dll.decoderReadAttribute.restype = ctypes.c_bool
        dll.decoderReadAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_char_p]
        dll.decoderGetVertexCount.restype = ctypes.c_uint32
        dll.decoderGetVertexCount.argtypes = [ctypes.c_void_p]
        dll.decoderGetIndexCount.restype = ctypes.c_uint32
        dll.decoderGetIndexCount.argtypes = [ctypes.c_void_p]
        dll.decoderGetAttributeByteLength.restype = ctypes.c_size_t
        dll.decoderGetAttributeByteLength.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        dll.decoderCopyAttribute.restype = None
        dll.decoderCopyAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        dll.decoderReadIndices.restype = ctypes.c_bool
        dll.decoderReadIndices.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        dll.decoderGetIndicesByteLength.restype = ctypes.c_size_t
        dll.decoderGetIndicesByteLength.argtypes = [ctypes.c_void_p]
        dll.decoderCopyIndices.restype = None
        dll.decoderCopyIndices.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    except Exception as exc:
        _DRACO_LOAD_ERROR = f"failed to load {dll_path}: {exc}"
        return None

    _DRACO_LOAD_ERROR = None
    _DRACO_DLL = dll
    return dll


def _decode_draco_attribute_buffer(
    dll,
    decoder,
    attr_id: int,
    component_type: int,
    attr_type: bytes,
    components: int,
    vertex_count: int,
):
    if not dll.decoderReadAttribute(decoder, attr_id, component_type, attr_type):
        return None

    byte_length = int(dll.decoderGetAttributeByteLength(decoder, attr_id))
    if byte_length <= 0:
        return None

    buffer = ctypes.create_string_buffer(byte_length)
    dll.decoderCopyAttribute(decoder, attr_id, buffer)
    if component_type == _GLTF_COMPONENT_TYPE_FLOAT32:
        format_char = "f"
        component_size = 4
    elif component_type == _GLTF_COMPONENT_TYPE_UINT8:
        format_char = "B"
        component_size = 1
    else:
        return None

    value_count = vertex_count * components
    expected_byte_length = value_count * component_size
    if byte_length < expected_byte_length:
        raise RuntimeError(
            f"draco attribute {attr_id} length {byte_length} is shorter than expected {expected_byte_length}"
        )

    values = struct.unpack_from(f"<{value_count}{format_char}", buffer.raw, 0)
    return [tuple(values[index: index + components]) for index in range(0, len(values), components)]


def _decode_draco_coremesh_v2(chunk: bytes) -> Optional[Tuple[List[dict], List[Tuple[int, int, int]], int]]:
    if len(chunk) < 4:
        raise ValueError("truncated v7 draco coremesh header")

    draco_bitstream_size = struct.unpack_from("<I", chunk, 0)[0]
    if 4 + draco_bitstream_size > len(chunk):
        raise ValueError("truncated v7 draco coremesh payload")
    if 4 + draco_bitstream_size != len(chunk):
        raise ValueError("unexpected trailing bytes in v7 draco coremesh payload")

    dll = _load_blender_draco_dll()
    if dll is None:
        detail = f": {_DRACO_LOAD_ERROR}" if _DRACO_LOAD_ERROR else ""
        raise RuntimeError(f"draco decoder is unavailable for version 7 coremesh{detail}")

    bitstream = chunk[4: 4 + draco_bitstream_size]
    bitstream_buffer = ctypes.create_string_buffer(bitstream, len(bitstream))
    decoder = dll.decoderCreate()
    if not decoder:
        raise RuntimeError("failed to create draco decoder for version 7 coremesh")

    try:
        if not dll.decoderDecode(decoder, bitstream_buffer, len(bitstream)):
            raise RuntimeError("failed to decode draco bitstream for version 7 coremesh")

        vertex_count = int(dll.decoderGetVertexCount(decoder))
        index_count = int(dll.decoderGetIndexCount(decoder))
        if vertex_count > _MAX_DRACO_VERTICES or index_count > _MAX_DRACO_INDICES:
            raise RuntimeError("draco mesh exceeds import safety limit")
        if vertex_count <= 0:
            return [], [], 0

        positions = _decode_draco_attribute_buffer(
            dll,
            decoder,
            0,
            _GLTF_COMPONENT_TYPE_FLOAT32,
            b"VEC3",
            3,
            vertex_count,
        )
        normals = _decode_draco_attribute_buffer(
            dll,
            decoder,
            1,
            _GLTF_COMPONENT_TYPE_FLOAT32,
            b"VEC3",
            3,
            vertex_count,
        )
        uvs = _decode_draco_attribute_buffer(
            dll,
            decoder,
            2,
            _GLTF_COMPONENT_TYPE_FLOAT32,
            b"VEC2",
            2,
            vertex_count,
        )
        tangents = _decode_draco_attribute_buffer(
            dll,
            decoder,
            3,
            _GLTF_COMPONENT_TYPE_UINT8,
            b"VEC4",
            4,
            vertex_count,
        )
        colors = _decode_draco_attribute_buffer(
            dll,
            decoder,
            4,
            _GLTF_COMPONENT_TYPE_UINT8,
            b"VEC4",
            4,
            vertex_count,
        )

        if positions is None:
            raise RuntimeError("draco coremesh did not expose a position attribute")

        faces: List[Tuple[int, int, int]] = []
        if index_count > 0:
            if not dll.decoderReadIndices(decoder, _GLTF_COMPONENT_TYPE_UINT32):
                raise RuntimeError("draco coremesh exposed indices but they could not be read")

            byte_length = int(dll.decoderGetIndicesByteLength(decoder))
            expected_index_bytes = index_count * 4
            if byte_length < expected_index_bytes:
                raise RuntimeError(
                    f"draco index buffer length {byte_length} is shorter than expected {expected_index_bytes}"
                )
            if index_count % 3 != 0:
                raise RuntimeError(f"draco index count {index_count} is not divisible by 3")

            index_buffer = ctypes.create_string_buffer(byte_length)
            dll.decoderCopyIndices(decoder, index_buffer)
            flat_indices = struct.unpack_from(f"<{index_count}I", index_buffer.raw, 0)
            if flat_indices and max(flat_indices) >= vertex_count:
                raise RuntimeError("draco coremesh index references a missing vertex")
            faces = [
                (flat_indices[index], flat_indices[index + 1], flat_indices[index + 2])
                for index in range(0, len(flat_indices), 3)
            ]

        vertices = []
        for index, position in enumerate(positions):
            tangent_bytes = tangents[index] if tangents and index < len(tangents) else None
            color_bytes = colors[index] if colors and index < len(colors) else None
            vertices.append(
                {
                    "position": position,
                    "normal": normals[index] if normals and index < len(normals) else None,
                    "uv": uvs[index] if uvs and index < len(uvs) else None,
                    "tangent": _decode_tangent_bytes(tangent_bytes),
                    "tangent_bytes": tangent_bytes,
                    "tangent_sign": _decode_tangent_sign(tangent_bytes),
                    "tangent_sign_byte": tangent_bytes[3] if tangent_bytes is not None else None,
                    "color": _decode_color_bytes(color_bytes),
                    "color_bytes": color_bytes,
                }
            )
        return vertices, faces, vertex_count
    finally:
        dll.decoderRelease(decoder)


def _parse_skinning_chunk(chunk: bytes) -> dict:
    offset = 0
    num_skinnings = struct.unpack_from("<I", chunk, offset)[0]
    offset += 4
    skinning, offset = _parse_skinning_arrays(chunk, offset, num_skinnings)
    num_bones = struct.unpack_from("<I", chunk, offset)[0]
    offset += 4
    bones, offset = _parse_bones(chunk, offset, num_bones)
    name_table_size = struct.unpack_from("<I", chunk, offset)[0]
    offset += 4
    name_table = chunk[offset: offset + name_table_size]
    offset += name_table_size
    bone_names = _decode_name_table(name_table, bones)
    bones = _attach_bone_names(bones, bone_names)
    num_subsets = struct.unpack_from("<I", chunk, offset)[0]
    offset += 4
    subsets, offset = _parse_subsets(chunk, offset, num_subsets)
    vertex_weights = _resolve_vertex_weights(num_skinnings, skinning, subsets, bone_names)

    return {
        "num_vertices": num_skinnings,
        "vertex_weights": vertex_weights,
        "bone_names": bone_names,
        "bones": bones,
        "has_skinning": bool(num_bones and skinning),
    }


def _parse_lods_chunk(chunk: bytes) -> dict:
    offset = 0
    if len(chunk) < 7:
        raise ValueError("truncated lods chunk")

    lod_type, num_high_quality_lods = struct.unpack_from("<HB", chunk, offset)
    offset += 3
    num_lod_offsets = struct.unpack_from("<I", chunk, offset)[0]
    offset += 4
    if offset + (num_lod_offsets * 4) > len(chunk):
        raise ValueError("truncated lod offsets")

    lod_offsets = list(struct.unpack_from(f"<{num_lod_offsets}I", chunk, offset)) if num_lod_offsets > 0 else []
    offset += num_lod_offsets * 4
    if offset != len(chunk):
        raise ValueError("unexpected trailing bytes in lods chunk")
    return {
        "lod_type": int(lod_type),
        "num_high_quality_lods": int(num_high_quality_lods),
        "lod_offsets": lod_offsets,
    }


def _parse_v6_or_v7(data: bytes, version: str, offset: int) -> dict:
    vertices = None
    faces: List[Tuple[int, int, int]] = []
    num_vertices = 0
    vertex_weights: List[Dict[str, float]] = []
    bone_names: List[str] = []
    bones: List[dict] = []
    has_skinning = False
    facs_metadata = _empty_facs_metadata()
    lod_metadata = {
        "lod_type": None,
        "num_high_quality_lods": 0,
        "lod_offsets": [],
    }
    coremesh_vertex_count = None
    skinning_vertex_count = None

    while offset < len(data):
        if offset + 16 > len(data):
            raise ValueError("truncated filemesh chunk header")

        chunk_type_raw = data[offset: offset + 8]
        chunk_type = chunk_type_raw.decode("ascii", errors="ignore").rstrip("\0 ")
        chunk_version, chunk_size = struct.unpack_from("<II", data, offset + 8)
        chunk_end = offset + 16 + chunk_size
        if chunk_end > len(data):
            raise ValueError(f"truncated {chunk_type or 'unknown'} chunk payload")
        chunk_data = data[offset + 16: chunk_end]
        offset = chunk_end

        if chunk_type == "COREMESH" and chunk_version == 1:
            vertices, faces, num_vertices = _parse_coremesh_v1(chunk_data)
            coremesh_vertex_count = num_vertices
        elif chunk_type == "COREMESH" and chunk_version == 2:
            vertices, faces, num_vertices = _decode_draco_coremesh_v2(chunk_data)
            coremesh_vertex_count = num_vertices
        elif chunk_type == "SKINNING" and chunk_version == 1:
            skinning_data = _parse_skinning_chunk(chunk_data)
            skinning_vertex_count = skinning_data["num_vertices"]
            if coremesh_vertex_count is not None and skinning_vertex_count != coremesh_vertex_count:
                raise ValueError(
                    f"skinning vertex count {skinning_vertex_count} does not match coremesh vertex count {coremesh_vertex_count}"
                )
            num_vertices = max(num_vertices, skinning_vertex_count)
            vertex_weights = skinning_data["vertex_weights"]
            bone_names = skinning_data["bone_names"]
            bones = skinning_data.get("bones") or []
            has_skinning = skinning_data["has_skinning"]
        elif chunk_type == "LODS" and chunk_version == 1:
            lod_metadata = _parse_lods_chunk(chunk_data)
        elif chunk_type == "FACS" and chunk_version == 1:
            facs_metadata = _parse_facs_chunk(chunk_data)

    if coremesh_vertex_count is not None and skinning_vertex_count is not None and skinning_vertex_count != coremesh_vertex_count:
        raise ValueError(
            f"skinning vertex count {skinning_vertex_count} does not match coremesh vertex count {coremesh_vertex_count}"
        )

    if version.startswith("version 7") and vertices is None:
        raise RuntimeError("version 7 filemesh could not decode draco coremesh data")

    if not vertex_weights and num_vertices > 0:
        vertex_weights = [{} for _ in range(num_vertices)]

    return {
        "version": version,
        "num_vertices": num_vertices,
        "faces": faces,
        "positions": _extract_vertex_attribute(vertices or [], "position") if vertices is not None else None,
        "normals": _extract_vertex_attribute(vertices or [], "normal") if vertices is not None else None,
        "uvs": _extract_vertex_attribute(vertices or [], "uv") if vertices is not None else None,
        "tangents": _extract_vertex_attribute(vertices or [], "tangent") if vertices is not None else None,
        "tangent_bytes": _extract_vertex_attribute(vertices or [], "tangent_bytes") if vertices is not None else None,
        "tangent_signs": _extract_vertex_attribute(vertices or [], "tangent_sign") if vertices is not None else None,
        "tangent_sign_bytes": _extract_vertex_attribute(vertices or [], "tangent_sign_byte") if vertices is not None else None,
        "colors": _extract_vertex_attribute(vertices or [], "color") if vertices is not None else None,
        "color_bytes": _extract_vertex_attribute(vertices or [], "color_bytes") if vertices is not None else None,
        "vertex_weights": vertex_weights,
        "bone_names": bone_names,
        "bones": bones,
        "has_skinning": has_skinning,
        **lod_metadata,
        **facs_metadata,
    }


def _parse_v1_ascii(data: bytes, version: str, offset: int) -> dict:
    """Parse an ascii v1.00/v1.01 FileMesh.

    Layout: ``version 1.00`` line, a face-count line, then a data line
    holding ``num_faces * 9`` bracket groups — per face, three vertex
    records of ``[pos][normal][uv]``.  Older assets wrap EACH float in its
    own brackets; newer ones pack comma-separated triplets per group.

    Quirks (per the format spec): version 1.00 positions are authored 2x
    too large (corrected in 1.01), and every version 1 mesh stores tex_V
    upside down, so the UV must be read as (tex_U, 1 - tex_V).
    """
    text = data[offset:].decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("ascii v1 filemesh is empty after version header")
    try:
        num_faces = int(lines[0])
    except ValueError as exc:
        raise ValueError(f"ascii v1 filemesh has no face count (got {lines[0]!r})") from exc

    positions: List[Tuple[float, float, float]] = []
    normals: List[Tuple[float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    faces: List[Tuple[int, int, int]] = []

    fields = []
    for line in lines[1:]:
        # "[x][y][z][nx][ny][nz][u][v][t]" -> list of bracketed groups.
        # Older v1 assets wrap EACH float in its own brackets; newer ones
        # pack a comma-separated triplet per group ("[x,y,z]"), so every
        # group must be split on commas as well.
        fields.extend(part for part in line.replace("[", "]").split("]") if part)

    floats: List[float] = []
    for part in fields:
        for piece in part.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                floats.append(float(piece))
            except ValueError:
                pass

    values_per_face = 9 * 3  # 3 vertices x 9 floats (pos+normal+uvw/t)
    available_faces = len(floats) // values_per_face
    num_faces = max(0, min(num_faces, available_faces))

    # v1.00 geometry is authored at 2x scale (fixed in v1.01); version 1
    # stores tex_V upside down across BOTH revisions.
    half_scale = version == "version 1.00"
    cursor = 0
    for face_index in range(num_faces):
        base = len(positions)
        for corner in range(3):
            i = cursor + corner * 9
            x, y, z = floats[i], floats[i + 1], floats[i + 2]
            if half_scale:
                x, y, z = x * 0.5, y * 0.5, z * 0.5
            u, v = floats[i + 6], 1.0 - floats[i + 7]
            positions.append((x, y, z))
            normals.append((floats[i + 3], floats[i + 4], floats[i + 5]))
            uvs.append((u, v))
        faces.append((base, base + 1, base + 2))
        cursor += values_per_face

    return {
        "version": version,
        "num_vertices": len(positions),
        "faces": faces,
        "positions": positions,
        "normals": normals,
        "uvs": uvs,
        "tangents": [],
        "tangent_bytes": [],
        "tangent_signs": [],
        "tangent_sign_bytes": [],
        "colors": [],
        "color_bytes": [],
        "vertex_weights": [{} for _ in range(len(positions))],
        "bone_names": [],
        "has_skinning": False,
        "lod_type": None,
        "num_high_quality_lods": 0,
        "lod_offsets": [],
        **_empty_facs_metadata(),
    }


def parse_filemesh(data: bytes) -> dict:
    """Parse enough of a FileMesh to reconstruct vertex weights in Blender."""
    version, offset = _parse_version_header(data)
    data = _normalize_filemesh_bytes(data)

    if version.startswith("version 1"):
        return _parse_v1_ascii(data, version, offset)
    if version.startswith("version 2") or version.startswith("version 3"):
        return _parse_v2_or_v3(data, version, offset)
    if version.startswith("version 4") or version.startswith("version 5"):
        return _parse_v4_or_v5(data, version, offset)
    if version.startswith("version 6") or version.startswith("version 7"):
        return _parse_v6_or_v7(data, version, offset)

    raise ValueError(f"unsupported filemesh version '{version}' (preview={_preview_bytes(data)!r})")


_ARRAY_ROWS_CLASS = None


def _as_rows(arr):
    """Wrap a numpy array so ``values or []`` guards keep working.

    Plain ndarray truthiness raises for size > 1; the wrapped class answers
    by element count instead, matching the list/tuple contract consumers
    already rely on."""
    global _ARRAY_ROWS_CLASS
    if arr is None:
        return None
    try:
        import numpy as np  # noqa: PLC0415 — blender bundles numpy
    except ImportError:
        return arr
    if _ARRAY_ROWS_CLASS is None:
        class _ArrayRows(np.ndarray):
            def __bool__(self):
                return self.size > 0
        _ARRAY_ROWS_CLASS = _ArrayRows
    if isinstance(arr, np.ndarray) and type(arr) is np.ndarray:
        return arr.view(_ARRAY_ROWS_CLASS)
    return arr


def _read_vertex_arrays(data: bytes, offset: int, num_verts: int, vertex_size: int):
    """Bulk-decode one FileMesh vertex block into numpy arrays.

    Layout matches _read_vertex_records: 0 position(3f) 12 normal(3f)
    24 uv(2f) 32 tangent(4B) 36 color(4B)."""
    import numpy as np  # noqa: PLC0415 — blender bundles numpy

    if num_verts <= 0:
        return {
            "positions": np.empty((0, 3), dtype=np.float32),
            "normals": None,
            "uvs": None,
            "tangent_bytes": None,
            "tangent_sign_bytes": None,
            "tangent_signs": None,
            "colors": None,
            "color_bytes": None,
        }, offset

    block_end = offset + num_verts * vertex_size
    if block_end > len(data):
        raise ValueError("truncated vertex block")

    float_count = num_verts * (vertex_size // 4)
    floats = np.frombuffer(data, dtype="<f4", count=float_count, offset=offset)
    floats = np.ascontiguousarray(floats).reshape(num_verts, vertex_size // 4).copy()
    positions = floats[:, 0:3].copy()
    normals = floats[:, 3:6].copy() if vertex_size >= 24 else None
    uvs = floats[:, 6:8].copy() if vertex_size >= 32 else None

    tangent_bytes = None
    tangent_sign_bytes = None
    tangent_signs = None
    colors = None
    color_bytes = None
    if vertex_size >= 36:
        raw = np.frombuffer(data, dtype=np.uint8, count=num_verts * vertex_size, offset=offset)
        raw = np.ascontiguousarray(raw).reshape(num_verts, vertex_size).copy()
        tangent_bytes = raw[:, 32:36].copy()
        tangent_sign_bytes = raw[:, 35].copy()
        tangent_signs = np.where(tangent_sign_bytes >= 127, 1.0, -1.0).astype(np.float32)
        if vertex_size >= 40:
            color_bytes = raw[:, 36:40].copy()
            colors = color_bytes.astype(np.float32) * (1.0 / 255.0)

    return {
        "positions": positions,
        "normals": normals,
        "uvs": uvs,
        "tangent_bytes": tangent_bytes,
        "tangent_sign_bytes": tangent_sign_bytes,
        "tangent_signs": tangent_signs,
        "colors": colors,
        "color_bytes": color_bytes,
    }, block_end


def _read_face_array(data: bytes, offset: int, num_faces: int):
    """Bulk-decode a FileMesh face block (three uint32 per face)."""
    import numpy as np  # noqa: PLC0415 — blender bundles numpy

    if num_faces <= 0:
        return np.empty((0, 3), dtype=np.uint32)
    end = offset + num_faces * 12
    if end > len(data):
        raise ValueError("truncated face block")
    faces = np.frombuffer(data, dtype="<u4", count=num_faces * 3, offset=offset)
    return np.ascontiguousarray(faces).reshape(num_faces, 3).copy()


def _assemble_mesh_arrays(
    version,
    num_vertices,
    faces,
    attrs,
    vertex_weights,
    bone_names,
    bones,
    has_skinning,
    lod_type,
    num_high_quality_lods,
    lod_offsets,
    facs_metadata,
):
    return {
        "version": version,
        "num_vertices": num_vertices,
        "faces": _as_rows(faces),
        "positions": _as_rows(attrs["positions"]),
        "normals": _as_rows(attrs["normals"]),
        "uvs": _as_rows(attrs["uvs"]),
        # The place pipeline never consumes decoded tangents (custom tangent
        # attributes are disabled for all synthesized meshes); the raw byte
        # forms above are kept for parity with the tuple-list parser.
        "tangents": None,
        "tangent_bytes": _as_rows(attrs["tangent_bytes"]),
        "tangent_signs": _as_rows(attrs["tangent_signs"]),
        "tangent_sign_bytes": _as_rows(attrs["tangent_sign_bytes"]),
        "colors": _as_rows(attrs["colors"]),
        "color_bytes": _as_rows(attrs["color_bytes"]),
        "vertex_weights": vertex_weights,
        "bone_names": bone_names,
        "bones": bones,
        "has_skinning": has_skinning,
        "lod_type": lod_type,
        "num_high_quality_lods": num_high_quality_lods,
        "lod_offsets": lod_offsets,
        **facs_metadata,
    }


def _parse_v2_or_v3_arrays(data: bytes, version: str, offset: int) -> dict:
    if version.startswith("version 2"):
        header_size, vertex_size, face_size, num_verts, num_faces = struct.unpack_from("<HBBII", data, offset)
        offset += header_size
        num_lod_offsets = 0
    else:
        header_size, vertex_size, face_size, _lod_size, num_lod_offsets, num_verts, num_faces = struct.unpack_from(
            "<HBBHHII", data, offset
        )
        offset += header_size

    attrs, offset = _read_vertex_arrays(data, offset, num_verts, vertex_size)
    if face_size == 12:
        faces = _read_face_array(data, offset, num_faces)
        offset += num_faces * 12
    else:
        faces = _read_face_array(data, offset, 0)
        offset += num_faces * face_size
    lod_offsets = list(struct.unpack_from(f"<{num_lod_offsets}I", data, offset)) if num_lod_offsets > 0 else []

    return _assemble_mesh_arrays(
        version,
        num_verts,
        faces,
        attrs,
        [{} for _ in range(num_verts)],
        [],
        [],
        False,
        None,
        0,
        lod_offsets,
        _empty_facs_metadata(),
    )


def _parse_v4_or_v5_arrays(data: bytes, version: str, offset: int) -> dict:
    facs_format = 0
    if version.startswith("version 5"):
        header = struct.unpack_from("<HHIIHHIHBBII", data, offset)
        header_size, lod_type, num_verts, num_faces, num_lod_offsets, num_bones, bone_names_size, num_subsets, hq_lods, _unused, facs_format, facs_size = header
    else:
        header = struct.unpack_from("<HHIIHHIHBB", data, offset)
        header_size, lod_type, num_verts, num_faces, num_lod_offsets, num_bones, bone_names_size, num_subsets, hq_lods, _unused = header
        facs_size = 0

    offset += header_size
    vertex_size = _infer_v4_vertex_size(
        len(data),
        offset,
        num_verts,
        num_faces,
        num_lod_offsets,
        num_bones,
        bone_names_size,
        num_subsets,
        facs_size,
    )
    attrs, offset = _read_vertex_arrays(data, offset, num_verts, vertex_size)

    skinning = []
    if num_bones > 0:
        skinning, offset = _parse_skinning_arrays(data, offset, num_verts)

    faces = _read_face_array(data, offset, num_faces)
    offset += num_faces * 12
    lod_offsets = list(struct.unpack_from(f"<{num_lod_offsets}I", data, offset)) if num_lod_offsets > 0 else []
    offset += num_lod_offsets * 4
    bones, offset = _parse_bones(data, offset, num_bones)
    name_table = data[offset: offset + bone_names_size]
    offset += bone_names_size
    bone_names = _decode_name_table(name_table, bones)
    bones = _attach_bone_names(bones, bone_names)
    subsets, offset = _parse_subsets(data, offset, num_subsets)
    vertex_weights = _resolve_vertex_weights(num_verts, skinning, subsets, bone_names)
    facs_metadata = _empty_facs_metadata()
    if facs_size > 0:
        if facs_format == 1:
            facs_metadata = _parse_facs_data(data[offset: offset + facs_size])
        elif facs_format != 0:
            facs_metadata = _unsupported_facs_metadata(
                facs_size,
                int(facs_format),
                f"unsupported facs data format {facs_format}",
            )

    return _assemble_mesh_arrays(
        version,
        num_verts,
        faces,
        attrs,
        vertex_weights,
        bone_names,
        bones,
        bool(num_bones and skinning),
        int(lod_type),
        int(hq_lods),
        lod_offsets,
        facs_metadata,
    )


def _parse_coremesh_v1_arrays(chunk: bytes):
    num_verts = struct.unpack_from("<I", chunk, 0)[0]
    if num_verts <= 0:
        return _read_vertex_arrays(chunk, 0, 0, 40)[0], _read_face_array(chunk, 0, 0), 0

    vertex_size = None
    for candidate_size in (40, 36):
        vertex_block_end = 4 + (num_verts * candidate_size)
        if vertex_block_end + 4 > len(chunk):
            continue
        candidate_faces = struct.unpack_from("<I", chunk, vertex_block_end)[0]
        if vertex_block_end + 4 + (candidate_faces * 12) == len(chunk):
            vertex_size = candidate_size
            break

    if vertex_size is None:
        raise ValueError("could not infer v6 coremesh vertex size")

    attrs, offset = _read_vertex_arrays(chunk, 4, num_verts, vertex_size)
    num_faces = struct.unpack_from("<I", chunk, offset)[0]
    offset += 4
    faces = _read_face_array(chunk, offset, num_faces)
    return attrs, faces, num_verts


def _parse_v6_arrays(data: bytes, version: str, offset: int) -> dict:
    attrs = None
    faces = _read_face_array(data, 0, 0)
    num_vertices = 0
    vertex_weights: List[Dict[str, float]] = []
    bone_names: List[str] = []
    bones: List[dict] = []
    has_skinning = False
    facs_metadata = _empty_facs_metadata()
    lod_metadata = {
        "lod_type": None,
        "num_high_quality_lods": 0,
        "lod_offsets": [],
    }
    coremesh_vertex_count = None
    skinning_vertex_count = None

    while offset < len(data):
        if offset + 16 > len(data):
            raise ValueError("truncated filemesh chunk header")

        chunk_type_raw = data[offset: offset + 8]
        chunk_type = chunk_type_raw.decode("ascii", errors="ignore").rstrip("\0 ")
        chunk_version, chunk_size = struct.unpack_from("<II", data, offset + 8)
        chunk_end = offset + 16 + chunk_size
        if chunk_end > len(data):
            raise ValueError(f"truncated {chunk_type or 'unknown'} chunk payload")
        chunk_data = data[offset + 16: chunk_end]
        offset = chunk_end

        if chunk_type == "COREMESH" and chunk_version == 1:
            attrs, faces, num_vertices = _parse_coremesh_v1_arrays(chunk_data)
            coremesh_vertex_count = num_vertices
        elif chunk_type == "COREMESH" and chunk_version == 2:
            raise ValueError("draco coremesh has no bulk decode path")
        elif chunk_type == "SKINNING" and chunk_version == 1:
            skinning_data = _parse_skinning_chunk(chunk_data)
            skinning_vertex_count = skinning_data["num_vertices"]
            if coremesh_vertex_count is not None and skinning_vertex_count != coremesh_vertex_count:
                raise ValueError(
                    f"skinning vertex count {skinning_vertex_count} does not match coremesh vertex count {coremesh_vertex_count}"
                )
            num_vertices = max(num_vertices, skinning_vertex_count)
            vertex_weights = skinning_data["vertex_weights"]
            bone_names = skinning_data["bone_names"]
            bones = skinning_data.get("bones") or []
            has_skinning = skinning_data["has_skinning"]
        elif chunk_type == "LODS" and chunk_version == 1:
            lod_metadata = _parse_lods_chunk(chunk_data)
        elif chunk_type == "FACS" and chunk_version == 1:
            facs_metadata = _parse_facs_chunk(chunk_data)

    if coremesh_vertex_count is not None and skinning_vertex_count is not None and skinning_vertex_count != coremesh_vertex_count:
        raise ValueError(
            f"skinning vertex count {skinning_vertex_count} does not match coremesh vertex count {coremesh_vertex_count}"
        )

    if attrs is None:
        raise ValueError("version 6 filemesh has no bulk-decodable coremesh")

    if not vertex_weights and num_vertices > 0:
        vertex_weights = [{} for _ in range(num_vertices)]

    return _assemble_mesh_arrays(
        version,
        num_vertices,
        faces,
        attrs,
        vertex_weights,
        bone_names,
        bones,
        has_skinning,
        lod_metadata["lod_type"],
        lod_metadata["num_high_quality_lods"],
        lod_metadata["lod_offsets"],
        facs_metadata,
    )


def parse_filemesh_arrays(data: bytes) -> dict:
    """Parse a FileMesh with bulk numpy decoding (arrays of rows).

    positions/normals/uvs/faces/colors come back as numpy arrays instead of
    tuple lists, so millions of per-vertex python objects are never created.
    Versions without a bulk path (ascii v1, draco v7) raise ValueError and
    callers should fall back to parse_filemesh."""
    version, offset = _parse_version_header(data)
    data = _normalize_filemesh_bytes(data)

    if version.startswith("version 2") or version.startswith("version 3"):
        return _parse_v2_or_v3_arrays(data, version, offset)
    if version.startswith("version 4") or version.startswith("version 5"):
        return _parse_v4_or_v5_arrays(data, version, offset)
    if version.startswith("version 6"):
        return _parse_v6_arrays(data, version, offset)

    raise ValueError(f"no bulk parser for filemesh version '{version}'")


def fetch_and_parse_filemesh(
    content_id,
    timeout: float = 15.0,
    auth_headers: Optional[Dict[str, str]] = None,
    use_arrays: bool = False,
    retain_parsed_cache: bool = True,
    allow_local_paths: bool = True,
) -> dict:
    """Fetch and parse FileMesh data with simple in-process caching.

    ``use_arrays`` requests the bulk numpy parser (array fields instead of
    tuple lists) and silently falls back to the classic parser when the
    payload has no bulk path. Array results are cached under a separate key
    so classic callers never receive them. ``retain_parsed_cache=False`` is
    for one-shot bulk imports that already group instances by asset; it avoids
    retaining a second, full decoded copy after the caller compacts/uploads
    the selected LOD."""
    cache_key = str(content_id)
    asset_key = _filemesh_asset_key(content_id)
    array_key = f"arrays:{asset_key}" if use_arrays else None
    if retain_parsed_cache and array_key is not None and array_key in _FILEMESH_CACHE:
        return _FILEMESH_CACHE[array_key]
    if retain_parsed_cache and asset_key in _FILEMESH_CACHE:
        return _FILEMESH_CACHE[asset_key]
    if retain_parsed_cache and cache_key in _FILEMESH_CACHE:
        return _FILEMESH_CACHE[cache_key]
    prefetch_error = _FILEMESH_PREFETCH_FAILURES.get(cache_key) or _FILEMESH_PREFETCH_FAILURES.get(asset_key)
    if prefetch_error is not None:
        raise RuntimeError(f"prefetch failed: {prefetch_error}")

    with _filemesh_fetch_lock(content_id):
        if retain_parsed_cache and array_key is not None and array_key in _FILEMESH_CACHE:
            return _FILEMESH_CACHE[array_key]
        if retain_parsed_cache and asset_key in _FILEMESH_CACHE:
            return _FILEMESH_CACHE[asset_key]
        try:
            t_net = time.perf_counter()
            raw = fetch_filemesh_bytes(
                content_id, timeout=timeout, auth_headers=auth_headers,
                allow_local_paths=allow_local_paths,
            )
            net_seconds = time.perf_counter() - t_net
            t_parse = time.perf_counter()
            if use_arrays:
                try:
                    parsed = parse_filemesh_arrays(raw)
                except Exception:
                    parsed = parse_filemesh(raw)
            else:
                parsed = parse_filemesh(raw)
            parse_seconds = time.perf_counter() - t_parse
        except Exception as exc:
            _FILEMESH_PREFETCH_FAILURES[asset_key] = str(exc)
            raise
        # Timing metadata rides on the parsed payload so the lazy import's
        # summary can split network latency from parser CPU.
        if isinstance(parsed, dict):
            parsed["_rbx_net_seconds"] = net_seconds
            parsed["_rbx_parse_seconds"] = parse_seconds
        if retain_parsed_cache:
            _FILEMESH_CACHE[cache_key] = parsed
            _FILEMESH_CACHE[asset_key] = parsed
            if array_key is not None:
                _FILEMESH_CACHE[array_key] = parsed
        return parsed


def prefetch_filemeshes(
    content_ids, max_workers: int = 12, timeout: float = 4.0,
    allow_local_paths: bool = True,
) -> None:
    """Warm the FileMesh caches for many content ids concurrently.

    Network + parse are thread-safe (each request builds its own opener;
    cache dict writes are atomic under the GIL — a duplicate fetch on a
    race is harmless). Auth headers are resolved once up-front on the
    calling (main) thread so no worker triggers a token refresh.
    Errors are swallowed here; the serial path will report them when it
    re-requests the same id."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    ids = []
    seen = set()
    for content_id in content_ids:
        if not content_id:
            continue
        asset_id = extract_asset_id(content_id)
        key = f"asset:{asset_id}" if asset_id is not None else str(content_id)
        if key in seen or key in _FILEMESH_CACHE:
            continue
        seen.add(key)
        ids.append(content_id)
    if not ids:
        return

    auth_headers = _get_auth_headers()  # resolve/refresh tokens on the main thread first

    def _warm(content_id):
        try:
            fetch_and_parse_filemesh(
                content_id, timeout=timeout, auth_headers=auth_headers,
                allow_local_paths=allow_local_paths,
            )
        except Exception as exc:
            # Do not repeat a known-dead request during this import. The
            # cache is cleared by release_import_cache after the operator ends.
            _FILEMESH_PREFETCH_FAILURES[str(content_id)] = str(exc)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_warm, ids))
