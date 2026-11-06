"""Calibrate on grayscale patches without changing production defaults."""
import copy
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import render
from compare import metrics,pixels

root=Path('build/visual_parity').resolve()
base=json.loads((root/'manifest.json').read_text())[0]
meta=json.loads((root/'studio'/'01_daylight.json').read_text())
base.update({key:meta[key] for key in ('width','height','camera_cf','fov')})
# Half the retinal screenshot, retaining its exact aspect ratio.
base['width']//=2;base['height']//=2
ref=pixels(root/'studio'/'01_daylight.png')[::2,::2]
production=render.import_ops._create_rbxl_global_lighting

def original(collection, lighting, *args, **kwargs):
    # Reproduce the pre-calibration branch without editing product files.
    return production(collection, dict(lighting, lighting_style=None), *args, **kwargs)

records=[]
for transform in ['Khronos PBR Neutral','Standard']:
    for strength in [0.2,0.3,0.5,1.0]:
        def lighting(*args,**kwargs):
            result=original(*args,**kwargs)
            world=render.bpy.context.scene.world
            nodes=world.node_tree.nodes
            bg=nodes['RBX Environment Lighting']
            camera=nodes['RBX Sky Background']
            camera.inputs['Strength'].default_value=1.0
            bg.inputs['Strength'].default_value=strength
            path=nodes.new('ShaderNodeLightPath');mix=nodes.new('ShaderNodeMixShader')
            world.node_tree.links.new(path.outputs['Is Camera Ray'],mix.inputs[0])
            world.node_tree.links.new(bg.outputs['Background'],mix.inputs[1])
            world.node_tree.links.new(camera.outputs['Background'],mix.inputs[2])
            world.node_tree.links.new(mix.outputs[0],next(n for n in nodes if n.type=='OUTPUT_WORLD').inputs['Surface'])
            render.bpy.context.scene.view_settings.view_transform=transform
            render.bpy.context.scene.view_settings.look='None'
            return result
        render.import_ops._create_rbxl_global_lighting=lighting
        output=root/'calibration'/(transform.replace(' ','_')+'_'+str(strength))
        output.mkdir(parents=True,exist_ok=True)
        render.render_case(root,copy.deepcopy(base),output,samples=32)
        result=metrics(ref,pixels(output/'01_daylight.png'),base)
        records.append({'transform':transform,'world_strength':strength,**result})
        print('SWEEP '+json.dumps({k:v for k,v in records[-1].items() if k!='probes'}),flush=True)
(root/'calibration'/'sweep.json').write_text(json.dumps(records,indent=2)+'\n')
