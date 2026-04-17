"""Utilities for fetching and parsing Roblox FileMesh skinning data."""

from __future__ import annotations

import ctypes
import gzip
import importlib
import json
from pathlib import Path
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple


_FILEMESH_CACHE: Dict[str, dict] = {}
_FILEMESH_BYTES_CACHE: Dict[str, bytes] = {}

_BONE_STRUCT = struct.Struct("<IHHf9f3f")
_SUBSET_STRUCT = struct.Struct("<IIIII26H")
_FILEMESH_FACS_HEADER_STRUCT = struct.Struct("<IIQII")
_QUANTIZED_MATRIX_HEADER_STRUCT = struct.Struct("<HII")
_TWO_POSE_CORRECTIVE_STRUCT = struct.Struct("<HH")
_THREE_POSE_CORRECTIVE_STRUCT = struct.Struct("<HHH")
_FACE_STRUCT = struct.Struct("<III")
_SKINNING_STRUCT = struct.Struct("<4B4B")
_FACS_TRANSFORM_CHANNELS = ("px", "py", "pz", "rx", "ry", "rz")
_GLTF_COMPONENT_TYPE_UINT32 = 5125
_GLTF_COMPONENT_TYPE_FLOAT32 = 5126
_DRACO_DLL_UNINITIALIZED = object()
_DRACO_DLL = _DRACO_DLL_UNINITIALIZED

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

    values = [list(flat_values[row_offset : row_offset + cols]) for row_offset in range(0, value_count, cols)]
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
    face_bone_names_blob = blob[offset : offset + face_bone_names_size]
    offset += face_bone_names_size
    face_control_names_blob = blob[offset : offset + face_control_names_size]
    offset += face_control_names_size
    quantized_transforms_blob = blob[offset : offset + quantized_transforms_size]
    offset += quantized_transforms_size
    two_pose_correctives_blob = blob[offset : offset + two_pose_correctives_size]
    offset += two_pose_correctives_size
    three_pose_correctives_blob = blob[offset : offset + three_pose_correctives_size]

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

    return _parse_facs_data(chunk[4 : 4 + facs_data_size])


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


def _preview_bytes(data: bytes, limit: int = 64) -> str:
    snippet = data[:limit]
    text = snippet.decode("ascii", errors="replace")
    return text.replace("\r", "\\r").replace("\n", "\\n")


def _normalize_filemesh_bytes(data: bytes) -> bytes:
    if data.startswith(b"\x1f\x8b"):
        try:
            data = gzip.decompress(data)
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


def _fetch_url_response(
    url: str,
    timeout: float = 15.0,
    follow_redirects: bool = True,
    extra_headers: Optional[Dict[str, str]] = None,
):
    _require_online_access("fetch Roblox mesh data")
    headers: Dict[str, str] = {
        "User-Agent": "RobloxStudio/WinInet",
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, headers=headers)
    opener = (
        urllib.request.build_opener()
        if follow_redirects
        else urllib.request.build_opener(_NoRedirectHandler())
    )
    with opener.open(request, timeout=timeout) as response:
        data = response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding or data.startswith(b"\x1f\x8b"):
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        return response, data


def _fetch_url_bytes(
    url: str,
    timeout: float = 15.0,
    extra_headers: Optional[Dict[str, str]] = None,
) -> bytes:
    _, data = _fetch_url_response(
        url, timeout=timeout, follow_redirects=True, extra_headers=extra_headers
    )
    return _normalize_filemesh_bytes(data)


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


