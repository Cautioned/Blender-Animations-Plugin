"""Offline citytemplate sky persistence and bounded-light proxy benchmark.

Requires sky_<face>.png payloads captured with the addon's authenticated fetch.
Uses source part bounds as geometry proxies; this is not a visual-parity test.
"""
import sys,json,time,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from bootstrap import load_addon_modules
load_addon_modules()
import bpy,numpy as np
from mathutils import Vector
from roblox_animations.core.rbxm import parse_rbxm
from roblox_animations.core.constants import get_transform_to_blender
from roblox_animations.operators import import_ops
from roblox_animations.rig import textures,local_lights
root=Path('build/city_lighting');meta=parse_rbxm(Path('citytemplate.rbxl').read_bytes());sky=meta['scene_sky']
for face,ref in sky.items():textures._IMAGE_BYTES_CACHE[textures._texture_asset_key(ref)]=(root/('sky_'+face+'.png')).read_bytes()
for o in list(bpy.data.objects):bpy.data.objects.remove(o,do_unlink=True)
scene=bpy.context.scene;scene.render.engine='BLENDER_EEVEE'
scene.render.resolution_x=640;scene.render.resolution_y=360;scene.render.resolution_percentage=100
if hasattr(scene,'eevee') and hasattr(scene.eevee,'taa_render_samples'):scene.eevee.taa_render_samples=8
collection=bpy.data.collections.new('CityBoundsProxies');scene.collection.children.link(collection)
lights=bpy.data.collections.new('CityLocalLights');scene.collection.children.link(lights)
sky_meta=import_ops._create_rbxl_skybox(collection,sky,meta['partAux']);assert sky_meta
sun=import_ops._create_rbxl_global_lighting(collection,meta['scene_lighting'],sky_meta,meta.get('scene_atmosphere'),meta.get('scene_post_effects'))
# Batch axis-aligned boxes through each source CFrame; no private meshes/maps.
cube=np.array([(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)],dtype=np.float32)*.5
faces0=np.array([(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)],dtype=np.int32)
parts=[e for e in meta['partAux'] if e.get('part_cf') and float(e.get('transparency',0))<1]
positions=[];faces=[];basis=get_transform_to_blender()
for e in parts:
 cf=e['part_cf'];rotation=np.asarray(cf[3:]).reshape(3,3);p=(cube*np.asarray(e['part_size']))@rotation.T+np.asarray(cf[:3]);p=p@np.asarray(basis.to_3x3()).T+np.asarray(basis.translation)
 faces.append(faces0+8*len(positions));positions.append(p)
from roblox_animations.rig.mesh_surface import _populate_mesh_geometry
mesh=bpy.data.meshes.new('CityBoundsProxies');_populate_mesh_geometry(mesh,np.concatenate(positions),np.concatenate(faces));obj=bpy.data.objects.new('CityBoundsProxies',mesh);collection.objects.link(obj)
material=bpy.data.materials.new('CityProxyGrey');material.use_nodes=True;material.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value=(.3,.3,.3,1);mesh.materials.append(material)
points=np.concatenate(positions);low=points.min(axis=0);high=points.max(axis=0);center=Vector((low+high)*.5);extent=float(max(high-low))
camera=bpy.data.objects.new('CityBenchmarkCamera',bpy.data.cameras.new('CityBenchmarkCamera'));scene.collection.objects.link(camera)
camera.location=center+Vector((extent*.55,-extent*.65,extent*.6));camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler();camera.data.lens=30;camera.data.clip_end=extent*5;scene.camera=camera
report={'proxy_parts':len(parts),'source_parts':len(meta['partAux']),'sky_packed':bool(bpy.data.images[sky_meta['rbx_environment_image']].packed_file),'results':[]}
for quality in ('accurate','auto'):
 t=time.perf_counter();count=local_lights.create_lights(lights,meta['scene_lights'],meta['partAux'],quality=quality);build=time.perf_counter()-t
 t=time.perf_counter();bpy.context.view_layer.update();update=time.perf_counter()-t
 row=dict(quality=quality,sources=count,lamps=len(lights.objects),build_seconds=build,update_seconds=update,frames=[])
 for frame in range(2):
  scene.render.filepath=str((root/f'{quality}_{frame}.png').resolve());t=time.perf_counter();bpy.ops.render.render(write_still=True);row['frames'].append(time.perf_counter()-t)
 report['results'].append(row);print('CITY_BENCHMARK',json.dumps(row),flush=True)
 if quality=='accurate':
  for light in list(lights.objects):
   data=light.data;bpy.data.objects.remove(light,do_unlink=True)
   if data.users==0:bpy.data.lights.remove(data)
image=bpy.data.images[sky_meta['rbx_environment_image']];before=list(image.pixels[:4]);name=image.name
path=str((root/'city_lighting_proxy.blend').resolve());bpy.ops.wm.save_as_mainfile(filepath=path,compress=True);bpy.ops.wm.open_mainfile(filepath=path)
image=bpy.data.images[name];after=list(image.pixels[:4]);report['sky_before_save']=before;report['sky_after_reopen']=after;report['sky_persist_max_error']=max(abs(a-b) for a,b in zip(before,after));assert report['sky_persist_max_error']<1e-4
(root/'verification.json').write_text(json.dumps(report,indent=2)+'\n');print('CITY_VERIFY_OK',json.dumps(report),flush=True)
