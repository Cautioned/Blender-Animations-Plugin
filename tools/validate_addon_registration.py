import sys,importlib,bpy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
addon=importlib.import_module('roblox_animations')
addon.register()
assert hasattr(bpy.context.scene,'rbx_anim_settings')
for cls in addon.CLASSES:
 assert getattr(bpy.types,cls.__name__,None) is not None or getattr(cls,'is_registered',False),cls.__name__
assert hasattr(bpy.ops.object,'rbxanims_attach_to_bone')
assert hasattr(bpy.ops.object,'rbxanims_import_and_attach')
from roblox_animations.rig import avatar_scale
assert callable(avatar_scale.entry_limb_scale)
print('REGISTER_OK',len(addon.CLASSES),'classes')
addon.unregister()
assert not hasattr(bpy.types.Scene,'rbx_anim_settings')
addon.register()
assert hasattr(bpy.context.scene,'rbx_anim_settings')
print('REREGISTER_OK')
addon.unregister()
