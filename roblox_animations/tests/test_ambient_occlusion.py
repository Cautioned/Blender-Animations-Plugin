"""Ordinary AO integration without extra UI or quality presets."""
import unittest
from types import SimpleNamespace
import bpy
from ..rig.ambient_occlusion import configure_ambient_occlusion
from ..operators import import_ops


class AmbientOcclusionTests(unittest.TestCase):
    def setUp(self):
        self.original=bpy.context.window.scene
        self.scene=bpy.data.scenes.new('Ambient occlusion regression');bpy.context.window.scene=self.scene
        self.scene.render.engine='BLENDER_EEVEE' if bpy.app.version>=(5,0,0) else 'BLENDER_EEVEE_NEXT'
        self.collection=bpy.data.collections.new('AO source');self.scene.collection.children.link(self.collection)
    def tearDown(self):
        bpy.context.window.scene=self.original
        for obj in list(self.collection.objects):
            data=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if data and data.users==0 and isinstance(data,bpy.types.Light):bpy.data.lights.remove(data)
        bpy.data.collections.remove(self.collection);bpy.data.scenes.remove(self.scene,do_unlink=True)
    def test_regular_ao_preserves_user_sampling_quality(self):
        ee=self.scene.eevee
        if not hasattr(ee,'use_raytracing'):self.skipTest('Uses legacy GTAO')
        ee.use_raytracing=False;ee.fast_gi_ray_count=7;ee.fast_gi_step_count=13
        ee.fast_gi_resolution='4';ee.fast_gi_quality=.7;ee.fast_gi_thickness_near=.3
        names=('fast_gi_ray_count','fast_gi_step_count','fast_gi_resolution','fast_gi_quality','fast_gi_thickness_near')
        before={n:getattr(ee,n) for n in names};objects=set(self.scene.objects)
        self.assertTrue(configure_ambient_occlusion(self.scene))
        self.assertTrue(ee.use_raytracing);self.assertTrue(ee.use_fast_gi)
        self.assertEqual(ee.fast_gi_method,'AMBIENT_OCCLUSION_ONLY')
        self.assertEqual(before,{n:getattr(ee,n) for n in names});self.assertEqual(objects,set(self.scene.objects))
        self.assertTrue(configure_ambient_occlusion(self.scene))
        self.assertEqual(before,{n:getattr(ee,n) for n in names})
    def test_import_configures_ordinary_ao_automatically(self):
        ee=self.scene.eevee
        if not hasattr(ee,'use_raytracing'):self.skipTest('Uses legacy GTAO')
        ee.use_raytracing=False
        import_ops._create_rbxl_global_lighting(self.collection,dict(lighting_style=0,brightness=0,global_shadows=True))
        self.assertTrue(ee.use_raytracing);self.assertEqual(ee.fast_gi_method,'AMBIENT_OCCLUSION_ONLY')
        self.assertTrue(self.scene.world['rbx_eevee_raytracing'])
    def test_source_disabled_shadows_disables_ao_and_can_resume(self):
        ee=self.scene.eevee
        if not hasattr(ee,'use_fast_gi'):self.skipTest('Uses legacy GTAO')
        configure_ambient_occlusion(self.scene,True)
        import_ops._create_rbxl_global_lighting(self.collection,dict(lighting_style=0,global_shadows=False))
        self.assertFalse(ee.use_fast_gi);self.assertFalse(self.scene.world['rbx_global_shadows'])
        import_ops._create_rbxl_global_lighting(self.collection,dict(lighting_style=0,global_shadows=True))
        self.assertTrue(ee.use_fast_gi)
    def test_cycles_is_unchanged(self):
        ee=self.scene.eevee;before=getattr(ee,'use_raytracing',None)
        self.scene.render.engine='CYCLES'
        self.assertFalse(configure_ambient_occlusion(self.scene))
        self.assertEqual(getattr(ee,'use_raytracing',None),before)
    def test_legacy_eevee_uses_standard_gtao(self):
        ee=SimpleNamespace(use_gtao=False,gtao_distance=8,gtao_factor=.7)
        scene=SimpleNamespace(render=SimpleNamespace(engine='BLENDER_EEVEE'),eevee=ee)
        self.assertTrue(configure_ambient_occlusion(scene));self.assertTrue(ee.use_gtao)
        self.assertEqual((ee.gtao_distance,ee.gtao_factor),(8,.7))
        self.assertFalse(configure_ambient_occlusion(scene,False));self.assertFalse(ee.use_gtao)
    def test_no_extra_toggle_or_state_is_registered(self):
        self.assertFalse(hasattr(self.scene.rbx_anim_settings,'rbx_shelter_shading'))
        configure_ambient_occlusion(self.scene)
        self.assertNotIn('RBXShelterShadingState',self.scene)