def fetch_filemesh_bytes(content_id, timeout: float = 15.0) -> bytes:
    """Fetch raw filemesh bytes from a content id or asset id.

    If the user is authenticated (via :mod:`roblox_animations.core.auth`) the
    OAuth bearer token is sent with the assetdelivery requests so that
    private / user-created meshes can be retrieved.
    """
    cache_key = str(content_id)
    if cache_key in _FILEMESH_BYTES_CACHE:
        return _FILEMESH_BYTES_CACHE[cache_key]

    direct_url = None
    text = str(content_id).strip() if content_id is not None else ""
    if text.lower().startswith(("http://", "https://")):
        direct_url = text

    asset_id = extract_asset_id(content_id)

    errors: List[str] = []

    # Resolve auth headers once (main-thread safe; no-op if not authenticated)
    auth_headers = _get_auth_headers()

    if direct_url:
        try:
            data = _fetch_url_bytes(
                direct_url,
                timeout=timeout,
                extra_headers=auth_headers if _uses_opencloud_auth(direct_url) else None,
            )
            _FILEMESH_BYTES_CACHE[cache_key] = data
            return data
        except Exception as exc:  # pragma: no cover
            errors.append(str(exc))

    if asset_id is None:
        raise ValueError(
            f"could not determine mesh asset id from content '{content_id}'"
        )

    delivery_urls = [
        # New OpenCloud endpoint (required since April 2025 for authenticated access)
        f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}",
        # Legacy assetdelivery (kept as fallback for temporarily-exempt public assets)
        f"https://assetdelivery.roblox.com/v1/asset/?id={asset_id}",
        f"https://assetdelivery.roblox.com/v2/asset/?id={asset_id}",
    ]

    # Try authenticated first (no-op when not logged in)
    if auth_headers:
        data = _try_delivery_urls(delivery_urls, asset_id, timeout, errors, auth_headers)
        if data is not None:
            _FILEMESH_BYTES_CACHE[cache_key] = data
            return data

    # Unauthenticated attempt is only useful on the legacy endpoints.
    unauthenticated_urls = [
        url for url in delivery_urls if not _uses_opencloud_auth(url)
    ]
    data = _try_delivery_urls(unauthenticated_urls, asset_id, timeout, errors)
    if data is not None:
        _FILEMESH_BYTES_CACHE[cache_key] = data
        return data

    legacy_url = f"https://www.roblox.com/asset/?id={asset_id}"
    try:
        data = _fetch_url_bytes(legacy_url, timeout=timeout)
        if not _looks_like_filemesh_payload(data):
            raise ValueError(
                f"legacy payload was not a filemesh (preview={_preview_bytes(data)!r})"
            )
        _FILEMESH_BYTES_CACHE[cache_key] = data
        return data
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
    for index, bone in enumerate(bones):
        name_index = bone.get("bone_name_index", -1)
        bone["name"] = bone_names[index] if index < len(bone_names) else None
        bone["resolved_name"] = bone_names[index] if index < len(bone_names) else None
        if isinstance(name_index, int) and 0 <= name_index < len(bone_names):
            bone["name"] = bone_names[index] if index < len(bone_names) else bone_names[name_index]
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
    for _ in range(num_verts):
        position = unpack_position("<3f", data, offset)
        normal = unpack_position("<3f", data, offset + 12) if has_normal else None
        uv = unpack_position("<2f", data, offset + 24) if has_uv else None
        vertices_append(
            {
                "position": position,
                "normal": normal,
                "uv": uv,
            }
        )
        offset += vertex_size
    return vertices, offset


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
    vertex_weights: List[Dict[str, float]] = [{} for _ in range(num_verts)]
    if not skinning or not subsets or not bone_names:
        return vertex_weights

    for subset in subsets:
        start = subset["verts_begin"]
        end = min(num_verts, start + subset["verts_length"])
        if start >= end:
            continue
        subset_bone_names = []
        for bone_index in subset["bone_indices"][: subset["num_bone_indices"]]:
            if bone_index == 0xFFFF or bone_index >= len(bone_names):
                subset_bone_names.append(None)
            else:
                subset_bone_names.append(bone_names[bone_index])
        for vertex_index in range(start, end):
            subset_indices, bone_weights = skinning[vertex_index]
            resolved: Dict[str, float] = {}
            total_weight = 0
            for subset_index, raw_weight in zip(subset_indices, bone_weights):
                if raw_weight <= 0:
                    continue

                if subset_index >= len(subset_bone_names):
                    continue

                bone_name = subset_bone_names[subset_index]
                if bone_name is None:
                    continue

                resolved[bone_name] = resolved.get(bone_name, 0.0) + raw_weight
                total_weight += raw_weight

            if total_weight > 0:
                inverse_total_weight = 1.0 / total_weight
                vertex_weights[vertex_index] = {
                    bone_name: weight * inverse_total_weight for bone_name, weight in resolved.items()
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
        "vertex_weights": [{} for _ in range(num_verts)],
        "bone_names": [],
        "has_skinning": False,
        "lod_type": None,
        "num_high_quality_lods": 0,
        "lod_offsets": lod_offsets,
        **_empty_facs_metadata(),
    }


def _infer_v4_vertex_size(total_len: int, offset: int, num_verts: int, num_faces: int, num_lod_offsets: int, num_bones: int, bone_names_size: int, num_subsets: int, facs_size: int = 0) -> int:
    tail_bytes = (num_faces * 12) + (num_lod_offsets * 4) + (num_bones * _BONE_STRUCT.size) + bone_names_size + (num_subsets * _SUBSET_STRUCT.size) + facs_size
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
    name_table = data[offset : offset + bone_names_size]
    offset += bone_names_size
    bone_names = _decode_name_table(name_table, bones)
    bones = _attach_bone_names(bones, bone_names)
    subsets, offset = _parse_subsets(data, offset, num_subsets)
    vertex_weights = _resolve_vertex_weights(num_verts, skinning, subsets, bone_names)
    facs_metadata = _empty_facs_metadata()
    if facs_size > 0:
        if facs_format == 1:
            facs_metadata = _parse_facs_data(data[offset : offset + facs_size])
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
    global _DRACO_DLL
    if _DRACO_DLL is not _DRACO_DLL_UNINITIALIZED:
        return _DRACO_DLL

    dll_path = _get_blender_draco_dll_path()
    if dll_path is None:
        _DRACO_DLL = None
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
    except Exception:
        _DRACO_DLL = None
        return None

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
    values = struct.unpack_from(f"<{vertex_count * components}f", buffer.raw, 0)
    return [tuple(values[index : index + components]) for index in range(0, len(values), components)]


