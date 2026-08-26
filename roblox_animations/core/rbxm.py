"""Minimal Roblox binary model (.rbxm) parser.

Parses enough of the binary model format to reconstruct the part-transform
context that ``creation.py`` consumes from the server-emitted metadata JSON.
This lets us bind skinned meshes entirely from the Roblox scene graph plus the
AssetDelivery-served FileMesh, with no FBX/OBJ intermediate.

The implementation is deliberately narrow: it decodes the instance hierarchy
and the property types needed for ``MeshPart`` / ``WrapLayer`` / ``WrapTarget``
reconstruction, and skips every chunk/property it does not recognise. It is
dependency-free (LZ4 block decompression is implemented inline) so it can run
inside Blender's bundled Python.

Format references:
  - https://github.com/rojo-rbx/rbx-dom/blob/master/docs/binary.md
  - CSGMDL union-mesh container layout and the 31-byte deobfuscation cycle:
    https://github.com/krakow10/rbx_mesh (format documentation only — the
    decoder here is an independent Python implementation).
"""

from __future__ import annotations

import math
import struct
from typing import Any, Dict, List, Optional, Set, Tuple

from .schema import CF12, WeaponGrip


FILE_MAGIC = b"<roblox!"
FILE_SIGNATURE = b"\x89\xff\x0d\x0a\x1a\x0a"
HEADER_SIZE = 32
END_MAGIC = b"</roblox>"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Import files are untrusted.  These limits intentionally leave ample room for
# large Studio places while preventing a model from turning the importer into a
# memory/CPU exhaustion primitive.
MAX_RBXM_SOURCE_BYTES = 512 * 1024 * 1024
_MAX_CHUNK_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_CHUNK_BYTES = 512 * 1024 * 1024
_MAX_CHUNKS = 8_192
_MAX_INSTANCES = 250_000
_MAX_STRING_BYTES = 8 * 1024 * 1024
_MAX_SHARED_STRING_BYTES = 128 * 1024 * 1024

# Property value type ids (subset we care about).
_TYPE_STRING = 0x01
_TYPE_BOOL = 0x02
_TYPE_INT32 = 0x03
_TYPE_FLOAT32 = 0x04
_TYPE_FLOAT64 = 0x05
_TYPE_INT64 = 0x06
# Per the rbx-dom spec, Int64 is type id 0x1b and is zigzag-transformed.
# Studio has also been observed writing untransformed int64 asset ids under
# 0x06 (handled via _TYPE_INT64).
_TYPE_INT64_SPEC = 0x1B
_TYPE_COLOR3 = 0x07
# Newer saves write plain float Color3 props (e.g. Texture.Color3) under
# this distinct type id; the payload is the same planar float32 triple.
_TYPE_COLOR3F = 0x0C
_TYPE_VECTOR3 = 0x0E
_TYPE_CFRAME = 0x10
_TYPE_ENUM = 0x12
_TYPE_REFERENT = 0x13
_TYPE_COLOR3UINT8 = 0x1A
_TYPE_SHARED_STRING = 0x1C
_TYPE_CONTENT = 0x22
# Type ids per the rbx-binformat spec (see rbx-reader-rts BinaryParser.ts).
_TYPE_NUMBER_SEQUENCE = 0x15
_TYPE_COLOR_SEQUENCE = 0x16
_TYPE_NUMBER_RANGE = 0x17
_SEQ_MAX_KEYPOINTS = 4096

# The 24 special-case CFrame rotations, keyed by their id byte. Each value is
# the 3x3 rotation matrix in row-major order (r00..r22). Rotations in the spec
# are (rx, ry, rz) degrees applied Y -> X -> Z. The matrices below are the
# evaluated result for each id (precomputed to avoid runtime trig).
#
# id 0x02 = identity; the rest are the axis-aligned 90-degree rotations.
_BASIC_ROTATIONS: Dict[int, Tuple[float, ...]] = {}


def _rot_x(d: float) -> Tuple[Tuple[float, float, float], ...]:
    c, s = math.cos(math.radians(d)), math.sin(math.radians(d))
    return ((1, 0, 0), (0, c, -s), (0, s, c))


def _rot_y(d: float) -> Tuple[Tuple[float, float, float], ...]:
    c, s = math.cos(math.radians(d)), math.sin(math.radians(d))
    return ((c, 0, s), (0, 1, 0), (-s, 0, c))


def _rot_z(d: float) -> Tuple[Tuple[float, float, float], ...]:
    c, s = math.cos(math.radians(d)), math.sin(math.radians(d))
    return ((c, -s, 0), (s, c, 0), (0, 0, 1))


def _mat_mul(a, b):
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _flat(m):
    return tuple(round(m[i][j], 6) for i in range(3) for j in range(3))


def _build_basic_rotations() -> None:
    # (id, rx, ry, rz) per the rbx-dom spec, applied Y -> X -> Z.
    entries = [
        (0x02, 0, 0, 0),
        (0x03, 90, 0, 0),
        (0x05, 0, 180, 180),
        (0x06, -90, 0, 0),
        (0x07, 0, 180, 90),
        (0x09, 0, 90, 90),
        (0x0A, 0, 0, 90),
        (0x0C, 0, -90, 90),
        (0x0D, -90, -90, 0),
        (0x0E, 0, -90, 0),
        (0x10, 90, -90, 0),
        (0x11, 0, 90, 180),
        (0x14, 0, 180, 0),
        (0x15, -90, -180, 0),
        (0x17, 0, 0, 180),
        (0x18, 90, 180, 0),
        (0x19, 0, 0, -90),
        (0x1B, 0, -90, -90),
        (0x1C, 0, -180, -90),
        (0x1E, 0, 90, -90),
        (0x1F, 90, 90, 0),
        (0x20, 0, 90, 0),
        (0x22, -90, 90, 0),
        (0x23, 0, -90, 180),
    ]
    for ident, rx, ry, rz in entries:
        # Spec: rotations applied Y -> X -> Z. Matrix composition applies the
        # rightmost factor first, so the composite is Ry @ Rx @ Rz.
        m = _mat_mul(_rot_y(ry), _mat_mul(_rot_x(rx), _rot_z(rz)))
        _BASIC_ROTATIONS[ident] = _flat(m)


_build_basic_rotations()


