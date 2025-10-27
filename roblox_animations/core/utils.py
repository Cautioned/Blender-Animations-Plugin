"""
Utility functions for the Roblox Animations Blender Addon.
"""

import bpy
import hashlib
import time
import re
from mathutils import Vector, Matrix
from .constants import (
    get_blender_version,
    get_transform_to_blender,
    identity_cf,
    cf_round,
    cf_round_fac,
    CACHE_DURATION,
    HASH_CACHE_DURATION,
)


# Global caches
_armature_cache = None
_armature_cache_timestamp = 0
_action_hash_cache = {}
_action_hash_cache_timestamp = 0

# Animation tracking globals
armature_update_timestamps = {}
last_known_synced_armature = ""
armature_anim_hashes = {}


def get_action_fcurves(action, slot=None):
    """Return the channelbag F-Curves for an action (Blender 4.4+ API)."""
    blender_version = get_blender_version()
    channelbag = get_action_channelbag(action, slot=slot)
    if channelbag and hasattr(channelbag, "fcurves"):
        return channelbag.fcurves

    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        return legacy_fcurves

    if blender_version >= (4, 4, 0):
        raise RuntimeError(
            "unable to access animation fcurves; Blender 4.4+ requires a valid Action slot/channelbag."
        )

    return []


def pose_bone_selected(pose_bone):
    """Compatibility helper for pose bone selection state across Blender versions."""
    if pose_bone is None:
        return False

    if hasattr(pose_bone, "select"):
        return bool(pose_bone.select)

    bone = getattr(pose_bone, "bone", None)
    if bone is None:
        return False

    if hasattr(bone, "select"):
        return bool(bone.select)

    if hasattr(bone, "select_get"):
        try:
            return bool(bone.select_get())
        except TypeError:
            pass

    return False


def pose_bone_set_selected(pose_bone, value):
    """Compatibility helper to set pose bone selection state."""
    if pose_bone is None:
        return

    if hasattr(pose_bone, "select"):
        pose_bone.select = bool(value)
        return

    bone = getattr(pose_bone, "bone", None)
    if bone is None:
        return

    if hasattr(bone, "select_set"):
        try:
            bone.select_set(bool(value))
            return
        except TypeError:
            pass

    if hasattr(bone, "select"):
        bone.select = bool(value)


def get_action_channelbag(action, slot=None):
    """Return the ensured channelbag for an action slot, with legacy fallbacks."""
    if not action:
        return None

    blender_version = get_blender_version()
    modern_action_api = blender_version >= (4, 4, 0)

    slots_attr = getattr(action, "slots", None)
    if slots_attr is not None:
        target_slot = slot
        if target_slot is None:
            if slots_attr:
                target_slot = slots_attr[0]
            else:
                try:
                    target_slot = action.slots.new(id_type="OBJECT", name=f"Object.{action.name}")
                except TypeError:
                    target_slot = action.slots.new(id_type="OBJECT")
                except Exception:
                    target_slot = None

        slot_errors = []

        if target_slot is not None:
            channelbag = None
            try:
                import bpy_extras.anim_utils as anim_utils
            except ImportError:
                anim_utils = None

            if anim_utils is not None:
                ensure_fn = getattr(anim_utils, "action_ensure_channelbag_for_slot", None)
                get_fn = getattr(anim_utils, "action_get_channelbag_for_slot", None)
                try:
                    if ensure_fn is not None:
                        channelbag = ensure_fn(action, target_slot)
                    elif get_fn is not None:
                        channelbag = get_fn(action, target_slot)
                except (AttributeError, TypeError) as exc:
                    slot_errors.append(exc)
                    channelbag = None
                except Exception as exc:  # pragma: no cover - defensive logging for unknown failures
                    slot_errors.append(exc)
                    channelbag = None

            if channelbag is not None:
                return channelbag

            direct_channelbag = getattr(target_slot, "channelbag", None)
            if direct_channelbag is not None:
                return direct_channelbag

            if hasattr(target_slot, "fcurves"):
                class _LegacySlotChannelbag:
                    def __init__(self, slot_obj):
                        self.fcurves = slot_obj.fcurves
                        self.groups = getattr(slot_obj, "groups", [])

                return _LegacySlotChannelbag(target_slot)

        if modern_action_api and slot_errors:
            messages = ", ".join(sorted({type(err).__name__ + ": " + str(err) for err in slot_errors if err}))
            raise RuntimeError(
                "failed to access Blender action channelbag via slots API; "
                "ensure bpy_extras.anim_utils is available and the action has a valid slot. "
                f"(bpy {blender_version}, errors: {messages or 'no additional details'})"
            )

    if hasattr(action, "fcurves"):
        class _LegacyActionChannelbag:
            def __init__(self, legacy_action):
                self.fcurves = legacy_action.fcurves
                self.groups = getattr(legacy_action, "groups", [])

        return _LegacyActionChannelbag(action)

    if modern_action_api:
        raise RuntimeError(
            "Blender 4.4+ no longer exposes action.fcurves directly; "
            "failed to obtain channelbag for action despite using the modern API."
        )

    return None


