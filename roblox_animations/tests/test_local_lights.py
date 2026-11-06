"""Measured local-light geometry and grouped EEVEE envelope regressions."""
import math
import unittest
from pathlib import Path
import bpy
from mathutils import Vector
from ..rig.local_lights import create_lights, ENVELOPE, FACE_NORMALS
from ..core.constants import get_transform_to_blender
from ..core.rbxm import parse_rbxm
I=[0,0,0,1,0,0,0,1,0,0,0,1]
class LocalLights(unittest.TestCase):
 def setUp(self):
  self.collection=bpy.data.collections.new('LocalLightTest');bpy.context.scene.collection.children.link(self.collection)
  self.part={'inst_ref':7,'part_cf':I,'part_size':[8,2,4]}
 def tearDown(self):
  groups={obj.light_linking.blocker_collection for obj in self.collection.objects if obj.type=='LIGHT' and obj.light_linking.blocker_collection}
  for obj in list(self.collection.objects):
   data=obj.data;kind=obj.type;bpy.data.objects.remove(obj,do_unlink=True)
   if data and data.users==0:
    (bpy.data.lights if kind=='LIGHT' else bpy.data.meshes).remove(data)
  for group in groups:bpy.data.collections.remove(group)
  bpy.data.collections.remove(self.collection)
 def make(self,kind='SpotLight',**props):
  source=dict(class_name=kind,name='Fixture',parent_ref=7,brightness=1,range=16,face=4,angle=60,enabled=True,shadows=True);source.update(props)
  self.assertEqual(create_lights(self.collection,[source],[self.part]),1)
  bpy.context.view_layer.update()
  return next(o for o in self.collection.objects if o.get('RBXLightSource'))
 def test_spot_starts_at_part_center_for_thick_hosts(self):
  self.assertLess(self.make().location.length,1e-6)
 def test_surface_retains_selected_face_and_projection_clearance(self):
  obj=self.make('SurfaceLight');expected=get_transform_to_blender()@Vector((0,-1,0))
  self.assertLess((Vector(obj['rbx_emitter_origin'])-expected).length,1e-5)
  direction=obj.rotation_quaternion@Vector((0,0,-1))
  self.assertLess((obj.location+direction*obj.data['rbx_surface_apex_offset']-expected).length,1e-5)
 def test_all_faces_apply_to_attachments(self):
  for face,normal in enumerate(FACE_NORMALS):
   root=self.make(attachment_cf=[2,3,4]+I[3:],face=face,brightness=0)
   expected=get_transform_to_blender().to_3x3()@Vector(normal)
   self.assertLess((root.rotation_quaternion@Vector((0,0,-1))-expected).length,1e-5)
   self.assertLess((root.location-get_transform_to_blender()@Vector((2,3,4))).length,1e-5)
   root['RBXLightSource']=''
 def test_attached_surface_is_spot_at_attachment(self):
  root=self.make('SurfaceLight',attachment_cf=[2,3,4]+I[3:]);self.assertEqual(root.data.type,'SPOT')
  self.assertLess((root.location-get_transform_to_blender()@Vector((2,3,4))).length,1e-5)
 def test_wide_spot_cones_are_preserved(self):
  root=self.make(angle=180);self.assertAlmostEqual(root.data.spot_size,math.pi,places=5)
 def test_range_120_and_finite_cutoff(self):
  root=self.make('PointLight',range=120)
  self.assertAlmostEqual(root.data.cutoff_distance,120)
  self.assertTrue(all(o.data.cutoff_distance<=120.001 and o.data.use_custom_distance for o in self.collection.objects))
 def test_helpers_are_coincident_parented_and_share_shadows(self):
  root=self.make('PointLight');self.assertEqual(len(self.collection.objects),len(ENVELOPE))
  for obj in self.collection.objects:
   self.assertLess((obj.matrix_world.translation-root.matrix_world.translation).length,1e-5)
   self.assertTrue(obj.data.use_shadow)
   if obj!=root:self.assertEqual(obj.parent,root);self.assertTrue(obj['RBXLightHelper'])
 def test_source_return_count_excludes_helpers(self):
  self.make('PointLight');self.assertEqual(sum(bool(o.get('RBXLightSource')) for o in self.collection.objects),1)
 def test_surface_extent_follows_host_face_not_range(self):
  root=self.make('SurfaceLight');self.assertEqual(list(root['rbx_surface_size']),[8,4])
  points={tuple(round(v,3) for v in o.matrix_world.translation) for o in self.collection.objects}
  self.assertGreater(len(points),1)
 def test_disabled_is_not_imported(self):
  self.assertEqual(create_lights(self.collection,[dict(class_name='PointLight',parent_ref=7,enabled=False)],[self.part]),0)
  self.assertEqual(len(self.collection.objects),0)
 def test_envelope_cancels_inverse_square_and_stops_at_range(self):
  def response(d):return sum(p*max(0,1-(d/c)**4)**2/d**2 for c,p in ENVELOPE)
  for d in (.1,.25,.5,.65):self.assertAlmostEqual(response(d),1,delta=.12)
  self.assertAlmostEqual(response(.9),.16,delta=.04)
  self.assertEqual(response(1),0);self.assertEqual(response(2),0)
 def test_native_roundtrip_retains_classes_face_and_zero_values(self):
  folder=Path(__file__).parent/'fixtures'/'light_roundtrip'
  for name,kind in (('01_point_default','PointLight'),('66_spotlight_attachment_front','SpotLight'),('71_surfacelight_attachment_front','SurfaceLight')):
   light=parse_rbxm((folder/(name+'.rbxm')).read_bytes())['scene_lights'][0]
   self.assertEqual(light['class_name'],kind)
   if 'attachment' in name:self.assertIsNotNone(light['attachment_cf']);self.assertEqual(light['face'],5)
  light=parse_rbxm((folder/'55_spotlight_range_zero.rbxm').read_bytes())['scene_lights'][0]
  self.assertEqual(light['range'],0)

 def test_host_self_shadow_is_excluded_only_for_its_own_light(self):
  host=bpy.data.objects.new('OpaqueHost',bpy.data.meshes.new('HostMesh'))
  host['RBXInstRef']=7;self.collection.objects.link(host)
  root=self.make('PointLight')
  blockers=root.light_linking.blocker_collection
  self.assertIsNotNone(blockers)
  self.assertEqual(list(blockers.objects),[host])
  self.assertEqual(blockers.collection_objects[0].light_linking.link_state,'EXCLUDE')
  self.assertTrue(host.visible_shadow)
  for obj in self.collection.objects:
   if obj.type=='LIGHT':self.assertEqual(obj.light_linking.blocker_collection,blockers)

 def test_lazy_light_housings_keep_identity_without_becoming_rig_parts(self):
  from ..operators import import_ops
  host=bpy.data.objects.new('LazyHost',bpy.data.meshes.new('LazyHostMesh'))
  self.collection.objects.link(host);host['RBXInstRef']=7;host['RBXPartIdx']=3
  import_ops._remove_lazy_map_identity(host,{'_rbx_local_light_host':True})
  self.assertEqual(host['RBXInstRef'],7);self.assertNotIn('RBXPartIdx',host)
  import_ops._remove_lazy_map_identity(host,{})
  self.assertNotIn('RBXInstRef',host)
