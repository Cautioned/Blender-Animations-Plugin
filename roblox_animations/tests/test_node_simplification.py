"""Numerical and sharing regressions for simplified surface shader graphs."""
import copy,math,unittest
from unittest.mock import patch
import bpy
from ..rig import textures
from ..operators import import_ops


def scalar(socket):
    if not socket.links:return float(socket.default_value)
    output=socket.links[0].from_socket;node=output.node
    if node.type=='TEX_IMAGE':return float(node.image.pixels[3])
    values=[scalar(s) for s in node.inputs]
    return {'MULTIPLY':lambda:values[0]*values[1],
            'SUBTRACT':lambda:values[0]-values[1],
            'ADD':lambda:values[0]+values[1],
            'MULTIPLY_ADD':lambda:values[0]*values[1]+values[2],
            'DIVIDE':lambda:values[0]/values[1] if values[1] else 0.}[node.operation]()


def vector(socket,point):
    if not socket.links:return tuple(socket.default_value)
    node=socket.links[0].from_node
    if node.type in ('UVMAP','ATTRIBUTE'):return point
    if node.type=='VECTOR_ROTATE':
        p=vector(node.inputs['Vector'],point);center=tuple(node.inputs['Center'].default_value)
        angle=node.inputs['Angle'].default_value;c,s=math.cos(angle),math.sin(angle)
        x,y=p[0]-center[0],p[1]-center[1]
        return (x*c-y*s+center[0],x*s+y*c+center[1],p[2])
    a,b,c=[vector(i,point) for i in node.inputs[:3]]
    if node.operation=='MULTIPLY_ADD':return tuple(a[k]*b[k]+c[k] for k in range(3))
    raise AssertionError(node.operation)


