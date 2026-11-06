"""Render a Studio-exported decal fixture through the actual import builders.

Run in Blender from the repository root. The reference RBXM is captured from
Studio, and diagnostic image payloads are cached locally to avoid HTTP drift.
"""
import sys,json,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from bootstrap import load_addon_modules
load_addon_modules()
import bpy
from mathutils import Vector,Matrix
from roblox_animations.core.rbxm import parse_rbxm
from roblox_animations.core.constants import get_transform_to_blender
from roblox_animations.rig import textures,primitive_shapes,filemesh,creation
from roblox_animations.operators import import_ops
root=Path('build/decal_wrap');meta=parse_rbxm((root/'reference.rbxm').read_bytes())
print('ENTRIES',json.dumps(meta['partAux']),flush=True)
for obj in list(bpy.data.objects):bpy.data.objects.remove(obj,do_unlink=True)
assets=Path('build/material_parity/assets')
for name,upload in json.loads((assets/'uploads.json').read_text()).items():
 textures._IMAGE_BYTES_CACHE[upload['usableUri']]=(assets/(name+'.png')).read_bytes()
collection=bpy.context.scene.collection
objects=[]
for entry in meta['partAux']:
 if entry.get('mesh_id'):
  data=filemesh.fetch_and_parse_filemesh(entry['mesh_id'])
  obj=creation._create_batched_filemesh_instances(collection,entry['name'],data,[entry])
 else:obj=primitive_shapes._create_batched_static_primitives(collection,entry['name'],[entry])
 assert obj is not None,entry['name']
 objects.append(obj)
 print('IMPORTED',obj.name,len(obj.data.vertices),[(m.name,[n.name for n in m.node_tree.nodes if n.type=='TEX_IMAGE']) for m in obj.data.materials],flush=True)
assert import_ops._create_rbx_decals(collection,meta['partAux'])==0
assert not any(o.get('RBXDecal') for o in bpy.data.objects)
scene=bpy.context.scene;scene.render.engine='BLENDER_EEVEE'
scene.render.resolution_x=1200;scene.render.resolution_y=600;scene.render.resolution_percentage=100
scene.view_settings.view_transform='Standard';scene.world.color=(.35,.35,.35)
basis=get_transform_to_blender();position=basis @ Vector((0,10,965));target=basis @ Vector((0,6,1000))
cam=bpy.data.objects.new('DecalCamera',bpy.data.cameras.new('DecalCamera'));collection.objects.link(cam)
cam.location=position;cam.rotation_euler=(target-position).to_track_quat('-Z','Y').to_euler()
cam.data.lens=cam.data.sensor_width/(2*math.tan(math.radians(50)/2)*2);scene.camera=cam
light=bpy.data.objects.new('DecalLight',bpy.data.lights.new('DecalLight','AREA'));collection.objects.link(light)
light.location=basis @ Vector((0,18,980));light.rotation_euler=(target-light.location).to_track_quat('-Z','Y').to_euler();light.data.energy=2500;light.data.shape='DISK';light.data.size=25
scene.render.filepath=str((root/'wrapped.png').resolve());bpy.ops.render.render(write_still=True)
for image in bpy.data.images:
 if image.has_data and image.source!='VIEWER':image.pack()
bpy.ops.wm.save_as_mainfile(filepath=str((root/'wrapped.blend').resolve()),compress=True)
print('DECAL_RENDER_OK',flush=True)
