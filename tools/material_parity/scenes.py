"""Material diagnostics with persistent uploaded maps and explicit sample labels."""
import copy,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from scenes import node,part,cf,look_at,fixture
ROOT=Path('build/material_parity')
UP=json.loads((ROOT/'assets/uploads.json').read_text())
C=UP['color_alpha']['usableUri']; S=UP['scalar']['usableUri']; N=UP['normal']['usableUri']
SPHERE='rbxasset://models/MaterialManager/smooth_sphere.mesh'
TINTS=([1,1,1],[.5,.5,.5],[.2,.65,.9],[1,.3,.1],[.1,.9,.2],[0,0,0])

def base(id,description):
    spec=fixture({'id':id,'description':description})
    spec['nodes']=spec['nodes'][:4]
    spec['camera_cf']=look_at([0,0,40],[0,0,0]);spec['nodes'][2]['properties']['CFrame'][1]=spec['camera_cf']
    spec['fov']=32;spec['nodes'][2]['properties']['FieldOfView'][1]=32
    # Keep the previously calibrated daylight lighting constant.
    spec['samples']=[]
    spec['nodes'].append(part('Background',[0,0,-4],[34,24,.4],[95,115,135],272,CastShadow=['bool',False]))
    return spec

def add(spec,i,name,mesh=False,color=(.65,.32,.15),trans=0,material=272):
    x=(i%6-2.5)*4; y=(1.5-i//6)*4
    obj=part(name,[x,y,0],[3.2,3.2,.4] if not mesh else [3.2]*3,[round(c*255) for c in color],material,
             Transparency=['float',trans],CastShadow=['bool',False])
    if mesh:
        obj['class']='MeshPart';obj['properties'].pop('Shape');obj['properties']['MeshId']=['string',SPHERE]
        obj['properties']['MeshSize']=['Vector3',[171.2562713623047]*3]
    parent=len(spec['nodes']);spec['nodes'].append(obj)
    spec['samples'].append({'name':name,'center':[x,y,0],'index':i,'mesh':mesh})
    return parent

def sa(spec,parent,mode=0,tint=(1,1,1),**extra):
    obj=node('SurfaceAppearance',parent,Name=['string','Surface'],AlphaMode=['token',mode],Color=['Color3',list(tint)],ColorMap=['string',C])
    obj['properties'].update({k:['Color3' if k=='EmissiveTint' else 'float' if k=='EmissiveStrength' else 'string',v] for k,v in extra.items()})
    spec['nodes'].append(obj)

def decal(spec,parent,cls='Decal',**extra):
    props=dict(Name=['string','Image'],Texture=['string',C],Face=['token',2],Color3=['Color3',[1,1,1]],Transparency=['float',0],ZIndex=['float',1])
    kinds={'Color3':'Color3','Face':'token','Texture':'string','UVScale':'Vector2','UVOffset':'Vector2'}
    props.update({k:[kinds.get(k,'float'),v] for k,v in extra.items()})
    spec['nodes'].append(node(cls,parent,**props))

scenes=[]
spec=base('01_sa_alpha','SurfaceAppearance: rows Overlay/Transparency/TintMask/Opaque; columns six SA tints; orange Part color')
for mode in range(4):
    for col,tint in enumerate(TINTS):
        p=add(spec,mode*6+col,f'mode{mode}_tint{col}',True);sa(spec,p,mode,tint)
scenes.append(spec)
spec=base('02_sa_part_alpha','Rows four AlphaModes; columns Part.Transparency 0/.25/.5/.75/.95/1')
for mode in range(4):
    for col,t in enumerate((0,.25,.5,.75,.95,1)):
        p=add(spec,mode*6+col,f'mode{mode}_part{t}',True,trans=t);sa(spec,p,mode,(.3,.75,.9))
scenes.append(spec)
spec=base('03_decal_tint','Decals: rows Transparency 0/.25/.5/1; columns six tints')
for row,t in enumerate((0,.25,.5,1)):
    for col,tint in enumerate(TINTS):
        p=add(spec,row*6+col,f'decal_t{t}_color{col}');decal(spec,p,Color3=tint,Transparency=t)
scenes.append(spec)
spec=base('04_texture_uv','Texture: asymmetric U/V tile sizes and positive/negative stud offsets; last row transparency/tint')
for i in range(24):
    u,v=((2,2),(1,2),(2,1),(3.2,1.6),(.8,3.2),(4,4))[i%6]
    ou,ov=((0,0),(.5,1),(-.5,-1),(1.1,-.7))[i//6]
    p=add(spec,i,f'tile{i}_u{u}_v{v}_off{ou}_{ov}')
    decal(spec,p,'Texture',StudsPerTileU=u,StudsPerTileV=v,OffsetStudsU=ou,OffsetStudsV=ov,Transparency=.4 if i>=18 else 0,Color3=(.35,.7,1) if i>=18 else (1,1,1))
scenes.append(spec)
spec=base('05_decal_uv','Decal rotation, UVScale and UVOffset; last row reverse-created ZIndex layers and Part transparency')
for i in range(24):
    p=add(spec,i,f'decal_uv{i}',trans=(i-18)*.2 if i>=18 else 0)
    if i<18:
        decal(spec,p,Rotation=(i%6)*45,UVScale=[(.5,1,2)[i//6]]*2,UVOffset=[.2 if i%2 else 0,-.15 if i%3==0 else 0])
    else:
        decal(spec,p,Color3=[1,.1,.1],ZIndex=10,Transparency=.35)
        decal(spec,p,Color3=[.1,1,1],ZIndex=-2,Transparency=.35)
scenes.append(spec)
spec=base('06_sa_pbr','SurfaceAppearance: baseline, red-channel roughness/metalness, shared-map scalar channels, normal map, emission strength/tint')
configs=[{},dict(RoughnessMap=S),dict(MetalnessMap=S),dict(MetalnessMap=S,RoughnessMap=S),dict(NormalMap=N),dict(NormalMap=N,RoughnessMap=S,MetalnessMap=S)]
for i in range(24):
    p=add(spec,i,f'pbr{i}',True);extra=configs[i%6].copy()
    if i>=12:extra.update(EmissiveMaskContent=S,EmissiveStrength=(0,1,2,5)[(i-12)//3],EmissiveTint=[.3,.8,1] if i%2 else [1,1,1])
    sa(spec,p,3,(1,1,1) if i<6 else (.4,.7,.9),**extra)
scenes.append(spec)
spec=base('07_material_alpha','Builtins: SmoothPlastic/Metal/Glass/Neon/Concrete/Fabric; rows Part.Transparency 0/.25/.6/1')
for i in range(24):
    p=add(spec,i,f'builtin{i}',color=(.4,.7,.9),trans=(0,.25,.6,1)[i//6],material=(272,1088,1568,288,816,1312)[i%6])
scenes.append(spec)
spec=base('08_decal_faces','All six NormalId faces rotated toward camera, rows face decals, tiled Texture, tinted decal, tilted cubes')
rots=[(0,0,-1,0,1,0,1,0,0),(1,0,0,0,0,-1,0,1,0),(1,0,0,0,1,0,0,0,1),(0,0,1,0,1,0,-1,0,0),(1,0,0,0,0,1,0,-1,0),(-1,0,0,0,1,0,0,0,-1)]
for i in range(24):
    p=add(spec,i,f'face{i%6}_row{i//6}')
    spec['nodes'][p]['properties']['Size'][1]=[3.2]*3
    spec['nodes'][p]['properties']['CFrame'][1][3:]=rots[i%6]
    decal(spec,p,'Texture' if i//6==1 else 'Decal',Face=i%6,**({'StudsPerTileU':2,'StudsPerTileV':1.5} if i//6==1 else {}),Color3=(.3,.7,1) if i//6==2 else (1,1,1))
scenes.append(spec)
spec=base('09_surface_precedence','Multiple SurfaceAppearances, classic MeshPart.TextureID, SurfaceAppearance without color map; two Part colors')
for i in range(24):
    p=add(spec,i,f'precedence{i}',True,color=(.65,.32,.15) if i<12 else (.1,.6,.8))
    if i%6 in (0,1,2):spec['nodes'][p]['properties']['TextureID']=['string',C]
    if i%6 in (1,2,3,4):sa(spec,p,0,(.4,.8,.5))
    if i%6 in (2,4):sa(spec,p,3,(.9,.2,.3),ColorMap=S)
    if i%6==5:
        sa(spec,p,3,(.4,.8,.5),NormalMap=N)
        spec['nodes'][-1]['properties'].pop('ColorMap')
scenes.append(spec)
spec=base('10_surface_controls','Mesh texture versus Part tint; SA without albedo; two SA children with an Overlay last; nearest versus filtered sampling')
for i in range(24):
    p=add(spec,i,f'control{i}',True,color=TINTS[i%6])
    if i<6:spec['nodes'][p]['properties']['TextureID']=['string',C]
    elif i<12:
        sa(spec,p,0,TINTS[i%6],NormalMap=N)
        spec['nodes'][-1]['properties'].pop('ColorMap')
    elif i<18:
        sa(spec,p,3,(1,1,1),ColorMap=S,NormalMap=N)
        sa(spec,p,0,(1,1,1))
    else:
        sa(spec,p,3,(1,1,1))
        spec['nodes'][-1]['properties']['ResampleMode']=['token',i%2]
scenes.append(spec)
ROOT.mkdir(exist_ok=True)
(ROOT/'manifest.json').write_text(json.dumps(scenes,indent=2))
template=(Path(__file__).parent/'studio_case.luau').read_text()
for spec in scenes:(ROOT/(spec['id']+'.luau')).write_text(template.replace('__SCENE_JSON__',json.dumps(spec)))
print('Generated',len(scenes),'cases,',sum(len(s['samples']) for s in scenes),'samples')
