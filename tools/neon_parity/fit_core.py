import sys,json,numpy as np
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from analyze import sample,decode,encode
specs=json.loads(Path('build/neon_parity/manifest.json').read_text());data={s['id']:sample(s) for s in specs if s['id'] in ('16_gray_bloom_off','17_exposure_plus_off','18_exposure_minus_off')}
pairs=[]
for case,e in [('16_gray_bloom_off',0),('17_exposure_plus_off',1),('18_exposure_minus_off',-1)]:
 for p in data[case]:
  if p['material']!=288:continue
  for authored,actual in zip(p['color'],p['core']):
   if 3<actual<250:pairs.append((authored/255,actual/255,e))
a=np.asarray(pairs);x=decode(a[:,0]);tar=a[:,1]
best=(1e9,None)
for exposure_scale in np.linspace(.4,.65,26):
 v=x*2**(a[:,2]*exposure_scale)
 for n in np.linspace(2.9,3.4,51):
  for k in np.linspace(.003,.005,101):
   err=float(np.mean((encode(v**n/(v**n+k))-tar)**2))
   if err<best[0]:best=(err,(float(n),float(k),float(exposure_scale)))
print('FINAL CORE FIT',best,'RGB RMSE',best[0]**.5*255)
for k,v in data.items():print(k,[(p['color'][0],p['core'][0]) for p in v if p['material']==288])
Path('build/neon_parity/core_fit.json').write_text(json.dumps({'fit':best,'samples':data},indent=2))
