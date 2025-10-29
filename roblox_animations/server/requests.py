"""
Request processing and task management for the animation server.
"""

import json
import traceback
import zlib
import bpy
import bpy_extras
from mathutils import Vector, Matrix
from ..core.utils import get_action_hash, get_scene_fps, set_scene_fps, cf_to_mat
from ..core import utils
from ..animation.serialization import serialize, is_deform_bone_rig


# Global request queues
pending_requests = []
pending_responses = {}

transform_to_blender = bpy_extras.io_utils.axis_conversion(
    from_forward="Z", from_up="Y", to_forward="-Y", to_up="Z"
).to_4x4()  # transformation matrix from Y-up to Z-up


def process_pending_requests():
    """Process any pending animation requests"""
    try:
        if pending_requests:  # Check if list is not empty
            print(f"Blender Addon: Processing {len(pending_requests)} pending requests")
            request = pending_requests.pop(0)
            request_type = request[0]

            if request_type == "export_animation":
                _, task_id, armature_name = request
                print(
                    f"Blender Addon: Dispatching export_animation task (task_id={task_id}, armature={armature_name})"
                )
                execute_in_main_thread(task_id, armature_name)
            elif request_type == "list_armatures":
                _, task_id = request
                print(
                    f"Blender Addon: Dispatching list_armatures task (task_id={task_id})"
                )
                execute_list_armatures(task_id)
            elif request_type == "import":
                if len(request) == 4:
                    _, task_id, animation_data, target_armature = request
                    print(
                        f"Blender Addon: Dispatching import task (task_id={task_id}, target={target_armature})"
                    )
                    execute_import_animation(task_id, animation_data, target_armature)
                else:
                    _, task_id, animation_data = request
                    print(f"Blender Addon: Dispatching import task (task_id={task_id})")
                    execute_import_animation(task_id, animation_data)
            elif request_type == "get_bone_rest":
                _, task_id, armature_name = request
                print(
                    f"Blender Addon: Dispatching get_bone_rest task (task_id={task_id}, armature={armature_name})"
                )
                execute_get_bone_rest(task_id, armature_name)

    except Exception as e:
        print(f"Blender Addon: Error processing requests: {str(e)}")
        traceback.print_exc()
    return 0.01  # Run every 10ms for good balance


def execute_list_armatures(task_id):
    """Execute the armature listing in the main thread"""
    try:
        print("Blender Addon: Executing list_armatures in main thread...")

        # Force refresh by invalidating cache first
        utils.invalidate_armature_cache()

        fresh_armatures = [
            obj.name for obj in bpy.data.objects if obj.type == "ARMATURE"
        ]

        armatures = []
        for armature_name in fresh_armatures:
            obj = bpy.data.objects.get(armature_name)
            if not obj:
                # Try to find the object even if it's hidden/disabled
                for obj_candidate in bpy.data.objects:
                    if (
                        obj_candidate.name == armature_name
                        and obj_candidate.type == "ARMATURE"
                    ):
                        obj = obj_candidate
                        break

            if obj:  # Double-check object still exists
                # build bone hierarchy map: { bone_name: parent_bone_name or None }
                bone_hierarchy = {}
                for bone in obj.data.bones:
                    bone_hierarchy[bone.name] = (
                        bone.parent.name if bone.parent else None
                    )

                armature_info = {
                    "name": obj.name,
                    "bones": [bone.name for bone in obj.data.bones],
                    "num_bones": len(obj.data.bones),
                    "has_animation": bool(
                        obj.animation_data and obj.animation_data.action
                    ),
                    "frame_range": [
                        bpy.context.scene.frame_start,
                        bpy.context.scene.frame_end,
                    ]
                    if obj.animation_data
                    else None,
                    "bone_hierarchy": bone_hierarchy,
                }
                armatures.append(armature_info)

                # Pre-cache the hash for this armature
                action = obj.animation_data.action if obj.animation_data else None
                utils.armature_anim_hashes[obj.name] = get_action_hash(action)

        found_armature_names = [a["name"] for a in armatures]
        print(f"Blender Addon: Found armatures: {found_armature_names}")

        response = {
            "armatures": armatures,
            "current": getattr(
                getattr(bpy.context.scene, "rbx_anim_settings", None),
                "rbx_anim_armature",
                None,
            ),
            "fps": bpy.context.scene.render.fps,
        }

        data = json.dumps(response).encode("utf-8")
        print(
            f"Blender Addon: Prepared response for task {task_id}. Armature count: {len(armatures)}"
        )
        pending_responses[task_id] = (True, data)

    except Exception as e:
        print(f"Blender Addon: Error in execute_list_armatures: {str(e)}")
        traceback.print_exc()
        pending_responses[task_id] = (
            False,
            f"Error listing armatures: {str(e)}",
        )


