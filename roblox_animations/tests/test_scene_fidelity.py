"""Import correctness checks for EEVEE lighting and SurfaceAppearance."""
import math
import struct
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import bpy
from mathutils import Vector

from .test_content_serialization import _model, _content
from ..core import rbxm
from ..core.constants import get_transform_to_blender
from ..operators import import_ops
from ..rig import textures


_IDENTITY = [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]


def _color(values):
    payload = b''
    for value in values:
        bits = struct.unpack('<I', struct.pack('<f', value))[0]
        payload += struct.pack('>I', ((bits << 1) & 0xffffffff) | (bits >> 31))
    return (7, payload)


def _float(value):
    return (5, struct.pack('<d', value))


class LightingMetadataTests(unittest.TestCase):
    def test_zero_values_and_right_face_survive_parsing(self):
        meta = rbxm.parse_rbxm(_model([('Part', -1, {}), ('SpotLight', 0, {
            'Brightness': _float(0), 'Range': _float(0), 'Angle': _float(0),
            'Face': (0x12, struct.pack('>I', 0)),
        })]))
        light = meta['scene_lights'][0]
        self.assertEqual([light[k] for k in ('brightness', 'range', 'angle', 'face')], [0, 0, 0, 0])

    def test_nested_attachments_resolve_owner_and_transform(self):
        part = rbxm._Instance(0, 'Part')
        a = rbxm._Instance(1, 'Attachment'); a.parent = 0
        b = rbxm._Instance(2, 'Attachment'); b.parent = 1
        light = rbxm._Instance(3, 'SpotLight'); light.parent = 2
        a.props['CFrame'] = [1, 2, 3] + _IDENTITY[3:]
        b.props['CFrame'] = [4, 5, 6] + _IDENTITY[3:]
        with mock.patch.object(rbxm, '_parse_chunks', return_value=({0: part, 1: a, 2: b, 3: light}, {0: part})):
            parsed = rbxm.parse_rbxm(b'')['scene_lights'][0]
        self.assertEqual(parsed['parent_ref'], 0)
        self.assertEqual(parsed['attachment_cf'][:3], [5, 7, 9])

    def test_sun_direction_matches_captured_studio_values(self):
        from ..core.lighting import sun_direction
        samples = [
            (12, 0, (0, 0.9170661, -0.3987352)),
            (6, 0, (0.9170616, 0, -0.3987456)),
            (0, 0, (0, -0.9170601, -0.3987491)),
            (12, 41.733, (0, 0.9497873, 0.3128965)),
            (6, 41.733, (0.9497908, 0, 0.3128856)),
            (14, 41.733, (-0.4748929, 0.8225383, 0.3129017)),
        ]
        for time, latitude, expected in samples:
            with self.subTest(time=time, latitude=latitude):
                actual = sun_direction(time, latitude)
                self.assertLess(sum((a-b)**2 for a,b in zip(actual, expected))**0.5, 0.0001)

    def test_fallback_ambient_survives_night_and_respects_zero_inputs(self):
        from ..core.lighting import fallback_environment_color
        self.assertEqual(fallback_environment_color({"environment_diffuse_scale": 0}, 0), (0, 0, 0))
        night = fallback_environment_color({"outdoor_ambient": [0.5]*3}, 0)
        self.assertTrue(all(channel > 0 for channel in night))
        no_shadows = fallback_environment_color({"global_shadows": False,
            "outdoor_ambient": [1]*3, "ambient": [0]*3, "environment_diffuse_scale": 0}, 1)
        self.assertEqual(no_shadows, (0, 0, 0))
        day = fallback_environment_color({"outdoor_ambient": [0.5]*3}, 1)
        self.assertTrue(all(a > b for a,b in zip(day,night)))

    def test_platform_texture_resolution_preserves_content_priority(self):
        from ..rig import filemesh
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = root / "content"
            content.mkdir()
            platform = root / "PlatformContent" / "pc" / "textures"
            platform.mkdir(parents=True)
            expected = platform / "parity_probe.tex"
            expected.write_bytes(b"platform texture")
            with mock.patch.object(filemesh, "_roblox_content_dirs", return_value=[content]), \
                    mock.patch.object(filemesh, "_resolve_bundled_rbxasset", return_value=None):
                self.assertEqual(filemesh._resolve_rbxasset_path("rbxasset://textures/parity_probe.tex"), expected.resolve())
                literal = content / "textures" / "parity_probe.tex"
                literal.parent.mkdir()
                literal.write_bytes(b"user content")
                self.assertEqual(filemesh._resolve_rbxasset_path("rbxasset://textures/parity_probe.tex"), literal.resolve())
                (root / "escape.bin").write_bytes(b"outside")
                self.assertIsNone(filemesh._resolve_rbxasset_path("rbxasset://../escape.bin"))

    def test_surface_tint_and_emission_metadata(self):
        data = _model([('MeshPart', -1, {}), ('SurfaceAppearance', 0, {
            'Color': _color([0.25, 0.5, 1]),
            'EmissiveMaskContent': (0x22, _content(['rbxassetid://123'])),
            'EmissiveStrength': _float(0),
            'EmissiveTint': _color([1, 0.5, 0.25]),
        })])
        surface = rbxm.parse_rbxm(data)['partAux'][0]['surface_appearance']
        self.assertEqual(surface['color'], [0.25, 0.5, 1])
        self.assertEqual(surface['emissive_mask'], 'rbxassetid://123')
        self.assertEqual(surface['emissive_strength'], 0)
        self.assertEqual(surface['emissive_tint'], [1, 0.5, 0.25])


