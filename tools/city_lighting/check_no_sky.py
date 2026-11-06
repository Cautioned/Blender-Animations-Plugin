"""Exercise the real place importer with no Sky and a failed optional rig step.

Uses the addon's existing login, just as a normal operator invocation does.
All geometry and lighting are generated locally; no imported asset downloads.
"""
import sys,time
from pathlib import Path
from unittest.mock import patch
root=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tools/visual_parity'))
from scenes import binary,node,part
import bpy
import roblox_animations
roblox_animations.register()
from roblox_animations.operators import import_ops
fixture={'nodes':[node('Workspace',-1,Name=['string','Workspace']),
    node('Lighting',-1,Name=['string','Lighting'],ClockTime=['float',14],Brightness=['float',2],LightingStyle=['token',0]),
    part('BoxA',[0,0,0],[4,2,3],[150,150,150],272),part('BoxB',[6,0,0],[4,2,3],[150,150,150],272)]}
path=root/'build/city_lighting/no_sky.rbxl';path.write_bytes(binary(fixture))
prior=bpy.context.scene.world
with patch.object(import_ops,'_create_rbxl_scene_rigs',side_effect=ValueError('optional rig fixture failure')):
    result=bpy.ops.object.rbxanims_import_rbxm(filepath=str(path));assert result=={'FINISHED'},result
    ticks=0
    while import_ops._LAZY_PLACE_IMPORTS and ticks<100:
        delay=import_ops._process_lazy_place_imports();ticks+=1
        if delay:time.sleep(min(delay,.02))
assert not import_ops._LAZY_PLACE_IMPORTS
world=bpy.context.scene.world
assert world is not prior and world is not None and 'rbx_ambient' in world
assert any(obj.type=='LIGHT' and obj.data.type=='SUN' for obj in bpy.context.scene.objects)
print('NO_SKY_OPTIONAL_FAILURE_IMPORT_OK',ticks,world.name,flush=True)
