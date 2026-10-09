"""Resolve weapon export/control relationships from actual bone wiring."""

import bpy


_FOLLOW_TYPES = {
    "COPY_TRANSFORMS",
    "COPY_LOCATION",
    "COPY_ROTATION",
    "COPY_SCALE",
    "CHILD_OF",
    "ARMATURE",
}


def bone_targets(bone):
    for constraint in bone.constraints:
        if constraint.type not in _FOLLOW_TYPES or constraint.mute:
            continue
        targets = constraint.targets if constraint.type == "ARMATURE" else (constraint,)
        for item in targets:
            target = getattr(item, "target", None)
            name = getattr(item, "subtarget", "")
            if target and target.type == "ARMATURE" and name in target.data.bones:
                yield target, name


def has_export_bones(armature):
    return any("transform" in b and "transform1" in b for b in armature.data.bones)


def resolve_weapon_rigs(selected, scene=None):
    """Return the export armature and its controls, from either selection.

    Incoming links are essential: controls usually only constrain themselves.
    Reject ambiguous incoming rigs instead of attaching to an unrelated rig.
    """
    objects = (scene or bpy.context.scene).objects
    incoming = []
    for obj in objects:
        if obj.type != "ARMATURE" or obj == selected or not has_export_bones(obj):
            continue
        count = sum(
            any(target == selected for target, _ in bone_targets(b))
            for b in obj.pose.bones
        )
        if count:
            incoming.append((count, obj))
    incoming.sort(key=lambda pair: pair[0], reverse=True)
    if incoming and (len(incoming) == 1 or incoming[0][0] > incoming[1][0]):
        return incoming[0][1], selected

    targets = {}
    for bone in selected.pose.bones:
        for target, _ in bone_targets(bone):
            if target != selected:
                targets[target] = targets.get(target, 0) + 1
    if has_export_bones(selected):
        if targets:
            ordered = sorted(targets, key=targets.get, reverse=True)
            if len(ordered) == 1 or targets[ordered[0]] > targets[ordered[1]]:
                return selected, ordered[0]
        has_internal_controls = any(
            target == selected
            for b in selected.pose.bones
            for target, _ in bone_targets(b)
        )
        return selected, selected if has_internal_controls else None
    exports = [target for target in targets if has_export_bones(target)]
    if len(exports) == 1:
        return exports[0], selected
    return selected, None


def clone_weapon_controls(context, source, controls, bone_names):
    """Resolve the controls per attachment, including mixed internal/external rigs."""
    from ..core.evaluation import rig_evaluation_context

    names = set(bone_names)
    groups = {}
    for name in names:
        bone = source.data.bones[name]
        while bone.parent and bone.parent.name in names:
            bone = bone.parent
        parent = bone.parent
        target = controls
        if parent:
            links = list(bone_targets(source.pose.bones[parent.name]))
            targets = set(obj for obj, _ in links)
            if len(targets) == 1:
                target = next(iter(targets))
            elif not links and (not target or parent.name not in target.data.bones):
                target = source
        if target:
            groups.setdefault(target, set()).add(name)
    mapping = {}
    for target, group in groups.items():
        with rig_evaluation_context(source, target):
            mapping.update(_clone_weapon_controls(context, source, target, group))
    return mapping


def _clone_weapon_controls(context, source, controls, bone_names):
    """Make only newly imported bones editable on the controls.

    Export bones follow the new controls in world space. Parent mapping comes
    from constraints, so renamed controls and different object transforms work.
    """
    from ..operators.import_ops import _ensure_all_bone_collections_visible

    names = set(bone_names)
    if not names or not controls:
        return {}
    parent_map = {}
    for name in names:
        bone = source.data.bones.get(name)
        if not bone or not bone.parent or bone.parent.name in names:
            continue
        parent = source.pose.bones[bone.parent.name]
        matches = list(
            dict.fromkeys(n for target, n in bone_targets(parent) if target == controls)
        )
        if len(matches) == 1:
            parent_map[bone.parent.name] = matches[0]
        elif not matches and bone.parent.name in controls.data.bones:
            parent_map[bone.parent.name] = bone.parent.name
        else:
            raise ValueError(
                f"Cannot identify a unique weapon control for '{bone.parent.name}'."
            )

    active = context.view_layer.objects.active
    mode = active.mode if active else "OBJECT"
    selected = list(context.selected_objects)
    mapping = {}
    evaluated = source.evaluated_get(context.evaluated_depsgraph_get())
    initial_pose = {
        name: controls.matrix_world.inverted()
        @ source.matrix_world
        @ evaluated.pose.bones[name].matrix
        for name in names
    }
    # Capture rest data before changing modes; transform into control space.
    relative = controls.matrix_world.inverted() @ source.matrix_world
    data = {
        b.name: (
            relative @ b.matrix_local,
            (relative.to_3x3() @ (b.tail_local - b.head_local)).length,
            b.parent.name if b.parent else None,
        )
        for b in source.data.bones
        if b.name in names
    }
    try:
        if active and active.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        context.view_layer.objects.active = controls
        controls.select_set(True)
        with _ensure_all_bone_collections_visible(controls):
            bpy.ops.object.mode_set(mode="EDIT")
            for name, (matrix, length, _) in data.items():
                bone = controls.data.edit_bones.new(
                    name if controls != source else name + "_Control"
                )
                bone.matrix = matrix
                bone.length = max(length, 0.001)
                bone.use_deform = False
                mapping[name] = bone.name
            for name, (_, _, parent) in data.items():
                parent_name = mapping.get(parent) or parent_map.get(parent)
                if parent_name:
                    controls.data.edit_bones[
                        mapping[name]
                    ].parent = controls.data.edit_bones[parent_name]
            bpy.ops.object.mode_set(mode="OBJECT")
        for name, control_name in mapping.items():
            controls.data.bones[control_name]["rbx_weapon_source"] = name
            # Match the visible pose, including parent constraint offsets.
            # Rest matrices alone would snap attachments on already posed rigs.
            context.view_layer.update()
            controls.pose.bones[control_name].matrix = initial_pose[name]
            context.view_layer.update()
            constraint = source.pose.bones[name].constraints.new("COPY_TRANSFORMS")
            constraint.name = "Weapon Control"
            constraint.target = controls
            constraint.subtarget = control_name
            constraint.owner_space = "WORLD"
            constraint.target_space = "WORLD"
    finally:
        if controls.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in selected:
            obj.select_set(True)
        context.view_layer.objects.active = active
        if active and mode != "OBJECT":
            bpy.ops.object.mode_set(mode=mode)
    return mapping
