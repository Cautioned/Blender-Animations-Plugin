"""Native Neon/Bloom metadata and safe, reusable EEVEE compositing."""
import json,math,struct,unittest
from pathlib import Path
import bpy
from ..core import rbxm
from ..core.neon import (bloom_settings,neon_response,NEON_AOV,NEON_RADIANCE)
from ..rig import textures,neon
from .test_content_serialization import _model

FIXTURES=Path(__file__).parent/'fixtures/neon_roundtrip'

class NeonMetadataTests(unittest.TestCase):
    def test_zero_bloom_values_survive_native_export(self):
        m=rbxm.parse_rbxm((FIXTURES/'08_bloom_intensity_zero.rbxm').read_bytes())
        self.assertEqual(m['scene_post_effects']['bloom']['intensity'],0)
        self.assertEqual(bloom_settings(m['scene_post_effects'])[0]['intensity'],0)
    def test_disabled_bloom_restores_implicit_glow(self):
        m=rbxm.parse_rbxm((FIXTURES/'14_bloom_disabled.rbxm').read_bytes())
        self.assertFalse(m['scene_post_effects']['bloom']['enabled'])
        self.assertTrue(bloom_settings(m['scene_post_effects'])[0]['implicit'])
    def test_multiple_native_blooms_are_retained(self):
        m=rbxm.parse_rbxm((FIXTURES/'15_bloom_multiple.rbxm').read_bytes())
        layers=m['scene_post_effects']['blooms']
        self.assertEqual([x['intensity'] for x in layers],[1,.5])
        self.assertEqual(len(bloom_settings(m['scene_post_effects'])),2)
    def test_missing_defaults_differ_from_explicit_zero(self):
        m=rbxm.parse_rbxm(_model([('BloomEffect',-1,{})]))
        self.assertEqual(m['scene_post_effects']['bloom']['size'],24)
        zero=(5,struct.pack('<d',0))
        m=rbxm.parse_rbxm(_model([('BloomEffect',-1,{'Size':zero,'Threshold':zero,'Intensity':zero})]))
        self.assertEqual([m['scene_post_effects']['bloom'][k] for k in ('size','threshold','intensity')],[0,0,0])
    def test_measured_midtones_match_captured_source(self):
        def decode(v):return v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4
        def encode(v):return v*12.92 if v<=.0031308 else 1.055*v**(1/2.4)-.055
        for authored,source in ((33,3),(55,23),(78,77),(100,147),(122,204),(144,235)):
            actual=255*encode(neon_response(NEON_RADIANCE*decode(authored/255)))
            self.assertLess(abs(actual-source),4)
        self.assertEqual(neon_response(0),0)

