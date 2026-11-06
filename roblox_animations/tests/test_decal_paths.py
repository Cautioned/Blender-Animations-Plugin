"""All-face regression gold captured from native Studio OBJ/RBXM exports."""
import copy,json,unittest
from pathlib import Path
from unittest.mock import patch
import bpy,numpy as np
from mathutils import Vector
from ..core.constants import get_transform_to_blender
from ..rig import textures,primitive_shapes,filemesh,creation

FIXTURES=Path(__file__).parent/'fixtures'
NATIVE=json.loads((FIXTURES/'decal_paths_studio.json').read_text())['cases']

def build(entry):
    collection=bpy.context.scene.collection
    if entry.get('union_mesh'):
        return creation._create_mesh_object_from_filemesh(collection,entry['name'],dict(entry['union_mesh'],embedded_union=True),entry)
    if entry.get('mesh_id'):
        data=filemesh.fetch_and_parse_filemesh(entry['mesh_id'])
        return creation._create_batched_filemesh_instances(collection,entry['name'],data,[entry])
    return primitive_shapes._create_batched_static_primitives(collection,entry['name'],[entry])

class NativeDecalPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        filemesh._FILEMESH_BYTES_CACHE['http://www.roblox.com/asset/?id=1660648424']=(FIXTURES/'decal_meshpart.mesh').read_bytes()
        cls.image=bpy.data.images.new('NativeDecalPathsImage',width=4,height=4,alpha=True)
    def setUp(self):
        self.objects=[]
        self.fetch=patch.object(textures,'fetch_texture_image',return_value=self.image);self.fetch.start()
    def tearDown(self):
        self.fetch.stop()
        for obj in self.objects:
            mesh=obj.data;bpy.data.objects.remove(obj,do_unlink=True);bpy.data.meshes.remove(mesh)
    def create(self,entry):
        obj=build(entry);self.assertIsNotNone(obj);self.objects.append(obj);return obj
    def check_native(self,case,face):
        entry=copy.deepcopy(case['entry']);entry['decals'][0]['face']=face
        obj=self.create(entry);mesh=obj.data;mesh.update();inverse=get_transform_to_blender().inverted()
        points=np.asarray([tuple(inverse @ (obj.matrix_world @ v.co)) for v in mesh.vertices])
        base=np.asarray(case['base']['positions']);dist=((points[:,None,:]-base[None,:,:])**2).sum(axis=2)
        self.assertLess(float(np.sqrt(dist.min(axis=0)).max()),.001)
        self.assertLess(float(np.sqrt(dist.min(axis=1)).max()),.001)
        decal=case['decals'][face];dpoints=np.asarray(decal['positions']);duv=np.asarray(decal['uvs'])
        nearest=((dpoints[:,None,:]-points[None,:,:])**2).sum(axis=2).argmin(axis=1)
        canonical=((points[:,None,:]-points[None,:,:])**2).sum(axis=2).argmin(axis=1)
        expected={tuple(sorted(int(canonical[nearest[c[0]]]) for c in tri)) for tri in decal['faces']}
        expected={tri for tri in expected if len(set(tri))==3};actual=set()
        ua,va={0:(2,1),1:(0,2),2:(0,1),3:(2,1),4:(0,2),5:(0,1)}[face];size=entry['part_size']
        for poly in mesh.polygons:
            mat=mesh.materials[poly.material_index]
            if not any(n.name.startswith('RBX TextureInstance') for n in mat.node_tree.nodes):continue
            actual.add(tuple(sorted(int(canonical[i]) for i in poly.vertices)))
            for i in poly.loop_indices:
                vi=mesh.loops[i].vertex_index;distances=((dpoints-points[vi])**2).sum(axis=1)
                candidates=np.flatnonzero(distances<=distances.min()+1e-7)
                if entry.get('union_mesh'):uv=mesh.attributes[f'RBXDecalUV.{face}'].data[i].vector
                else:uv=mesh.uv_layers['UVMap' if textures._uses_native_decal_uv(entry) else f'RBXSurfaceUV.{face}'].data[i].uv
                sampled=uv if textures._uses_native_decal_uv(entry) else (uv[0]/size[ua],1-uv[1]/size[va])
                error=min(max(abs(sampled[k]-duv[n][k]) for k in (0,1)) for n in candidates)
                self.assertLess(error,1e-4,(entry['name'],face,i))
        self.assertEqual(actual,expected)
        if entry.get('shape') in ('ball','cylinder'):
            self.assertTrue(mesh.has_custom_normals)
            self.assertTrue(all(p.normal.dot(p.center)>0 for p in mesh.polygons))
    def test_union_six_decal_and_texture_faces_fit_uv_limit(self):
        entry=copy.deepcopy(next(c['entry'] for c in NATIVE if c['entry'].get('union_mesh')))
        entry['decals']=[dict(texture='union-decal',face=f) for f in range(6)]
        entry['texture_instances']=[dict(texture='union-tile',face=f,studs_per_tile=2) for f in range(6)]
        obj=self.create(entry)
        self.assertLessEqual(len(obj.data.uv_layers),8)
        self.assertTrue(all(obj.data.attributes.get(f'RBXDecalUV.{f}') for f in range(6)))
    def test_deferred_overlap_keeps_z_index_order_and_hydrates_slope(self):
        entry=copy.deepcopy(next(c['entry'] for c in NATIVE if c['entry'].get('shape')=='wedge'))
        entry['decals']=[dict(texture='front-topmost',face=5,z_index=9,transparency=.35),dict(texture='top-underneath',face=1,z_index=2)]
        with textures.defer_texture_image_loading(),patch.object(textures,'fetch_texture_image',return_value=None):obj=self.create(entry)
        slope=obj.data.materials[obj.data.polygons[-1].material_index]
        self.assertEqual([x['ref'] for x in textures._overlay_layer_registry(slope)],['top-underneath','front-topmost'])
        self.assertTrue(textures.refresh_cached_part_material(entry))
        self.assertTrue(all(n.image is not None for n in slope.node_tree.nodes if n.type=='TEX_IMAGE'))
    def test_deferred_limb_rebuild_preserves_each_face_selection(self):
        entry=dict(name='Head',shape='ball',class_name='Part',part_size=[6,5,4],color=[.3,.5,.7],material=272,
                   decals=[dict(texture='right-only',face=0),dict(texture='front-only',face=5)])
        with textures.defer_texture_image_loading(),patch.object(textures,'fetch_texture_image',return_value=None):obj=self.create(entry)
        self.assertTrue(textures.refresh_cached_part_material(entry))
        for mat in obj.data.materials:
            layers=textures._overlay_layer_registry(mat)
            self.assertLessEqual(len(layers),1)
    def test_uvs_follow_armature_deformation_on_host(self):
        case=next(c for c in NATIVE if c['entry']['class_name']=='MeshPart');entry=copy.deepcopy(case['entry']);obj=self.create(entry)
        bpy.context.view_layer.objects.active=obj;obj.select_set(True)
        arm=bpy.data.armatures.new('DecalPose');rig=bpy.data.objects.new('DecalPose',arm);bpy.context.scene.collection.objects.link(rig)
        bpy.context.view_layer.objects.active=rig;rig.select_set(True)
        try:
            bpy.ops.object.mode_set(mode='EDIT');bone=arm.edit_bones.new('Head');bone.head=(0,0,0);bone.tail=(0,0,1);bpy.ops.object.mode_set(mode='OBJECT')
            group=obj.vertex_groups.new(name='Head');group.add(list(range(len(obj.data.vertices))),1,'REPLACE')
            modifier=obj.modifiers.new('DecalPose','ARMATURE');modifier.object=rig
            rig.pose.bones['Head'].location=(1,2,3);bpy.context.view_layer.update()
            evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get());mesh=evaluated.to_mesh()
            try:
                self.assertGreater((mesh.vertices[0].co-obj.data.vertices[0].co).length,3)
                for source in obj.data.uv_layers:
                    for a,b in zip(source.data,mesh.uv_layers[source.name].data):self.assertLess((a.uv-b.uv).length,1e-6)
                self.assertEqual([p.material_index for p in mesh.polygons],[p.material_index for p in obj.data.polygons])
            finally:evaluated.to_mesh_clear()
        finally:
            if bpy.context.object and bpy.context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
            bpy.data.objects.remove(rig,do_unlink=True);bpy.data.armatures.remove(arm)

    def test_lazy_meshpart_instances_preserve_face_slots_and_layout_keys(self):
        import time
        from ..operators import import_ops
        entry=copy.deepcopy(next(c['entry'] for c in NATIVE if c['entry']['class_name']=='MeshPart'))
        data=filemesh.fetch_and_parse_filemesh(entry['mesh_id'])
        other=copy.deepcopy(entry);other['part_cf'][0]=10
        changed=copy.deepcopy(entry);changed['decals'][0]['face']=5
        self.assertEqual(import_ops._lazy_mesh_instance_key(entry),import_ops._lazy_mesh_instance_key(other))
        self.assertNotEqual(import_ops._lazy_mesh_instance_key(entry),import_ops._lazy_mesh_instance_key(changed))
        resized=copy.deepcopy(entry);resized['part_size']=[3,4,5]
        self.assertNotEqual(import_ops._lazy_mesh_instance_key(entry),import_ops._lazy_mesh_instance_key(resized))
        collection=bpy.data.collections.new('LazyDecalFixture');bpy.context.scene.collection.children.link(collection)
        job=dict(entry=entry,entries=[(entry,collection),(other,collection)],collection=collection,
                 mesh_data=data,material_cache={},material_state=None)
        try:
            complete,count=import_ops._apply_lazy_mesh_job(job,10,time.perf_counter()+10)
            self.assertTrue(complete);self.assertEqual(count,2)
            first,second=list(collection.objects)
            self.assertIs(first.data,second.data)
            for obj in (first,second):
                self.assertEqual(len(obj.material_slots),2)
                ids=textures._polygon_face_ids(obj.data,entry,first)
                for poly,faces in zip(obj.data.polygons,ids):
                    material=obj.material_slots[poly.material_index].material
                    has_decal=any(n.name.startswith('RBX TextureInstance') for n in material.node_tree.nodes)
                    self.assertEqual(has_decal,0 in faces)
        finally:
            for obj in list(collection.objects):bpy.data.objects.remove(obj,do_unlink=True)
            bpy.data.collections.remove(collection)

for case in NATIVE:
    for face in range(6):
        def native_case(self,case=case,face=face):self.check_native(case,face)
        setattr(NativeDecalPathTests,'test_native_'+case['entry']['name'].removeprefix('Audit').removesuffix('Face0')+'_'+str(face),native_case)
