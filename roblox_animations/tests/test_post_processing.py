"""Source metadata, display controls and non-destructive compositor lifetime."""
import struct
import unittest
import bpy
from ..core.rbxm import parse_rbxm
from ..core.post_processing import correction_matrix
from ..rig import neon, post_processing
from .test_content_serialization import _model


class PostMetadataTests(unittest.TestCase):
    def test_serialized_material_generation_alias_preserves_false_and_true(self):
        for name in ('Use2022MaterialsXml', 'Use2022Materials'):
            for value in (False, True):
                with self.subTest(name=name, value=value):
                    meta = parse_rbxm(_model([('MaterialService', -1, {name: (2, bytes([value]))})]))
                    self.assertIs(meta['use_2022_materials'], value)
        # Native serialized property wins over a stale runtime alias.
        meta = parse_rbxm(_model([('MaterialService', -1, {
            'Use2022MaterialsXml': (2, b'\0'), 'Use2022Materials': (2, b'\1')})]))
        self.assertFalse(meta['use_2022_materials'])
        self.assertNotIn('use_2022_materials', parse_rbxm(_model([('MaterialService', -1, {})])))

    def test_multiple_corrections_and_disabled_effect_are_retained(self):
        meta = parse_rbxm(_model([('Lighting', -1, {}),
            ('ColorCorrectionEffect', 0, {'Brightness': (5, struct.pack('<d', .2))}),
            ('ColorCorrectionEffect', 0, {'Enabled': (2, b'\0')}),
            ('ColorCorrectionEffect', 0, {'Saturation': (5, struct.pack('<d', -1))})]))
        effects = meta['scene_post_effects']
        self.assertEqual(len(effects['color_corrections']), 3)
        self.assertFalse(effects['color_corrections'][1]['enabled'])
        self.assertEqual(effects['color_correction']['saturation'], -1)

    def test_identity_black_white_tint_saturation_and_disabled_controls(self):
        def apply(effect, rgb):
            return [sum(row[j]*rgb[j] for j in range(3))+row[3]
                    for row in correction_matrix({'color_corrections': effect})]
        self.assertEqual(apply([], [.1, .5, .9]), [.1, .5, .9])
        self.assertEqual(apply([{'enabled': False, 'brightness': 1}], [0, 0, 0]), [0, 0, 0])
        self.assertEqual(apply([{'brightness': .2}], [0, 0, 0]), [.2]*3)
        self.assertEqual(apply([{'tint': [0, .5, 1]}], [1, 1, 1]), [0, .5, 1])
        gray = apply([{'saturation': -1}], [1, 0, 0])
        for value in gray: self.assertAlmostEqual(value, .299)
        self.assertEqual(apply([{'contrast': -1}], [0, .5, 1]), [.5]*3)
        # Two effects compose, instead of silently dropping the earlier one.
        self.assertEqual(apply([{'brightness': .2}, {'tint': [.5]*3}], [0, 0, 0]), [.1]*3)


