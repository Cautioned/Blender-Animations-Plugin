"""Color-management regression tests: linear composites in the clothing
bake and viewport surface tint, and sRGB decoding of sky cubemap faces."""
import unittest
from unittest import mock

import bpy

from ..operators import import_ops
from ..rig import clothing
from ..rig import textures


def _gray_image(name, value=0.5, size=8):
    image = bpy.data.images.new(name, width=size, height=size, alpha=False)
    # Colorspace FIRST: Blender zeroes a generated image's RGB buffer on
    # any post-upload colorspace assignment.
    image.colorspace_settings.name = "sRGB"
    image.pixels = [value, value, value, 1.0] * (size * size)
    image.update()
    return image


class ColorPipelineTests(unittest.TestCase):
    def setUp(self):
        self._images = []

    def tearDown(self):
        for image in self._images:
            try:
                if image.name in bpy.data.images:
                    bpy.data.images.remove(image)
            except ReferenceError:
                pass
        textures._TINTED_SURFACE_BAND_CACHE.clear()
        textures._BAND_IMAGE_CACHE.clear()
        clothing.set_clothing_context()

    def _track(self, image):
        if image is not None:
            self._images.append(image)
        return image

    def test_sky_equirect_decodes_face_pixels(self):
        # The equirectangular world map is Linear-tagged, so the sRGB sky
        # faces must be decoded when read: output = decode(source), never
        # the raw encoded value.
        faces = [
            (key, self._track(_gray_image(f"rbx_test_sky_{key}")))
            for key in ("right", "left", "front", "back", "up", "down")
        ]
        out = self._track(
            import_ops._create_equirectangular_sky_image(faces, name="rbx_test_sky_env")
        )
        self.assertIsNotNone(out)
        source = faces[0][1].pixels[0]
        expected = import_ops._srgb_decode_array([source, source, source, 1.0])
        for index in (0, 100, 4000, len(out.pixels) - 4):
            self.assertAlmostEqual(out.pixels[index], expected[0], places=4)
            self.assertAlmostEqual(out.pixels[index + 1], expected[1], places=4)
            self.assertAlmostEqual(out.pixels[index + 2], expected[2], places=4)
            self.assertAlmostEqual(out.pixels[index + 3], 1.0, places=4)

    def test_tinted_surface_band_composites_in_linear(self):
        # The viewport bake must match the render shader: decode atlas and
        # tint to linear, multiply, re-encode.  The old encoded-space
        # multiply (0.5 x 2.0 = 1.0) over-brightened saturated tints.
        band = self._track(_gray_image("rbx_test_band", size=4))
        with mock.patch.object(textures, "_band_image", return_value=band):
            tinted = textures._tinted_surface_band("diff", 0, (1.0, 0.5, 0.25))
        self._track(tinted)
        self.assertIsNotNone(tinted)
        src = band.pixels[0]
        lin = textures._srgb_decode_scalar(src)
        # The baked image is an 8-bit datablock: clamp to [0, 1] and allow
        # byte quantization (places=2).
        expected = (
            min(textures._srgb_encode_scalar(lin * textures._srgb_decode_scalar(2.0)), 1.0),
            min(textures._srgb_encode_scalar(lin * textures._srgb_decode_scalar(1.0)), 1.0),
            min(textures._srgb_encode_scalar(lin * textures._srgb_decode_scalar(0.5)), 1.0),
        )
        self.assertAlmostEqual(tinted.pixels[0], expected[0], places=2)
        self.assertAlmostEqual(tinted.pixels[1], expected[1], places=2)
        self.assertAlmostEqual(tinted.pixels[2], expected[2], places=2)
        self.assertAlmostEqual(tinted.pixels[3], 1.0, places=4)

    def test_group_bake_composites_in_linear(self):
        # A single-triangle synthetic guide isolates the composite math:
        # 0.25 template at alpha 0.5 over black must yield
        # encode(decode(0.25) * 0.5) ~ 0.173, not the encoded-space 0.125.
        # The real torso guide has overlapping triangles that composite the
        # same texel several times, so a fixed-point assertion there is
        # degenerate by construction.
        import numpy as np

        tri = clothing._GuideTriangle(
            (10.0, 10.0), (20.0, 10.0), (20.0, 20.0),
            (0.5, 0.5), (0.5, 0.5), (0.5, 0.5),
            rect_height=clothing._GROUP_RECT["torso"][3],
        )
        lookup = clothing._GuideLookup.__new__(clothing._GuideLookup)
        lookup.triangles = [tri]

        tw, th = 2, 2
        tpl = np.zeros(tw * th * 4, dtype=np.float32)
        tpl[0::4] = 0.25
        tpl[1::4] = 0.25
        tpl[2::4] = 0.25
        tpl[3::4] = 0.5

        def provider(ref):
            return (tpl, tw, th, True)

        clothing.set_clothing_context(shirt_template="fake_shirt")
        with mock.patch.object(clothing, "_get_guide", return_value=lookup):
            w, h, buf = clothing._bake_group_pixels(
                "torso", (0.0, 0.0, 0.0, 1.0), provider
            )
        self.assertIsNotNone(buf)
        expected = clothing._srgb_encode(
            clothing._srgb_decode(0.25) * 0.5
        )
        covered = 0
        for i in range(0, w * h * 4, 4):
            if buf[i] > 1e-4:
                covered += 1
                self.assertAlmostEqual(buf[i], expected, places=3)
                self.assertAlmostEqual(buf[i + 1], expected, places=3)
                self.assertAlmostEqual(buf[i + 2], expected, places=3)
        self.assertGreater(covered, 50)
