"""Render baseline/optimized shader graphs and compare output pixels.

Run inside Blender from the repo root. Baseline functions are retained in
build/node_audit/baseline_source/textures.py for this local comparison.
"""
import sys,ast,math,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from bootstrap import load_addon_modules
load_addon_modules()
import bpy,numpy as np
from roblox_animations.rig import textures
root=Path('build/node_audit')
original={n:getattr(textures,n) for n in ('_bind_surface_image_uv','_bind_independent_layer_alpha')}
source=ast.parse((root/'baseline_source/textures.py').read_text())
baseline=ast.Module(body=[n for n in source.body if isinstance(n,ast.FunctionDef) and n.name in original],type_ignores=[])
for obj in list(bpy.data.objects):bpy.data.objects.remove(obj,do_unlink=True)
scene=bpy.context.scene;scene.render.engine='BLENDER_EEVEE';scene.render.resolution_x=640;scene.render.resolution_y=320;scene.render.resolution_percentage=100
scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_mode='RGBA';scene.view_settings.view_transform='Standard';scene.view_settings.look='None';scene.view_settings.exposure=0
if hasattr(scene,'eevee') and hasattr(scene.eevee,'taa_render_samples'):scene.eevee.taa_render_samples=32
world=bpy.data.worlds.new('ShaderComparisonWorld');world.use_nodes=True;world.node_tree.nodes['Background'].inputs['Color'].default_value=(.3,.3,.3,1);scene.world=world
camera=bpy.data.objects.new('ShaderComparisonCamera',bpy.data.cameras.new('ShaderComparisonCamera'));scene.collection.objects.link(camera);camera.location=(0,0,8);camera.data.type='ORTHO';camera.data.ortho_scale=5;scene.camera=camera
light=bpy.data.objects.new('ShaderComparisonLight',bpy.data.lights.new('ShaderComparisonLight','AREA'));scene.collection.objects.link(light);light.location=(0,0,5);light.data.energy=250;light.data.size=5
# Asymmetric RGBA diagnostics expose orientation, clipping and compositing.
images={}
for index in range(3):
 size=64;y,x=np.mgrid[:size,:size];pixels=np.ones((size,size,4),dtype=np.float32)
 pixels[:,:,0]=((x//8+index)%3)/2;pixels[:,:,1]=y/(size-1);pixels[:,:,2]=1-x/(size-1)
 pixels[:,:,3]=((x//8+y//8+index)%4)/4
 image=bpy.data.images.new('Layer'+str(index),width=size,height=size,alpha=True)
 image.colorspace_settings.name='sRGB';image.pixels.foreach_set(pixels.reshape(-1));image.update();images[str(index)]=image
# Checker behind translucent panels makes coverage changes visible.
mesh=bpy.data.meshes.new('Checker');mesh.from_pydata([(-4,-3,-.1),(4,-3,-.1),(4,3,-.1),(-4,3,-.1)],[],[(0,1,2,3)]);obj=bpy.data.objects.new('Checker',mesh);scene.collection.objects.link(obj)
mat=bpy.data.materials.new('Checker');mat.use_nodes=True;checker=mat.node_tree.nodes.new('ShaderNodeTexChecker');checker.inputs['Scale'].default_value=16;mat.node_tree.links.new(checker.outputs['Color'],mat.node_tree.nodes['Principled BSDF'].inputs['Base Color']);mesh.materials.append(mat)
cases=[]
for index,(name,transparent,rotate) in enumerate((('One',False,False),('Three',False,False),('Transparent',True,False),('Rotated',True,True))):
 x=(index%2-.5)*2.35;y=(.5-index//2)*1.2
 mesh=bpy.data.meshes.new(name);mesh.from_pydata([(x-1,y-.5,0),(x+1,y-.5,0),(x+1,y+.5,0),(x-1,y+.5,0)],[],[(0,1,2,3)])
 uv=mesh.uv_layers.new(name='RBXSurfaceUV.5');uv.data.foreach_set('uv',(0,5,6,5,6,0,0,0))
 obj=bpy.data.objects.new(name,mesh);scene.collection.objects.link(obj)
 entry=dict(name=name,class_name='MeshPart',mesh_id='mesh',part_size=[6,5,4],color=[.3,.5,.7],material=272,transparency=.65 if transparent else 0)
 entry['decals']=[dict(texture=str(i),face=5,color=[.9,1,.8],transparency=.2+i*.15,
     rotation=(45+i*15) if rotate else 0,uv_scale=[.7,1.2] if rotate else [1,1],uv_offset=[.15,-.2] if rotate else [0,0]) for i in range(1 if name=='One' else 3)]
 textures._ensure_builtin_material_color(mesh,entry['color']);cases.append((obj,entry))
report={}
for label in ('before','after'):
 if label=='before':exec(compile(baseline,'baseline_surface_nodes','exec'),textures.__dict__)
 else:
  for name,func in original.items():setattr(textures,name,func)
 with patch.object(textures,'fetch_texture_image',side_effect=lambda ref,**kw:images[str(ref)]):
  for obj,entry in cases:
   material=textures.build_part_material(label+'_'+obj.name,entry);obj.data.materials.clear();obj.data.materials.append(material)
 report[label]={obj.name:sum(n.type!='FRAME' for n in obj.data.materials[0].node_tree.nodes) for obj,_ in cases}
 scene.render.filepath=str((root/(label+'.png')).resolve());bpy.ops.render.render(write_still=True)
a=bpy.data.images.load(str((root/'before.png').resolve()),check_existing=False);b=bpy.data.images.load(str((root/'after.png').resolve()),check_existing=False)
pa=np.empty(len(a.pixels),dtype=np.float32);pb=np.empty(len(b.pixels),dtype=np.float32);a.pixels.foreach_get(pa);b.pixels.foreach_get(pb)
diff=np.abs(pa-pb);report['max_pixel_error']=float(diff.max());report['mean_pixel_error']=float(diff.mean());report['changed_channels']=int(np.count_nonzero(diff))
(root/'render_comparison.json').write_text(json.dumps(report,indent=2)+'\n');print('NODE_RENDER_COMPARISON',json.dumps(report),flush=True)
assert report['max_pixel_error']<=2/255,report
for image in images.values():image.pack()
bpy.ops.wm.save_as_mainfile(filepath=str((root/'simplified_materials.blend').resolve()),compress=True)
