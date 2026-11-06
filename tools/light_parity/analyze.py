import json,sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from compare import project
from image_io import pixels
ROOT=Path('build/light_parity')
def linear(v):
 v=np.asarray(v)/255
 return np.where(v<=.04045,v/12.92,((v+.055)/1.055)**2.4)
def measurements(folder):
 out={}
 for s in json.loads((ROOT/'manifest.json').read_text()):
  p=ROOT/folder/(s['id']+'.png')
  if not p.exists():continue
  a=pixels(p);meta=dict(s,width=a.shape[1],height=a.shape[0]);samples=[]
  for x in (0,1,2,3,4,6,8,10,12,14,16,20,24,30):
   px,py=map(round,project([x,0,0],meta));samples.append(np.median(a[py-1:py+2,px-1:px+2,:3],axis=(0,1)).tolist())
  out[s['id']]={'rgb':samples,'light':s['light']}
 return out
if __name__=='__main__':
 for k,v in measurements(sys.argv[1] if len(sys.argv)>1 else 'studio').items():print(k,[round(x[0],1) for x in v['rgb']])
