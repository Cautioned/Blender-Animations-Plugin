"""EEVEE Neon radiance pass and a non-destructive viewport/render compositor."""
import json,math
import bpy
from ..core.neon import (NEON_RADIANCE,NEON_RESPONSE_POWER,NEON_RESPONSE_K,
                        NEON_EXPOSURE_SCALE,NEON_AOV,NEON_PIPELINE_VERSION,bloom_settings)

_TAG='RBXNeonManaged'


def ensure_neon_aov(scene):
    for layer in scene.view_layers:
        aov=next((a for a in layer.aovs if a.name==NEON_AOV),None)
        if aov is None:aov=layer.aovs.add();aov.name=NEON_AOV
        aov.type='COLOR'


def bind_neon_material(material,principled,color_socket):
    """Unlit radiance; the compositor supplies the measured display curve."""
    nodes,links=material.node_tree.nodes,material.node_tree.links
    base=principled.inputs['Base Color']
    for link in list(base.links):links.remove(link)
    base.default_value=(0,0,0,1)
    spec=principled.inputs.get('Specular IOR Level') or principled.inputs.get('Specular')
    if spec is not None:spec.default_value=0
    principled.inputs['Roughness'].default_value=1
    emission=principled.inputs.get('Emission Color') or principled.inputs.get('Emission')
    links.new(color_socket,emission)
    principled.inputs['Emission Strength'].default_value=NEON_RADIANCE
    scale=nodes.new('ShaderNodeVectorMath');scale.operation='SCALE';scale.name='RBX Neon Radiance'
    scale.inputs['Scale'].default_value=NEON_RADIANCE
    links.new(color_socket,scale.inputs[0])
    aov=nodes.new('ShaderNodeOutputAOV');aov.name='RBX Neon Radiance Pass'
    if hasattr(aov,'aov_name'):aov.aov_name=NEON_AOV
    else:aov.name=NEON_AOV
    links.new(scale.outputs['Vector'],aov.inputs['Color'])
    material['RBXNeon']=True
    ensure_neon_aov(bpy.context.scene)
    configure_neon_compositor(bpy.context.scene)


def _root_tree(scene):
    modern=hasattr(scene,'compositing_node_group')
    tree=scene.compositing_node_group if modern else getattr(scene,'node_tree',None)
    if modern and tree is not None and tree.users>1:
        tree=tree.copy();scene.compositing_node_group=tree
    if tree is None:
        if modern:
            tree=bpy.data.node_groups.new('RBX Scene Compositor','CompositorNodeTree')
            tree.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
            scene.compositing_node_group=tree
            output=tree.nodes.new('NodeGroupOutput')
        else:
            scene.use_nodes=True;tree=scene.node_tree
            output=next((n for n in tree.nodes if n.type=='COMPOSITE'),None) or tree.nodes.new('CompositorNodeComposite')
        source=next((n for n in tree.nodes if n.type=='R_LAYERS'),None) or tree.nodes.new('CompositorNodeRLayers')
        tree.links.new(source.outputs['Image'],output.inputs[0])
        source.location=(-440,80);output.location=(440,80)
    return tree,modern


def _input(group,name):
    group.interface.new_socket(name=name,in_out='INPUT',socket_type='NodeSocketFloat' if name=='Alpha' else 'NodeSocketColor')


