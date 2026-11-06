"""Neon source/halo controls in a marker-guarded Studio test place."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from scenes import fixture,part,node,look_at
ROOT=Path('build/neon_parity')
COLORS=((255,255,255),(128,128,128),(255,32,32),(32,255,32),(32,32,255),(255,190,32))
CASES=[
 ('01_dark_default','Night, no BloomEffect',{},None),
 ('02_day_default','Day, no BloomEffect',{'ClockTime':14,'Brightness':2,'OutdoorAmbient':[.5]*3,'EnvironmentDiffuseScale':1},None),
 ('03_bloom_default','Bloom intensity 1, size 24, threshold .95',{},{}),
 ('04_bloom_size_zero','Bloom size zero still permits color adjustment',{}, {'Size':0}),
 ('05_bloom_wide','Bloom radius 56',{}, {'Size':56}),
 ('06_bloom_threshold_zero','Bloom all visible colors',{}, {'Threshold':0}),
 ('07_bloom_threshold_high','Bloom threshold 1',{}, {'Threshold':1}),
 ('08_bloom_intensity_zero','Zero bloom intensity',{}, {'Intensity':0}),
 ('09_neon_transparency','Neon transparency 0/.25/.5/1',{},{}),
 ('10_exposure_plus','Exposure +1',{'ExposureCompensation':1},{}),
 ('11_exposure_minus','Exposure -1',{'ExposureCompensation':-1},{}),
 ('12_occlusion_scale','Thin and wide emitters with a foreground occluder',{},{}),
]
def build(id,description,lighting,bloom):
 s=fixture({'id':id,'description':description});s['nodes']=s['nodes'][:4]
 s['camera_cf']=look_at([0,0,40],[0,0,0]);s['fov']=32
 s['nodes'][2]['properties']['CFrame'][1]=s['camera_cf'];s['nodes'][2]['properties']['FieldOfView'][1]=32
 s['samples']=[]
 settings={'ClockTime':0,'Brightness':0,'Ambient':[0]*3,'OutdoorAmbient':[0]*3,'EnvironmentDiffuseScale':0,'EnvironmentSpecularScale':0,'GlobalShadows':True}
 settings.update(lighting)
 for k,v in settings.items():s['nodes'][1]['properties'][k][1]=v
 clock=settings['ClockTime'];s['nodes'][1]['properties']['TimeOfDay'][1]=f'{int(clock):02d}:00:00'
 s['nodes'].append(part('Background',[0,0,-4],[36,26,.2],[6]*3,272,CastShadow=['bool',False]))
 for i in range(24):
  row,col=i//6,i%6;x,y=(col-2.5)*4,(1.5-row)*4
  material=272 if row==3 and id!='09_neon_transparency' else 288
  level=(1,.5,.25,1)[row];color=[round(c*level) for c in COLORS[col]]
  t=(0,.25,.5,1)[row] if id=='09_neon_transparency' else 0
  size=[2.4,2.4,.2]
  if id=='12_occlusion_scale':size=[(.1,.25,.5,1,2,3)[col],2.4,.2]
  s['nodes'].append(part(f'neon_{i}' if material==288 else f'control_{i}',[x,y,0],size,color,material,Transparency=['float',t],CastShadow=['bool',False]))
  s['samples'].append({'name':s['nodes'][-1]['properties']['Name'][1],'center':[x,y,0],'size':size,'color':color,'material':material,'transparency':t})
 if id=='12_occlusion_scale':s['nodes'].append(part('Occluder',[0,2,.8],[30,.6,.3],[3]*3,272,CastShadow=['bool',False]))
 if bloom is not None:
  props={'Intensity':1,'Size':24,'Threshold':.95};props.update(bloom)
  s['nodes'].append(node('BloomEffect',1,Name=['string','ParityBloom'],Enabled=['bool',True],**{k:['float',v] for k,v in props.items()}))
 return s
scenes=[build(*c) for c in CASES]
s=build('13_grayscale','24 grayscale levels, no BloomEffect',{},None)
for i,sample in enumerate(s['samples']):
    value=round(i*255/23)
    s['nodes'][5+i]['properties']['Color3uint8'][1]=[value]*3
    s['nodes'][5+i]['properties']['Material'][1]=288
    sample.update(color=[value]*3,material=288)
scenes.append(s)
s=build('14_bloom_disabled','Explicit disabled BloomEffect',{}, {})
s['nodes'][-1]['properties']['Enabled'][1]=False
scenes.append(s)
s=build('15_bloom_multiple','Two enabled BloomEffects',{}, {})
s['nodes'].append(node('BloomEffect',1,Name=['string','ParityBloomSecond'],Enabled=['bool',True],Intensity=['float',.5],Size=['float',12],Threshold=['float',.7]))
scenes.append(s)
s=build('16_gray_bloom_off','Grayscale response with bloom intensity zero',{}, {'Intensity':0})
for i,sample in enumerate(s['samples']):
    value=round(i*255/23)
    s['nodes'][5+i]['properties']['Color3uint8'][1]=[value]*3
    s['nodes'][5+i]['properties']['Material'][1]=288
    sample.update(color=[value]*3,material=288)
scenes.append(s)
scenes.append(build('17_exposure_plus_off','Exposure +1 with bloom intensity zero',{'ExposureCompensation':1},{'Intensity':0}))
scenes.append(build('18_exposure_minus_off','Exposure -1 with bloom intensity zero',{'ExposureCompensation':-1},{'Intensity':0}))
scenes.append(build('19_day_bloom','Daylight Neon and ordinary material controls',{'ClockTime':14,'Brightness':2,'OutdoorAmbient':[.5]*3,'EnvironmentDiffuseScale':1},{}))
ROOT.mkdir(exist_ok=True)
(ROOT/'manifest.json').write_text(json.dumps(scenes,indent=2))
template=Path('tools/material_parity/studio_case.luau').read_text()
template=template.replace('local serialized = game:GetService("SerializationService"):SerializeInstancesAsync({root})', '''local exportRoots={root}
for index,item in spec.nodes do
    if item.class=="BloomEffect" then table.insert(exportRoots,objects[index]) end
end
local serialized = game:GetService("SerializationService"):SerializeInstancesAsync(exportRoots)''')
for scene in scenes:(ROOT/(scene['id']+'.luau')).write_text(template.replace('__SCENE_JSON__',json.dumps(scene)))
print('Generated',len(scenes),'Neon controls')
