import sys,json,numpy as np
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from compare import pixels,metrics
ROOT=Path('build/visual_parity');OUT=Path('/private/tmp/rbx_shelter_experiment_20261105')
records=[]
for case in ['01_daylight','10_indoor']:
 a=pixels(ROOT/'studio'/(case+'.png'));meta=json.loads((ROOT/'studio'/(case+'.json')).read_text())
 for mode in ['baseline','visibility','radiance','fast_ao','fast_gi','fast_detail','product','dense']:
  path=(ROOT/'blender'/(case+'.png')) if mode=='baseline' else OUT/mode/(case+'.png')
  if not path.exists():continue
  b=pixels(path);s=a.shape[0]//b.shape[0];aa=np.rint(a.reshape(b.shape[0],s,b.shape[1],s,4).mean(axis=(1,3))).astype(np.uint8)
  m=dict(meta,width=b.shape[1],height=b.shape[0]);r=metrics(aa,b,m);records.append(dict(case=case,mode=mode,**r));print(case,mode,round(r['mae_rgb_255'],3),round(r['probe_mae_rgb_255'],3),flush=True)
OUT.joinpath('comparison.json').write_text(json.dumps(records,indent=2))
