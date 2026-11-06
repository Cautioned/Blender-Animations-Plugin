"""Validate an experimental sky/display fit on every captured scenario."""
import copy
import json
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

def candidate(*args,**kwargs):
    result=original(*args,**kwargs)
    scene=render.bpy.context.scene;world=scene.world;nodes=world.node_tree.nodes
    bg=nodes['RBX Environment Lighting'];camera=nodes['RBX Sky Background']
    bg.inputs['Strength'].default_value=0.2
    camera.inputs['Strength'].default_value=1.0
    path=nodes.new('ShaderNodeLightPath');mix=nodes.new('ShaderNodeMixShader')
    world.node_tree.links.new(path.outputs['Is Camera Ray'],mix.inputs[0])
    world.node_tree.links.new(bg.outputs['Background'],mix.inputs[1])
    world.node_tree.links.new(camera.outputs['Background'],mix.inputs[2])
    world.node_tree.links.new(mix.outputs[0],next(n for n in nodes if n.type=='OUTPUT_WORLD').inputs['Surface'])
    scene.view_settings.view_transform='Standard';scene.view_settings.look='None'
    return result

for spec in manifest:
    metadata=root/'studio'/(spec['id']+'.json')
    if not metadata.exists():continue
    meta=json.loads(metadata.read_text())
    spec.update({key:meta[key] for key in ('width','height','camera_cf','fov')})
    spec['width']//=2;spec['height']//=2
    reference=pixels(root/'studio'/(spec['id']+'.png'))
    reference=np.rint(reference.reshape(spec['height'],2,spec['width'],2,4).mean(axis=(1,3))).astype(np.uint8)
    row={'case':spec['id']}
    for name,fn in [('current',original),('candidate',candidate)]:
        render.import_ops._create_rbxl_global_lighting=fn
        output=root/('validation_'+name);output.mkdir(parents=True,exist_ok=True)
        render.render_case(root,copy.deepcopy(spec),output,samples=32)
        row[name]=metrics(reference,pixels(output/(spec['id']+'.png')),spec)
    records.append(row)
    (root/'validation.json').write_text(json.dumps(records,indent=2)+'\n')
    print('VALIDATE '+json.dumps({'case':row['case'],**{name:round(row[name]['probe_mae_rgb_255'],2) for name in ('current','candidate')}}),flush=True)
