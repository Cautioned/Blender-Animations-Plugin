"""Regression cases from actual Studio 0.746 native RBXM exports.

The fixtures use diagnostic textures uploaded to test creator 7255061823.
Images are mocked here; visual tests live in tools/material_parity.
"""
from pathlib import Path
import struct, unittest
from unittest import mock
import bpy
from ..core.rbxm import parse_rbxm
from ..rig import textures,primitive_shapes
from .test_content_serialization import _model,_content
from .test_materials import _fake_image

FIXTURES=Path(__file__).parent/'fixtures/material_roundtrip'
def entries(case):return {e['name']:e for e in parse_rbxm((FIXTURES/(case+'.rbxm')).read_bytes())['partAux']}

class NativeMaterialMetadataTests(unittest.TestCase):
    def test_native_texture_keeps_both_densities_and_negative_offsets(self):
        e=entries('04_texture_uv')
        layer=e['tile1_u1_v2_off0_0']['texture_instance']
        self.assertEqual((layer['studs_per_tile_u'],layer['studs_per_tile_v']),(1,2))
        layer=e['tile12_u2_v2_off-0.5_-1']['texture_instance']
        self.assertEqual((layer['offset_studs_u'],layer['offset_studs_v']),(-.5,-1))
        self.assertFalse(e['tile1_u1_v2_off0_0']['cast_shadow'])

    def test_native_vector2_transforms_and_layer_order_survive(self):
        e=entries('05_decal_uv')
        layer=e['decal_uv1']['decals'][0]
        self.assertEqual(layer['rotation'],45)
        self.assertEqual(layer['uv_scale'],[.5,.5])
        self.assertAlmostEqual(layer['uv_offset'][0],.2)
        self.assertEqual([x['z_index'] for x in e['decal_uv18']['decals']],[-2,10])

    def test_new_decal_color_content_overrides_legacy_texture(self):
        for cls,key in [('Decal','decals'),('Texture','texture_instances')]:
            parsed=parse_rbxm(_model([('Part',-1,{}),(cls,0,{
                'ColorMapContent':(0x22,_content(['rbxassetid://123'])),
                'Texture':(1,struct.pack('<I',len(b'rbxassetid://999'))+b'rbxassetid://999'),
            })]))
            self.assertEqual(parsed['partAux'][0][key][0]['texture'],'rbxassetid://123')

    def test_uint8_decal_tint_one_is_not_full_intensity(self):
        parsed=parse_rbxm(_model([('Part',-1,{}),('Decal',0,{
            'TextureContent':(0x22,_content(['rbxassetid://123'])),
            'Color3uint8':(0x1a,bytes([1,128,255])),
        })]))
        self.assertEqual(parsed['partAux'][0]['decals'][0]['color'],[1/255,128/255,1])

    def test_inactive_surface_does_not_supply_maps(self):
        e=entries('10_surface_controls')['control12']
        self.assertEqual(len(e['surface_appearances']),2)
        active=textures._entry_surface_appearances(e)
        self.assertEqual(len(active),1)
        self.assertEqual(active[0]['normal_map'],'')
        self.assertEqual(active[0]['alpha_mode'],0)
        self.assertNotIn('rbxassetid://79662202130119',textures.material_texture_refs(e))

