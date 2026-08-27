"""Texture fetching and material construction for rbxm-imported rigs.

Handles the two texture systems that appear on avatars:
  * SurfaceAppearance (PBR ColorMap etc.) — e.g. accessories/swords
  * Face decals (classic head smile)
as well as per-part base colors. Images are fetched via the AssetDelivery API
(rbxassetid://) or resolved from the local Roblox install (rbxasset://).
"""

from __future__ import annotations

from array import array
from contextlib import contextmanager
from hashlib import sha1
import io
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import bpy

from .filemesh import (
    _resolve_rbxasset_path,
    _extract_locations_from_payload,
    _fetch_url_bytes,
    _fetch_url_response,
    _get_auth_headers,
    is_trusted_roblox_asset_url,
    extract_asset_id,
)


_IMAGE_CACHE: dict = {}
_IMAGE_BYTES_CACHE: dict = {}
_IMAGE_RAW_CACHE: dict = {}
_PAYLOAD_IMAGE_CACHE: dict = {}
_IMAGE_PREFETCH_FAILURES: dict = {}
# Transient network errors (timeouts, 429s, auth blips) must not condemn an
# asset to a black material for the whole session: failures expire and a
# finalize-time retry pass gives them one more shot per import.
_PREFETCH_FAILURE_TTL_SECONDS = 120.0
_PREFETCH_FAILED_REFS: set = set()
_PART_MATERIAL_CACHE: dict = {}
_OBJECT_TINT_BUILTIN_CACHE: dict = {}
_DEFER_TEXTURE_IMAGES = 0
_IMAGE_RAW_CACHE_MAX = 24
_MAX_IMAGE_BYTES = 64 * 1024 * 1024

# Bumped whenever the material graph conventions change (Attribute node ->
# Vertex Color node, gamma chain removal, tint wiring, SA overlay
# compositing) or cached datablocks must be invalidated (tint name
# collision fix, overlay alpha copy, opaque overlay blend fix, colour
# binds forcing sRGB, buffer-safe colorspace flips, role-aware fetch
# colorspace, variant colour maps Non-Color, per-role image views).
# Cached materials from an earlier
# add-on build or a previous import in the same Blender session carry this
# stamp; a mismatch forces an in-place rebuild so old datablocks never
# outlive a graph change.
_MATERIAL_GRAPH_VERSION = 21

# ShaderNodeMix arrived in Blender 3.4; the legacy 3.x build needs the
# MixRGB equivalent (same blend modes, Fac instead of Factor).
try:
    import bpy as _bpy
    _HAS_MIX_NODE = hasattr(_bpy.types, "ShaderNodeMix")
except Exception:
    _HAS_MIX_NODE = True
_LINK_ARGS_REVERSED = not _HAS_MIX_NODE


def _graph_current(material) -> bool:
    """True when a cached material's graph was built by this code version."""
    try:
        return int(material.get("RBXMaterialGraphVersion", 0)) == _MATERIAL_GRAPH_VERSION
    except (TypeError, ValueError):
        return False


def _entry_texture_instances(entry: dict):
    """Every child Texture instance layer, bottom first."""
    found = entry.get("texture_instances")
    if found:
        return [ti for ti in found if isinstance(ti, dict) and ti.get("texture")]
    ti = entry.get("texture_instance")
    if isinstance(ti, dict) and ti.get("texture"):
        return [ti]
    return []


def _entry_surface_appearances(entry: dict):
    """Every SurfaceAppearance layer, bottom first."""
    found = entry.get("surface_appearances")
    if found:
        return [sa for sa in found if isinstance(sa, dict)]
    sa = entry.get("surface_appearance")
    return [sa] if isinstance(sa, dict) else []


def _layer_tag(index: int) -> str:
    """Node-name suffix for composite layer ``index`` (0 keeps legacy names)."""
    return "" if index == 0 else f".{index}"


def _layer_node_name(base: str, index: int) -> str:
    return base if index == 0 else f"{base}.{index}"


# One image datablock can be shared by several materials (content-addressed
# sharing).  A datablock has ONE colorspace, so colour and data chains can
# never sample the same datablock: the first role to bind owns it, and the
# second gets a dedicated pixel copy in its own colorspace.
_IMAGE_COLOR_USES: dict = {}
_IMAGE_DATA_USES: dict = {}
_ROLE_COPY_CACHE: dict = {}  # (image.name, colorspace) -> (copy, pixel fingerprint)