class PostCompositorTests(unittest.TestCase):
    def setUp(self):
        self.original = bpy.context.window.scene
        self.scene = bpy.data.scenes.new('Post regression'); bpy.context.window.scene = self.scene
        self.scene.render.engine = 'BLENDER_EEVEE' if bpy.app.version >= (5, 0, 0) else 'BLENDER_EEVEE_NEXT'
        self.groups = set(bpy.data.node_groups)
        self.viewport = [(a.spaces.active.shading, a.spaces.active.shading.use_compositor)
                         for screen in bpy.data.screens for a in screen.areas if a.type == 'VIEW_3D']
        self.effects = {'color_correction': {'brightness': .15, 'contrast': .1,
                                            'saturation': -.5, 'tint': [1, .85, .7]}}
    def tearDown(self):
        bpy.context.window.scene = self.original
        bpy.data.scenes.remove(self.scene, do_unlink=True)
        for group in set(bpy.data.node_groups)-self.groups:
            bpy.data.node_groups.remove(group, do_unlink=True)
        for shading, value in self.viewport: shading.use_compositor = value
    def tree(self): return self.scene.compositing_node_group if hasattr(self.scene, 'compositing_node_group') else self.scene.node_tree
    def managed(self): return [n for n in self.tree().nodes if n.get('RBXPostManaged')]
    def test_reimport_updates_one_stage_and_disabling_restores_user_graph(self):
        tree, modern = neon._root_tree(self.scene)
        source = next(n for n in tree.nodes if n.type == 'R_LAYERS')
        output = next(n for n in tree.nodes if n.type in ('GROUP_OUTPUT', 'COMPOSITE'))
        grade = tree.nodes.new('ShaderNodeGamma' if modern else 'CompositorNodeGamma')
        tree.links.new(source.outputs['Image'], grade.inputs[0]); tree.links.new(grade.outputs[0], output.inputs[0])
        self.assertTrue(post_processing.configure_post_processing(self.scene, self.effects))
        group = self.managed()[0].node_tree
        post_processing.configure_post_processing(self.scene, self.effects)
        self.assertEqual(len(self.managed()), 1); self.assertIs(self.managed()[0].node_tree, group)
        self.assertEqual(grade.inputs[0].links[0].from_node.as_pointer(), self.managed()[0].as_pointer())
        self.assertEqual(output.inputs[0].links[0].from_node.as_pointer(), grade.as_pointer())
        post_processing.configure_post_processing(self.scene, {'color_correction': {'enabled': False}})
        self.assertFalse(self.managed())
        self.assertEqual(grade.inputs[0].links[0].from_node.as_pointer(), source.as_pointer())
    def test_neon_is_before_correction_even_when_material_arrives_late(self):
        post_processing.configure_post_processing(self.scene, self.effects)
        neon.configure_neon_compositor(self.scene, {})
        post = self.managed()[0]
        self.assertTrue(post.inputs['Image'].links[0].from_node.get('RBXNeonManaged'))
        post_processing.configure_post_processing(self.scene, self.effects)
        self.assertEqual(len(self.managed()), 1)
    def test_display_space_and_exposure_drivers_are_explicit(self):
        self.scene.view_settings.exposure = 1.5
        post_processing.configure_post_processing(self.scene, self.effects)
        group = self.managed()[0].node_tree
        self.assertEqual(group.nodes['RBX Display Color'].to_color_space, 'sRGB')
        self.assertEqual(group.nodes['RBX Scene Linear Color'].from_color_space, 'sRGB')
        drivers = group.animation_data.drivers
        self.assertEqual({curve.driver.expression for curve in drivers}, {'exposure', '-exposure'})
        self.assertTrue(all(curve.driver.variables[0].targets[0].id == self.scene for curve in drivers))
        self.assertEqual(self.scene.view_settings.exposure, 1.5)
    def test_explicit_bloom_supports_non_neon_and_zero_suppresses_it(self):
        fx = {'blooms': [{'enabled': True, 'intensity': .5, 'size': 24, 'threshold': .8},
                         {'enabled': True, 'intensity': 0}]}
        post_processing.configure_post_processing(self.scene, fx)
        group = self.managed()[0].node_tree
        self.assertEqual(sum(n.type == 'BLUR' for n in group.nodes), 1)
        self.assertIsNotNone(group.nodes.get('RBX Non-Neon Highlights'))
        self.assertTrue(self.managed()[0].inputs['Neon'].links)
        raw = self.managed()[0].inputs['Scene'].links[0].from_node
        self.assertEqual(raw.type, 'R_LAYERS')
        neon.configure_neon_compositor(self.scene, fx)
        self.assertTrue(self.managed()[0].inputs['Image'].links[0].from_node.get('RBXNeonManaged'))
        self.assertEqual(self.managed()[0].inputs['Scene'].links[0].from_node.as_pointer(), raw.as_pointer())
        post_processing.configure_post_processing(self.scene, {'bloom': {'intensity': 0}})
        self.assertFalse(self.managed())
    def test_custom_image_only_compositor_is_preserved(self):
        tree, modern = neon._root_tree(self.scene)
        source = next(n for n in tree.nodes if n.type == 'R_LAYERS'); tree.nodes.remove(source)
        nodes = set(tree.nodes)
        self.assertFalse(post_processing.configure_post_processing(self.scene, self.effects))
        self.assertEqual(set(tree.nodes), nodes)


