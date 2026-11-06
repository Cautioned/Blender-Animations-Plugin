"""Imported authored weights must work even without MeshPart skinning flags."""
import unittest

import bpy
from mathutils import Vector

from ..rig.skin_binding import _ensure_armature_modifier, _apply_index_bound_weights


class TestAuthoredSkinBinding(unittest.TestCase):
    def setUp(self):
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True)
        self.rig = bpy.context.object
        for name, x in [('Head', 0), ('UnusedControl', 2)]:
            bone = self.rig.data.edit_bones.new(name)
            bone.head = (x, 0, 0)
            bone.tail = (x, 0, 1)
            bone.use_deform = False
        bpy.ops.object.mode_set(mode='OBJECT')
        mesh = bpy.data.meshes.new('AuthoredHead')
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        self.obj = bpy.data.objects.new('AuthoredHead', mesh)
        bpy.context.scene.collection.objects.link(self.obj)
        self.obj.vertex_groups.new(name='Head').add([0, 1, 2], 1, 'REPLACE')
        self.obj.vertex_groups.new(name='UnusedControl')

    def tearDown(self):
        mesh = self.obj.data
        armature = self.rig.data
        bpy.data.objects.remove(self.obj, do_unlink=True)
        bpy.data.objects.remove(self.rig, do_unlink=True)
        bpy.data.meshes.remove(mesh)
        bpy.data.armatures.remove(armature)

    def test_weighted_classic_head_bone_deforms_mesh(self):
        _ensure_armature_modifier(self.obj, self.rig)
        self.assertTrue(self.rig.data.bones['Head'].use_deform)
        self.assertFalse(self.rig.data.bones['UnusedControl'].use_deform)
        self.rig.pose.bones['Head'].location = (0.3, 0, 0)
        bpy.context.view_layer.update()
        evaluated = self.obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        try:
            for original, actual in zip(self.obj.data.vertices, mesh.vertices):
                self.assertLess((actual.co - original.co - Vector((0.3, 0, 0))).length, 1e-6)
        finally:
            evaluated.to_mesh_clear()

    def test_missing_source_bones_accumulate_on_fallback(self):
        self.obj.vertex_groups.clear()
        binding = {
            'entry': {'name': 'Head'},
            'mesh_data': {
                'bone_names': ['MissingA', 'MissingB', 'UnusedControl'],
                'vertex_weights': [
                    {'MissingA': 0.25, 'MissingB': 0.5, 'UnusedControl': 0.25}
                ] * 3,
            },
        }
        self.assertTrue(_apply_index_bound_weights(self.obj, self.rig, binding))
        for vertex in self.obj.data.vertices:
            weights = {self.obj.vertex_groups[g.group].name: g.weight for g in vertex.groups}
            self.assertAlmostEqual(weights['Head'], 0.75)
            self.assertAlmostEqual(weights['UnusedControl'], 0.25)
            self.assertAlmostEqual(sum(weights.values()), 1)
