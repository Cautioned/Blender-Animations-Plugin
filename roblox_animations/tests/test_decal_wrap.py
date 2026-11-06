"""Native Studio decal geometry/UV regressions; run inside Blender."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import bpy
from mathutils import Vector
from ..rig import textures, primitive_shapes
from ..core.constants import get_transform_to_blender

class DecalWrapTests(unittest.TestCase):
    def setUp(self):
        self.entry=dict(name='Sphere',shape='ball',part_size=[6,6,6],color=[.4,.5,.6],material=272,decals=[dict(face=5,texture='test-decal')])
        self.image=bpy.data.images.new('decal_diagnostic',width=4,height=4,alpha=True)
        self.mesh=bpy.data.meshes.new('decal_test')
        data=primitive_shapes._build_primitive_mesh_data(self.entry)
        basis=get_transform_to_blender()
        self.mesh.from_pydata([basis @ Vector(p) for p in data['positions']],[],data['faces']);self.mesh.update()
        self.obj=bpy.data.objects.new('decal_test',self.mesh)
    def tearDown(self):
        bpy.data.objects.remove(self.obj,do_unlink=True);bpy.data.meshes.remove(self.mesh)
    def test_native_sphere_topology_and_outward_winding(self):
        self.assertEqual(len(self.mesh.vertices),294);self.assertEqual(len(self.mesh.polygons),432)
        self.assertTrue(all(p.normal.dot(p.center)>0 for p in self.mesh.polygons))
        ids=textures._polygon_face_ids(self.mesh,self.entry)
        self.assertEqual([ids.count((face,)) for face in range(6)],[72]*6)
    def test_uvs_match_three_native_studio_decal_patches(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/decal_sphere_studio.json').read_text())
        inverse=get_transform_to_blender().inverted()
        for native in fixture['faces']:
            face=native['face'];textures._ensure_surface_image_uvs(self.obj,self.entry,[dict(face=face,is_decal=True)])
            uv=self.mesh.uv_layers[f'RBXSurfaceUV.{face}'];ids=textures._polygon_face_ids(self.mesh,self.entry)
            for p,faces in zip(self.mesh.polygons,ids):
                if face not in faces:continue
                for i in p.loop_indices:
                    point=inverse @ self.mesh.vertices[self.mesh.loops[i].vertex_index].co
                    n=min(range(len(native['points'])),key=lambda k:(point-Vector(native['points'][k])).length_squared)
                    # Studio's OBJ writes only six significant digits at Z=1000.
                    self.assertLess((point-Vector(native['points'][n])).length,.01)
                    expected=native['uv'][n];actual=uv.data[i].uv
                    self.assertAlmostEqual(actual.x/6,expected[0],places=4)
                    self.assertAlmostEqual(1-actual.y/6,expected[1],places=4)
    def test_mesh_decal_uses_native_uv_over_texture_id(self):
        entry=dict(self.entry,name='Head',mesh_id='rbxasset://fonts/head.mesh',texture_id='base-texture',face_decal={'texture':'test-decal'})
        with patch.object(textures,'fetch_texture_image',return_value=self.image):mat=textures.build_part_material('NativeHeadDecal',entry)
        nodes=mat.node_tree.nodes;layers=[n for n in nodes if n.type=='TEX_IMAGE' and n.name.startswith('RBX TextureInstance')]
        self.assertEqual(len(layers),1);self.assertEqual(layers[0].extension,'CLIP')
        uv=next(n for n in nodes if n.name.startswith('RBX Surface UV') and n.type=='UVMAP')
        self.assertEqual(uv.uv_map,'UVMap');self.assertFalse(any(n.name=='RBX ClothingBake' for n in nodes))
        self.assertEqual(len(textures._entry_texture_instances(entry)),1)
    def test_weighted_host_keeps_decal_without_extra_geometry(self):
        self.mesh.uv_layers.new(name='UVMap')
        group=self.obj.vertex_groups.new(name='Head');group.add(list(range(len(self.mesh.vertices))),1,'REPLACE')
        with patch.object(textures,'fetch_texture_image',return_value=self.image):textures.apply_part_material(self.obj,self.entry)
        self.assertEqual(len(self.obj.vertex_groups),1);self.assertEqual(group.weight(0),1)
        self.assertEqual(len(self.mesh.vertices),294)
        self.assertEqual(sum(1 for p in self.mesh.polygons if p.material_index!=0),72)

    def test_mesh_overlay_preserves_host_tint_attribute(self):
        entry=dict(self.entry,name='Head',mesh_id='rbxasset://fonts/head.mesh')
        self.mesh.uv_layers.new(name='UVMap')
        with patch.object(textures,'fetch_texture_image',return_value=self.image):
            textures.apply_part_material(self.obj,entry)
        self.assertIsNotNone(self.mesh.color_attributes.get('RBXColor'))

    def test_native_uv_material_does_not_share_primitive_projection(self):
        mesh=dict(self.entry,mesh_id='rbxasset://fonts/head.mesh')
        self.assertNotEqual(textures.part_material_identity(mesh),textures.part_material_identity(self.entry))

    def test_layers_keep_z_index_ties_and_face_metadata(self):
        entry=dict(self.entry,decals=[dict(texture='a',face=5,z_index=2),dict(texture='b',face=0,z_index=1),dict(texture='c',face=1,z_index=2)])
        layers=textures._entry_texture_instances(entry)
        self.assertEqual([x['texture'] for x in layers],['b','a','c'])
        self.assertEqual([x['face'] for x in layers],[0,5,1])
