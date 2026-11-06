"""Native tint-mask row checks; source transparency rows remain separate."""
import json,sys,math,html
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from compare import pixels,project
ROOT=Path('build/sphere_material_parity')
REFERENCE=Path('build/material_parity')


def run():
    manifest=json.loads((REFERENCE/'manifest.json').read_text());records=[];sections=[]
    for spec in manifest[:2]:
        case=spec['id'];meta=json.loads((REFERENCE/'studio'/(case+'.json')).read_text());a=pixels(REFERENCE/'studio'/(case+'.png'))
        result={'case':case}
        for folder in ['linear_mask','corrected_mask']:
            b=pixels(ROOT/folder/(case+'.png'));scale=a.shape[0]//b.shape[0]
            aa=np.rint(a.reshape(b.shape[0],scale,b.shape[1],scale,4).mean(axis=(1,3)))
            m=dict(meta,width=b.shape[1],height=b.shape[0]);err=abs(aa[:,:,:3]-b[:,:,:3]);tints=[]
            for sample in spec['samples']:
                if sample['index']//6!=2:continue
                x,y=project(sample['center'],m);r=m['height']/(2*math.tan(math.radians(m['fov'])/2))*1.55/40
                region=err[round(y-r):round(y+r),round(x-r):round(x+r)]
                tints.append({'name':sample['name'],'error':float(region.mean())})
            result[folder]={'mean':float(np.mean([r['error'] for r in tints])),'samples':tints}
        result['reduction_percent']=100*(1-result['corrected_mask']['mean']/result['linear_mask']['mean']);records.append(result)
        sections.append(f'<h2>{case}</h2><p>Tint-mask row error: {result["linear_mask"]["mean"]:.2f} → {result["corrected_mask"]["mean"]:.2f}/255 ({result["reduction_percent"]:.1f}% lower).</p><div>'+''.join(f'<figure><figcaption>{label}</figcaption><img src="{path}/{case}.png"></figure>' for label,path in [('Studio','../material_parity/studio'),('Linear tint blend','linear_mask'),('Display-space tint blend','corrected_mask')])+'</div>')
    (ROOT/'alpha_mask_comparison.json').write_text(json.dumps(records,indent=2)+'\n')
    (ROOT/'alpha_report.html').write_text('<!doctype html><meta charset="utf-8"><title>Tint-mask comparisons</title><style>body{font:15px system-ui;background:#16191d;color:#eee;margin:24px}div{display:flex;gap:12px}figure{flex:1;min-width:0;margin:0}img{width:100%}</style><h1>Native alpha-band tint-mask checks</h1><p>Identical cameras and uploaded map bytes. Metrics isolate the third row: six tints in the first scene and six Part transparencies in the second. Other alpha modes remain visible for context. Brightness/BRDF differences still contribute to raw image error.</p>'+''.join(sections))
    print(json.dumps(records,indent=2))


if __name__=='__main__':run()
