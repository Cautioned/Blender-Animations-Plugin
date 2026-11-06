"""Warm-render AO cost on citytemplate bounds proxies, not full-map FPS."""
import bpy,time,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from bootstrap import load_addon_modules
load_addon_modules()
from roblox_animations.rig.ambient_occlusion import configure_ambient_occlusion
bpy.ops.wm.open_mainfile(filepath=str(Path('build/city_lighting/city_lighting_proxy.blend').resolve()))
scene=bpy.context.scene;scene.render.resolution_x=640;scene.render.resolution_y=360;scene.render.resolution_percentage=100
if hasattr(scene.eevee,'taa_render_samples'):scene.eevee.taa_render_samples=8
out=Path('/private/tmp/rbx_regular_ao_city_20261105');out.mkdir(exist_ok=True)
records=[]
for index,mode in enumerate(['off','regular','regular','off']):
    scene.eevee.use_raytracing=False
    if mode=='regular':configure_ambient_occlusion(scene,True)
    row={'mode':mode,'frames':[],'light_objects':sum(o.type=='LIGHT' for o in scene.objects),'vertices':sum(len(o.data.vertices) for o in scene.objects if o.type=='MESH')}
    for i in range(3):
        scene.render.filepath=str(out/(str(index)+'_'+mode+'_'+str(i)+'.png'))
        t=time.perf_counter();bpy.ops.render.render(write_still=True);row['frames'].append(time.perf_counter()-t)
    row['warm_seconds']=sum(row['frames'][1:])/2;records.append(row);print('REGULAR_AO_CITY',json.dumps(row),flush=True)
(out/'benchmark.json').write_text(json.dumps(records,indent=2)+'\n')
