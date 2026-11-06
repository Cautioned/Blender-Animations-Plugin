"""Render real importer output in EEVEE from generated binary fixtures.

blender -b --factory-startup --python-exit-code 1 --python tools/visual_parity/render.py -- --cases 01_daylight
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootstrap import load_addon_modules
load_addon_modules()

import bpy
from roblox_animations.core.rbxm import parse_rbxm
from roblox_animations.core.constants import get_transform_to_blender
from roblox_animations.core.utils import cf_to_mat
from roblox_animations.rig import creation, primitive_shapes, textures
from roblox_animations.operators import import_ops


def render_case(root, spec, output, samples=64, save_blend=False, prepare_scene=None):
    # Start with an empty scene; retain image caches across cases.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    scene=bpy.context.scene
    try:
        scene.render.engine='BLENDER_EEVEE'
    except TypeError:
        scene.render.engine='BLENDER_EEVEE_NEXT'
    if hasattr(scene, 'eevee'):
        for attr in ('taa_render_samples', 'taa_samples'):
            if hasattr(scene.eevee, attr):
                setattr(scene.eevee, attr, samples)
    scene.render.resolution_x=spec['width']
    scene.render.resolution_y=spec['height']
    scene.render.resolution_percentage=100
    scene.render.image_settings.file_format='PNG'
    scene.render.image_settings.color_mode='RGB'
    collection=bpy.data.collections.new('ParityFixture')
    scene.collection.children.link(collection)
    meta=parse_rbxm((root/(spec['id']+'.rbxl')).read_bytes())
    reference_materials=root/'reference_materials.json'
    if reference_materials.exists():
        meta['use_2022_materials']=json.loads(reference_materials.read_text())['use_2022_materials']
    for entry in meta['partAux']:
        entry['_use_2022_materials']=meta.get('use_2022_materials', True)
    # Collect all texture refs before graph construction, so parallel fetch
    # shares payloads across the normal material/hydration code paths.
    refs=[]
    for entry in meta['partAux']:
        refs.extend(textures.material_texture_refs(entry))
    textures.prefetch_texture_bytes(list(dict.fromkeys(refs)))
    for entry in meta['partAux']:
        obj=primitive_shapes._create_batched_static_primitives(collection,entry['name'],[entry])
        if obj is None:
            raise RuntimeError('Failed to build '+entry['name'])
    import_ops._create_rbxl_global_lighting(
        collection,meta.get('scene_lighting'),atmosphere=meta.get('scene_atmosphere'),
        post_effects=meta.get('scene_post_effects'))
    import_ops._create_rbxl_lights(collection,meta.get('scene_lights'),meta['partAux'])
    camera=bpy.data.cameras.new('ParityCamera')
    camera.type='PERSP'
    # Roblox FieldOfView is vertical. Blender's horizontal sensor width
    # corresponds to that angle after multiplying by the aspect ratio.
    camera.sensor_fit='HORIZONTAL'
    aspect=spec['width']/spec['height']
    camera.lens=camera.sensor_width/(2*math.tan(math.radians(spec['fov'])/2)*aspect)
    cam=bpy.data.objects.new('ParityCamera',camera)
    collection.objects.link(cam)
    cam.matrix_world=get_transform_to_blender() @ cf_to_mat(spec['camera_cf'])
    scene.camera=cam
    if prepare_scene is not None:
        prepare_scene(scene, collection, meta)
    started=time.perf_counter()
    path=output/(spec['id']+'.png')
    scene.render.filepath=str(path)
    bpy.ops.render.render(write_still=True)
    missing=[]
    for mat in bpy.data.materials:
        if mat.use_nodes:
            missing.extend(f'{mat.name}/{n.name}' for n in mat.node_tree.nodes
                           if n.type=='TEX_IMAGE' and n.image is None)
    result={'case':spec['id'],'blender':bpy.app.version_string,'engine':scene.render.engine,
            'resolution':[spec['width'],spec['height']], 'seconds':time.perf_counter()-started,
            'view_transform':scene.view_settings.view_transform,'look':scene.view_settings.look,
            'exposure':scene.view_settings.exposure,'missing_images':missing,
            'use_2022_materials':meta.get('use_2022_materials'), 'parts':len(meta['partAux']),'lights':len(meta.get('scene_lights',[])),
            'camera_cf':spec['camera_cf'],'fov':spec['fov']}
    (output/(spec['id']+'.json')).write_text(json.dumps(result,indent=2)+'\n')
    if save_blend:
        for image in bpy.data.images:
            if image.source != 'VIEWER' and image.has_data and not image.packed_file:
                image.pack()
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.spaces.active.shading.type = 'RENDERED'
                    area.spaces.active.shading.use_scene_world_render = True
                    area.spaces.active.shading.use_scene_lights_render = True
                    area.spaces.active.region_3d.view_perspective = 'CAMERA'
        bpy.ops.wm.save_as_mainfile(filepath=str(output/(spec['id']+'.blend')),compress=True)
    print('PARITY_RENDER '+json.dumps(result),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path('build/visual_parity'))
    parser.add_argument('--cases',nargs='*')
    parser.add_argument('--samples',type=int,default=64)
    parser.add_argument('--scale',type=float,default=1.0)
    parser.add_argument('--save-blend',action='store_true')
    parser.add_argument('--output',default='blender')
    args=parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else [])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if not 0 < args.scale <= 1:
        parser.error('--scale must be in (0,1]')
    root=args.root.resolve()
    output=root/args.output
    output.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((root/'manifest.json').read_text())
    for spec in manifest:
        reference=root/'studio'/(spec['id']+'.json')
        if reference.exists():
            capture=json.loads(reference.read_text())
            for key in ('width','height','camera_cf','fov'):
                spec[key]=capture[key]
        if args.cases and spec['id'] not in args.cases:
            continue
        spec['width']=max(1,round(spec['width']*args.scale))
        spec['height']=max(1,round(spec['height']*args.scale))
        render_case(root,spec,output,args.samples,save_blend=args.save_blend)


if __name__=='__main__':
    main()