def _decode_draco_coremesh_v2(chunk: bytes) -> Optional[Tuple[List[dict], List[Tuple[int, int, int]], int]]:
    if len(chunk) < 4:
        raise ValueError("truncated v7 draco coremesh header")

    draco_bitstream_size = struct.unpack_from("<I", chunk, 0)[0]
    if 4 + draco_bitstream_size > len(chunk):
        raise ValueError("truncated v7 draco coremesh payload")

    dll = _load_blender_draco_dll()
    if dll is None:
        raise RuntimeError("draco decoder is unavailable for version 7 coremesh")

    bitstream = chunk[4 : 4 + draco_bitstream_size]
    bitstream_buffer = ctypes.create_string_buffer(bitstream, len(bitstream))
    decoder = dll.decoderCreate()
    if not decoder:
        raise RuntimeError("failed to create draco decoder for version 7 coremesh")

    try:
        if not dll.decoderDecode(decoder, bitstream_buffer, len(bitstream)):
            raise RuntimeError("failed to decode draco bitstream for version 7 coremesh")

        vertex_count = int(dll.decoderGetVertexCount(decoder))
        index_count = int(dll.decoderGetIndexCount(decoder))
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

        if positions is None:
            raise RuntimeError("draco coremesh did not expose a position attribute")

        faces: List[Tuple[int, int, int]] = []
        if index_count > 0 and dll.decoderReadIndices(decoder, _GLTF_COMPONENT_TYPE_UINT32):
            byte_length = int(dll.decoderGetIndicesByteLength(decoder))
            if byte_length > 0:
                index_buffer = ctypes.create_string_buffer(byte_length)
                dll.decoderCopyIndices(decoder, index_buffer)
                flat_indices = struct.unpack_from(f"<{index_count}I", index_buffer.raw, 0)
                faces = [
                    (flat_indices[index], flat_indices[index + 1], flat_indices[index + 2])
                    for index in range(0, len(flat_indices) - 2, 3)
                ]

        vertices = []
        for index, position in enumerate(positions):
            vertices.append(
                {
                    "position": position,
                    "normal": normals[index] if normals and index < len(normals) else None,
                    "uv": uvs[index] if uvs and index < len(uvs) else None,
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
    name_table = chunk[offset : offset + name_table_size]
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

    while offset + 16 <= len(data):
        chunk_type_raw = data[offset : offset + 8]
        chunk_type = chunk_type_raw.decode("ascii", errors="ignore").rstrip("\0 ")
        chunk_version, chunk_size = struct.unpack_from("<II", data, offset + 8)
        chunk_data = data[offset + 16 : offset + 16 + chunk_size]
        offset += 16 + chunk_size

        if chunk_type == "COREMESH" and chunk_version == 1:
            vertices, faces, num_vertices = _parse_coremesh_v1(chunk_data)
        elif chunk_type == "COREMESH" and chunk_version == 2:
            vertices, faces, num_vertices = _decode_draco_coremesh_v2(chunk_data)
        elif chunk_type == "SKINNING" and chunk_version == 1:
            skinning_data = _parse_skinning_chunk(chunk_data)
            num_vertices = max(num_vertices, skinning_data["num_vertices"])
            vertex_weights = skinning_data["vertex_weights"]
            bone_names = skinning_data["bone_names"]
            bones = skinning_data.get("bones") or []
            has_skinning = skinning_data["has_skinning"]
        elif chunk_type == "LODS" and chunk_version == 1:
            lod_metadata = _parse_lods_chunk(chunk_data)
        elif chunk_type == "FACS" and chunk_version == 1:
            facs_metadata = _parse_facs_chunk(chunk_data)

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
        "vertex_weights": vertex_weights,
        "bone_names": bone_names,
        "bones": bones,
        "has_skinning": has_skinning,
        **lod_metadata,
        **facs_metadata,
    }


def parse_filemesh(data: bytes) -> dict:
    """Parse enough of a FileMesh to reconstruct vertex weights in Blender."""
    version, offset = _parse_version_header(data)
    data = _normalize_filemesh_bytes(data)

    if version.startswith("version 1"):
        raise ValueError("ascii v1 filemeshes do not contain skinning data")
    if version.startswith("version 2") or version.startswith("version 3"):
        return _parse_v2_or_v3(data, version, offset)
    if version.startswith("version 4") or version.startswith("version 5"):
        return _parse_v4_or_v5(data, version, offset)
    if version.startswith("version 6") or version.startswith("version 7"):
        return _parse_v6_or_v7(data, version, offset)

    raise ValueError(f"unsupported filemesh version '{version}' (preview={_preview_bytes(data)!r})")


def fetch_and_parse_filemesh(content_id, timeout: float = 15.0) -> dict:
    """Fetch and parse FileMesh data with simple in-process caching."""
    cache_key = str(content_id)
    if cache_key in _FILEMESH_CACHE:
        return _FILEMESH_CACHE[cache_key]

    raw = fetch_filemesh_bytes(content_id, timeout=timeout)
    parsed = parse_filemesh(raw)
    _FILEMESH_CACHE[cache_key] = parsed
    return parsed