"""Native EEVEE approximations of Roblox's finite local-light envelope.

The signed, coincident lamps cancel EEVEE's inverse-square term. They must
share position, color, cone and shadow settings; they are one imported source.
No material or compositor rewrites are involved. See tools/light_parity/README.md.
"""
import math
import bpy
from mathutils import Vector, Matrix
from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat

# Least-squares approximation of clamp((1-d/Range)/.25, 0, 1)^2.
# Each coefficient multiplies EEVEE's (1-(d/cutoff)^4)^2 / d^2.
# The outer positive lamp is first so the source remains easy to select.
ENVELOPE = (
    (1., .9542511913885077), (.9, .9364532864691555),
    (.82, -2.0314444585800464), (.73, .2460712318032601),
    (.6, .0020421816689727913), (.45, -.05415019180297224),
    (.3, -.03596207639106601), (.16, -.012500763690124598),
    (.08, -.00398463615873495), (.03, -.0006433515903793168),
)
# Fitted to the same finite Roblox envelope, sampled at .03..1 Range.
# RMS response error .054, maximum .112 over that interval. Large scenes
# use one cone/sample per source to bound native lamp and shadow-map cost.
COMPACT_ENVELOPE = (
    (1.,1.1937020494496302),(.06,-.001611005085397556),
    (.12,-.009215560492455829),(.3,-.06027677523966367),
    (.8,-1.1219073133208275),
)
LIGHT_HELPER_BUDGET = 2048

def lighting_profile(lights,parts,quality='auto'):
    if quality in ('accurate','compact','native'):return quality
    by_ref={p.get('inst_ref'):p for p in parts if isinstance(p,dict)}
    predicted=0;count=0
    for source in lights or ():
        part=by_ref.get(source.get('parent_ref'));kind=source.get('class_name')
        if part is None or not source.get('enabled',True) or kind not in ('PointLight','SpotLight','SurfaceLight'):continue
        count+=1;samples=1
        if kind=='SurfaceLight' and source.get('attachment_cf') is None:
            face=int(source.get('face',5));axis=face%3 if face in (0,1,2) else (face-3)%3
            size=part.get('part_size') or (1,1,1)
            for k in range(3):
                if k!=axis:samples*=3 if float(size[k])>2 or float(source.get('angle',90))<1 else 1
        predicted+=len(ENVELOPE)*(1 if kind=='PointLight' else 2)*samples
    if predicted<=LIGHT_HELPER_BUDGET:return 'accurate'
    return 'compact' if count*len(COMPACT_ENVELOPE)<=LIGHT_HELPER_BUDGET else 'native'

FACE_NORMALS = ((1,0,0),(0,1,0),(0,0,1),(-1,0,0),(0,-1,0),(0,0,-1))

