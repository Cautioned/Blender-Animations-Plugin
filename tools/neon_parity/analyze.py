"""Measure source cores and edge profiles without fitting background pixels."""
import json,sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from compare import pixels,project
ROOT=Path('build/neon_parity')

def decode(x):return np.where(x<=.04045,x/12.92,((x+.055)/1.055)**2.4)
def encode(x):return np.where(x<=.0031308,12.92*x,1.055*np.maximum(x,0)**(1/2.4)-.055)
def sample(spec,folder='studio'):
 p=ROOT/folder/(spec['id']+'.png');a=pixels(p)
 meta=json.loads((ROOT/'studio'/(spec['id']+'.json')).read_text())
 meta.update(width=a.shape[1],height=a.shape[0]);out=[]
 for item in spec['samples']:
  cx,cy=project(item['center'],meta);cx,cy=round(cx),round(cy)
  radius=max(1,round(2*a.shape[0]/1306))
  core=np.median(a[cy-radius:cy+radius+1,cx-radius:cx+radius+1,:3].reshape(-1,3),axis=0)
  x,y,z=item['center'];edge=project([x+item['size'][0]/2,y,z+item['size'][2]/2],meta)[0]
  halo=[a[cy,min(a.shape[1]-1,round(edge+d*a.shape[1]/1944)),:3].tolist() for d in (5,10,20,40)]
  out.append(dict(item,core=core.tolist(),halo=halo))
 return out
if __name__=='__main__':
 specs=json.loads((ROOT/'manifest.json').read_text());records={}
 for spec in specs:
  v=sample(spec);records[spec['id']]=v
  print(spec['id'],'core',[v[i]['core'] for i in (0,1,6,7,12,13)],'halo',v[0]['halo'])
 # Fit a shared HDR response against grayscale controls and exposure pairs.
 pairs=[]
 for case,exposure in [('13_grayscale',0),('10_exposure_plus',1),('11_exposure_minus',-1),('08_bloom_intensity_zero',0)]:
  for p in records[case]:
   if p['material']!=288:continue
   for authored,actual in zip(p['color'],p['core']):
    if 5<actual<250:pairs.append((authored/255,actual/255,exposure))
 a=np.array(pairs);x=decode(a[:,0])*2**a[:,2];target=a[:,1]
 best=(1e9,None)
 for n in np.linspace(2,4,201):
  for k in np.geomspace(.001,.02,180):
   q=x**n/(x**n+k);pred=encode(q);err=float(np.mean((pred-target)**2))
   if err<best[0]:best=(err,(float(n),float(k)))
 print('Fitted hill in linear color',best,'RGB RMSE',best[0]**.5*255)
 (ROOT/'source_measurements.json').write_text(json.dumps({'measurements':records,'hill_fit':best},indent=2))
