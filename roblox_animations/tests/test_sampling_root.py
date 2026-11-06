"""Imported roots store their rest transforms in Roblox coordinates."""
import unittest

import bpy
from mathutils import Matrix

from ..animation.sampling import serialize_animation_state
from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat, mat_to_cf


class TestImportedRootSampling(unittest.TestCase):
    def test_scene_placement_is_not_exported_as_root_motion(self):
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.add(type='ARMATURE', enter_editmode=True)
        rig = bpy.context.object
        data = rig.data
        rest = Matrix.Translation((-25, 3, 12)) @ Matrix.Rotation(0.4, 4, 'Y')
        bone = data.edit_bones.new('Root')
        bone.matrix = get_transform_to_blender() @ rest
        bone.length = 0.25
        for key, matrix in [('transform', rest), ('transform1', Matrix.Identity(4)),
                            ('nicetransform', Matrix.Identity(4))]:
            bone[key] = [list(row) for row in matrix]
        bone['rbx_source_name'] = 'Root'
        bpy.ops.object.mode_set(mode='OBJECT')
        try:
            bpy.context.view_layer.update()
            state = serialize_animation_state(rig)
            actual = cf_to_mat(state.get('Root', mat_to_cf(Matrix.Identity(4))))
            self.assertLess(max(abs(x-y) for ra,rb in zip(actual,Matrix.Identity(4))
                                for x,y in zip(ra,rb)), 1e-5)
            rig.pose.bones['Root'].location = (0.2, -0.1, 0.3)
            bpy.context.view_layer.update()
            actual = cf_to_mat(serialize_animation_state(rig)['Root'])
            expected = rest.inverted() @ get_transform_to_blender().inverted() @ rig.pose.bones['Root'].matrix
            self.assertLess(max(abs(x-y) for ra,rb in zip(actual,expected)
                                for x,y in zip(ra,rb)), 1e-5)
        finally:
            bpy.data.objects.remove(rig, do_unlink=True)
            bpy.data.armatures.remove(data)
