"""Supplement the unchanged whole-image metrics with fixed material regions.

Use daylight only: room geometry in other fixtures can occlude these points.
These small medians check surface color, not complete BRDF/texture parity.
"""
import argparse,json,sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from compare import pixels,project


def evaluate(root,folders):
    spec=next(s for s in json.loads((root/'manifest.json').read_text()) if s['id']=='01_daylight')
    meta=json.loads((root/'studio/01_daylight.json').read_text())
    rows=[];images={}
    for folder in ['studio']+folders:
        image=pixels(root/folder/'01_daylight.png')
        images[folder]=image
    for entry in spec['nodes']:
        props=entry['properties'];name=props.get('Name',[None,None])[1]
        if name not in ('Plastic','SmoothPlastic','Metal','Glass','Neon','Concrete','Wood','Brick','Fabric','Marble','Ice','Grass'):continue
        point=np.array(props['CFrame'][1][:3],dtype=float);half=np.array(props['Size'][1])/2
        if props['Shape'][1]==0:
            normal=np.array(meta['camera_cf'][:3])-point;normal/=np.linalg.norm(normal)
            point+=normal*half[0]
        else:point[1]+=half[1]
        row={'material':name,'point':point.tolist(),'samples':{}}
        for folder,image in images.items():
            m=dict(meta,width=image.shape[1],height=image.shape[0]);x,y=map(round,project(point,m));r=max(2,round(image.shape[0]*.009))
            area=image[max(0,y-r):y+r+1,max(0,x-r):x+r+1,:3]
            row['samples'][folder]=np.median(area.reshape(-1,3),axis=0).tolist()
        row['errors']={folder:float(np.mean(np.abs(np.array(row['samples'][folder])-row['samples']['studio']))) for folder in folders}
        rows.append(row)
    result={'case':'01_daylight','description':'Fixed surface RGB medians; supplemental color probes, not a complete material parity score.',
            'mean_errors':{folder:float(np.mean([r['errors'][folder] for r in rows])) for folder in folders},'materials':rows}
    (root/'material_probes.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result['mean_errors']))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path('build/visual_parity'));parser.add_argument('--folders',nargs='+',default=['post_baseline','blender'])
    args=parser.parse_args();evaluate(args.root.resolve(),args.folders)