def execute_in_main_thread(task_id, armature_name):
    """Execute the animation export in the main thread"""
    try:
        if not armature_name:
            pending_responses[task_id] = (False, "No armature selected")
            return

        ao = bpy.data.objects.get(armature_name)
        if not ao:
            # Try to find the object even if it's hidden/disabled
            for obj in bpy.data.objects:
                if obj.name == armature_name and obj.type == "ARMATURE":
                    ao = obj
                    break

            if not ao:
                pending_responses[task_id] = (
                    False,
                    f"Armature '{armature_name}' not found.",
                )
                return

        if ao.type != "ARMATURE":
            pending_responses[task_id] = (
                False,
                f"Object '{armature_name}' is not an armature (type: {ao.type}). Please select a valid armature object.",
            )
            return

        bpy.context.view_layer.objects.active = ao
        # Only switch mode if necessary to avoid expensive operations
        if ao.mode != "POSE":
            print(f"Blender Addon: Switching to POSE mode for '{armature_name}'...")
            bpy.ops.object.mode_set(mode="POSE")

        desired_fps = get_scene_fps()
        set_scene_fps(desired_fps)

        # Check if this is a deform bone rig or if deform bone serialization is forced
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        force_deform = getattr(settings, "force_deform_bone_serialization", False)

        use_deform_bone_serialization = is_deform_bone_rig(ao) or force_deform
        print(
            f"Server export: Using {'deform bone' if use_deform_bone_serialization else 'Motor6D'} serialization"
        )
        print(f"Blender Addon: Starting animation export for '{armature_name}'...")

        serialized = serialize(ao)
        if not serialized:
            pending_responses[task_id] = (
                False,
                f"Failed to serialize animation for '{armature_name}'. Check if the armature has animation data or keyframes.",
            )
            return

        if not serialized.get("kfs") or len(serialized["kfs"]) == 0:
            pending_responses[task_id] = (
                False,
                f"No animation data found for '{armature_name}'. Please add keyframes or animation data to the armature.",
            )
            return

        encoded = json.dumps(serialized, separators=(",", ":"))
        compressed = zlib.compress(encoded.encode("utf-8"))

        pending_responses[task_id] = (True, compressed)

    except Exception as e:
        print(f"Error in main thread: {str(e)}")
        traceback.print_exc()
        pending_responses[task_id] = (False, f"Error during export: {str(e)}")


