"""Isolated native local-light sweeps. All dimensions are Roblox studs."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from scenes import fixture,part,node,cf
ROOT=Path('build/light_parity');ROOT.mkdir(exist_ok=True)
scenes=[]
def add(kind,tag,**options):
 id=f'{len(scenes)+1:02d}_{tag}'
 s=fixture({'id':id,'description':f'{kind}: {tag}'});s['nodes']=s['nodes'][:4]
 s['camera_cf']=[0,40,0,1,0,0,0,0,1,0,-1,0];s['fov']=60
 s['nodes'][2]['properties']['CFrame'][1]=s['camera_cf'];s['nodes'][2]['properties']['FieldOfView'][1]=60
 settings={'ClockTime':0,'Brightness':0,'Ambient':[0]*3,'OutdoorAmbient':[0]*3,'EnvironmentDiffuseScale':0,'EnvironmentSpecularScale':0}
 for k,v in settings.items():s['nodes'][1]['properties'][k][1]=v
 s['nodes'][1]['properties']['TimeOfDay'][1]='00:00:00'
 s['nodes'].append(part('Receiver',[0,-.1,0],[100,.2,100],[180]*3,272))
 s['nodes'].append(part('Mount',[0,options.get('height',8),0],options.get('size',[1,1,1]),[255]*3,272,Transparency=['float',1],CastShadow=['bool',False]))
 parent=5
 if options.get('attached'):
  parent=len(s['nodes']);s['nodes'].append(node('Attachment',5,Name=['string','Emitter'],CFrame=['CoordinateFrame',[2,-.5,0,1,0,0,0,0,1,0,-1,0]]))
 props={'Name':['string','TestLight'],'Brightness':['float',options.get('brightness',1)],'Range':['float',options.get('range',16)],'Color':['Color3',options.get('color',[1,1,1])],'Enabled':['bool',options.get('enabled',True)],'Shadows':['bool',options.get('shadows',False)]}
 if kind!='PointLight':props.update(Angle=['float',options.get('angle',60)],Face=['token',4])
 s['nodes'].append(node(kind,parent,**props))
 if options.get('blocker'):s['nodes'].append(part('Blocker',[1,3.5,0],[3,1,4],[180]*3,272))
 s['light']={'kind':kind,**options};scenes.append(s)
for k in ('PointLight','SpotLight','SurfaceLight'):
 prefix={'PointLight':'point','SpotLight':'spot','SurfaceLight':'surface'}[k]
 add(k,prefix+'_default')
 for r in (4,8,24,32,60,120):add(k,prefix+f'_range_{r}',range=r,height=2 if r==4 else 8)
 for b in (.25,4):add(k,prefix+f'_brightness_{str(b).replace(".","p")}',brightness=b)
 if k!='PointLight':
  for a in (0,15,30,90,120,180):add(k,prefix+f'_angle_{a}',angle=a)
  add(k,prefix+'_attachment',attached=True)
 add(k,prefix+'_tint',color=[1,.4,.1])
 add(k,prefix+'_shadow',blocker=True,shadows=True)
 add(k,prefix+'_no_shadow',blocker=True,shadows=False)
for size in ([8,1,2],[2,1,8]):add('SurfaceLight','surface_rectangle_'+str(size[0]),size=size,angle=30)
for k in ('PointLight','SpotLight','SurfaceLight'):
 for field in ('range','brightness'):add(k,k.lower()+'_'+field+'_zero',**{field:0})
for kind in ('PointLight','SpotLight','SurfaceLight'):
 for h in (2,4,16):add(kind,kind.lower()+f'_height_{h}',height=h,range=32)
 if kind!='PointLight':
  add(kind,kind.lower()+'_thick_mount',size=[1,8,1],range=16)
  add(kind,kind.lower()+'_attachment_front',attached=True)
  scenes[-1]['nodes'][-1]['properties']['Face'][1]=5
add('PointLight','point_global_shadows_off',blocker=True,shadows=True)
scenes[-1]['nodes'][1]['properties']['GlobalShadows'][1]=False
add('PointLight','point_disabled',enabled=False)
add('SpotLight','spot_daylight_combined',brightness=2)
for key,value in {'ClockTime':14,'Brightness':2,'OutdoorAmbient':[.5]*3,'EnvironmentDiffuseScale':1,'EnvironmentSpecularScale':1}.items():scenes[-1]['nodes'][1]['properties'][key][1]=value
scenes[-1]['nodes'][1]['properties']['TimeOfDay'][1]='14:00:00'
add('PointLight','point_colored_overlap',color=[1,.15,.05])
scenes[-1]['nodes'].append(part('SecondMount',[6,8,0],[1,1,1],[255]*3,272,Transparency=['float',1],CastShadow=['bool',False]))
scenes[-1]['nodes'].append(node('PointLight',len(scenes[-1]['nodes'])-1,Name=['string','SecondLight'],Range=['float',16],Brightness=['float',1],Color=['Color3',[.05,.2,1]],Enabled=['bool',True],Shadows=['bool',False]))
for kind in ('PointLight','SpotLight','SurfaceLight'):
 add(kind,kind.lower()+'_opaque_mount',shadows=True)
 scenes[-1]['nodes'][5]['properties']['Transparency'][1]=0
 scenes[-1]['nodes'][5]['properties']['CastShadow'][1]=True
(ROOT/'manifest.json').write_text(json.dumps(scenes,indent=2))
template=Path('tools/material_parity/studio_case.luau').read_text()
for s in scenes:(ROOT/(s['id']+'.luau')).write_text(template.replace('__SCENE_JSON__',json.dumps(s)))
print('Generated',len(scenes),'light scenarios')
