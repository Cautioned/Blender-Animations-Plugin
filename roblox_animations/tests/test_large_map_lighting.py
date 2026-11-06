"""Large-map sky completion, persistence and lighting-cost regressions."""
import json,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
import bpy
from ..core.asset_pipeline import AssetRegistry,AssetState
from ..operators import import_ops
from ..rig import textures,local_lights

class SkyWorkerTests(unittest.TestCase):
    def setUp(self):
        self.ref='rbxassetid://908070605';self.key=textures._texture_asset_key(self.ref)
        textures._IMAGE_BYTES_CACHE.pop(self.key,None);textures._IMAGE_CACHE.pop(self.key,None)
        self.registry=AssetRegistry();self.registry.register(self.key,self.ref,'texture');self.registry.subscribe('sky',[self.key])
    def test_cache_write_failure_releases_sky_subscriber(self):
        with patch.object(textures,'_fetch_image_bytes',return_value=b'image'),patch.object(textures,'_ensure_image_temp_file',side_effect=OSError('disk failure')):
            textures.prefetch_texture_bytes([self.ref],auth_headers={},asset_registry=self.registry)
        self.assertEqual(self.registry.drain_ready_consumers(),['sky'])
        self.assertEqual(self.registry.get(self.key).state,AssetState.failed)
    def test_retry_is_on_worker_and_sky_gets_longer_timeout(self):
        import threading
        seen=[]
        def fetch(*args,**kwargs):
            seen.append((threading.get_ident(),kwargs['timeout']));return None if len(seen)==1 else b'image'
        with patch.object(textures,'_fetch_image_bytes',side_effect=fetch),patch.object(textures,'_ensure_image_temp_file'),patch.object(textures,'_warm_raw_pixels'):
            textures.prefetch_texture_bytes([self.ref],timeout=4,auth_headers={},raw_decode_refs=[self.ref],asset_registry=self.registry)
        self.assertEqual(len(seen),2);self.assertTrue(all(t!=threading.get_ident() and timeout>=15 for t,timeout in seen))
        self.assertEqual(self.registry.get(self.key).state,AssetState.ready)
    def test_optional_raw_decoder_failure_keeps_asset_ready(self):
        with patch.object(textures,'_fetch_image_bytes',return_value=b'image'),patch.object(textures,'_ensure_image_temp_file'),patch.object(textures,'_warm_raw_pixels',side_effect=RuntimeError('decoder unavailable')):
            textures.prefetch_texture_bytes([self.ref],auth_headers={},raw_decode_refs=[self.ref],asset_registry=self.registry)
        self.assertEqual(self.registry.get(self.key).state,AssetState.ready)
    def test_coordinator_failure_finishes_unreported_assets_once(self):
        self.assertEqual(self.registry.finish_pending('worker stopped'),1)
        self.assertEqual(self.registry.finish_pending('again'),0)
        self.assertEqual(self.registry.drain_ready_consumers(),['sky'])

    def test_failed_coordinator_cannot_strand_the_real_sky_tick(self):
        from concurrent.futures import Future
        from .test_lazy_queue import _deferred_state
        state=_deferred_state(True);state['jobs']=[]
        future=Future();future.set_exception(RuntimeError('coordinator failed'))
        state['texture_future']=future;state['sky_assets_ready']=False
        state['texture_assets'].register(self.key,self.ref,'texture')
        state['texture_assets'].subscribe(import_ops._SKY_ASSET_CONSUMER,[self.key])
        calls=[];state['post_texture_finalized']=False;state['post_texture_finalize']=lambda:calls.append('sky')
        prior=list(import_ops._LAZY_PLACE_IMPORTS);import_ops._LAZY_PLACE_IMPORTS[:]=[state]
        try:
            with patch.object(textures,'refresh_all_unhydrated_materials',return_value=0),patch.object(textures,'audit_unhydrated_materials',return_value=0):
                import_ops._process_lazy_place_imports()
            self.assertEqual(calls,['sky']);self.assertTrue(state['post_texture_finalized'])
            self.assertFalse(import_ops._LAZY_PLACE_IMPORTS)
        finally:import_ops._LAZY_PLACE_IMPORTS[:]=prior

