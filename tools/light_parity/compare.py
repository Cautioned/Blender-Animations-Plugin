"""Camera-checked native Studio / EEVEE comparisons, scored over lit pixels."""
import json,sys,html
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from image_io import pixels
from receiver import png_bytes
ROOT=Path('build/light_parity')
def main():
 records=[];rows=[]
 for spec in json.loads((ROOT/'manifest.json').read_text()):
  id=spec['id'];reference=pixels(ROOT/'studio'/(id+'.png'))[:,:,:3]
  before=pixels(ROOT/'before'/(id+'.png'))[:,:,:3];after=pixels(ROOT/'blender'/(id+'.png'))[:,:,:3]
  h,w=after.shape[:2];assert reference.shape[:2]==(h*2,w*2)
  reference=reference.reshape(h,2,w,2,3).mean(axis=(1,3))
  for folder in ('before','blender'):
   meta=json.loads((ROOT/folder/(id+'.json')).read_text());source=json.loads((ROOT/'studio'/(id+'.json')).read_text())
   assert np.allclose(meta['camera_cf'],source['camera_cf'],atol=1e-5) and abs(meta['fov']-source['fov'])<1e-5
  mask=np.max(reference,axis=2)>3
  union=mask|(np.max(before,axis=2)>3)|(np.max(after,axis=2)>3)
  err0=np.abs(before-reference);err1=np.abs(after-reference)
  score=lambda e:float(e[union].mean()) if union.any() else float(e.mean())
  rec={'case':id,'before_mae':score(err0),'after_mae':score(err1),'before_whole':float(err0.mean()),'after_whole':float(err1.mean()),'source_lit_pixels':int(mask.sum()),'lit_union_pixels':int(union.sum()),'kind':spec['light']['kind'],
       'source_roi_before':float(err0[mask].mean()) if mask.any() else None,
       'source_roi_after':float(err1[mask].mean()) if mask.any() else None,
       'current_roi_after':float(err1[mask|(np.max(after,axis=2)>3)].mean()) if (mask|(np.max(after,axis=2)>3)).any() else 0.};records.append(rec)
  diff=np.concatenate([np.minimum(err1*3,255).astype('uint8'),np.full((h,w,1),255,dtype='uint8')],axis=2)
  (ROOT/'difference').mkdir(exist_ok=True);(ROOT/'difference'/(id+'.png')).write_bytes(png_bytes(w,h,diff.tobytes()))
  images=''.join(f'<td><img loading="lazy" src="{folder}/{id}.png"></td>' for folder in ('studio','before','blender','difference'))
  roi='' if rec['source_roi_after'] is None else f"; source-lit ROI {rec['source_roi_before']:.2f} → {rec['source_roi_after']:.2f}"
  rows.append(f'<tr><th colspan="4">{html.escape(spec["description"])} — MAE {rec["before_mae"]:.2f} → {rec["after_mae"]:.2f}{roi}</th></tr><tr>{images}</tr>')
 summary={key:float(np.mean([r[key] for r in records])) for key in ('before_mae','after_mae','before_whole','after_whole')}
 summary['source_roi_before']=float(np.mean([r['source_roi_before'] for r in records if r['source_roi_before'] is not None]))
 summary['source_roi_after']=float(np.mean([r['source_roi_after'] for r in records if r['source_roi_after'] is not None]))
 groups={}
 for kind in ('PointLight','SpotLight','SurfaceLight'):
  group=[r for r in records if r['kind']==kind]
  groups[kind]={key:float(np.mean([r[key] for r in group if r[key] is not None])) for key in ('before_mae','after_mae','source_roi_before','source_roi_after','current_roi_after')}
 (ROOT/'comparison.json').write_text(json.dumps({'summary':summary,'groups':groups,'cases':records},indent=2))
 (ROOT/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>Local light parity</title><style>body{background:#17191c;color:#eee;font:16px system-ui}table{width:100%;table-layout:fixed}img{width:100%}th{text-align:left;padding:16px}td{width:25%}</style><h1>Native Studio → EEVEE local lights</h1><p>Studio • Before • After • Absolute difference ×3. MAE is measured over the common union of illuminated pixels across Studio, before and after. The additional source-lit ROI score uses only pixels lit in Studio. Neither score counts unrelated black background. All cameras and FOVs are checked.</p><p>'+html.escape(str(summary))+'</p><p>Saved Blender scenes: <a href="blender/01_point_default.blend">PointLight</a> · <a href="blender/30_spot_shadow.blend">SpotLight shadows</a> · <a href="blender/51_surface_rectangle_8.blend">SurfaceLight</a> · <a href="blender/76_pointlight_opaque_mount.blend">Opaque housing</a>.</p><table><tr><th>Studio CaptureService</th><th>Importer before</th><th>Importer after</th><th>Difference ×3</th></tr>'+''.join(rows)+'</table>')
 print(json.dumps(summary))
if __name__=='__main__':main()
