"""Classic clothing compositing for rbxm-imported avatars (bake-and-crop).

Implements Roblox's R15 clothing-compositing pipeline (ComposeTextureMap +
BindTextures). Rather than rewriting mesh UVs, each body limb's crop of the
shared 1024x568 composite atlas is baked into a standalone Blender image:

  1. the limb's BodyColor fills the crop rect,
  2. the pants and/or shirt template is projected through Roblox's
     compositing guide meshes (content/avatar/compositing/R15Composit*.mesh),
     whose vertex positions are crop-local atlas pixels and whose UVs address
     the shared clothing template.

The limb mesh keeps its native UVs, which already address its crop of the
atlas, so the baked crop image samples exactly like Roblox's runtime
composite.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .filemesh import (
    _fetch_url_bytes,
    _fetch_url_response,
    _extract_locations_from_payload,
    _get_auth_headers,
    _resolve_rbxasset_path,
    _uses_opencloud_auth,
    extract_asset_id,
    parse_filemesh,
)


_TEMPLATE_ID_CACHE: Dict[int, Optional[str]] = {}

# numpy ships inside Blender's Python; the pure-python paths below are kept
# for headless test runners that lack it.
_NP = None


def _numpy():
    global _NP
    if _NP is None:
        try:
            import numpy as _np
            _NP = _np
        except ImportError:
            _NP = False
    return _NP or None


def _srgb_decode(value: float) -> float:
    """sRGB EOTF (blender's transfer curve, extrapolated above 1)."""
    value = float(value)
    if value <= 0.04045:
        return value * (1.0 / 12.92)
    return ((value + 0.055) * (1.0 / 1.055)) ** 2.4


def _srgb_encode(value: float) -> float:
    """Inverse sRGB EOTF (linear -> encoded)."""
    value = float(value)
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * (max(value, 0.0) ** (1.0 / 2.4)) - 0.055


def _decode_rgb(buf):
    """Decode an RGBA buffer's rgb channels to linear (alpha untouched).

    Returns a NEW buffer: template arrays may be shared between limb
    bakes, so the caller's buffer is never mutated.
    """
    np = _numpy()
    if np is not None and isinstance(buf, np.ndarray):
        arr = np.asarray(buf, dtype=np.float32).reshape(-1, 4).copy()
        rgb = arr[:, :3]
        low = rgb <= 0.04045
        arr[:, :3] = np.where(
            low, rgb * (1.0 / 12.92), ((rgb + 0.055) * (1.0 / 1.055)) ** 2.4
        )
        return arr.reshape(-1)
    out = list(buf)
    for i in range(0, len(out), 4):
        for c in (i, i + 1, i + 2):
            v = out[c]
            out[c] = v * (1.0 / 12.92) if v <= 0.04045 else ((v + 0.055) * (1.0 / 1.055)) ** 2.4
    return out


def _encode_rgb(buf):
    """Encode an RGBA buffer's rgb channels back to sRGB (alpha untouched)."""
    np = _numpy()
    if np is not None and isinstance(buf, np.ndarray):
        arr = np.asarray(buf, dtype=np.float32).reshape(-1, 4).copy()
        rgb = arr[:, :3]
        low = rgb <= 0.0031308
        arr[:, :3] = np.where(
            low,
            rgb * 12.92,
            1.055 * np.maximum(rgb, 0.0) ** (1.0 / 2.4) - 0.055,
        )
        return arr.reshape(-1)
    out = []
    for i in range(0, len(buf), 4):
        for c in range(3):
            v = buf[i + c]
            out.append(v * 12.92 if v <= 0.0031308 else 1.055 * (max(v, 0.0) ** (1.0 / 2.4)) - 0.055)
        out.append(buf[i + 3])
    return out


def resolve_clothing_template_id(asset_id) -> Optional[str]:
    """Resolve a HumanoidDescription clothing asset id (Shirt/Pants) to its
    template content id by fetching the asset and reading its *Template
    property. Handles binary rbxm and XML assets; returns None on failure.
    Results are cached (including failures) per session."""
    try:
        asset_id = int(asset_id)
    except (TypeError, ValueError):
        return None
    if asset_id in _TEMPLATE_ID_CACHE:
        return _TEMPLATE_ID_CACHE[asset_id]

    result: Optional[str] = None
    auth = _get_auth_headers()
    urls = [
        f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{asset_id}",
        f"https://assetdelivery.roblox.com/v1/asset/?id={asset_id}",
    ]
    payload: Optional[bytes] = None
    for url in urls:
        try:
            headers = auth if _uses_opencloud_auth(url) else None
            response, data = _fetch_url_response(url, follow_redirects=False, extra_headers=headers)
            location = response.headers.get("Location")
            locations = [location] if location else _extract_locations_from_payload(data)
            if locations:
                # Clothing asset (rbxm/xml), not a mesh: the version-marker
                # trim in the mesh normalizer must not run on it.
                payload = _fetch_url_bytes(locations[0], trim_mesh_header=False)
            else:
                payload = data
            if payload:
                break
        except Exception as exc:
            print(f"[RbxClothing] template asset fetch '{url}' failed: {exc}")
            continue

    if payload:
        stripped = payload.lstrip()[:64]
        if stripped.startswith(b"<roblox!"):
            try:
                from ..core.rbxm import parse_rbxm  # noqa: PLC0415

                meta = parse_rbxm(payload)
                result = meta.get("shirt_template") or meta.get("pants_template")
            except Exception as exc:
                print(f"[RbxClothing] template asset rbxm parse failed: {exc}")
        elif stripped.startswith((b"<roblox", b"<?xml")):
            try:
                import re  # noqa: PLC0415

                text = payload.decode("utf-8", errors="replace")
                match = re.search(
                    r"<(?:Content|url)[^>]*name=\"(?:ShirtTemplate|PantsTemplate)\"[^>]*>\s*<?(?:url)?>?([^<\s]+)",
                    text,
                )
                if match is None:
                    match = re.search(
                        r"name=\"(?:ShirtTemplate|PantsTemplate)\"[^>]*>.*?(https?://[^<\s]+|rbxasset[^<\s]+)",
                        text,
                        re.DOTALL,
                    )
                if match:
                    result = match.group(1).strip()
            except Exception as exc:
                print(f"[RbxClothing] template asset xml parse failed: {exc}")

    if result:
        # Normalize bare ids/urls to an rbxassetid:// ref the texture fetcher
        # understands.
        numeric = extract_asset_id(result)
        if numeric is not None and "rbxassetid" not in str(result).lower():
            result = f"rbxassetid://{numeric}"
    _TEMPLATE_ID_CACHE[asset_id] = result
    return result


# Atlas layout (Roblox R15 compositing). Canvas is 1024x568; each limb owns
# a crop rect that its mesh UVs address in normalized space.
ATLAS_WIDTH = 1024
ATLAS_HEIGHT = 568

_TORSO_RECT = (0, 0, 388, 272)
_LEFT_ARM_RECT = (496, 0, 264, 284)
_LEFT_LEG_RECT = (496, 284, 264, 284)
_RIGHT_ARM_RECT = (760, 0, 264, 284)
_RIGHT_LEG_RECT = (760, 284, 264, 284)

_GUIDE_TORSO = "rbxasset://avatar/compositing/R15CompositTorsoBase.mesh"
_GUIDE_LEFT_ARM = "rbxasset://avatar/compositing/R15CompositLeftArmBase.mesh"
_GUIDE_RIGHT_ARM = "rbxasset://avatar/compositing/R15CompositRightArmBase.mesh"
# Roblox reuses the arm guides for the legs (COMPOSIT_*_LIMB).
_GUIDE_LEFT_LEG = _GUIDE_LEFT_ARM
_GUIDE_RIGHT_LEG = _GUIDE_RIGHT_ARM


def _limb_group(part_name: str) -> Optional[str]:
    name = (part_name or "").lower()
    if "torso" in name:
        return "torso"
    if name.startswith("left"):
        if "arm" in name or "hand" in name:
            return "left_arm"
        if "leg" in name or "foot" in name:
            return "left_leg"
    if name.startswith("right"):
        if "arm" in name or "hand" in name:
            return "right_arm"
        if "leg" in name or "foot" in name:
            return "right_leg"
    return None


_GROUP_GUIDE = {
    "torso": _GUIDE_TORSO,
    "left_arm": _GUIDE_LEFT_ARM,
    "right_arm": _GUIDE_RIGHT_ARM,
    "left_leg": _GUIDE_LEFT_LEG,
    "right_leg": _GUIDE_RIGHT_LEG,
}

_GROUP_RECT = {
    "torso": _TORSO_RECT,
    "left_arm": _LEFT_ARM_RECT,
    "right_arm": _RIGHT_ARM_RECT,
    "left_leg": _LEFT_LEG_RECT,
    "right_leg": _RIGHT_LEG_RECT,
}

# Bake order per limb group, bottom layer first (Roblox layer indices:
# pants=1 then shirt=2 on the torso; shirt=1 on arms; pants=1 on legs).
_GROUP_LAYERS = {
    "torso": ("pants_template", "shirt_template"),
    "left_arm": ("shirt_template",),
    "right_arm": ("shirt_template",),
    "left_leg": ("pants_template",),
    "right_leg": ("pants_template",),
}


# ---------------------------------------------------------------------------
# R6 (Roblox R6CharacterAssembler.ComposeTextureMap)
#
# Roblox composes onto a 1024x768 canvas and then binds only RECT_BODY
# (0, 256, 1024, 512) to the limb meshes — the classic mesh UVs are
# normalized to that 1024x512 crop, NOT the full canvas.  Rendering the crop
# directly is equivalent (the y-256 offset cancels), so the bake canvas is
# 1024x512 and guide rows map as ``512 - guide_y`` (the assembler's
# Vertex2D.ToPoint flip, crop-relative).  Each limb's body color is painted
# through its own composit guide, and the shirt/pants templates are projected
# through full-canvas guides that cover all limbs at once.  Pants is layer 1,
# shirt layer 2.  The head (RECT_HEAD + face decal) is handled by the
# existing face-decal material path, so only the five body parts bake here.
# ---------------------------------------------------------------------------

_R6_CANVAS_W = 1024
_R6_CANVAS_H = 512  # RECT_BODY height, not the full 768-tall canvas

_R6_GUIDE_TORSO = "rbxasset://avatar/compositing/CompositTorsoBase.mesh"
_R6_GUIDE_LEFT_ARM = "rbxasset://avatar/compositing/CompositLeftArmBase.mesh"
_R6_GUIDE_RIGHT_ARM = "rbxasset://avatar/compositing/CompositRightArmBase.mesh"
_R6_GUIDE_LEFT_LEG = "rbxasset://avatar/compositing/CompositLeftLegBase.mesh"
_R6_GUIDE_RIGHT_LEG = "rbxasset://avatar/compositing/CompositRightLegBase.mesh"
_R6_GUIDE_SHIRT = "rbxasset://avatar/compositing/CompositShirtTemplate.mesh"
_R6_GUIDE_PANTS = "rbxasset://avatar/compositing/CompositPantsTemplate.mesh"

# Exact R6 part names (lowercased) -> limb guide. R15 parts never match
# these exactly ("Torso" vs "UpperTorso"/"LowerTorso"), so an exact hit
# doubles as the R6/R15 discriminator.
_R6_LIMB_GUIDES = {
    "torso": _R6_GUIDE_TORSO,
    "left arm": _R6_GUIDE_LEFT_ARM,
    "right arm": _R6_GUIDE_RIGHT_ARM,
    "left leg": _R6_GUIDE_LEFT_LEG,
    "right leg": _R6_GUIDE_RIGHT_LEG,
}


def _r6_limb(part_name: str) -> Optional[str]:
    """Return the lowercase R6 limb name, or None if not an R6 body part."""
    name = (part_name or "").strip().lower()
    return name if name in _R6_LIMB_GUIDES else None


# ---------------------------------------------------------------------------
# Guide meshes
# ---------------------------------------------------------------------------


def _load_guide(resource: str) -> Optional[dict]:
    path = _resolve_rbxasset_path(resource)
    if path is None or not path.is_file():
        print(f"[RbxClothing] guide not found on disk: {resource} (resolved={path})")
        return None
    try:
        return parse_filemesh(path.read_bytes())
    except Exception as exc:
        print(f"[RbxClothing] guide parse failed for {path}: {exc!r}")
        return None


class _GuideTriangle:
    """One guide triangle with precomputed barycentric terms.

    Implements Roblox's Vertex2D.ToPoint(canvas, offset): guide mesh
    positions are pre-rounded, and the destination Y is inverted via
    ``rect.height - round(pos.y)`` (crop-local, since we bake per-crop
    rather than the full 1024x568 canvas). UVs address the clothing
    template in raw V convention (v=0 at the template's top row).
    """

    __slots__ = ("ax", "ay", "v0x", "v0y", "v1x", "v1y", "inv_denom",
                 "ua", "ub", "uc", "minx", "maxx", "miny", "maxy",
                 "_dot00", "_dot01", "_dot11")

    def __init__(self, pa, pb, pc, ua, ub, uc, rect_height):
        ax = int(pa[0] + 0.5) if pa[0] >= 0 else -int(-pa[0] + 0.5)
        ay = rect_height - (int(pa[1] + 0.5) if pa[1] >= 0 else -int(-pa[1] + 0.5))
        bx = int(pb[0] + 0.5) if pb[0] >= 0 else -int(-pb[0] + 0.5)
        by = rect_height - (int(pb[1] + 0.5) if pb[1] >= 0 else -int(-pb[1] + 0.5))
        cx = int(pc[0] + 0.5) if pc[0] >= 0 else -int(-pc[0] + 0.5)
        cy = rect_height - (int(pc[1] + 0.5) if pc[1] >= 0 else -int(-pc[1] + 0.5))
        pa = (ax, ay)
        pb = (bx, by)
        pc = (cx, cy)
        self.ax, self.ay = pa[0], pa[1]
        self.v0x, self.v0y = pc[0] - pa[0], pc[1] - pa[1]
        self.v1x, self.v1y = pb[0] - pa[0], pb[1] - pa[1]
        dot00 = self.v0x * self.v0x + self.v0y * self.v0y
        dot01 = self.v0x * self.v1x + self.v0y * self.v1y
        dot11 = self.v1x * self.v1x + self.v1y * self.v1y
        denom = dot00 * dot11 - dot01 * dot01
        self.inv_denom = (1.0 / denom) if abs(denom) > 1e-12 else 0.0
        self._dot00 = dot00
        self._dot01 = dot01
        self._dot11 = dot11
        self.ua, self.ub, self.uc = ua, ub, uc
        self.minx = min(pa[0], pb[0], pc[0])
        self.maxx = max(pa[0], pb[0], pc[0])
        self.miny = min(pa[1], pb[1], pc[1])
        self.maxy = max(pa[1], pb[1], pc[1])

    def template_uv(self, px: float, py: float) -> Optional[Tuple[float, float]]:
        v2x, v2y = px - self.ax, py - self.ay
        dot02 = self.v0x * v2x + self.v0y * v2y
        dot12 = self.v1x * v2x + self.v1y * v2y
        u = (self._dot11 * dot02 - self._dot01 * dot12) * self.inv_denom
        v = (self._dot00 * dot12 - self._dot01 * dot02) * self.inv_denom
        if u < -1e-6 or v < -1e-6 or (u + v) > 1.0 + 1e-6:
            return None
        w = 1.0 - u - v
        tu = w * self.ua[0] + v * self.ub[0] + u * self.uc[0]
        tv = w * self.ua[1] + v * self.ub[1] + u * self.uc[1]
        return (tu, tv)


class _GuideLookup:
    def __init__(self, guide: dict, rect_height: int):
        positions = guide.get("positions") or []
        uvs = guide.get("uvs") or []
        faces = guide.get("faces") or []
        self.triangles: List[_GuideTriangle] = []
        for face in faces:
            try:
                ia, ib, ic = int(face[0]), int(face[1]), int(face[2])
                pa, pb, pc = positions[ia], positions[ib], positions[ic]
            except Exception:
                continue
            ua = uvs[ia] if ia < len(uvs) and uvs[ia] else (0.0, 0.0)
            ub = uvs[ib] if ib < len(uvs) and uvs[ib] else (0.0, 0.0)
            uc = uvs[ic] if ic < len(uvs) and uvs[ic] else (0.0, 0.0)
            self.triangles.append(
                _GuideTriangle(pa, pb, pc, ua, ub, uc, rect_height)
            )


_GUIDE_CACHE: Dict[str, Optional[_GuideLookup]] = {}


def _get_guide(group: str) -> Optional[_GuideLookup]:
    if group in _GUIDE_CACHE:
        return _GUIDE_CACHE[group]
    lookup = None
    resource = _GROUP_GUIDE.get(group)
    rect = _GROUP_RECT.get(group)
    if resource and rect:
        guide = _load_guide(resource)
        if guide is not None:
            lookup = _GuideLookup(guide, rect[3])
    _GUIDE_CACHE[group] = lookup
    return lookup


def _get_guide_for_resource(resource: str, rect_height: int) -> Optional[_GuideLookup]:
    """Guide lookup keyed by resource path (used by the R6 pipeline)."""
    cache_key = f"{resource}@{rect_height}"
    if cache_key in _GUIDE_CACHE:
        return _GUIDE_CACHE[cache_key]
    lookup = None
    guide = _load_guide(resource)
    if guide is not None:
        lookup = _GuideLookup(guide, rect_height)
    _GUIDE_CACHE[cache_key] = lookup
    return lookup


# ---------------------------------------------------------------------------
# Clothing context
# ---------------------------------------------------------------------------

_CLOTHING_CONTEXT = {
    "shirt_template": None,
    "pants_template": None,
    "face_texture": None,
    "face_transparency": 0.0,
    "body_colors": {},
}

_BAKE_CACHE: Dict[str, object] = {}


def _context_cache_tag() -> tuple:
    """Content identity of the active clothing context.

    Bake caches key off this tag so two characters can NEVER share a baked
    limb image even when their bakes interleave (the woman wearing the
    man's clothes bug)."""
    body_colors = _CLOTHING_CONTEXT.get("body_colors") or {}
    color_items = tuple(
        sorted(
            (str(key), tuple(round(float(component), 5) for component in value)
             if isinstance(value, (list, tuple)) else str(value))
            for key, value in body_colors.items()
        )
    )
    return (
        _CLOTHING_CONTEXT.get("shirt_template"),
        _CLOTHING_CONTEXT.get("pants_template"),
        _CLOTHING_CONTEXT.get("face_texture"),
        _CLOTHING_CONTEXT.get("face_transparency"),
        color_items,
    )


def set_clothing_context(shirt_template=None, pants_template=None, face_texture=None, body_colors=None, face_transparency=None):
    """Prime the clothing pipeline for an import (called by the operator)."""
    _CLOTHING_CONTEXT["shirt_template"] = shirt_template
    _CLOTHING_CONTEXT["pants_template"] = pants_template
    _CLOTHING_CONTEXT["face_texture"] = face_texture
    _CLOTHING_CONTEXT["body_colors"] = dict(body_colors or {})
    try:
        _CLOTHING_CONTEXT["face_transparency"] = (
            max(0.0, min(1.0, float(face_transparency)))
            if face_transparency is not None else 0.0
        )
    except (TypeError, ValueError):
        _CLOTHING_CONTEXT["face_transparency"] = 0.0
    _BAKE_CACHE.clear()
    _R6_CLOTHING_CACHE.clear()


def clothing_available() -> bool:
    return bool(
        _CLOTHING_CONTEXT.get("shirt_template")
        or _CLOTHING_CONTEXT.get("pants_template")
        or _CLOTHING_CONTEXT.get("face_texture")
        or _CLOTHING_CONTEXT.get("body_colors")
    )


def _is_head(part_name: str) -> bool:
    return (part_name or "").strip().lower() == "head"


def is_clothing_limb(part_name: str) -> bool:
    return (
        _limb_group(part_name) is not None
        or _r6_limb(part_name) is not None
        or _is_head(part_name)
    )


def context_texture_refs(part_name: str, tint_ref=None) -> Tuple[str, ...]:
    """Texture dependencies needed to bake one body part."""
    if _is_head(part_name):
        refs = (_CLOTHING_CONTEXT.get("face_texture"), tint_ref)
    else:
        r6_limb = _r6_limb(part_name)
        group = _limb_group(part_name)
        if r6_limb is not None:
            # The R6 implementation builds one shared clothing canvas before
            # cropping limbs, so either template is a real dependency.
            keys = ("pants_template", "shirt_template")
        else:
            keys = _GROUP_LAYERS.get(group, ())
        refs = tuple(_CLOTHING_CONTEXT.get(key) for key in keys)
    return tuple(dict.fromkeys(ref for ref in refs if ref))


def context_signature(part_name: str, tint_ref=None) -> tuple:
    """Visual identity of the clothing bake used by a material cache key."""
    color = _hd_color_override(part_name)
    return (
        context_texture_refs(part_name, tint_ref=tint_ref),
        tuple(round(float(component), 6) for component in color[:3])
        if color is not None else (),
    )


# ---------------------------------------------------------------------------
# Bake
# ---------------------------------------------------------------------------


def _tri_pixel_pack(np, tri: _GuideTriangle, w: int, h: int):
    """Dest ids + template UVs for every pixel inside ``tri``'s bbox."""
    x0 = max(0, int(tri.minx))
    x1 = min(w - 1, int(tri.maxx + 1.0))
    y0 = max(0, int(tri.miny))
    y1 = min(h - 1, int(tri.maxy + 1.0))
    if x1 < x0 or y1 < y0:
        return None
    xs = np.arange(x0, x1 + 1, dtype=np.float64) + 0.5
    ys = np.arange(y0, y1 + 1, dtype=np.float64) + 0.5
    px, py = np.meshgrid(xs, ys)
    v2x = px - tri.ax
    v2y = py - tri.ay
    dot02 = tri.v0x * v2x + tri.v0y * v2y
    dot12 = tri.v1x * v2x + tri.v1y * v2y
    u = (tri._dot11 * dot02 - tri._dot01 * dot12) * tri.inv_denom
    v = (tri._dot00 * dot12 - tri._dot01 * dot02) * tri.inv_denom
    inside = (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1.0 + 1e-6)
    if not inside.any():
        return None
    wgt = 1.0 - u - v
    tu = wgt * tri.ua[0] + v * tri.ub[0] + u * tri.uc[0]
    tv = wgt * tri.ua[1] + v * tri.ub[1] + u * tri.uc[1]
    xi = np.arange(x0, x1 + 1, dtype=np.int64)
    yi = np.arange(y0, y1 + 1, dtype=np.int64)
    gx, gy = np.meshgrid(xi, yi)
    ids = ((h - 1 - gy) * w + gx).ravel()
    sel = inside.ravel()
    return ids[sel], tu.ravel()[sel], tv.ravel()[sel]


def _sample_bilinear_grid(np, tpl, tw: int, th: int, u, v, has_alpha: bool):
    """Vectorized twin of _sample_bilinear over 1-D uv arrays."""
    flat = np.ascontiguousarray(tpl, dtype=np.float32).reshape(-1, 4)
    u = np.clip(u, 0.0, 1.0)
    v = np.clip(v, 0.0, 1.0)
    fx = u * (tw - 1)
    fy = (1.0 - v) * (th - 1)
    x0 = np.floor(fx).astype(np.int64)
    y0 = np.floor(fy).astype(np.int64)
    x1 = np.minimum(x0 + 1, tw - 1)
    y1 = np.minimum(y0 + 1, th - 1)
    tx = (fx - x0)[:, None]
    ty = (fy - y0)[:, None]
    c00 = flat[y0 * tw + x0]
    c10 = flat[y0 * tw + x1]
    c01 = flat[y1 * tw + x0]
    c11 = flat[y1 * tw + x1]
    top = c00 * (1.0 - tx) + c10 * tx
    bot = c01 * (1.0 - tx) + c11 * tx
    out = top * (1.0 - ty) + bot * ty
    if not has_alpha:
        out = out.copy()
        out[:, 3] = 1.0
    return out


def _rasterize_layer_np(np, buf, w: int, h: int, lookup: _GuideLookup, tpl, tw: int, th: int, has_alpha: bool):
    """numpy version of _rasterize_layer."""
    dest = buf.reshape(-1, 4)
    flat = np.ascontiguousarray(tpl, dtype=np.float32).reshape(-1, 4)
    for tri in lookup.triangles:
        packed = _tri_pixel_pack(np, tri, w, h)
        if packed is None:
            continue
        ids, tu, tv = packed
        samples = _sample_bilinear_grid(np, flat, tw, th, tu, tv, has_alpha)
        sa = samples[:, 3]
        keep = sa > 0.0
        if not keep.any():
            continue
        ids = ids[keep]
        samples = samples[keep]
        sa = sa[keep]
        inv = 1.0 - sa
        dest[ids, :3] = samples[:, :3] * sa[:, None] + dest[ids, :3] * inv[:, None]
        dest[ids, 3] = 1.0


def _rasterize_layer_over_np(np, buf, w: int, h: int, lookup: _GuideLookup, tpl, tw: int, th: int, has_alpha: bool):
    """numpy version of _rasterize_layer_over."""
    dest = buf.reshape(-1, 4)
    flat = np.ascontiguousarray(tpl, dtype=np.float32).reshape(-1, 4)
    for tri in lookup.triangles:
        packed = _tri_pixel_pack(np, tri, w, h)
        if packed is None:
            continue
        ids, tu, tv = packed
        samples = _sample_bilinear_grid(np, flat, tw, th, tu, tv, has_alpha)
        sa = samples[:, 3]
        keep = sa > 0.0
        if not keep.any():
            continue
        ids = ids[keep]
        samples = samples[keep]
        sa = sa[keep]
        da = dest[ids, 3]
        out_a = sa + da * (1.0 - sa)
        keep2 = out_a > 1e-6
        if not keep2.any():
            continue
        ids = ids[keep2]
        samples = samples[keep2]
        sa = sa[keep2]
        da = da[keep2]
        out_a = out_a[keep2]
        inv = 1.0 - sa
        dest[ids, :3] = (
            samples[:, :3] * sa[:, None] + dest[ids, :3] * da[:, None] * inv[:, None]
        ) / out_a[:, None]
        dest[ids, 3] = out_a


def _fill_guide_np(np, buf, w: int, h: int, lookup: _GuideLookup, rgba):
    """numpy version of _fill_guide.  ``rgba`` is sRGB-encoded and is
    decoded to linear here so the bake composites in scene-linear space."""
    dest = buf.reshape(-1, 4)
    r = _srgb_decode(rgba[0])
    g = _srgb_decode(rgba[1])
    b = _srgb_decode(rgba[2])
    a = float(rgba[3])
    for tri in lookup.triangles:
        packed = _tri_pixel_pack(np, tri, w, h)
        if packed is None:
            continue
        dest[packed[0]] = (r, g, b, a)


def _sample_bilinear(tpl, tw: int, th: int, u: float, v: float, has_alpha: bool):
    """Sample a Blender image pixel buffer (row 0 = bottom).

    ``v`` arrives in the template's raw convention (v=0 at the TOP row,
    matching Roblox's ``iy = round(uv.y * height)`` into a top-down
    compositing bitmap), so it is flipped before addressing Blender's
    bottom-up buffer.
    """
    u = 0.0 if u < 0.0 else (1.0 if u > 1.0 else u)
    v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
    fx = u * (tw - 1)
    fy = (1.0 - v) * (th - 1)
    x0 = int(fx)
    y0 = int(fy)
    x1 = x0 + 1 if x0 + 1 < tw else tw - 1
    y1 = y0 + 1 if y0 + 1 < th else th - 1
    tx = fx - x0
    ty = fy - y0

    i00 = (y0 * tw + x0) * 4
    i10 = (y0 * tw + x1) * 4
    i01 = (y1 * tw + x0) * 4
    i11 = (y1 * tw + x1) * 4

    a00 = tpl[i00 + 3] if has_alpha else 1.0
    a10 = tpl[i10 + 3] if has_alpha else 1.0
    a01 = tpl[i01 + 3] if has_alpha else 1.0
    a11 = tpl[i11 + 3] if has_alpha else 1.0

    out = [0.0, 0.0, 0.0, 0.0]
    for c in range(3):
        top = tpl[i00 + c] * (1.0 - tx) + tpl[i10 + c] * tx
        bot = tpl[i01 + c] * (1.0 - tx) + tpl[i11 + c] * tx
        out[c] = top * (1.0 - ty) + bot * ty
    atop = a00 * (1.0 - tx) + a10 * tx
    abot = a01 * (1.0 - tx) + a11 * tx
    out[3] = atop * (1.0 - ty) + abot * ty
    return out


def _rasterize_layer(buf, w: int, h: int, lookup: _GuideLookup, tpl, tw: int, th: int, has_alpha: bool):
    """Alpha-over one clothing template onto the crop buffer via the guide.

    Barycentric math runs in the guide's top-down pixel space (Roblox
    BarycentricPoint over compositing-bitmap coordinates), then the destination
    row is flipped into Blender's bottom-up buffer so the baked image is
    stored display-convention (right-side-up), matching the loop-UV flip
    applied at mesh build time.
    """
    np = _numpy()
    if np is not None and isinstance(buf, np.ndarray):
        _rasterize_layer_np(np, buf, w, h, lookup, tpl, tw, th, has_alpha)
        return
    for tri in lookup.triangles:
        x0 = max(0, int(tri.minx))
        x1 = min(w - 1, int(tri.maxx + 1.0))
        y0 = max(0, int(tri.miny))
        y1 = min(h - 1, int(tri.maxy + 1.0))
        if x1 < x0 or y1 < y0:
            continue
        for y in range(y0, y1 + 1):
            py = y + 0.5
            row = (h - 1 - y) * w
            for x in range(x0, x1 + 1):
                tuv = tri.template_uv(x + 0.5, py)
                if tuv is None:
                    continue
                sr, sg, sb, sa = _sample_bilinear(tpl, tw, th, tuv[0], tuv[1], has_alpha)
                if sa <= 0.0:
                    continue
                i = (row + x) * 4
                if sa >= 1.0:
                    buf[i] = sr
                    buf[i + 1] = sg
                    buf[i + 2] = sb
                else:
                    inv = 1.0 - sa
                    buf[i] = sr * sa + buf[i] * inv
                    buf[i + 1] = sg * sa + buf[i + 1] * inv
                    buf[i + 2] = sb * sa + buf[i + 2] * inv
                buf[i + 3] = 1.0


def _rasterize_layer_over(buf, w: int, h: int, lookup: _GuideLookup, tpl, tw: int, th: int, has_alpha: bool):
    """Porter-Duff 'over' for RGBA destinations (R6 clothing canvas, which
    starts transparent so per-limb body colors can be composited beneath).
    Same destination row flip as _rasterize_layer."""
    np = _numpy()
    if np is not None and isinstance(buf, np.ndarray):
        _rasterize_layer_over_np(np, buf, w, h, lookup, tpl, tw, th, has_alpha)
        return
    for tri in lookup.triangles:
        x0 = max(0, int(tri.minx))
        x1 = min(w - 1, int(tri.maxx + 1.0))
        y0 = max(0, int(tri.miny))
        y1 = min(h - 1, int(tri.maxy + 1.0))
        if x1 < x0 or y1 < y0:
            continue
        for y in range(y0, y1 + 1):
            py = y + 0.5
            row = (h - 1 - y) * w
            for x in range(x0, x1 + 1):
                tuv = tri.template_uv(x + 0.5, py)
                if tuv is None:
                    continue
                sr, sg, sb, sa = _sample_bilinear(tpl, tw, th, tuv[0], tuv[1], has_alpha)
                if sa <= 0.0:
                    continue
                i = (row + x) * 4
                da = buf[i + 3]
                out_a = sa + da * (1.0 - sa)
                if out_a <= 1e-6:
                    continue
                inv = 1.0 - sa
                buf[i] = (sr * sa + buf[i] * da * inv) / out_a
                buf[i + 1] = (sg * sa + buf[i + 1] * da * inv) / out_a
                buf[i + 2] = (sb * sa + buf[i + 2] * da * inv) / out_a
                buf[i + 3] = out_a


def _fill_guide(buf, w: int, h: int, lookup: _GuideLookup, rgba):
    """Solid-fill a guide's triangles (R6 body-color undercoat for a limb).
    Same destination row flip as _rasterize_layer.  ``rgba`` is sRGB-encoded
    and is decoded to linear here."""
    np = _numpy()
    if np is not None and isinstance(buf, np.ndarray):
        _fill_guide_np(np, buf, w, h, lookup, rgba)
        return
    r = _srgb_decode(rgba[0])
    g = _srgb_decode(rgba[1])
    b = _srgb_decode(rgba[2])
    a = float(rgba[3])
    for tri in lookup.triangles:
        x0 = max(0, int(tri.minx))
        x1 = min(w - 1, int(tri.maxx + 1.0))
        y0 = max(0, int(tri.miny))
        y1 = min(h - 1, int(tri.maxy + 1.0))
        if x1 < x0 or y1 < y0:
            continue
        for y in range(y0, y1 + 1):
            py = y + 0.5
            row = (h - 1 - y) * w
            for x in range(x0, x1 + 1):
                if tri.template_uv(x + 0.5, py) is None:
                    continue
                i = (row + x) * 4
                buf[i] = r
                buf[i + 1] = g
                buf[i + 2] = b
                buf[i + 3] = a


def _bake_group_pixels_np(np, group: str, body_rgba, template_provider):
    """Vectorized R15 crop bake (numpy).  Composites in scene-linear:
    body colour and every template decode on entry, the result re-encodes
    on exit."""
    rect = _GROUP_RECT.get(group)
    if rect is None:
        return None
    w, h = rect[2], rect[3]
    br = _srgb_decode(body_rgba[0])
    bg = _srgb_decode(body_rgba[1])
    bb = _srgb_decode(body_rgba[2])
    buf = np.empty(w * h * 4, dtype=np.float32)
    buf[0::4] = br
    buf[1::4] = bg
    buf[2::4] = bb
    buf[3::4] = 1.0
    lookup = _get_guide(group)
    if lookup is None:
        print(f"[RbxClothing] no guide lookup for '{group}'; baking body color only")
    for layer_key in _GROUP_LAYERS.get(group, ()):
        ref = _CLOTHING_CONTEXT.get(layer_key)
        if not ref or lookup is None:
            continue
        template = template_provider(ref)
        if template is None:
            print(f"[RbxClothing] template fetch failed for {layer_key} ref={ref!r}")
            continue
        tpl, tw, th, has_alpha = template
        tpl = _decode_rgb(tpl)
        _rasterize_layer(buf, w, h, lookup, tpl, tw, th, has_alpha)
    return w, h, _encode_rgb(buf)


def _bake_group_pixels(group: str, body_rgba, template_provider):
    """Bake one limb group's crop. ``template_provider(ref)`` must return
    (pixels, width, height, has_alpha) or None. bpy-free for headless tests.
    Composites in scene-linear and re-encodes on exit.
    Returns (width, height, buffer) or None."""
    np = _numpy()
    if np is not None:
        return _bake_group_pixels_np(np, group, body_rgba, template_provider)
    rect = _GROUP_RECT.get(group)
    if rect is None:
        return None
    w, h = rect[2], rect[3]
    br = _srgb_decode(body_rgba[0])
    bg = _srgb_decode(body_rgba[1])
    bb = _srgb_decode(body_rgba[2])
    buf = []
    pixel = (br, bg, bb, 1.0)
    buf.extend(pixel * (w * h))

    lookup = _get_guide(group)
    if lookup is None:
        print(f"[RbxClothing] no guide lookup for '{group}'; baking body color only")
    for layer_key in _GROUP_LAYERS.get(group, ()):
        ref = _CLOTHING_CONTEXT.get(layer_key)
        if not ref or lookup is None:
            continue
        template = template_provider(ref)
        if template is None:
            print(f"[RbxClothing] template fetch failed for {layer_key} ref={ref!r}")
            continue
        tpl, tw, th, has_alpha = template
        tpl = _decode_rgb(tpl)
        _rasterize_layer(buf, w, h, lookup, tpl, tw, th, has_alpha)
    return w, h, _encode_rgb(buf)


def _template_pixels_bpy(image):
    try:
        tw, th = int(image.size[0]), int(image.size[1])
    except Exception:
        return None
    if tw <= 0 or th <= 0:
        return None
    try:
        np = _numpy()
        if np is not None:
            pixels = np.empty(tw * th * 4, dtype=np.float32)
            image.pixels.foreach_get(pixels)
        else:
            pixels = list(image.pixels[:])
    except Exception:
        return None
    if len(pixels) < tw * th * 4:
        return None
    has_alpha = getattr(image, "depth", 32) == 32
    return pixels, tw, th, has_alpha


def _write_image_pixels(image, buf):
    """Fast pixel upload for numpy buffers and python lists alike."""
    np = _numpy()
    if np is not None and isinstance(buf, np.ndarray):
        image.pixels.foreach_set(np.ascontiguousarray(buf, dtype=np.float32).reshape(-1))
    else:
        image.pixels = buf


def _new_bake_image(name: str, w: int, h: int):
    """Create a fresh baked clothing image datablock.

    NEVER reuse an existing datablock by name: two characters' limb bakes
    share a label ("rbx_cloth_torso") and reusing it would overwrite one
    character's pixels with the other's.  Blender suffixes duplicates; old
    images without users get purged between imports."""
    import bpy

    return bpy.data.images.new(name, width=w, height=h, alpha=False)


def _bake_group_image(group: str, body_rgba):

    from . import textures

    def provider(ref):
        # The bake is a synchronous dependency of the material build: a
        # deferred fetch would skip the clothing layer entirely (place
        # imports build sync meshes inside a defer window).
        image = textures.fetch_texture_image(
            ref, name=f"cloth_{group}", ignore_defer=True
        )
        if image is None:
            print(f"[RbxClothing] fetch_texture_image returned None for {ref!r}")
            return None
        pixels = _template_pixels_bpy(image)
        if pixels is None:
            print(f"[RbxClothing] template image unreadable for {ref!r} (size={tuple(image.size)})")
        return pixels

    baked = _bake_group_pixels(group, body_rgba, provider)
    if baked is None:
        return None
    w, h, buf = baked
    image = _new_bake_image(f"rbx_cloth_{group}", w, h)
    try:
        image.colorspace_settings.name = "sRGB"
    except Exception:
        pass
    _write_image_pixels(image, buf)
    try:
        image.pack()
    except Exception:
        pass
    image.update()
    return image


# ---------------------------------------------------------------------------
# R6 bake
# ---------------------------------------------------------------------------

_R6_CLOTHING_CACHE: Dict[str, object] = {}


def _r6_clothing_canvas_np(np, template_provider):
    """Vectorized R6 clothing canvas (numpy)."""
    w, h = _R6_CANVAS_W, _R6_CANVAS_H
    buf = np.zeros(w * h * 4, dtype=np.float32)
    for layer_key, resource in (
        ("pants_template", _R6_GUIDE_PANTS),
        ("shirt_template", _R6_GUIDE_SHIRT),
    ):
        ref = _CLOTHING_CONTEXT.get(layer_key)
        if not ref:
            continue
        lookup = _get_guide_for_resource(resource, h)
        if lookup is None:
            print(f"[RbxClothing] R6: no guide for {resource}")
            continue
        template = template_provider(ref)
        if template is None:
            print(f"[RbxClothing] R6: template fetch failed for {layer_key} ref={ref!r}")
            continue
        tpl, tw, th, has_alpha = template
        tpl = _decode_rgb(tpl)
        _rasterize_layer_over(buf, w, h, lookup, tpl, tw, th, has_alpha)
    _R6_CLOTHING_CACHE["canvas"] = buf
    return buf


def _r6_clothing_canvas(template_provider):
    """Shared RGBA canvas holding pants (layer 1) then shirt (layer 2),
    projected through the full-canvas R6 guides. Transparent where no
    clothing covers. Cached once per import."""
    if "canvas" in _R6_CLOTHING_CACHE:
        return _R6_CLOTHING_CACHE["canvas"]
    np = _numpy()
    if np is not None:
        return _r6_clothing_canvas_np(np, template_provider)
    w, h = _R6_CANVAS_W, _R6_CANVAS_H
    buf = [0.0] * (w * h * 4)
    for layer_key, resource in (
        ("pants_template", _R6_GUIDE_PANTS),
        ("shirt_template", _R6_GUIDE_SHIRT),
    ):
        ref = _CLOTHING_CONTEXT.get(layer_key)
        if not ref:
            continue
        lookup = _get_guide_for_resource(resource, h)
        if lookup is None:
            print(f"[RbxClothing] R6: no guide for {resource}")
            continue
        template = template_provider(ref)
        if template is None:
            print(f"[RbxClothing] R6: template fetch failed for {layer_key} ref={ref!r}")
            continue
        tpl, tw, th, has_alpha = template
        tpl = _decode_rgb(tpl)
        _rasterize_layer_over(buf, w, h, lookup, tpl, tw, th, has_alpha)
    _R6_CLOTHING_CACHE["canvas"] = buf
    return buf


def _bake_r6_limb_pixels_np(np, limb: str, body_rgba, template_provider):
    """Vectorized full-canvas R6 limb bake (numpy).  The canvas and body
    colour are linear (the canvas decodes its templates); the output
    re-encodes."""
    w, h = _R6_CANVAS_W, _R6_CANVAS_H
    br, bg, bb = float(body_rgba[0]), float(body_rgba[1]), float(body_rgba[2])
    body = np.array(
        [_srgb_decode(br), _srgb_decode(bg), _srgb_decode(bb)], dtype=np.float32
    )
    resource = _R6_LIMB_GUIDES.get(limb)
    lookup = _get_guide_for_resource(resource, h) if resource else None
    if lookup is None:
        print(f"[RbxClothing] R6: no guide lookup for '{limb}'; body color only")
        buf = np.empty(w * h * 4, dtype=np.float32)
        buf[0::4] = body[0]
        buf[1::4] = body[1]
        buf[2::4] = body[2]
        buf[3::4] = 1.0
        return w, h, _encode_rgb(buf)

    clothing = _r6_clothing_canvas(template_provider)
    cloth_v = np.ascontiguousarray(clothing, dtype=np.float32).reshape(h, w, 4)

    buf = np.zeros(w * h * 4, dtype=np.float32)
    _fill_guide(buf, w, h, lookup, (br, bg, bb, 1.0))
    buf_v = buf.reshape(h, w, 4)

    sa = cloth_v[:, :, 3]
    da = buf_v[:, :, 3]
    out_a = sa + da * (1.0 - sa)
    keep = (sa > 0.0) & (out_a > 1e-6)
    if keep.any():
        inv = 1.0 - sa
        buf_v[keep, :3] = (
            cloth_v[keep, :3] * sa[keep, None]
            + buf_v[keep, :3] * da[keep, None] * inv[keep, None]
        ) / out_a[keep, None]
        buf_v[keep, 3] = out_a[keep]

    a = buf_v[:, :, 3]
    inv = 1.0 - a
    buf_v[:, :, :3] = buf_v[:, :, :3] * a[:, :, None] + body * inv[:, :, None]
    buf_v[:, :, 3] = 1.0
    return w, h, _encode_rgb(buf)


def _bake_r6_limb_pixels(limb: str, body_rgba, template_provider):
    """Full-canvas R6 bake for one limb: body color through the limb's
    guide, clothing 'over' on top, flattened onto the body color.  Linear
    composite, sRGB-encoded output."""
    np = _numpy()
    if np is not None:
        return _bake_r6_limb_pixels_np(np, limb, body_rgba, template_provider)
    w, h = _R6_CANVAS_W, _R6_CANVAS_H
    resource = _R6_LIMB_GUIDES.get(limb)
    lookup = _get_guide_for_resource(resource, h) if resource else None
    if lookup is None:
        print(f"[RbxClothing] R6: no guide lookup for '{limb}'; body color only")
        br = _srgb_decode(body_rgba[0])
        bg = _srgb_decode(body_rgba[1])
        bb = _srgb_decode(body_rgba[2])
        buf = []
        buf.extend((br, bg, bb, 1.0) * (w * h))
        return w, h, _encode_rgb(buf)

    clothing = _r6_clothing_canvas(template_provider)

    # Limb canvas: transparent, body color through the guide, clothing over.
    buf = [0.0] * (w * h * 4)
    _fill_guide(buf, w, h, lookup, (body_rgba[0], body_rgba[1], body_rgba[2], 1.0))
    for i in range(w * h):
        si = i * 4
        sa = clothing[si + 3]
        if sa <= 0.0:
            continue
        da = buf[si + 3]
        out_a = sa + da * (1.0 - sa)
        inv = 1.0 - sa
        buf[si] = (clothing[si] * sa + buf[si] * da * inv) / out_a
        buf[si + 1] = (clothing[si + 1] * sa + buf[si + 1] * da * inv) / out_a
        buf[si + 2] = (clothing[si + 2] * sa + buf[si + 2] * da * inv) / out_a
        buf[si + 3] = out_a

    # Flatten onto the body color so unsampled regions are sane (the limb's
    # mesh UVs only ever address its own guide region anyway).
    br = _srgb_decode(body_rgba[0])
    bg = _srgb_decode(body_rgba[1])
    bb = _srgb_decode(body_rgba[2])
    for i in range(w * h):
        si = i * 4
        a = buf[si + 3]
        if a >= 1.0:
            continue
        inv = 1.0 - a
        buf[si] = buf[si] * a + br * inv
        buf[si + 1] = buf[si + 1] * a + bg * inv
        buf[si + 2] = buf[si + 2] * a + bb * inv
        buf[si + 3] = 1.0
    return w, h, _encode_rgb(buf)


def _bake_r6_limb_image(limb: str, body_rgba):

    from . import textures

    def provider(ref):
        # Synchronous bake dependency — must not honor deferred loading
        # (see _bake_group_image).
        image = textures.fetch_texture_image(
            ref, name=f"cloth_r6_{limb}", ignore_defer=True
        )
        if image is None:
            print(f"[RbxClothing] fetch_texture_image returned None for {ref!r}")
            return None
        return _template_pixels_bpy(image)

    w, h, buf = _bake_r6_limb_pixels(limb, body_rgba, provider)
    image = _new_bake_image(f"rbx_cloth_r6_{limb.replace(' ', '_')}", w, h)
    try:
        image.colorspace_settings.name = "sRGB"
    except Exception:
        pass
    _write_image_pixels(image, buf)
    try:
        image.pack()
    except Exception:
        pass
    image.update()
    return image


def _bake_head_pixels_np(np, body_rgba, template_provider, tint_ref=None):
    """Vectorized face-decal/tint-map composite (numpy)."""
    ref = _CLOTHING_CONTEXT.get("face_texture")
    tint = False
    if not ref and tint_ref:
        ref = tint_ref
        tint = True
    if not ref:
        return None
    template = template_provider(ref)
    if template is None:
        print(f"[RbxClothing] head texture fetch failed for ref={ref!r}")
        return None
    tpl, tw, th, has_alpha = template
    tpl = _decode_rgb(tpl)
    br = _srgb_decode(body_rgba[0])
    bg = _srgb_decode(body_rgba[1])
    bb = _srgb_decode(body_rgba[2])
    body = np.array([br, bg, bb], dtype=np.float32)
    tpl_v = np.ascontiguousarray(tpl, dtype=np.float32).reshape(th, tw, 4)
    sa_arr = tpl_v[:, :, 3] if has_alpha else np.ones((th, tw), dtype=np.float32)
    face_fade = 1.0 - float(_CLOTHING_CONTEXT.get("face_transparency") or 0.0)
    if face_fade < 1.0:
        # Decal.Transparency fades the whole face decal toward the body
        # color, exactly like the decal-plane path's alpha multiply.
        sa_arr = sa_arr * face_fade
    buf = np.empty(th * tw * 4, dtype=np.float32).reshape(th, tw, 4)
    if tint:
        with np.errstate(divide="ignore", invalid="ignore"):
            straight = np.where(
                sa_arr[:, :, None] > 1e-3,
                np.minimum(tpl_v[:, :, :3] / sa_arr[:, :, None], 1.0),
                1.0,
            )
        inv = (1.0 - sa_arr)[:, :, None]
        buf[:, :, :3] = straight * sa_arr[:, :, None] + (body * straight) * inv
    else:
        inv = (1.0 - sa_arr)[:, :, None]
        buf[:, :, :3] = tpl_v[:, :, :3] * sa_arr[:, :, None] + body * inv
    buf[:, :, 3] = 1.0
    return tw, th, _encode_rgb(buf.reshape(-1))


def _bake_head_pixels(body_rgba, template_provider, tint_ref=None):
    """Body color fill with the face decal alpha-over, 1:1 in the decal's
    own pixel space (the head mesh's UVs address the decal directly).

    When ``tint_ref`` is given instead (rthro/skinned heads with no face
    decal), the texture is treated as a luminance tint map: output is
    headColor x straight(rgb), matching Roblox's MeshPart rendering of
    grayscale+alpha head textures. Blender hands us premultiplied pixels,
    so rgb is un-premultiplied defensively — the skin region of these
    maps is white-with-low-alpha and would otherwise read as black.
    Returns (width, height, buffer) or None."""
    np = _numpy()
    if np is not None:
        return _bake_head_pixels_np(np, body_rgba, template_provider, tint_ref)
    ref = _CLOTHING_CONTEXT.get("face_texture")
    tint = False
    if not ref and tint_ref:
        ref = tint_ref
        tint = True
    if not ref:
        return None
    template = template_provider(ref)
    if template is None:
        print(f"[RbxClothing] head texture fetch failed for ref={ref!r}")
        return None
    tpl, tw, th, has_alpha = template
    tpl = _decode_rgb(tpl)
    br = _srgb_decode(body_rgba[0])
    bg = _srgb_decode(body_rgba[1])
    bb = _srgb_decode(body_rgba[2])
    buf = [0.0] * (tw * th * 4)
    face_fade = 1.0 - float(_CLOTHING_CONTEXT.get("face_transparency") or 0.0)
    for i in range(0, tw * th * 4, 4):
        sa = (tpl[i + 3] if has_alpha else 1.0) * face_fade
        if tint:
            # Rthro tint maps: base = headColor x straight(gray); then the
            # straight texture alpha-overs on top. Skin is white+transparent
            # (tint zone -> headColor), eyes are opaque white (stay white),
            # features are opaque dark.
            if sa > 1e-3:
                inv_a = 1.0 / sa
                sr = min(tpl[i] * inv_a, 1.0)
                sg = min(tpl[i + 1] * inv_a, 1.0)
                sb = min(tpl[i + 2] * inv_a, 1.0)
            else:
                sr = sg = sb = 1.0
            inv = 1.0 - sa
            buf[i] = sr * sa + (br * sr) * inv
            buf[i + 1] = sg * sa + (bg * sg) * inv
            buf[i + 2] = sb * sa + (bb * sb) * inv
        else:
            inv = 1.0 - sa
            buf[i] = tpl[i] * sa + br * inv
            buf[i + 1] = tpl[i + 1] * sa + bg * inv
            buf[i + 2] = tpl[i + 2] * sa + bb * inv
        buf[i + 3] = 1.0
    return tw, th, _encode_rgb(buf)


def _bake_head_image(body_rgba, tint_ref=None):

    from . import textures

    def provider(ref):
        # Synchronous bake dependency — must not honor deferred loading
        # (see _bake_group_image).
        image = textures.fetch_texture_image(
            ref, name="cloth_head", ignore_defer=True
        )
        if image is None:
            print(f"[RbxClothing] fetch_texture_image returned None for {ref!r}")
            return None
        return _template_pixels_bpy(image)

    baked = _bake_head_pixels(body_rgba, provider, tint_ref=tint_ref)
    if baked is None:
        return None
    w, h, buf = baked
    image = _new_bake_image("rbx_cloth_head", w, h)
    try:
        image.colorspace_settings.name = "sRGB"
    except Exception:
        pass
    _write_image_pixels(image, buf)
    try:
        image.pack()
    except Exception:
        pass
    image.update()
    return image


def _hd_color_override(part_name: str) -> Optional[Tuple[float, float, float, float]]:
    """HumanoidDescription per-limb body color, if one was provided in the
    clothing context. Takes priority over the part's own Color3uint8,
    matching Roblox's render-time behavior."""
    colors = _CLOTHING_CONTEXT.get("body_colors") or {}
    if not colors:
        return None
    key = None
    if _is_head(part_name):
        key = "head"
    else:
        r6_limb = _r6_limb(part_name)
        if r6_limb is not None:
            key = r6_limb.replace(" ", "_")
        else:
            key = _limb_group(part_name)
    color = colors.get(key) if key else None
    if color is None or len(color) < 3:
        return None
    return (float(color[0]), float(color[1]), float(color[2]), 1.0)


def get_limb_texture(part_name: str, body_rgba=None, tint_ref=None):
    """Return the baked clothing texture for a body limb, or None.

    The result is cached per limb group for the duration of an import.
    ``tint_ref`` lets the caller supply a head TextureID (rthro tint maps)
    when no face decal exists."""
    body = _hd_color_override(part_name) or body_rgba or (1.0, 1.0, 1.0, 1.0)
    body_rgb = (float(body[0]), float(body[1]), float(body[2]), 1.0)

    if _is_head(part_name):
        if not clothing_available() and not tint_ref:
            return None
        cache_key = ("head", tint_ref, _context_cache_tag())
        if cache_key in _BAKE_CACHE:
            return _BAKE_CACHE[cache_key]
        try:
            image = _bake_head_image(body_rgb, tint_ref=tint_ref)
        except Exception as exc:
            print(f"[RbxClothing] head bake failed: {exc}")
            image = None
        _BAKE_CACHE[cache_key] = image
        return image

    if not clothing_available():
        return None

    r6_limb = _r6_limb(part_name)
    if r6_limb is not None:
        cache_key = ("r6", r6_limb, _context_cache_tag())
        if cache_key in _BAKE_CACHE:
            return _BAKE_CACHE[cache_key]
        try:
            image = _bake_r6_limb_image(r6_limb, body_rgb)
        except Exception as exc:
            print(f"[RbxClothing] R6 bake failed for '{r6_limb}': {exc}")
            image = None
        _BAKE_CACHE[cache_key] = image
        return image

    group = _limb_group(part_name)
    if group is None:
        return None
    cache_key = (group, _context_cache_tag())
    if cache_key in _BAKE_CACHE:
        return _BAKE_CACHE[cache_key]
    try:
        image = _bake_group_image(group, body_rgb)
    except Exception as exc:
        print(f"[RbxClothing] bake failed for '{group}': {exc}")
        image = None
    _BAKE_CACHE[cache_key] = image
    return image
