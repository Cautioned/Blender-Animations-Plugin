"""Managed post-processing after Neon and before existing user grading.

Color correction operates in display sRGB, with source exposure applied
once. Explicit BloomEffects also affect non-Neon highlights; Neon keeps its
separately measured core/halo response. This is an EEVEE approximation.
"""
import json
import bpy
from ..core.post_processing import active_corrections, correction_matrix
from ..core.neon import NEON_AOV
from .neon import _root_tree, ensure_neon_aov

_TAG = 'RBXPostManaged'
_VERSION = 2


def _make_group(scene, effects):
    modern = bpy.app.version >= (5, 0, 0)
    group = bpy.data.node_groups.new('RBX Post Processing', 'CompositorNodeTree')
    for name, direction in (('Image', 'INPUT'), ('Scene', 'INPUT'), ('Neon', 'INPUT'), ('Image', 'OUTPUT')):
        group.interface.new_socket(name=name, in_out=direction, socket_type='NodeSocketColor')
    nodes, links = group.nodes, group.links
    inp = nodes.new('NodeGroupInput'); out = nodes.new('NodeGroupOutput')
    def node(kind, name):
        n = nodes.new(kind); n.name = name
        n.location = (200 * (len(nodes) % 6), -160 * (len(nodes) // 6))
        return n
    def mix(op, a, b, name):
        n = node('ShaderNodeMixRGB' if modern else 'CompositorNodeMixRGB', name)
        n.blend_type = op; n.inputs[0].default_value = 1
        links.new(a, n.inputs[1]); links.new(b, n.inputs[2])
        return n.outputs[0]
    image = inp.outputs['Image']
    blooms = effects.get('blooms')
    if blooms is None:
        blooms = [effects['bloom']] if effects.get('bloom') else []
    # Explicit bloom is a scene effect, not exclusive to the Neon material.
    residual = None
    for index, effect in enumerate(blooms):
        if not effect.get('enabled', True) or float(effect.get('intensity', 1)) <= 0:
            continue
        if residual is None:
            residual = mix('SUBTRACT', inp.outputs['Scene'], inp.outputs['Neon'], 'RBX Non-Neon Highlights')
        intensity = max(0., float(effect.get('intensity', 1)))
        size = max(0., float(effect.get('size', 24)))
        def mathnode(operation, name, a, b):
            n = node('ShaderNodeMath' if modern else 'CompositorNodeMath', name)
            n.operation = operation
            for socket, value in zip(n.inputs, (a, b)):
                if isinstance(value, (float, int)): socket.default_value = value
                else: links.new(value, socket)
            return n.outputs[0]
        split = node('CompositorNodeSeparateColor', 'RBX Scene Bloom Channels ' + str(index)); split.mode = 'RGB'
        links.new(residual, split.inputs[0])
        peak = mathnode('MAXIMUM', 'RBX Scene Bloom RG ' + str(index), split.outputs[0], split.outputs[1])
        peak = mathnode('MAXIMUM', 'RBX Scene Bloom Peak ' + str(index), peak, split.outputs[2])
        threshold = max(0., float(effect.get('threshold', .95)))
        threshold_node = node('ShaderNodeValue' if modern else 'CompositorNodeValue', 'RBX Scene Bloom Threshold ' + str(index))
        driver = threshold_node.outputs[0].driver_add('default_value').driver
        var = driver.variables.new(); var.name = 'exposure'; var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'SCENE'; var.targets[0].id = scene
        var.targets[0].data_path = 'view_settings.exposure'
        driver.expression = repr(threshold) + ' * pow(2, -exposure)'
        excess = mathnode('SUBTRACT', 'RBX Scene Bloom Excess ' + str(index), peak, threshold_node.outputs[0])
        positive = mathnode('MAXIMUM', 'RBX Scene Bloom Positive ' + str(index), excess, 0)
        fraction = mathnode('DIVIDE', 'RBX Scene Bloom Fraction ' + str(index), positive, peak)
        highlights = mix('MULTIPLY', residual, fraction, 'RBX Scene Bloom Highlights ' + str(index))
        blur = node('CompositorNodeBlur', 'RBX Scene Bloom ' + str(index))
        radius = 8 + 1.1 * size
        if modern:
            blur.inputs['Type'].default_value = 'Gaussian'
            info = node('CompositorNodeImageInfo', 'RBX Scene Bloom Dimensions ' + str(index))
            links.new(residual, info.inputs['Image'])
            axes = node('ShaderNodeSeparateXYZ', 'RBX Scene Bloom Axes ' + str(index))
            links.new(info.outputs['Dimensions'], axes.inputs[0])
            pixels = mathnode('MULTIPLY', 'RBX Scene Bloom Pixel Radius ' + str(index), axes.outputs['Y'], radius/653)
            vector = node('ShaderNodeCombineXYZ', 'RBX Scene Bloom Radius ' + str(index))
            links.new(pixels, vector.inputs['X']); links.new(pixels, vector.inputs['Y'])
            links.new(vector.outputs[0], blur.inputs['Size'])
        else:
            blur.filter_type = 'GAUSS'
            blur.size_x = blur.size_y = max(1, round(radius * scene.render.resolution_y / 653 * scene.render.resolution_percentage / 100))
        links.new(highlights, blur.inputs['Image'])
        strength = node('ShaderNodeValue' if modern else 'CompositorNodeValue', 'RBX Scene Bloom Intensity ' + str(index))
        strength.outputs[0].default_value = intensity
        halo = mix('MULTIPLY', blur.outputs['Image'], strength.outputs[0], 'RBX Scale Scene Bloom ' + str(index))
        image = mix('ADD', image, halo, 'RBX Add Scene Bloom ' + str(index))
    if active_corrections(effects):
        # Scene exposure happens after compositing in Blender. Temporarily
        # apply it here, then compensate, to grade the visible color once.
        def exposure(source, sign, name):
            n = node('CompositorNodeExposure', name)
            links.new(source, n.inputs['Image'])
            driver = n.inputs['Exposure'].driver_add('default_value').driver
            var = driver.variables.new(); var.name = 'exposure'; var.type = 'SINGLE_PROP'
            var.targets[0].id_type = 'SCENE'; var.targets[0].id = scene
            var.targets[0].data_path = 'view_settings.exposure'
            driver.expression = 'exposure' if sign > 0 else '-exposure'
            return n.outputs[0]
        image = exposure(image, 1, 'RBX Apply Source Exposure')
        encode = node('CompositorNodeConvertColorSpace', 'RBX Display Color')
        encode.from_color_space = 'scene_linear'; encode.to_color_space = 'sRGB'
        links.new(image, encode.inputs[0])
        split = node('CompositorNodeSeparateColor', 'RBX Display Channels'); split.mode = 'RGB'
        links.new(encode.outputs[0], split.inputs[0])
        combine = node('CompositorNodeCombineColor', 'RBX Corrected Display'); combine.mode = 'RGB'
        matrix = correction_matrix(effects)
        for i, row in enumerate(matrix):
            value = None
            for j, coefficient in enumerate(row[:3]):
                if coefficient == 0: continue
                n = node('ShaderNodeMath' if modern else 'CompositorNodeMath', f'RBX Grade {i} {j}')
                n.operation = 'MULTIPLY_ADD'
                links.new(split.outputs[j], n.inputs[0]); n.inputs[1].default_value = coefficient
                if value is None: n.inputs[2].default_value = row[3]
                else: links.new(value, n.inputs[2])
                value = n.outputs[0]
            clamp = node('ShaderNodeClamp' if modern else 'CompositorNodeMapRange', 'RBX Display Bounds ' + str(i))
            if modern:
                if value is not None: links.new(value, clamp.inputs['Value'])
                else: clamp.inputs['Value'].default_value = row[3]
                clamp.inputs['Min'].default_value = 0; clamp.inputs['Max'].default_value = 1
            else:
                clamp.use_clamp = True
                if value is not None: links.new(value, clamp.inputs[0])
                else: clamp.inputs[0].default_value = row[3]
            links.new(clamp.outputs[0], combine.inputs[i])
        links.new(split.outputs[3], combine.inputs[3])
        decode = node('CompositorNodeConvertColorSpace', 'RBX Scene Linear Color')
        decode.from_color_space = 'sRGB'; decode.to_color_space = 'scene_linear'
        links.new(combine.outputs[0], decode.inputs[0])
        image = exposure(decode.outputs[0], -1, 'RBX Compensate Display Exposure')
    links.new(image, out.inputs[0])
    group[_TAG] = True
    return group


def configure_post_processing(scene, effects):
    """Reuse owned stages, preserving user consumers and all other passes."""
    enabled = bool(active_corrections(effects) or any(
        e.get('enabled', True) and float(e.get('intensity', 1)) > 0
        for e in effects.get('blooms', [effects['bloom']] if effects.get('bloom') else [])))
    existing = scene.compositing_node_group if hasattr(scene, 'compositing_node_group') else scene.node_tree
    if not enabled and (existing is None or not any(n.get(_TAG) for n in existing.nodes)):
        return False
    if enabled and (effects.get('blooms') or effects.get('bloom')):
        ensure_neon_aov(scene)
    tree, modern = _root_tree(scene)
    managed = [n for n in tree.nodes if n.get(_TAG)]
    if not enabled:
        for n in managed:
            source = n.inputs['Image'].links[0].from_socket if n.inputs['Image'].links else None
            if source:
                for link in list(n.outputs['Image'].links): tree.links.new(source, link.to_socket)
            group = n.node_tree; tree.nodes.remove(n)
            if group.users == 0: bpy.data.node_groups.remove(group)
        return False
    sources = [n for n in tree.nodes if (n.get('RBXNeonManaged') or
               n.type == 'R_LAYERS' and (n.scene is None or n.scene == scene))
               and n.outputs['Image'].links]
    # Render inputs already passing through Neon must not get a second stage.
    sources = [n for n in sources if n.get('RBXNeonManaged') or not any(
        link.to_node.get('RBXNeonManaged') for link in n.outputs['Image'].links)]
    if not managed and not sources: return False
    signature = json.dumps([_VERSION, effects, scene.name_full], sort_keys=True)
    if managed and all(n.get('RBXPostSignature') == signature for n in managed): return True
    group = _make_group(scene, effects)
    if managed:
        for n in managed:
            old = n.node_tree; n.node_tree = group; n['RBXPostSignature'] = signature
            if old.users == 0: bpy.data.node_groups.remove(old)
    else:
        for source in sources:
            destinations = [link.to_socket for link in list(source.outputs['Image'].links)]
            n = tree.nodes.new('CompositorNodeGroup'); n.node_tree = group
            n.name = 'RBX Post Processing'; n[_TAG] = True; n['RBXPostSignature'] = signature
            tree.links.new(source.outputs['Image'], n.inputs['Image'])
            layers = source.inputs['Image'].links[0].from_node if source.get('RBXNeonManaged') else source
            tree.links.new(layers.outputs['Image'], n.inputs['Scene'])
            aov = layers.outputs.get(NEON_AOV)
            if aov: tree.links.new(aov, n.inputs['Neon'])
            for destination in destinations: tree.links.new(n.outputs['Image'], destination)
    scene.render.use_compositing = True
    if hasattr(scene, 'use_nodes'): scene.use_nodes = True
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                try: area.spaces.active.shading.use_compositor = 'ALWAYS'
                except (AttributeError, TypeError): pass
    scene['RBXPostPipelineVersion'] = _VERSION
    return True