class RbxmError(ValueError):
    """Raised when a .rbxm buffer cannot be parsed."""


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def read(self, n: int) -> bytes:
        if n < 0:
            raise RbxmError("negative buffer read")
        if self.pos + n > len(self.data):
            raise RbxmError("unexpected end of buffer")
        out = self.data[self.pos: self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.read(1)[0]

    def u16le(self) -> int:
        return struct.unpack_from("<H", self.read(2))[0]

    def u32le(self) -> int:
        return struct.unpack_from("<I", self.read(4))[0]

    def i32le(self) -> int:
        return struct.unpack_from("<i", self.read(4))[0]

    def f32le(self) -> float:
        return struct.unpack_from("<f", self.read(4))[0]

    def f64le(self) -> float:
        return struct.unpack_from("<d", self.read(8))[0]

    def string(self) -> str:
        length = self.u32le()
        if length > _MAX_STRING_BYTES:
            raise RbxmError("string exceeds import safety limit")
        return self.read(length).decode("utf-8", errors="replace")

    def binary_string(self):
        """Read a length-prefixed string, preserving binary payloads as bytes.

        Roblox abuses ``String`` properties to smuggle binary blobs (union
        ChildData operand trees, EditableImage data, ...). ``str`` decoding
        with ``errors="replace"`` corrupts those payloads, so this returns
        ``bytes`` whenever the payload is not clean UTF-8.
        """
        length = self.u32le()
        if length > _MAX_STRING_BYTES:
            raise RbxmError("binary string exceeds import safety limit")
        data = self.read(length)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data


def _untransform_i32(value: int) -> int:
    # Inverse of Roblox's zigzag: (x << 1) ^ (x >> 31).
    value &= 0xFFFFFFFF
    return (value >> 1) ^ -(value & 1)


def _untransform_i64(value: int) -> int:
    # Inverse of Roblox's 64-bit zigzag: (x << 1) ^ (x >> 63).
    value &= 0xFFFFFFFFFFFFFFFF
    return (value >> 1) ^ -(value & 1)


def _deinterleave(data: bytes, count: int, size: int) -> bytes:
    """Undo byte interleaving for `count` values of `size` bytes each."""
    if count == 0:
        return b""
    if len(data) < count * size:
        raise RbxmError("truncated interleaved array")
    out = bytearray(count * size)
    for byte_index in range(size):
        source_start = byte_index * count
        out[byte_index::size] = data[source_start: source_start + count]
    return bytes(out)


def _roblox_float_to_ieee(bits: int) -> int:
    # Roblox stores the sign bit after the mantissa (LSB); standard IEEE-754
    # stores it as the MSB. Rotate the 32-bit value right by one.
    bits &= 0xFFFFFFFF
    return ((bits >> 1) | ((bits & 1) << 31)) & 0xFFFFFFFF


def _lz4_block_decompress(src: bytes, expected_length: int) -> bytes:
    """Decompress a raw LZ4 block (no frame). Dependency-free fallback.

    Roblox writes LZ4 *block* format (not the framed .lz4 format), so we
    implement the token/literal/match state machine directly.
    """
    out = bytearray()
    src_len = len(src)
    i = 0
    try:
        while i < src_len:
            token = src[i]
            i += 1

            literal_length = token >> 4
            if literal_length == 15:
                while True:
                    b = src[i]
                    i += 1
                    literal_length += b
                    if b != 255:
                        break
            if literal_length > expected_length - len(out):
                raise RbxmError("lz4 output exceeds declared chunk length")
            out += src[i: i + literal_length]
            i += literal_length

            if i >= src_len:
                # Last sequence has no match.
                break

            offset = src[i] | (src[i + 1] << 8)
            i += 2
            if offset == 0:
                raise RbxmError("lz4 match offset of zero")

            match_length = token & 0x0F
            if match_length == 15:
                while True:
                    b = src[i]
                    i += 1
                    match_length += b
                    if b != 255:
                        break
            match_length += 4

            if match_length > expected_length - len(out):
                raise RbxmError("lz4 output exceeds declared chunk length")

            start = len(out) - offset
            if start < 0:
                raise RbxmError("lz4 match offset before output start")
            # An overlapping LZ4 match repeats the ``offset``-byte prefix.
            # Building that repetition in C-backed bytes operations avoids a
            # Python append for every output byte (millions on large places).
            pattern = bytes(out[start: start + min(offset, match_length)])
            if match_length <= offset:
                out.extend(pattern)
            else:
                repetitions, remainder = divmod(match_length, offset)
                out.extend(pattern * repetitions)
                if remainder:
                    out.extend(pattern[:remainder])
    except IndexError as exc:  # pragma: no cover - corrupt data
        raise RbxmError(f"malformed lz4 block: {exc}") from exc

    if len(out) != expected_length:
        raise RbxmError("lz4 output length does not match chunk header")
    return bytes(out)


def _decompress_chunk_body(body: bytes, compressed_len: int, uncompressed_len: int) -> bytes:
    if uncompressed_len > _MAX_CHUNK_BYTES:
        raise RbxmError("chunk exceeds import safety limit")
    if compressed_len == 0:
        if len(body) != uncompressed_len:
            raise RbxmError("uncompressed chunk length does not match header")
        return body
    if body[:4] == _ZSTD_MAGIC:
        raise RbxmError(
            "zstd-compressed rbxm chunks are not supported (no bundled decoder)"
        )
    return _lz4_block_decompress(body, uncompressed_len)


def _read_interleaved_i32(reader: _Reader, count: int) -> List[int]:
    raw = _deinterleave(reader.read(count * 4), count, 4)
    if not count:
        return []
    if count >= 256:
        try:
            import numpy as np  # bundled with Blender

            values = np.frombuffer(raw, dtype=">i4")
            return ((values >> 1) ^ -(values & 1)).tolist()
        except ImportError:
            pass
    values = struct.unpack_from(f">{count}i", raw, 0)
    return [_untransform_i32(v) for v in values]


def _read_interleaved_u32(reader: _Reader, count: int) -> List[int]:
    raw = _deinterleave(reader.read(count * 4), count, 4)
    if not count:
        return []
    if count >= 256:
        try:
            import numpy as np  # bundled with Blender

            return np.frombuffer(raw, dtype=">u4").tolist()
        except ImportError:
            pass
    return list(struct.unpack_from(f">{count}I", raw, 0))


def _read_interleaved_i64(reader: _Reader, count: int) -> List[int]:
    # Int64 properties (e.g. HumanoidDescription clothing asset ids) are
    # byte-interleaved like Int32 but stored big-endian WITHOUT the zigzag
    # transform.
    raw = _deinterleave(reader.read(count * 8), count, 8)
    if not count:
        return []
    if count >= 256:
        try:
            import numpy as np  # bundled with Blender

            return np.frombuffer(raw, dtype=">i8").tolist()
        except ImportError:
            pass
    return list(struct.unpack_from(f">{count}q", raw, 0))


def _read_interleaved_i64_zigzag(reader: _Reader, count: int) -> List[int]:
    """Spec Int64 (0x1b): big-endian zigzag-transformed, byte-interleaved."""
    raw = _deinterleave(reader.read(count * 8), count, 8)
    if not count:
        return []
    if count >= 256:
        try:
            import numpy as np  # bundled with Blender

            return [_untransform_i64(v) for v in np.frombuffer(raw, dtype=">u8").tolist()]
        except ImportError:
            pass
    return [_untransform_i64(v) for v in struct.unpack_from(f">{count}Q", raw, 0)]


def _read_interleaved_f32(reader: _Reader, count: int) -> List[float]:
    raw = _deinterleave(reader.read(count * 4), count, 4)
    if not count:
        return []
    if count >= 256:
        try:
            import numpy as np

            encoded = np.frombuffer(raw, dtype=">u4").astype(np.uint32)
            ieee = (encoded >> 1) | ((encoded & 1) << 31)
            return ieee.view(np.float32).tolist()
        except ImportError:
            pass
    ints = struct.unpack_from(f">{count}I", raw, 0)
    floats = []
    for bits in ints:
        ieee = _roblox_float_to_ieee(bits)
        floats.append(struct.unpack("<f", struct.pack("<I", ieee))[0])
    return floats


def _read_referents(reader: _Reader, count: int) -> List[int]:
    values = _read_interleaved_i32(reader, count)
    if count >= 256:
        try:
            import numpy as np  # bundled with Blender

            return np.cumsum(np.asarray(values, dtype=np.int64)).tolist()
        except ImportError:
            pass
    out = []
    accum = 0
    for delta in values:
        accum += delta
        out.append(accum)
    return out


def _read_cframes(reader: _Reader, count: int) -> List[Tuple[float, ...]]:
    rotations: List[Tuple[float, ...]] = []
    for _ in range(count):
        ident = reader.u8()
        if ident == 0:
            # Raw 9-float orientation, stored row-major R00..R22 per the spec.
            rotations.append(struct.unpack_from("<9f", reader.read(36), 0))
        else:
            rotation = _BASIC_ROTATIONS.get(ident)
            if rotation is None:
                raise RbxmError(f"unknown basic rotation id 0x{ident:02x}")
            rotations.append(rotation)

    xs = _read_interleaved_f32(reader, count)
    ys = _read_interleaved_f32(reader, count)
    zs = _read_interleaved_f32(reader, count)

    cframes = []
    for index in range(count):
        r = rotations[index]
        cframes.append(
            (
                xs[index],
                ys[index],
                zs[index],
                r[0], r[1], r[2],
                r[3], r[4], r[5],
                r[6], r[7], r[8],
            )
        )
    return cframes


def _read_contents(reader: _Reader, count: int) -> List[str]:
    """Decode the 0x22 Content type, returning the URI (or empty string)."""
    source_types = _read_interleaved_i32(reader, count)
    uri_count = reader.u32le()
    if uri_count > _MAX_INSTANCES:
        raise RbxmError("content URI table exceeds import safety limit")
    uris = [reader.string() for _ in range(uri_count)]
    object_count = reader.u32le()
    if object_count > _MAX_INSTANCES:
        raise RbxmError("content object table exceeds import safety limit")
    _read_referents(reader, object_count)  # discard object refs
    external_count = reader.u32le()
    if external_count > _MAX_INSTANCES:
        raise RbxmError("content external table exceeds import safety limit")
    _read_referents(reader, external_count)  # discard external refs

    out = []
    uri_iter = iter(uris)
    for source_type in source_types:
        if source_type == 1:  # Uri
            out.append(next(uri_iter, ""))
        else:
            out.append("")
    return out


_PROP_READERS = {}


def _read_prop_values(reader: _Reader, type_id: int, count: int):
    if type_id == _TYPE_STRING:
        # Binary-safe: text props come back as str, binary blobs as bytes.
        return [reader.binary_string() for _ in range(count)]
    if type_id == _TYPE_BOOL:
        return [bool(reader.u8()) for _ in range(count)]
    if type_id == _TYPE_INT32:
        return _read_interleaved_i32(reader, count)
    if type_id == _TYPE_INT64:
        return _read_interleaved_i64(reader, count)
    if type_id == _TYPE_INT64_SPEC:
        return _read_interleaved_i64_zigzag(reader, count)
    if type_id == _TYPE_SHARED_STRING:
        # Indices into the SSTR table; resolved to the actual shared bytes
        # after all chunks are parsed (SSTR precedes PROP per the spec).
        return _read_interleaved_u32(reader, count)
    if type_id == _TYPE_FLOAT32:
        return _read_interleaved_f32(reader, count)
    if type_id == _TYPE_FLOAT64:
        return [reader.f64le() for _ in range(count)]
    if type_id == _TYPE_COLOR3:
        rs = _read_interleaved_f32(reader, count)
        gs = _read_interleaved_f32(reader, count)
        bs = _read_interleaved_f32(reader, count)
        if count >= 256:
            try:
                import numpy as np  # bundled with Blender

                return np.column_stack((rs, gs, bs)).tolist()
            except ImportError:
                pass
        return [[rs[i], gs[i], bs[i]] for i in range(count)]
    if type_id == _TYPE_COLOR3F:
        return _read_prop_values(reader, _TYPE_COLOR3, count)
    if type_id == _TYPE_VECTOR3:
        xs = _read_interleaved_f32(reader, count)
        ys = _read_interleaved_f32(reader, count)
        zs = _read_interleaved_f32(reader, count)
        if count >= 256:
            try:
                import numpy as np  # bundled with Blender

                return np.column_stack((xs, ys, zs)).tolist()
            except ImportError:
                pass
        return [[xs[i], ys[i], zs[i]] for i in range(count)]
    if type_id == _TYPE_CFRAME:
        return _read_cframes(reader, count)
    if type_id == _TYPE_ENUM:
        return _read_interleaved_u32(reader, count)
    if type_id == _TYPE_REFERENT:
        return _read_referents(reader, count)
    if type_id == _TYPE_COLOR3UINT8:
        raw = reader.read(count * 3)
        # Stored as three consecutive arrays (R..R, G..G, B..B), no interleave.
        rs = raw[0:count]
        gs = raw[count: 2 * count]
        bs = raw[2 * count: 3 * count]
        return [(rs[i], gs[i], bs[i]) for i in range(count)]
    if type_id == _TYPE_CONTENT:
        return _read_contents(reader, count)
    if type_id == _TYPE_NUMBER_SEQUENCE:
        # Per instance: u32 keypoint count, then that many (time, value,
        # envelope) plain LITTLE-endian f32 triples (no bit transform).
        out = []
        for _ in range(count):
            keypoints = reader.u32le()
            if keypoints > _SEQ_MAX_KEYPOINTS:
                raise RbxmError("number sequence exceeds import safety limit")
            raw = reader.read(keypoints * 12)
            values = struct.unpack_from(f"<{keypoints * 3}f", raw, 0) if keypoints else ()
            out.append([
                [values[i * 3], values[i * 3 + 1], values[i * 3 + 2]]
                for i in range(keypoints)
            ])
        return out
    if type_id == _TYPE_COLOR_SEQUENCE:
        # Per instance: u32 keypoint count, then per keypoint five plain
        # LITTLE-endian f32s: (time, r, g, b, envelope).
        out = []
        for _ in range(count):
            keypoints = reader.u32le()
            if keypoints > _SEQ_MAX_KEYPOINTS:
                raise RbxmError("color sequence exceeds import safety limit")
            raw = reader.read(keypoints * 20)
            values = struct.unpack_from(f"<{keypoints * 5}f", raw, 0) if keypoints else ()
            out.append([
                [values[i * 5], [values[i * 5 + 1], values[i * 5 + 2], values[i * 5 + 3]], values[i * 5 + 4]]
                for i in range(keypoints)
            ])
        return out
    if type_id == _TYPE_NUMBER_RANGE:
        # Per instance: two plain LITTLE-endian f32s (min, max).
        return [[*struct.unpack_from("<2f", reader.read(8), 0)] for _ in range(count)]
    raise RbxmError(f"unsupported property type id 0x{type_id:02x}")


class _Instance:
    __slots__ = ("referent", "class_name", "name", "props", "children", "parent")

    def __init__(self, referent: int, class_name: str):
        self.referent = referent
        self.class_name = class_name
        self.name = ""
        self.props: Dict[str, object] = {}
        self.children: List["_Instance"] = []
        self.parent: Optional[int] = None

    def __repr__(self):  # pragma: no cover
        return f"<{self.class_name} {self.name!r} #{self.referent}>"


# Attribute blob type ids (rbxattr spec).  Only the types the exporter uses
# (strings) plus the common ones needed to SKIP foreign attributes need to
# be understood; unknown types abort the scan defensively.
_ATTR_STRING = 0x02
_ATTR_BOOL = 0x03
_ATTR_NUMBER = 0x06
_ATTR_UDIM = 0x09
_ATTR_UDIM2 = 0x0A
_ATTR_BRICKCOLOR = 0x0E
_ATTR_COLOR3 = 0x0F
_ATTR_VECTOR2 = 0x10
_ATTR_VECTOR3 = 0x11
_ATTR_NUMBER_SEQUENCE = 0x17
_ATTR_COLOR_SEQUENCE = 0x19
_ATTR_RECT = 0x1C
_ATTR_NUMBER_RANGE = 0x1B
_ATTR_MAX_KEYS = 256


def _decode_attributes_blob(blob: bytes) -> Dict[str, Any]:
    """Decode a serialized ``Attributes`` property blob into a name->value dict.

    Format (little-endian): u32 attribute count, then per attribute u32 key
    length, key bytes, u8 type id, and the type's value.  Values we don't
    need (e.g. sequences) stop the scan instead of guessing sizes.
    """
    reader = _Reader(blob)
    out: Dict[str, Any] = {}
    try:
        count = min(reader.u32le(), _ATTR_MAX_KEYS)
        for _ in range(count):
            key_length = reader.u32le()
            if key_length > _MAX_STRING_BYTES:
                break
            key = reader.read(key_length).decode("utf-8", errors="replace")
            type_id = reader.u8()
            if type_id == _ATTR_STRING:
                length = reader.u32le()
                if length > _MAX_STRING_BYTES:
                    break
                value: object = reader.read(length).decode("utf-8", errors="replace")
            elif type_id == _ATTR_BOOL:
                value = bool(reader.u8())
            elif type_id == _ATTR_NUMBER:
                value = reader.f64le()
            elif type_id == _ATTR_VECTOR3:
                value = [reader.f32le(), reader.f32le(), reader.f32le()]
            elif type_id == _ATTR_VECTOR2:
                value = [reader.f32le(), reader.f32le()]
            elif type_id == _ATTR_UDIM:
                value = [reader.f32le(), reader.i32le()]
            elif type_id == _ATTR_UDIM2:
                value = [reader.f32le(), reader.i32le(), reader.f32le(), reader.i32le()]
            elif type_id == _ATTR_COLOR3:
                value = [reader.f32le(), reader.f32le(), reader.f32le()]
            elif type_id == _ATTR_BRICKCOLOR:
                value = reader.u32le()
            elif type_id == _ATTR_RECT:
                value = [reader.f32le() for _ in range(4)]
            elif type_id == _ATTR_NUMBER_RANGE:
                value = [reader.f32le(), reader.f32le()]
            elif type_id == _ATTR_NUMBER_SEQUENCE:
                keypoints = reader.u32le()
                if keypoints > _SEQ_MAX_KEYPOINTS:
                    break
                raw = reader.read(keypoints * 12)
                values = struct.unpack_from(f"<{keypoints * 3}f", raw, 0) if keypoints else ()
                value = [
                    [values[i * 3], values[i * 3 + 1], values[i * 3 + 2]]
                    for i in range(keypoints)
                ]
            elif type_id == _ATTR_COLOR_SEQUENCE:
                keypoints = reader.u32le()
                if keypoints > _SEQ_MAX_KEYPOINTS:
                    break
                raw = reader.read(keypoints * 20)
                values = struct.unpack_from(f"<{keypoints * 5}f", raw, 0) if keypoints else ()
                value = [
                    [values[i * 5], [values[i * 5 + 1], values[i * 5 + 2], values[i * 5 + 3]], values[i * 5 + 4]]
                    for i in range(keypoints)
                ]
            else:
                # Unknown attribute type: cannot size-skip reliably, stop.
                break
            out[key] = value
    except RbxmError:
        pass
    return out


def _instance_attributes(inst: _Instance) -> Dict[str, Any]:
    """Decoded attributes for one instance (empty dict when absent/broken)."""
    blob = inst.props.get("Attributes")
    if isinstance(blob, (bytes, bytearray)):
        return _decode_attributes_blob(bytes(blob))
    return {}


def _parse_cf12(text: object) -> Optional[CF12]:
    """Comma-separated 12-float CFrame string -> list, or None."""
    if not isinstance(text, str):
        return None
    try:
        values = [float(part) for part in text.split(",")]
    except ValueError:
        return None
    return values if len(values) == 12 else None


# Attribute names the Studio plugin stamps for weapon grip connections.
# (No RBX-prefixed names: that namespace is reserved for CoreScripts.)
_WEAPON_GRIP_COUNT = "BlenderGripCount"
_WEAPON_GRIP_PREFIX = "BlenderGrip"
_WEAPON_GRIP_VERSION = "BlenderGripVersion"


def _parse_chunks(data: bytes) -> Tuple[Dict[int, _Instance], Dict[int, _Instance]]:
    if len(data) > MAX_RBXM_SOURCE_BYTES:
        raise RbxmError("file exceeds import safety limit")
    if data[:8] != FILE_MAGIC:
        raise RbxmError("not a roblox binary model (bad magic)")
    if data[8:14] != FILE_SIGNATURE:
        raise RbxmError("not a roblox binary model (bad signature)")

    reader = _Reader(data, 0)
    reader.read(14)
    _version = reader.u16le()
    _class_count = reader.i32le()
    _instance_count = reader.i32le()
    if not (0 <= _class_count <= _MAX_INSTANCES):
        raise RbxmError("invalid or excessive class count")
    if not (0 <= _instance_count <= _MAX_INSTANCES):
        raise RbxmError("invalid or excessive instance count")
    reader.read(8)  # reserved

    instances: Dict[int, _Instance] = {}
    classes: Dict[int, Tuple[str, List[int]]] = {}
    shared_strings: List[bytes] = []
    shared_string_props: List[Tuple[_Instance, str, object]] = []
    decoded_chunk_bytes = 0
    shared_string_bytes = 0
    chunk_count = 0

    while reader.remaining() > 0:
        chunk_count += 1
        if chunk_count > _MAX_CHUNKS:
            raise RbxmError("too many chunks")
        name = reader.read(4).rstrip(b"\0")
        compressed_len = reader.u32le()
        uncompressed_len = reader.u32le()
        reader.read(4)  # reserved
        if compressed_len > _MAX_CHUNK_BYTES:
            raise RbxmError("compressed chunk exceeds import safety limit")
        body_len = compressed_len if compressed_len else uncompressed_len
        body = reader.read(body_len)

        if name == b"END":
            break

        decoded_chunk_bytes += uncompressed_len
        if decoded_chunk_bytes > _MAX_TOTAL_CHUNK_BYTES:
            raise RbxmError("decoded chunks exceed import safety limit")

        chunk = _decompress_chunk_body(body, compressed_len, uncompressed_len)
        cr = _Reader(chunk)

        if name == b"SSTR":
            _version = cr.u32le()
            count = cr.u32le()
            if count > _MAX_INSTANCES:
                raise RbxmError("shared string table exceeds import safety limit")
            for _ in range(count):
                cr.read(16)  # MD5 hash (unused by Studio, per the spec)
                length = cr.u32le()
                if length > _MAX_STRING_BYTES:
                    raise RbxmError("shared string exceeds import safety limit")
                shared_string_bytes += length
                if shared_string_bytes > _MAX_SHARED_STRING_BYTES:
                    raise RbxmError("shared strings exceed import safety limit")
                shared_strings.append(cr.read(length))

        elif name == b"INST":
            class_id = cr.u32le()
            class_name = cr.string()
            object_format = cr.u8()
            count = cr.u32le()
            if count > _instance_count or count > _MAX_INSTANCES:
                raise RbxmError("instance chunk exceeds declared instance count")
            if len(instances) + count > _instance_count:
                raise RbxmError("instances exceed declared instance count")
            if class_id not in classes and len(classes) >= _class_count:
                raise RbxmError("classes exceed declared class count")
            referents = _read_referents(cr, count)
            if object_format == 1:
                cr.read(count)  # service markers
            classes[class_id] = (class_name, referents)
            for referent in referents:
                instances[referent] = _Instance(referent, class_name)

        elif name == b"PROP":
            class_id = cr.u32le()
            prop_name = cr.string()
            if cr.remaining() <= 0:
                continue
            type_id = cr.u8()
            class_info = classes.get(class_id)
            if class_info is None:
                break
            class_name, referents = class_info
            # Attributes serialize as a String-typed binary blob; decoding
            # it as UTF-8 would corrupt the payload.  Read it raw here and
            # let consumers decode it on demand.  Studio has used both
            # property names across versions.
            if prop_name in ("Attributes", "AttributesSerialize") and type_id == _TYPE_STRING:
                values = []
                try:
                    for _ in range(len(referents)):
                        length = cr.u32le()
                        if length > _MAX_STRING_BYTES:
                            raise RbxmError("attributes blob exceeds import safety limit")
                        values.append(cr.read(length))
                except RbxmError:
                    continue
                for referent, value in zip(referents, values):
                    inst = instances.get(referent)
                    if inst is not None:
                        inst.props["Attributes"] = value
                continue
            try:
                values = _read_prop_values(cr, type_id, len(referents))
            except RbxmError:
                # Unknown/unsupported property type; skip this chunk.
                continue
            for referent, value in zip(referents, values):
                inst = instances.get(referent)
                if inst is None:
                    continue
                if prop_name == "Name":
                    inst.name = value if isinstance(value, str) else str(value)
                elif type_id == _TYPE_SHARED_STRING:
                    # Deferred: SSTR may be compressed while the PROP chunks
                    # are parsed in any order, so resolve indices afterwards.
                    shared_string_props.append((inst, prop_name, value))
                else:
                    inst.props[prop_name] = value

        elif name == b"PRNT":
            _prnt_version = cr.u8()
            count = cr.u32le()
            if count > _instance_count or count > _MAX_INSTANCES:
                raise RbxmError("parenting chunk exceeds declared instance count")
            child_refs = _read_referents(cr, count)
            parent_refs = _read_referents(cr, count)
            for child_ref, parent_ref in zip(child_refs, parent_refs):
                child = instances.get(child_ref)
                if child is None:
                    continue
                child.parent = parent_ref if parent_ref != -1 else None

        # META / SIGN and anything else: ignored.

    # Resolve SharedString indices to the actual shared payloads.
    for inst, prop_name, index in shared_string_props:
        if isinstance(index, int) and 0 <= index < len(shared_strings):
            inst.props[prop_name] = shared_strings[index]

    # Link children.
    roots: Dict[int, _Instance] = {}
    for referent, inst in instances.items():
        parent = instances.get(inst.parent) if inst.parent is not None else None
        if parent is not None:
            parent.children.append(inst)
        else:
            roots[referent] = inst

    return instances, roots


def _first_prop(inst: _Instance, *names: str) -> Any:
    for name in names:
        value = inst.props.get(name)
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# UnionOperation / NegateOperation render meshes (CSG)
#
# Modern Studio serialises a union's render mesh into the ``MeshData2``
# SharedString property (with the CSG operand tree in ``ChildData2``). The
# payload is a CSGMDL container. Format reference (community reverse
# engineering): https://github.com/krakow10/rbx_mesh — ``union_graphics``.
#
# Layout summary (little-endian throughout):
#   bytes 0..9    "CSGMDL" + u32 version, XOR-obfuscated like the rest
#   bytes 10..41  32-byte hash
#   u32 vertex_count, u32 vertex_stride (84 in v2)
#   vertex_count × Vertex {
#       f32×3 position, f32×3 normal, u8×4 color, u32 normal_id,
#       f32×2 uv, f32×3 tangent, 16 bytes padding
#   }
#   u32 index_count, then index_count × u32 vertex indices
#
# The whole blob is XORed with a 31-byte noise cycle (offset 0 of the blob ==
# offset 0 of the cycle, i.e. the first ten bytes decode to the ASCII magic).
# ---------------------------------------------------------------------------

_CSGMDL_XOR_CYCLE = bytes(
    (86, 46, 110, 88, 49, 32, 48, 4, 52, 105, 12, 119, 12, 1, 94, 0,
     26, 96, 55, 105, 29, 82, 43, 7, 79, 36, 89, 101, 83, 4, 122)
)
_CSGMDL_MAGIC = b"CSGMDL"
_CSGMDL2_VERTEX_STRIDE = 84
_CSGMDL_MAX_VERTICES = 4_000_000


def _deobfuscate_csgmdl(blob: bytes) -> bytes:
    cycle = _CSGMDL_XOR_CYCLE
    return bytes(b ^ cycle[i % 31] for i, b in enumerate(blob))


def _parse_csgmdl(blob: bytes) -> Optional[dict]:
    """Parse a CSGMDL union render-mesh blob into a filemesh-style dict.

    Returns None when the blob is not a recognised/supported CSGMDL version.
    """
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 54:
        return None
    if len(blob) > 256 * 1024 * 1024:  # sanity: 256 MiB
        return None
    plain = _deobfuscate_csgmdl(bytes(blob))
    if plain[:6] != _CSGMDL_MAGIC:
        return None
    version = struct.unpack_from("<I", plain, 6)[0]
    if version != 2:
        # v4 shares the v2 vertex layout but is unobserved in the wild here;
        # v5 (deinterleaved, state-machine indices) and CSGK (asset reference)
        # are different formats — bail rather than guess.
        return None

    reader = _Reader(plain, 10)
    reader.read(32)  # content hash
    vertex_count = reader.u32le()
    vertex_stride = reader.u32le()
    if vertex_count == 0 or vertex_count > _CSGMDL_MAX_VERTICES:
        return None
    if vertex_stride < _CSGMDL2_VERTEX_STRIDE:
        return None
    if reader.remaining() < vertex_count * vertex_stride + 4:
        return None

    positions: List[Tuple[float, float, float]] = []
    normals: List[Tuple[float, float, float]] = []
    colors: List[Tuple[float, float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    if vertex_stride % 4 == 0:
        try:
            import numpy as np  # bundled with Blender

            block = reader.read(vertex_count * vertex_stride)
            floats = np.frombuffer(block, dtype="<f4").reshape(vertex_count, vertex_stride // 4)
            positions = [tuple(row) for row in floats[:, 0:3].tolist()]
            normals = [tuple(row) for row in floats[:, 3:6].tolist()]
            uvs = [tuple(row) for row in floats[:, 8:10].tolist()]
            raw = np.frombuffer(block, dtype=np.uint8).reshape(vertex_count, vertex_stride)
            colors = [
                tuple(row)
                for row in (raw[:, 24:28].astype(np.float32) * (1.0 / 255.0)).tolist()
            ]

            index_count = reader.u32le()
            if index_count % 3 != 0 or index_count > 3 * _CSGMDL_MAX_VERTICES:
                return None
            if reader.remaining() < index_count * 4:
                return None
            indices = np.frombuffer(reader.read(index_count * 4), dtype="<u4")
            if indices.size and int(indices.max()) >= vertex_count:
                return None
            faces = [
                tuple(int(x) for x in row)
                for row in indices.reshape(-1, 3).tolist()
            ]
            if not faces:
                return None
            return {
                "positions": positions,
                "normals": normals,
                "uvs": uvs,
                "colors": colors,
                "faces": faces,
            }
        except ImportError:
            pass

    for _ in range(vertex_count):
        vertex = reader.read(vertex_stride)
        px, py, pz, nx, ny, nz = struct.unpack_from("<6f", vertex, 0)
        positions.append((px, py, pz))
        normals.append((nx, ny, nz))
        cr, cg, cb, ca = vertex[24], vertex[25], vertex[26], vertex[27]
        colors.append((cr / 255.0, cg / 255.0, cb / 255.0, ca / 255.0))
        # u32 normal_id at 28 (dominant axis hint) — not needed downstream.
        u, v = struct.unpack_from("<2f", vertex, 32)
        uvs.append((u, v))
        # f32×3 tangent at 40 + 16 bytes padding at 52.

    index_count = reader.u32le()
    if index_count % 3 != 0 or index_count > 3 * _CSGMDL_MAX_VERTICES:
        return None
    if reader.remaining() < index_count * 4:
        return None
    indices = struct.unpack_from(f"<{index_count}I", reader.read(index_count * 4), 0)
    faces = []
    for i in range(0, index_count, 3):
        a, b, c = indices[i], indices[i + 1], indices[i + 2]
        if a >= vertex_count or b >= vertex_count or c >= vertex_count:
            return None
        faces.append((a, b, c))
    if not faces:
        return None

    return {
        "positions": positions,
        "normals": normals,
        "uvs": uvs,
        "colors": colors,
        "faces": faces,
    }


def _extract_union_mesh(inst: _Instance) -> Optional[dict]:
    """Resolve a UnionOperation's embedded render mesh, if present.

    Modern files carry it in the ``MeshData2`` SharedString; very old files
    carried an unobfuscated CSGMDL blob directly in the ``MeshData`` string.
    """
    candidates = []
    for prop_name in ("MeshData2", "MeshData", "SolidMeshHolder"):
        value = inst.props.get(prop_name)
        if isinstance(value, (bytes, bytearray)):
            candidates.append(value)
    for blob in candidates:
        mesh = _parse_csgmdl(blob)
        if mesh is not None:
            return mesh
    return None


def _find_descendant_of_class(inst: _Instance, class_name: str) -> Optional[_Instance]:
    stack = list(inst.children)
    while stack:
        node = stack.pop()
        if node.class_name == class_name:
            return node
        stack.extend(node.children)
    return None


def _find_descendants_of_class(inst: _Instance, class_name: str) -> List[_Instance]:
    """Every descendant of ``class_name`` in document (layering) order.

    Roblox renders multiple Texture / SurfaceAppearance children as stacked
    layers, bottom first, so the order of this list IS the composite order.
    """
    found = []

    def walk(node):
        for child in node.children:
            if child.class_name == class_name:
                found.append(child)
            walk(child)

    walk(inst)
    return found


def _cf(value):
    # CFrames are decoded as 12-component tuples already.
    if isinstance(value, (list, tuple)) and len(value) == 12:
        return [float(c) for c in value]
    return None


def _cf_multiply(a, b):
    """Compose two 12-component CFrames (pos xyz + row-major 3x3): a * b."""
    if a is None or b is None:
        return None
    ax, ay, az = a[0], a[1], a[2]
    ar = a[3:12]
    bx, by, bz = b[0], b[1], b[2]
    br = b[3:12]
    # rot = ar * br (row-major 3x3 multiply)
    rot = [
        ar[0] * br[0] + ar[1] * br[3] + ar[2] * br[6],
        ar[0] * br[1] + ar[1] * br[4] + ar[2] * br[7],
        ar[0] * br[2] + ar[1] * br[5] + ar[2] * br[8],
        ar[3] * br[0] + ar[4] * br[3] + ar[5] * br[6],
        ar[3] * br[1] + ar[4] * br[4] + ar[5] * br[7],
        ar[3] * br[2] + ar[4] * br[5] + ar[5] * br[8],
        ar[6] * br[0] + ar[7] * br[3] + ar[8] * br[6],
        ar[6] * br[1] + ar[7] * br[4] + ar[8] * br[7],
        ar[6] * br[2] + ar[7] * br[5] + ar[8] * br[8],
    ]
    # pos = a.pos + a.rot * b.pos
    px = ax + ar[0] * bx + ar[1] * by + ar[2] * bz
    py = ay + ar[3] * bx + ar[4] * by + ar[5] * bz
    pz = az + ar[6] * bx + ar[7] * by + ar[8] * bz
    return [px, py, pz] + rot


def _vec3(value):
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return [float(c) for c in value]
    return None


def _content_to_str(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        # SharedString-stored Content values resolve to raw payload bytes.
        try:
            return bytes(value).decode("utf-8").rstrip("\0")
        except UnicodeDecodeError:
            return ""
    return ""


def _content_or_asset_id(inst: _Instance, *names) -> str:
    """Read a texture-ish prop stored as Content/SharedString, or a raw
    int64 asset id in old saves (CharacterMesh-style encoding)."""
    value = _first_prop(inst, *names)
    if isinstance(value, (int, float)) and value > 0:
        return f"rbxassetid://{int(value)}"
    return _content_to_str(value)


_BEAM_PARENT_CLASSES = (
    "Part", "MeshPart", "WedgePart", "CornerWedgePart", "UnionOperation",
)


def rbxm_beams(instances, roots, workspace_refs=None) -> List[dict]:
    """Collect enabled Beam instances with resolved attachment endpoints.

    Attachment CFrames are part-relative, so each endpoint records the
    attachment CFrame plus its ancestor BasePart's world CFrame; the
    importer multiplies them (a place file stores BasePart.CFrame in
    world space).  ColorSequence/NumberSequence property types are not
    decoded by the binary reader yet, so colour gradients and
    per-segment transparency are not represented.
    """
    if workspace_refs is None:
        workspace_refs = _workspace_descendant_refs(instances, roots)

    def endpoint(ref):
        att = instances.get(ref) if isinstance(ref, int) else None
        if att is None or att.class_name != "Attachment":
            return None
        att_cf = _cf(_first_prop(att, "CFrame"))
        if att_cf is None:
            return None
        parent = instances.get(att.parent) if att.parent is not None else None
        part_cf = None
        while parent is not None:
            if parent.class_name in _BEAM_PARENT_CLASSES:
                part_cf = _cf(_first_prop(parent, "CFrame"))
                break
            parent = instances.get(parent.parent) if parent.parent is not None else None
        return {"cf": att_cf, "part_cf": part_cf}

    def prop_float(inst, prop, default):
        value = _first_prop(inst, prop)
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def clean_seq(value):
        """A parsed sequence, or None when its keypoints are implausible.

        Sequence serialization is version-dependent; a layout that does not
        match the file produces denormal garbage, which must never reach
        the importer.  Plausible keypoints have ascending times in [0, 1]
        and values/envelopes in [0, 1].
        """
        if not isinstance(value, list) or not value:
            return None
        try:
            previous = -1.0
            for keypoint in value:
                t = float(keypoint[0])
                v = keypoint[1]
                e = float(keypoint[2]) if len(keypoint) > 2 else 0.0
                if not (-1e-3 <= t <= 1.0 + 1e-3) or t < previous - 1e-6:
                    return None
                if isinstance(v, (list, tuple)):
                    if len(v) != 3 or any(not (-1e-3 <= float(c) <= 1.0 + 1e-3) for c in v):
                        return None
                elif not (-1e-3 <= float(v) <= 1.0 + 1e-3):
                    return None
                if not (-1e-3 <= e <= 1.0 + 1e-3):
                    return None
                previous = t
            return value
        except (TypeError, ValueError, IndexError):
            return None

    beams = []
    for inst in instances.values():
        if inst.class_name != "Beam":
            continue
        if workspace_refs is not None and inst.referent not in workspace_refs:
            continue
        try:
            enabled = bool(_first_prop(inst, "Enabled") or False)
        except (TypeError, ValueError):
            enabled = False
        if not enabled:
            continue
        a0 = endpoint(_first_prop(inst, "Attachment0"))
        a1 = endpoint(_first_prop(inst, "Attachment1"))
        if a0 is None or a1 is None:
            continue
        beams.append({
            "name": inst.name,
            "texture": _content_to_str(_first_prop(inst, "Texture")),
            "color_seq": clean_seq(_first_prop(inst, "Color")),
            "transparency_seq": clean_seq(_first_prop(inst, "Transparency")),
            "width0": prop_float(inst, "Width0", 0.1),
            "width1": prop_float(inst, "Width1", 0.1),
            "curve_size0": prop_float(inst, "CurveSize0", 0.0),
            "curve_size1": prop_float(inst, "CurveSize1", 0.0),
            "segments": max(3, min(64, int(prop_float(inst, "Segments", 10)))),
            "texture_length": prop_float(inst, "TextureLength", 1.0),
            "texture_mode": int(_first_prop(inst, "TextureMode") or 0),
            "texture_speed": prop_float(inst, "TextureSpeed", 0.0),
            "face_camera": bool(_first_prop(inst, "FaceCamera")),
            "light_emission": prop_float(inst, "LightEmission", 1.0),
            "light_influence": prop_float(inst, "LightInfluence", 0.0),
            "z_offset": prop_float(inst, "ZOffset", 0.0),
            "attachment0": a0,
            "attachment1": a1,
        })
    return beams


def _build_wrap_layer(mesh_part: _Instance) -> Optional[Dict[str, Any]]:
    wrap = _find_descendant_of_class(mesh_part, "WrapLayer")
    if wrap is None:
        return None
    data: Dict[str, Any] = {
        "name": wrap.name,
        "enabled": bool(_first_prop(wrap, "Enabled") or False),
    }
    auto_skin = _first_prop(wrap, "AutoSkin")
    if auto_skin is not None:
        data["auto_skin"] = str(auto_skin)
    for key, prop in (
        ("reference_mesh_id", "ReferenceMeshId"),
        ("cage_mesh_id", "CageMeshId"),
        ("temporary_reference_id", "TemporaryReferenceId"),
        ("temporary_cage_mesh_id", "TemporaryCageMeshId"),
        ("hsr_asset_id", "HSRAssetId"),
    ):
        text = _content_to_str(_first_prop(wrap, prop))
        if text:
            data[key] = text
    for key, prop in (
        ("reference_origin", "ReferenceOrigin"),
        ("cage_origin", "CageOrigin"),
        ("bind_offset", "BindOffset"),
        ("import_origin", "ImportOrigin"),
    ):
        cf = _cf(_first_prop(wrap, prop))
        if cf:
            data[key] = cf
    for key, prop in (("order", "Order"), ("puffiness", "Puffiness"), ("shrink_factor", "ShrinkFactor")):
        num = _first_prop(wrap, prop)
        if isinstance(num, (int, float)):
            data[key] = num
    return data


def _build_wrap_target(mesh_part: _Instance) -> Optional[Dict[str, Any]]:
    wrap = _find_descendant_of_class(mesh_part, "WrapTarget")
    if wrap is None:
        return None
    data: Dict[str, Any] = {"name": wrap.name}
    for key, prop in (
        ("cage_mesh_id", "CageMeshId"),
        ("temporary_cage_mesh_id", "TemporaryCageMeshId"),
        ("hsr_asset_id", "HSRAssetId"),
    ):
        text = _content_to_str(_first_prop(wrap, prop))
        if text:
            data[key] = text
    for key, prop in (("cage_origin", "CageOrigin"), ("import_origin", "ImportOrigin")):
        cf = _cf(_first_prop(wrap, prop))
        if cf:
            data[key] = cf
    stiffness = _first_prop(wrap, "Stiffness")
    if isinstance(stiffness, (int, float)):
        data["stiffness"] = stiffness
    return data


def _build_character_mesh(character_mesh: _Instance) -> Optional[Dict[str, Any]]:
    """CharacterMesh metadata: the modern replacement for SpecialMesh on
    classic R6 body parts.

    Newer saves carry the mesh/textures as Content-typed properties
    (``MeshContent`` / ``BaseTextureContent`` / ``OverlayTextureContent``);
    legacy saves use the int64 asset-id twins (``MeshId`` etc.).
    """
    data: Dict[str, Any] = {}
    mesh_id = _content_to_str(_first_prop(character_mesh, "MeshContent"))
    if not mesh_id:
        legacy_id = _first_prop(character_mesh, "MeshId")
        if isinstance(legacy_id, (int, float)) and legacy_id > 0:
            mesh_id = f"rbxassetid://{int(legacy_id)}"
        else:
            mesh_id = _content_to_str(legacy_id)
    if mesh_id:
        data["mesh_id"] = mesh_id
    body_part = _first_prop(character_mesh, "BodyPart")
    if isinstance(body_part, (int, float)):
        data["body_part"] = int(body_part)
    overlay = _content_to_str(_first_prop(character_mesh, "OverlayTextureContent"))
    if not overlay:
        overlay_id = _first_prop(character_mesh, "OverlayTextureId")
        if isinstance(overlay_id, (int, float)) and overlay_id > 0:
            overlay = f"rbxassetid://{int(overlay_id)}"
    if overlay:
        data["overlay_texture_id"] = overlay
    base = _content_to_str(_first_prop(character_mesh, "BaseTextureContent"))
    if not base:
        base_id = _first_prop(character_mesh, "BaseTextureId")
        if isinstance(base_id, (int, float)) and base_id > 0:
            base = f"rbxassetid://{int(base_id)}"
    if base:
        data["base_texture_id"] = base
    return data or None


def _volume(size) -> float:
    if not size or len(size) < 3:
        return 0.0
    return float(size[0]) * float(size[1]) * float(size[2])


def _workspace_descendant_refs(instances, roots):
    """Referents reachable from a Workspace root, or None for plain models.

    Places keep geometry under Workspace; everything else (ReplicatedStorage,
    ServerStorage, StarterPack, Lighting) is runtime/setup content that must
    not become scene objects.  A .rbxm model has no Workspace root, so all of
    its instances are candidates and this returns None to disable filtering.
    """
    workspace_root = None
    for instance in (roots or {}).values():
        if instance.class_name == "Workspace":
            workspace_root = instance
            break
    if workspace_root is None:
        return None
    refs = set()
    stack = [workspace_root]
    while stack:
        instance = stack.pop()
        refs.add(instance.referent)
        stack.extend(instance.children)
    return refs


def rbxm_to_part_aux(data: Optional[bytes] = None, *, instances=None, roots=None) -> List[Dict[str, Any]]:
    """Parse a .rbxm buffer into the partAux entry schema used by creation.py.

    Returns one entry per BasePart (MeshPart and primitive parts). MeshParts
    additionally carry mesh_id / mesh_size / wrap metadata.
    """
    if instances is None:
        if data is None:
            raise ValueError("rbxm data or parsed instances are required")
        instances, roots = _parse_chunks(data)
    primary_part_refs = {
        primary_ref
        for instance in instances.values()
        if instance.class_name == "Model"
        for primary_ref in [_first_prop(instance, "PrimaryPart")]
        if isinstance(primary_ref, int)
    }

    # MaterialVariants live as children of MaterialService; parts reference
    # them by NAME through BasePart.MaterialVariant.  Collect them first so
    # each part entry can carry its resolved variant maps.
    material_variants = {}
    for instance in instances.values():
        if instance.class_name != "MaterialVariant":
            continue
        base_material = _first_prop(instance, "BaseMaterial")
        if not isinstance(base_material, (int, float)):
            continue
        material_variants[instance.name] = {
            "base_material": int(base_material),
            "color_map": _content_to_str(_first_prop(instance, "ColorMap")) or "",
            "normal_map": _content_to_str(_first_prop(instance, "NormalMap")) or "",
            "metalness_map": _content_to_str(_first_prop(instance, "MetalnessMap")) or "",
            "roughness_map": _content_to_str(_first_prop(instance, "RoughnessMap")) or "",
            "studs_per_tile": _first_prop(instance, "StudsPerTile"),
        }

    entries: List[dict] = []
    counter = 0
    workspace_refs = _workspace_descendant_refs(instances, roots)

    # R6 character detection: any plain Part under a Humanoid model whose
    # name is a classic body part gets its client-side fonts mesh.  The part
    # itself has no mesh data in the file (the renderer substitutes by name
    # at runtime).
    r6_classic_meshes: Dict[int, str] = {}
    for instance in instances.values():
        if instance.class_name != "Model":
            continue
        if _find_descendant_of_class(instance, "Humanoid") is None:
            continue
        stack = list(instance.children)
        while stack:
            child = stack.pop()
            if (
                child.class_name == "Part"
                and _find_descendant_of_class(child, "SpecialMesh") is None
            ):
                mesh_uri = _R6_CLASSIC_BODY_MESHES.get(
                    (child.name or "").strip().lower()
                )
                if mesh_uri:
                    r6_classic_meshes[child.referent] = mesh_uri
            stack.extend(child.children)

    # CharacterMesh registry: instances live at the model root and target
    # their body part through BodyPart.  Prefer the entry carrying an
    # explicit MeshId when duplicates exist.
    character_meshes: Dict[int, dict] = {}
    for instance in instances.values():
        if instance.class_name != "CharacterMesh":
            continue
        body_part = _first_prop(instance, "BodyPart")
        if not isinstance(body_part, (int, float)):
            continue
        cm = _build_character_mesh(instance) or {}
        cm["body_part"] = int(body_part)
        existing = character_meshes.get(int(body_part))
        if existing is None or (cm.get("mesh_id") and not existing.get("mesh_id")):
            character_meshes[int(body_part)] = cm

    for inst in instances.values():
        if workspace_refs is not None and inst.referent not in workspace_refs:
            continue
        if inst.class_name not in ("MeshPart", "Part", "WedgePart", "CornerWedgePart", "UnionOperation"):
            continue
        counter += 1

        size = _vec3(_first_prop(inst, "size", "Size"))
        part_cf = _cf(_first_prop(inst, "CFrame"))
        # Accessory Handle parts are generically named "Handle" — use the
        # parent Accessory name instead so duplicate-named parts get unique,
        # meaningful names (e.g. "White Bunny Ears" instead of "Handle").
        obj_name = inst.name
        if inst.parent is not None:
            parent_inst = instances.get(inst.parent)
            if parent_inst is not None and parent_inst.class_name == "Accessory":
                obj_name = parent_inst.name
        entry = {
            "idx": counter,
            "inst_ref": inst.referent,
            "parent_ref": inst.parent,
            "name": obj_name,
            # Exact Roblox class (MeshPart / Part / WedgePart / UnionOperation /
            # CornerWedgePart) — drives the Studio-style ".class" object suffix.
            "class_name": inst.class_name,
            "dims_fp": size,
            "vol_fp": _volume(size),
            "part_size": size,
            "part_cf": part_cf,
        }
        if inst.referent in primary_part_refs:
            entry["is_primary_part"] = True

        # CharacterMesh replaces SpecialMesh on classic R6 body parts.  The
        # instance lives either as a child of the part or (the common layout,
        # per R6CharacterAssembler.AssembleModel) at the model root, keyed by
        # BodyPart.  An explicit MeshId wins; without one the part falls back
        # to the classic fonts mesh for that BodyPart.
        character_mesh = _find_descendant_of_class(inst, "CharacterMesh")
        character_mesh_data = None
        if inst.class_name == "Part" and character_mesh is not None:
            character_mesh_data = _build_character_mesh(character_mesh)
        elif inst.class_name == "Part":
            name_key = (obj_name or "").strip().lower()
            for body_part, part_name in _R6_BODY_PART_NAMES.items():
                if name_key == part_name:
                    character_mesh_data = character_meshes.get(int(body_part))
                    break
        if inst.class_name == "Part" and character_mesh_data is not None:
            entry["character_mesh"] = dict(character_mesh_data)
            mesh_id = character_mesh_data.get("mesh_id")
            body_part = character_mesh_data.get("body_part")
            if not mesh_id and isinstance(body_part, int):
                mesh_id = _R6_BODY_PART_MESHES.get(body_part)
            if mesh_id:
                entry["mesh_class"] = "MeshPart"
                entry["mesh_id"] = mesh_id
            # The part renders the CharacterMesh, never its primitive shape.
            entry.pop("shape", None)

        if inst.class_name == "MeshPart":
            entry["mesh_class"] = "MeshPart"
            mesh_id = _content_to_str(_first_prop(inst, "MeshId"))
            if mesh_id:
                entry["mesh_id"] = mesh_id
            mesh_size = _vec3(_first_prop(inst, "MeshSize"))
            if mesh_size:
                entry["mesh_size"] = mesh_size
            # Skinned meshes are declared by MeshPart.HasSkinnedMesh.
            # creation.py uses this flag to mark the joint's bone as a
            # Blender deform bone (armature-modifier skinning silently does
            # nothing otherwise).
            entry["has_skinning"] = bool(_first_prop(inst, "HasSkinnedMesh"))
            wrap_layer = _build_wrap_layer(inst)
            if wrap_layer:
                entry["wrap_layer"] = wrap_layer
            wrap_target = _build_wrap_target(inst)
            if wrap_target:
                entry["wrap_target"] = wrap_target
        elif inst.class_name == "WedgePart":
            entry["shape"] = "wedge"
        elif inst.class_name == "CornerWedgePart":
            entry["shape"] = "corner_wedge"
        elif inst.class_name == "UnionOperation":
            # Union/CSG part: the render mesh is embedded in the file (the
            # MeshData2 SharedString) rather than referenced by asset id.
            union_mesh = _extract_union_mesh(inst)
            if union_mesh is not None:
                entry["union_mesh"] = union_mesh
                entry["mesh_class"] = "UnionOperation"
            else:
                # AssetId-referenced CSG assets and unsupported CSGMDL
                # versions: no offline-decodable mesh is available.
                entry["union_unsupported"] = True
        elif inst.class_name == "Part" and _find_descendant_of_class(inst, "SpecialMesh") is None and character_mesh is None and character_mesh_data is None:
            # Plain primitive Part (no SpecialMesh child) → block/ball/cylinder/etc.
            shape_val = _first_prop(inst, "shape", "Shape")
            if isinstance(shape_val, (int, float)):
                shape_int = int(shape_val)
                entry["shape"] = {0: "ball", 1: "block", 2: "cylinder",
                                  3: "wedge", 4: "corner_wedge"}.get(shape_int, "block")
            else:
                entry["shape"] = "block"
        else:
            # Classic head/torso: a Part with a SpecialMesh child. The MeshId may
            # be explicit (FileMesh type), or empty for builtin parametric meshes
            # (MeshType Head/Torso), which resolve to rbxasset:// builtins.
            special = _find_descendant_of_class(inst, "SpecialMesh")
            if special is not None:
                mesh_id = _content_to_str(_first_prop(special, "MeshId"))
                is_builtin_mesh_type = False
                if not mesh_id:
                    mesh_type = _first_prop(special, "MeshType")
                    mesh_id = _BUILTIN_MESHTYPE_IDS.get(
                        int(mesh_type) if isinstance(mesh_type, (int, float)) else -1
                    ) or ""
                    is_builtin_mesh_type = bool(mesh_id)
                if mesh_id:
                    entry["mesh_class"] = "MeshPart"
                    entry["mesh_id"] = mesh_id
                    mesh_scale = _vec3(_first_prop(special, "Scale"))
                    part_size = entry.get("part_size") or [1.0, 1.0, 1.0]
                    if mesh_scale and not is_builtin_mesh_type:
                        # FileMesh SpecialMesh: rendered = raw ⊙ Scale.
                        # _compute_mesh_scale divides part/mesh, so store
                        # mesh_size = part / Scale to get a multiplicative
                        # quotient.
                        entry["mesh_size"] = [
                            part_size[i] / mesh_scale[i] if abs(mesh_scale[i]) > 1e-8 else part_size[i]
                            for i in range(3)
                        ]
                    else:
                        # Builtin parametric MeshTypes (Head/Torso) render at
                        # the raw mesh size; SpecialMesh.Scale is ignored.
                        # mesh_size = part_size makes the scale quotient 1.
                        entry["mesh_size"] = list(part_size)
                # Classic accessories store their texture on the SpecialMesh,
                # not the Part.
                special_texture = _content_or_asset_id(special, "TextureId", "TextureID")
                if special_texture:
                    entry["texture_id"] = special_texture

        # Substitute the classic R6 body mesh for plain Parts that carry no
        # explicit mesh in the file.  MeshSize is left unset so the importer
        # infers it from the FileMesh bounds and scales to the part's Size.
        classic_mesh_id = r6_classic_meshes.get(inst.referent)
        if classic_mesh_id and not entry.get("mesh_id"):
            entry["mesh_class"] = "MeshPart"
            entry["mesh_id"] = classic_mesh_id
            entry.pop("shape", None)

        # Base color (Color3uint8) applies to every part.
        color = _first_prop(inst, "Color3uint8")
        if color is not None:
            entry["color"] = [c / 255.0 for c in color]

        # Preserve Roblox's material enum.  It is independent of Color3 and
        # must survive into Blender's Principled material conversion; otherwise
        # every untextured Part silently becomes Principled's generic default.
        material = _first_prop(inst, "Material")
        if isinstance(material, (int, float)):
            entry["material"] = int(material)

        # MaterialVariant is a NAME referencing a MaterialService child; its
        # maps swap the material's colour texture while keeping the tint.
        # Modern Studio saves the reference as ``MaterialVariantSerialized``
        # (a plain name string for these files); very old saves may hold a
        # raw referent instead of the name.
        material_variant = _first_prop(inst, "MaterialVariant", "MaterialVariantSerialized")
        if isinstance(material_variant, (int, float)):
            ref_inst = instances.get(int(material_variant))
            if ref_inst is not None:
                material_variant = ref_inst.name
        if material_variant is not None and str(material_variant):
            variant_name = str(material_variant)
            entry["material_variant"] = variant_name
            variant_data = material_variants.get(variant_name)
            if variant_data:
                entry["material_variant_data"] = variant_data
                # A MaterialVariant completely replaces the part's material
                # look — the part's own Enum.Material is ignored.  Adopt the
                # variant's BaseMaterial so the built-in pipeline (tint,
                # material UVs, hydration) applies to the variant instead.
                base = int(variant_data.get("base_material", 0)) or None
                if base is not None:
                    entry["material"] = base

        # Roblox stores opacity inversely: 0 is opaque and 1 is invisible.
        # Preserve it on every BasePart so Blender material construction can
        # apply the corresponding alpha after textures and clothing are bound.
        transparency = _first_prop(inst, "Transparency")
        if isinstance(transparency, (int, float)):
            entry["transparency"] = max(0.0, min(1.0, float(transparency)))

        # MeshPart texture id (most are empty on classic avatars).
        texture_id = _content_or_asset_id(inst, "TextureID", "TextureId")
        if texture_id:
            entry["texture_id"] = texture_id

        # Classic places render walls through ``Texture`` CHILDREN.  Unlike
        # the MeshPart.TextureID tint (which replaces the material look),
        # the child instance is a surface texture drawn OVER the part, so
        # Roblox keeps the part's material underneath.  A part can carry
        # SEVERAL children, stacked bottom-first; dropping all but the
        # first leaves the composite missing layers.  Preserve the source
        # separately so the material builder can composite instead of
        # replacing — collapsing it into texture_id both drops the material
        # and binds the image alpha as material transparency.
        texture_children = _find_descendants_of_class(inst, "Texture")
        texture_instances = []
        for texture_child in texture_children:
            child_texture = _content_or_asset_id(
                texture_child, "Texture", "TextureId", "TextureID"
            )
            if not child_texture:
                continue
            instance_data: Dict[str, Any] = {"texture": child_texture}
            # Roblox draws a Texture child on ONE surface; the default is
            # Front (NormalId 5).  The material builder splits primitive
            # meshes per surface so the instance never bleeds across faces.
            face = _first_prop(texture_child, "Face")
            if isinstance(face, (int, float)):
                instance_data["face"] = int(face)
            else:
                instance_data["face"] = 5
            # Modern Texture instances tint the applied image by their
            # Color3.  Color3 props serialize as Color3uint8 (0-255),
            # but accept a raw float Color3 for exotic files.
            tint = _first_prop(texture_child, "Color3uint8", "Color3")
            if tint is not None:
                try:
                    tint = [float(c) for c in tint[:3]]
                    if any(c > 1.0 for c in tint):
                        tint = [c / 255.0 for c in tint]
                    instance_data["color"] = tint
                except (TypeError, ValueError, IndexError):
                    pass
            try:
                studs = float(_first_prop(texture_child, "StudsPerTileU") or 0.0)
            except (TypeError, ValueError):
                studs = 0.0
            if studs > 0.0:
                instance_data["studs_per_tile"] = studs
            try:
                child_transparency = float(
                    _first_prop(texture_child, "Transparency") or 0.0
                )
            except (TypeError, ValueError):
                child_transparency = 0.0
            instance_data["transparency"] = max(0.0, min(1.0, child_transparency))
            texture_instances.append(instance_data)
        if texture_instances:
            entry["texture_instances"] = texture_instances
            # Singular key kept for callers/tests built around one layer.
            entry["texture_instance"] = texture_instances[0]

        # SurfaceAppearance (PBR color/normal/roughness/metalness maps).
        # Multiple children stack like Texture layers, bottom-first.
        surface_children = _find_descendants_of_class(inst, "SurfaceAppearance")
        surface_appearances = []
        for surface in surface_children:
            alpha_mode = _first_prop(surface, "AlphaMode")
            surface_appearances.append({
                "color_map": _content_to_str(_first_prop(surface, "ColorMap")),
                "normal_map": _content_to_str(_first_prop(surface, "NormalMap")),
                "roughness_map": _content_to_str(_first_prop(surface, "RoughnessMap")),
                "metalness_map": _content_to_str(_first_prop(surface, "MetalnessMap")),
                "texture_pack": _content_to_str(_first_prop(surface, "TexturePack")),
                # 0=Overlay (alpha reveals part color), 1=Transparency
                # (alpha cuts against the world — foliage, fences).
                "alpha_mode": (
                    int(alpha_mode) if isinstance(alpha_mode, (int, float)) else 0
                ),
            })
        if surface_appearances:
            entry["surface_appearances"] = surface_appearances
            # Singular key kept for callers/tests built around one layer.
            entry["surface_appearance"] = surface_appearances[0]

        # Decals project a texture onto one named part face. Keep classic head
        # decals on their existing clothing-composite path and preserve all
        # other decals for the importer to create as projected overlay planes.
        decals = []
        for decal in _find_descendants_of_class(inst, "Decal"):
            decal_texture = _content_to_str(_first_prop(decal, "Texture"))
            if not decal_texture:
                continue
            decal_data: Dict[str, Any] = {"texture": decal_texture}
            face = _first_prop(decal, "Face")
            if isinstance(face, (int, float)):
                decal_data["face"] = int(face)
            tint = _first_prop(decal, "Color3uint8", "Color3")
            if tint is not None:
                try:
                    tint = [float(component) for component in tint[:3]]
                    decal_data["color"] = [
                        component / 255.0 if component > 1.0 else component
                        for component in tint
                    ]
                except (TypeError, ValueError, IndexError):
                    pass
            transparency = _first_prop(decal, "Transparency")
            if isinstance(transparency, (int, float)):
                decal_data["transparency"] = max(0.0, min(1.0, float(transparency)))
            decals.append(decal_data)
        if decals:
            entry["decals"] = decals
            if entry.get("name") == "Head" and _find_descendant_of_class(inst, "SpecialMesh") is None:
                # Classic heads render the face decal. Dynamic heads carry a
                # SpecialMesh and render ITS texture instead; the decal is
                # legacy data that must not become the face texture.  Keep
                # the whole instance (tint, transparency) so the clothing
                # composite can honor Decal.Transparency.
                entry["face_decal"] = dict(decals[0])

        # AvatarPartScaleType marker (drives HumanoidDescription scaling).
        scale_marker = _find_descendant_of_class(inst, "StringValue")
        if scale_marker is not None and getattr(scale_marker, "name", "") == "AvatarPartScaleType":
            scale_type = _first_prop(scale_marker, "Value")
            if isinstance(scale_type, str) and scale_type:
                entry["scale_type"] = scale_type.replace("Proportions", "")

        # Surface types: Smooth=0, Glue=1, Weld=2, Studs=3, Inlet=4,
        # Universal=5.  Stored per face so the material builder can assign
        # the correct atlas band to each face.
        # Face order in our generator: -Z, +Z, +Y, -Y, +X, -X
        _SURFACE_KEYS = ("BackSurface", "FrontSurface", "TopSurface",
                         "BottomSurface", "RightSurface", "LeftSurface")
        surface_types = []
        for surface_key in _SURFACE_KEYS:
            val = _first_prop(inst, surface_key)
            surface_types.append(int(val) if isinstance(val, (int, float)) else 0)
        entry["surface_types"] = surface_types
        # Also flag whether ANY face has a classic surface texture
        # (Glue=1, Studs=3, Inlet=4, Universal=5) for the material check.
        if any(st in (1, 3, 4, 5) for st in surface_types):
            entry["has_studs"] = True

        entries.append(entry)

    return entries


# Builtin FileMesh assets for parametric SpecialMesh types (empty MeshId).
# Head=0 and Torso=1 are the only MeshTypes backed by a real .mesh file.
_BUILTIN_MESHTYPE_IDS = {
    0: "rbxasset://fonts/head.mesh",
    1: "rbxasset://fonts/torso.mesh",
}

# Classic R6 characters keep their body meshes client-side: saved files
# store plain Parts (no MeshId or SpecialMesh) and the runtime swaps the
# fonts meshes in by part name.  Substitute the same meshes at parse time so
# imports render the classic character instead of primitive blocks.
_R6_CLASSIC_BODY_MESHES = {
    "head": "rbxasset://fonts/head.mesh",
    "torso": "rbxasset://fonts/torso.mesh",
    "left arm": "rbxasset://fonts/leftarm.mesh",
    "right arm": "rbxasset://fonts/rightarm.mesh",
    "left leg": "rbxasset://fonts/leftleg.mesh",
    "right leg": "rbxasset://fonts/rightleg.mesh",
}

# CharacterMesh.BodyPart enum -> classic fonts mesh (Head=0 ... RightLeg=5).
_R6_BODY_PART_MESHES = {
    0: "rbxasset://fonts/head.mesh",
    1: "rbxasset://fonts/torso.mesh",
    2: "rbxasset://fonts/leftarm.mesh",
    3: "rbxasset://fonts/rightarm.mesh",
    4: "rbxasset://fonts/leftleg.mesh",
    5: "rbxasset://fonts/rightleg.mesh",
}

# CharacterMesh.BodyPart enum -> classic part name (lowercased).  CharacterMesh
# instances sit at the model root and target their part via BodyPart
# (R6CharacterAssembler.AssembleModel), not via parenting.
_R6_BODY_PART_NAMES = {
    0: "head",
    1: "torso",
    2: "left arm",
    3: "right arm",
    4: "left leg",
    5: "right leg",
}


_JOINT_CLASSES = ("Motor6D", "Weld", "WeldConstraint", "AnimationConstraint", "RigidConstraint", "Snap")
_BASE_PART_CLASSES = ("MeshPart", "Part", "WedgePart", "CornerWedgePart", "UnionOperation")


def _joint_parts(joint: _Instance) -> Tuple[Optional[int], Optional[int]]:
    """Return (Part0, Part1) referents for a joint instance."""
    part0 = _first_prop(joint, "Part0", "Attachment0")
    part1 = _first_prop(joint, "Part1", "Attachment1")
    return (
        part0 if isinstance(part0, int) else None,
        part1 if isinstance(part1, int) else None,
    )


_ATTACHMENT_JOINT_CLASSES = ("AnimationConstraint", "RigidConstraint")


def _resolve_joint_endpoint(instances: Dict[int, _Instance], ref: Optional[int]):
    """Resolve a joint endpoint referent to (part_ref, cframe_in_part_space).

    Motor6D/Weld endpoints are parts directly (cframe None — the joint's
    C0/C1 supplies the offset). Constraint endpoints are Attachments: the
    parent part is the attachment's parent, and the attachment's own CFrame
    (part-space) plays the role of C0/C1.
    """
    if ref is None:
        return None, None
    inst = instances.get(ref)
    if inst is None:
        return None, None
    if inst.class_name in _BASE_PART_CLASSES:
        return ref, None
    if inst.class_name == "Attachment":
        parent_ref = inst.parent
        parent = instances.get(parent_ref) if parent_ref is not None else None
        if parent is not None and parent.class_name in _BASE_PART_CLASSES:
            return parent_ref, _cf(_first_prop(inst, "CFrame"))
    return None, None


def _build_rig_tree(instances: Dict[int, _Instance], roots: Dict[int, _Instance], allowed_parts=None) -> Optional[dict]:
    """Synthesize the ``meta_loaded['rig']`` joint tree from Motor6D/Weld joints.

    Mirrors the semantics produced by ``Components/RigPart.lua``:
      * ``transform``         = the part's world CFrame
      * ``jointtransform0``   = C0 (or C1 if the joint direction is reversed)
      * ``jointtransform1``   = C1 (or C0 if reversed)
      * ``jname``             = child part name
      * ``pname``             = parent part name (resolved later by reencode)
    """
    # Index joints that connect two parts we know about. Constraint joints
    # (AnimationConstraint/RigidConstraint) reference Attachments instead of
    # parts; resolve each endpoint to its parent part.
    allowed_parts = set(allowed_parts) if allowed_parts is not None else None
    joints = [i for i in instances.values() if i.class_name in _JOINT_CLASSES]

    # Build child->joint and parent->joint maps. A rig part is a "child" when it
    # is Part1 and Part0 is also a BasePart; direction may be reversed.
    # Process deform joints (Motor6D / AnimationConstraint) first so we know
    # which parts form the skeleton.  Rigid joints (Weld / RigidConstraint /
    # Snap) connect accessories TO the skeleton — the skeleton part is always
    # the parent.
    _DEFORM_JOINTS = ("Motor6D", "AnimationConstraint")
    _RIGID_JOINTS = ("Weld", "WeldConstraint", "RigidConstraint", "Snap")

    child_edges = []  # (parent_ref, child_ref, joint, parent_is_part0)
    rigid_edges = []  # deferred: orient after we know the skeleton parts

    for joint in joints:
        part0_ref, part1_ref = _joint_parts(joint)
        part0_ref, _ = _resolve_joint_endpoint(instances, part0_ref)
        part1_ref, _ = _resolve_joint_endpoint(instances, part1_ref)
        if part0_ref is None or part1_ref is None:
            continue
        if allowed_parts is not None and (part0_ref not in allowed_parts or part1_ref not in allowed_parts):
            continue
        if part0_ref == part1_ref:
            continue
        if joint.class_name in _DEFORM_JOINTS:
            child_edges.append((part0_ref, part1_ref, joint, True))
        else:
            rigid_edges.append((part0_ref, part1_ref, joint))

    # Now orient rigid edges: the skeleton part is the parent, the accessory is
    # the child.  A part is "in the skeleton" if it appears in any deform edge.
    skeleton_parts = set()
    for p, c, _, _ in child_edges:
        skeleton_parts.add(p)
        skeleton_parts.add(c)

    for part0_ref, part1_ref, joint in rigid_edges:
        p0_in_skel = part0_ref in skeleton_parts
        p1_in_skel = part1_ref in skeleton_parts
        if p0_in_skel and not p1_in_skel:
            # Part0 is skeleton, Part1 is accessory → Part0 is parent
            child_edges.append((part0_ref, part1_ref, joint, True))
        elif p1_in_skel and not p0_in_skel:
            # Part1 is skeleton, Part0 is accessory → Part1 is parent
            child_edges.append((part1_ref, part0_ref, joint, False))
        elif p0_in_skel and p1_in_skel:
            # Both are skeleton parts — treat Part0 as parent (convention)
            child_edges.append((part0_ref, part1_ref, joint, True))
        else:
            # Neither is skeleton (two accessories connected) — convention
            child_edges.append((part0_ref, part1_ref, joint, True))

    # Root = a part that is a parent (Part0) but never a child (Part1).
    child_refs = {c for _, c, _, _ in child_edges}
    parent_refs = {p for p, _, _, _ in child_edges}
    root_candidates = [r for r in parent_refs if r not in child_refs]

    # Build adjacency: parent_ref -> list of (child_ref, joint, parent_is_part0)
    adjacency: Dict[int, List[Tuple[int, _Instance, bool]]] = {}
    for parent_ref, child_ref, joint, pip0 in child_edges:
        adjacency.setdefault(parent_ref, []).append((child_ref, joint, pip0))

    def node_for(part_ref: int) -> dict:
        part = instances[part_ref]
        # Accessory Handle parts are generically named "Handle" — use the
        # parent Accessory name so duplicate-named parts get unique,
        # meaningful names in the rig tree.
        node_name = part.name
        if part.parent is not None:
            parent_inst = instances.get(part.parent)
            if parent_inst is not None and parent_inst.class_name == "Accessory":
                node_name = parent_inst.name
        # Collect WrapLayer child names so _derive_mesh_to_bone can map
        # them to the correct parent bone (e.g. "Flare" → "LowerTorso").
        wrap_layer_names = []
        for child_inst in (part.children or []):
            if child_inst is not None and child_inst.class_name == "WrapLayer":
                wl_name = getattr(child_inst, "name", None)
                if wl_name:
                    wrap_layer_names.append(wl_name)
        return {
            "jname": node_name,
            "pname": None,
            "inst_ref": part_ref,
            "transform": _cf(_first_prop(part, "CFrame")),
            "children": [],
            "aux": [],
            "auxTransform": [],
            "isDeformBone": False,
            "jointType": None,
            "wrapLayerNames": wrap_layer_names,
            # MeshPart.HasSkinnedMesh: only skinned-mesh joints should
            # become Blender deform bones.
            "hasSkinnedMesh": bool(_first_prop(part, "HasSkinnedMesh")),
        }

    def build(part_ref: int, parent_ref: Optional[int], joint: Optional[_Instance], parent_is_part0: bool) -> dict:
        node = node_for(part_ref)
        if parent_ref is not None and joint is not None:
            parent = instances.get(parent_ref)
            node["pname"] = parent.name if parent is not None else None
            node["jointType"] = joint.class_name
            if joint.class_name in _ATTACHMENT_JOINT_CLASSES:
                # Constraint joints: the attachments' part-space CFrames are
                # the joint transforms (Attachment0 on the parent side,
                # Attachment1 on the child side).
                att0_ref, att1_ref = _joint_parts(joint)
                att0 = instances.get(att0_ref) if isinstance(att0_ref, int) else None
                att1 = instances.get(att1_ref) if isinstance(att1_ref, int) else None
                c0 = _cf(_first_prop(att0, "CFrame")) if att0 is not None else None
                c1 = _cf(_first_prop(att1, "CFrame")) if att1 is not None else None
            else:
                c0 = _cf(_first_prop(joint, "C0"))
                c1 = _cf(_first_prop(joint, "C1"))
            if c0 is not None and c1 is not None:
                if parent_is_part0:
                    node["jointtransform0"] = c0
                    node["jointtransform1"] = c1
                else:
                    node["jointtransform0"] = c1
                    node["jointtransform1"] = c0
        for child_ref, child_joint, child_pip0 in adjacency.get(part_ref, []):
            node["children"].append(build(child_ref, part_ref, child_joint, child_pip0))
        return node

    # Attach skinned-mesh Bone instances. Bones are not joints — they parent
    # directly to a BasePart (or another Bone) and carry only a CFrame — so
    # they never appear in the joint graph above. Emit them as deform-bone
    # children of their owning part's node so the armature picks them up.
    # Match the obj deform path (RigPart.Encode): transform = WorldCFrame,
    # jointtransform0 = bone.CFrame (parent-relative local), jointtransform1 =
    # identity. load_rigbone computes o_trans = transform @ jointtransform1,
    # which with identity jointtransform1 lands the head at the world CFrame.
    _IDENTITY_CF = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

    def bone_node(bone_inst: _Instance, parent_world_cf) -> dict:
        local_cf = _cf(_first_prop(bone_inst, "CFrame"))
        world_cf = local_cf
        if local_cf is not None and parent_world_cf is not None:
            world_cf = _cf_multiply(parent_world_cf, local_cf)
        node = {
            "jname": bone_inst.name,
            "pname": None,
            "inst_ref": bone_inst.referent,
            "transform": world_cf,
            "children": [],
            "aux": [],
            "auxTransform": [],
            "isDeformBone": True,
            "jointType": "Bone",
            "wrapLayerNames": [],
            "jointtransform0": local_cf,
            "jointtransform1": list(_IDENTITY_CF),
        }
        for child_inst in (bone_inst.children or []):
            if child_inst is not None and child_inst.class_name == "Bone":
                child_node = bone_node(child_inst, world_cf)
                child_node["pname"] = bone_inst.name
                node["children"].append(child_node)
        return node

    def attach_bones(node: dict) -> None:
        inst_ref = node.get("inst_ref")
        part = instances.get(inst_ref) if isinstance(inst_ref, int) else None
        # Only BasePart nodes gain Bone children here — bone_node already
        # recursed nested Bones, so re-walking Bone nodes would duplicate them.
        if part is not None and part.class_name in _BASE_PART_CLASSES:
            part_world_cf = _cf(_first_prop(part, "CFrame"))
            for child_inst in (part.children or []):
                if child_inst is not None and child_inst.class_name == "Bone":
                    child_node = bone_node(child_inst, part_world_cf)
                    child_node["pname"] = node.get("jname")
                    node["children"].append(child_node)
        for child_node in node.get("children") or []:
            attach_bones(child_node)

    # Prefer a single root; otherwise pick the first candidate deterministically.
    if not root_candidates:
        # Cyclic or fully-connected graph; fall back to the first parent.
        root_candidates = sorted(parent_refs)
    if not root_candidates:
        # No joints at all. A bone-only skinned rig (MeshParts with Bone
        # children, no Motor6Ds) still needs a rig tree so the armature
        # picks the bones up — root it at the first part that owns Bones.
        bone_parents = {
            inst.parent
            for inst in instances.values()
            if inst.class_name == "Bone" and inst.parent is not None
        }
        part_refs = [
            ref
            for ref, inst in instances.items()
            if inst.class_name in _BASE_PART_CLASSES
            and (allowed_parts is None or ref in allowed_parts)
        ]
        if not part_refs:
            return None
        rooted = [ref for ref in part_refs if ref in bone_parents]
        root_ref = rooted[0] if rooted else part_refs[0]
        root = node_for(root_ref)
        root["pname"] = instances[root_ref].name
    else:
        root_ref = root_candidates[0]
        root = build(root_ref, None, None, True)
        root["pname"] = instances[root_ref].name

    attach_bones(root)

    return root


def _scene_rigs(instances: Dict[int, _Instance], roots: Dict[int, _Instance]) -> List[dict]:
    """Return one Motor6D rig per Humanoid-containing Model in a place."""
    rigs = []
    for model in instances.values():
        if model.class_name != "Model":
            continue
        stack = list(model.children)
        descendants = []
        has_humanoid = False
        while stack:
            child = stack.pop()
            descendants.append(child)
            if child.class_name == "Humanoid":
                has_humanoid = True
            stack.extend(child.children)
        if not has_humanoid:
            continue
        part_refs = {
            child.referent for child in descendants
            if child.class_name in _BASE_PART_CLASSES
        }
        rig = _build_rig_tree(instances, roots, part_refs)
        if rig is None:
            continue
        mapping = _derive_mesh_to_bone(rig)
        rigs.append({
            "name": model.name or "Rig",
            "model_ref": model.referent,
            "part_refs": sorted(part_refs),
            "rig": rig,
            "meshToBone": mapping,
        })
    return rigs


def _rig_components(instances: Dict[int, _Instance], roots: Dict[int, _Instance]) -> List[Set[int]]:
    """Connected components of the joint graph, as sets of BasePart referents.

    Joints (Motor6D/Weld/...) link two resolved parts; Bone instances belong
    to the component of the BasePart that owns them (possibly through other
    Bones).  A component qualifies as a rig when it has at least one joint
    edge or at least one Bone-owning part, matching the two trees that
    ``_build_rig_tree`` itself will synthesize.
    """
    edges: List[Tuple[int, int]] = []
    for joint in instances.values():
        if joint.class_name not in _JOINT_CLASSES:
            continue
        part0_ref, part1_ref = _joint_parts(joint)
        part0_ref, _ = _resolve_joint_endpoint(instances, part0_ref)
        part1_ref, _ = _resolve_joint_endpoint(instances, part1_ref)
        if part0_ref is not None and part1_ref is not None and part0_ref != part1_ref:
            edges.append((part0_ref, part1_ref))

    bone_owners: Set[int] = set()

    def owning_part(inst: _Instance) -> Optional[int]:
        ref = inst.parent
        seen = set()
        while ref is not None and ref not in seen:
            seen.add(ref)
            node = instances.get(ref)
            if node is None:
                return None
            if node.class_name in _BASE_PART_CLASSES:
                return ref
            ref = node.parent
        return None

    for inst in instances.values():
        if inst.class_name == "Bone":
            owner = owning_part(inst)
            if owner is not None:
                bone_owners.add(owner)

    parent: Dict[int, int] = {}

    def find(ref: int) -> int:
        root_ref = ref
        while parent.get(root_ref, root_ref) != root_ref:
            root_ref = parent[root_ref]
        while parent.get(ref, ref) != ref:
            next_ref = parent[ref]
            parent[ref] = root_ref
            ref = next_ref
        return root_ref

    def union(ref_a: int, ref_b: int) -> None:
        root_a, root_b = find(ref_a), find(ref_b)
        if root_a != root_b:
            parent[root_b] = root_a

    for ref_a, ref_b in edges:
        union(ref_a, ref_b)

    members: Dict[int, List[int]] = {}
    for part_ref in {ref for edge in edges for ref in edge} | bone_owners:
        members.setdefault(find(part_ref), []).append(part_ref)

    edge_counts: Dict[int, int] = {}
    for ref_a, ref_b in edges:
        root_ref = find(ref_a)
        edge_counts[root_ref] = edge_counts.get(root_ref, 0) + 1

    components: List[Set[int]] = []
    for part_refs in members.values():
        root_ref = find(part_refs[0])
        if edge_counts.get(root_ref, 0) == 0 and not (set(part_refs) & bone_owners):
            continue
        components.append(set(part_refs))
    return components


def _model_rigs(instances: Dict[int, _Instance], roots: Dict[int, _Instance]) -> List[dict]:
    """Per-rig entries for a MODEL (.rbxm) file.

    Places enumerate every Humanoid Model; a model file can instead pack
    several independent Motor6D/Bone assemblies (character packs, gadget
    sets).  ``_build_rig_tree`` over the whole file only roots the first
    joint-graph component, so without the component fallback below the
    importer would generate one armature no matter how many rigs the file
    holds.
    """
    rigs = _scene_rigs(instances, roots)
    covered: Set[int] = set()
    for rig in rigs:
        covered.update(rig.get("part_refs") or [])

    def model_name_for(part_ref: int) -> Optional[str]:
        ref = instances[part_ref].parent
        while ref is not None:
            node = instances.get(ref)
            if node is None:
                break
            if node.class_name == "Model":
                return node.name
            ref = node.parent
        return None

    for component in _rig_components(instances, roots):
        if not component or component <= covered:
            continue
        refs = sorted(component)
        rig = _build_rig_tree(instances, roots, refs)
        if rig is None:
            continue
        mapping = _derive_mesh_to_bone(rig)
        rigs.append({
            "name": model_name_for(refs[0]) or f"Rig {len(rigs) + 1}",
            "model_ref": None,
            "part_refs": refs,
            "rig": rig,
            "meshToBone": mapping,
        })
    return rigs


def _derive_mesh_to_bone(rig: Optional[dict]) -> Dict[str, str]:
    """Build an authoritative part-name -> bone-name map from the rig tree.

    Since the .rbxm preserves the actual Motor6D graph, each BasePart's bone is
    simply its own jname (the joint node shares the part's name). Emitting this
    lets creation.py pre-constrain meshes without any name-matching heuristics.

    Accessories attached via Weld / RigidConstraint (no Motor6D) do not create
    their own bones — they should follow their parent bone instead.
    """
    mapping: Dict[str, str] = {}
    if not rig:
        return mapping

    def walk(node: dict, parent_bone: Optional[str]) -> None:
        jname = node.get("jname")
        joint_type = node.get("jointType")
        # Motor6D and AnimationConstraint define deformable bones (the part IS
        # the bone).  Weld / RigidConstraint / Snap are rigid attachments — the
        # part should be constrained to its parent bone instead.
        is_deform = joint_type in ("Motor6D", "AnimationConstraint") or node.get("isDeformBone")
        bone = jname if is_deform else parent_bone
        if jname and bone:
            mapping[jname] = bone
            # Also map each WrapLayer child name to the same bone so the cage
            # fitter knows which body part cage to use for each clothing item.
            for wl_name in node.get("wrapLayerNames") or []:
                if wl_name:
                    mapping[wl_name] = bone
        for child in node.get("children") or []:
            walk(child, jname if is_deform else parent_bone)

    # Root is always a deform bone (HumanoidRootPart)
    root_bone = rig.get("jname") if isinstance(rig, dict) else None
    walk(rig, root_bone)
    return mapping


def parse_rbxm(data: bytes) -> Dict[str, Any]:
    """Parse a .rbxm buffer into a metadata dict mirroring the server export."""
    instances, roots = _parse_chunks(data)
    workspace_refs = _workspace_descendant_refs(instances, roots)
    meta = {
        "partAux": rbxm_to_part_aux(instances=instances, roots=roots),
        "source": "rbxm",
        # Kept deliberately small: enough to rebuild the supported scene
        # hierarchy without serializing arbitrary instance properties/scripts.
        "scene_nodes": [
            {
                "inst_ref": instance.referent,
                "parent_ref": instance.parent,
                "name": instance.name,
                "class_name": instance.class_name,
            }
            for instance in instances.values()
            if workspace_refs is None or instance.referent in workspace_refs
        ],
    }
    try:
        meta["beams"] = rbxm_beams(instances, roots, workspace_refs)
    except Exception:  # pragma: no cover - defensive
        meta["beams"] = []
    # Smooth terrain: keep the raw blobs; the importer decodes and meshes
    # them lazily (the grid can be several MB and millions of cells).
    terrain_instance = next(
        (instance for instance in instances.values() if instance.class_name == "Terrain"),
        None,
    )
    if terrain_instance is not None:
        grid_raw = _first_prop(terrain_instance, "SmoothGrid")
        if isinstance(grid_raw, str):
            grid: Any = grid_raw.encode("latin-1")
        elif isinstance(grid_raw, (bytes, bytearray)):
            grid = bytes(grid_raw)
        else:
            grid = None
        colors_raw = _first_prop(terrain_instance, "MaterialColors")
        if isinstance(colors_raw, str):
            colors: Any = colors_raw.encode("latin-1")
        elif isinstance(colors_raw, (bytes, bytearray)):
            colors = bytes(colors_raw)
        else:
            colors = None
        if grid:
            water_color = _first_prop(terrain_instance, "WaterColor") or [0.047, 0.329, 0.361]
            try:
                water_color = [float(c) for c in water_color[:3]]
            except (TypeError, ValueError):
                water_color = [0.047, 0.329, 0.361]
            meta["terrain"] = {
                "smoothgrid": bytes(grid),
                "colors": bytes(colors or b""),
                "water": {
                    "color": water_color,
                    "transparency": float(_first_prop(terrain_instance, "WaterTransparency") or 0.3),
                    "reflectance": float(_first_prop(terrain_instance, "WaterReflectance") or 1.0),
                },
            }

    scene_lights = []
    for instance in instances.values():
        if instance.class_name not in ("PointLight", "SpotLight", "SurfaceLight"):
            continue
        color = _first_prop(instance, "Color") or [1.0, 1.0, 1.0]
        scene_lights.append(
            {
                "class_name": instance.class_name,
                "name": instance.name,
                "parent_ref": instance.parent,
                "enabled": bool(_first_prop(instance, "Enabled")) if _first_prop(instance, "Enabled") is not None else True,
                "brightness": float(_first_prop(instance, "Brightness") or 1.0),
                "range": float(_first_prop(instance, "Range") or 16.0),
                "angle": float(_first_prop(instance, "Angle") or 45.0),
                "face": int(_first_prop(instance, "Face") or 5),
                "shadows": bool(_first_prop(instance, "Shadows")),
                "color": [float(component) for component in color[:3]],
            }
        )
    if scene_lights:
        meta["scene_lights"] = scene_lights

    # Lighting is a singleton scene renderer configuration, rather than a
    # light instance.  Keep the values in Roblox units; the importer performs
    # the necessarily approximate Blender conversion in one place.
    for instance in instances.values():
        if instance.class_name != "Lighting":
            continue

        def lighting_color(name, default):
            value = _first_prop(instance, name)
            if not isinstance(value, (list, tuple)) or len(value) < 3:
                return list(default)
            return [float(component) for component in value[:3]]

        def lighting_number(name, default):
            value = _first_prop(instance, name)
            return float(value) if isinstance(value, (int, float)) else default

        time_of_day = _first_prop(instance, "TimeOfDay")
        if isinstance(time_of_day, str):
            try:
                hours, minutes, seconds = (float(piece) for piece in time_of_day.split(":"))
                clock_time = hours + minutes / 60.0 + seconds / 3600.0
            except (TypeError, ValueError):
                clock_time = lighting_number("ClockTime", 14.0)
        else:
            clock_time = lighting_number("ClockTime", 14.0)

        meta["scene_lighting"] = {
            "ambient": lighting_color("Ambient", (0.0, 0.0, 0.0)),
            "outdoor_ambient": lighting_color("OutdoorAmbient", (0.0, 0.0, 0.0)),
            "brightness": lighting_number("Brightness", 1.0),
            "clock_time": clock_time,
            "geographic_latitude": lighting_number("GeographicLatitude", 41.733),
            "exposure_compensation": lighting_number("ExposureCompensation", 0.0),
            "environment_diffuse_scale": lighting_number("EnvironmentDiffuseScale", 1.0),
            "environment_specular_scale": lighting_number("EnvironmentSpecularScale", 1.0),
            "global_shadows": bool(_first_prop(instance, "GlobalShadows")) if _first_prop(instance, "GlobalShadows") is not None else True,
            "shadow_softness": lighting_number("ShadowSoftness", 0.0),
            "fog_color": lighting_color("FogColor", (0.75, 0.75, 0.75)),
            "fog_start": lighting_number("FogStart", 0.0),
            "fog_end": lighting_number("FogEnd", 100000.0),
            "technology": _first_prop(instance, "Technology"),
        }
        break

    def effect_color(instance, name, default):
        value = _first_prop(instance, name)
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            return list(default)
        return [float(component) for component in value[:3]]

    for instance in instances.values():
        if instance.class_name != "Atmosphere":
            continue
        meta["scene_atmosphere"] = {
            "enabled": bool(_first_prop(instance, "Enabled")) if _first_prop(instance, "Enabled") is not None else True,
            "density": float(_first_prop(instance, "Density") or 0.0),
            "offset": float(_first_prop(instance, "Offset") or 0.0),
            "haze": float(_first_prop(instance, "Haze") or 0.0),
            "glare": float(_first_prop(instance, "Glare") or 0.0),
            "color": effect_color(instance, "Color", (0.75, 0.75, 0.75)),
            "decay": effect_color(instance, "Decay", (1.0, 1.0, 1.0)),
        }
        break

    post_effects = {}
    for instance in instances.values():
        if instance.class_name not in ("ColorCorrectionEffect", "BloomEffect", "SunRaysEffect"):
            continue
        if instance.class_name == "ColorCorrectionEffect":
            post_effects["color_correction"] = {
                "enabled": bool(_first_prop(instance, "Enabled")) if _first_prop(instance, "Enabled") is not None else True,
                "brightness": float(_first_prop(instance, "Brightness") or 0.0),
                "contrast": float(_first_prop(instance, "Contrast") or 0.0),
                "saturation": float(_first_prop(instance, "Saturation") or 0.0),
                "tint": effect_color(instance, "TintColor", (1.0, 1.0, 1.0)),
            }
        elif instance.class_name == "BloomEffect":
            post_effects["bloom"] = {
                "enabled": bool(_first_prop(instance, "Enabled")) if _first_prop(instance, "Enabled") is not None else True,
                "intensity": float(_first_prop(instance, "Intensity") or 0.0),
                "size": float(_first_prop(instance, "Size") or 0.0),
                "threshold": float(_first_prop(instance, "Threshold") or 0.0),
            }
        else:
            post_effects["sun_rays"] = {
                "enabled": bool(_first_prop(instance, "Enabled")) if _first_prop(instance, "Enabled") is not None else True,
                "intensity": float(_first_prop(instance, "Intensity") or 0.0),
                "spread": float(_first_prop(instance, "Spread") or 0.0),
            }
    if post_effects:
        meta["scene_post_effects"] = post_effects

    for instance in instances.values():
        if instance.class_name != "MaterialService":
            continue
        use_2022 = _first_prop(instance, "Use2022Materials")
        meta["use_2022_materials"] = True if use_2022 is None else bool(use_2022)
        break

    for instance in instances.values():
        if instance.class_name != "Sky":
            continue
        sky = {
            key: _content_to_str(_first_prop(instance, prop))
            for key, prop in (
                ("back", "SkyboxBk"), ("down", "SkyboxDn"), ("front", "SkyboxFt"),
                ("left", "SkyboxLf"), ("right", "SkyboxRt"), ("up", "SkyboxUp"),
            )
        }
        if any(sky.values()):
            meta["scene_sky"] = sky
        break
    rig = _build_rig_tree(instances, roots)
    if rig is not None:
        meta["rig"] = rig
        mesh_to_bone = _derive_mesh_to_bone(rig)
        if mesh_to_bone:
            meta["meshToBone"] = mesh_to_bone
    scene_rigs = (
        _model_rigs(instances, roots)
        if workspace_refs is None
        else _scene_rigs(instances, roots)
    )
    if scene_rigs:
        meta["scene_rigs"] = scene_rigs

    # Clothing templates (classic avatar shirt/pants). These are composited
    # onto body limbs by Roblox's renderer; capture the template ids so the
    # importer can reconstruct the look.
    for inst in instances.values():
        if inst.class_name == "Shirt":
            template = _content_to_str(_first_prop(inst, "ShirtTemplate"))
            if template:
                meta["shirt_template"] = template
        elif inst.class_name == "Pants":
            template = _content_to_str(_first_prop(inst, "PantsTemplate"))
            if template:
                meta["pants_template"] = template

    # HumanoidDescription (modern avatar spec). Per-limb body colors take
    # priority over part Color3uint8 at render time; clothing asset ids act
    # as fallbacks when no Shirt/Pants instances are present.
    for inst in instances.values():
        if inst.class_name != "HumanoidDescription":
            continue
        body_colors = {}
        for key, prop in (
            ("head", "HeadColor3"),
            ("torso", "TorsoColor3"),
            ("left_arm", "LeftArmColor3"),
            ("right_arm", "RightArmColor3"),
            ("left_leg", "LeftLegColor3"),
            ("right_leg", "RightLegColor3"),
        ):
            color = _first_prop(inst, prop)
            if isinstance(color, (list, tuple)) and len(color) >= 3:
                # Legacy saves store Color3uint8 (0-255); modern saves write
                # float Color3 (0-1). Normalize to 0-1 either way.
                if any(float(component) > 1.0 for component in color[:3]):
                    color = [float(component) / 255.0 for component in color[:3]]
                body_colors[key] = [float(color[0]), float(color[1]), float(color[2])]
        if body_colors:
            meta["hd_body_colors"] = body_colors
        for key, prop in (
            ("hd_shirt_id", "Shirt"),
            ("hd_pants_id", "Pants"),
            ("hd_graphic_tshirt_id", "GraphicTShirt"),
            ("hd_face_id", "Face"),
        ):
            value = _first_prop(inst, prop)
            if isinstance(value, (int, float)) and value > 0:
                meta[key] = int(value)
        scale = {}
        # NB: the serialized property names carry a "Scale" suffix that the
        # Lua API names (Height/Width/...) do not.
        for key, prop, default in (
            ("height", "HeightScale", 1.0),
            ("width", "WidthScale", 1.0),
            ("depth", "DepthScale", 1.0),
            ("head", "HeadScale", 1.0),
            ("proportion", "ProportionScale", 0.0),
            ("body_type", "BodyTypeScale", 0.0),
        ):
            value = _first_prop(inst, prop)
            scale[key] = float(value) if isinstance(value, (int, float)) else default
        meta["hd_scale"] = scale
        break

    # Classic characters keep their body colors in a BodyColors instance
    # rather than the HumanoidDescription; HD colors stay authoritative when
    # both are present.
    if not meta.get("hd_body_colors"):
        for inst in instances.values():
            if inst.class_name != "BodyColors":
                continue
            body_colors = {}
            for key, prop in (
                ("head", "HeadColor3"),
                ("torso", "TorsoColor3"),
                ("left_arm", "LeftArmColor3"),
                ("right_arm", "RightArmColor3"),
                ("left_leg", "LeftLegColor3"),
                ("right_leg", "RightLegColor3"),
            ):
                color = _first_prop(inst, prop)
                if isinstance(color, (list, tuple)) and len(color) >= 3:
                    # Legacy saves store Color3uint8 (0-255); modern saves
                    # write float Color3 (0-1). Normalize to 0-1 either way.
                    if any(float(component) > 1.0 for component in color[:3]):
                        color = [float(component) / 255.0 for component in color[:3]]
                    body_colors[key] = [float(color[0]), float(color[1]), float(color[2])]
            if body_colors:
                meta["hd_body_colors"] = body_colors
            break

    # Legacy scale NumberValues parented to the Humanoid (BodyHeightScale
    # etc.). These are what the engine actually applies to the rig instance —
    # the HumanoidDescription can say 1.0 while the rig carries 2.0 — so they
    # override the HD-derived values when present.
    for inst in instances.values():
        if inst.class_name != "Humanoid":
            continue
        legacy = {}
        for child in inst.children:
            if child.class_name not in ("NumberValue",):
                continue
            legacy[child.name] = _first_prop(child, "Value")
        if legacy:
            scale = dict(meta.get("hd_scale") or {
                "height": 1.0, "width": 1.0, "depth": 1.0,
                "head": 1.0, "proportion": 0.0, "body_type": 0.0,
            })
            for key, value_name in (
                ("height", "BodyHeightScale"),
                ("width", "BodyWidthScale"),
                ("depth", "BodyDepthScale"),
                ("head", "HeadScale"),
                ("proportion", "BodyProportionScale"),
                ("body_type", "BodyTypeScale"),
            ):
                value = legacy.get(value_name)
                if isinstance(value, (int, float)):
                    scale[key] = float(value)
            meta["hd_scale"] = scale
        break

    # Weapon grip metadata stamped by the Studio plugin's RBXM weapon
    # export.  The grip joint itself references the rig-side hand (outside
    # this file), so the connection travels as attributes on the weapon
    # container/root instead of a dangling joint reference.
    for instance in instances.values():
        attrs = _instance_attributes(instance)
        if not attrs:
            continue
        try:
            grip_count = int(attrs.get(_WEAPON_GRIP_COUNT) or "0")
        except (TypeError, ValueError):
            continue
        if grip_count <= 0:
            continue
        grips: List[WeaponGrip] = []
        for index in range(min(grip_count, 64)):
            prefix = f"{_WEAPON_GRIP_PREFIX}{index}_"
            root = attrs.get(prefix + "Root")
            bone = attrs.get(prefix + "Bone")
            if not isinstance(root, str) or not isinstance(bone, str):
                continue
            c0 = _parse_cf12(attrs.get(prefix + "C0"))
            c1 = _parse_cf12(attrs.get(prefix + "C1"))
            if c0 is None or c1 is None:
                continue
            joint_type = attrs.get(prefix + "JointType")
            joint_name = attrs.get(prefix + "JointName")
            grips.append({
                "root": root,
                "bone": bone,
                "jointType": joint_type if isinstance(joint_type, str) else "Motor6D",
                "jointName": joint_name if isinstance(joint_name, str) else None,
                "connectionC0": c0,
                "connectionC1": c1,
            })
        if grips:
            meta["weaponGrip"] = grips
            grip_version = attrs.get(_WEAPON_GRIP_VERSION)
            if isinstance(grip_version, str):
                meta["weaponGripVersion"] = grip_version
            break
    return meta