class NeonCompositorTests(unittest.TestCase):
    def setUp(self):
        self.original=bpy.context.window.scene
        self.scene=bpy.data.scenes.new('Neon regression');bpy.context.window.scene=self.scene
        self.scene.render.engine='BLENDER_EEVEE' if bpy.app.version>=(5,0,0) else 'BLENDER_EEVEE_NEXT'
        self.groups=set(bpy.data.node_groups);self.materials=set(bpy.data.materials)
        self.viewport=[(a.spaces.active.shading,a.spaces.active.shading.use_compositor) for s in bpy.data.screens for a in s.areas if a.type=='VIEW_3D']
    def tearDown(self):
        bpy.context.window.scene=self.original
        bpy.data.scenes.remove(self.scene,do_unlink=True)
        for m in set(bpy.data.materials)-self.materials:bpy.data.materials.remove(m,do_unlink=True)
        for g in set(bpy.data.node_groups)-self.groups:bpy.data.node_groups.remove(g,do_unlink=True)
        for shading,value in self.viewport:shading.use_compositor=value
        textures._PART_MATERIAL_CACHE.clear();textures._OBJECT_TINT_BUILTIN_CACHE.clear();textures._BAKED_BUILTIN_TINT_CACHE.clear()
    def root(self):return self.scene.compositing_node_group if hasattr(self.scene,'compositing_node_group') else self.scene.node_tree
    def managed(self):return [n for n in self.root().nodes if n.get('RBXNeonManaged')]
    def test_neon_is_unlit_and_exports_visible_radiance(self):
        mat=textures.build_part_material('Neon',{'name':'Neon','material':288,'color':[.5,.5,.5],'transparency':.5})
        p=textures._principled_node(mat)
        self.assertEqual(tuple(p.inputs['Base Color'].default_value[:3]),(0,0,0))
        self.assertFalse(p.inputs['Base Color'].links)
        self.assertEqual(p.inputs['Emission Strength'].default_value,NEON_RADIANCE)
        self.assertTrue(mat.use_backface_culling)
        self.assertEqual(mat.surface_render_method,'DITHERED')
        self.assertEqual(mat.node_tree.nodes['RBX Neon Radiance Pass'].aov_name,NEON_AOV)
        self.assertEqual(len([a for a in self.scene.view_layers[0].aovs if a.name==NEON_AOV]),1)
    def test_pipeline_is_idempotent_and_aov_is_linked(self):
        neon.configure_neon_compositor(self.scene,{})
        node=self.managed()[0];group=node.node_tree
        self.assertTrue(node.inputs['Neon'].links)
        neon.configure_neon_compositor(self.scene,{})
        self.assertEqual(len(self.managed()),1);self.assertIs(self.managed()[0].node_tree,group)
    def test_existing_compositor_links_are_preserved(self):
        tree,modern=neon._root_tree(self.scene)
        source=next(n for n in tree.nodes if n.type=='R_LAYERS')
        output=next(n for n in tree.nodes if n.type in ('GROUP_OUTPUT','COMPOSITE'))
        grade=tree.nodes.new('ShaderNodeGamma' if modern else 'CompositorNodeGamma');grade.name='User grading'
        tree.links.new(source.outputs['Image'],grade.inputs[0]);tree.links.new(grade.outputs[0],output.inputs[0])
        neon.configure_neon_compositor(self.scene,{})
        self.assertEqual(output.inputs[0].links[0].from_node.as_pointer(),grade.as_pointer())
        self.assertEqual(grade.inputs[0].links[0].from_node.as_pointer(),self.managed()[0].as_pointer())
        self.assertEqual(self.managed()[0].inputs['Image'].links[0].from_node.as_pointer(),source.as_pointer())
    def test_zero_intensity_has_no_blur_and_late_material_keeps_settings(self):
        fx={'bloom':{'enabled':True,'intensity':0,'size':0,'threshold':0}}
        neon.configure_neon_compositor(self.scene,fx)
        group=self.managed()[0].node_tree
        self.assertFalse(any(n.type=='BLUR' for n in group.nodes))
        textures.build_part_material('Late Neon',{'name':'Late','material':288,'color':[1,0,0]})
        self.assertIs(self.managed()[0].node_tree,group)
        self.assertEqual(json.loads(group['RBXNeonSettings'])[0]['intensity'],0)
    def test_viewport_and_resolution_use_the_same_image_domain(self):
        neon.configure_neon_compositor(self.scene,{})
        group=self.managed()[0].node_tree
        if bpy.app.version>=(5,0,0):
            self.assertIsNotNone(group.nodes.get('RBX Neon Image Dimensions'))
            self.assertTrue(group.nodes['RBX Bloom Blur 0'].inputs['Size'].links)
        self.assertTrue(all(shading.use_compositor=='ALWAYS' for shading,_ in self.viewport))
        self.assertTrue(self.scene.render.use_compositing)
    def test_exposure_driver_targets_its_scene(self):
        neon.configure_neon_compositor(self.scene,{})
        group=self.managed()[0].node_tree
        driver=group.animation_data.drivers[0].driver
        self.assertIs(driver.variables[0].targets[0].id,self.scene)
        self.assertEqual(driver.variables[0].targets[0].data_path,'view_settings.exposure')