class RoundtripShaderTests(unittest.TestCase):
    def setUp(self):
        self.image=_fake_image();self.materials=[];self.objects=[]
        self.fetch=mock.patch.object(textures,'fetch_texture_image',return_value=self.image);self.fetch.start()
    def tearDown(self):
        self.fetch.stop()
        for obj in self.objects:
            mesh=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
        for mat in self.materials:
            if mat.name in bpy.data.materials:bpy.data.materials.remove(mat,do_unlink=True)
        if self.image.name in bpy.data.images:bpy.data.images.remove(self.image,do_unlink=True)
        textures._PART_MATERIAL_CACHE.clear();textures._IMAGE_COLOR_USES.clear();textures._IMAGE_DATA_USES.clear()
    def build(self,e):
        m=textures.build_part_material('Roundtrip',e);self.materials.append(m);return m
    def test_shared_scalar_asset_uses_red_for_both_properties(self):
        e=entries('06_sa_pbr')['pbr3'];m=self.build(e)
        p=textures._principled_node(m)
        rough=p.inputs['Roughness'].links[0].from_socket;metal=p.inputs['Metallic'].links[0].from_socket
        self.assertEqual(rough,metal);self.assertEqual(rough.name,'Red')
        self.assertEqual(rough.node.inputs['Color'].links[0].from_node.image.colorspace_settings.name,'Non-Color')
    def test_classic_mesh_texture_ignores_host_color(self):
        e=entries('10_surface_controls')['control1'];m=self.build(e)
        self.assertIsNone(m.node_tree.nodes.get('RBX TextureID Tint'))
        p=textures._principled_node(m)
        self.assertEqual(p.inputs['Base Color'].links[0].from_node.name,'RBX TextureID')
    def test_empty_surface_albedo_uses_surface_color(self):
        e=entries('10_surface_controls')['control8'];m=self.build(e);p=textures._principled_node(m)
        self.assertFalse(p.inputs['Base Color'].links)
        self.assertIsNone(m.node_tree.nodes.get('RBX Material ColorAttr'))
        for a,b in zip(p.inputs['Base Color'].default_value[:3],(.03310477,.38005634,.78741229)):
            self.assertAlmostEqual(a,b,places=4)
    def test_filtering_and_cache_identity_follow_surface(self):
        e=entries('10_surface_controls')['control19'];m=self.build(e)
        self.assertEqual(m.node_tree.nodes['RBX ColorMap'].interpolation,'Closest')
        changed=dict(e);changed['surface_appearances']=[dict(e['surface_appearances'][-1],resample_mode=0)]
        self.assertNotEqual(textures.part_material_identity(e),textures.part_material_identity(changed))
    def test_face_extent_splits_surface_image_material_cache(self):
        e=entries('05_decal_uv')['decal_uv0'];changed=dict(e,part_size=[6,3.2,.4])
        self.assertNotEqual(textures.part_material_identity(e),textures.part_material_identity(changed))
    def test_decal_survives_invisible_host_without_floating_planes(self):
        e=entries('05_decal_uv')['decal_uv23'];m=self.build(e);p=textures._principled_node(m)
        self.assertEqual(p.inputs['Alpha'].links[0].from_node.name,'RBX Surface Coverage.1')
        self.assertEqual(len(textures._entry_texture_instances(e)),2)
    def test_surface_uvs_remain_in_part_space_after_rotation(self):
        original=entries('05_decal_uv')['decal_uv0']
        a=dict(original);b=dict(original)
        a['part_cf']=[0,0,0,1,0,0,0,1,0,0,0,1]
        b['part_cf']=[5,4,3,0,0,1,0,1,0,-1,0,0]
        col=bpy.data.collections.new('Roundtrip UV');bpy.context.scene.collection.children.link(col)
        for e in (a,b):
            obj=primitive_shapes._create_batched_static_primitives(col,'UVFixture',[e]);self.objects.append(obj)
        ua=self.objects[0].data.uv_layers['RBXSurfaceUV.2'];ub=self.objects[1].data.uv_layers['RBXSurfaceUV.2']
        self.assertEqual(len(ua.data),len(ub.data))
        for a,b in zip(ua.data,ub.data):
            for x,y in zip(a.uv,b.uv):self.assertAlmostEqual(x,y,places=5)
        # Source top-left (-X,+Y,+Z) must have top-origin surface UV (0,0).
        # This catches a vertical flip that rotation-invariance alone misses.
        from ..core.constants import get_transform_to_blender
        from mathutils import Vector
        inverse=get_transform_to_blender().inverted_safe()
        obj=self.objects[0];found=False
        for loop,uv in zip(obj.data.loops,ua.data):
            point=inverse @ obj.data.vertices[loop.vertex_index].co
            if abs(point.x+1.6)<1e-5 and abs(point.y-1.6)<1e-5 and point.z>0:
                self.assertAlmostEqual(uv.uv.x,0,places=5)
                self.assertAlmostEqual(uv.uv.y,0,places=5)
                found=True
        self.assertTrue(found)
        self.assertFalse(self.objects[0].visible_shadow)
        for obj in self.objects:
            for m in obj.data.materials:
                if m not in self.materials:self.materials.append(m)
        bpy.data.collections.remove(col)