def _make_group(scene,settings):
    group=bpy.data.node_groups.new('RBX Neon and Bloom','CompositorNodeTree')
    for name in ('Image','Neon','Alpha'):_input(group,name)
    group.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
    group[_TAG]=True
    nodes,links=group.nodes,group.links
    inp=nodes.new('NodeGroupInput');out=nodes.new('NodeGroupOutput')
    def node(kind,name):
        n=nodes.new(kind);n.name=name;n.location=(220*(len(nodes)%7),-160*(len(nodes)//7));return n
    modern=bpy.app.version>=(5,0,0)
    def mathnode(op,name,a,b=None):
        n=node('ShaderNodeMath' if modern else 'CompositorNodeMath',name);n.operation=op
        for s,v in ((n.inputs[0],a),(n.inputs[1],b)):
            if v is None:continue
            if isinstance(v,(float,int)):s.default_value=v
            else:links.new(v,s)
        return n.outputs[0]
    def mix(op,name,a,b,factor=1):
        n=node('ShaderNodeMixRGB' if modern else 'CompositorNodeMixRGB',name);n.blend_type=op
        n.inputs[0].default_value=factor
        links.new(a,n.inputs[1]);links.new(b,n.inputs[2]);return n.outputs[0]
    exposure=node('ShaderNodeValue' if modern else 'CompositorNodeValue','RBX Scene Exposure')
    driver=exposure.outputs[0].driver_add('default_value').driver
    v=driver.variables.new();v.name='exposure';v.type='SINGLE_PROP'
    v.targets[0].id_type='SCENE';v.targets[0].id=scene;v.targets[0].data_path='view_settings.exposure'
    driver.expression='exposure'
    half=mathnode('MULTIPLY','RBX Neon Exposure Stops',exposure.outputs[0],NEON_EXPOSURE_SCALE)
    core_exposure=mathnode('POWER','RBX Neon Exposure',2,half)
    display_exposure=mathnode('POWER','RBX Display Exposure',2,exposure.outputs[0])
    # Scaling a color by a float uses the compositor's color multiplication.
    def scale(color,factor,name):
        n=node('ShaderNodeMixRGB' if modern else 'CompositorNodeMixRGB',name);n.blend_type='MULTIPLY'
        n.inputs[0].default_value=1;links.new(color,n.inputs[1])
        if isinstance(factor,(int,float)):n.inputs[2].default_value=(factor,factor,factor,1)
        else:links.new(factor,n.inputs[2])
        return n.outputs[0]
    # Image-domain dimensions keep viewport and render radii consistent.
    image_scale=None
    if modern:
        info=node('CompositorNodeImageInfo','RBX Neon Image Dimensions')
        links.new(inp.outputs['Neon'],info.inputs['Image'])
        dimensions=node('ShaderNodeSeparateXYZ','RBX Neon Image Axes')
        links.new(info.outputs['Dimensions'],dimensions.inputs[0])
        image_scale=mathnode('MULTIPLY','RBX Neon Pixel Scale',dimensions.outputs['Y'],1/653)
    exposed=scale(inp.outputs['Neon'],core_exposure,'RBX Exposed Neon Core')
    glow=None
    for i,effect in enumerate(settings):
        intensity=effect['intensity']
        if intensity<=0:continue
        # Roblox's bright saturated Neon colors bloom independently of
        # luminance. Threshold the maximum channel, preserving the hue.
        split=node('CompositorNodeSeparateColor','RBX Bloom Channels '+str(i));split.mode='RGB'
        links.new(exposed,split.inputs[0])
        peak=mathnode('MAXIMUM','RBX Bloom RG '+str(i),split.outputs[0],split.outputs[1])
        peak=mathnode('MAXIMUM','RBX Bloom Peak '+str(i),peak,split.outputs[2])
        excess=mathnode('SUBTRACT','RBX Bloom Excess '+str(i),peak,effect['threshold'])
        positive=mathnode('MAXIMUM','RBX Bloom Positive '+str(i),excess,0)
        fraction=mathnode('DIVIDE','RBX Bloom Highlight Fraction '+str(i),positive,peak)
        fraction=mathnode('POWER','RBX Bloom Soft Threshold '+str(i),fraction,1.0)
        # Bound the glow color to match saturated Studio halos. This avoids
        # turning a saturated yellow surface into an orange halo.
        capped=node('ShaderNodeMixRGB' if modern else 'CompositorNodeMixRGB','RBX Bloom Color Cap '+str(i));capped.blend_type='DARKEN';capped.inputs[0].default_value=1
        links.new(inp.outputs['Neon'],capped.inputs[1]);capped.inputs[2].default_value=(1,1,1,1)
        bounded=scale(capped.outputs[0],NEON_RADIANCE,'RBX Bloom Bounded Radiance '+str(i))
        color=mix('MIX','RBX Bloom Hue '+str(i),inp.outputs['Neon'],bounded,min(1,effect['threshold']))
        highlight=scale(color,fraction,'RBX Bloom Highlights '+str(i))
        blur=node('CompositorNodeBlur','RBX Bloom Blur '+str(i))
        if 'Type' in blur.inputs:blur.inputs['Type'].default_value='Gaussian'
        else:blur.filter_type='GAUSS'
        radius=(8 if effect['implicit'] else 10)+effect['size']*1.1
        small=min(1,effect['size']/8)
        intensity*=1 if effect['implicit'] else .43+.57*small
        tail_weight=.15-.09*small
        tail_scale=3-small
        # Radius is in the logical Studio viewport's pixel units; scale with
        # render resolution, including high-DPI captures and preview renders.
        size_driver=radius*scene.render.resolution_y/653*scene.render.resolution_percentage/100
        if 'Size' in blur.inputs:
            blur.inputs['Size'].default_value=(size_driver,size_driver)
        else:blur.size_x=blur.size_y=max(1,round(size_driver))
        # A small peak-preserving footprint retains subpixel/thin emitters.
        if modern:
            channels=node('CompositorNodeSeparateColor','RBX Glow Footprint Channels '+str(i));channels.mode='RGB'
            links.new(highlight,channels.inputs[0])
            footprint=node('CompositorNodeCombineColor','RBX Glow Footprint '+str(i));footprint.mode='RGB'
            footprint_size=mathnode('MULTIPLY','RBX Glow Footprint Pixels '+str(i),image_scale,1.0)
            for channel in range(3):
                expand=node('CompositorNodeDilateErode',f'RBX Glow Footprint {i} {channel}')
                expand.inputs['Type'].default_value='Distance'
                links.new(channels.outputs[channel],expand.inputs['Mask'])
                links.new(footprint_size,expand.inputs['Size'])
                links.new(expand.outputs['Mask'],footprint.inputs[channel])
            highlight=footprint.outputs[0]
            radius_value=mathnode('MULTIPLY','RBX Bloom Pixel Radius '+str(i),image_scale,radius)
            radius_vector=node('ShaderNodeCombineXYZ','RBX Bloom Radius '+str(i))
            links.new(radius_value,radius_vector.inputs['X']);links.new(radius_value,radius_vector.inputs['Y'])
            links.new(radius_vector.outputs[0],blur.inputs['Size'])
        links.new(highlight,blur.inputs['Image'])
        wide=node('CompositorNodeBlur','RBX Bloom Tail '+str(i))
        if 'Type' in wide.inputs:wide.inputs['Type'].default_value='Gaussian'
        else:wide.filter_type='GAUSS'
        if 'Size' in wide.inputs:wide.inputs['Size'].default_value=(size_driver*tail_scale,size_driver*tail_scale)
        else:wide.size_x=wide.size_y=max(1,round(size_driver*tail_scale))
        if modern:
            wide_radius=mathnode('MULTIPLY','RBX Bloom Tail Pixels '+str(i),radius_value,tail_scale)
            wide_vector=node('ShaderNodeCombineXYZ','RBX Bloom Tail Radius '+str(i))
            links.new(wide_radius,wide_vector.inputs['X']);links.new(wide_radius,wide_vector.inputs['Y'])
            links.new(wide_vector.outputs[0],wide.inputs['Size'])
        links.new(highlight,wide.inputs['Image'])
        spread=mix('MIX','RBX Bloom Spread '+str(i),blur.outputs['Image'],wide.outputs['Image'],tail_weight)
        contribution=scale(spread,intensity,'RBX Bloom Intensity '+str(i))
        glow=contribution if glow is None else mix('ADD','RBX Combined Bloom '+str(i),glow,contribution)
    hdr=exposed if glow is None else mix('ADD','RBX Neon plus Bloom',exposed,glow)
    split=node('CompositorNodeSeparateColor','RBX Neon HDR Channels');split.mode='RGB';links.new(hdr,split.inputs[0])
    combine=node('CompositorNodeCombineColor','RBX Neon Display Response');combine.mode='RGB'
    for i in range(3):
        power=mathnode('POWER','RBX Neon Response Power '+str(i),split.outputs[i],NEON_RESPONSE_POWER)
        denom=mathnode('ADD','RBX Neon Response Denominator '+str(i),power,NEON_RESPONSE_K)
        mapped=mathnode('DIVIDE','RBX Neon Response '+str(i),power,denom)
        unexposed=mathnode('DIVIDE','RBX Neon Display Compensation '+str(i),mapped,display_exposure)
        links.new(unexposed,combine.inputs[i])
    residual=mix('SUBTRACT','RBX Preserve Non-Neon',inp.outputs['Image'],inp.outputs['Neon'])
    result=mix('ADD','RBX Add Neon Display',residual,combine.outputs[0])
    alpha=node('CompositorNodeSetAlpha','RBX Preserve Alpha');links.new(result,alpha.inputs['Image']);links.new(inp.outputs['Alpha'],alpha.inputs['Alpha'])
    links.new(alpha.outputs['Image'],out.inputs[0])
    group['RBXNeonSettings']=json.dumps(settings,sort_keys=True)
    return group


def configure_neon_compositor(scene, post_effects=None):
    """Insert one managed stage before existing render-layer image consumers.

    User grading/output nodes and non-image passes retain their connections.
    An image-only custom compositor without a scene render input is left alone.
    """
    if not str(scene.render.engine).startswith('BLENDER_EEVEE'):
        return False
    ensure_neon_aov(scene)
    tree, modern = _root_tree(scene)
    managed = [n for n in tree.nodes if n.get(_TAG)]
    if post_effects is None and managed:
        settings = json.loads(managed[0].node_tree['RBXNeonSettings'])
    else:
        settings = bloom_settings(post_effects)
    signature = json.dumps({'settings': settings, 'version': NEON_PIPELINE_VERSION, 'scene':scene.name_full}, sort_keys=True)
    if not managed or any(n.get('RBXNeonSignature') != signature for n in managed):
        sources = [n for n in tree.nodes if n.type == 'R_LAYERS'
                   and (n.scene is None or n.scene == scene) and n.outputs['Image'].links]
        if not managed and not sources:
            return False
        group = _make_group(scene, settings)
        if managed:
            for n in managed:
                n.node_tree = group
                n['RBXNeonSignature'] = signature
        else:
            for source in sources:
                destinations = [link.to_socket for link in list(source.outputs['Image'].links)
                                if not (link.to_node.get('RBXPostManaged') and link.to_socket.name == 'Scene')]
                n = tree.nodes.new('CompositorNodeGroup')
                n.name = 'RBX Neon and Bloom'
                n.node_tree = group
                n[_TAG] = True
                n['RBXNeonSignature'] = signature
                n.location = (source.location.x + 220, source.location.y)
                tree.links.new(source.outputs['Image'], n.inputs['Image'])
                socket = source.outputs.get(NEON_AOV)
                if socket is None:
                    source.layer = bpy.context.view_layer.name
                    socket = source.outputs.get(NEON_AOV)
                if socket is not None:
                    tree.links.new(socket, n.inputs['Neon'])
                if source.outputs.get('Alpha') is not None:
                    tree.links.new(source.outputs['Alpha'], n.inputs['Alpha'])
                for destination in destinations:
                    tree.links.new(n.outputs['Image'], destination)
    scene.render.use_compositing = True
    if hasattr(scene, 'use_nodes'):
        scene.use_nodes = True
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                try:
                    area.spaces.active.shading.use_compositor = 'ALWAYS'
                except (AttributeError, TypeError):
                    pass
    scene['RBXNeonPipelineVersion'] = NEON_PIPELINE_VERSION
    return True
