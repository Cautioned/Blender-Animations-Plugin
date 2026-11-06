"""Native sphere chart scale, import-path parity, and alpha-mask blend domain."""
import json,unittest
from pathlib import Path
from unittest import mock
import bpy,numpy as np
from ..rig import primitive_shapes as shapes,textures,creation
from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat


class SphereMaterialTests(unittest.TestCase):
    def test_all_native_chart_derivatives_match_nonuniform_studio_export(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/decal_paths_studio.json').read_text())
        case=next(c for c in fixture['cases'] if c['entry'].get('shape')=='ball')
        base=case['base'];faces=np.array([[c[0] for c in f] for f in base['faces']]);uv_ids=np.array([[c[1] for c in f] for f in base['faces']])
        p=np.array(base['positions']);size=np.array(case['entry']['part_size']);q=p/(size/2)
        actual=np.array(shapes._sphere_material_uvs(p,faces,size,.125)).reshape(-1,3,2)
        expected=np.array(base['uvs'])[uv_ids]
        center=q[faces].mean(1);axes=abs(center).argmax(1);sign=np.sign(center[np.arange(len(faces)),axes])
        for axis in range(3):
            for s in [-1,1]:
                with self.subTest(axis=axis,side=s):
                    difference=actual[(axes==axis)&(sign==s)]-expected[(axes==axis)&(sign==s)]
                    # Native atlas offsets are arbitrary; derivatives fix scale,
                    # orientation and corner distortion independently of phase.
                    offset=np.median(difference.reshape(-1,2),axis=0)
                    self.assertLess(float(abs(difference-offset).max()),.00015)

    def test_cached_uncached_and_individual_object_uvs_match(self):
        collection=bpy.data.collections.new('Sphere UV regression');bpy.context.scene.collection.children.link(collection)
        dummy=bpy.data.materials.new('Sphere material placeholder')
        entry={'name':'UV sphere','class_name':'Part','shape':'ball','part_size':[6,5,4],
               'part_cf':[12,7,-4,0,0,1,0,1,0,-1,0,0], 'color':[.6,.4,.2],'material':848}
        data=shapes._build_primitive_mesh_data(entry)
        try:
            with mock.patch.object(textures,'apply_part_material',return_value=True):
                obj=creation._create_mesh_object_from_filemesh(collection,'individual',data,entry)
            reference=np.array(shapes._sphere_material_uvs(data['positions'],data['faces'],entry['part_size'],.1))
            individual=np.array([u.uv[:] for u in obj.data.uv_layers['RBXMaterialUV'].data])
            self.assertTrue(np.allclose(individual,reference,atol=1e-6))
            batch=shapes._create_batched_static_primitives(collection,'cached',[entry],material=dummy)
            self.assertTrue(np.allclose([u.uv[:] for u in batch.data.uv_layers['RBXMaterialUV'].data],reference,atol=1e-6))
            with mock.patch.object(shapes,'_create_batched_static_primitives_np',side_effect=RuntimeError('exercise Python fallback')):
                fallback=shapes._create_batched_static_primitives(collection,'Python fallback',[entry],material=dummy)
            self.assertTrue(np.allclose([u.uv[:] for u in fallback.data.uv_layers['RBXMaterialUV'].data],reference,atol=1e-6))
            raw=shapes._static_primitive_part_arrays(np,entry,np.eye(3),np.zeros(3),True,.1,0)
            self.assertTrue(np.allclose(raw[2],reference,atol=1e-6))
            # The generic material helper sees transformed geometry, too.
            mesh=bpy.data.meshes.new('Generic transformed sphere')
            transform=get_transform_to_blender() @ cf_to_mat(entry['part_cf'])
            mesh.from_pydata([tuple(transform @ __import__('mathutils').Vector(p)) for p in data['positions']],[],data['faces']);mesh.update()
            uv=textures._ensure_builtin_material_uvs(mesh,entry)
            self.assertTrue(np.allclose([u.uv[:] for u in uv.data],reference,atol=1e-6))
            bpy.data.meshes.remove(mesh)
        finally:
            for obj in list(collection.objects):
                mesh=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
                if mesh.users==0:bpy.data.meshes.remove(mesh)
            bpy.data.collections.remove(collection);bpy.data.materials.remove(dummy)

    def test_mask_blend_has_native_display_domain_and_linear_endpoints(self):
        mat=bpy.data.materials.new('Mask blend domain');mat.use_nodes=True
        try:
            tex=mat.node_tree.nodes.new('ShaderNodeTexImage')
            out=textures._tint_mask_color(mat,tex,'Mask test',tint=[0,0,0])
            gamma=out.node
            self.assertAlmostEqual(gamma.inputs['Gamma'].default_value,2.2,places=6)
            mix=gamma.inputs['Color'].links[0].from_node
            self.assertEqual(mix.name,'Mask test')
            # The measured display-space fit maps 50% black tint to ~0.218 linear,
            # rather than the old linear blend's 0.5 (visibly too pale).
            self.assertAlmostEqual(.5**gamma.inputs['Gamma'].default_value,.217638,places=5)
            self.assertEqual(tex.outputs['Alpha'].links[0].to_node.as_pointer(),mix.as_pointer())
            self.assertFalse(textures._principled_node(mat).inputs['Alpha'].links)
        finally:bpy.data.materials.remove(mat)
