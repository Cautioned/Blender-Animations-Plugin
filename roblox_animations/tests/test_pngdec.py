"""Tests for the pure-Python PNG decoder (rig/pngdec.py)."""
import struct
import unittest
import zlib

import numpy as np

from roblox_animations.rig import pngdec


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _make_png(
    width: int,
    height: int,
    color_type: int,
    raw_rows: list,
    palette: bytes = b"",
    trns: bytes = b"",
    bit_depth: int = 8,
    interlace: int = 0,
) -> bytes:
    ihdr = struct.pack(
        ">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace
    )
    chunks = [_chunk(b"IHDR", ihdr)]
    if palette:
        chunks.append(_chunk(b"PLTE", palette))
    if trns:
        chunks.append(_chunk(b"tRNS", trns))
    body = bytearray()
    for row in raw_rows:
        if isinstance(row, (list, tuple)):
            body.append(row[0])  # filter byte
            body.extend(bytes(row[1]))
        else:
            body.append(0)
            body.extend(bytes(row))
    chunks.append(_chunk(b"IDAT", zlib.compress(bytes(body))))
    chunks.append(_chunk(b"IEND", b""))
    return pngdec._SIGNATURE + b"".join(chunks)


def _rgba(r, g, b, a=255):
    return np.array([r, g, b, a], dtype=np.uint8)


class TestPngDecode(unittest.TestCase):
    def test_rgba_roundtrip(self):
        rows = [bytes([0, 10, 20, 30, 1, 11, 21, 31])]
        data = _make_png(2, 1, 6, rows)
        width, height, has_alpha, rgba = pngdec.decode_png(data)
        self.assertEqual((width, height), (2, 1))
        self.assertTrue(has_alpha)
        self.assertEqual(rgba.shape, (1, 2, 4))
        self.assertEqual(rgba[0, 0].tolist(), [0, 10, 20, 30])
        self.assertEqual(rgba[0, 1].tolist(), [1, 11, 21, 31])

    def test_rgb_no_alpha(self):
        data = _make_png(1, 1, 2, [bytes([9, 8, 7])])
        width, height, has_alpha, rgba = pngdec.decode_png(data)
        self.assertEqual((width, height), (1, 1))
        self.assertFalse(has_alpha)
        self.assertEqual(rgba[0, 0].tolist(), [9, 8, 7, 255])

    def test_palette_with_trns(self):
        palette = bytes([255, 0, 0, 0, 0, 255])  # red, blue
        trns = bytes([128, 255])  # red half-transparent
        data = _make_png(2, 1, 3, [bytes([0, 1])], palette=palette, trns=trns)
        width, height, has_alpha, rgba = pngdec.decode_png(data)
        self.assertTrue(has_alpha)
        self.assertEqual(rgba[0, 0].tolist(), [255, 0, 0, 128])
        self.assertEqual(rgba[0, 1].tolist(), [0, 0, 255, 255])

    def test_sub_filter(self):
        # Sub filter: each byte stores the delta from the previous byte.
        raw = bytes([7, 7, 7, 7, 7])
        data = _make_png(5, 1, 0, [(1, raw)])
        _, _, _, rgba = pngdec.decode_png(data)
        self.assertEqual(rgba[0, :, 0].tolist(), [7, 14, 21, 28, 35])

    def test_up_filter(self):
        data = _make_png(2, 2, 0, [bytes([1, 2]), (2, bytes([3, 4]))])
        _, _, _, rgba = pngdec.decode_png(data)
        self.assertEqual(rgba[:, :, 0].tolist(), [[1, 2], [4, 6]])

    def test_average_and_paeth_filters(self):
        # Average: delta from floor((above + reconstructed-left) / 2).
        data = _make_png(2, 2, 0, [bytes([10, 10]), (3, bytes([0, 0]))])
        _, _, _, rgba = pngdec.decode_png(data)
        self.assertEqual(rgba[1, 0, 0], 5)
        self.assertEqual(rgba[1, 1, 0], 7)

    def test_paeth_filter(self):
        # Paeth: raw zero picks the best predictor (above=10 for both pixels).
        data = _make_png(2, 2, 0, [bytes([10, 10]), (4, bytes([0, 0]))])
        _, _, _, rgba = pngdec.decode_png(data)
        self.assertEqual(rgba[1, 0, 0], 10)
        self.assertEqual(rgba[1, 1, 0], 10)

    def test_gray_with_trns(self):
        trns = struct.pack(">H", 3)  # gray 3 -> transparent
        data = _make_png(2, 1, 0, [bytes([3, 4])], trns=trns)
        _, _, has_alpha, rgba = pngdec.decode_png(data)
        self.assertTrue(has_alpha)
        self.assertEqual(rgba[0, 0].tolist(), [3, 3, 3, 0])
        self.assertEqual(rgba[0, 1].tolist(), [4, 4, 4, 255])

    def test_interlaced_rejected(self):
        data = _make_png(1, 1, 6, [bytes([0, 0, 0, 255])], interlace=1)
        self.assertIsNone(pngdec.decode_png(data))

    def test_16bit_rejected(self):
        data = _make_png(
            1, 1, 0, [bytes([0, 0])], bit_depth=16
        )
        self.assertIsNone(pngdec.decode_png(data))

    def test_garbage_rejected(self):
        self.assertIsNone(pngdec.decode_png(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64))
        self.assertIsNone(pngdec.decode_png(b""))

    def test_rgb_trns(self):
        trns = struct.pack(">HHH", 1, 2, 3)
        data = _make_png(2, 1, 2, [bytes([1, 2, 3, 4, 5, 6])], trns=trns)
        _, _, has_alpha, rgba = pngdec.decode_png(data)
        self.assertTrue(has_alpha)
        self.assertEqual(rgba[0, 0].tolist(), [1, 2, 3, 0])
        self.assertEqual(rgba[0, 1].tolist(), [4, 5, 6, 255])


if __name__ == "__main__":
    unittest.main()