def _image_content_fingerprint(image):
    """Cheap identity hash of the current pixel buffer.

    Generated images can be REMOVED and a same-named, same-sized datablock
    created later in the session; name+size alone cannot tell them apart.
    Sampling ~128 pixels keeps the check cheap while making stale-cache
    hits practically impossible.
    """
    try:
        image.update()
        pixels = image.pixels
        size = len(pixels)
        if size <= 0:
            return None
        sample = []
        step = max(1, size // 512)
        for index in range(0, size, step):
            sample.append(round(float(pixels[index]), 4))
        sample.append(round(float(image.size[0]), 4))
        sample.append(round(float(image.size[1]), 4))
        return tuple(sample)
    except (ReferenceError, RuntimeError):
        return None


def _role_image_copy(image, target: str):
    """A pixel-identical copy of ``image`` bound to ``target`` colorspace.

    Copies are cached per (name, colorspace): shared payloads bound by both
    roles need exactly one duplicate per asset.  The cache validates the
    SOURCE's pixel fingerprint on every hit so a later, same-named image
    can never be served the previous image's pixels.  The colorspace is set
    BEFORE the pixel upload (post-upload assignment wipes generated image
    buffers).
    """
    if image is None:
        return None
    try:
        key = (image.name, target)
        cached = _ROLE_COPY_CACHE.get(key)
        if cached is not None:
            cached_copy, cached_fp = cached
            try:
                if cached_copy.name in bpy.data.images and cached_copy.size[:2] == image.size[:2]:
                    current_fp = _image_content_fingerprint(image)
                    if current_fp is not None and current_fp == cached_fp:
                        return cached_copy
            except ReferenceError:
                pass
        image.update()
        width, height = (int(value) for value in image.size[:2])
        try:
            is_float = bool(image.is_float)
        except (AttributeError, ReferenceError):
            is_float = False
        suffix = "srgb" if target == "sRGB" else "data"
        copy = bpy.data.images.new(
            f"{image.name}.{suffix}", width=width, height=height, alpha=True,
            float_buffer=is_float,
        )
        try:
            copy.colorspace_settings.name = target
        except Exception:
            pass
        copy.pixels[:] = image.pixels[:]
        copy.update()
        _ROLE_COPY_CACHE[key] = (copy, _image_content_fingerprint(image))
        return copy
    except (ReferenceError, RuntimeError):
        return None


def _color_image_view(image):
    """The datablock a colour bind should sample.

    Claims the content-shared original for the colour chain (sRGB); when a
    data bind got there first, returns a dedicated sRGB copy instead.
    """
    if image is None:
        return None
    try:
        if image.as_pointer() in _IMAGE_DATA_USES:
            return _role_image_copy(image, "sRGB")
        _IMAGE_COLOR_USES[image.as_pointer()] = image
        _set_image_colorspace(image, "sRGB")
        return image
    except ReferenceError:
        return None


def _data_image_view(image):
    """The datablock a data bind should sample (mirror of _color_image_view)."""
    if image is None:
        return None
    try:
        if image.as_pointer() in _IMAGE_COLOR_USES:
            return _role_image_copy(image, "Non-Color")
        _IMAGE_DATA_USES[image.as_pointer()] = image
        _set_image_colorspace(image, "Non-Color")
        return image
    except ReferenceError:
        return None


def _set_image_colorspace(image, target: str) -> None:
    """Set ``image``'s colorspace without destroying its pixel buffer.

    On Blender 4.1–5.1 ANY colorspace assignment on an unpacked GENERATED
    image wipes its RGB buffer — even assigning the value it already has
    (verified in headless renders).  File-backed and packed images survive
    a flip because the buffer is re-read from source, so unpacked
    generated images are packed once before the flip; if packing fails the
    assignment is skipped rather than zeroing the texture.
    """
    if image is None:
        return
    try:
        if image.colorspace_settings.name == target:
            # A packed generated image went through a post-upload flip at
            # some point; refresh its display texture even on the no-op path
            # so a stale Eevee GPU texture never survives a session. Blender
            # 5.2 refreshes this itself; calling update() for every reused
            # node bind is measurable main-thread churn on large imports.
            if (
                bpy.app.version[:2] < (5, 2)
                and getattr(image, "source", "") == "GENERATED"
                and image.packed_file is not None
            ):
                image.update()
            return
        if getattr(image, "source", "") == "GENERATED" and image.packed_file is None:
            image.update()
            image.pack()
        image.colorspace_settings.name = target
        # Refresh the display/GPU texture: Eevee keeps a stale (zeroed)
        # texture after a colorspace flip on a generated image, while Cycles
        # re-reads the CPU buffer and looks fine — the classic
        # black-in-Eevee-only symptom.
        image.update()
    except ReferenceError:
        pass
    except Exception:
        pass


def _overlay_layer_registry(material) -> list:
    """The layer registry stamped onto the material by the build pass."""
    try:
        raw = material.get("RBXOverlayLayers")
        if not raw:
            return []
        return json.loads(raw)
    except (TypeError, ValueError, ReferenceError):
        return []


def _live_image(image):
    """Return ``image`` when it still references a live bpy datablock.

    Blender can purge unused image datablocks between imports (or another
    script can remove them) while these module-level caches keep their old
    RNA wrappers.  Any attribute access on such a wrapper raises
    ``ReferenceError: StructRNA of type Image has been removed``, so every
    cache hit must be revalidated before use.
    """
    if image is None:
        return None
    try:
        if image.name in bpy.data.images:
            return image
    except ReferenceError:
        pass
    return None


def _live_material(material):
    """Same liveness revalidation as _live_image, for cached materials."""
    if material is None:
        return None
    try:
        if material.name in bpy.data.materials:
            return material
    except ReferenceError:
        pass
    return None


def _texture_asset_key(texture_ref: str) -> str:
    asset_id = extract_asset_id(texture_ref)
    return f"asset:{asset_id}" if asset_id is not None else str(texture_ref)


@contextmanager
def defer_texture_image_loading():
    """Temporarily build materials without decoding image assets."""
    global _DEFER_TEXTURE_IMAGES
    _DEFER_TEXTURE_IMAGES += 1
    try:
        yield
    finally:
        _DEFER_TEXTURE_IMAGES -= 1


def release_import_byte_cache() -> None:
    """Drop downloaded source bytes once Blender image datablocks exist."""
    _IMAGE_BYTES_CACHE.clear()
    _IMAGE_RAW_CACHE.clear()
    _PAYLOAD_IMAGE_CACHE.clear()
    _IMAGE_PREFETCH_FAILURES.clear()
    _PREFETCH_FAILED_REFS.clear()


def _thumbnail_location(payload: bytes) -> Optional[str]:
    """Extract the public image thumbnail URL returned by thumbnails.roblox.com."""
    try:
        data = json.loads(payload.decode("utf-8"))
        row = (data.get("data") or [None])[0]
        url = row.get("imageUrl") if isinstance(row, dict) else None
        return url if isinstance(url, str) and url else None
    except Exception:
        return None


def _looks_like_image_payload(data: bytes) -> bool:
    """True when ``data`` starts with a known image magic.

    Accepting every format Blender's loader decodes means a valid WebP/GIF/
    BMP served directly (no redirect) is kept instead of discarded as a
    non-image response.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8":
        return True
    if data[:4] in (b"GIF8", b"BM"):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _fetch_image_bytes(
    texture_ref: str,
    auth_headers: Optional[dict] = None,
    timeout: float = 15.0,
    timing: Optional[dict] = None,
) -> Optional[bytes]:
    """Fetch image bytes for a texture ref, sending auth so private assets resolve."""
    from .filemesh import _uses_opencloud_auth

    def record(name, started):
        if timing is not None:
            timing[name] = timing.get(name, 0.0) + (time.perf_counter() - started)

    cache_key = str(texture_ref)
    asset_key = _texture_asset_key(texture_ref)
    cached = _IMAGE_BYTES_CACHE.get(cache_key) or _IMAGE_BYTES_CACHE.get(asset_key)
    if cached is not None:
        _IMAGE_BYTES_CACHE[cache_key] = cached
        return cached
    if not _prefetch_failure_is_stale(cache_key) and not _prefetch_failure_is_stale(asset_key):
        return None
    # An expired failure gets one fresh attempt instead of a session-long
    # poison pill.
    _IMAGE_PREFETCH_FAILURES.pop(cache_key, None)
    _IMAGE_PREFETCH_FAILURES.pop(asset_key, None)

    asset_id = extract_asset_id(texture_ref)
    # ``None`` means serial/main-thread use.  Prefetch workers receive a
    # bearer resolved before they started, avoiding concurrent token refreshes.
    auth = _get_auth_headers() if auth_headers is None else auth_headers

    urls = []
    if texture_ref.lower().startswith(("http://", "https://")) and is_trusted_roblox_asset_url(texture_ref):
        urls.append(texture_ref)
    if asset_id is not None:
        if auth:
            urls.insert(
                0,
                f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}",
            )
        urls.extend((
            f"https://assetdelivery.roblox.com/v2/assetId/{asset_id}",
            f"https://assetdelivery.roblox.com/v1/asset/?id={asset_id}",
        ))

    result = None
    for url in urls:
        headers = auth if _uses_opencloud_auth(url) else None
        try:
            started = time.perf_counter()
            response, payload = _fetch_url_response(
                url, timeout=timeout, follow_redirects=False, extra_headers=headers,
                max_bytes=_MAX_IMAGE_BYTES,
            )
            record("delivery", started)
            location = response.headers.get("Location")
            locations = [location] if location else _extract_locations_from_payload(payload)
            if locations:
                started = time.perf_counter()
                result = _fetch_url_bytes(
                    locations[0],
                    timeout=timeout,
                    max_bytes=_MAX_IMAGE_BYTES,
                    trim_mesh_header=False,
                )
                record("cdn", started)
                break
            # No redirect: the payload may itself be the image.
            if _looks_like_image_payload(payload):
                result = payload
                break
        except Exception as exc:
            print(f"[RbxTexture] image fetch '{url}' failed: {exc}")
            continue
    # Public image thumbnails remain anonymously available even where Roblox's
    # raw asset-delivery endpoint now requires OAuth.  This is a visual
    # fallback (not suitable for mesh decoding), but gives old Sky assets a
    # usable scene world and viewport sky without forcing a login.
    if result is None and asset_id is not None:
        thumbnail_url = (
            "https://thumbnails.roblox.com/v1/assets?"
            f"assetIds={asset_id}&returnPolicy=PlaceHolder&size=768x432&"
            "format=Png&isCircular=false"
        )
        try:
            started = time.perf_counter()
            _, payload = _fetch_url_response(
                thumbnail_url, timeout=timeout, follow_redirects=True,
                max_bytes=_MAX_IMAGE_BYTES,
            )
            record("thumbnail", started)
            image_url = _thumbnail_location(payload)
            if image_url:
                started = time.perf_counter()
                result = _fetch_url_bytes(
                    image_url,
                    timeout=timeout,
                    max_bytes=_MAX_IMAGE_BYTES,
                    trim_mesh_header=False,
                )
                record("cdn", started)
                print(f"[RbxTexture] using thumbnail fallback for asset {asset_id}")
        except Exception as exc:
            print(f"[RbxTexture] thumbnail fallback for asset {asset_id} failed: {exc}")
    if result is not None:
        _IMAGE_BYTES_CACHE[cache_key] = result
        _IMAGE_BYTES_CACHE[asset_key] = result
    return result


def _prefetch_failure_is_stale(key) -> bool:
    """True when ``key`` has no failure record or its failure expired."""
    stamp = _IMAGE_PREFETCH_FAILURES.get(key)
    if stamp is None:
        return True
    try:
        return time.monotonic() - float(stamp) > _PREFETCH_FAILURE_TTL_SECONDS
    except (TypeError, ValueError):
        return True


def retry_failed_texture_fetches() -> int:
    """Give each failed prefetch one more attempt (finalize-time pass).

    Transient failures would otherwise finalize materials without their
    images — the black-material symptom.  Retried refs that fail again are
    re-poisoned with a fresh timestamp, so the next import's prefetch can
    try once more.
    """
    refs = list(_PREFETCH_FAILED_REFS)
    _PREFETCH_FAILED_REFS.clear()
    recovered = 0
    for ref in refs:
        if not ref:
            continue
        _IMAGE_PREFETCH_FAILURES.pop(str(ref), None)
        _IMAGE_PREFETCH_FAILURES.pop(_texture_asset_key(ref), None)
        try:
            data = _fetch_image_bytes(ref)
        except Exception:
            data = None
        if data is not None:
            recovered += 1
        else:
            _IMAGE_PREFETCH_FAILURES[str(ref)] = time.monotonic()
            _IMAGE_PREFETCH_FAILURES[_texture_asset_key(ref)] = time.monotonic()
            _PREFETCH_FAILED_REFS.add(str(ref))
    if recovered:
        print(f"[RbxTexture] retry pass recovered {recovered} texture asset(s)")
    return recovered


def entry_texture_bytes_ready(entry: dict, prefetch_done: bool = False) -> bool:
    """True when every texture this entry needs is locally available.

    Incremental hydration uses this to refresh materials while the prefetch
    worker is still fetching other assets: a miss only defers the entry, and
    known prefetch failures count as ready so the entry is finalized without
    its image instead of retrying forever.
    """
    if not prefetch_done:
        try:
            from . import clothing

            if clothing.is_clothing_limb(str(entry.get("name") or "")):
                # Clothing crops depend on shirt/pants/face bytes prefetched
                # globally, not on any per-entry ref; wait for the prefetch.
                return False
        except Exception:
            pass
    refs = []
    try:
        refs.extend(builtin_material_texture_refs(entry))
    except Exception:
        pass
    for ti in _entry_texture_instances(entry):
        if ti.get("texture"):
            refs.append(ti["texture"])
    for sa in _entry_surface_appearances(entry):
        for key in ("color_map", "normal_map", "roughness_map", "metalness_map"):
            if sa.get(key):
                refs.append(sa[key])
    for value in (
        entry.get("texture_id"),
        (entry.get("face_decal") or {}).get("texture"),
    ):
        if value:
            refs.append(value)
    for ref in refs:
        if not ref:
            continue
        asset_key = _texture_asset_key(ref)
        if (
            asset_key in _IMAGE_BYTES_CACHE
            or str(ref) in _IMAGE_BYTES_CACHE
            or asset_key in _IMAGE_PREFETCH_FAILURES
            or str(ref) in _IMAGE_PREFETCH_FAILURES
        ):
            continue
        if str(ref).lower().startswith("rbxasset://"):
            continue  # local files are instant
        return False
    return True


def prefetch_texture_bytes(
    texture_refs,
    max_workers: int = 12,
    timeout: float = 4.0,
    auth_headers: Optional[dict] = None,
    raw_decode_refs=(),
    asset_registry=None,
) -> None:
    """Warm the image-bytes cache concurrently (bpy-free, thread-safe).
    bpy Image creation still happens later on the main thread, but that
    part is milliseconds compared to the network roundtrips.
    """
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    raw_decode_keys = {
        _texture_asset_key(ref) for ref in raw_decode_refs if ref
    }
    refs = []
    seen = set()
    for ref in texture_refs:
        if not ref:
            continue
        asset_id = extract_asset_id(ref)
        key = f"asset:{asset_id}" if asset_id is not None else str(ref)
        if asset_registry is not None:
            asset_registry.register(key, str(ref), "texture")
        if key in seen:
            continue
        if key in _IMAGE_BYTES_CACHE:
            if asset_registry is not None:
                asset_registry.finish(key)
            continue
        if key in _IMAGE_CACHE or str(ref) in _IMAGE_CACHE:
            # The image datablock already exists (a re-import in this
            # session); re-downloading its bytes would be pure waste.
            if asset_registry is not None:
                asset_registry.finish(key)
            continue
        if key.lower().startswith("rbxasset://"):
            if asset_registry is not None:
                asset_registry.finish(key)
            continue  # local files are instant
        seen.add(key)
        refs.append(ref)
    if not refs:
        return

    # Deferred place imports call this from a background coordinator. OAuth
    # refresh mutates the saved refresh token, so use caller-resolved headers.
    auth_headers = _get_auth_headers() if auth_headers is None else auth_headers

    def _warm_unlimited(ref):
        timing = {}
        key = _texture_asset_key(ref)
        if asset_registry is not None:
            asset_registry.mark_fetching(key)
        started = time.perf_counter()
        result = _fetch_image_bytes(ref, auth_headers, timeout=timeout, timing=timing)
        timing["fetch"] = time.perf_counter() - started
        if result is None:
            _IMAGE_PREFETCH_FAILURES[str(ref)] = time.monotonic()
            _IMAGE_PREFETCH_FAILURES[_texture_asset_key(ref)] = time.monotonic()
            _PREFETCH_FAILED_REFS.add(str(ref))
            if asset_registry is not None:
                asset_registry.finish(key, error="asset unavailable or request timed out", timings=timing)
        else:
            # Pre-write the hash temp file in the worker. Do NOT expand every
            # download into a float32 NumPy buffer here: a 1024² image becomes
            # 16 MiB, then gets copied again into bpy, and most prefetched
            # maps are never instantiated. Blender's file loader decodes the
            # images that are actually needed and preserves its own
            # colourspace handling.
            started = time.perf_counter()
            _ensure_image_temp_file(result)
            timing["cache_write"] = time.perf_counter() - started
            if _texture_asset_key(ref) in raw_decode_keys:
                started = time.perf_counter()
                _warm_raw_pixels(result)
                timing["native_decode"] = time.perf_counter() - started
            if asset_registry is not None:
                # Bytes already have one owner in _IMAGE_BYTES_CACHE. The
                # registry transports readiness, not data; retaining another
                # reference here doubles their lifetime after cache release.
                asset_registry.finish(key, timings=timing)
            timing["bytes"] = len(result)
        timing["assets"] = 1
        return timing

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        totals = {}
        for timing in pool.map(_warm_unlimited, refs):
            for name, value in timing.items():
                totals[name] = totals.get(name, 0.0) + value
    return totals


def _image_payload_key(data: bytes) -> str:
    """Content-addressed key for a texture payload (stable across sessions)."""
    return sha1(data).hexdigest()


def _ensure_image_temp_file(data: bytes) -> Path:
    """Write source bytes to a hash-named temp file (safe in prefetch workers)."""
    tmp = Path(tempfile.gettempdir()) / f"rbx_tex_{_image_payload_key(data)}.png"
    if tmp.exists():
        return tmp
    part = tmp.with_suffix(".png.part")
    try:
        part.write_bytes(data)
        os.replace(part, tmp)
    except OSError:
        try:
            part.unlink()
        except OSError:
            pass
        try:
            tmp.write_bytes(data)
        except OSError:
            pass
    return tmp


def _load_image_from_bytes(data: bytes, name: str, non_color: bool = False):
    """Load a bpy image from raw bytes: worker-decoded pixels when possible.

    Payload-keyed sharing preserves the dedup that the old temp-file
    check_existing path provided: identical bytes under different refs
    resolve to one datablock."""
    key = _image_payload_key(data)
    existing = _PAYLOAD_IMAGE_CACHE.get(key)
    if existing is not None:
        try:
            _ = existing.name
            return existing
        except ReferenceError:
            _PAYLOAD_IMAGE_CACHE.pop(key, None)
    raw = _IMAGE_RAW_CACHE.get(key)
    if raw is not None:
        image = _image_from_raw_pixels(name, *raw, non_color=non_color)
        # The decoded floats are ~16MB per 1024x1024 map and are only a
        # decode cache: once the bpy image exists they are pure overhead.
        _IMAGE_RAW_CACHE.pop(key, None)
    else:
        # No decoder covered this payload (non-PNG without Pillow): fall
        # back to a bpy decode of the pre-written temp file. Decoding again
        # here to warm the cache would waste main-thread time on an image we
        # just built.
        tmp = _ensure_image_temp_file(data)
        image = bpy.data.images.load(str(tmp), check_existing=True)
        # File-backed flips are safe (the buffer re-reads from disk), so the
        # role colorspace can be applied after the load.
        _set_image_colorspace(image, "Non-Color" if non_color else "sRGB")
    if image is not None:
        _PAYLOAD_IMAGE_CACHE[key] = image
    return image


_PILLOW_STATE = {"tried": False, "ok": False}


def _import_pillow():
    """Return True when a preinstalled Pillow is importable (mac/linux)."""
    if _PILLOW_STATE["tried"]:
        return _PILLOW_STATE["ok"]
    _PILLOW_STATE["tried"] = True
    try:
        import PIL  # noqa: F401
        _PILLOW_STATE["ok"] = True
    except ImportError:
        pass
    return _PILLOW_STATE["ok"]


def _decode_to_raw_pixels(data: bytes):
    """Decode image bytes to (width, height, alpha_flag, bottom-up RGBA).

    PNG payloads go through the pure-Python decoder (pngdec); everything
    else uses a preinstalled Pillow when available.  Returns None when no
    decoder is available for this payload (callers then fall back to
    Blender's native file loader)."""
    if len(data) > _MAX_IMAGE_BYTES:
        return None
    try:
        import numpy as np  # bundled with Blender
    except ImportError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        from . import pngdec

        decoded = pngdec.decode_png(data)
        if decoded is not None:
            width, height, has_alpha, rgba_u8 = decoded
            rgba = rgba_u8.astype(np.float32)
            rgba *= 1.0 / 255.0
            # bpy image rows are bottom-up; the decoder returns top-down file order.
            return (
                width,
                height,
                1 if has_alpha else 0,
                np.ascontiguousarray(rgba[::-1].reshape(-1)),
            )
    if not _import_pillow():
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image.load()
            mode = image.mode
            has_alpha = "A" in mode or (
                mode == "P" and "transparency" in image.info
            )
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > (1 << 26):
                return None
            if mode in ("I;16", "I;16L", "I;16B", "I;16N") or mode == "I":
                # High-bit-depth gray: Pillow's convert() truncates to 8-bit.
                # Blender's reader linearizes 16-bit sources through the sRGB
                # transfer curve on load (unlike 8-bit, which stays raw), so
                # reproduce that here for pixel parity.
                scale = 1.0 / 65535.0 if mode != "I" else 1.0 / 4294967295.0
                base = np.asarray(image, dtype=np.float32) * scale
                base = np.where(
                    base <= 0.04045,
                    base * (1.0 / 12.92),
                    ((base + 0.055) * (1.0 / 1.055)) ** 2.4,
                )
                rgba = np.repeat(base[:, :, None], 3, axis=2)
                pad = np.ones((height, width, 1), dtype=np.float32)
                rgba = np.concatenate([rgba, pad], axis=2)
                has_alpha = False
            else:
                converted = image.convert("RGBA" if has_alpha else "RGB")
                rgba = np.asarray(converted, dtype=np.float32) * (1.0 / 255.0)
        if rgba.shape[2] == 3:
            pad = np.ones((height, width, 1), dtype=np.float32)
            rgba = np.concatenate([rgba, pad], axis=2)
        # bpy image rows are bottom-up; Pillow returns top-down file order.
        alpha_flag = 1 if has_alpha else 0
        return width, height, alpha_flag, np.ascontiguousarray(rgba[::-1].reshape(-1))
    except Exception:
        return None


def _warm_raw_pixels(data: bytes) -> None:
    """Decode and cache raw pixels (worker-side where possible).

    Bounded: each decoded map is ~16MB of floats, and a place import can
    reference hundreds of maps. The cache is only a decode shortcut — the
    bpy-side fallback re-reads the pre-written temp file when an entry is
    absent.
    """
    if len(_IMAGE_RAW_CACHE) >= _IMAGE_RAW_CACHE_MAX:
        return
    key = _image_payload_key(data)
    if key in _IMAGE_RAW_CACHE:
        return
    decoded = _decode_to_raw_pixels(data)
    if decoded is not None:
        _IMAGE_RAW_CACHE[key] = decoded


def prefetched_raw_pixels(texture_ref: str):
    """Return worker-decoded pixels for a ref without consuming the cache."""
    data = _IMAGE_BYTES_CACHE.get(str(texture_ref))
    if data is None:
        data = _IMAGE_BYTES_CACHE.get(_texture_asset_key(texture_ref))
    return _IMAGE_RAW_CACHE.get(_image_payload_key(data)) if data else None


def release_prefetched_raw_pixels(texture_ref: str) -> None:
    data = _IMAGE_BYTES_CACHE.get(str(texture_ref)) or _IMAGE_BYTES_CACHE.get(_texture_asset_key(texture_ref))
    if data:
        _IMAGE_RAW_CACHE.pop(_image_payload_key(data), None)


def material_texture_refs(entry: dict) -> tuple:
    """Unique image refs consumed by one deferred material."""
    refs = list(builtin_material_texture_refs(entry))
    refs.extend(ti.get("texture") for ti in _entry_texture_instances(entry))
    for surface in _entry_surface_appearances(entry):
        refs.extend(surface.get(key) for key in (
            "color_map", "normal_map", "roughness_map", "metalness_map",
        ))
    refs.extend((entry.get("texture_id"), (entry.get("face_decal") or {}).get("texture")))
    try:
        from . import clothing

        part_name = str(entry.get("name") or "")
        if clothing.is_clothing_limb(part_name):
            refs.extend(
                clothing.context_texture_refs(
                    part_name, tint_ref=entry.get("texture_id")
                )
            )
    except Exception:
        pass
    return tuple(dict.fromkeys(ref for ref in refs if ref and not str(ref).lower().startswith("rbxasset://")))


def _image_from_raw_pixels(
    name: str, width: int, height: int, alpha_flag, pixels, non_color: bool = False
):
    """Build a bpy image datablock from decoded RGBA floats (no decompress)."""
    image = bpy.data.images.new(
        name, width=width, height=height, alpha=bool(alpha_flag)
    )
    # Colorspace must be set BEFORE the pixel upload: switching it on a
    # generated image afterwards drops the buffer (same rule as the
    # classic-surface band images).  The role is known at fetch time, so
    # data maps are born Non-Color and colour maps sRGB — no post-upload
    # flip is ever needed for a fresh datablock.
    try:
        image.colorspace_settings.name = "Non-Color" if non_color else "sRGB"
    except Exception:
        pass
    image.pixels.foreach_set(pixels)
    image.update()
    return image


def fetch_texture_image(
    texture_ref: str,
    name: str = "texture",
    non_color: bool = False,
    ignore_defer: bool = False,
):
    """Resolve a texture reference (rbxasset:// or rbxassetid:// or url) to a bpy Image.

    ``non_color`` selects the colorspace the image is CREATED with (data
    maps: Non-Color; colour chains: sRGB).  Setting it before the pixel
    upload avoids post-upload colorspace flips, which wipe generated image
    buffers and can leave Eevee with a stale GPU texture.

    ``ignore_defer`` skips the deferred-loading gate for fetches that are a
    SYNCHRONOUS dependency of the current build (the clothing bake needs its
    templates NOW — a deferred fetch would silently skip the layer forever).
    """
    if not texture_ref:
        return None
    asset_key = _texture_asset_key(texture_ref)
    cached = _IMAGE_CACHE.get(texture_ref) or _IMAGE_CACHE.get(asset_key)
    if cached is not None:
        # Blender can purge an unused image datablock between imports while
        # this module-level cache still holds its invalid RNA wrapper.
        try:
            _ = cached.name
            return cached
        except ReferenceError:
            _IMAGE_CACHE.pop(texture_ref, None)
            _IMAGE_CACHE.pop(asset_key, None)

    if _DEFER_TEXTURE_IMAGES and not ignore_defer:
        return None

    image = None
    try:
        if texture_ref.lower().startswith("rbxasset://"):
            local = _resolve_rbxasset_path(texture_ref)
            if local and local.is_file():
                image = bpy.data.images.load(str(local), check_existing=True)
                # File-backed: the flip is safe (buffer re-reads from disk).
                _set_image_colorspace(image, "Non-Color" if non_color else "sRGB")
        else:
            data = _fetch_image_bytes(texture_ref)
            if data:
                image = _load_image_from_bytes(data, name, non_color=non_color)
    except Exception as exc:  # pragma: no cover - network/environment dependent
        print(f"[RbxTexture] Failed to fetch '{texture_ref}': {exc}")
        image = None

    # Only cache successes. A failure here is usually transient (offline,
    # not logged in yet) and must not poison every later import.
    if image is not None:
        _IMAGE_CACHE[texture_ref] = image
        _IMAGE_CACHE[asset_key] = image
    return image


def invalidate_texture_cache() -> None:
    """Drop cached images (e.g. after login, so anonymously-failed fetches
    are retried with credentials)."""
    _IMAGE_CACHE.clear()
    _IMAGE_BYTES_CACHE.clear()
    _PAYLOAD_IMAGE_CACHE.clear()
    _PART_MATERIAL_CACHE.clear()
    _OBJECT_TINT_BUILTIN_CACHE.clear()
    _IMAGE_PREFETCH_FAILURES.clear()
    _PREFETCH_FAILED_REFS.clear()
    _ROLE_COPY_CACHE.clear()
    _IMAGE_COLOR_USES.clear()
    _IMAGE_DATA_USES.clear()


def _new_material(name: str, reset_nodes: bool = True):
    """Create a fresh datablock; Blender auto-suffixes name collisions.

    Names are labels only, never an identity channel: content-addressed
    reuse happens exclusively through the module caches.  Looking
    datablocks up by name let two different keys clobber one datablock
    (the mixed-up material bug class).
    """
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    if reset_nodes:
        _reinit_material(material)
    return material


def _reinit_material(material) -> None:
    """Clear a material's graph back to Principled -> Output, in place."""
    material.use_nodes = True
    if material.node_tree is not None:
        material.node_tree.nodes.clear()
        principled = material.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
        principled.location = (0, 0)
        output = material.node_tree.nodes.new("ShaderNodeOutputMaterial")
        output.location = (300, 0)
        material.node_tree.links.new(
            principled.outputs["BSDF"], output.inputs["Surface"]
        )


def _part_material_cache_key(entry: dict):
    """Return a visual signature for reusable MeshPart materials."""
    # Scope the clothing context to THIS entry's character before reading
    # context_signature, otherwise the key (computed on the main thread for
    # lazy place imports) captures a neighbour's clothing and the worker
    # reuses the wrong material.
    _scoped_clothing_context(entry)

    def stable(value):
        try:
            return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return repr(value)

    name = str(entry.get("name") or "")
    try:
        from . import clothing

        clothing_role = name.strip().lower() if clothing.is_clothing_limb(name) else ""
        clothing_signature = (
            clothing.context_signature(name, tint_ref=entry.get("texture_id"))
            if clothing_role else ()
        )
    except Exception:
        clothing_role = name.strip().lower() if name.strip().lower() == "head" else ""
        clothing_signature = ()
    try:
        color = tuple(round(float(value), 5) for value in (entry.get("color") or ()))
        transparency = round(float(entry.get("transparency", 0.0)), 5)
        reflectance = round(float(entry.get("reflectance", 0.0)), 5)
    except (TypeError, ValueError):
        return None
    # Built-in materials are tinted through the RBXColor attribute, so the
    # part colour must NOT split them into per-colour material datablocks —
    # that is exactly the id churn that slows place imports down.  The
    # variant-aware effective material covers MaterialVariant-only parts.
    # Meshes with asset-baked vertex colors multiply the tint in-shader, so
    # their colour must stay in the key (different tints = different graphs).
    mesh_vertex_colors = bool(entry.get("_mesh_vertex_colors"))
    if (
        _effective_material_id(entry) is not None
        and not mesh_vertex_colors
        and not any(
            (sa or {}).get("color_map") for sa in _entry_surface_appearances(entry)
        )
        and not entry.get("texture_id")
    ):
        color = ()
    return (
        _effective_material_id(entry), variant_signature(entry),
        color, transparency, reflectance,
        str(entry.get("texture_id") or ""),
        entry.get("texture_studs_per_tile"),
        stable(_entry_texture_instances(entry)),
        stable(_entry_surface_appearances(entry)), stable(entry.get("face_decal")),
        clothing_role, clothing_signature,
        bool(entry.get("_use_2022_materials", True)),
        mesh_vertex_colors,
    )


def part_material_identity(entry: dict):
    """Return the canonical identity used by every part-material consumer."""
    return _part_material_cache_key(entry)


def _principled_node(material):
    nodes = material.node_tree.nodes
    for node in nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def _image_texture_node(material, image, label):
    nodes = material.node_tree.nodes
    node = nodes.new("ShaderNodeTexImage")
    node.image = image
    node.label = label
    return node


def _bind_named_uv(material, tex_node, layer_name: str, node_name: str) -> None:
    """Bind a texture to a specific uv layer, never Blender's active layer."""
    uv_node = material.node_tree.nodes.get(node_name)
    if uv_node is None:
        uv_node = material.node_tree.nodes.new("ShaderNodeUVMap")
        uv_node.name = node_name
        uv_node.label = f"RBX UV: {layer_name}"
        uv_node.uv_map = layer_name
        uv_node.location = (-700, tex_node.location[1])
    _link(material, uv_node.outputs["UV"], tex_node.inputs["Vector"])


def _overlay_alpha_image_copy(image):
    """Removed: overlay factors read the texture node's own alpha output."""
    return None


def _set_base_color(material, rgba):
    principled = _principled_node(material)
    if principled is not None:
        principled.inputs["Base Color"].default_value = rgba
    # Solid/Workbench "Material" viewport mode reads material.diffuse_color,
    # NOT the Principled node — without this the part renders grey in Solid
    # mode even though Rendered mode (which uses the shader) shows the color.
    try:
        material.diffuse_color = (rgba[0], rgba[1], rgba[2], 1.0)
    except Exception:
        pass


def _link(material, out_socket, in_socket):
    if out_socket is None or in_socket is None:
        return
    if _LINK_ARGS_REVERSED:
        # Blender <3.4: NodeLinks.new(input, output).
        material.node_tree.links.new(in_socket, out_socket)
    else:
        material.node_tree.links.new(out_socket, in_socket)


def _socket_by_type(node, kind, name, socket_type):
    """Named-socket lookup that survives Blender 5.1's per-data-type sets.

    ShaderNodeMix exposes A/B/Factor/Result once per data type (VALUE,
    VECTOR, RGBA, ROTATION), so a name-only lookup hits the float variant
    and colour chains silently collapse to grey scalars.  Older releases
    have unique socket names; the final fallback keeps them working.
    """
    sockets = node.inputs if kind == "input" else node.outputs
    for socket in sockets:
        if socket.name == name and socket.type == socket_type:
            return socket
    return node.inputs[name] if kind == "input" else node.outputs.get(name)


def _set_color_default(socket, rgba) -> None:
    """Set a color socket default, tolerating 3-channel color sockets.

    Blender's Mix node color sockets are 3-channel RGB on 4.x but RGBA
    elsewhere; drop the alpha component when the socket rejects 4 items
    rather than crashing the whole material build.
    """
    try:
        socket.default_value = rgba
    except Exception:
        socket.default_value = rgba[:3]


def _float_factor_socket(mix):
    """The mix node's float factor socket, verified by socket TYPE.

    Blender 5.1 exposes a Factor per data type; a name-only lookup can land
    on a vector-typed variant and the fade link then fails (leaving the
    overlay factor unlinked and the transparency visually ignored).
    MixRGB (pre-3.4) names the socket Fac.
    """
    for socket in mix.inputs:
        if socket.name in ("Factor", "Fac") and str(getattr(socket, "type", "")) in (
            "VALUE",
            "FLOAT",
        ):
            return socket
    return None


def _new_mix_node(material, name, label, blend_type):
    """Create an RGBA mix node, returning (node, a, b, factor, result).

    Pre-3.4 Blender lacks ShaderNodeMix, so the legacy build falls back to
    MixRGB: same blend modes, Fac instead of Factor, Color1/Color2 sockets.
    """
    if _HAS_MIX_NODE:
        mix = material.node_tree.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = blend_type
        a = _socket_by_type(mix, "input", "A", "RGBA")
        b = _socket_by_type(mix, "input", "B", "RGBA")
        factor = _float_factor_socket(mix)
        result = _socket_by_type(mix, "output", "Result", "RGBA")
    else:
        mix = material.node_tree.nodes.new("ShaderNodeMixRGB")
        mix.blend_type = blend_type
        a = mix.inputs["Color1"]
        b = mix.inputs["Color2"]
        factor = mix.inputs["Fac"]
        result = mix.outputs["Color"]
    mix.name = name
    mix.label = label
    return mix, a, b, factor, result


def _output_surface_socket(material):
    """The material output node's Surface input socket."""
    for node in material.node_tree.nodes:
        if node.type == "OUTPUT_MATERIAL":
            try:
                return node.inputs["Surface"]
            except KeyError:
                return None
    return None


def _bind_overlay_alpha(material, principled, texture_node, color, alpha_scale=1.0, tint=None, tag="") -> None:
    """Composite an overlay texture over the part colour by its alpha.

    Roblox AlphaMode.Overlay (0) and child ``Texture`` instances both treat
    the texture RGB as the overlay and its alpha as the reveal factor:
    final = map x a + underneath x (1-a).  Feeding the raw RGB instead
    renders masked trim sheets (black RGB with the pattern in alpha) as
    solid black.

    When Base Color already has a source (e.g. a built-in material map or
    an earlier composite layer), that source becomes the underneath layer —
    Texture instances and SurfaceAppearances draw OVER whatever is beneath
    them on Roblox, they do not replace it.  ``tag`` namespaces the mix and
    alpha nodes so stacked layers each get their own (layer 0 keeps the
    legacy names).
    ``alpha_scale`` fades the whole overlay (child Texture.Transparency).
    ``tint`` multiplies into the texture RGB (child Texture.Color3).
    """
    try:
        tint_color = tuple(
            max(0.0, min(1.0, float(component))) for component in color[:3]
        )
    except (TypeError, ValueError, IndexError):
        tint_color = (1.0, 1.0, 1.0)
    mix, a_socket, b_socket, factor_socket, color_out = _new_mix_node(
        material, f"RBX Overlay Mix{tag}",
        "RBX overlay composite (map α over part colour)", "MIX",
    )
    mix.location = (-150, 0)
    texture_node.image = _color_image_view(texture_node.image)
    factor_source = texture_node.outputs["Alpha"]
    base_input = principled.inputs["Base Color"]
    existing = base_input.links[0].from_socket if base_input.links else None
    if existing is not None:
        # Composite the decal over whatever already drives Base Color (e.g.
        # a built-in material colour map) instead of the flat part colour.
        _link(material, existing, a_socket)
    else:
        _set_color_default(a_socket, tint_color + (1.0,))
    if alpha_scale != 1.0 and factor_socket is not None:
        # Atomic: the fade node must be fully wired or removed.  An
        # orphaned fade leaves the mix factor unlinked and the default
        # (0.5) drives the overlay — a half-strength decal that looks like
        # the transparency was never applied.
        fade = material.node_tree.nodes.new("ShaderNodeMath")
        try:
            fade.operation = "MULTIPLY"
            fade.name = f"RBX Overlay Alpha Scale{tag}"
            fade.label = "RBX overlay fade (1 − texture transparency)"
            fade.location = (-300, -200)
            fade.inputs[1].default_value = max(0.0, min(1.0, float(alpha_scale)))
            _link(material, factor_source, fade.inputs[0])
            _link(material, fade.outputs["Value"], factor_socket)
        except Exception:
            try:
                material.node_tree.nodes.remove(fade)
            except Exception:
                pass
            if factor_socket is not None:
                _link(material, factor_source, factor_socket)
            print(
                "[RbxTexture] overlay fade wiring failed; "
                "binding the overlay at full strength"
            )
    else:
        if alpha_scale != 1.0 and factor_socket is None:
            print(
                "[RbxTexture] no float factor socket on the overlay mix; "
                "binding the overlay at full strength"
            )
        if factor_socket is not None:
            _link(material, factor_source, factor_socket)
    overlay_color = texture_node.outputs["Color"]
    if tint is not None and any(abs(c - 1.0) > 1e-5 for c in tint[:3]):
        try:
            instance_tint = tuple(
                max(0.0, min(1.0, float(component))) for component in tint[:3]
            )
        except (TypeError, ValueError, IndexError):
            instance_tint = None
        if instance_tint is not None:
            tint_mix, tint_a, tint_b, tint_factor, tint_result = _new_mix_node(
                material, f"RBX Overlay Tint{tag}",
                "RBX overlay tint (texture colour × instance Color3)", "MULTIPLY",
            )
            tint_mix.location = (-300, 150)
            if tint_factor is not None:
                tint_factor.default_value = 1.0
            _set_color_default(tint_a, instance_tint + (1.0,))
            _link(material, texture_node.outputs["Color"], tint_b)
            overlay_color = tint_result
    _link(material, overlay_color, b_socket)
    _link(material, color_out, base_input)


def _bind_transparency_alpha(material, principled, texture_node, tint=None, bind_alpha=True, alpha_scale=None) -> None:
    """Bind a Roblox transparency-alpha texture (leaves, fences, grass).

    Unlike overlay alpha (decal semantics, where transparent texels reveal
    the part color beneath), these maps cut/blend against the world behind
    the part: RGB drives Base Color and alpha drives Principled Alpha.
    Baking alpha-over-color is exactly wrong here — foliage maps are usually
    white (or black) at alpha=0, so the overlay path renders those texels as
    solid white leaves instead of holes.

    Classic MeshPart TextureIDs are mostly grayscale tint maps (white RGB,
    hue from Color3); pass ``tint`` (the part color) to multiply it into the
    texture. SurfaceAppearance color maps are full-color and skip the tint.
    ``bind_alpha=False`` keeps the multiply without touching Principled
    Alpha — used for opaque built-in material color maps.
    ``alpha_scale`` multiplies the texture alpha (Roblox also multiplies
    BasePart.Transparency into the map), mirroring the community-standard
    Map Range chain without an extra node when the scale is 1.
    """
    base_input = principled.inputs["Base Color"]
    for link in list(base_input.links):
        material.node_tree.links.remove(link)
    if tint is not None:
        tint_color = tuple(
            max(0.0, min(1.0, float(component))) for component in tint[:3]
        )
        # MULTIPLY with Factor=1 is just A x B — cheaper and clearer than
        # routing a vector math node into the color chain.
        mix, tint_a, tint_b, factor, tint_result = _new_mix_node(
            material, "RBX TextureID Tint",
            "RBX TextureID Tint (part colour x texture)", "MULTIPLY",
        )
        mix.location = (-150, 0)
        if factor is not None:
            factor.default_value = 1.0
        _set_color_default(tint_a, tint_color + (1.0,))
        _link(material, texture_node.outputs["Color"], tint_b)
        _link(material, tint_result, base_input)
    else:
        _link(material, texture_node.outputs["Color"], base_input)
    if bind_alpha:
        alpha_input = principled.inputs.get("Alpha")
        if alpha_input is not None:
            if alpha_scale is not None and abs(alpha_scale - 1.0) > 1e-6:
                try:
                    scale = max(0.0, min(1.0, float(alpha_scale)))
                except (TypeError, ValueError):
                    scale = 1.0
                multiply = material.node_tree.nodes.new("ShaderNodeMath")
                multiply.operation = "MULTIPLY"
                multiply.name = "RBX TextureID AlphaScale"
                multiply.label = "RBX texture alpha x part transparency"
                multiply.location = (-150, -300)
                multiply.inputs[1].default_value = scale
                _link(material, texture_node.outputs["Alpha"], multiply.inputs[0])
                _link(material, multiply.outputs["Value"], alpha_input)
            else:
                _link(material, texture_node.outputs["Alpha"], alpha_input)


def _activate_texture_node(material, texture_node) -> None:
    """Make ``texture_node`` the active node (Workbench Texture mode draw)."""
    try:
        material.node_tree.nodes.active = texture_node
    except Exception:
        pass


def _apply_part_transparency(material, principled, entry: dict, texture_alpha: bool = False) -> None:
    """Apply Roblox BasePart.Transparency (0 opaque, 1 invisible)."""
    try:
        transparency = float(entry.get("transparency", 0.0))
    except (TypeError, ValueError):
        transparency = 0.0
    alpha = max(0.0, min(1.0, 1.0 - transparency))
    # Sorted alpha blending is wrong for masked/variable-alpha texture maps:
    # intersecting faces and repeated instances then fight for draw order and
    # look like GPU corruption or overdraw.  Keep those maps in Blender's
    # dithered/hashed path.  True BasePart transparency needs sorted blending.
    part_blended = alpha < 1.0
    has_transparency = texture_alpha or part_blended

    alpha_input = principled.inputs.get("Alpha")
    if alpha_input is not None:
        alpha_input.default_value = alpha
    try:
        color = material.diffuse_color
        material.diffuse_color = (color[0], color[1], color[2], alpha)
    except Exception:
        pass

    # Blender 5.1 renamed the material rendering control; retain the legacy
    # property for older Blender releases supported by the add-on.
    try:
        material.surface_render_method = "BLENDED" if part_blended else "DITHERED"
    except Exception:
        pass
    try:
        material.blend_method = (
            "BLEND" if part_blended else "HASHED" if texture_alpha else "OPAQUE"
        )
    except Exception:
        pass
    # EEVEE's legacy material shadow mode is independent of blend mode.  Do
    # not leave imported opaque parts at a version-dependent default of NONE;
    # transparent Roblox parts need hashed shadows so they still occlude light.
    try:
        material.shadow_method = "HASHED" if has_transparency else "OPAQUE"
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        material.use_transparent_shadow = has_transparency
    except (AttributeError, TypeError):
        pass


# ── Roblox built-in materials ────────────────────────────────────────
# The binary ``Material`` property carries the Enum.Material.Value integer
# directly (256 = Plastic, 512 = Wood, 848 = Brick, 1088 = Metal, ...), the
# same values rbxmx XML files store. The asset ids below are Roblox's
# base-material texture maps from the creator-docs material reference.

# Enum.Material.Value -> name (create.roblox.com/docs/reference/engine/enums/Material).
_MATERIAL_NAME_BY_ID = {
    256: "Plastic", 272: "SmoothPlastic", 288: "Neon", 512: "Wood",
    528: "WoodPlanks", 784: "Marble", 788: "Basalt", 800: "Slate",
    804: "CrackedLava", 816: "Concrete", 820: "Limestone", 832: "Granite",
    836: "Pavement", 848: "Brick", 864: "Pebble", 880: "Cobblestone",
    896: "Rock", 912: "Sandstone", 1040: "CorrodedMetal",
    1056: "DiamondPlate", 1072: "Foil", 1088: "Metal", 1280: "Grass",
    1284: "LeafyGrass", 1296: "Sand", 1312: "Fabric", 1328: "Snow",
    1344: "Mud", 1360: "Ground", 1376: "Asphalt", 1392: "Salt",
    1536: "Ice", 1552: "Glacier", 1568: "Glass", 1584: "ForceField",
    2304: "Cardboard", 2305: "Carpet", 2306: "CeramicTiles",
    2307: "ClayRoofTiles", 2308: "RoofShingles", 2309: "Leather",
    2310: "Plaster", 2311: "Rubber",
}

# (colour map, normal map, metalness map, roughness map) asset ids.
_MATERIAL_MAPS_2022 = {
    "Wood": ("9920625290", "9439641376", "", "9439648605"),
    "Slate": ("9920599782", "9439612514", "", "9439612733"),
    "Concrete": ("9920484153", "9466554006", "", "9466554186"),
    "CorrodedMetal": ("9920589327", "9439548484", "9439548749", "9439556441"),
    "DiamondPlate": ("10237720195", "9438583222", "9438583347", "9438583558"),
    "Foil": ("9466552117", "9424786192", "9424786272", "9424786620"),
    "Grass": ("9920551868", "9438955773", "", "9438955997"),
    "Ice": ("9920555943", "9467301039", "", "9467301203"),
    "Marble": ("9439430596", "9439431240", "", "9439431383"),
    "Metal": ("9920574687", "9873295432", "9873318201", "9873318890"),
    "Pebble": ("9920581082", "9439528644", "", "9439537267"),
    "Sand": ("9920591683", "9439577084", "", "9439577327"),
    "WoodPlanks": ("9920626778", "9439650689", "", "9439658127"),
    "Fabric": ("9920517696", "9873280412", "", "9873282563"),
    "Granite": ("9920550238", "9438882935", "", "9438883109"),
    "Brick": ("9920482813", "9438453152", "", "9438453413"),
    "Cobblestone": ("9919718991", "9438457162", "", "9438457470"),
    "Sandstone": ("9920596120", "9439596530", "", "9439596711"),
    "Basalt": ("9920482056", "9438412214", "", "9438412457"),
    "CrackedLava": ("9920484943", "9438508790", "", "9438509046"),
    "Glacier": ("9920518732", "9438812958", "", "9438851286"),
    "Ground": ("9920554482", "9439043558", "", "9439043765"),
    "LeafyGrass": ("9920557906", "9439080781", "", "9439080950"),
    "Limestone": ("9920561437", "9439415191", "", "9439415495"),
    "Mud": ("9920578473", "9439509827", "", "9439510012"),
    "Pavement": ("9920579943", "9439519281", "", "9439519532"),
    "Rock": ("9920587470", "9439538417", "", "9439545859"),
    "Salt": ("9920590225", "9439565809", "", "9439566688"),
    "Snow": ("9920620284", "9439632006", "", "9439632145"),
    "Asphalt": ("9930003046", "9429449876", "", "9429450346"),
    "Glass": ("9438868521", "7547304785", "", "7547304892"),
    "Cardboard": ("14108651729", "14108654002", "", "14108654299"),
    "Carpet": ("14108662587", "14108663154", "", "14108663726"),
    "CeramicTiles": ("17429425079", "17429425915", "17429426100", "17429426861"),
    "ClayRoofTiles": ("18147681935", "18147683410", "", "18147684855"),
    "Leather": ("14108670073", "14108670486", "", "14108670748"),
    "Plaster": ("14108671255", "14108671870", "", "14108672378"),
    "RoofShingles": ("119722544879522", "77534750680073", "", "129397260312247"),
    "Rubber": ("14108673018", "14108674698", "14108674894", "14108675142"),
}

# Pre-2022 legacy base textures (MaterialService.Use2022Materials=false).
# Materials missing here render as flat part-colour plastic in legacy places.
_MATERIAL_MAPS_LEGACY = {
    "Brick": ("7546648254", "7546649654", "", "7546650017"),
    "Cobblestone": ("7546651802", "7546652689", "", "7546652892"),
    "Concrete": ("7546653328", "7546653707", "", "7546653868"),
    "CorrodedMetal": ("7547183598", "7547181182", "7547184321", "7547184588"),
    "DiamondPlate": ("7546654401", "7546654536", "7547162002", "7547162137"),
    "Fabric": ("7547100606", "7547100915", "", "7547101072"),
    "Foil": ("7546644642", "7546644903", "7546644642", "7546644963"),
    "Glass": ("7547304577", "7547304785", "", "7547304892"),
    "Granite": ("7547164400", "7546654648", "", "7547164660"),
    "Grass": ("7547167347", "7547168653", "", "7547169207"),
    "Ice": ("7546644642", "7547171198", "", "7547171276"),
    "Marble": ("7547174345", "7547176060", "", "7547177213"),
    "Metal": ("7547178395", "7547287997", "7547288112", "7547179082"),
    "Pebble": ("7547291174", "7546645052", "", "7547291306"),
    "Sand": ("7547294684", "7547294810", "", "7547295087"),
    "Slate": ("7547297050", "7547297808", "", "7547298051"),
    "Wood": ("7547190453", "7547190548", "7547190619", "7547303147"),
    "WoodPlanks": ("7547301709", "7547188159", "7547188891", "7547332869"),
}


def _builtin_material_entry(material_id, use_2022=True):
    """Resolve an Enum.Material.Value int to {name, maps} or None."""
    if not isinstance(material_id, int):
        return None
    name = _MATERIAL_NAME_BY_ID.get(material_id)
    if name is None:
        return None
    table = _MATERIAL_MAPS_2022 if use_2022 else _MATERIAL_MAPS_LEGACY
    return {"name": name, "maps": table.get(name, ("", "", "", ""))}


def _effective_material_id(entry: dict):
    """The built-in material that actually renders for this entry.

    A MaterialVariant completely replaces the part's material look, so its
    BaseMaterial wins whenever a variant is present; the part's own
    Enum.Material only applies without one.
    """
    variant = entry.get("material_variant_data") or {}
    base = variant.get("base_material")
    try:
        if base in _MATERIAL_NAME_BY_ID:
            return base
    except TypeError:
        pass
    try:
        if entry.get("material") in _MATERIAL_NAME_BY_ID:
            return entry.get("material")
    except TypeError:
        pass
    return None


def variant_signature(entry: dict):
    """Compact identity of the resolved MaterialVariant, or None.

    Names are labels only: two variants with the same name can carry
    different maps across imports, so every cache keyed on a variant uses
    its data, never its name.
    """
    variant = entry.get("material_variant_data") or {}
    if not variant:
        return None
    return (
        variant.get("base_material"),
        str(variant.get("color_map") or ""),
        str(variant.get("normal_map") or ""),
        str(variant.get("metalness_map") or ""),
        str(variant.get("roughness_map") or ""),
        variant.get("studs_per_tile"),
    )


def _builtin_material_for_entry(entry: dict, use_2022=None):
    """Resolve the built-in material, letting MaterialVariant win."""
    if use_2022 is None:
        use_2022 = bool(entry.get("_use_2022_materials", True))
    variant = entry.get("material_variant_data") or {}
    builtin = _builtin_material_entry(variant.get("base_material"), use_2022=use_2022)
    if builtin is None:
        builtin = _builtin_material_entry(entry.get("material"), use_2022=use_2022)
    return builtin


def builtin_material_texture_refs(entry):
    """rbxassetid refs for an entry's built-in material maps, if any.

    MaterialVariant (resolved from MaterialService) replaces the material's
    colour texture and keeps the part tint, so its maps belong in the same
    prefetch/hydration/UV set.
    """
    variant = entry.get("material_variant_data")
    if variant:
        return [
            variant[key]
            for key in ("color_map", "normal_map", "metalness_map", "roughness_map")
            if variant.get(key)
        ]
    builtin = _builtin_material_for_entry(entry)
    if builtin is None:
        return []
    return [f"rbxassetid://{ref}" for ref in builtin["maps"] if ref]


def entry_uses_shared_builtin_tint(entry: dict) -> bool:
    """True when the part colour rides the RBXColor attribute.

    Every Enum.Material drops colour from its cache key, so every built-in
    (with or without texture maps) must tint through the attribute.  A
    MaterialVariant replaces the default material, so its BaseMaterial
    counts too.
    """
    return _effective_material_id(entry) is not None


def entry_uses_baked_tint(entry: dict) -> bool:
    """True when an instance override needs the baked per-colour material.

    Only attribute-tinted built-ins need it: SurfaceAppearance and
    TextureID materials keep colour in their cache key, so the shared
    material path already produces a correct per-colour datablock.  This
    mirrors the colour-collapse condition in _part_material_cache_key
    exactly — baked materials exist precisely where that cache cannot
    split by colour.
    """
    return (
        entry_uses_shared_builtin_tint(entry)
        and not any(
            (sa or {}).get("color_map") for sa in _entry_surface_appearances(entry)
        )
        and not entry.get("texture_id")
    )


def _bind_linear_tint(material, texture_node, use_attribute_tint, base_input):
    """Multiply the colour map by the part colour, both already linear.

    The part colour arrives through the RBXColor colour-attribute layer read
    by a Vertex Color node (ShaderNodeAttribute does not expose colour
    attributes in this Blender build; the Vertex Color node decodes the
    stored sRGB values to linear, verified against an RGB node in a render
    test).  The texture node also outputs linear, so one MULTIPLY reproduces
    the engine's linear-space tint with no gamma machinery.
    """
    if not use_attribute_tint:
        _link(material, texture_node.outputs["Color"], base_input)
        return
    nodes = material.node_tree.nodes
    color_attr = nodes.new("ShaderNodeVertexColor")
    color_attr.layer_name = "RBXColor"
    color_attr.name = "RBX Material ColorAttr"
    color_attr.label = "RBX part colour (RBXColor vertex colours)"
    color_attr.location = (texture_node.location[0], texture_node.location[1] - 240)
    multiply = nodes.new("ShaderNodeVectorMath")
    multiply.operation = "MULTIPLY"
    multiply.name = "RBX Material Tint"
    multiply.label = "RBX part colour × map (linear)"
    multiply.location = (texture_node.location[0] + 250, texture_node.location[1])
    _link(material, texture_node.outputs["Color"], multiply.inputs[0])
    _link(material, color_attr.outputs["Color"], multiply.inputs[1])
    _link(material, multiply.outputs["Vector"], base_input)


def _apply_mesh_vertex_color_tint(material, principled, color, enabled) -> None:
    """Multiply asset-baked RBXColor vertex colors into the tint chain.

    Regular FileMesh parts can carry baked per-vertex colors in their mesh
    asset; Studio multiplies those by the part Color3 (and TextureID).  The
    mesh's RBXColor attribute holds the baked colors (written by
    creation._apply_mesh_vertex_colors), so this stage multiplies them into
    whatever Base Color chain the texture/builtin paths left behind — once.
    A component already provided by the chain (attribute-read tint or the
    TextureID tint mix) is not applied twice.
    """
    if not enabled:
        return
    nodes = material.node_tree.nodes
    base_input = principled.inputs["Base Color"]
    links = [link for link in base_input.links]
    chain_nodes = [link.from_node for link in links]
    reads_baked = any(
        node is not None
        and getattr(node, "type", None) == "VERTEX_COLOR"
        and getattr(node, "layer_name", None) == "RBXColor"
        for node in chain_nodes
    )
    chain_has_tint = any(
        node is not None
        and getattr(node, "name", "").startswith("RBX TextureID Tint")
        for node in chain_nodes
    )
    try:
        tint = (
            max(0.0, min(1.0, float(component))) for component in color[:3]
        )
        tint = tuple(tint) + (1.0,)
    except (TypeError, ValueError, IndexError):
        tint = (1.0, 1.0, 1.0, 1.0)

    def _multiply(a_socket, b_socket, label, node_name):
        mix, mix_a, mix_b, factor, result = _new_mix_node(
            material, node_name, label, "MULTIPLY",
        )
        if factor is not None:
            factor.default_value = 1.0
        _link(material, a_socket, mix_a)
        _link(material, b_socket, mix_b)
        return result

    current = links[0].from_socket if links else None
    if current is None:
        # No chain at all: the Principled default IS the flat part tint.
        rgb = nodes.new("ShaderNodeRGB")
        _set_color_default(rgb.outputs["Color"], tint)
        current = rgb.outputs["Color"]
        chain_has_tint = True
    if not reads_baked:
        attr_node = nodes.new("ShaderNodeVertexColor")
        attr_node.layer_name = "RBXColor"
        attr_node.name = "RBX Mesh VertexColors"
        attr_node.label = "RBX mesh vertex colors (asset baked)"
        attr_node.location = (-400, 300)
        current = _multiply(
            current, attr_node.outputs["Color"],
            "RBX vertex colors × colour chain",
            "RBX Mesh VertexColors Multiply",
        )
    if not chain_has_tint:
        rgb = nodes.new("ShaderNodeRGB")
        _set_color_default(rgb.outputs["Color"], tint)
        current = _multiply(
            current, rgb.outputs["Color"],
            "RBX part colour × vertex colors",
            "RBX Mesh VertexColors Tint",
        )
    for link in links:
        material.node_tree.links.remove(link)
    _link(material, current, base_input)


def _ensure_builtin_material_color(mesh, rgba):
    """Flat per-part RBXColor attribute feeding the shared built-in tint.

    Written once per mesh (instances share it, and batches merge only
    same-colour parts), so the shader's Attribute node always finds data.
    The write OVERWRITES any pre-existing values: batched filemeshes can
    carry the asset's own baked vertex colours into RBXColor, and the part
    colour must win for every built-in/plain part (union meshes return
    before this point and keep their per-vertex colours).
    """
    color_attributes = getattr(mesh, "color_attributes", None)
    if color_attributes is None:
        return
    layer = color_attributes.get("RBXColor")
    if layer is None:
        try:
            layer = color_attributes.new(name="RBXColor", type="BYTE_COLOR", domain="CORNER")
        except Exception:
            return
    rgba = (
        max(0.0, min(1.0, float(rgba[0]))),
        max(0.0, min(1.0, float(rgba[1]))),
        max(0.0, min(1.0, float(rgba[2]))),
        1.0,
    )
    count = len(layer.data)
    if count == 0:
        return
    try:
        import numpy as np  # bundled with Blender

        layer.data.foreach_set("color", np.tile(np.asarray(rgba, dtype=np.float32), count))
    except ImportError:
        layer.data.foreach_set("color", list(rgba) * count)


_PLAIN_TINT_MATERIAL_CACHE: dict = {}


def plain_tint_material(transparency=0.0):
    """One shared Principled material for plain Color3 BaseParts.

    Plain parts (no Enum.Material, no texture) would otherwise need one
    material datablock per distinct colour; place static-batch keys include
    colour, so a city of tinted buildings churns hundreds of expensive
    datablocks.  The colour instead lives in the mesh's RBXColor attribute
    (scene-linear values), which this shader reads directly.
    """
    try:
        transparency = max(0.0, min(1.0, float(transparency)))
    except (TypeError, ValueError):
        transparency = 0.0
    key = round(transparency, 5)
    material = _PLAIN_TINT_MATERIAL_CACHE.get(key)
    live = _live_material(material)
    if live is not None and _graph_current(live):
        return live
    name = f"rbx_plain_tint_t{key:.3f}"
    # Rebuild in place when the datablock survives from an older graph
    # version; otherwise create a fresh one (names are labels only).
    material = live if live is not None else bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    if nodes.get("RBX Plain Color") is None or not _graph_current(material):
        _reinit_material(material)
        nodes = material.node_tree.nodes
    principled = _principled_node(material)
    if principled is None:
        return material
    if nodes.get("RBX Plain Color") is None:
        attr_node = nodes.new("ShaderNodeVertexColor")
        attr_node.layer_name = "RBXColor"
        attr_node.name = "RBX Plain Color"
        attr_node.label = "RBX Plain Color (per-mesh Color3)"
        attr_node.location = (-300, 200)
        _link(material, attr_node.outputs["Color"], principled.inputs["Base Color"])
    _apply_part_transparency(material, principled, {"transparency": transparency})
    try:
        # Solid viewport mode reads diffuse_color; the per-mesh attribute is
        # only evaluated by Material/Rendered shading.  White keeps the
        # shared datablock neutral there (use viewport Colour: Attribute to
        # show per-part colours in Solid mode).
        material.diffuse_color = (1.0, 1.0, 1.0, max(0.0, min(1.0, 1.0 - transparency)))
    except Exception:
        pass
    material["RBXPlainTint"] = True
    material["RBXMaterialGraphVersion"] = _MATERIAL_GRAPH_VERSION
    _PLAIN_TINT_MATERIAL_CACHE[key] = material
    return material


def ensure_plain_color_attribute(mesh, rgba):
    """Write a mesh's RBXColor attribute with sRGB Color3 values.

    The Vertex Color node decodes the stored sRGB to linear on read, which
    matches what an RGB node with the same colour produces (verified in a
    render test), so storage is plain sRGB — identical to the built-in
    tint attribute.  Overwrites pre-existing values so asset-baked vertex
    colours never leak through on plain parts.
    """
    _ensure_builtin_material_color(mesh, rgba)


# Roblox tiles built-in materials at a fixed studs-per-tile scale using
# per-face planar UVs — it ignores mesh UVs entirely. Native FileMesh UVs
# instead stretch one tile across the whole mesh, which imports as a huge
# zoomed-in texture. 10 studs per tile matches the engine's default for
# built-in materials; MaterialVariants override it per material.
_BUILTIN_MATERIAL_STUDS_PER_TILE = 10.0
_BUILTIN_MATERIAL_UV_LAYER = "RBXMaterialUV"


def _face_projection_axes(pts):
    """Pick the two dominant in-plane local axes for a polygonal face.

    V (the image's vertical) always follows the part's local up on vertical
    faces, so patterns stay upright: X walls project (Z, Y), Y faces (X, Z),
    Z walls (X, Y).  The U direction flips with the face normal sign so the
    pattern reads correctly viewed from OUTSIDE every face — roblox mirrors
    per face, and a sign-less projection mirrors half of them.

    Returns (axis_u, axis_v, u_sign).
    """
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
    axn, ayn, azn = abs(nx), abs(ny), abs(nz)
    if axn >= ayn and axn >= azn:
        return (2, 1, -1.0 if nx > 0 else 1.0)  # +X: U=-Z, -X: U=+Z
    if ayn >= axn and ayn >= azn:
        return (0, 2, 1.0)
    return (0, 1, 1.0 if nz > 0 else -1.0)  # +Z: U=+X, -Z: U=-X


def _ensure_builtin_material_uvs(mesh):
    """Build (once) the stud-density planar UV layer for built-in materials.

    Projected in LOCAL space, exactly like the engine: scaling a part scales
    its material pattern, and instances sharing a mesh datablock stay
    consistent because the projection only depends on the shared geometry.
    """
    uv_layer = mesh.uv_layers.get(_BUILTIN_MATERIAL_UV_LAYER)
    if uv_layer is not None:
        return uv_layer
    try:
        uv_layer = mesh.uv_layers.new(name=_BUILTIN_MATERIAL_UV_LAYER)
    except Exception:
        return None
    units = 1.0 / _BUILTIN_MATERIAL_STUDS_PER_TILE
    loop_count = len(mesh.loops)
    if loop_count == 0:
        return uv_layer
    try:
        import numpy as np  # bundled with Blender

        vertex_index = np.empty(loop_count, dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", vertex_index)
        coords = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", coords)
        coords = coords.reshape(-1, 3)
        loop_coords = coords[vertex_index]
        poly_count = len(mesh.polygons)
        loop_start = np.empty(poly_count, dtype=np.int32)
        loop_total = np.empty(poly_count, dtype=np.int32)
        mesh.polygons.foreach_get("loop_start", loop_start)
        mesh.polygons.foreach_get("loop_total", loop_total)
        poly_of_loop = np.repeat(np.arange(poly_count, dtype=np.int64), loop_total)
        # Per-polygon dominant in-plane axes from its first triangle.
        p0 = loop_coords[loop_start]
        p1 = loop_coords[np.minimum(loop_start + 1, loop_count - 1)]
        p2 = loop_coords[np.minimum(loop_start + 2, loop_count - 1)]
        e1 = p1 - p0
        e2 = p2 - p0
        nx = e1[:, 1] * e2[:, 2] - e1[:, 2] * e2[:, 1]
        ny = e1[:, 2] * e2[:, 0] - e1[:, 0] * e2[:, 2]
        nz = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
        axn, ayn, azn = np.abs(nx), np.abs(ny), np.abs(nz)
        x_dominant = axn >= np.maximum(ayn, azn)
        y_dominant = ~x_dominant & (ayn >= np.maximum(axn, azn))
        z_dominant = ~x_dominant & ~y_dominant
        # Same convention as _face_projection_axes, including the U flip for
        # faces viewed from the negative side.
        au = np.where(x_dominant, 2, np.where(y_dominant, 0, 0))
        av = np.where(x_dominant, 1, np.where(y_dominant, 2, 1))
        u_sign = np.where(
            x_dominant,
            -np.sign(nx),
            np.where(z_dominant, np.sign(nz), 1.0),
        ).astype(np.float32)
        loop_au = au[poly_of_loop]
        loop_av = av[poly_of_loop]
        loop_u_sign = u_sign[poly_of_loop]
        row = np.arange(loop_count)
        col_u = loop_coords[row, loop_au] * loop_u_sign
        col_v = loop_coords[row, loop_av]
        # reduceat: per-polygon minima over contiguous loop segments.
        min_u = np.minimum.reduceat(col_u, loop_start)
        min_v = np.minimum.reduceat(col_v, loop_start)
        uv = np.empty((loop_count, 2), dtype=np.float32)
        uv[:, 0] = (col_u - min_u[poly_of_loop]) * units
        uv[:, 1] = (col_v - min_v[poly_of_loop]) * units
        uv_layer.data.foreach_set("uv", uv.reshape(-1))
        return uv_layer
    except Exception:
        pass
    for poly in mesh.polygons:
        pts = [mesh.vertices[v].co for v in poly.vertices]
        if len(pts) < 3:
            continue
        ax_u, ax_v, u_sign = _face_projection_axes(pts)
        origin_u = min(p[ax_u] * u_sign for p in pts)
        origin_v = min(p[ax_v] for p in pts)
        for loop_index, point in zip(poly.loop_indices, pts):
            uv_layer.data[loop_index].uv = (
                (point[ax_u] * u_sign - origin_u) * units,
                (point[ax_v] - origin_v) * units,
            )
    return uv_layer


def _bind_material_uv(
    material, tex_node, studs_per_tile=None, scale_node_name="RBX Material UV Scale"
):
    """Sample ``tex_node`` from the stud-density material UV layer.

    One UVMap node per material feeds every material texture node instead
    of allocating one per map.  A MaterialVariant with a different
    studs-per-tile density scales the shared UV layer through a VectorMath
    node instead of rebuilding the mesh attribute.  Callers that must not
    retile the material's own maps (a child Texture instance has its own
    density) pass a distinct ``scale_node_name``: the extra scale node still
    feeds from the raw shared UV layer, so densities never compound.
    """
    uv_node = material.node_tree.nodes.get("RBX Material UV")
    if uv_node is None:
        uv_node = material.node_tree.nodes.new("ShaderNodeUVMap")
        uv_node.uv_map = _BUILTIN_MATERIAL_UV_LAYER
        uv_node.name = "RBX Material UV"
        uv_node.label = "RBX Material UV (fixed studs-per-tile)"
    uv_node.location = (tex_node.location[0], tex_node.location[1] - 120)
    source_socket = uv_node.outputs["UV"]
    try:
        if not studs_per_tile:
            studs_per_tile = _BUILTIN_MATERIAL_STUDS_PER_TILE
        studs_per_tile = float(studs_per_tile)
        if studs_per_tile <= 0.0:
            studs_per_tile = _BUILTIN_MATERIAL_STUDS_PER_TILE
    except (TypeError, ValueError):
        studs_per_tile = _BUILTIN_MATERIAL_STUDS_PER_TILE
    if abs(studs_per_tile - _BUILTIN_MATERIAL_STUDS_PER_TILE) > 1e-6:
        scale_node = material.node_tree.nodes.get(scale_node_name)
        if scale_node is None:
            scale_node = material.node_tree.nodes.new("ShaderNodeVectorMath")
            scale_node.operation = "SCALE"
            scale_node.name = scale_node_name
        scale_node.label = f"RBX Material UV ({studs_per_tile:g} studs/tile)"
        scale_node.inputs["Scale"].default_value = (
            _BUILTIN_MATERIAL_STUDS_PER_TILE / studs_per_tile
        )
        scale_node.location = (uv_node.location[0] + 260, uv_node.location[1])
        _link(material, uv_node.outputs["UV"], scale_node.inputs[0])
        source_socket = scale_node.outputs[0]
    _link(material, source_socket, tex_node.inputs["Vector"])


def _builtin_map_ref(value) -> str:
    """Normalize a built-in map id or variant map ref to a fetchable ref.

    Base-material tables carry bare asset ids (ints OR digit strings);
    MaterialVariant entries carry full content strings.  Only refs already in
    a fetchable form (rbxasset/rbxassetid/url) pass through untouched.
    """
    if not value:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        if stripped.lower().startswith(("rbxasset", "http")):
            return value
        if stripped.isdigit():
            return f"rbxassetid://{stripped}"
        return value
    return f"rbxassetid://{value}"


def _apply_builtin_material(material, principled, part_name, entry, builtin):
    """Wire a Roblox built-in material's PBR maps into the shader.

    Roblox multiplies the material colour map by the part Color3, so the
    colour map uses the tinted multiply path; normal/roughness/metalness
    maps bind like their SurfaceAppearance equivalents.
    """
    name = builtin["name"]
    color_map, normal_map, metalness_map, roughness_map = builtin["maps"]
    variant_data = entry.get("material_variant_data")
    variant_studs = None
    if variant_data:
        color_map = variant_data.get("color_map") or ""
        normal_map = variant_data.get("normal_map") or ""
        metalness_map = variant_data.get("metalness_map") or ""
        roughness_map = variant_data.get("roughness_map") or ""
        variant_studs = variant_data.get("studs_per_tile")
    color = entry.get("color")
    tint = tuple(max(0.0, min(1.0, float(c))) for c in color[:3]) if color else None
    color_ref = _builtin_map_ref(color_map)

    if color_ref:
        # MaterialVariant colour maps arrive linear-encoded (unlike the
        # base-material colour maps, which are sRGB); sampling a variant
        # map through the sRGB transfer curve double-gammas it.  Variant
        # maps therefore bind Non-Color like the other data maps.
        variant_color = bool(variant_data)
        image = fetch_texture_image(
            color_ref, name=f"{part_name}_matcolor", non_color=variant_color
        )
        image = _data_image_view(image) if variant_color else _color_image_view(image)
        # The graph is built even while image bytes are deferred; hydration
        # only assigns the image datablock afterwards.
        tex_node = _image_texture_node(material, image, "MaterialColor")
        tex_node.name = "RBX Material ColorMap"
        tex_node.label = (
            "RBX MaterialVariant ColorMap (× part colour)"
            if variant_data
            else f"RBX {name} ColorMap (× part colour)"
        )
        tex_node.location = (-400, 0)
        _bind_material_uv(material, tex_node, variant_studs)
        base_input = principled.inputs["Base Color"]
        for link in list(base_input.links):
            material.node_tree.links.remove(link)
        _bind_linear_tint(material, tex_node, tint is not None, base_input)
        _activate_texture_node(material, tex_node)
    elif tint is not None:
        # Built-ins without a colour map (Plastic, SmoothPlastic, Neon): the
        # part colour IS the diffuse.  The Vertex Color node decodes the
        # stored sRGB values to linear, so the colour socket gets exactly
        # what an RGB node with the same sRGB colour would produce.
        nodes = material.node_tree.nodes
        color_attr = nodes.new("ShaderNodeVertexColor")
        color_attr.layer_name = "RBXColor"
        color_attr.name = "RBX Material ColorAttr"
        color_attr.label = "RBX part colour (RBXColor vertex colours)"
        color_attr.location = (-400, 0)
        _link(material, color_attr.outputs["Color"], principled.inputs["Base Color"])
        if name == "Neon":
            # Neon glows in its own part colour.  Drive emission from the
            # same attribute as the diffuse: built-in materials collapse
            # colour into the RBXColor layer, and the object-tint/baked-tint
            # paths rewire exactly this node — a static default would bake
            # the FIRST part's colour into every neon part's glow.
            _link(
                material,
                color_attr.outputs["Color"],
                principled.inputs["Emission Color"],
            )
            principled.inputs["Emission Strength"].default_value = 2.0
            principled.inputs["Roughness"].default_value = 0.25
        elif name == "SmoothPlastic":
            principled.inputs["Roughness"].default_value = 0.12
            specular = (
                principled.inputs.get("Specular IOR Level")
                or principled.inputs.get("Specular")
            )
            if specular is not None:
                specular.default_value = 0.5
    elif name == "SmoothPlastic":
        principled.inputs["Roughness"].default_value = 0.12
        specular = (
            principled.inputs.get("Specular IOR Level")
            or principled.inputs.get("Specular")
        )
        if specular is not None:
            specular.default_value = 0.5

    for ref, socket_name, location in (
        (normal_map, "Normal", (-400, -400)),
        (roughness_map, "Roughness", (-400, -250)),
        (metalness_map, "Metallic", (-400, -100)),
    ):
        if not ref:
            continue
        image = fetch_texture_image(
            _builtin_map_ref(ref), name=f"{part_name}_mat{socket_name.lower()}",
            non_color=True,
        )
        image = _data_image_view(image)
        tex_node = _image_texture_node(material, image, f"Material{socket_name}")
        tex_node.name = f"RBX Material {socket_name}"
        tex_node.label = f"RBX {name} {socket_name}"
        tex_node.location = location
        _bind_material_uv(material, tex_node, variant_studs)
        if socket_name == "Normal":
            normal_node = material.node_tree.nodes.new("ShaderNodeNormalMap")
            normal_node.name = "RBX Material NormalMap"
            normal_node.label = f"RBX {name} NormalMap (strength here)"
            normal_node.location = (-150, -400)
            _link(material, tex_node.outputs["Color"], normal_node.inputs["Color"])
            _link(material, normal_node.outputs["Normal"], principled.inputs["Normal"])
        else:
            _link(material, tex_node.outputs["Color"], principled.inputs[socket_name])


def build_part_material(
    part_name: str,
    entry: dict,
    material_name: Optional[str] = None,
    reset_nodes: bool = True,
    material=None,
):
    """Build (or rebuild, when ``material`` is given) a part material.

    ``material`` selects the datablock to rebuild IN PLACE (hydration and
    stale-cache repairs keep the same datablock so existing object slots
    stay valid).  Without it a fresh datablock is created; its name is a
    label only.
    """
    if material is None:
        material = _new_material(
            material_name or f"rbx_{part_name}",
            reset_nodes=reset_nodes,
        )
    elif reset_nodes:
        _reinit_material(material)
    principled = _principled_node(material)
    if principled is None:
        return material

    # Blender display names for place imports are namespaced as
    # ``<place>.place/<Part>.<Class>``.  Gameplay semantics such as classic
    # clothing must use the original Roblox Part.Name, never that display name.
    source_part_name = entry.get("name") or part_name

    # Base color from Color3uint8 (sRGB-ish approximation), with the
    # HumanoidDescription per-limb color taking priority when present.
    color = entry.get("color")
    try:
        from . import clothing

        hd = clothing._hd_color_override(source_part_name)
        if hd is not None:
            color = hd
    except Exception:
        pass

    # Union (CSG) meshes carry per-vertex colors baked by Studio's mesher —
    # for multi-color unions these differ from the part's Color3uint8, so the
    # shader must read the RBXColor attribute (written by
    # creation._apply_mesh_vertex_colors) instead of the flat part color.
    union_mesh = entry.get("union_mesh") or {}
    union_colors = union_mesh.get("colors") if isinstance(union_mesh, dict) else None
    # Regular FileMesh parts whose asset carries baked vertex colors: the
    # geometry pass flagged the entry, and the mesh's RBXColor attribute
    # holds the varying colors.  SurfaceAppearance colour maps override them
    # (the SA build below owns Base Color).
    mesh_baked_colors = bool(
        entry.get("_mesh_vertex_colors")
        and not union_colors
        and not any(
            (sa or {}).get("color_map") for sa in _entry_surface_appearances(entry)
        )
    )
    # SurfaceAppearance overrides the CSG-baked vertex colours (its colour
    # map, Overlay or Transparency, drives the look) — the vertex-colour
    # chain must yield to the SA build below.
    use_vertex_colors = bool(
        union_colors
        and not any(
            (sa or {}).get("color_map") for sa in _entry_surface_appearances(entry)
        )
        and any(
            abs(c[0] - union_colors[0][0]) > 1e-3
            or abs(c[1] - union_colors[0][1]) > 1e-3
            or abs(c[2] - union_colors[0][2]) > 1e-3
            for c in union_colors[:64]
        )
    )
    if use_vertex_colors:
        attr_node = material.node_tree.nodes.new("ShaderNodeVertexColor")
        attr_node.layer_name = "RBXColor"
        attr_node.name = "RBX VertexColor"
        attr_node.label = "RBX VertexColor (union per-vertex color)"
        attr_node.location = (-400, 300)
        _link(material, attr_node.outputs["Color"], principled.inputs["Base Color"])
        # Solid/Material mode can't read the attribute node — fall back to the
        # dominant vertex color so the part isn't the default grey there.
        if union_colors:
            dom = max(set(union_colors), key=union_colors.count)
            try:
                material.diffuse_color = (dom[0], dom[1], dom[2], 1.0)
            except Exception:
                pass
    elif color:
        rgba = (color[0], color[1], color[2], 1.0)
        _set_base_color(material, rgba)

    # Roblox Plastic is colour-led: Color3 is the visual identity of an
    # ordinary BasePart, while its dielectric highlight is deliberately
    # restrained. Blender Principled's default IOR/specular response is much
    # whiter and more prominent, making near-white Roblox pavement look grey
    # under the same lighting. Preserve the enum so plain Parts do not all
    # share Blender's unrelated generic-material response.
    if entry.get("material") == 256:
        principled.inputs["Metallic"].default_value = 0.0
        principled.inputs["Roughness"].default_value = 0.62
        # Blender 4+ calls this socket "Specular IOR Level"; keep the fallback
        # for older releases where it was named simply "Specular".
        specular = (
            principled.inputs.get("Specular IOR Level")
            or principled.inputs.get("Specular")
        )
        if specular is not None:
            specular.default_value = 0.20
        material["rbx_material"] = "Plastic"

    # SurfaceAppearance layers, bottom first.  A part can carry SEVERAL
    # SurfaceAppearance children (and several classic Texture children) —
    # they stack like layers: colour maps composite in order, and for each
    # PBR map the LAST layer that provides it wins.  Dropping every layer
    # after the first (the old behaviour) left the stacked trim sheets half
    # missing, which is the dark, wrong-looking wall problem.
    surface_list = _entry_surface_appearances(entry)
    # AlphaMode: 0=Overlay, 1=Transparency.
    texture_alpha_used = False
    color_map_ref = ""
    overlay_layers = []
    layer_index = 0
    for surface in surface_list:
        sa_color = surface.get("color_map")
        alpha_mode = int(surface.get("alpha_mode") or 0)
        if sa_color:
            color_map_ref = sa_color
        if not sa_color:
            continue
        image = fetch_texture_image(sa_color, name=f"{part_name}_color")
        image = _color_image_view(image)
        # Build the wiring even when the image bytes are deferred; hydration
        # only assigns the datablock afterwards.
        tex_node = _image_texture_node(material, image, "ColorMap")
        tex_node.name = _layer_node_name("RBX ColorMap", layer_index)
        tex_node.label = "RBX ColorMap (SurfaceAppearance)"
        tex_node.location = (-400, 0)
        if alpha_mode == 1:
            # Transparency: RGB drives Base Color, alpha cuts the part
            # against the world (foliage, fences).  It REPLACES the colour
            # chain beneath it.  BasePart.Transparency multiplies into the
            # map alpha, same as Studio.
            try:
                part_alpha = 1.0 - max(0.0, min(1.0, float(entry.get("transparency", 0.0))))
            except (TypeError, ValueError):
                part_alpha = 1.0
            _bind_transparency_alpha(material, principled, tex_node, alpha_scale=part_alpha)
            texture_alpha_used = True
            overlay_layers.append({
                "node": tex_node.name,
                "ref": sa_color,
                "mode": "transparency",
            })
        else:
            # Overlay: the texture alpha reveals whatever is beneath, so a
            # masked trim sheet (black RGB + alpha pattern) must not render
            # its raw RGB — composite map x a over the current colour
            # source, which may be an earlier layer.
            tag = _layer_tag(layer_index)
            _bind_overlay_alpha(material, principled, tex_node, color, tag=tag)
            overlay_layers.append({
                "node": tex_node.name,
                "ref": sa_color,
                "mode": "overlay",
                "tag": tag,
            })
        layer_index += 1
        _activate_texture_node(material, tex_node)

    # SurfaceAppearance PBR maps. Assets ship either a single packed
    # metalness+roughness texture (R=metalness, G=roughness) or separate
    # grayscale maps per property; handle both. Normal maps need a Non-Color
    # image + NormalMap node. When both sockets reference the same packed
    # asset, decode it once and drive Metallic/Roughness off a single
    # separate-color node (2 nodes) instead of per-socket copies (4 nodes).
    # Topmost layer wins per map: iterating bottom-first, the last
    # assignment holds.
    metalness_ref = ""
    roughness_ref = ""
    normal_map_ref = ""
    for layer in surface_list:
        if layer.get("metalness_map"):
            metalness_ref = layer.get("metalness_map")
        if layer.get("roughness_map"):
            roughness_ref = layer.get("roughness_map")
        if layer.get("normal_map"):
            normal_map_ref = layer.get("normal_map")
    packed_mr = bool(metalness_ref and metalness_ref == roughness_ref)
    separate_mr = None
    selected_refs = {
        "metalness_map": metalness_ref,
        "roughness_map": roughness_ref,
    }
    for surface_key, socket_name, packed_channel in (
        ("metalness_map", "Metallic", "Red"),
        ("roughness_map", "Roughness", "Green"),
    ):
        map_ref = selected_refs[surface_key]
        if not map_ref:
            continue
        # Packed assets decode once: the first slot builds the shared tex +
        # split pair, the second only wires the remaining channel.
        if packed_mr and separate_mr is not None:
            channel_out = (
                separate_mr.outputs.get(packed_channel)
                or separate_mr.outputs.get(packed_channel[0])
            )
            _link(material, channel_out, principled.inputs[socket_name])
            continue
        image = fetch_texture_image(
            map_ref, name=f"{part_name}_{surface_key}", non_color=True
        )
        image = _data_image_view(image)
        tex_node = _image_texture_node(material, image, surface_key)
        tex_node.name = (
            "RBX MetalRough Map" if packed_mr else f"RBX Surface {surface_key}"
        )
        tex_node.location = (-400, -100 if surface_key == "metalness_map" else -250)
        if packed_mr:
            # Separate RGB was removed in Blender 5.1. Separate Color is its
            # replacement, while the fallback keeps older Blender releases
            # working with the same packed Roblox texture convention.
            try:
                separate_mr = material.node_tree.nodes.new("ShaderNodeSeparateColor")
                separate_mr.mode = "RGB"
                input_name = "Color"
            except RuntimeError:
                separate_mr = material.node_tree.nodes.new("ShaderNodeSeparateRGB")
                input_name = "Image"
            separate_mr.name = "RBX MetalRough Split"
            separate_mr.label = "RBX MetalRough Split (packed R=metal G=rough)"
            separate_mr.location = (-150, -175)
            _link(material, tex_node.outputs["Color"], separate_mr.inputs[input_name])
            # Legacy SeparateRGB used single-letter channel names (R/G).
            channel_out = (
                separate_mr.outputs.get(packed_channel)
                or separate_mr.outputs.get(packed_channel[0])
            )
            _link(material, channel_out, principled.inputs[socket_name])
        else:
            # Grayscale scalar map.
            _link(material, tex_node.outputs["Color"], principled.inputs[socket_name])

    normal_map_ref = normal_map_ref or ""
    if normal_map_ref:
        image = fetch_texture_image(
            normal_map_ref, name=f"{part_name}_normal", non_color=True
        )
        image = _data_image_view(image)
        tex_node = _image_texture_node(material, image, "NormalMapTex")
        tex_node.name = "RBX NormalMapTex"
        tex_node.label = "RBX NormalMapTex (PBR)"
        tex_node.location = (-400, -400)
        normal_node = material.node_tree.nodes.new("ShaderNodeNormalMap")
        normal_node.name = "RBX NormalMap"
        normal_node.label = "RBX NormalMap (strength here)"
        normal_node.location = (-150, -400)
        _link(material, tex_node.outputs["Color"], normal_node.inputs["Color"])
        _link(material, normal_node.outputs["Normal"], principled.inputs["Normal"])

    # MeshPart.TextureID (classic accessories without SurfaceAppearance).
    # Only when no SurfaceAppearance color map was bound, since the two
    # address the same Base Color slot and SurfaceAppearance wins on Roblox.
    # Skinned/rthro heads skip this: their TextureID is a grayscale+alpha tint
    # map that the clothing pipeline bakes into an opaque composite (raw
    # binding renders black and vanishes in solid/texture viewport mode).
    # Classic R6 dynamic heads also skip it: their SpecialMesh.TextureId is
    # a face map the engine draws OVER the head color, so it rides the
    # clothing head bake (face_texture in the context); the opaque multiply
    # here would let the map's transparent skin texels eat the head color.
    #
    # The classic pipeline draws these textures fully OPAQUE (the image's
    # alpha channel is unused there), so no alpha is bound: the multiply
    # keeps the part-colour tint, and leaving the material out of the
    # transparent pass avoids BLEND sorting/overlap artifacts on accessory
    # meshes like ears that overlap themselves and the head.
    texture_id_ref = entry.get("texture_id")
    is_head = (source_part_name or "").strip().lower() == "head"
    is_classic_part_head = is_head and entry.get("class_name") == "Part"
    head_face_baked = False
    if is_classic_part_head:
        try:
            from . import clothing

            head_face_baked = bool(clothing._CLOTHING_CONTEXT.get("face_texture"))
        except Exception:
            head_face_baked = False
    if texture_id_ref and not color_map_ref and (
        not is_head or (is_classic_part_head and not head_face_baked)
    ):
        image = fetch_texture_image(texture_id_ref, name=f"{part_name}_texid")
        image = _color_image_view(image)
        tex_node = _image_texture_node(material, image, "TextureID")
        tex_node.name = "RBX TextureID"
        tex_node.label = "RBX TextureID (mesh texture)"
        tex_node.location = (-400, 0)
        # Classic Texture children tile at their own studs-per-tile density
        # (a planar per-face projection), unlike native FileMesh UVs.
        if entry.get("texture_studs_per_tile"):
            _bind_material_uv(material, tex_node, entry["texture_studs_per_tile"])
        _bind_transparency_alpha(
            material, principled, tex_node, tint=color, bind_alpha=False
        )
        texture_alpha_used = False
        _activate_texture_node(material, tex_node)

    # Built-in materials (Enum.Material in the binary place format). Applied
    # only when neither SurfaceAppearance nor TextureID claimed Base Color —
    # both override the material look on Roblox.  A child Texture instance is
    # NOT an override: it draws over the material, so the material is kept.
    builtin = _builtin_material_for_entry(entry)
    if builtin is not None and not color_map_ref and not texture_id_ref:
        _apply_builtin_material(material, principled, source_part_name, entry, builtin)

    # Child Texture instances: surface textures over the part (and its
    # material).  Composite each by image alpha so transparent texels reveal
    # the material underneath instead of cutting holes in the part.  Several
    # instances stack bottom-first over whatever colour chain the
    # SurfaceAppearance layers left behind.
    texture_instances = _entry_texture_instances(entry)
    if texture_instances and not texture_id_ref and not is_head:
        for texture_instance in texture_instances:
            texture_instance_ref = texture_instance.get("texture")
            if not texture_instance_ref:
                continue
            image = fetch_texture_image(texture_instance_ref, name=f"{part_name}_texinst")
            image = _color_image_view(image)
            if image is not None:
                try:
                    image.alpha_mode = "CHANNEL_PACKED"
                except (AttributeError, TypeError):
                    pass
            tex_node = _image_texture_node(material, image, "TextureInstance")
            tex_node.name = _layer_node_name("RBX TextureInstance", layer_index)
            tex_node.label = "RBX Texture instance (surface texture over part)"
            tex_node.location = (-400, 0)
            if texture_instance.get("studs_per_tile"):
                # The instance tiles at its own density; it must not retile
                # the material's maps, so it gets a dedicated scale chain.
                _bind_material_uv(
                    material,
                    tex_node,
                    texture_instance["studs_per_tile"],
                    scale_node_name=_layer_node_name(
                        "RBX Instance UV Scale", layer_index
                    ),
                )
            else:
                # No own density: sample the shared material UV layer.  An
                # unlinked Vector input renders the node BLACK in Eevee
                # (Cycles silently falls back to the first UV layer), which
                # was the dark-wall path in the viewport.
                _bind_material_uv(material, tex_node)
            try:
                instance_alpha = 1.0 - max(
                    0.0, min(1.0, float(texture_instance.get("transparency", 0.0)))
                )
            except (TypeError, ValueError):
                instance_alpha = 1.0
            print(
                f"[RbxTexture] instance '{material.name}' "
                f"transparency={texture_instance.get('transparency')} "
                f"fade={round(instance_alpha, 4)}"
            )
            try:
                instance_tint = tuple(
                    float(component) for component in texture_instance.get("color") or ()
                )
            except (TypeError, ValueError):
                instance_tint = None
            tag = _layer_tag(layer_index)
            # Decal semantics: only the TEXTURE fades.  The composite mixes
            # the part colour with the texture colour by the image alpha
            # scaled by (1 - Transparency); Principled Alpha stays 1 so the
            # part itself never turns transparent.
            _bind_overlay_alpha(
                material,
                principled,
                tex_node,
                color,
                alpha_scale=instance_alpha,
                tint=instance_tint,
                tag=tag,
            )
            overlay_layers.append({
                "node": tex_node.name,
                "ref": texture_instance_ref,
                "mode": "overlay",
                "tag": tag,
            })
            layer_index += 1
            _activate_texture_node(material, tex_node)

    # Face decal: composited with the head body color inside the clothing
    # pipeline (clothing.get_limb_texture), applied by _apply_clothing_bake
    # below. Nothing to do here anymore.

    # Classic clothing (shirt/pants/bodycolor) baked into a per-limb crop.
    _apply_clothing_bake(material, principled, source_part_name, entry)
    # Asset-baked mesh vertex colors multiply the part tint (last, so they
    # compose with whichever Base Color chain the paths above built).
    _apply_mesh_vertex_color_tint(material, principled, color, mesh_baked_colors)
    _apply_part_transparency(
        material, principled, entry, texture_alpha=texture_alpha_used
    )
    try:
        material["RBXOverlayLayers"] = json.dumps(overlay_layers, sort_keys=True)
    except (TypeError, ValueError):
        material["RBXOverlayLayers"] = "[]"
    material["RBXTextureHydrated"] = _DEFER_TEXTURE_IMAGES == 0
    if _DEFER_TEXTURE_IMAGES:
        # Stamped at BUILD time (not just refresh time) so the finalize
        # rehydration sweep can find deferred materials whose refresh was
        # never queued; without it an image-less material is unsweepable.
        try:
            material["RBXDeferredEntryJson"] = json.dumps(
                entry, separators=(",", ":"), default=str
            )
        except (TypeError, ValueError):
            pass
    material["RBXMaterialGraphVersion"] = _MATERIAL_GRAPH_VERSION
    _organize_material_nodes(material)

    # NOTE: primitive Parts with classic surface textures (Studs/Inlet/Glue/
    # Universal) are NOT handled here — they need per-face materials, which
    # apply_part_material builds via _primitive_surface_material.

    return material


def _apply_clothing_bake(material, principled, part_name, entry):
    """Bind the limb's baked clothing crop as its base color texture.

    The bake (body color + pants/shirt projected through Roblox's compositing
    guide meshes) happens once per limb group in ``clothing.get_limb_texture``;
    the mesh keeps its native UVs, which already address its atlas crop.
    """
    try:
        from . import clothing
    except Exception:
        return
    if not clothing.is_clothing_limb(part_name):
        return

    # Classic R6 dynamic heads are plain Parts whose SpecialMesh.TextureId
    # is a full-colour face texture; the standard TextureID path renders it.
    # The clothing bake would overwrite it with a body-colour tint composite.
    # R15 rthro heads (MeshParts) keep the grayscale tint bake.
    if (
        clothing._is_head(part_name)
        and not clothing._CLOTHING_CONTEXT.get("face_texture")
        and entry.get("class_name") == "Part"
    ):
        return

    color = entry.get("color") or (1.0, 1.0, 1.0)
    image = clothing.get_limb_texture(
        part_name,
        (color[0], color[1], color[2], 1.0),
        tint_ref=entry.get("texture_id"),
    )
    if image is None:
        return
    tex_node = _image_texture_node(material, image, "ClothingBake")
    tex_node.name = "RBX ClothingBake"
    tex_node.label = "RBX Clothing Bake (shirt/pants/skin composite)"
    tex_node.location = (-400, 0)
    _bind_named_uv(material, tex_node, "UVMap", "RBX Clothing UV")
    _link(material, tex_node.outputs["Color"], principled.inputs["Base Color"])
    # Solid/Texture viewport mode draws the material's ACTIVE image node;
    # make it the opaque bake so limbs never sample a transparent source.
    try:
        material.node_tree.nodes.active = tex_node
    except Exception:
        pass


# ── Roblox classic-surface atlas ──────────────────────────────────────
# Part1_diff.png / Part1_nmap.png are 128x2048 atlases split into four
# 128x512 bands (verified against Roblox's content textures):
#   band 0 (y    0- 512): Studs      (SurfaceType 3)
#   band 1 (y  512-1024): Glue       (SurfaceType 1)
#   band 2 (y 1024-1536): Inlet      (SurfaceType 4)
#   band 3 (y 1536-2048): Universal  (SurfaceType 5)
# Each band is a 64px-per-stud tiling pattern.  The bands are cropped into
# separate images because Blender's REPEAT wrap only wraps at UV 0..1 — a
# face longer than 8 studs sampling the full atlas would bleed into the next
# band, while a cropped 128x512 band image tiles over any face length.
# Surface type -> atlas band.  Band order matches the 2016 renderer's
# getStudsAtlasInfo offsets: Studs (0), Glue/Weld share band 1, Inlet (2),
# Universal (3).  Weld renders with the glue band in the engine.
_SURFACE_TYPE_TO_BAND = {3: 0, 1: 1, 2: 1, 4: 2, 5: 3}
_BAND_PX = 512

_ATLAS_CACHE: dict = {}
_BAND_IMAGE_CACHE: dict = {}
_TINTED_SURFACE_BAND_CACHE: dict = {}
_PRIMITIVE_MAT_CACHE: dict = {}


def _atlas_image(kind):
    """Load the classic-surface atlas ('diff' or 'norm') once per session."""
    img = _ATLAS_CACHE.get(kind)
    if _live_image(img) is not None:
        return img
    import os as _os

    tex_dir = _os.path.join(_os.path.dirname(__file__), "..", "textures")
    fname = "Part1_diff.png" if kind == "diff" else "Part1_nmap.png"
    path = _os.path.join(tex_dir, fname)
    if not _os.path.isfile(path):
        return None
    img = bpy.data.images.load(path, check_existing=True)
    _ATLAS_CACHE[kind] = img
    return img


def _band_image(kind, band):
    """Extract one 128x512 band from the atlas as its own tileable image.

    Cached globally — every part shares the same band images instead of
    packing a private atlas copy per part (which used to embed 565+ atlas
    copies into the .blend for a single map).
    """
    key = (kind, int(band))
    img = _BAND_IMAGE_CACHE.get(key)
    if _live_image(img) is not None:
        return img
    src = _atlas_image(kind)
    if src is None:
        return None
    try:
        import numpy as np  # bundled with Blender

        w, h = src.size  # 128, 2048
        px = np.empty(w * h * 4, dtype=np.float32)
        src.pixels.foreach_get(px)
        px = px.reshape(h, w, 4)
        # bpy pixel rows are bottom-up: atlas band k (counted from the image
        # top) occupies bpy rows [(4-k)*512 - 512, (4-k)*512).
        r1 = (4 - int(band)) * _BAND_PX
        r0 = r1 - _BAND_PX
        band_px = np.ascontiguousarray(px[r0:r1])
        if kind == "norm":
            band_px = _symmetrize_normal_band(band_px, w)
        img = bpy.data.images.new(
            f"rbx_surface_{kind}_{band}", width=w, height=_BAND_PX, alpha=True
        )
        if kind == "norm":
            # Must be set BEFORE foreach_set: switching colorspace on a
            # generated (file-less) image afterwards makes Blender drop the
            # pixel buffer entirely — the band turns black and every studded
            # face gets a bogus (-1,-1,-1) normal (the "alternating dark
            # parts" artifact).
            try:
                img.colorspace_settings.name = "Non-Color"
            except Exception:
                pass
        img.pixels.foreach_set(band_px.ravel())
        img.pack()  # embed in .blend
        _BAND_IMAGE_CACHE[key] = img
        return img
    except Exception as exc:
        print(f"[RbxTexture] Failed to extract {kind} band {band}: {exc}")
        return None


def _srgb_decode_scalar(value: float) -> float:
    """sRGB EOTF with the linear branch (matches the shader's socket
    conversion, including values above 1 which extend the power curve)."""
    value = float(value)
    if value <= 0.04045:
        return value * (1.0 / 12.92)
    return ((value + 0.055) * (1.0 / 1.055)) ** 2.4


def _srgb_encode_scalar(value: float) -> float:
    """Inverse sRGB EOTF (linear -> encoded), clamped at zero below."""
    value = float(value)
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * (max(value, 0.0) ** (1.0 / 2.4)) - 0.055


def _tinted_surface_band(kind, band, color):
    """Return a viewport-ready, Roblox-tinted classic-surface image.

    Solid/Texture mode displays the active image node verbatim, so it cannot
    evaluate the shader's multiply tint. Bake that multiply into a cached
    image rather than showing the raw grey stud/inlet/glue atlas.
    """
    source = _band_image(kind, band)
    if source is None:
        return None
    r, g, b = (max(0.0, min(1.0, float(component))) for component in color[:3])
    key = (kind, int(band), round(r, 4), round(g, 4), round(b, 4))
    cached = _TINTED_SURFACE_BAND_CACHE.get(key)
    if _live_image(cached) is not None:
        return cached
    try:
        # Match the render shader's x2 part-colour compensation exactly:
        # the tint rides an sRGB-managed colour socket, so the atlas pixels
        # AND the tint decode to linear before the multiply, and the baked
        # viewport image re-encodes the result (it is sRGB-tagged).  The
        # old encoded-space multiply diverged from the render for saturated
        # part colours.
        tint = (min(r * 2.0, 4.0), min(g * 2.0, 4.0), min(b * 2.0, 4.0))
        linear_tint = tuple(_srgb_decode_scalar(component) for component in tint)
        try:
            import numpy as np  # bundled with Blender

            pixels = np.empty(source.size[0] * source.size[1] * 4, dtype=np.float32)
            source.pixels.foreach_get(pixels)
            arr = pixels.reshape(-1, 4)
            rgb = arr[:, :3]
            low = rgb <= 0.04045
            lin = np.where(
                low, rgb * (1.0 / 12.92), ((rgb + 0.055) * (1.0 / 1.055)) ** 2.4
            )
            for channel in range(3):
                lin[:, channel] *= linear_tint[channel]
            low_e = lin <= 0.0031308
            arr[:, :3] = np.where(
                low_e,
                lin * 12.92,
                1.055 * np.maximum(lin, 0.0) ** (1.0 / 2.4) - 0.055,
            )
        except ImportError:
            pixels = array("f", [0.0]) * len(source.pixels)
            source.pixels.foreach_get(pixels)
            for index in range(0, len(pixels), 4):
                for channel in range(3):
                    linear = _srgb_decode_scalar(pixels[index + channel])
                    pixels[index + channel] = _srgb_encode_scalar(
                        linear * linear_tint[channel]
                    )
        image = bpy.data.images.new(
            f"rbx_surface_{kind}_{band}_tint_{r:.3f}_{g:.3f}_{b:.3f}",
            width=source.size[0],
            height=source.size[1],
            alpha=True,
        )
        image.pixels.foreach_set(pixels)
        image.update()
        image.pack()
        _TINTED_SURFACE_BAND_CACHE[key] = image
        return image
    except Exception as exc:
        print(f"[RbxTexture] Failed to tint {kind} surface band {band}: {exc}")
        return source


def _symmetrize_normal_band(band_px, width):
    """Make each 64px stud cell of a normal band fully dihedral-symmetric (D4).

    The atlas's stud bevel is directional (its highlight is baked on one side),
    and the tangent frame a face samples it with is not fixed: opposite faces
    get mirrored UV tangents, and part rotations / the longer-edge U-V swap in
    creation._generate_face_uvs rotate the tangent frame by 90° steps.  Folding
    the cell about its centre axes alone (mirror symmetry) fixes the mirrored
    faces but leaves the bump dependent on part rotation.  Averaging over all 8
    isometries of the square (4 rotations x optional mirror) makes the bump
    radially symmetric — invariant under any axis-aligned tangent frame — while
    keeping its height profile.  Each fold re-signs the X/Y normal components
    with the same linear map as its pixel transform, so the symmetric bump
    stays a valid outward normal map.
    """
    import numpy as np

    band = band_px.copy()
    h = band.shape[0]
    for cy in range(0, h, 64):
        for cx in range(0, width, 64):
            cell = band[cy:cy + 64, cx:cx + 64]
            n = cell[:, :, :3] * 2.0 - 1.0  # decode tangent-space normal
            acc = np.zeros_like(n)
            for k in range(4):
                # np.rot90(m, 1) maps pixels (x, y) -> (y, -x), so the normal
                # vector gets the same linear map: (nx, ny) -> (ny, -nx).
                # (copy(): rot90 returns a view that writes through to n.)
                r = np.rot90(n, k).copy()
                vx, vy = r[:, :, 0].copy(), r[:, :, 1].copy()
                if k == 1:
                    r[:, :, 0], r[:, :, 1] = vy, -vx
                elif k == 2:
                    r[:, :, 0], r[:, :, 1] = -vx, -vy
                elif k == 3:
                    r[:, :, 0], r[:, :, 1] = -vy, vx
                # Mirror of the rotation: a mirror across the vertical axis
                # negates nx.
                m = r[:, ::-1].copy()
                m[:, :, 0] *= -1
                acc += r + m
            sym = acc / 8.0
            # Re-encode, preserving alpha.
            cell[:, :, :3] = np.clip(sym * 0.5 + 0.5, 0.0, 1.0)
    return band


def _primitive_surface_material(color, surface_type, transparency=0.0):
    """Material for one classic surface type, tinted by the part colour.

    Shared across every part with the same (colour, surface type) pair.
    Roblox renders these textures greyscale and tints them by Color3, so the
    diffuse is MULTIPLIED by the part colour (x2 to compensate the ~0.5 grey
    mean), never used as a replacement.
    """
    r = g = b = 1.0
    if color:
        r, g, b = float(color[0]), float(color[1]), float(color[2])
    surface_type = int(surface_type)
    try:
        transparency = max(0.0, min(1.0, float(transparency)))
    except (TypeError, ValueError):
        transparency = 0.0
    cache_key = (round(r, 4), round(g, 4), round(b, 4), surface_type, round(transparency, 4))
    material = _PRIMITIVE_MAT_CACHE.get(cache_key)
    if _live_material(material) is not None:
        return material

    material = bpy.data.materials.new(
        f"rbx_surf{surface_type}_{cache_key[0]:.3f}_{cache_key[1]:.3f}_{cache_key[2]:.3f}"
    )
    material.use_nodes = True
    nodes = material.node_tree.nodes
    principled = _principled_node(material)
    if principled is None:
        return material
    principled.inputs["Base Color"].default_value = (r, g, b, 1.0)
    # Solid/Workbench "Material" viewport mode reads diffuse_color (the stud
    # texture only shows in its "Texture" mode); keep it in sync with the part
    # colour so studded parts aren't grey in Solid mode.
    try:
        material.diffuse_color = (r, g, b, 1.0)
    except Exception:
        pass
    _apply_part_transparency(material, principled, {"transparency": transparency})

    band = _SURFACE_TYPE_TO_BAND.get(surface_type)
    if band is not None:
        diff = _band_image("diff", band)
        if diff is not None:
            # Node names are stable hooks — users can find/swap these in the
            # shader editor (or via scripts) without reading this file:
            #   "RBX Stud Diffuse" -> set .image to replace the texture
            #   "RBX Tint"         -> edit socket A to re-tint the surface
            tex_node = _image_texture_node(material, diff, "SurfaceDiffuse")
            tex_node.name = "RBX Stud Diffuse"
            tex_node.label = "RBX Stud Diffuse (swap image to re-texture)"
            tex_node.extension = "REPEAT"
            tex_node.location = (-500, 300)
            _bind_named_uv(material, tex_node, "UVMap", "RBX Surface UV")
            mix_node, tint_a, tint_b, factor, tint_result = _new_mix_node(
                material, "RBX Tint", "RBX Tint (part colour ×2)", "MULTIPLY",
            )
            if factor is not None:
                factor.default_value = 1.0
            # x2 part colour: the band's ~0.5 grey mean multiplies back to ~1.
            _set_color_default(
                tint_a,
                (min(r * 2.0, 4.0), min(g * 2.0, 4.0), min(b * 2.0, 4.0), 1.0),
            )
            mix_node.location = (-250, 300)
            _link(material, tex_node.outputs["Color"], tint_b)
            _link(material, tint_result, principled.inputs["Base Color"])
            # Solid/Texture viewport shading draws the ACTIVE image node and
            # cannot evaluate RBX Tint, so activate a pre-tinted atlas copy.
            try:
                viewport_image = _tinted_surface_band("diff", band, (r, g, b))
                viewport_node = _image_texture_node(
                    material, viewport_image or diff, "SurfaceDiffuseSolid"
                )
                viewport_node.name = "RBX Stud Diffuse Solid"
                viewport_node.label = "RBX tinted surface (Solid viewport)"
                viewport_node.extension = "REPEAT"
                _bind_named_uv(
                    material, viewport_node, "UVMap", "RBX Surface UV"
                )
                nodes.active = viewport_node
            except Exception:
                pass

        norm = _band_image("norm", band)
        if norm is not None:
            tex_node = _image_texture_node(material, norm, "SurfaceNormal")
            tex_node.name = "RBX Stud Normal"
            tex_node.label = "RBX Stud Normal (symmetrized — replace at own risk)"
            tex_node.extension = "REPEAT"
            tex_node.location = (-500, -100)
            _bind_named_uv(material, tex_node, "UVMap", "RBX Surface UV")
            normal_node = nodes.new("ShaderNodeNormalMap")
            normal_node.name = "RBX NormalMap"
            normal_node.label = "RBX NormalMap (strength here)"
            normal_node.location = (-250, -100)
            _link(material, tex_node.outputs["Color"], normal_node.inputs["Color"])
            _link(material, normal_node.outputs["Normal"], principled.inputs["Normal"])

    _PRIMITIVE_MAT_CACHE[cache_key] = material
    _organize_material_nodes(material)
    return material


def _node_frame_name(node) -> str:
    """The organizing frame for a node, per its RBX role prefix."""
    name = node.name
    # One frame per overlay layer tag: "RBX Overlay", "RBX Overlay.1", ...
    for prefix in (
        "RBX ColorMap", "RBX Overlay Mix", "RBX Overlay Tint",
        "RBX Overlay Alpha Scale", "RBX Overlay Alpha",
    ):
        if name.startswith(prefix):
            return "RBX Overlay" + name[len(prefix):]
    if name.startswith("RBX TextureInstance"):
        return "RBX Texture Instance" + name[len("RBX TextureInstance"):]
    if name.startswith("RBX Material") or name in ("RBX Tint", "RBX VertexColor"):
        return "RBX Material"
    if name.startswith("RBX NormalMap"):
        return "RBX Normal"
    if name.startswith(("RBX MetalRough", "RBX Surface metalness", "RBX Surface roughness")):
        return "RBX PBR Maps"
    if name.startswith(("RBX TextureID", "RBX TextureID Tint")):
        return "RBX TextureID"
    if name.startswith(
        ("RBX Instance UV", "RBX Material UV", "RBX Surface UV", "RBX Clothing UV")
    ):
        return "RBX UV"
    return None


def _organize_material_nodes(material) -> None:
    """Lay out a built graph: left->right columns, frames per role, hidden
    options on utility nodes (Node Tree Organisation Cookbook basics)."""
    try:
        tree = material.node_tree
        principled = _principled_node(material)
        if principled is None:
            return
        # Column depth: principled = 0, its sources walk left.
        depth = {principled: 0}
        queue = [principled]
        visited = {principled}
        while queue:
            node = queue.pop(0)
            for input_socket in node.inputs:
                for link in input_socket.links:
                    source = link.from_node
                    if source not in visited:
                        visited.add(source)
                        depth[source] = depth[node] + 1
                        queue.append(source)
        # Nodes not on any input chain (loose) get dumped in one column.
        for node in tree.nodes:
            if node not in visited and node.type not in ("OUTPUT_MATERIAL", "FRAME"):
                depth[node] = max(depth.values(), default=0) + 1
        columns = {}
        for node, level in depth.items():
            columns.setdefault(level, []).append(node)
        row_offset = 0
        for level in sorted(columns, reverse=True):
            column = sorted(columns[level], key=lambda n: n.name)
            for index, node in enumerate(column):
                node.location = (level * -280.0, row_offset - index * 210.0)
            row_offset -= max(220.0, 90.0 * len(column))
        # Output node one column right of the principled.
        for node in tree.nodes:
            if node.type == "OUTPUT_MATERIAL":
                node.location = (280.0, -50.0)
        # Small frames per role, positioned around their members.
        frames = {}
        for node in tree.nodes:
            if node.type in ("FRAME", "OUTPUT_MATERIAL", "BSDF_PRINCIPLED"):
                continue
            frame_name = _node_frame_name(node)
            if frame_name is None:
                continue
            if node.type in ("MATH", "VECTOR_MATH", "MAP_RANGE", "MIX", "MIX_RGB"):
                # Collapse the option UI on utility nodes; their labels
                # already describe the operation.
                try:
                    node.hide = True
                except Exception:
                    pass
            frame = frames.get(frame_name)
            if frame is None:
                try:
                    frame = tree.nodes.new("NodeFrame")
                except RuntimeError:
                    frame = tree.nodes.new("ShaderNodeFrame")
                frame.label = frame_name
                frames[frame_name] = frame
            try:
                node.parent = frame
            except (AttributeError, RuntimeError, TypeError):
                pass
        for frame in frames.values():
            try:
                _fit_frame_to_children(tree, frame)
            except Exception:
                pass
    except Exception:
        # Organization is cosmetic; a failure must never break the build.
        pass


def _fit_frame_to_children(tree, frame) -> None:
    """Place a frame around its parented nodes (frames auto-size to their
    children; only the position needs setting)."""
    frame_name = getattr(frame, "name", None)
    children = [
        node for node in tree.nodes
        if getattr(getattr(node, "parent", None), "name", None) == frame_name
        and node is not frame
    ]
    if not children:
        return
    min_x = min(node.location.x for node in children)
    max_y = max(node.location.y for node in children)
    frame.location = (min_x - 40.0, max_y + 20.0)


def _scoped_clothing_context(entry):
    """Set the clothing context for one part entry.

    rbxm imports stamp every entry with ITS OWN character's clothing
    (_rbxm_clothing_scoped); anything else keeps the file-level context the
    legacy import paths set up front.
    """
    if not entry.get("_rbxm_clothing_scoped"):
        return
    try:
        from . import clothing

        # NB: no clothing_available() gate here — that reads the CURRENT
        # context, which is exactly what we are about to replace.
        face = entry.get("face_decal") or {}
        clothing.set_clothing_context(
            shirt_template=entry.get("shirt_template"),
            pants_template=entry.get("pants_template"),
            face_texture=entry.get("face_texture") or face.get("texture"),
            body_colors=entry.get("body_colors"),
            face_transparency=(
                entry.get("face_transparency")
                if entry.get("face_transparency") is not None
                else face.get("transparency", 0.0)
            ),
        )
    except Exception:
        pass


def get_part_material(part_name: str, entry: dict):
    """Return the reusable one-slot material for a normal MeshPart."""
    _scoped_clothing_context(entry)
    cache_key = _part_material_cache_key(entry)
    material = _PART_MATERIAL_CACHE.get(cache_key) if cache_key is not None else None
    live = _live_material(material)
    if live is not None and _graph_current(live):
        return live
    # Stale or dead: rebuild.  A surviving datablock is rebuilt IN PLACE so
    # objects already referencing it pick up the current graph; otherwise a
    # fresh datablock is created (names are labels only).
    if live is not None:
        material = build_part_material(part_name, entry, material=live)
    else:
        material = build_part_material(part_name, entry)
    if cache_key is not None:
        _PART_MATERIAL_CACHE[cache_key] = material
    return material


# Roblox NormalId values -> surface names (matches _polygon_face_ids).
_NORMALID_FACE_NAMES = {
    0: "Right",
    1: "Top",
    2: "Back",
    3: "Left",
    4: "Bottom",
    5: "Front",
}


def _face_material_name(mesh_name: str, faces) -> str:
    """Material name for one or more face NormalIds (e.g. Wall_Front)."""
    names = []
    for face in faces:
        try:
            face_int = int(face)
        except (TypeError, ValueError):
            face_int = face
        names.append(_NORMALID_FACE_NAMES.get(face_int, f"Face{face_int}"))
    return f"{mesh_name}_{'_'.join(names)}"


def _polygon_face_ids(mesh, entry=None):
    """Per-polygon Roblox NormalId LISTS for child Texture assignment.

    Box shapes use the generator's fixed face order (immune to the t2b axis
    remap and to arbitrary part rotations); the curved ball classifies by
    polygon centre in part-local space.  Lists hold more than one id where
    the renderer draws that face's decal in several slots (GfxRender/
    GeometryGenerator.cpp decal tables): a wedge slope carries BOTH Top and
    Front, a corner-wedge slope carries Top plus Back or Left, and a
    cylinder side decal only covers its own quadrant.
    """
    shape = (entry or {}).get("shape", "block")
    polys = len(mesh.polygons)
    if shape in ("block", 1):
        # Generator order: -Z, +Z, +Y, -Y, +X, -X
        # -> Front, Back, Top, Bottom, Right, Left.
        table = ((5,), (2,), (1,), (4,), (0,), (3,))
        return [table[min(t // 2, 5)] for t in range(polys)]
    if shape == "wedge":
        # +Z rect=Back, +X tri=Right, -X tri=Left, bottom=Bottom,
        # slope carries Top AND Front (same decal quad in the renderer).
        table = ((2,), (0,), (3,), (4,), (1, 5))
        return [table[min(t // 2, 4)] for t in range(polys)]
    if shape == "corner_wedge":
        # +X tri=Right, slope A-D-C=Top+Back, slope A-E-D=Top+Left,
        # bottom=Bottom, -Z tri=Front.
        table = ((0,), (1, 2), (1, 3), (4,), (5,))
        return [table[min(t // 2, 4)] for t in range(polys)]
    if shape in ("cylinder", 2):
        # +X cap fans (Right), -X cap fans (Left), then 16 side quads
        # starting at +Y (Top) every 22.5 degrees around the circle.
        ids = []
        for t in range(polys):
            if t < 32:
                ids.append((0,) if t < 16 else (3,))
            else:
                a = 22.5 * (((t - 32) // 2) + 0.5)
                if a <= 45.0 or a >= 315.0:
                    ids.append((1,))
                elif a <= 135.0:
                    ids.append((2,))
                elif a <= 225.0:
                    ids.append((4,))
                else:
                    ids.append((5,))
        return ids
    # Ball: six patches by the dominant local axis of the polygon centre.
    try:
        from ..core.constants import get_transform_to_blender
        from ..core.utils import cf_to_mat
        from mathutils import Matrix, Vector

        part_cf = (entry or {}).get("part_cf")
        basis = get_transform_to_blender() @ (
            cf_to_mat(part_cf) if part_cf else Matrix.Identity(4)
        )
        inverse = basis.inverted_safe()
    except Exception:
        return [() for _ in range(polys)]
    ids = []
    for poly in mesh.polygons:
        try:
            local = inverse @ Vector(poly.center)
        except Exception:
            ids.append(())
            continue
        ax, ay, az = abs(local[0]), abs(local[1]), abs(local[2])
        if ax >= ay and ax >= az:
            ids.append((0,) if local[0] > 0 else (3,))
        elif ay >= ax and ay >= az:
            ids.append((1,) if local[1] > 0 else (4,))
        else:
            ids.append((2,) if local[2] > 0 else (5,))
    return ids


def apply_part_material(mesh_obj, entry: dict) -> bool:
    """Attach a built material to a synthesized mesh object."""
    if mesh_obj is None or getattr(mesh_obj, "type", None) != "MESH":
        return False
    mesh = mesh_obj.data

    texture_instances = _entry_texture_instances(entry)
    is_primitive = bool(entry.get("shape")) and not entry.get("mesh_id")
    if is_primitive and texture_instances:
        # Roblox draws each Texture child on ONE surface (NormalId).  The
        # flat graph applied every instance over the whole mesh, so a wall
        # texture tinted all six sides.  Split primitive meshes into one
        # material per surface: faces with instances composite them over
        # the face's base material; studded faces without an instance keep
        # their classic surface-band material.
        face_ids = _polygon_face_ids(mesh, entry)
        per_face = {}
        for instance in texture_instances:
            try:
                face = int(instance.get("face", 5))
            except (TypeError, ValueError):
                face = 5
            per_face.setdefault(face, []).append(instance)
        base_entry = dict(entry)
        base_entry.pop("texture_instances", None)
        base_entry.pop("texture_instance", None)
        has_studs = bool(entry.get("has_studs"))
        color = entry.get("color")
        transparency = entry.get("transparency", 0.0)
        face_st = None
        if has_studs:
            face_st = entry.get("_face_surface_types")
            if not face_st or len(face_st) != len(mesh.polygons):
                # Fallback: every face uses the dominant textured surface type.
                dominant = 0
                for cand in (3, 4, 1, 5):
                    if cand in (entry.get("surface_types") or []):
                        dominant = cand
                        break
                face_st = [dominant] * len(mesh.polygons)
            face_st = [int(st) for st in face_st]
        materials = {}
        for face, instances in per_face.items():
            face_entry = dict(entry)
            face_entry["texture_instances"] = list(instances)
            face_entry.pop("texture_instance", None)
            materials[face] = get_part_material(
                _face_material_name(mesh_obj.name, [face]), face_entry
            )
        base_material = get_part_material(mesh_obj.name, base_entry)
        band_materials = {}
        if has_studs:
            for st in set(face_st):
                if st not in _SURFACE_TYPE_TO_BAND:
                    continue
                band_materials[st] = _primitive_surface_material(
                    color, st, transparency=transparency
                )
        uv_layer = _ensure_builtin_material_uvs(mesh)
        if uv_layer is not None:
            try:
                mesh.uv_layers.active = uv_layer
                mesh.uv_layers.active_render = uv_layer
            except Exception:
                pass
        if builtin_material_texture_refs(entry):
            _ensure_builtin_material_color(mesh, entry.get("color") or (1.0, 1.0, 1.0))
        elif entry_uses_shared_builtin_tint(entry):
            _ensure_builtin_material_color(mesh, entry.get("color") or (1.0, 1.0, 1.0))
        slot_of = {}
        band_slot = {}
        mesh.materials.clear()
        slot_of[None] = len(mesh.materials)
        mesh.materials.append(base_material)
        for st in sorted(band_materials):
            band_slot[st] = len(mesh.materials)
            mesh.materials.append(band_materials[st])
        for face in sorted(materials):
            slot_of[face] = len(mesh.materials)
            mesh.materials.append(materials[face])
        for poly, ids, st in zip(
            mesh.polygons, face_ids, face_st or [None] * len(face_ids)
        ):
            matching = [face for face in ids if face in slot_of]
            if len(matching) == 1:
                poly.material_index = slot_of[matching[0]]
            elif len(matching) > 1:
                # One face carries several decal slots (wedge slope: Top +
                # Front).  Composite both instance lists into one material.
                key = tuple(sorted(matching))
                slot = slot_of.get(key)
                if slot is None:
                    merged = []
                    for face in key:
                        merged.extend(per_face[face])
                    face_entry = dict(entry)
                    face_entry["texture_instances"] = list(merged)
                    face_entry.pop("texture_instance", None)
                    slot_of[key] = len(mesh.materials)
                    mesh.materials.append(
                        get_part_material(
                            _face_material_name(mesh_obj.name, key),
                            face_entry,
                        )
                    )
                    slot = slot_of[key]
                poly.material_index = slot
            elif st in band_slot:
                poly.material_index = band_slot[st]
            else:
                poly.material_index = slot_of[None]
        return True

    # Primitive Parts with classic surface textures (Studs/Inlet/Glue/
    # Universal) get one shared material per surface type, assigned per face.
    if entry.get("shape") and not entry.get("mesh_id") and entry.get("has_studs"):
        face_st = entry.get("_face_surface_types")
        if not face_st or len(face_st) != len(mesh.polygons):
            # Fallback: every face uses the dominant textured surface type.
            dominant = 0
            for cand in (3, 4, 1, 5):
                if cand in (entry.get("surface_types") or []):
                    dominant = cand
                    break
            face_st = [dominant] * len(mesh.polygons)
        color = entry.get("color")
        present = []
        for st in face_st:
            st = int(st)
            if st not in present:
                present.append(st)
        transparency = entry.get("transparency", 0.0)
        mats = {}
        for st in present:
            if int(st) in _SURFACE_TYPE_TO_BAND:
                mats[st] = _primitive_surface_material(color, st, transparency=transparency)
            else:
                # Smooth/Weld faces: the full material build (built-in
                # materials, TextureID, clothing) instead of the flat
                # surface-band material. Classic Parts with a material
                # (brick, concrete, wood, ...) otherwise import as bare
                # Color3 on every non-studded face.
                mats[st] = get_part_material(mesh_obj.name, entry)
        if builtin_material_texture_refs(entry):
            _ensure_builtin_material_uvs(mesh)
            _ensure_builtin_material_color(mesh, color or (1.0, 1.0, 1.0))
        elif entry_uses_shared_builtin_tint(entry):
            _ensure_builtin_material_color(mesh, color or (1.0, 1.0, 1.0))
        index_of = {st: i for i, st in enumerate(present)}
        mesh.materials.clear()
        for st in present:
            mesh.materials.append(mats[st])
        for poly, st in zip(mesh.polygons, face_st):
            poly.material_index = index_of[int(st)]
        return True

    # Union (CSG) meshes with genuinely multi-color vertex data: split faces
    # into per-color material groups so EVERY viewport mode (Solid included)
    # shows the right color per face, not one flat dominant color.  A
    # SurfaceAppearance colour map overrides the baked vertex colours and
    # goes through the normal material path below instead.
    union_mesh = entry.get("union_mesh")
    if (
        isinstance(union_mesh, dict)
        and not any(
            (sa or {}).get("color_map") for sa in _entry_surface_appearances(entry)
        )
        and _apply_union_color_materials(mesh_obj, mesh, entry, union_mesh)
    ):
        return True

    material = get_part_material(mesh_obj.name, entry)
    if builtin_material_texture_refs(entry):
        uv_layer = _ensure_builtin_material_uvs(mesh)
        if uv_layer is not None:
            try:
                mesh.uv_layers.active = uv_layer
                mesh.uv_layers.active_render = uv_layer
            except Exception:
                pass
        if not entry.get("_mesh_vertex_colors"):
            _ensure_builtin_material_color(mesh, entry.get("color") or (1.0, 1.0, 1.0))
    elif (
        (entry.get("texture_id") and entry.get("texture_studs_per_tile"))
        or (
            any(
                ti.get("texture") for ti in _entry_texture_instances(entry)
            )
            and not entry.get("texture_id")
        )
    ):
        # Classic Texture child: tiled planar projection at the texture's
        # stud density (or the shared material UV layer when it has none).
        uv_layer = _ensure_builtin_material_uvs(mesh)
        if uv_layer is not None:
            try:
                mesh.uv_layers.active = uv_layer
                mesh.uv_layers.active_render = uv_layer
            except Exception:
                pass
    elif entry_uses_shared_builtin_tint(entry) and not entry.get("_mesh_vertex_colors"):
        # Built-ins without maps still tint through the attribute.
        _ensure_builtin_material_color(mesh, entry.get("color") or (1.0, 1.0, 1.0))
    if len(mesh.materials):
        mesh.materials[0] = material
    else:
        mesh.materials.append(material)
    return True


_UNION_MAT_CACHE: dict = {}

_BAKED_BUILTIN_TINT_CACHE: dict = {}


def object_tinted_builtin_material(entry: dict):
    """Return the lazy-instance variant of a shared built-in material.

    FileMesh instances link one mesh datablock, so their old RBXColor layer
    cannot carry a different tint per object. Baking a material per colour
    fixed that visual bug, but turned a large place into hundreds of almost
    identical node trees. Object Info's Color is evaluated per object, so
    this graph keeps linked meshes editable without that churn.
    """
    cache_key = _part_material_cache_key(entry)
    if cache_key is None:
        return get_part_material(str(entry.get("name") or "Part"), entry)
    material = _OBJECT_TINT_BUILTIN_CACHE.get(cache_key)
    live = _live_material(material)
    if live is not None and _graph_current(live):
        return live
    if live is not None:
        material = build_part_material(
            str(entry.get("name") or "Part"), entry, material=live
        )
    else:
        material = build_part_material(
            str(entry.get("name") or "Part"),
            entry,
            material_name=f"rbx_object_tint_{entry.get('material')}",
        )
    nodes = material.node_tree.nodes
    attr_node = nodes.get("RBX Material ColorAttr")
    if attr_node is not None:
        object_info = nodes.new("ShaderNodeObjectInfo")
        object_info.name = "RBX Material Object Tint"
        object_info.label = "RBX part colour (object tint)"
        object_info.location = attr_node.location
        for link in list(attr_node.outputs["Color"].links):
            to_socket = link.to_socket
            material.node_tree.links.remove(link)
            material.node_tree.links.new(object_info.outputs["Color"], to_socket)
        nodes.remove(attr_node)
    material["RBXObjectTint"] = True
    _OBJECT_TINT_BUILTIN_CACHE[cache_key] = material
    return material


def set_object_rbx_tint(mesh_obj, entry: dict) -> None:
    """Store Roblox Color3 on an object for ``object_tinted_builtin_material``.

    Color sockets and ``Object.color`` use the same scene-linear RNA values,
    so assigning the original Color3 here matches the prior RGB-node path.
    Applying another gamma conversion would darken every instance.
    """
    try:
        color = entry.get("color") or (1.0, 1.0, 1.0)
        mesh_obj.color = (
            max(0.0, min(1.0, float(color[0]))),
            max(0.0, min(1.0, float(color[1]))),
            max(0.0, min(1.0, float(color[2]))),
            1.0,
        )
    except (AttributeError, TypeError, ValueError, IndexError):
        pass


def baked_builtin_tint_material(entry: dict):
    """One material per (built-in, colour) for differently tinted instances.

    The shared built-in material reads the RBXColor attribute on the mesh,
    which a SHARED mesh datablock cannot split per instance.  Instances of
    one filemesh with different colours get this baked copy instead: the
    attribute node is replaced by the constant colour and the existing
    sRGB→linear gamma keeps the same tone mapping.
    """
    try:
        color = tuple(
            round(float(component), 5) for component in (entry.get("color") or ())[:3]
        )
        transparency = round(float(entry.get("transparency", 0.0)), 5)
    except (TypeError, ValueError):
        color, transparency = (1.0, 1.0, 1.0), 0.0
    key = (
        _part_material_cache_key(entry) or "raw",
        color,
        transparency,
    )
    material = _BAKED_BUILTIN_TINT_CACHE.get(key)
    live = _live_material(material)
    if live is not None and _graph_current(live):
        return live
    if live is not None:
        # Rebuild in place so existing object slots keep the same datablock.
        material = build_part_material(
            str(entry.get("name") or "Part"), entry, material=live
        )
    else:
        material = build_part_material(
            str(entry.get("name") or "Part"),
            entry,
            material_name=f"rbx_tint_{entry.get('material')}",
        )
    nodes = material.node_tree.nodes
    attr_node = nodes.get("RBX Material ColorAttr")
    if attr_node is not None:
        try:
            # Swap the vertex-colour read for a colour-managed constant:
            # an RGB node with the same sRGB colour produces exactly the
            # linear value the vertex-colour node would have decoded.
            rgb_node = nodes.new("ShaderNodeRGB")
            rgb_node.outputs["Color"].default_value = (
                color[0], color[1], color[2], 1.0,
            )
            for link in list(attr_node.outputs["Color"].links):
                to_socket = link.to_socket
                material.node_tree.links.remove(link)
                material.node_tree.links.new(rgb_node.outputs["Color"], to_socket)
            material.node_tree.nodes.remove(attr_node)
            # Rename only after removing the vertex-colour node, or
            # Blender's unique-name rule suffixes it with .001.
            rgb_node.name = "RBX Material ColorAttr"
        except Exception:
            pass
    _BAKED_BUILTIN_TINT_CACHE[key] = material
    if not bool(material.get("RBXTextureHydrated", False)):
        # Built inside the deferred-image pass, so its texture nodes have no
        # image datablock yet.  Geometry can finish applying AFTER the
        # hydration loop marked this entry's signature as seen, so the loop
        # will never revisit it; keep the entry here for the end-of-import
        # sweep (hydrate_pending_baked_materials).
        try:
            material["RBXDeferredEntryJson"] = json.dumps(entry, default=str)
        except (TypeError, ValueError):
            pass
    return material


def hydrate_pending_baked_materials() -> int:
    """Hydrate deferred baked tint materials whose bytes are now ready.

    Returns the number hydrated.  The hydration loop only re-queues
    signatures it has never seen, so baked materials created after their
    signature was already processed would otherwise keep their image-less
    (black) texture nodes for the rest of the session.
    """
    refreshed = 0
    for material in list(_BAKED_BUILTIN_TINT_CACHE.values()):
        if _live_material(material) is None:
            continue
        if bool(material.get("RBXTextureHydrated", False)):
            continue
        raw = material.get("RBXDeferredEntryJson")
        if not raw:
            continue
        try:
            entry = json.loads(raw)
            if not entry_texture_bytes_ready(entry):
                continue
            missing = hydrate_material_images(material, entry)
            material["RBXTextureHydrated"] = not missing
            refreshed += 1
        except (TypeError, ValueError, ReferenceError, RuntimeError) as exc:
            print(f"[RbxTexture] baked material hydrate failed: {exc}")
    return refreshed


def _hydrate_node_image(node, image, non_color: bool = False, ref: str = "") -> None:
    """Assign a fetched image into an existing texture node, resolving the
    role view so shared payloads never cross colours."""
    if node is None:
        return
    try:
        if image is not None:
            node.image = _data_image_view(image) if non_color else _color_image_view(image)
        elif ref:
            # A poison-pilled ref returns None; without this line the node
            # silently keeps its image-less (black) datablock forever.
            print(
                f"[RbxTexture] hydration miss: node '{node.name}' "
                f"ref '{ref}' returned no image"
            )
    except Exception:
        pass


def hydrate_material_images(material, entry: dict) -> None:
    """Fill image datablocks into a deferred graph whose nodes already exist.

    The deferred pass builds every node and link, skipping only the image
    datablock itself; this assigns them without re-creating the graph.
    """
    nodes = material.node_tree.nodes
    part_name = str(entry.get("name") or material.name)
    surface_list = _entry_surface_appearances(entry)
    color_map_ref = ""
    metalness_ref = ""
    roughness_ref = ""
    normal_ref = ""
    for surface in surface_list:
        if surface.get("color_map"):
            color_map_ref = surface["color_map"]
        if surface.get("metalness_map"):
            metalness_ref = surface["metalness_map"]
        if surface.get("roughness_map"):
            roughness_ref = surface["roughness_map"]
        if surface.get("normal_map"):
            normal_ref = surface["normal_map"]
    # Colour layers: the registry stamped by the build lists every
    # SurfaceAppearance colour map and Texture instance node in composite
    # order, so deferred graphs hydrate ALL layers — not just the first.
    for layer in _overlay_layer_registry(material):
        node = nodes.get(layer.get("node") or "")
        _hydrate_node_image(
            node,
            fetch_texture_image(layer["ref"], name=f"{part_name}_layer"),
            ref=str(layer["ref"]),
        )
    packed = bool(metalness_ref and metalness_ref == roughness_ref)
    if packed and metalness_ref:
        _hydrate_node_image(
            nodes.get("RBX MetalRough Map"),
            fetch_texture_image(
                metalness_ref, name=f"{part_name}_metalness_map", non_color=True
            ),
            non_color=True,
            ref=str(metalness_ref),
        )
    else:
        selected_refs = {
            "metalness_map": metalness_ref,
            "roughness_map": roughness_ref,
        }
        for surface_key in ("metalness_map", "roughness_map"):
            map_ref = selected_refs[surface_key]
            if not map_ref:
                continue
            _hydrate_node_image(
                nodes.get(f"RBX Surface {surface_key}"),
                fetch_texture_image(
                    map_ref, name=f"{part_name}_{surface_key}", non_color=True
                ),
                non_color=True,
                ref=str(map_ref),
            )
    if normal_ref:
        _hydrate_node_image(
            nodes.get("RBX NormalMapTex"),
            fetch_texture_image(normal_ref, name=f"{part_name}_normal", non_color=True),
            non_color=True,
            ref=str(normal_ref),
        )
    texture_id_ref = entry.get("texture_id")
    is_head = (part_name or "").strip().lower() == "head"
    is_classic_part_head = is_head and entry.get("class_name") == "Part"
    head_face_baked = False
    if is_classic_part_head:
        try:
            from . import clothing

            head_face_baked = bool(clothing._CLOTHING_CONTEXT.get("face_texture"))
        except Exception:
            head_face_baked = False
    if texture_id_ref and not color_map_ref and (
        not is_head or (is_classic_part_head and not head_face_baked)
    ):
        _hydrate_node_image(
            nodes.get("RBX TextureID"),
            fetch_texture_image(texture_id_ref, name=f"{part_name}_texid"),
            ref=str(texture_id_ref),
        )
    builtin = _builtin_material_for_entry(entry)
    if builtin is not None and not color_map_ref and not texture_id_ref:
        color_map, normal_map, metalness_map, roughness_map = builtin["maps"]
        variant_data = entry.get("material_variant_data")
        if variant_data:
            color_map = variant_data.get("color_map") or ""
            normal_map = variant_data.get("normal_map") or ""
            metalness_map = variant_data.get("metalness_map") or ""
            roughness_map = variant_data.get("roughness_map") or ""
        for map_ref, node_name, non_color in (
            (_builtin_map_ref(color_map), "RBX Material ColorMap", bool(variant_data)),
            (_builtin_map_ref(normal_map), "RBX Material Normal", True),
            (_builtin_map_ref(metalness_map), "RBX Material Metallic", True),
            (_builtin_map_ref(roughness_map), "RBX Material Roughness", True),
        ):
            if not map_ref:
                continue
            _hydrate_node_image(
                nodes.get(node_name),
                fetch_texture_image(
                    map_ref, name=f"{part_name}_{node_name}", non_color=non_color
                ),
                non_color=non_color,
                ref=str(map_ref),
            )
    # The overlay factor samples the texture node's OWN alpha output; no
    # copy datablock.  (A copy node was the previous suspect for factors
    # reading 1 despite the fade: sample the real pixels directly.)
    for layer in _overlay_layer_registry(material):
        if layer.get("mode") != "overlay":
            continue
        tag = layer.get("tag") or ""
        main = nodes.get(layer.get("node") or "")
        overlay_mix = nodes.get(f"RBX Overlay Mix{tag}")
        if overlay_mix is None:
            # Texture instances composite through a Mix Shader.
            overlay_mix = nodes.get(f"RBX Instance Mix Shader{tag}")
        if overlay_mix is None or main is None:
            continue
        main_image = getattr(main, "image", None)
        if main_image is None:
            continue
        # Direct alpha wiring at build time; nothing to create or rewire
        # during hydration.
    _apply_missing_color_map_fallback(material, entry)
    missing = [
        node.name
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE" and getattr(node, "image", None) is None
    ]
    # Stamp completeness here so every caller (direct and deferred) shares
    # one verdict; incomplete materials stay falsy and remain sweepable.
    material["RBXTextureHydrated"] = not missing
    if missing:
        print(
            f"[RbxTexture] hydration incomplete for '{material.name}': "
            + ", ".join(missing[:6])
            + (f" (+{len(missing) - 6} more)" if len(missing) > 6 else "")
        )
    return len(missing)


def _apply_missing_color_map_fallback(material, entry) -> None:
    """Degrade to the flat part colour when a colour map never arrived.

    An image-less texture node renders BLACK in Eevee/material preview, which
    reads as a broken part rather than a missing download.  If the colour
    texture still has no image after hydration, rewire the graph so the part
    colour (the Vertex Color/RGB tint or the Base Color default) shows
    instead.
    """
    nodes = material.node_tree.nodes
    principled = _principled_node(material)
    if principled is None:
        return
    registry = _overlay_layer_registry(material)
    if registry:
        missing_color_layer = False
        for layer in registry:
            node = nodes.get(layer.get("node") or "")
            if node is None or getattr(node, "image", None) is not None:
                continue
            if layer.get("mode") == "transparency":
                # Missing transparency colour map: cut its chain and show
                # the flat part colour instead of black.
                missing_color_layer = True
                for link in list(node.outputs["Color"].links):
                    material.node_tree.links.remove(link)
                continue
            # Overlay decal without its image: remove the whole layer
            # cluster (texture, alpha copy, fade, tint) and pull the mix
            # factor to zero so Result = A, revealing the layer underneath.
            # Leaving the nodes in place would strand dead "RBX Texture
            # instance" entries in the shader graph.
            tag = layer.get("tag") or ""
            mix = nodes.get(f"RBX Overlay Mix{tag}")
            if mix is None:
                mix = nodes.get(f"RBX Instance Mix Shader{tag}")
            if mix is not None:
                try:
                    factor_socket = _float_factor_socket(mix)
                    for link in list(factor_socket.links):
                        material.node_tree.links.remove(link)
                    factor_socket.default_value = 0.0
                except (AttributeError, TypeError):
                    pass
            for dead_name in (
                layer.get("node") or "",
                f"RBX Overlay Alpha{tag}",
                f"RBX Overlay Alpha Scale{tag}",
                f"RBX Overlay Tint{tag}",
                f"RBX Instance BSDF{tag}",
            ):
                dead = nodes.get(dead_name)
                if dead is not None:
                    try:
                        material.node_tree.nodes.remove(dead)
                    except (RuntimeError, ReferenceError):
                        pass
            # Instance layers feed Base Color AND Alpha directly.  Removing
            # their nodes leaves both unlinked: restore the flat part colour
            # and opaque alpha so the part renders instead of vanishing.
            if layer.get("mode") == "overlay" and mix is None:
                try:
                    if not principled.inputs["Base Color"].links:
                        color = entry.get("color") or (1.0, 1.0, 1.0)
                        principled.inputs["Base Color"].default_value = (
                            max(0.0, min(1.0, float(color[0]))),
                            max(0.0, min(1.0, float(color[1]))),
                            max(0.0, min(1.0, float(color[2]))),
                            1.0,
                        )
                except (TypeError, ValueError, IndexError):
                    pass
                try:
                    if not principled.inputs["Alpha"].links:
                        principled.inputs["Alpha"].default_value = 1.0
                except (AttributeError, KeyError):
                    pass
        if missing_color_layer:
            color = entry.get("color")
            if color:
                try:
                    principled.inputs["Base Color"].default_value = (
                        max(0.0, min(1.0, float(color[0]))),
                        max(0.0, min(1.0, float(color[1]))),
                        max(0.0, min(1.0, float(color[2]))),
                        1.0,
                    )
                except (TypeError, ValueError, IndexError):
                    pass
    tex = nodes.get("RBX Material ColorMap")
    if (
        tex is not None
        and getattr(tex, "image", None) is None
        and tex.outputs["Color"].links
    ):
        # Built-in tint chain: texture x tint -> Base Color.  Replace the
        # dead texture with white so the multiply keeps the tint (Vertex
        # Color or baked RGB) instead of blacking it out.
        white = nodes.new("ShaderNodeRGB")
        white.outputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        white.name = "RBX Material ColorMap Fallback"
        white.label = "RBX missing colour map fallback (white)"
        for link in list(tex.outputs["Color"].links):
            to_socket = link.to_socket
            material.node_tree.links.remove(link)
            material.node_tree.links.new(white.outputs["Color"], to_socket)


def part_material_is_built(entry: dict) -> bool:
    """True when the entry's cached material datablock already exists."""
    return bool(_cached_materials_for_entry(entry))


def _cached_materials_for_entry(entry: dict):
    """Every datablock this entry's import path may have assigned.

    Shared built-in materials live in the part cache; differently coloured
    instances of one filemesh get a baked per-colour copy on top of (or
    instead of) the shared material.  The hydration loop must refresh ALL of
    them — an image-less texture node renders black, so a hydrated shared
    material still leaves its baked siblings untextured unless they are
    visited too.
    """
    found = []
    cache_key = _part_material_cache_key(entry)
    material = _PART_MATERIAL_CACHE.get(cache_key) if cache_key is not None else None
    if _live_material(material) is not None and not _graph_current(material):
        # Rebuild in place so the hydration pass acts on the current graph,
        # never on a datablock left over from an older code version.
        try:
            material = build_part_material(material.name, entry, material=material)
        except (ReferenceError, RuntimeError, ValueError):
            material = None
    if _live_material(material) is not None:
        found.append(material)
    object_tint = None
    if entry_uses_baked_tint(entry):
        object_tint = _live_material(_OBJECT_TINT_BUILTIN_CACHE.get(cache_key))
        if object_tint is not None and not _graph_current(object_tint):
            try:
                object_tint = object_tinted_builtin_material(entry)
            except (ReferenceError, RuntimeError, ValueError):
                object_tint = None
    if object_tint is not None and all(object_tint is not other for other in found):
        found.append(object_tint)
    try:
        color = tuple(
            round(float(component), 5) for component in (entry.get("color") or ())[:3]
        )
        transparency = round(float(entry.get("transparency", 0.0)), 5)
    except (TypeError, ValueError):
        color, transparency = (1.0, 1.0, 1.0), 0.0
    key = (
        _part_material_cache_key(entry) or "raw",
        color,
        transparency,
    )
    # Only entries that actually TAKE the baked path may reach its cache: an
    # SA/TextureID entry with the same (material, colour) tuple must never
    # hydrate a tint wall's baked datablock with its own refs (that both
    # floods it with the wrong textures and lets the missing-map fallback
    # white-swap its colour map before the real hydration runs).
    baked = None
    if entry_uses_baked_tint(entry):
        baked = _live_material(_BAKED_BUILTIN_TINT_CACHE.get(key))
        if baked is not None and not _graph_current(baked):
            try:
                baked = baked_builtin_tint_material(entry)
            except (ReferenceError, RuntimeError, ValueError):
                baked = None
    if baked is not None and all(baked is not other for other in found):
        found.append(baked)
    # Primitive parts split into one material per face when child Texture
    # instances are present (apply_part_material).  Each variant has its own
    # cache entry, and place imports build them during the deferred pass —
    # before texture bytes exist — so the base signature's refresh must
    # hydrate these too or their image-less nodes stay black forever.
    instances = _entry_texture_instances(entry)
    if instances and entry.get("shape") and not entry.get("mesh_id"):
        # apply_part_material also builds an instance-free BASE material whose
        # cache key excludes the instances; resolve it alongside the face
        # variants so the shared material hydrates too.
        base_entry = dict(entry)
        base_entry.pop("texture_instances", None)
        base_entry.pop("texture_instance", None)
        base_key = _part_material_cache_key(base_entry)
        base_material = (
            _PART_MATERIAL_CACHE.get(base_key) if base_key is not None else None
        )
        if _live_material(base_material) is not None and not _graph_current(base_material):
            try:
                base_material = build_part_material(
                    base_material.name, base_entry, material=base_material
                )
            except (ReferenceError, RuntimeError, ValueError):
                base_material = None
        if _live_material(base_material) is not None and all(
            base_material is not other for other in found
        ):
            found.append(base_material)
        per_face = {}
        for instance in instances:
            try:
                face = int(instance.get("face", 5))
            except (TypeError, ValueError):
                face = 5
            per_face.setdefault(face, []).append(instance)
        for face_instances in per_face.values():
            face_entry = dict(entry)
            face_entry["texture_instances"] = list(face_instances)
            face_entry.pop("texture_instance", None)
            face_key = _part_material_cache_key(face_entry)
            face_material = (
                _PART_MATERIAL_CACHE.get(face_key) if face_key is not None else None
            )
            if _live_material(face_material) is not None and not _graph_current(face_material):
                try:
                    face_material = build_part_material(
                        face_material.name, face_entry, material=face_material
                    )
                except (ReferenceError, RuntimeError, ValueError):
                    face_material = None
            if _live_material(face_material) is not None and all(
                face_material is not other for other in found
            ):
                found.append(face_material)
    return found


def _image_less_node_names(material) -> list:
    """Names of TEX_IMAGE nodes whose datablock never landed."""
    try:
        return [
            node.name
            for node in material.node_tree.nodes
            if node.type == "TEX_IMAGE" and getattr(node, "image", None) is None
        ]
    except (ReferenceError, RuntimeError, AttributeError):
        return []


def refresh_cached_part_material(entry: dict) -> bool:
    """Finish one deferred material after texture bytes are ready."""
    materials = _cached_materials_for_entry(entry)
    if not materials:
        print(
            "[RbxTexture] refresh found no cached materials for "
            f"'{entry.get('name')}'"
        )
        return False
    try:
        entry_json = json.dumps(entry, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        entry_json = None
    for material in materials:
        # Verify NODES, not the flag: a flag stamped True by an earlier
        # build hides materials whose fetch silently returned nothing.
        if not _image_less_node_names(material):
            material["RBXTextureHydrated"] = True
            continue
        try:
            # Stamped so the finalize-time rehydration sweep can revisit this
            # material after the retry pass recovers any failed bytes.
            if entry_json is not None:
                material["RBXDeferredEntryJson"] = entry_json
            try:
                from . import clothing

                is_limb = clothing.is_clothing_limb(str(entry.get("name") or ""))
            except Exception:
                is_limb = False
            if is_limb:
                # Clothing crops are baked from globally-prefetched bytes that
                # the deferred pass skips entirely; a full rebuild is the only
                # correct path for limbs.
                build_part_material(
                    material.name, entry, material=material
                )
                missing = _image_less_node_names(material)
            else:
                # The deferred pass built every node and link; only the image
                # datablocks are missing.  Assign them without recreating the
                # graph (the old clear+rebuild cost ~7 ms per material).
                missing = hydrate_material_images(material, entry)
            material["RBXTextureHydrated"] = not missing
            if missing:
                print(
                    f"[RbxTexture] material '{material.name}' still missing "
                    f"{missing} texture(s) after refresh"
                )
        except (ReferenceError, RuntimeError, ValueError) as exc:
            print(f"[RbxTexture] deferred material refresh failed: {exc}")
    # The verdict is node-based: success means nothing image-less remains,
    # regardless of what any flag said before this call.
    return all(not _image_less_node_names(material) for material in materials)


def refresh_all_unhydrated_materials() -> int:
    """Finalize-time sweep: re-run hydration for every deferred material
    that still has image-less nodes, after the retry pass recovered any
    bytes it could.  Returns the number of materials completed by the sweep."""
    recovered = 0
    unsweepable = 0
    for material in list(bpy.data.materials):
        try:
            if not _image_less_node_names(material):
                continue
            raw = material.get("RBXDeferredEntryJson")
            if not raw:
                unsweepable += 1
                continue
            entry = json.loads(raw)
            missing = hydrate_material_images(material, entry)
            material["RBXTextureHydrated"] = not missing
            if not missing:
                recovered += 1
        except (TypeError, ValueError, ReferenceError, RuntimeError) as exc:
            print(
                "[RbxTexture] rehydration sweep failed for "
                f"'{getattr(material, 'name', '?')}': {exc}"
            )
    if unsweepable:
        print(
            "[RbxTexture] rehydration sweep: "
            f"{unsweepable} material(s) have image-less nodes but no "
            "deferred entry to rehydrate from (beams, decals, etc.)"
        )
    if recovered:
        print(f"[RbxTexture] rehydration sweep completed {recovered} material(s)")
    return recovered


def audit_unhydrated_materials(limit: int = 12) -> int:
    """Report every material whose graph still references image-less
    texture nodes after hydration.  Each hit is a texture that never landed
    — the silent black-material failure made visible.

    Returns the total number of offenders and prints the first ``limit``.
    """
    offenders = []
    for material in bpy.data.materials:
        if not getattr(material, "use_nodes", False):
            continue
        try:
            missing = [
                node.name
                for node in material.node_tree.nodes
                if node.type == "TEX_IMAGE" and getattr(node, "image", None) is None
            ]
        except (ReferenceError, RuntimeError, AttributeError):
            continue
        if missing:
            offenders.append((material.name, missing))
    if offenders:
        print(
            "[RbxTexture] hydration audit: "
            f"{len(offenders)} material(s) still have image-less texture nodes"
        )
        for name, missing in offenders[:limit]:
            print(
                f"[RbxTexture]   '{name}': "
                + ", ".join(missing[:6])
                + (f" (+{len(missing) - 6} more)" if len(missing) > 6 else "")
            )
    else:
        print("[RbxTexture] hydration audit: all materials have their texture images")
    return len(offenders)


def _union_color_material(part_name: str, rgba, transparency=0.0):
    """A flat-color material for one color group of a union mesh (cached)."""
    try:
        transparency = max(0.0, min(1.0, float(transparency)))
    except (TypeError, ValueError):
        transparency = 0.0
    key = (
        part_name,
        round(rgba[0], 3),
        round(rgba[1], 3),
        round(rgba[2], 3),
        round(transparency, 3),
    )
    material = _UNION_MAT_CACHE.get(key)
    if _live_material(material) is not None:
        return material
    material = _new_material(
        f"rbx_{part_name}_c{key[1]:.2f}_{key[2]:.2f}_{key[3]:.2f}_t{key[4]:.2f}"
    )
    _set_base_color(material, rgba)  # sets Principled + diffuse_color
    principled = _principled_node(material)
    if principled is not None:
        _apply_part_transparency(material, principled, {"transparency": transparency})
    _UNION_MAT_CACHE[key] = material
    return material


def _apply_union_color_materials(mesh_obj, mesh, entry, union_mesh) -> bool:
    """Split a multi-color union into one material per vertex color.

    Returns True when the split was applied, False to fall back to the single
    build_part_material path (single-color unions / no usable color data).
    """
    colors = union_mesh.get("colors")
    if not colors or len(mesh.polygons) == 0:
        return False

    # Quantize colors so float noise doesn't explode the material count.
    def _q(col):
        return (round(col[0], 2), round(col[1], 2), round(col[2], 2))

    distinct = {_q(c) for c in colors}
    if not distinct:
        return False  # no usable color data: part-color material path

    # Face color = the quantized color shared by its vertices (mesher output
    # has per-color shells, so all three verts of a face share a color; if
    # they straddle, take the first vertex's).
    face_color_idx = []
    present = []
    index_of = {}
    for poly in mesh.polygons:
        col = _q(colors[poly.vertices[0]]) if poly.vertices else (1.0, 1.0, 1.0)
        idx = index_of.get(col)
        if idx is None:
            idx = len(present)
            index_of[col] = idx
            present.append(col)
        face_color_idx.append(idx)

    part_name = entry.get("name") or mesh_obj.name
    mesh.materials.clear()
    for col in present:
        mesh.materials.append(
            _union_color_material(
                part_name,
                (col[0], col[1], col[2], 1.0),
                transparency=entry.get("transparency", 0.0),
            )
        )
    for poly, idx in zip(mesh.polygons, face_color_idx):
        poly.material_index = idx
    return True
