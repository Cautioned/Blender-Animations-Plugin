"""Image comparisons and an inspectable HTML contact sheet; requires numpy."""
import argparse
import html
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parent))
from bootstrap import load_addon_modules
from receiver import png_bytes
load_addon_modules()
from roblox_animations.rig.pngdec import decode_png


def pixels(path):
    result=decode_png(Path(path).read_bytes())
    if result is None: raise ValueError('Unsupported PNG: '+str(path))
    return result[3]


def project(point, meta):
    cf=np.asarray(meta['camera_cf'],dtype=float)
    rotation=cf[3:].reshape(3,3)
    p=rotation.T @ (np.asarray(point)-cf[:3])
    focal=meta['height']/(2*math.tan(math.radians(meta['fov'])/2))
    return (meta['width']/2+focal*p[0]/-p[2],meta['height']/2-focal*p[1]/-p[2])


def probe_samples(image, meta):
    points={f'patch_{i}':[-14+i*4,0.11,15] for i in range(8)}
    points.update({'plastic_top':[-12,4.5,-9],'floor':[17,0,8]})
    found={}
    for name,point in points.items():
        x,y=project(point,meta);x,y=round(x),round(y)
        radius=max(2,int(image.shape[0]/200))
        region=image[max(0,y-radius):y+radius+1,max(0,x-radius):x+radius+1,:3]
        if region.size:found[name]=np.median(region.reshape(-1,3),axis=0)
    return found


def metrics(reference, actual, meta):
    if reference.shape!=actual.shape:raise ValueError('Image dimensions differ; render with captured camera metadata')
    error=np.abs(reference[:,:,:3].astype(float)-actual[:,:,:3].astype(float))
    target=probe_samples(reference,meta);observed=probe_samples(actual,meta)
    probes={key:{'studio':value.tolist(),'blender':observed[key].tolist(),
                  'mae_rgb_255':float(np.mean(np.abs(value-observed[key])))} for key,value in target.items()}
    return {'mae_rgb_255':float(np.mean(error)),
            'rmse_rgb_255':float(np.sqrt(np.mean(error*error))),
            'probe_mae_rgb_255':float(np.mean([p['mae_rgb_255'] for p in probes.values()])),
            'probes':probes}


def material_summary(root):
    path=root/'material_probes.json'
    if not path.exists():return ''
    probes=json.loads(path.read_text());folders=list(probes['mean_errors'])
    headings=''.join('<th>'+html.escape(folder)+'</th>' for folder in folders)
    rows=''.join('<tr><td>'+html.escape(row['material'])+'</td>'+''.join(
        f"<td>{row['errors'][folder]:.2f}</td>" for folder in folders)+'</tr>' for row in probes['materials'])
    means='; '.join(f"{html.escape(folder)} {value:.2f}/255" for folder,value in probes['mean_errors'].items())
    return ('<!-- RBX material probes --><details><summary>Daylight material color probes</summary>'
            '<p>Fixed surface medians on 12 materials; supplemental color checks, not full texture/BRDF parity. Mean: '+means+
            '</p><table><tr><th>Material</th>'+headings+'</tr>'+rows+'</table></details><!-- /RBX material probes -->')


