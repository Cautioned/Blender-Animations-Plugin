"""Temporary visibility for evaluating and editing hidden rig dependencies."""

from contextlib import contextmanager

import bpy


@contextmanager
def rig_evaluation_context(*objects):
    """Enable the rig and its dependencies, restoring all visibility on exit."""
    required = set()
    pending = [obj for obj in objects if obj is not None]
    while pending:
        obj = pending.pop()
        if obj in required or not isinstance(obj, bpy.types.Object):
            continue
        required.add(obj)
        if obj.parent:
            pending.append(obj.parent)
        constraints = list(obj.constraints)
        if obj.type == "ARMATURE":
            constraints.extend(c for bone in obj.pose.bones for c in bone.constraints)
        for constraint in constraints:
            for attr in ("target", "pole_target"):
                target = getattr(constraint, attr, None)
                if target:
                    pending.append(target)
            for target in getattr(constraint, "targets", ()):
                if target.target:
                    pending.append(target.target)
        for owner in (obj, obj.data):
            animation = getattr(owner, "animation_data", None)
            for curve in getattr(animation, "drivers", ()):
                for variable in curve.driver.variables:
                    pending.extend(
                        target.id for target in variable.targets if target.id
                    )

    layers = []
    collections = {}

    def visit(layer):
        needed = any(obj in required for obj in layer.collection.objects)
        for child in layer.children:
            needed = visit(child) or needed
        if needed:
            layers.append((layer, layer.exclude, layer.hide_viewport))
            collections[layer.collection] = layer.collection.hide_viewport
        return needed

    visit(bpy.context.view_layer.layer_collection)
    object_states = []
    try:
        for collection in collections:
            collection.hide_viewport = False
        for layer, _, _ in reversed(layers):
            if layer.exclude:
                layer.exclude = False
            if layer.hide_viewport:
                layer.hide_viewport = False
        for obj in required:
            hidden = (
                obj.hide_get() if obj.name in bpy.context.view_layer.objects else None
            )
            object_states.append((obj, obj.hide_viewport, hidden))
            obj.hide_viewport = False
            if hidden is not None:
                obj.hide_set(False)
        bpy.context.view_layer.update()
        yield
    finally:
        for obj, disabled, hidden in object_states:
            if hidden is not None:
                obj.hide_set(hidden)
            obj.hide_viewport = disabled
        for layer, excluded, hidden in reversed(layers):
            if layer.hide_viewport != hidden:
                layer.hide_viewport = hidden
            if layer.exclude != excluded:
                layer.exclude = excluded
        for collection, hidden in collections.items():
            collection.hide_viewport = hidden
        bpy.context.view_layer.update()
