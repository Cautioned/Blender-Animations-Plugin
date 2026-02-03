"""
Utility functions for the Roblox Animations Blender Addon.
"""

import bpy
import hashlib
import time
from mathutils import Matrix, Vector
from .constants import (
    get_blender_version,
    CACHE_DURATION,
    HASH_CACHE_DURATION,
    get_transform_to_blender,
)


# Global caches
_armature_cache = None
_armature_cache_timestamp = 0
_action_hash_cache = {}
_action_hash_cache_timestamp = 0

# Animation tracking globals
armature_anim_hashes = {}


def get_action_fcurves(action, slot=None):
    """Return the channelbag F-Curves for an action (Blender 4.4+ API).
    
    Handles legacy rigs imported into newer Blender versions by checking
    multiple sources for fcurves and preferring non-empty results.
    """
    blender_version = get_blender_version()
    channelbag = get_action_channelbag(action, slot=slot)
    
    # Try channelbag fcurves first
    if channelbag and hasattr(channelbag, "fcurves"):
        channelbag_fcurves = channelbag.fcurves
        # Only use if it actually has fcurves
        if channelbag_fcurves and len(channelbag_fcurves) > 0:
            return channelbag_fcurves

    # Fallback: check legacy action.fcurves directly
    # This handles cases where older rigs have empty slots but valid legacy fcurves
    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None and len(legacy_fcurves) > 0:
        return legacy_fcurves
    
    # If channelbag exists but was empty, still return it (might be intentionally empty)
    if channelbag and hasattr(channelbag, "fcurves"):
        return channelbag.fcurves

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
    """Return the ensured channelbag for an action slot, with legacy fallbacks.
    
    Handles the case where older rigs imported into Blender 4.4+ may have
    empty legacy slots - this function will find a slot with actual animation data.
    """
    if not action:
        return None

    blender_version = get_blender_version()
    modern_action_api = blender_version >= (4, 4, 0)

    slots_attr = getattr(action, "slots", None)
    if slots_attr is not None:
        target_slot = slot
        if target_slot is None:
            # Instead of just taking the first slot, find one that has actual data
            # This handles legacy rigs that may have empty slots from older Blender versions
            best_slot = None
            best_slot_fcurve_count = 0
            
            for candidate_slot in slots_attr:
                # Try to get fcurve count for this slot
                fcurve_count = 0
                try:
                    # Check via channelbag
                    candidate_channelbag = getattr(candidate_slot, "channelbag", None)
                    if candidate_channelbag and hasattr(candidate_channelbag, "fcurves"):
                        fcurve_count = len(candidate_channelbag.fcurves)
                    elif hasattr(candidate_slot, "fcurves"):
                        fcurve_count = len(candidate_slot.fcurves)
                except Exception:
                    pass
                
                # Prefer slot with more fcurves
                if fcurve_count > best_slot_fcurve_count:
                    best_slot = candidate_slot
                    best_slot_fcurve_count = fcurve_count
                elif best_slot is None:
                    # If no slot has fcurves yet, at least pick the first one
                    best_slot = candidate_slot
            
            if best_slot is not None:
                target_slot = best_slot
            elif slots_attr:
                # Fallback to first slot if our search found nothing
                target_slot = slots_attr[0]
            else:
                try:
                    target_slot = action.slots.new(
                        id_type="OBJECT", name=f"Object.{action.name}"
                    )
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
                ensure_fn = getattr(
                    anim_utils, "action_ensure_channelbag_for_slot", None
                )
                get_fn = getattr(anim_utils, "action_get_channelbag_for_slot", None)
                try:
                    if ensure_fn is not None:
                        channelbag = ensure_fn(action, target_slot)
                    elif get_fn is not None:
                        channelbag = get_fn(action, target_slot)
                except (AttributeError, TypeError) as exc:
                    slot_errors.append(exc)
                    channelbag = None
                except (
                    Exception
                ) as exc:  # pragma: no cover - defensive logging for unknown failures
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
            messages = ", ".join(
                sorted(
                    {type(err).__name__ + ": " + str(err) for err in slot_errors if err}
                )
            )
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
    fcurves = sorted(fcurves, key=lambda fc: (fc.data_path, fc.array_index))

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
    )
    return hashlib.md5(str(timeline_data).encode("utf-8")).hexdigest()


def get_armature_timeline_hash(armature_name):
    """Get combined hash of action and timeline for an armature"""
    obj = get_object_by_name(armature_name)
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

    if (
        cache_key not in _action_hash_cache
        or current_time - _action_hash_cache_timestamp > HASH_CACHE_DURATION
    ):
        # Refresh cache with combined hash
        _action_hash_cache[cache_key] = get_armature_timeline_hash(armature_name)
        _action_hash_cache_timestamp = current_time

    return _action_hash_cache[cache_key]


