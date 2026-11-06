"""Deterministic Studio/Blender fixtures from one scene description (stdlib only)."""
import argparse
import copy
import json
import math
import struct
from pathlib import Path
from xml.etree import ElementTree as ET

IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1]


def cf(position):
    return list(position) + IDENTITY


def look_at(eye, target):
    def unit(v):
        length = math.sqrt(sum(x*x for x in v))
        return [x/length for x in v]
    def cross(a, b):
        return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
    forward = unit([b-a for a, b in zip(eye, target)])
    right = unit(cross(forward, [0, 1, 0]))
    up = cross(right, forward)
    return list(eye) + [v for row in zip(right, up, [-x for x in forward]) for v in row]


def node(cls, parent, **properties):
    return {'class': cls, 'parent': parent, 'properties': properties}


def part(name, position, size, color, material=256, shape=1, **extra):
    return node('Part', 0, Name=['string', name], CFrame=['CoordinateFrame', cf(position)],
                Size=['Vector3', size], Color3uint8=['Color3uint8', color],
                Material=['token', material], Shape=['token', shape],
                Anchored=['bool', True], CanCollide=['bool', True],
                TopSurface=['token', 0], BottomSurface=['token', 0], **extra)


def fixture(case):
    # The studio camera and Blender camera both use vertical FOV.
    camera_cf = look_at([24, 8, 32], [0, 3, 0]) if case.get('room') else look_at([27, 23, 35], [0, 2, 0])
    nodes = [node('Workspace', -1, Name=['string', 'Workspace'], CurrentCamera=['Ref', 2]),
             node('Lighting', -1, Name=['string', 'Lighting'], **{
                 'Ambient': ['Color3', [0, 0, 0]], 'OutdoorAmbient': ['Color3', [0.5, 0.5, 0.5]],
                 'LightingStyle': ['token', 0], 'PrioritizeLightingQuality': ['bool', True],
                 'Technology': ['token', 4], 'Brightness': ['float', 2], 'ClockTime': ['float', 14],
                 'TimeOfDay': ['string', '14:00:00'], 'GeographicLatitude': ['float', 41.733],
                 'GlobalShadows': ['bool', True], 'ShadowSoftness': ['float', 0.2],
                 'ExposureCompensation': ['float', 0], 'EnvironmentDiffuseScale': ['float', 1],
                 'EnvironmentSpecularScale': ['float', 1], 'FogStart': ['float', 0],
                 'FogEnd': ['float', 100000], 'FogColor': ['Color3', [0.75, 0.75, 0.75]],
                 'ColorShift_Top': ['Color3', [0, 0, 0]], 'ColorShift_Bottom': ['Color3', [0, 0, 0]],
             }), node('Camera', 0, Name=['string', 'ParityCamera'],
                       CFrame=['CoordinateFrame', camera_cf], FieldOfView=['float', 45],
                       Focus=['CoordinateFrame', cf([0, 2, 0])], CameraType=['token', 6]),
             node('MaterialService', -1, Name=['string', 'MaterialService'], Use2022MaterialsXml=['bool', True])]
    nodes.append(part('Floor', [0, -0.5, 0], [46, 1, 38], [150, 150, 150], 272))
    materials = [('Plastic', 256), ('SmoothPlastic', 272), ('Metal', 1088), ('Glass', 1568),
                 ('Neon', 288), ('Concrete', 816), ('Wood', 512), ('Brick', 848),
                 ('Fabric', 1312), ('Marble', 784), ('Ice', 1536), ('Grass', 1280)]
    for i, (name, material) in enumerate(materials):
        x, z = (i % 4 - 1.5)*8, (i // 4 - 1)*9
        tint = [160, 160, 160] if i < 4 else [170, 110, 75]
        extra = {'Transparency': ['float', 0.35]} if name in ('Glass', 'Ice') else {}
        nodes.append(part(name, [x, 2.25, z], [4.5, 4.5, 4.5], tint, material,
                          shape=0 if i % 2 else 1, **extra))
    for i, color in enumerate(([32]*3, [64]*3, [128]*3, [192]*3, [255]*3,
                                [255, 32, 32], [32, 255, 32], [32, 32, 255])):
        nodes.append(part('ColorPatch'+str(i), [-14 + i*4, 0.05, 15], [3, 0.1, 3], color, 272))
    for name, value in case.get('lighting', {}).items():
        kind = nodes[1]['properties'][name][0]
        nodes[1]['properties'][name] = [kind, value]
    time = float(nodes[1]['properties']['ClockTime'][1])
    nodes[1]['properties']['TimeOfDay'] = ['string', f'{int(time):02d}:{int((time%1)*60):02d}:00']
    if case.get('room'):
        for name, pos, size in [('BackWall',[0,5,-17],[40,10,1]),
                                ('Roof',[0,10,0],[40,1,34]), ('LeftWall',[-20,5,0],[1,10,34])]:
            nodes.append(part(name,pos,size,[190]*3,272))
    if case.get('light'):
        spec = case['light']
        owner = len(nodes)
        nodes.append(part('LightMount',[0,8,3],[1,1,1],[255]*3,272,
                          Transparency=['float',1], CastShadow=['bool',False]))
        parent = owner
        if spec.get('attached'):
            parent = len(nodes)
            # A rotated attachment pointing down, offset from the host part.
            local = [2,-1,0, 1,0,0, 0,0,1, 0,-1,0]
            nodes.append(node('Attachment',owner,Name=['string','Emitter'],CFrame=['CoordinateFrame',local]))
        nodes.append(node(spec.get('class','PointLight'),parent, Name=['string','TestLight'],
                          Brightness=['float',spec.get('brightness',1)], Range=['float',20],
                          Angle=['float',60], Face=['token',4], Color=['Color3',[1,0.85,0.65]],
                          Enabled=['bool',True],Shadows=['bool',True]))
    if case.get('light') and case['light'].get('class','PointLight')=='PointLight':
        nodes[-1]['properties'].pop('Angle',None)
        nodes[-1]['properties'].pop('Face',None)
    if case.get('correction'):
        nodes.append(node('ColorCorrectionEffect',1,Name=['string','Correction'],Enabled=['bool',True],
                          **{k:['Color3' if k=='TintColor' else 'float',v] for k,v in case['correction'].items()}))
    nodes.append(node('StringValue', 0, Name=['string', 'RBXParityFixtureMarker'], Value=['string','RBXMonkey visual parity fixture']))
    return {'id':case['id'],'nodes':nodes,'camera_cf':camera_cf,'fov':45,'width':960,'height':640,
            'description':case['description']}


CASES = [
    {'id':'01_daylight','description':'12 builtin materials, neutral/color patches; noon baseline'},
    {'id':'02_dawn','description':'Low solar elevation','lighting':{'ClockTime':6.5}},
    {'id':'03_dusk','description':'Evening shadows','lighting':{'ClockTime':17.5}},
    {'id':'04_night','description':'Night sky and dark materials','lighting':{'ClockTime':0}},
    {'id':'05_no_environment','description':'Environment scales zero','lighting':{'EnvironmentDiffuseScale':0,'EnvironmentSpecularScale':0}},
    {'id':'06_diffuse_only','description':'Sky diffuse without specular','lighting':{'EnvironmentDiffuseScale':1,'EnvironmentSpecularScale':0}},
    {'id':'07_specular_only','description':'Sky specular without diffuse','lighting':{'EnvironmentDiffuseScale':0,'EnvironmentSpecularScale':1}},
    {'id':'08_ambient','description':'Ambient with sun off','lighting':{'Brightness':0,'Ambient':[0.25,0.25,0.25],'OutdoorAmbient':[0.25,0.25,0.25]}},
    {'id':'09_shadowless','description':'GlobalShadows disabled','lighting':{'GlobalShadows':False}},
    {'id':'10_indoor','description':'Sheltered surfaces vs outdoor fill','room':True},
    {'id':'11_point','description':'Point light at night','lighting':{'ClockTime':0,'Brightness':0},'light':{'class':'PointLight'}},
    {'id':'12_point_zero','description':'Zero brightness stays dark','lighting':{'ClockTime':0,'Brightness':0},'light':{'class':'PointLight','brightness':0}},
    {'id':'13_attached_spot','description':'Attachment position/orientation and local spotlight','lighting':{'ClockTime':0,'Brightness':0},'light':{'class':'SpotLight','attached':True}},
    {'id':'14_exposure','description':'Exposure +1 stop','lighting':{'ExposureCompensation':1}},
    {'id':'15_correction','description':'Postprocess brightness, tint, saturation','correction':{'Brightness':0.15,'Contrast':0.1,'Saturation':-0.5,'TintColor':[1,0.85,0.7]}},
    {'id':'16_soft_shadows','description':'Shadow softness endpoint','lighting':{'ShadowSoftness':1}},
]


def _string(value):
    data=value.encode(); return struct.pack('<I',len(data))+data


def _i32(value):
    return struct.pack('>I', (value<<1) ^ (value>>31))


def _f32(value):
    bits=struct.unpack('<I',struct.pack('<f',value))[0]
    return struct.pack('>I',((bits<<1)&0xffffffff)|(bits>>31))


def _refs(values):
    last=0; raw=[]
    for value in values:
        raw.append(_i32(value-last)); last=value
    return bytes(v[lane] for lane in range(4) for v in raw)


def _chunk(name,data):
    return name.ljust(4,b'\0')+struct.pack('<III',0,len(data),0)+data


def binary(scene):
    chunks=[]; nodes=scene['nodes']
    for ref, item in enumerate(nodes):
        cls=item['class']
        chunks.append(_chunk(b'INST',struct.pack('<I',ref)+_string(cls)+b'\0'+struct.pack('<I',1)+_refs([ref])))
        for name,(kind,value) in item['properties'].items():
            if kind=='string': type_id,payload=1,_string(value)
            elif kind=='bool': type_id,payload=2,bytes([bool(value)])
            elif kind=='float': type_id,payload=5,struct.pack('<d',value)
            elif kind=='token': type_id,payload=0x12,struct.pack('>I',value)
            elif kind=='Color3': type_id,payload=7,b''.join(_f32(v) for v in value)
            elif kind=='Color3uint8': type_id,payload=0x1a,bytes(value)
            elif kind=='Vector3': type_id,payload=0xe,b''.join(_f32(v) for v in value)
            elif kind=='CoordinateFrame': type_id,payload=0x10,b'\0'+struct.pack('<9f',*value[3:])+b''.join(_f32(v) for v in value[:3])
            elif kind=='Ref': type_id,payload=0x13,_refs([value])
            else: raise ValueError(kind)
            chunks.append(_chunk(b'PROP',struct.pack('<I',ref)+_string(name)+bytes([type_id])+payload))
    chunks.append(_chunk(b'PRNT',b'\0'+struct.pack('<I',len(nodes))+_refs(range(len(nodes)))+_refs([n['parent'] for n in nodes])))
    chunks.append(_chunk(b'END',b'</roblox>'))
    return b'<roblox!\x89\xff\r\n\x1a\n'+struct.pack('<HII',0,len(nodes),len(nodes))+bytes(8)+b''.join(chunks)


def xml(scene):
    root=ET.Element('roblox',{'version':'4'})
    elements=[]
    for i,item in enumerate(scene['nodes']):
        parent=root if item['parent']==-1 else elements[item['parent']]
        element=ET.SubElement(parent,'Item',{'class':item['class'],'referent':'RBX'+str(i)})
        elements.append(element)
        props=ET.SubElement(element,'Properties')
        for name,(kind,value) in item['properties'].items():
            prop=ET.SubElement(props,kind,{'name':name})
            if kind=='CoordinateFrame':
                for key,v in zip(('X','Y','Z','R00','R01','R02','R10','R11','R12','R20','R21','R22'),value):
                    ET.SubElement(prop,key).text=str(v)
            elif kind in ('Color3','Vector3'):
                for key,v in zip(('R','G','B') if kind=='Color3' else ('X','Y','Z'),value):
                    ET.SubElement(prop,key).text=str(v)
            elif kind=='Color3uint8': prop.text=str(0xff000000 | value[0]<<16 | value[1]<<8 | value[2])
            elif kind=='Ref':prop.text='RBX'+str(value)
            elif kind=='bool':prop.text=str(value).lower()
            else:prop.text=str(value)
    ET.indent(root)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def generate(output):
    output.mkdir(parents=True,exist_ok=True)
    scenes=[fixture(case) for case in CASES]
    for scene in scenes:
        (output/(scene['id']+'.rbxl')).write_bytes(binary(scene))
        (output/(scene['id']+'.rbxlx')).write_bytes(xml(scene))
        template = (Path(__file__).parent/'studio_case.luau').read_text()
        (output/(scene['id']+'.luau')).write_text(template.replace('__SCENE_JSON__',json.dumps(scene)))
    (output/'manifest.json').write_text(json.dumps(scenes,indent=2)+'\n')
    print(f'Generated {len(scenes)} cases, {len(scenes[0]["nodes"])} baseline nodes: {output}')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('build/visual_parity'))
    generate(parser.parse_args().output)
