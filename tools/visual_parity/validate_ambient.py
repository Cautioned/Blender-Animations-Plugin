"""Test a separate authored ambient fill; validate against all captures."""
import copy
import json
import math
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent))
import render
from compare import metrics,pixels
root=Path('build/visual_parity').resolve()
manifest=json.loads((root/'manifest.json').read_text())
production=render.import_ops._create_rbxl_global_lighting

def original(collection, lighting, *args, **kwargs):
    # Reproduce the pre-calibration branch without editing product files.
    return production(collection, dict(lighting, lighting_style=None), *args, **kwargs)

records=[]

def candidate(collection,lighting,*args,**kwargs):
    result=original(collection,lighting,*args,**kwargs)
    scene=render.bpy.context.scene;world=scene.world;nodes=world.node_tree.nodes
    ambient=lighting.get('ambient',[0,0,0]);outdoor=lighting.get('outdoor_ambient',ambient)
    if lighting.get('global_shadows',True):
        fill=[0.2*ambient[i]+0.8*max(ambient[i],outdoor[i]) for i in range(3)]
    else:fill=ambient
    fill=render.textures._linear_rgb(fill)
    day=max(0,math.sin(nodes['RBX Nishita Sky'].sun_elevation))
    diffuse=lighting.get('environment_diffuse_scale',1)
    color=[2.3*fill[i]+day*diffuse*(0.18,0.45,0.7)[i] for i in range(3)]
    bg=nodes['RBX Environment Lighting'];camera=nodes['RBX Sky Background']
    for link in list(bg.inputs['Color'].links):world.node_tree.links.remove(link)
    bg.inputs['Color'].default_value=(*color,1)
    bg.inputs['Strength'].default_value=1
    camera.inputs['Strength'].default_value=1
    path=nodes.new('ShaderNodeLightPath');mix=nodes.new('ShaderNodeMixShader')
    world.node_tree.links.new(path.outputs['Is Camera Ray'],mix.inputs[0])
    world.node_tree.links.new(bg.outputs['Background'],mix.inputs[1])
    world.node_tree.links.new(camera.outputs['Background'],mix.inputs[2])
    world.node_tree.links.new(mix.outputs[0],next(n for n in nodes if n.type=='OUTPUT_WORLD').inputs['Surface'])
    result.data.energy*=3
    scene.view_settings.view_transform='Standard';scene.view_settings.look='None'
    return result

render.import_ops._create_rbxl_global_lighting=candidate
for spec in manifest:
    meta=json.loads((root/'studio'/(spec['id']+'.json')).read_text())
    spec.update({key:meta[key] for key in ('width','height','camera_cf','fov')})
    spec['width']//=2;spec['height']//=2
    ref=pixels(root/'studio'/(spec['id']+'.png'))
    ref=np.rint(ref.reshape(spec['height'],2,spec['width'],2,4).mean(axis=(1,3))).astype(np.uint8)
    output=root/'validation_ambient';output.mkdir(parents=True,exist_ok=True)
    render.render_case(root,copy.deepcopy(spec),output,samples=32)
    row={'case':spec['id'],**metrics(ref,pixels(output/(spec['id']+'.png')),spec)}
    records.append(row)
    (root/'validation_ambient.json').write_text(json.dumps(records,indent=2)+'\n')
    print('AMBIENT '+json.dumps({k:v for k,v in row.items() if k!='probes'}),flush=True)
