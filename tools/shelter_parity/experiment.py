"""Compare native EEVEE world visibility probes and real-time GI controls."""
import sys,json,time,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
import render
import bpy,numpy as np
from mathutils import Vector
ROOT=Path('build/visual_parity').resolve()
OUT=Path('/private/tmp/rbx_shelter_experiment_20261105');OUT.mkdir(exist_ok=True)
manifest=json.loads((ROOT/'manifest.json').read_text())
records=[]
mode=sys.argv[sys.argv.index('--')+1] if '--' in sys.argv else 'visibility'

def prepare(scene,collection,meta):
    if mode!='product': scene.eevee.use_raytracing=False
    if mode=='baseline':return
    if mode=='product':
        return  # Production import already enables ordinary screen-space AO.
    if mode.startswith('fast'):
        scene.eevee.use_raytracing=True;scene.eevee.use_fast_gi=True
        scene.eevee.fast_gi_method='GLOBAL_ILLUMINATION' if mode=='fast_gi' else 'AMBIENT_OCCLUSION_ONLY'
        scene.eevee.fast_gi_resolution='2';scene.eevee.fast_gi_ray_count=2;scene.eevee.fast_gi_step_count=8
        if mode=='fast_detail':
            scene.eevee.fast_gi_ray_count=4;scene.eevee.fast_gi_step_count=16
            scene.eevee.fast_gi_quality=.5;scene.eevee.fast_gi_thickness_near=1.0
        return
    points=[obj.matrix_world @ Vector(p) for obj in collection.all_objects if obj.type=='MESH' for p in obj.bound_box]
    low=Vector(tuple(min(p[i] for p in points) for i in range(3)));high=Vector(tuple(max(p[i] for p in points) for i in range(3)))
    low-=Vector((.5,.5,.5));high+=Vector((.5,.5,2))
    data=bpy.data.lightprobes.new('Shelter test','VOLUME');obj=bpy.data.objects.new('Shelter test',data);collection.objects.link(obj)
    obj.location=(low+high)/2;obj.scale=(high-low)/2
    for axis,resolution in zip('xyz',[24,20,12] if mode=='dense' else [12,10,6]):setattr(data,'resolution_'+axis,resolution)
    data.surfel_density=4;data.bake_samples=128
    data.capture_world=mode in ('radiance','dense');data.capture_indirect=mode in ('radiance','dense');data.capture_emission=False
    data.capture_distance=30;data.normal_bias=.15;data.facing_bias=.25
    bpy.context.view_layer.objects.active=obj;obj.select_set(True)
    started=time.perf_counter();result=bpy.ops.object.lightprobe_cache_bake(subset='ACTIVE')
    print('SHELTER_BAKE',mode,result,time.perf_counter()-started,flush=True)
    records.append({'case':spec['id'],'mode':mode,'bake_seconds':time.perf_counter()-started})

bpy.ops.wm.read_factory_settings(use_empty=True)
for original in manifest:
    if mode!='product' and original['id'] not in ['10_indoor','01_daylight']:continue
    spec=dict(original);capture=json.loads((ROOT/'studio'/(spec['id']+'.json')).read_text())
    spec.update({k:capture[k] for k in ['width','height','camera_cf','fov']});spec['width']//=2;spec['height']//=2
    output=OUT/mode;output.mkdir(exist_ok=True)
    render.render_case(ROOT,spec,output,32,save_blend=mode=='visibility',prepare_scene=prepare)
(OUT/(mode+'_bakes.json')).write_text(json.dumps(records,indent=2))
