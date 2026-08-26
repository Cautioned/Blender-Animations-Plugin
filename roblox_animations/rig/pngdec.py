"""Pure-Python PNG decoder for the texture pipeline.

Uses only the stdlib (zlib/struct) and Blender's bundled numpy.  Unsupported
variants (16-bit, Adam7 interlacing, malformed payloads) return None and
callers fall back to Blender's native file loader.
"""
import struct
import zlib
from typing import Optional, Tuple

_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_COLOR_GRAY = 0
_COLOR_RGB = 2
_COLOR_PALETTE = 3
_COLOR_GRAY_A = 4
_COLOR_RGBA = 6

_BYTES_PER_PIXEL = {
    _COLOR_GRAY: 1,
    _COLOR_RGB: 3,
    _COLOR_PALETTE: 1,
    _COLOR_GRAY_A: 2,
    _COLOR_RGBA: 4,
}


def _unfilter(raw: bytes, height: int, stride: int, bpp: int):
    """Undo PNG scanline filtering; returns an (height, stride) uint8 array."""
    import numpy as np

    source = np.frombuffer(raw, dtype=np.uint8).reshape(height, stride + 1)
    recon = np.zeros((height, stride), dtype=np.uint16)
    for y in range(height):
        filter_type = int(source[y, 0])
        row = source[y, 1:].astype(np.uint16)
        above = recon[y - 1] if y > 0 else None
        if filter_type == 0:
            recon[y] = row
        elif filter_type == 1:  # Sub: recon[j] = raw[j] + recon[j-bpp]
            out = row.copy()
            for k in range(bpp):
                out[k::bpp] = np.cumsum(out[k::bpp])
            recon[y] = out & 0xFF
        elif filter_type == 2:  # Up
            recon[y] = (row + above) & 0xFF
        elif filter_type in (3, 4):  # Average / Paeth (recursive along the row)
            out = np.empty(stride, dtype=np.uint16)
            for i in range(stride):
                left = int(out[i - bpp]) if i >= bpp else 0
                up = int(above[i]) if above is not None else 0
                up_left = int(above[i - bpp]) if above is not None and i >= bpp else 0
                if filter_type == 3:
                    pred = (left + up) // 2
                else:
                    p = left + up - up_left
                    pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                    if pa <= pb and pa <= pc:
                        pred = left
                    elif pb <= pc:
                        pred = up
                    else:
                        pred = up_left
                out[i] = (int(row[i]) + pred) & 0xFF
            recon[y] = out
        else:
            return None
    return recon.astype(np.uint8)


def decode_png(data: bytes) -> Optional[Tuple[int, int, bool, "object"]]:
    """Decode an 8-bit non-interlaced PNG into uint8 RGBA (top-down).

    Returns (width, height, has_alpha, rgba_uint8 ndarray) or None when
    the payload is unsupported or malformed.
    """
    try:
        import numpy as np

        if not data.startswith(_SIGNATURE):
            return None
        width = height = bit_depth = color_type = interlace = None
        palette = None
        trns = None
        idat = bytearray()
        pos = len(_SIGNATURE)
        while pos + 8 <= len(data):
            length = struct.unpack_from(">I", data, pos)[0]
            chunk_type = data[pos + 4 : pos + 8]
            chunk_start = pos + 8
            chunk_end = chunk_start + length
            if chunk_end > len(data):
                return None
            chunk = data[chunk_start:chunk_end]
            if chunk_type == b"IHDR":
                width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                    ">IIBBBBB", chunk
                )
            elif chunk_type == b"PLTE":
                palette = chunk
            elif chunk_type == b"tRNS":
                trns = chunk
            elif chunk_type == b"IDAT":
                idat.extend(chunk)
            elif chunk_type == b"IEND":
                break
            pos = chunk_end + 4  # skip the CRC
        if (
            width is None
            or height is None
            or width <= 0
            or height <= 0
            or width * height > (1 << 26)
            or bit_depth != 8
            or interlace != 0
            or color_type not in _BYTES_PER_PIXEL
        ):
            return None
        bpp = _BYTES_PER_PIXEL[color_type]
        stride = width * bpp
        raw = zlib.decompress(bytes(idat))
        if len(raw) < height * (stride + 1):
            return None
        scan = _unfilter(raw[: height * (stride + 1)], height, stride, bpp)
        if scan is None:
            return None
        pixels = scan.reshape(height, width, bpp)

        if color_type == _COLOR_PALETTE:
            if palette is None or len(palette) % 3 != 0:
                return None
            table = np.frombuffer(palette, dtype=np.uint8).reshape(-1, 3)
            idx = pixels[:, :, 0]
            if idx.max() >= len(table):
                return None
            rgb = table[idx]
            alpha = np.full((height, width, 1), 255, dtype=np.uint8)
            if trns is not None:
                pal_alpha = np.frombuffer(trns, dtype=np.uint8)
                a = np.full(len(table), 255, dtype=np.uint8)
                a[: len(pal_alpha)] = pal_alpha
                alpha = a[idx][:, :, None]
            rgba = np.concatenate([rgb, alpha], axis=2)
            return width, height, bool(trns), rgba

        has_alpha = color_type in (_COLOR_GRAY_A, _COLOR_RGBA)
        if color_type == _COLOR_GRAY:
            rgb = np.repeat(pixels, 3, axis=2)
            alpha = np.full((height, width, 1), 255, dtype=np.uint8)
            if trns is not None and len(trns) >= 2:
                gray_key = struct.unpack(">H", trns[:2])[0]
                alpha = np.where(
                    pixels[:, :, 0] == (gray_key & 0xFF),
                    np.uint8(0),
                    np.uint8(255),
                )[:, :, None]
                has_alpha = True
            rgba = np.concatenate([rgb, alpha], axis=2)
        elif color_type == _COLOR_RGB:
            alpha = np.full((height, width, 1), 255, dtype=np.uint8)
            if trns is not None and len(trns) >= 6:
                r_key, g_key, b_key = struct.unpack(">HHH", trns[:6])
                key_match = (
                    (pixels[:, :, 0] == (r_key & 0xFF))
                    & (pixels[:, :, 1] == (g_key & 0xFF))
                    & (pixels[:, :, 2] == (b_key & 0xFF))
                )
                alpha = np.where(key_match, np.uint8(0), np.uint8(255))[:, :, None]
                has_alpha = True
            rgba = np.concatenate([pixels, alpha], axis=2)
        else:  # GRAY_A or RGBA
            if color_type == _COLOR_GRAY_A:
                gray = np.repeat(pixels[:, :, 0:1], 3, axis=2)
                rgba = np.concatenate([gray, pixels[:, :, 1:2]], axis=2)
            else:
                rgba = pixels
        return width, height, has_alpha, np.ascontiguousarray(rgba, dtype=np.uint8)
    except Exception:
        return None
