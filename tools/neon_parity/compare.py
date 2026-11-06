"""Inspect actual Studio / EEVEE Neon cores and halo edge profiles."""
import html,json,sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent))
from analyze import sample
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from compare import pixels
from receiver import png_bytes
ROOT=Path('build/neon_parity')

def report():
    specs=json.loads((ROOT/'manifest.json').read_text());records=[];sections=[]
    for spec in specs:
        case=spec['id'];a=pixels(ROOT/'studio'/(case+'.png'))
        source=sample(spec);meta=json.loads((ROOT/'studio'/(case+'.json')).read_text())
        assert max(a[:,:,:3].std(axis=(0,1)))>5,'Blank reference '+case
        record={'case':case,'samples':len(source)}
        for folder in ('before','blender'):
            path=ROOT/folder/(case+'.png')
            if not path.exists():continue
            b=pixels(path);bm=json.loads(path.with_suffix('.json').read_text())
            assert np.allclose(meta['camera_cf'],bm['camera_cf'],atol=.0001) and abs(meta['fov']-bm['fov'])<.0001,'Camera mismatch '+case
            assert not bm['missing_images'],(case,bm['missing_images'])
            sy,sx=a.shape[0]//b.shape[0],a.shape[1]//b.shape[1]
            assert sy==sx and a.shape[0]==b.shape[0]*sy and a.shape[1]==b.shape[1]*sx
            aa=np.rint(a.reshape(b.shape[0],sy,b.shape[1],sx,4).mean(axis=(1,3)))
            error=np.abs(aa[:,:,:3]-b[:,:,:3].astype(float))
            observed=sample(spec,folder);core=[];halo=[]
            per_sample=[]
            for ref,got in zip(source,observed):
                if ref['material']!=288:continue
                c=float(np.mean(np.abs(np.array(ref['core'])-got['core'])))
                h=float(np.mean(np.abs(np.array(ref['halo'])-got['halo'])))
                core.append(c);halo.append(h)
                per_sample.append({'name':ref['name'],'source_core':ref['core'],'eevee_core':got['core'],'core_mae':c,'halo_mae':h})
            record[folder]={'whole_image_mae':float(error.mean()),'core_mae':float(np.mean(core)),'halo_mae':float(np.mean(halo)),'probes':per_sample}
            if folder=='blender':
                heat=np.clip(error*3,0,255).astype(np.uint8);rgba=np.concatenate([heat,np.full((*heat.shape[:2],1),255,dtype=np.uint8)],axis=2)
                (ROOT/'differences').mkdir(exist_ok=True);(ROOT/'differences'/(case+'.png')).write_bytes(png_bytes(b.shape[1],b.shape[0],rgba.tobytes()))
        def img(folder,label):
            return f'<figure><figcaption>{label}</figcaption><img loading="lazy" src="{folder}/{case}.png"></figure>'
        final=record.get('blender',{})
        sections.append(f'<section><h2>{case}</h2><p>{html.escape(spec["description"])} — Core error {final.get("core_mae",0):.2f}/255; halo error {final.get("halo_mae",0):.2f}/255</p><div>{img("studio","Studio")}{img("before","Before")}{img("blender","EEVEE")}{img("differences","Difference ×3")}</div></section>')
        records.append(record)
        print(case,{k:{a:b for a,b in v.items() if a!='probes'} for k,v in record.items() if k in ('before','blender')},flush=True)
    (ROOT/'comparison.json').write_text(json.dumps(records,indent=2))
    (ROOT/'report.html').write_text('''<!doctype html><meta charset="utf-8"><title>Roblox Neon / EEVEE</title><style>body{font:15px system-ui;background:#16191d;color:#eee;margin:24px}div{display:flex;gap:12px}figure{flex:1;min-width:0;margin:0}img{width:100%}section{margin:36px 0}figcaption{color:#bbb}</style><h1>Roblox Neon / EEVEE</h1><p>Native Studio CaptureService references and actual Studio-exported RBXM models. Core brightness and halo color/spread are measured separately. Cameras and image sizes are checked. Display/haze response is calibrated to these Realistic-lighting controls; renderer parity is not certified. Default, disabled, zero-intensity and multiple BloomEffects are distinct cases.</p>'''+''.join(sections))
if __name__=='__main__':report()
