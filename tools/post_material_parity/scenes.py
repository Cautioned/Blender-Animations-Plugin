"""Generate a separate, explicitly 2022-material Studio verification place."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
import scenes


def generate(root):
    root.mkdir(parents=True,exist_ok=True)
    specs=[scenes.fixture(scenes.CASES[i]) for i in (0,14)]
    bloom=scenes.fixture({'id':'17_scene_bloom',
        'description':'Explicit BloomEffect on ordinary highlights and Neon'})
    bloom['nodes'].append(scenes.node('BloomEffect',1,Name=['string','SceneBloom'],
        Enabled=['bool',True],Intensity=['float',.5],Size=['float',24],Threshold=['float',.5]))
    specs.append(bloom)
    template=(Path(__file__).resolve().parents[1]/'visual_parity/studio_case.luau').read_text()
    for spec in specs:
        (root/(spec['id']+'.rbxl')).write_bytes(scenes.binary(spec))
        # Native serialized property is Use2022MaterialsXml, not its runtime name.
        (root/(spec['id']+'.rbxlx')).write_bytes(scenes.xml(spec))
        (root/(spec['id']+'.luau')).write_text(template.replace('__SCENE_JSON__',json.dumps(spec)))
    (root/'manifest.json').write_text(json.dumps(specs,indent=2)+'\n')


if __name__=='__main__':generate(Path('build/post_material_parity'))
