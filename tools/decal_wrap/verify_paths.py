"""Compare and render the captured all-face Studio decal reference in Blender."""
import sys,json,math
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tools/visual_parity'))
from bootstrap import load_addon_modules
load_addon_modules()
import bpy,numpy as np
from mathutils import Vector
from roblox_animations.core.rbxm import parse_rbxm
from roblox_animations.core.constants import get_transform_to_blender
from roblox_animations.rig import textures,primitive_shapes,filemesh,creation
root=Path('build/decal_paths');meta=parse_rbxm((root/'reference_all.rbxm').read_bytes());G=json.loads((root/'native_all_groups.json').read_text())
filemesh._FILEMESH_BYTES_CACHE['http://www.roblox.com/asset/?id=1660648424']=Path('roblox_animations/tests/fixtures/decal_meshpart.mesh').read_bytes()
assets=Path('build/material_parity/assets')
for name,upload in json.loads((assets/'uploads.json').read_text()).items():textures._IMAGE_BYTES_CACHE[upload['usableUri']]=(assets/(name+'.png')).read_bytes()
lookup={k.lower():v for k,v in G.items()};basis=get_transform_to_blender();inverse=basis.inverted()
results=[]
for entry in meta['partAux']:
 name=entry['name'];group=lookup.get(name.lower()+'1')
 if group is None:continue
 if entry.get('union_mesh'):
  data=dict(entry['union_mesh'],embedded_union=True)
  obj=creation._create_mesh_object_from_filemesh(bpy.context.scene.collection,name,data,entry)
 elif entry.get('mesh_id'):
  data=filemesh.fetch_and_parse_filemesh(entry['mesh_id']);obj=creation._create_batched_filemesh_instances(bpy.context.scene.collection,name,data,[entry])
 else:obj=primitive_shapes._create_batched_static_primitives(bpy.context.scene.collection,name,[entry])
 assert obj is not None,name
 obj.data.update()
 points=np.asarray([tuple(inverse @ (obj.matrix_world @ v.co)) for v in obj.data.vertices]);native=np.asarray(group['v']);dist=((points[:,None,:]-native[None,:,:])**2).sum(axis=2)
 geometry=float(np.sqrt(dist.min(axis=1)).max());reverse=float(np.sqrt(dist.min(axis=0)).max())
 face=int(entry['decals'][0].get('face',5));decal=lookup[name.lower()+'2'];dpoints=np.asarray(decal['v']);duv=np.asarray(decal['vt']);nearest=((dpoints[:,None,:]-points[None,:,:])**2).sum(axis=2).argmin(axis=1)
 # Canonicalize duplicate mesh positions before comparing triangle coverage.
 canonical=((points[:,None,:]-points[None,:,:])**2).sum(axis=2).argmin(axis=1)
 dfaces=[[c[0]-decal['offset'][0] for c in t] for t in decal['f']]
 expected={tuple(sorted(int(canonical[nearest[i]]) for i in t)) for t in dfaces if len(set(int(canonical[nearest[i]]) for i in t))==3}
 actual=set();uv_errors=[]
 size=entry['part_size'];ua,va={0:(2,1),1:(0,2),2:(0,1),3:(2,1),4:(0,2),5:(0,1)}[face]
 for poly in obj.data.polygons:
  material=obj.data.materials[poly.material_index]
  tex=next((n for n in material.node_tree.nodes if n.name.startswith('RBX TextureInstance')),None)
  if tex is None:continue
  actual.add(tuple(sorted(int(canonical[i]) for i in poly.vertices)))
  uv_layer=(obj.data.attributes[f'RBXDecalUV.{face}'] if entry.get('union_mesh') else obj.data.uv_layers['UVMap' if textures._uses_native_decal_uv(entry) else f'RBXSurfaceUV.{face}'])
  for i in poly.loop_indices:
   vi=obj.data.loops[i].vertex_index
   distances=((dpoints-points[vi])**2).sum(axis=1)
   candidates=np.flatnonzero(distances<=distances.min()+1e-7)
   uv=uv_layer.data[i].vector if entry.get('union_mesh') else uv_layer.data[i].uv
   sampled=tuple(uv) if textures._uses_native_decal_uv(entry) else (uv[0]/size[ua],1-uv[1]/size[va])
   uv_errors.append(min(max(abs(sampled[k]-duv[dn][k]) for k in (0,1)) for dn in candidates))
 result=dict(name=name,geometry_error=max(geometry,reverse),native_triangles=len(expected),imported_triangles=len(actual),coverage_difference=len(expected^actual),uv_error=max(uv_errors or [0]))
 results.append(result);print('DECAL_PATH',json.dumps(result),flush=True)
(root/'comparison.json').write_text(json.dumps(results,indent=2)+'\n')

# Render the native union close-up used for the Studio visual check.
scene=bpy.context.scene
for obj in list(bpy.data.objects):
    if obj.type=='MESH' and obj.name!='AuditUnionFace0':obj.hide_render=True
scene.render.engine='BLENDER_EEVEE';scene.render.resolution_x=960;scene.render.resolution_y=720;scene.render.resolution_percentage=100
scene.view_settings.view_transform='Standard';scene.world.color=(.4,.4,.4)
position=basis @ Vector((-12,13,207));target=basis @ Vector((-22.5,7,220))
cam=bpy.data.objects.new('DecalPathsCamera',bpy.data.cameras.new('DecalPathsCamera'));scene.collection.objects.link(cam)
cam.location=position;cam.rotation_euler=(target-position).to_track_quat('-Z','Y').to_euler()
cam.data.lens=cam.data.sensor_width/(2*math.tan(math.radians(50)/2)*960/720);scene.camera=cam
light=bpy.data.objects.new('DecalPathsLight',bpy.data.lights.new('DecalPathsLight','AREA'));scene.collection.objects.link(light)
light.location=basis @ Vector((-12,22,207));light.rotation_euler=(target-light.location).to_track_quat('-Z','Y').to_euler();light.data.energy=1100;light.data.size=15
scene.render.filepath=str((root/'union_wrapped.png').resolve());bpy.ops.render.render(write_still=True)
for image in bpy.data.images:
    if image.has_data and image.source!='VIEWER':image.pack()
bpy.ops.wm.save_as_mainfile(filepath=str((root/'checked_paths.blend').resolve()),compress=True)
assert all(r['geometry_error']<.001 and r['coverage_difference']==0 and r['uv_error']<1e-4 for r in results),results
print('ALL_NATIVE_DECAL_PATHS_MATCH',len(results),flush=True)