def get_action_hash(action):
    """Get hash of an action's keyframe data."""
    if not action:
        return ""

    # Get fcurves using compatibility function
    fcurves = get_action_fcurves(action)
    if not fcurves:
        return ""
    
    # Sort fcurves to ensure consistent hash
    fcurves = sorted(fcurves, key=lambda fc: (
        fc.data_path, fc.array_index))

    hash_parts = []
    for fcurve in fcurves:
        # Include data path and index to distinguish curves
        hash_parts.append(f"{fcurve.data_path}:{fcurve.array_index}")
        for kp in fcurve.keyframe_points:
            # Include all relevant keyframe point properties
            kp_data = (
                kp.co.x,
                kp.co.y,
                kp.handle_left.x,
                kp.handle_left.y,
                kp.handle_right.x,
                kp.handle_right.y,
                kp.interpolation,
                kp.easing,
            )
            hash_parts.append(repr(kp_data))

    data_string = "".join(hash_parts)
    return hashlib.md5(data_string.encode("utf-8")).hexdigest()


def get_timeline_hash():
    """Get hash of timeline settings (frame start, end, fps, etc.)"""
    scene = bpy.context.scene
    timeline_data = (
        scene.frame_start,
        scene.frame_end,
        scene.render.fps,
        scene.frame_current,
    )
    return hashlib.md5(str(timeline_data).encode("utf-8")).hexdigest()


def get_armature_timeline_hash(armature_name):
    """Get combined hash of action and timeline for an armature"""
    obj = bpy.data.objects.get(armature_name)
    if not obj or obj.type != "ARMATURE":
        return ""
    
    action = obj.animation_data.action if obj.animation_data else None
    action_hash = get_action_hash(action)
    timeline_hash = get_timeline_hash()
    
    # Combine action and timeline hashes
    combined_data = f"{action_hash}:{timeline_hash}"
    return hashlib.md5(combined_data.encode("utf-8")).hexdigest()


def get_cached_armature_hash(armature_name):
    """Get cached combined hash (action + timeline) or compute if cache is stale"""
    global _action_hash_cache, _action_hash_cache_timestamp
    
    if not armature_name:
        return ""
    
    current_time = time.time()
    cache_key = f"{armature_name}_combined"  # Use armature name as cache key
    
    if (cache_key not in _action_hash_cache or 
        current_time - _action_hash_cache_timestamp > HASH_CACHE_DURATION):
        
        # Refresh cache with combined hash
        _action_hash_cache[cache_key] = get_armature_timeline_hash(armature_name)
        _action_hash_cache_timestamp = current_time
    
    return _action_hash_cache[cache_key]


def has_animation_changed(action):
    """Check if animation has changed since last check - simplified approach"""
    global _last_animation_check_time, _animation_change_detected
    
    current_time = time.time()
    
    # Always return True for now to ensure changes are detected
    # The caching will still provide performance benefits
    # TODO: Implement more reliable change detection
    return True


