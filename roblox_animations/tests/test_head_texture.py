"""Dynamic head textures preserve skin and translucent cosmetic details."""

import unittest
from unittest import mock

import bpy
import numpy as np

from ..rig import clothing, textures


class TestHeadTexture(unittest.TestCase):
    def setUp(self):
        self.context = dict(clothing._CLOTHING_CONTEXT)
        self.cache = dict(clothing._BAKE_CACHE)
        clothing.set_clothing_context()

    def tearDown(self):
        clothing._CLOTHING_CONTEXT.clear()
        clothing._CLOTHING_CONTEXT.update(self.context)
        clothing._BAKE_CACHE.clear()
        clothing._BAKE_CACHE.update(self.cache)

    def test_straight_rgba_overlay_in_both_bake_paths(self):
        # Includes low-alpha RGB samples from HeadColor's texture 110889549029927.
        samples = [
            [1, 0.5019608, 0.5019608, 2 / 255],
            [0.827451, 0.454902, 0.466667, 60 / 255],
            [0.2, 0.9, 0.6, 0],
            [0, 0, 0, 0.1],
            [1, 1, 1, 1],
            [0.1, 0.2, 0.3, 1],
        ]
        for numpy_enabled in (False, True):
            for body in (
                (184 / 255, 114 / 255, 69 / 255, 1),
                (0.1, 0.2, 0.3, 1),
                (1, 1, 1, 1),
            ):
                with self.subTest(numpy=numpy_enabled, body=body):
                    pixels = np.array(samples, dtype=np.float32).reshape(-1)
                    if not numpy_enabled:
                        pixels = pixels.tolist()
                    with mock.patch.object(
                        clothing, "_numpy", return_value=np if numpy_enabled else None
                    ):
                        _, _, result = clothing._bake_head_pixels(
                            body, lambda ref: (pixels, 6, 1, True), tint_ref="head"
                        )
                    for i, sample in enumerate(samples):
                        for c in range(3):
                            linear = clothing._srgb_decode(sample[c]) * sample[
                                3
                            ] + clothing._srgb_decode(body[c]) * (1 - sample[3])
                            self.assertAlmostEqual(
                                result[4 * i + c],
                                clothing._srgb_encode(linear),
                                places=5,
                            )
                        self.assertEqual(result[4 * i + 3], 1)

    def test_native_and_worker_decoders_supply_straight_rgb(self):
        import struct
        import zlib

        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        rgba = bytes([211, 116, 119, 60])
        payload = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" + rgba))
            + chunk(b"IEND", b"")
        )
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "head.png"
            path.write_bytes(payload)
            native = bpy.data.images.load(str(path), check_existing=False)
            raw = textures._decode_to_raw_pixels(payload)
            worker = textures._image_from_raw_pixels("head_worker", *raw)
            try:
                for image in (native, worker):
                    pixels, _, _, _ = clothing._template_pixels_bpy(image)
                    for actual, expected in zip(pixels, rgba):
                        self.assertAlmostEqual(float(actual), expected / 255, places=6)
            finally:
                bpy.data.images.remove(native)
                bpy.data.images.remove(worker)

    def test_different_skin_colors_do_not_share_head_bake(self):
        with mock.patch.object(
            clothing, "_bake_head_image", side_effect=[object(), object()]
        ) as bake:
            first = clothing.get_limb_texture(
                "Head", (0.1, 0.2, 0.3, 1), tint_ref="same"
            )
            second = clothing.get_limb_texture(
                "Head", (0.6, 0.7, 0.8, 1), tint_ref="same"
            )
            self.assertIsNot(first, second)
            self.assertEqual(bake.call_count, 2)
            self.assertIs(
                first,
                clothing.get_limb_texture("Head", (0.1, 0.2, 0.3, 1), tint_ref="same"),
            )

    def test_classic_face_fade_still_reveals_skin(self):
        clothing.set_clothing_context(face_texture="face", face_transparency=1)
        _, _, pixels = clothing._bake_head_pixels(
            (0.3, 0.4, 0.5, 1), lambda ref: ([1, 0, 0, 1], 1, 1, True)
        )
        np.testing.assert_allclose(pixels, [0.3, 0.4, 0.5, 1], atol=1e-6)