class MaterialProjectionTests(unittest.TestCase):
    def test_generic_mesh_does_not_restart_at_triangle_diagonal(self):
        from ..rig import textures
        mesh = bpy.data.meshes.new('Continuous material UV')
        # Adjacent triangles with different minima along both projected axes.
        mesh.from_pydata([(0,0,0), (3,0,0), (2,3,0), (-1,2,0)], [], [(0,1,2),(0,2,3)])
        mesh.update()
        try:
            uv = textures._ensure_builtin_material_uvs(mesh)
            found = {}
            for poly in mesh.polygons:
                for loop in poly.loop_indices:
                    vertex = mesh.loops[loop].vertex_index
                    value = tuple(uv.data[loop].uv)
                    if vertex in found:
                        for a,b in zip(value, found[vertex]): self.assertAlmostEqual(a,b,places=6)
                    found[vertex] = value
            self.assertAlmostEqual(found[2][0], .3, places=6)
            self.assertAlmostEqual(found[2][1], .3, places=6)
        finally: bpy.data.meshes.remove(mesh)

    def test_curved_template_and_uncached_shapes_share_projection_anchor(self):
        import numpy as np
        from ..rig import primitive_shapes
        for shape in ('ball', 'cylinder', 'wedge', 'corner_wedge'):
            with self.subTest(shape=shape):
                entry = {'shape': shape, 'part_size': [4,4,4]}
                data = primitive_shapes._build_primitive_mesh_data(entry, include_surface_data=False)
                positions = np.array(data['positions']); faces = np.array(data['faces'])
                result = primitive_shapes._static_primitive_part_arrays(np, entry, np.eye(3), np.zeros(3), True, .1, 0)
                uv = result[2].reshape(-1,3,2)
                found = {}; checked = 0
                for triangle, face in enumerate(faces):
                    points = positions[face]
                    au,av,sign,_ = primitive_shapes._primitive_face_projection_axes(points)
                    for corner, vertex in enumerate(face):
                        key = (int(vertex), au, av, sign)
                        if key in found:
                            self.assertTrue(np.allclose(uv[triangle,corner],found[key],atol=1e-7)); checked += 1
                        found[key] = uv[triangle,corner]
                self.assertGreater(checked,0)
                template = primitive_shapes._primitive_shape_template(np, shape)
                if template is not None:
                    coeff = template[2].reshape(-1,3,2)
                    self.assertTrue(np.allclose(coeff*4*.1,uv,atol=1e-7))


class BuiltinTintMaskTests(unittest.TestCase):
    def test_builtin_alpha_masks_tint_without_changing_shared_surface_image(self):
        from unittest import mock
        from ..rig import textures
        image = bpy.data.images.new('Builtin grout regression', width=2, height=1, alpha=True, float_buffer=True)
        image.pixels[:] = [.6,.6,.6,0, .8,.8,.8,1]; image.update()
        textures._PART_MATERIAL_CACHE.clear()
        try:
            with mock.patch.object(textures, 'fetch_texture_image', return_value=image):
                mat = textures.build_part_material('Tint-mask Brick', {'material':848,'color':[.2,.4,.7]})
                nodes = mat.node_tree.nodes
                color = nodes['RBX Material ColorMap']
                self.assertEqual(color.image.alpha_mode,'CHANNEL_PACKED')
                self.assertIsNot(color.image,image)
                self.assertEqual(image.alpha_mode,'STRAIGHT')
                self.assertAlmostEqual(color.image.pixels[0],.6,places=5)
                mask = nodes['RBX Material Tint Mask']
                self.assertTrue(any(link.from_node == color and link.from_socket.name == 'Alpha'
                                    for socket in mask.inputs for link in socket.links))
                # Hydration uses the same role and reuses its image copy.
                packed = color.image
                textures._hydrate_node_image(color,image)
                self.assertIs(color.image,packed)
                self.assertFalse(textures._principled_node(mat).inputs['Alpha'].links)
            bpy.data.materials.remove(mat,do_unlink=True)
        finally:
            for im in list(bpy.data.images):
                if im.name.startswith('Builtin grout regression'):bpy.data.images.remove(im,do_unlink=True)
            textures._PART_MATERIAL_CACHE.clear()

    def test_deferred_builtin_still_has_mask_when_bytes_arrive_later(self):
        from unittest import mock
        from ..rig import textures
        textures._PART_MATERIAL_CACHE.clear()
        with mock.patch.object(textures, 'fetch_texture_image', return_value=None):
            mat = textures.build_part_material('Deferred grout', {'material':848,'color':[.2,.4,.7]})
        try:
            self.assertIsNotNone(mat.node_tree.nodes.get('RBX Material Tint Mask'))
            self.assertTrue(mat.node_tree.nodes['RBX Material ColorMap']['RBXBuiltinPackedColor'])
        finally:
            bpy.data.materials.remove(mat,do_unlink=True); textures._PART_MATERIAL_CACHE.clear()