def on_animation_update(scene):
    """depsgraph handler to detect animation changes"""
    global last_known_synced_armature, armature_anim_hashes, armature_update_timestamps
    if not last_known_synced_armature:
        return

    obj = bpy.data.objects.get(last_known_synced_armature)
    if not (obj and obj.type == "ARMATURE"):
        return

    action = obj.animation_data.action if obj.animation_data else None

    current_hash = get_action_hash(action)
    last_hash = armature_anim_hashes.get(obj.name)

    if current_hash != last_hash:
        print(f"Keyframe change detected for {obj.name}. Updating timestamp.")
        armature_update_timestamps[obj.name] = time.time()
        armature_anim_hashes[obj.name] = current_hash


def get_cached_armatures():
    """Get cached armature list or refresh if cache is stale"""
    global _armature_cache, _armature_cache_timestamp
    
    current_time = time.time()
    if (_armature_cache is None or 
        current_time - _armature_cache_timestamp > CACHE_DURATION):
        
        # Refresh cache
        _armature_cache = [obj.name for obj in bpy.data.objects if obj.type == 'ARMATURE']
        _armature_cache_timestamp = current_time
        print(f"Blender Addon: Refreshed armature cache with {len(_armature_cache)} armatures")
    
    return _armature_cache


def invalidate_armature_cache():
    """Force cache invalidation - useful for tests"""
    global _armature_cache, _armature_cache_timestamp
    _armature_cache = None
    _armature_cache_timestamp = 0


def armature_items(self, context):
    """Callback for armature enum property"""
    items = []
    for armature_name in get_cached_armatures():
        items.append((armature_name, armature_name, ""))
    return items


# Matrix and CFrame utilities
def cf_to_mat(cf):
    """Convert CFrame to matrix"""
    mat = Matrix.Translation((cf[0], cf[1], cf[2]))
    mat[0][0:3] = (cf[3], cf[4], cf[5])
    mat[1][0:3] = (cf[6], cf[7], cf[8])
    mat[2][0:3] = (cf[9], cf[10], cf[11])
    return mat


def mat_to_cf(mat):
    """Convert matrix to CFrame"""
    r_mat = [
        mat[0][3],
        mat[1][3],
        mat[2][3],
        mat[0][0],
        mat[0][1],
        mat[0][2],
        mat[1][0],
        mat[1][1],
        mat[1][2],
        mat[2][0],
        mat[2][1],
        mat[2][2],
    ]
    return r_mat


def get_unique_name(base_name):
    """Generates a unique object name by appending a .001, .002, etc. suffix if the name already exists."""
    existing_names = {obj.name for obj in bpy.data.objects}
    if base_name not in existing_names:
        return base_name

    counter = 1
    new_name = f"{base_name}.{counter:03d}"
    while new_name in existing_names:
        counter += 1
        new_name = f"{base_name}.{counter:03d}"
    return new_name


def find_master_collection_for_object(obj):
    """Find the top-level 'RIG: ' collection for a given object."""
    for coll in bpy.data.collections:
        if coll.name.startswith("RIG: ") and obj.name in [o.name for o in coll.all_objects]:
            return coll
    return None


def find_parts_collection_in_master(master_collection, create_if_missing=False):
    """Finds the 'Parts' collection within a master rig collection. Optionally creates it."""
    if not master_collection:
        return None
    for child in master_collection.children:
        if child.name.startswith("Parts"):
            return child
    if create_if_missing:
        parts_coll = bpy.data.collections.new("Parts")
        master_collection.children.link(parts_coll)
        return parts_coll
    return None


def set_scene_fps(desired_fps):
    """Set the scene FPS"""
    scene = bpy.context.scene
    scene.render.fps = int(desired_fps)
    scene.render.fps_base = 1.0  # Ensure fps_base is set to 1.0 for consistency


def get_scene_fps():
    """Get the current scene FPS"""
    scene = bpy.context.scene
    return scene.render.fps / scene.render.fps_base