class NodeSimplificationTests(unittest.TestCase):
    def setUp(self):
        self.materials=[]
        self.image=bpy.data.images.new('SimplificationTexture',width=1,height=1,alpha=True)
        self.image.pixels[:]=(.6,.3,.8,.5)
        self.entry=dict(name='Panel',class_name='MeshPart',mesh_id='mesh',part_size=[6,5,4],color=[.3,.5,.7],material=272)
    def tearDown(self):
        for material in self.materials:
            if material.name in bpy.data.materials:bpy.data.materials.remove(material)
    def build(self,entry):
        with patch.object(textures,'fetch_texture_image',return_value=self.image):mat=textures.build_part_material('Simplification',entry)
        self.materials.append(mat);return mat
    def uv_material(self):
        mat=bpy.data.materials.new('UVFormulaTest');mat.use_nodes=True;self.materials.append(mat);return mat
    def test_identical_layers_share_complete_uv_transform(self):
        entry=dict(self.entry,decals=[dict(texture=str(i),face=5,transparency=.25) for i in range(3)])
        mat=self.build(entry);nodes=mat.node_tree.nodes
        maps=[n for n in nodes if n.type=='TEX_IMAGE']
        self.assertEqual(len(maps),3)
        self.assertEqual(len({n.inputs['Vector'].links[0].from_socket.as_pointer() for n in maps}),1)
        self.assertEqual(sum(n.type=='UVMAP' for n in nodes),1)
        self.assertEqual(sum(n.type=='VECT_MATH' for n in nodes),1)
        self.assertEqual(sum(n.type!='FRAME' for n in nodes),14)
    def test_different_transforms_share_lookup_but_not_result(self):
        mat=self.uv_material();a=mat.node_tree.nodes.new('ShaderNodeTexImage');b=mat.node_tree.nodes.new('ShaderNodeTexImage')
        layer=dict(face=5,is_decal=True)
        textures._bind_surface_image_uv(mat,a,layer,self.entry,0)
        textures._bind_surface_image_uv(mat,b,dict(layer,uv_offset=[.25,0]),self.entry,1)
        self.assertNotEqual(a.inputs['Vector'].links[0].from_socket,b.inputs['Vector'].links[0].from_socket)
        self.assertEqual(sum(n.type=='UVMAP' for n in mat.node_tree.nodes),1)
    def test_different_faces_never_share_projection(self):
        mat=self.uv_material();a=mat.node_tree.nodes.new('ShaderNodeTexImage');b=mat.node_tree.nodes.new('ShaderNodeTexImage')
        textures._bind_surface_image_uv(mat,a,dict(face=5,is_decal=True),self.entry,0)
        textures._bind_surface_image_uv(mat,b,dict(face=2,is_decal=True),self.entry,1)
        self.assertNotEqual(a.inputs['Vector'].links[0].from_socket,b.inputs['Vector'].links[0].from_socket)
    def test_uv_formula_matches_authored_operation_order(self):
        for native in (False,True):
            entry=dict(self.entry,class_name='Part' if native else 'MeshPart')
            for angle in (0,45,-90):
                for scale,offset in (((1,1),(0,0)),((.5,2),(.2,-.3)),((-1,.75),(-.2,.4))):
                    mat=self.uv_material();tex=mat.node_tree.nodes.new('ShaderNodeTexImage')
                    layer=dict(face=5,is_decal=True,rotation=angle,uv_scale=scale,uv_offset=offset,offset_studs_u=.4,offset_studs_v=-.7)
                    textures._bind_surface_image_uv(mat,tex,layer,entry)
                    for p in ((0,0,0),(.3,.7,0),(3,2.5,0),(6,5,0)):
                        x,y=p[:2] if native else (p[0]/6+.4/6,1-p[1]/5+.7/5)
                        c,s=math.cos(math.radians(angle)),math.sin(math.radians(angle))
                        x,y=(x-.5)*c-(y-.5)*s+.5,(x-.5)*s+(y-.5)*c+.5
                        expected=(x*scale[0]+offset[0],y*scale[1]+1-scale[1]-offset[1])
                        actual=vector(tex.inputs['Vector'],p)
                        for a,b in zip(actual,expected):self.assertAlmostEqual(a,b,places=5)
    def test_transparent_coverage_and_factors_match_porter_duff(self):
        entry=dict(self.entry,transparency=.7,decals=[dict(texture=str(i),face=5,transparency=t) for i,t in enumerate((.2,.5,.7))])
        mat=self.build(entry);nodes=mat.node_tree.nodes;expected=.3
        for i,fade in enumerate((.8,.5,.3)):
            tag='' if i==0 else '.'+str(i)
            overlay=float(nodes['RBX TextureInstance'+tag].image.pixels[3])*fade
            expected=overlay+expected*(1-overlay)
            mix=nodes['RBX Overlay Mix'+tag]
            self.assertAlmostEqual(scalar(textures._float_factor_socket(mix)),overlay/expected,places=6)
            self.assertAlmostEqual(scalar(nodes['RBX Surface Straight Factor'+tag].inputs[1]),expected,places=6)
        self.assertAlmostEqual(scalar(textures._principled_node(mat).inputs['Alpha']),expected,places=6)
        self.assertEqual(sum(n.type!='FRAME' for n in nodes),22)
    def test_missing_middle_layer_preserves_surrounding_alpha(self):
        entry=dict(self.entry,transparency=.7,decals=[dict(texture=str(i),face=5,transparency=.2) for i in range(3)])
        mat=self.build(entry);nodes=mat.node_tree.nodes;nodes['RBX TextureInstance.1'].image=None
        textures._apply_missing_color_map_fallback(mat,entry)
        overlay=float(nodes['RBX TextureInstance'].image.pixels[3])*.8
        expected=overlay+.3*(1-overlay);expected=overlay+expected*(1-overlay)
        self.assertAlmostEqual(scalar(textures._principled_node(mat).inputs['Alpha']),expected,places=6)
        self.assertEqual(scalar(textures._float_factor_socket(nodes['RBX Overlay Mix.1'])),0.)
    def test_eevee_world_has_only_connected_nodes(self):
        scene=bpy.context.scene;scene.render.engine='BLENDER_EEVEE'
        collection=bpy.data.collections.new('SimplifiedWorldTest');scene.collection.children.link(collection)
        try:
            import_ops._create_rbxl_global_lighting(collection,dict(clock_time=12),dict(rbx_environment_image=self.image.name))
            nodes=scene.world.node_tree.nodes
            self.assertEqual(len(nodes),3)
            self.assertTrue(all(n.type=='OUTPUT_WORLD' or any(s.links for s in n.outputs) for n in nodes))
        finally:
            for obj in list(collection.objects):bpy.data.objects.remove(obj,do_unlink=True)
            bpy.data.collections.remove(collection)