class EeveeFidelityTests(unittest.TestCase):
    def setUp(self):
        self.collection = bpy.data.collections.new('fidelity_test')
        bpy.context.scene.collection.children.link(self.collection)
        self.world = bpy.context.scene.world
        self.engine = bpy.context.scene.render.engine
        try:
            bpy.context.scene.render.engine = 'BLENDER_EEVEE'
        except TypeError:
            bpy.context.scene.render.engine = 'BLENDER_EEVEE_NEXT'
        self.materials = []
        self.image = bpy.data.images.new('fidelity_image', width=2, height=2, alpha=True)
        self.image.colorspace_settings.name = 'sRGB'
        self.image.pixels[:] = [0.5, 0.5, 0.5, 0.25] * 4
        self.image.update()
        self.patch = mock.patch.object(textures, 'fetch_texture_image', return_value=self.image)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        bpy.context.scene.world = self.world
        bpy.context.scene.render.engine = self.engine
        for obj in list(self.collection.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(self.collection)
        for mat in self.materials:
            bpy.data.materials.remove(mat)
        bpy.data.images.remove(self.image)

    def material(self, surface):
        mat = textures.build_part_material('Fidelity', {
            'class_name': 'MeshPart', 'color': [0.2, 0.4, 0.8],
            'surface_appearance': surface,
        })
        self.materials.append(mat)
        return mat

    def test_attachment_lights_use_emitter_origin_and_selected_face(self):
        cf = [2, 3, 4] + _IDENTITY[3:]
        count = import_ops._create_rbxl_lights(self.collection, [{
            'class_name': 'SpotLight', 'parent_ref': 7, 'attachment_cf': cf,
            'face': 0, 'brightness': 0, 'range': 16,
        }], [{'inst_ref': 7, 'part_cf': _IDENTITY, 'part_size': [10, 10, 10]}])
        self.assertEqual(count, 1)
        obj = self.collection.objects[0]
        transform = get_transform_to_blender()
        self.assertLess((obj.location - transform @ Vector(cf[:3])).length, 1e-5)
        direction = obj.rotation_quaternion @ Vector((0, 0, -1))
        self.assertLess((direction - transform.to_3x3() @ Vector((1, 0, 0))).length, 1e-5)
        self.assertEqual(obj.data.energy, 0)

    def test_zero_range_lights_have_no_energy(self):
        for kind in ("PointLight", "SpotLight", "SurfaceLight"):
            count = import_ops._create_rbxl_lights(self.collection, [{
                "name": kind, "class_name": kind, "parent_ref": 7, "brightness": 1, "range": 0,
            }], [{"inst_ref": 7, "part_cf": _IDENTITY, "part_size": [1]*3}])
            self.assertEqual(count, 1)
            self.assertEqual(self.collection.objects[kind].data.energy, 0)

    def test_light_color_uses_linear_values(self):
        import_ops._create_rbxl_lights(self.collection, [{
            "name": "Colored", "class_name": "PointLight", "parent_ref": 7, "color": [0.5, 0.5, 0.5],
        }], [{"inst_ref": 7, "part_cf": _IDENTITY, "part_size": [1]*3}])
        self.assertAlmostEqual(self.collection.objects["Colored"].data.color[0], 0.21404114)

    def test_classic_surface_shader_matches_authored_tint_transfer(self):
        with mock.patch.object(textures, "_band_image", return_value=self.image):
            mat = textures._primitive_surface_material((0.25, 0.5, 1.0), 3)
        self.materials.append(mat)
        tint = mat.node_tree.nodes["RBX Tint"]
        color = textures._socket_by_type(tint, "input", "A", "RGBA").default_value
        self.assertAlmostEqual(color[0], 0.21404114)
        self.assertAlmostEqual(color[1], 1.0)

    def test_procedural_sky_angles_use_radians(self):
        import_ops._create_rbxl_global_lighting(self.collection, {'clock_time': 12, 'geographic_latitude': 0})
        sky = bpy.context.scene.world.node_tree.nodes['RBX Nishita Sky']
        self.assertAlmostEqual(sky.sun_elevation, math.radians(66.5), places=4)
        self.assertTrue(math.isfinite(sky.sun_rotation))

    def test_surface_tint_is_wired_in_both_alpha_modes(self):
        for mode in (0, 1):
            mat = self.material({'color_map': 'fake', 'color': [1, 0.25, 0.5], 'alpha_mode': mode})
            name = 'RBX Overlay Tint' if mode == 0 else 'RBX TextureID Tint'
            self.assertIsNotNone(mat.node_tree.nodes.get(name))

    def test_tint_mask_does_not_make_surface_transparent(self):
        mat = self.material({'color_map': 'fake', 'color': [1, 0, 0], 'alpha_mode': 2})
        shader = textures._principled_node(mat)
        self.assertIsNotNone(mat.node_tree.nodes.get('RBX Surface Tint Mask'))
        self.assertFalse(shader.inputs['Alpha'].is_linked)
        self.assertEqual(shader.inputs['Alpha'].default_value, 1)

    def test_opaque_surface_ignores_texture_alpha(self):
        mat = self.material({"color_map": "fake", "color": [1, 0.5, 0.25], "alpha_mode": 3})
        shader = textures._principled_node(mat)
        self.assertFalse(shader.inputs["Alpha"].is_linked)
        self.assertEqual(shader.inputs["Alpha"].default_value, 1)
        self.assertIsNone(mat.node_tree.nodes.get("RBX Overlay Mix"))

    def test_missing_surface_color_map_falls_back_through_tint_chain(self):
        for mode in (1, 2, 3):
            entry = {"color": [0.2, 0.4, 0.8], "transparency": 0.3,
                     "surface_appearance": {"color_map": "missing", "color": [0.5]*3, "alpha_mode": mode}}
            with mock.patch.object(textures, "fetch_texture_image", return_value=None):
                mat = textures.build_part_material("MissingSurface", entry)
                self.materials.append(mat)
                textures._apply_missing_color_map_fallback(mat, entry)
            shader = textures._principled_node(mat)
            self.assertFalse(shader.inputs["Base Color"].is_linked)
            self.assertAlmostEqual(shader.inputs["Base Color"].default_value[0], 0.03310477)
            self.assertAlmostEqual(shader.inputs["Alpha"].default_value, 0.7)

    def test_emissive_mask_strength_and_deferred_hydration(self):
        entry = {'surface_appearance': {'emissive_mask': 'fake-mask',
                 'emissive_strength': 3, 'emissive_tint': [1, 0, 0]}}
        mat = self.material(entry['surface_appearance'])
        shader = textures._principled_node(mat)
        self.assertTrue(shader.inputs['Emission Strength'].is_linked)
        self.assertEqual(mat.node_tree.nodes['RBX Surface Emissive Strength'].inputs[1].default_value, 3)
        mask = mat.node_tree.nodes['RBX Surface Emissive Mask']
        mask.image = None
        textures.hydrate_material_images(mat, entry)
        self.assertIsNotNone(mask.image)
        self.assertIn('fake-mask', textures.material_texture_refs(entry))
        changed = {'surface_appearance': dict(entry['surface_appearance'], emissive_strength=0)}
        self.assertNotEqual(textures.part_material_identity(entry), textures.part_material_identity(changed))