class LargeMapLightingTests(unittest.TestCase):
    def setUp(self):
        self.collection=bpy.data.collections.new('LargeMapLightingTest');bpy.context.scene.collection.children.link(self.collection)
    def tearDown(self):
        for obj in list(self.collection.objects):
            data=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if data and data.users==0:
                (bpy.data.lights if isinstance(data,bpy.types.Light) else bpy.data.meshes).remove(data)
        bpy.data.collections.remove(self.collection)
    def test_large_maps_keep_every_source_within_helper_budget(self):
        part=dict(inst_ref=1,part_cf=[0,0,0,1,0,0,0,1,0,0,0,1],part_size=[6,5,4])
        lights=[dict(class_name='SpotLight',parent_ref=1,brightness=1,range=16,name='Lamp'+str(i)) for i in range(268)]
        self.assertEqual(local_lights.create_lights(self.collection,lights,[part]),268)
        self.assertEqual(len(self.collection.objects),1340)
        self.assertEqual(sum(bool(o.get('RBXLightSource')) for o in self.collection.objects),268)
        self.assertTrue(all(o.data.cutoff_distance<=16.001 for o in self.collection.objects))
    def test_compact_falloff_stays_close_to_native_envelope(self):
        import math
        errors=[]
        for i in range(30,1000):
            d=i/1000;actual=sum(p*max(0,1-(d/c)**4)**2/d**2 for c,p in local_lights.COMPACT_ENVELOPE)
            target=min(1,((1-d)/.25))**2;errors.append(abs(actual-target))
        self.assertLess(max(errors),.12)
        self.assertLess(math.sqrt(sum(x*x for x in errors)/len(errors)),.06)
    def test_showing_lighting_preserves_raytracing_quality(self):
        scene=bpy.context.scene;scene.render.engine='BLENDER_EEVEE';eevee=getattr(scene,'eevee',None)
        if eevee is None or not hasattr(eevee,'use_raytracing'):self.skipTest('No EEVEE settings')
        eevee.use_raytracing=True
        names=('fast_gi_resolution','fast_gi_ray_count','fast_gi_step_count','fast_gi_quality','use_fast_gi')
        before={n:getattr(eevee,n) for n in names if hasattr(eevee,n)}
        options=eevee.ray_tracing_options;options.resolution_scale='4';options.screen_trace_quality=.25
        import_ops._create_rbxl_global_lighting(self.collection,dict(clock_time=12,brightness=2))
        self.assertEqual(before,{n:getattr(eevee,n) for n in before})
        self.assertEqual(options.resolution_scale,'4');self.assertAlmostEqual(options.screen_trace_quality,.25)
    def test_generated_environment_is_packed_and_incomplete_sky_is_visible(self):
        image=bpy.data.images.new('KnownSkyFace',width=1,height=1);image.pixels[:]=(.4,.6,.8,1)
        def fetch(ref,**kwargs):return image if ref=='known' else None
        with patch.object(textures,'fetch_texture_image',side_effect=fetch),patch.object(textures,'prefetched_raw_pixels',return_value=None):
            sky=import_ops._create_rbxl_skybox(self.collection,{'right':'known','left':'missing'},[])
        env=bpy.data.images[sky['rbx_environment_image']]
        self.assertTrue(env.packed_file);self.assertEqual(sky['rbx_missing_sky_faces'],['left'])
        import numpy as np
        pixels=np.empty(len(env.pixels),dtype=np.float32);env.pixels.foreach_get(pixels)
        self.assertGreater(float(pixels.reshape(-1,4)[:,:3].max()),.1)