def get_cached_armatures():
    """Get cached armature list or refresh if cache is stale"""
    global _armature_cache, _armature_cache_timestamp

    current_time = time.time()
    if (
        _armature_cache is None
        or current_time - _armature_cache_timestamp > CACHE_DURATION
    ):
        # Refresh cache
        _armature_cache = [
            obj.name for obj in bpy.data.objects if obj.type == "ARMATURE"
        ]
        _armature_cache_timestamp = current_time
        print(
            f"Blender Addon: Refreshed armature cache with {len(_armature_cache)} armatures"
        )

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


def to_matrix(value):
    """Safely convert IDProperty value to Matrix"""
    if isinstance(value, Matrix):
        return value
    
    # Handle IDPropertyArray or list
    if hasattr(value, "to_list"):
        value = value.to_list()
    elif hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        value = list(value)
    
    if isinstance(value, list):
        if len(value) == 4:
            # Assume list of lists (4x4)
            try:
                return Matrix(tuple(tuple(row) for row in value))
            except Exception:
                pass
        elif len(value) == 16:
            # Assume flat list
            try:
                return Matrix([value[i:i+4] for i in range(0, 16, 4)])
            except Exception:
                pass
        elif len(value) == 12:
             # Assume CFrame list
            try:
                return cf_to_mat(value)
            except Exception:
                pass
                
    return Matrix.Identity(4)


def get_rig_facing_direction(armature_obj):
    """
    Determine the facing direction of a rig by extracting the forward vector
    from the root bone's transform.
    
    Args:
        armature_obj: The armature object (bpy.types.Object with type='ARMATURE')
    
    Returns:
        tuple: (forward_vector, root_bone_name) where:
            - forward_vector: Vector in Blender space representing the forward direction
            - root_bone_name: Name of the root bone used, or None if not found
    
    Returns None, None if the armature has no root bone or transform data.
    """
    if not armature_obj or armature_obj.type != "ARMATURE":
        return None, None
    
    # Find root bone (no parent)
    root_bone = None
    for bone in armature_obj.data.bones:
        if not bone.parent:
            root_bone = bone
            break
    
    if not root_bone:
        return None, None
    
    t2b = get_transform_to_blender()
    forward_vector = None
    
    # Try to get transform from Motor6D properties first
    if "transform" in root_bone:
        try:
            transform_data = root_bone["transform"]
            mat = to_matrix(transform_data)
            
            # Extract forward direction: in Roblox space, forward is +Z
            # Convert to Blender space
            roblox_forward = Vector((0, 0, 1))
            forward_vector = (t2b @ mat).to_3x3().to_4x4() @ roblox_forward
            forward_vector.normalize()
        except (KeyError, TypeError, ValueError):
            # Fall through to using bone matrix
            pass
    
    # Fallback: use bone's rest pose matrix (for deform rigs or if transform not available)
    if forward_vector is None:
        try:
            # Get the bone's matrix in object space
            bone_matrix = root_bone.matrix_local.copy()
            # Extract forward direction from the bone's Y-axis (Blender's forward)
            # In Blender, bones point along their Y-axis, so this is the bone's actual forward direction
            forward_vector = bone_matrix.to_3x3() @ Vector((0, 1, 0))
            forward_vector.normalize()
        except Exception:
            return None, None
    
    # Transform to world space if we got it from bone matrix
    # (transform from Motor6D is already in world space after t2b conversion
    if forward_vector and "transform" not in root_bone:
        # Convert from armature object space to world space
        forward_vector = (armature_obj.matrix_world.to_3x3() @ forward_vector).normalized()
    
    return forward_vector, root_bone.name


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


def get_object_by_name(name, scene=None):
    if not name:
        return None
    if scene is None:
        scene = getattr(bpy.context, "scene", None)
    if scene and hasattr(scene, "objects"):
        return scene.objects.get(name)
    return None


def object_exists(name, scene=None):
    return get_object_by_name(name, scene) is not None


def iter_scene_objects(scene=None):
    if scene is None:
        scene = getattr(bpy.context, "scene", None)
    if scene and hasattr(scene, "objects"):
        return scene.objects
    return []


def find_master_collection_for_object(obj):
    """Find the top-level 'RIG: ' collection for a given object."""
    for coll in bpy.data.collections:
        if coll.name.startswith("RIG: ") and obj.name in [
            o.name for o in coll.all_objects
        ]:
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