def report(root, actual_folder="blender", baseline_folder=None):
    manifest=json.loads((root/'manifest.json').read_text())
    rows=[];records=[]
    for spec in manifest:
        case=spec['id'];reference=root/'studio'/(case+'.png');actual=root/actual_folder/(case+'.png')
        status='Reference capture missing';record={'case':case,'status':status}
        if reference.exists() and actual.exists():
            meta=json.loads((root/'studio'/(case+'.json')).read_text())
            a,b=pixels(reference),pixels(actual)
            try:
                if float(np.std(a[:,:,:3])) < 1:
                    raise ValueError('Invalid blank reference capture; recapture before comparison')
                blender_meta=json.loads((root/actual_folder/(case+'.json')).read_text())
                if max(abs(x-y) for x,y in zip(meta['camera_cf'],blender_meta['camera_cf']))>0.0001 or abs(meta['fov']-blender_meta['fov'])>0.0001:
                    raise ValueError('Camera metadata differs; rerender before comparison')
                if a.shape!=b.shape:
                    if a.shape[0]%b.shape[0] or a.shape[1]%b.shape[1] or a.shape[0]//b.shape[0]!=a.shape[1]//b.shape[1]:
                        raise ValueError('Capture and render aspect ratios differ')
                    scale=a.shape[0]//b.shape[0]
                    a=np.rint(a.reshape(b.shape[0],scale,b.shape[1],scale,4).mean(axis=(1,3))).astype(np.uint8)
                    meta=dict(meta,width=b.shape[1],height=b.shape[0])
                measured=metrics(a,b,meta)
                record={'case':case,'status':'Compared; no visual-parity claim',**measured}
                if baseline_folder and (root/baseline_folder/(case+'.png')).exists():
                    before=pixels(root/baseline_folder/(case+'.png'))
                    before_meta=json.loads((root/baseline_folder/(case+'.json')).read_text())
                    if before.shape!=b.shape or before_meta['camera_cf']!=blender_meta['camera_cf'] or before_meta['fov']!=blender_meta['fov']:
                        raise ValueError('Baseline dimensions/camera differ')
                    baseline=metrics(a,before,meta)
                    record['baseline']={k:v for k,v in baseline.items() if k!='probes'}
                    record['improvement_percent']=100*(1-measured['mae_rgb_255']/baseline['mae_rgb_255'])
                heat=np.clip(np.abs(a[:,:,:3].astype(float)-b[:,:,:3].astype(float))*3,0,255).astype(np.uint8)
                rgba=np.concatenate([heat,np.full((*heat.shape[:2],1),255,dtype=np.uint8)],axis=2)
                path=root/'differences'/(case+'.png');path.parent.mkdir(exist_ok=True)
                path.write_bytes(png_bytes(rgba.shape[1],rgba.shape[0],rgba.tobytes()))
                status=f"RGB MAE {measured['mae_rgb_255']:.2f}/255; patch MAE {measured['probe_mae_rgb_255']:.2f}/255"
            except ValueError as exc:
                status=str(exc);record={'case':case,'status':status}
        records.append(record)
        def img(folder):
            path=root/folder/(case+'.png')
            return f'<img loading="lazy" src="{folder}/{case}.png" alt="{folder} {case}">' if path.exists() else '<p>Not captured</p>'
        rows.append(f'<section><h2>{case}</h2><p>{html.escape(spec["description"])} — {html.escape(status)}</p>'
                    f'<div><figure><figcaption>Studio</figcaption>{img("studio")}</figure>'+
                    (f'<figure><figcaption>Before</figcaption>{img(baseline_folder)}</figure>' if baseline_folder else '')+
                    f'<figure><figcaption>EEVEE</figcaption>{img(actual_folder)}</figure>'
                    f'<figure><figcaption>Absolute difference ×3</figcaption>{img("differences")}</figure></div></section>')
    (root/'comparison.json').write_text(json.dumps(records,indent=2)+'\n')
    compared=[r for r in records if 'mae_rgb_255' in r]
    summary=f"<p>Mean whole-image RGB error: {np.mean([r['mae_rgb_255'] for r in compared]):.2f}/255.</p>" if compared else ''
    before=[r for r in compared if 'baseline' in r]
    if before:
        old=np.mean([r['baseline']['mae_rgb_255'] for r in before]);new=np.mean([r['mae_rgb_255'] for r in before])
        summary+=f'<p>Same-camera baseline: {old:.2f} → {new:.2f}/255 ({100*(1-new/old):.1f}% lower error).</p>'
    if (root/'reference_materials.json').exists():
        summary+='<p>Material generation: legacy, as declared by Use2022MaterialsXml=false in the saved native Studio reference place. Original synthetic binaries incorrectly requested 2022 materials. See reference_materials.json for provenance.</p>'
    summary+=material_summary(root)
    (root/'report.html').write_text('''<!doctype html><meta charset="utf-8"><title>Roblox / EEVEE comparisons</title>
<style>body{font:15px system-ui;background:#15171b;color:#eee;margin:24px}section{margin:32px 0}h2{font-size:18px}div{display:flex;gap:12px}figure{margin:0;flex:1;min-width:0}img{width:100%;height:auto}figcaption{margin-bottom:8px;color:#bbb}</style>
<h1>Roblox Studio / EEVEE comparison suite</h1><p>Matching camera and vertical field of view. Studio captures use CaptureService and record actual pixel size and graphics settings. Metrics measure image differences; they do not certify visual parity.</p>'''+summary+''.join(rows))
    print(json.dumps([{k:v for k,v in r.items() if k!='probes'} for r in records],indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path('build/visual_parity'))
    parser.add_argument('--actual-folder',default='blender');parser.add_argument('--baseline-folder')
    args=parser.parse_args();report(args.root.resolve(),args.actual_folder,args.baseline_folder)