def execute_import_animation(task_id, animation_data, target_armature=None):
    """Execute the animation import in the main thread"""
    try:
        # Use target armature if provided, otherwise fall back to scene selection
        if target_armature:
            armature_name = target_armature
        else:
            settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
            armature_name = settings.rbx_anim_armature if settings else None

        if not armature_name:
            raise ValueError(
                "No armature specified for import. Please provide target armature or select one in the scene."
            )

        ao = bpy.data.objects.get(armature_name)
        if not ao:
            # Try to find the object even if it's hidden/disabled
            for obj in bpy.data.objects:
                if obj.name == armature_name and obj.type == "ARMATURE":
                    ao = obj
                    break

            if not ao:
                raise ValueError(
                    f"Selected object '{armature_name}' is not a valid armature."
                )

        if ao.type != "ARMATURE":
            raise ValueError(
                f"Selected object '{armature_name}' is not a valid armature."
            )

        bpy.context.view_layer.objects.active = ao
        if ao.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")

        # Clear existing animation data
        if ao.animation_data:
            ao.animation_data_clear()

        # Reset pose to rest position to ensure a clean slate
        if ao.pose:
            for bone in ao.pose.bones:
                bone.matrix_basis = Matrix.Identity(4)
            bpy.context.view_layer.update()  # Ensure the pose update is registered

        action = bpy.data.actions.new(name=f"{armature_name}_ImportedAnimation")
        ao.animation_data_create()
        ao.animation_data.action = action

        # Ensure a compatible action slot exists (Blender 4.4+)
        active_slot = None
        if hasattr(action, "slots"):
            if action.slots:
                active_slot = action.slots[0]
            else:
                try:
                    active_slot = action.slots.new(
                        id_type="OBJECT", name=f"OB{ao.name}"
                    )
                except TypeError:
                    active_slot = action.slots.new(id_type="OBJECT")
            if active_slot and hasattr(ao.animation_data, "action_slot"):
                try:
                    ao.animation_data.action_slot = active_slot
                except Exception:
                    pass
        else:
            pass

        fps = animation_data.get("export_info", {}).get("fps", get_scene_fps())
        set_scene_fps(fps)

        scene = bpy.context.scene
        scene.frame_start = 0
        scene.frame_end = int(animation_data["t"] * fps)

        is_deform_rig = animation_data.get("is_deform_bone_rig", False)

        # This will store all transform data for all bones across all frames before we create any keyframes.
        # Format: { bone_name: { frame: {"location": Vector, "rotation": Quaternion, "scale": Vector}, ... }, ... }
        all_bone_data = {}

        # Reset pose to rest position and pre-populate all transformable bones
        # with their rest pose at the start frame. This ensures a defined initial state.
        if ao.pose:
            for bone in ao.pose.bones:
                bone.matrix_basis = Matrix.Identity(4)
            bpy.context.view_layer.update()

            start_frame = scene.frame_start
            for bone in ao.pose.bones:
                is_transformable = (
                    not is_deform_rig and "is_transformable" in bone.bone
                ) or (is_deform_rig and bone.bone.use_deform)
                if is_transformable:
                    all_bone_data[bone.name] = {
                        start_frame: {
                            "location": bone.location.copy(),
                            "rotation_quaternion": bone.rotation_quaternion.copy(),
                            "scale": bone.scale.copy(),
                        }
                    }

        for kf_data in animation_data["kfs"]:
            frame = int(kf_data["t"] * fps)
            state = kf_data["kf"]

            bones_to_process = []
            for bone_name in state.keys():
                pose_bone = ao.pose.bones.get(bone_name)
                if pose_bone:
                    bones_to_process.append(pose_bone)

            bones_to_process.sort(key=lambda b: len(b.parent_recursive))

            # Simplified single-pass processing loop.
            # By iterating through bones sorted by hierarchy (parents first), we ensure
            # that when we calculate a child's matrix, the parent's matrix for the
            # current frame has already been set.
            for pose_bone in bones_to_process:
                bone_name = pose_bone.name
                pose_data = state.get(bone_name)
                if not pose_data:
                    continue

                # Backwards compatibility: Handle old list-based format and new dict-based format.
                easing_style = "Linear"  # Default easing
                easing_direction = "In"  # Default easing

                if isinstance(pose_data, list) and len(pose_data) == 3:
                    # New, more robust format: [ [cframe_components], "EasingStyle", "EasingDirection" ]
                    cframe_components = pose_data[0]
                    easing_style = pose_data[1]
                    easing_direction = pose_data[2]
                elif isinstance(pose_data, dict):
                    # old format with easing styles
                    cframe_components = pose_data.get("components", [])
                    easing_style = pose_data.get("easingStyle", "Linear")
                    easing_direction = pose_data.get("easingDirection", "In")
                elif isinstance(pose_data, list):
                    # oldest format, just a list of cframe components
                    cframe_components = pose_data

                if not cframe_components:
                    continue

                bone_transform = cf_to_mat(cframe_components)

                # --- Matrix Calculation ---
                if is_deform_rig:
                    settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
                    scale_factor = getattr(settings, "rbx_deform_rig_scale", 1.0)

                    loc, rot, sca = bone_transform.decompose()
                    loc_blender = Vector(
                        (
                            -loc.x * scale_factor,
                            loc.y * scale_factor,
                            -loc.z * scale_factor,
                        )
                    )
                    sca_blender = Vector((sca.x, sca.z, sca.y))
                    rot.x, rot.z = -rot.x, -rot.z
                    loc_mat = Matrix.Translation(loc_blender)
                    rot_mat = rot.to_matrix().to_4x4()
                    sca_mat = Matrix.Diagonal(sca_blender).to_4x4()
                    delta_transform = loc_mat @ rot_mat @ sca_mat
                    rest_local_transform = pose_bone.bone.matrix_local
                    final_matrix = rest_local_transform @ delta_transform
                else:  # Motor6D rig
                    back_trans = transform_to_blender.inverted()
                    extr_transform = Matrix(pose_bone.bone["nicetransform"]).inverted()

                    orig_mat = Matrix(pose_bone.bone["transform"])
                    orig_mat_tr1 = Matrix(pose_bone.bone["transform1"])

                    if pose_bone.parent and "transform" in pose_bone.parent.bone:
                        parent_orig_mat = Matrix(pose_bone.parent.bone["transform"])
                        parent_orig_mat_tr1 = Matrix(
                            pose_bone.parent.bone["transform1"]
                        )

                        orig_base_mat = back_trans @ (orig_mat @ orig_mat_tr1)
                        parent_orig_base_mat = back_trans @ (
                            parent_orig_mat @ parent_orig_mat_tr1
                        )
                        orig_transform = parent_orig_base_mat.inverted() @ orig_base_mat

                        cur_transform = orig_transform @ bone_transform

                        parent_extr_transform = Matrix(
                            pose_bone.parent.bone["nicetransform"]
                        ).inverted()

                        # Use the parent's current matrix from the pose, which was set in the previous iteration
                        parent_matrix = pose_bone.parent.matrix
                        parent_obj_transform = back_trans @ (
                            parent_matrix @ parent_extr_transform
                        )

                        cur_obj_transform = parent_obj_transform @ cur_transform
                    else:
                        cur_obj_transform = bone_transform

                    final_matrix = (
                        transform_to_blender
                        @ cur_obj_transform
                        @ extr_transform.inverted()
                    )

                # --- Apply and Store ---
                pose_bone.matrix = final_matrix

                if bone_name in all_bone_data:
                    all_bone_data[bone_name][frame] = {
                        "location": pose_bone.location.copy(),
                        "rotation_quaternion": pose_bone.rotation_quaternion.copy(),
                        "scale": pose_bone.scale.copy(),
                        "easingStyle": easing_style,
                        "easingDirection": easing_direction,
                    }

        for bone_name, frame_data in all_bone_data.items():
            sorted_frames = sorted(frame_data.keys())

            channelbag = utils.get_action_channelbag(action)
            if channelbag is None or not hasattr(channelbag, "fcurves"):
                legacy_fcurves = getattr(action, "fcurves", None)
                if legacy_fcurves is None:
                    raise RuntimeError(
                        "Unable to access animation channelbag for import"
                    )

                class _LegacyChannelbag:
                    def __init__(self, fcurves, groups):
                        self.fcurves = fcurves
                        self.groups = groups

                    def new(self, *args, **kwargs):
                        return self.fcurves.new(*args, **kwargs)

                channelbag = _LegacyChannelbag(
                    legacy_fcurves, getattr(action, "groups", [])
                )

            # Create fcurves with version-appropriate parameters
            def create_fcurve(data_path, index, group_name):
                try:
                    return channelbag.fcurves.new(data_path, index=index)
                except TypeError:
                    if hasattr(channelbag.fcurves, "new"):
                        for candidate in ("group_name", "action_group"):
                            try:
                                return channelbag.fcurves.new(
                                    data_path, index=index, **{candidate: group_name}
                                )
                            except TypeError:
                                continue
                    return channelbag.fcurves.new(data_path, index=index)

            # Location
            loc_fcurves = [
                create_fcurve(
                    f'pose.bones["{bpy.utils.escape_identifier(bone_name)}"].location',
                    i,
                    bone_name,
                )
                for i in range(3)
            ]
            # Rotation
            rot_fcurves = [
                create_fcurve(
                    f'pose.bones["{bpy.utils.escape_identifier(bone_name)}"].rotation_quaternion',
                    i,
                    bone_name,
                )
                for i in range(4)
            ]
            # Scale
            scale_fcurves = [
                create_fcurve(
                    f'pose.bones["{bpy.utils.escape_identifier(bone_name)}"].scale',
                    i,
                    bone_name,
                )
                for i in range(3)
            ]

            # Most Roblox easings map to their corresponding interpolation type in Blender.
            interpolation_map = {
                "Linear": "LINEAR",
                "Constant": "CONSTANT",
                "Elastic": "ELASTIC",
                "Bounce": "BOUNCE",
                "Sine": "SINE",
                "Quad": "QUAD",
                "Cubic": "CUBIC",
                "CubicV2": "CUBIC",
                "Quart": "QUART",
                "Quint": "QUINT",
                "Expo": "EXPO",
                "Circular": "CIRC",
                "Back": "BACK",
            }

            num_frames = len(sorted_frames)
            if num_frames == 0:
                continue

            for fcurve in loc_fcurves + rot_fcurves + scale_fcurves:
                fcurve.keyframe_points.add(num_frames)

            for idx, frame in enumerate(sorted_frames):
                transforms = frame_data[frame]
                loc = transforms["location"]
                rot = transforms["rotation_quaternion"]
                scl = transforms["scale"]

                for axis in range(3):
                    kp = loc_fcurves[axis].keyframe_points[idx]
                    kp.co = (frame, loc[axis])
                    kp.handle_left_type = kp.handle_right_type = "AUTO"
                for axis in range(4):
                    kp = rot_fcurves[axis].keyframe_points[idx]
                    kp.co = (frame, rot[axis])
                    kp.handle_left_type = kp.handle_right_type = "AUTO"
                for axis in range(3):
                    kp = scale_fcurves[axis].keyframe_points[idx]
                    kp.co = (frame, scl[axis])
                    kp.handle_left_type = kp.handle_right_type = "AUTO"

            if num_frames > 1:
                for idx in range(num_frames - 1):
                    current_frame_time = sorted_frames[idx]
                    current_transforms = frame_data[current_frame_time]
                    easing_style = current_transforms.get("easingStyle", "Linear")
                    easing_direction = current_transforms.get("easingDirection", "In")

                    for fcurve in loc_fcurves + rot_fcurves + scale_fcurves:
                        kp_current = fcurve.keyframe_points[idx]
                        kp_current.interpolation = interpolation_map.get(
                            easing_style, "LINEAR"
                        )

                        if kp_current.interpolation not in ["LINEAR", "CONSTANT"]:
                            if easing_direction == "In":
                                kp_current.easing = "EASE_IN"
                            elif easing_direction == "Out":
                                kp_current.easing = "EASE_OUT"
                            elif easing_direction == "InOut":
                                kp_current.easing = "EASE_IN_OUT"
                        else:
                            kp_current.easing = "AUTO"

            for fcurve in loc_fcurves + rot_fcurves + scale_fcurves:
                fcurve.update()

        pending_responses[task_id] = (True, "Animation imported successfully")

    except Exception as e:
        print(f"Error in main thread (import_animation): {str(e)}")
        traceback.print_exc()
        pending_responses[task_id] = (False, f"Error importing animation: {str(e)}")