def create_lights(collection, lights, parts, quality="auto"):
    from .textures import _linear_rgb
    profile=lighting_profile(lights,parts,quality)
    by_ref={entry.get('inst_ref'):entry for entry in parts if isinstance(entry,dict)}
    created=0
    hosts={int(obj["RBXInstRef"]):obj for obj in collection.all_objects
           if obj.type=='MESH' and obj.get("RBXInstRef") is not None}
    shadow_groups={}
    for source in lights or []:
        part=by_ref.get(source.get('parent_ref'))
        kind=source.get('class_name')
        if part is None or not source.get('enabled',True) or kind not in ('PointLight','SpotLight','SurfaceLight'):
            continue
        brightness=max(0.,float(source.get('brightness',1.)))
        radius=max(0.,float(source.get('range',8. if kind=='PointLight' else 16.)))
        angle=max(0.,min(180.,float(source.get('angle',90.))))
        matrix=get_transform_to_blender() @ cf_to_mat(part.get('part_cf'))
        attachment=source.get('attachment_cf')
        if attachment is not None:matrix=matrix @ cf_to_mat(attachment)
        face=int(source.get('face',5));face=face if 0<=face<6 else 5
        normal=Vector(FACE_NORMALS[face]);direction=(matrix.to_3x3()@normal).normalized()
        origin=matrix.translation.copy()
        # Studio SpotLights originate at the part center. Only a part-mounted
        # SurfaceLight starts on a face. Face also applies inside Attachments.
        size=part.get('part_size') or (1.,1.,1.)
        axis=max(range(3),key=lambda i:abs(normal[i]))
        axes=[i for i in range(3) if i!=axis]
        surface=kind=='SurfaceLight' and attachment is None
        if surface:origin=matrix @ (normal*float(size[axis])*.5)
        apex_offset=0.
        samples=[(origin,1.,angle)]
        if surface:
            # Finite face quadrature. EEVEE area lamps cannot restrict beam
            # spread, so sample soft spot cones across the actual rectangle.
            # Cap the grid independently of part size to keep imports bounded.
            widths=[max(.001,float(size[i])) for i in axes]
            counts=[3 if profile=='accurate' and (w>2. or angle<1.) else 1 for w in widths]
            if 5.<=angle<179.9:
                cell_minor=min(widths[i]/counts[i] for i in range(2))
                apex_offset=cell_minor/(2.*math.tan(math.radians(angle)*.5))
            samples=[]
            for u in range(counts[0]):
                for v in range(counts[1]):
                    local=Vector((0,0,0))
                    local[axes[0]]=((u+.5)/counts[0]-.5)*widths[0]
                    local[axes[1]]=((v+.5)/counts[1]-.5)*widths[1]
                    cell=max(widths[0]/counts[0],widths[1]/counts[1])
                    # Approximate the remaining cell extent over the middle
                    # of its influence interval, rather than deriving size
                    # directly from Range as the previous disk emitter did.
                    extent=cell*(2. if angle<1. else 1.)
                    cone=min(180.,math.degrees(2*math.atan(math.tan(math.radians(min(angle,179.9))*.5)+extent/max(.01,radius))))
                    if apex_offset:cone=angle
                    if angle>=180:cone=180.
                    samples.append((origin-direction*apex_offset+matrix.to_3x3()@local,1./(counts[0]*counts[1]),cone))
            # Normalize overlapping samples at the center of the emitting
            # face, evaluated at half Range. Averaging would darken large
            # panels simply because they require more quadrature samples.
            total=0.
            depth=max(.01,radius*.5)+apex_offset
            for position,weight,cone in samples:
                lateral=(position-(origin-direction*apex_offset)).length
                cosine=depth/math.sqrt(depth*depth+lateral*lateral)
                for width,mix in ((cone,.25),(cone*.86,.75)):
                    edge=math.cos(math.radians(width)*.5)
                    t=max(0.,min(1.,(cosine-edge)/max(1e-9,1.-edge)))
                    total+=cosine*mix*t*t*(3.-2.*t)
            samples=[(position,1./max(.001,total),cone) for position,_,cone in samples]
        samples.sort(key=lambda sample:(sample[0]-origin).length_squared)
        native_radius=radius+apex_offset if radius else 0.
        name=source.get('name') or kind
        root=None
        blockers=None
        host=hosts.get(source.get('parent_ref'))
        # Roblox excludes a local light's own BasePart from its shadow map.
        # Keep that exception local to this source: the host still shadows
        # every other lamp. EEVEE supports shadow linking in modern Blender.
        if source.get('shadows',False) and host is not None and bpy.app.version>=(4,3,0):
            key=host.as_pointer()
            blockers=shadow_groups.get(key)
            if blockers is None:
                blockers=bpy.data.collections.new('RBX self-shadow exclusion '+host.name)
                blockers.objects.link(host)
                blockers.collection_objects[0].light_linking.link_state='EXCLUDE'
                shadow_groups[key]=blockers
        for sample_index,(position,weight,cone) in enumerate(samples):
            # Two smooth native cones approximate Studio's quadratic cone
            # response without clamping wide cones to the old 60-degree fit.
            inner=.86 if surface else .75-.1*max(0.,(angle-60.)/120.)
            cones=([(cone,.25),(cone*inner,.75)] if profile=='accurate' else [(cone*.9,1.)]) if kind!='PointLight' else [(0.,1.)]
            if brightness==0 or radius==0:cones=cones[:1]
            for cone_index,(cone_width,cone_weight) in enumerate(cones):
                envelope=ENVELOPE if profile=='accurate' else COMPACT_ENVELOPE if profile=='compact' else ((1.,.08),)
                lobes=envelope if brightness and radius else ((1.,0.),)
                for lobe_index,(cutoff,power) in enumerate(lobes):
                    data=bpy.data.lights.new(name if root is None else name+' falloff','POINT' if kind=='PointLight' else 'SPOT')
                    data.color=_linear_rgb(source.get('color') or (1.,1.,1.))
                    data.energy=45.*brightness*native_radius*native_radius*power*weight*cone_weight
                    data.use_custom_distance=True;data.cutoff_distance=max(.01,native_radius*cutoff)
                    data.shadow_soft_size=.001
                    if apex_offset:data.shadow_buffer_clip_start=apex_offset+.001
                    data.use_shadow=bool(source.get('shadows',False))
                    if hasattr(data,'use_shadow_jitter'):data.use_shadow_jitter=False
                    if data.type=='SPOT':
                        data.spot_size=max(math.radians(.01),math.radians(cone_width))
                        data.spot_blend=1.
                    obj=bpy.data.objects.new(name if root is None else name+' falloff',data)
                    collection.objects.link(obj)
                    if blockers is not None:obj.light_linking.blocker_collection=blockers
                    obj.rotation_mode='QUATERNION'
                    world=Matrix.Translation(position) @ direction.to_track_quat('-Z','Y').to_matrix().to_4x4()
                    obj.matrix_world=world
                    data['rbx_range']=radius;data['rbx_angle']=angle
                    data['rbx_surface_apex_offset']=apex_offset
                    if root is None:
                        root=obj;obj['RBXLightSource']=kind;obj['RBXLightVersion']=2;obj['RBXLightQuality']=profile
                        obj['rbx_brightness']=brightness;obj['rbx_face']=face
                        obj['rbx_surface_size']=[float(size[i]) for i in axes] if surface else [0.,0.]
                        obj['rbx_emitter_origin']=list(origin)
                    else:
                        obj.parent=root;obj.matrix_parent_inverse=root.matrix_world.inverted();obj.matrix_basis=world
                        obj['RBXLightHelper']=True
        created+=1
    if profile!='accurate':print(f'[RbxLighting] {created} sources use {profile} lighting ({len(collection.objects)} objects in collection).')
    return created
