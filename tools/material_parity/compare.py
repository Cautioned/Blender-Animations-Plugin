"""Report actual Studio / imported EEVEE material cards, including before/after."""
import html,json,math,sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from compare import pixels,project
from receiver import png_bytes
ROOT=Path('build/material_parity')

def run():
    manifest=json.loads((ROOT/'manifest.json').read_text());results=[];sections=[]
    for spec in manifest:
        case=spec['id'];ref=ROOT/'studio'/(case+'.png');final=ROOT/'blender'/(case+'.png');before=ROOT/'before'/(case+'.png')
        if not ref.exists():continue
        a=pixels(ref);meta=json.loads(ref.with_suffix('.json').read_text())
        assert max(a[:,:,:3].std(axis=(0,1)))>5,'Blank Studio reference: '+case
        record={'case':case,'samples':[]}
        for folder,path in [('before',before),('blender',final)]:
            if not path.exists():continue
            b=pixels(path);bm=json.loads(path.with_suffix('.json').read_text())
            assert np.allclose(meta['camera_cf'],bm['camera_cf'],atol=.0001) and abs(meta['fov']-bm['fov'])<.0001,'Camera differs'
            assert not bm['missing_images'],(case,bm['missing_images'])
            sy,sx=a.shape[0]//b.shape[0],a.shape[1]//b.shape[1]
            assert sy==sx and a.shape[:2]==(b.shape[0]*sy,b.shape[1]*sx)
            aa=np.rint(a.reshape(b.shape[0],sy,b.shape[1],sx,4).mean(axis=(1,3)))
            m=dict(meta,width=b.shape[1],height=b.shape[0]);errors=np.abs(aa[:,:,:3]-b[:,:,:3].astype(float))
            record[folder]={'whole_image_mae':float(errors.mean())}
            carderrors=[]
            for sample in spec['samples']:
                x,y=project(sample['center'],m);radius=m['height']/(2*math.tan(math.radians(m['fov'])/2))*1.7/40
                x0,x1=max(0,round(x-radius)),min(b.shape[1],round(x+radius));y0,y1=max(0,round(y-radius)),min(b.shape[0],round(y+radius))
                e=float(errors[y0:y1,x0:x1].mean());carderrors.append(e)
                if folder=='before':record['samples'].append({'name':sample['name'],'before_mae':e})
                else:
                    found=next((x for x in record['samples'] if x['name']==sample['name']),None)
                    if found is None:found={'name':sample['name']};record['samples'].append(found)
                    found['final_mae']=e
            record[folder]['mean_card_mae']=float(np.mean(carderrors))
            if folder=='blender':
                heat=np.clip(errors*3,0,255).astype(np.uint8);rgba=np.concatenate([heat,np.full((*heat.shape[:2],1),255,dtype=np.uint8)],axis=2)
                (ROOT/'differences').mkdir(exist_ok=True);(ROOT/'differences'/(case+'.png')).write_bytes(png_bytes(b.shape[1],b.shape[0],rgba.tobytes()))
        results.append(record)
        def img(folder):
            return f'<figure><figcaption>{folder}</figcaption><img src="{folder}/{case}.png"></figure>' if (ROOT/folder/(case+'.png')).exists() else ''
        rows=''.join('<tr><td>'+html.escape(s['name'])+'</td><td>'+str(round(s.get('before_mae',0),2))+'</td><td>'+str(round(s.get('final_mae',0),2))+'</td></tr>' for s in record['samples'])
        sections.append(f'<section><h2>{case}</h2><p>{html.escape(spec["description"])}</p><div>{img("studio")}{img("before")}{img("blender")}{img("differences")}</div><details><summary>Per-sample RGB error (0–255)</summary><table><tr><th>Sample</th><th>Before</th><th>After</th></tr>{rows}</table></details></section>')
    (ROOT/'comparison.json').write_text(json.dumps(results,indent=2))
    (ROOT/'report.html').write_text('''<!doctype html><meta charset="utf-8"><title>Material round-trip comparisons</title><style>body{font:15px system-ui;background:#16191d;color:#eee;margin:24px}div{display:flex;gap:12px}figure{flex:1;min-width:0;margin:0}img{width:100%}section{margin:35px 0}td,th{padding:4px 12px;text-align:left}summary{cursor:pointer}</style><h1>Studio → RBXM → Blender / EEVEE</h1><p>Uploaded diagnostic textures belong to test creator 7255061823. Models are actual native Studio SerializationService exports. Camera, FOV, image size and pixel hashes are recorded with CaptureService references. EEVEE uses the exact diagnostic upload source bytes locally, leaving the serialized asset URIs unchanged; this suite measures materials, not private-asset delivery. Raw RGB errors include the remaining lighting/BRDF differences and do not certify 1:1 parity. Differences are amplified ×3.</p>'''+''.join(sections))
    print(json.dumps([{k:v for k,v in r.items() if k!='samples'} for r in results],indent=2))
if __name__=='__main__':run()