def execute_get_bone_rest(task_id, armature_name):
    """
    Gets the rest pose for all bones in an armature.
    This function uses the same rest pose calculation as the deform bone
    serializer to ensure perfect consistency between the rig setup
    and animation export.
    """
    original_mode = None
    ao = None
    saved_bone_matrices = {}

    try:
        if not armature_name:
            pending_responses[task_id] = (False, "No armature selected")
            return

        ao = bpy.data.objects.get(armature_name)
        if not ao:
            # Try to find the object even if it's hidden/disabled
            for obj in bpy.data.objects:
                if obj.name == armature_name and obj.type == "ARMATURE":
                    ao = obj
                    break

            if not ao:
                pending_responses[task_id] = (
                    False,
                    f"Object '{armature_name}' is not a valid armature.",
                )
                return

        if ao.type != "ARMATURE":
            pending_responses[task_id] = (
                False,
                f"Object '{armature_name}' is not a valid armature.",
            )
            return

        from ..core.constants import get_transform_to_blender

        back_trans = get_transform_to_blender().inverted()
        world_transform = back_trans @ ao.matrix_world

        bone_poses = {}

        original_mode = ao.mode
        bpy.context.view_layer.objects.active = ao
        if ao.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")

        # Save current bone poses before clearing them
        for bone in ao.pose.bones:
            saved_bone_matrices[bone.name] = bone.matrix_basis.copy()

        # Clear transforms to guarantee we are calculating from the rest pose.
        # This is crucial for ensuring that pose_bone.matrix reflects the actual
        # rest pose of the armature.
        bpy.ops.pose.select_all(action="SELECT")
        bpy.ops.pose.transforms_clear()
        bpy.ops.pose.select_all(action="DESELECT")

        # Cache for rest transforms to avoid re-calculating for parents
        rest_transform_cache = {}

        # Iterate through bones sorted by hierarchy to ensure parents are processed before their children.
        # This is essential for correctly calculating parent-relative transforms.
        sorted_bones = sorted(ao.pose.bones, key=lambda b: len(b.parent_recursive))

        for pose_bone in sorted_bones:
            has_nicetransform = "nicetransform" in pose_bone.bone

            if has_nicetransform:
                # Traditional bone with nicetransform - use tail position logic
                extr_inv = Matrix(pose_bone.bone["nicetransform"]).inverted()
                # Use the tail position instead of head by translating matrix_local by the bone vector.
                tail_local_matrix = pose_bone.bone.matrix_local @ Matrix.Translation(
                    pose_bone.bone.vector
                )
                rest_obj_transform = world_transform @ (tail_local_matrix @ extr_inv)
            else:
                # New bone without nicetransform - use head position (matrix_local)
                rest_obj_transform = world_transform @ pose_bone.bone.matrix_local

            rest_transform_cache[pose_bone.name] = rest_obj_transform

            # Calculate the bone's rest transform relative to its parent.
            if pose_bone.parent:
                parent_rest_transform = rest_transform_cache.get(pose_bone.parent.name)
                if parent_rest_transform:
                    try:
                        # The relative transform is the transformation from the parent's space to the child's space.
                        rest_local_transform = (
                            parent_rest_transform.inverted() @ rest_obj_transform
                        )
                    except ValueError:
                        # Fallback for non-invertible parent matrix, though this is rare.
                        rest_local_transform = rest_obj_transform
                else:
                    # This case should not be hit with presorted bones, but serves as a safe fallback.
                    rest_local_transform = rest_obj_transform
            else:
                # For root bones, the local transform is the same as its object-space transform.
                rest_local_transform = rest_obj_transform

            world_matrix = rest_obj_transform
            relative_matrix = rest_local_transform

            # Convert matrices to Roblox CFrame-compatible components.
            world_translation = world_matrix.to_translation()
            world_components = [
                world_translation.x,
                world_translation.y,
                world_translation.z,
                world_matrix[0][0],
                world_matrix[0][1],
                world_matrix[0][2],
                world_matrix[1][0],
                world_matrix[1][1],
                world_matrix[1][2],
                world_matrix[2][0],
                world_matrix[2][1],
                world_matrix[2][2],
            ]

            relative_translation = relative_matrix.to_translation()
            relative_components = [
                relative_translation.x,
                relative_translation.y,
                relative_translation.z,
                relative_matrix[0][0],
                relative_matrix[0][1],
                relative_matrix[0][2],
                relative_matrix[1][0],
                relative_matrix[1][1],
                relative_matrix[1][2],
                relative_matrix[2][0],
                relative_matrix[2][1],
                relative_matrix[2][2],
            ]

            is_synthetic = pose_bone.bone.get("is_synthetic_helper", False)
            bone_poses[pose_bone.name] = {
                "world": world_components,
                "relative": relative_components,
                "parent": pose_bone.parent.name if pose_bone.parent else None,
                "is_synthetic_helper": is_synthetic,
            }

        # Restore original mode
        if ao.mode != original_mode:
            bpy.ops.object.mode_set(mode=original_mode)

        response = {"armature": armature_name, "bone_poses": bone_poses}

        data = json.dumps(response).encode("utf-8")
        pending_responses[task_id] = (True, data)

    except Exception as e:
        traceback.print_exc()
        pending_responses[task_id] = (False, f"Error getting bone rest poses: {str(e)}")
    finally:
        # Restore original bone poses
        if ao and saved_bone_matrices:
            for bone in ao.pose.bones:
                if bone.name in saved_bone_matrices:
                    bone.matrix_basis = saved_bone_matrices[bone.name]

        # Ensure mode is restored even if an error occurs
        if original_mode and ao and ao.mode != original_mode:
            bpy.ops.object.mode_set(mode=original_mode)
