"""Deterministic diagnostic maps, not artistic textures; standard library only."""
import json, math, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from receiver import png_bytes

def generate(root):
    root.mkdir(parents=True,exist_ok=True)
    size=256
    outputs={}
    for kind in ('color_alpha','scalar','normal'):
        raw=bytearray()
        for y in range(size):
            for x in range(size):
                if kind=='color_alpha':
                    # Asymmetric colors identify face orientation; alpha columns
                    # contain exact 0, 1/4, 1/2, 3/4, and 1 coverage.
                    colors=((230,60,35),(40,205,80),(40,80,230),(230,190,45))
                    rgb=colors[(y//128)*2+x//128]
                    if x<12 or y<12: rgb=(245,245,245)
                    if x>224 and y>208: rgb=(15,15,15)
                    alpha=(0,64,128,192,255)[min(4,x*5//size)]
                    pixel=(*rgb,alpha)
                elif kind=='scalar':
                    # Colored data detects accidental packed-channel decoding.
                    pixel=((32,224,96,160)[x//64],(224,32,160,96)[x//64],128,255)
                else:
                    nx=.45*math.sin(x/size*math.tau*4)
                    ny=.45*math.sin(y/size*math.tau*4)
                    nz=math.sqrt(1-nx*nx-ny*ny)
                    pixel=tuple(round(127.5*(v+1)) for v in (nx,ny,nz))+(255,)
                raw.extend(pixel)
        path=root/(kind+'.png'); path.write_bytes(png_bytes(size,size,raw))
        outputs[kind]=str(path.resolve())
    (root/'source.json').write_text(json.dumps(outputs,indent=2))
    print(json.dumps(outputs))
if __name__=='__main__':generate(Path('build/material_parity/assets'))
