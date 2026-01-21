"""
ik (inverse kinematics) setup and management utilities.
"""

import math
from typing import Optional, Tuple, List

import bpy
from mathutils import Vector, Matrix


def has_ik_constraint(ao: "bpy.types.Object", pose_bone: "bpy.types.PoseBone") -> bool:
    """Check if the given pose bone has an IK constraint applied to it.
    
    Also checks if this bone is an IK target/pole bone (created by our IK system).
    """
    # Check if this bone has an IK constraint
    for constraint in pose_bone.constraints:
        if constraint.type == "IK":
            return True
    
    # Check if this is an IK target or pole bone we created
    bone_name = pose_bone.name
    if bone_name.endswith("-IKTarget") or bone_name.endswith("-IKPole"):
        return True
    
    # Check if any bone in the armature has an IK constraint targeting this bone
    for other_bone in ao.pose.bones:
        for constraint in other_bone.constraints:
            if constraint.type == "IK":
                if constraint.subtarget == bone_name or constraint.pole_subtarget == bone_name:
                    return True
    
    return False


def get_ik_constraint(ao: "bpy.types.Object", pose_bone: "bpy.types.PoseBone") -> Optional["bpy.types.KinematicConstraint"]:
    """Get the IK constraint for the given pose bone.
    
    If the bone is an IK target/pole, finds the constraint that uses it.
    """
    # Check if this bone has an IK constraint directly
    for constraint in pose_bone.constraints:
        if constraint.type == "IK":
            return constraint
    
    # If this is an IK target or pole bone, find the bone that uses it
    bone_name = pose_bone.name
    if bone_name.endswith("-IKTarget") or bone_name.endswith("-IKPole"):
        for other_bone in ao.pose.bones:
            for constraint in other_bone.constraints:
                if constraint.type == "IK":
                    if constraint.subtarget == bone_name or constraint.pole_subtarget == bone_name:
                        return constraint
    
    return None


def update_pole_axis(
    ao: "bpy.types.Object",
    pose_bone: "bpy.types.PoseBone",
    target_axis: Vector
) -> None:
    """Update the pole bone position to change the IK bend direction.
    
    This repositions the pole bone to face the specified axis direction,
    which changes which way the IK chain bends. The pole is positioned
    perpendicular to the chain, offset in the target axis direction.
    """
    # Find the IK constraint
    ik_constraint = get_ik_constraint(ao, pose_bone)
    if not ik_constraint:
        return
    
    # Get the pole bone name
    pole_bone_name = ik_constraint.pole_subtarget
    if not pole_bone_name:
        return
    
    # Get the bone that has the IK constraint (the chain end)
    constrained_bone = None
    for bone in ao.pose.bones:
        for constraint in bone.constraints:
            if constraint == ik_constraint:
                constrained_bone = bone
                break
        if constrained_bone:
            break
    
    if not constrained_bone:
        return
    
    # Gather chain bones
    chain_count = ik_constraint.chain_count
    chain_bones = [constrained_bone]
    current = constrained_bone.parent
    while current and len(chain_bones) < chain_count:
        chain_bones.append(current)
        current = current.parent
    
    if len(chain_bones) < 2:
        return
    
    # Calculate chain geometry
    chain_end = chain_bones[0].tail  # End of chain (e.g., ankle)
    chain_start = chain_bones[-1].head  # Start of chain (e.g., hip)
    chain_vector = chain_end - chain_start
    chain_length = chain_vector.length
    
    # Find the middle joint (elbow/knee) - this is where the bend happens
    if len(chain_bones) >= 2:
        middle_joint = chain_bones[-1].tail  # The joint between upper and lower bones
    else:
        middle_joint = (chain_start + chain_end) / 2
    
    # Calculate pole position: offset from middle joint in the target axis direction
    # Use a distance proportional to the chain length for visibility
    pole_distance = chain_length * 0.5
    new_pole_head = middle_joint + target_axis.normalized() * pole_distance
    
    # Switch to edit mode to move the pole bone
    bpy.context.view_layer.objects.active = ao
    current_mode = ao.mode
    bpy.ops.object.mode_set(mode="EDIT")
    
    amt = ao.data
    pole_edit_bone = amt.edit_bones.get(pole_bone_name)
    if pole_edit_bone:
        # Calculate tail position (small offset from head in the axis direction)
        pole_tail_offset = target_axis.normalized() * 0.3
        pole_edit_bone.head = new_pole_head
        pole_edit_bone.tail = new_pole_head + pole_tail_offset
    
    bpy.ops.object.mode_set(mode="POSE")
    
    # Adjust the pole angle to match the new direction
    # This helps the IK solver understand the intended bend direction
    ik_constraint.pole_angle = _calculate_pole_angle(
        chain_start, middle_joint, chain_end, new_pole_head
    )


def _calculate_pole_angle(
    chain_start: Vector,
    middle_joint: Vector, 
    chain_end: Vector,
    pole_pos: Vector
) -> float:
    """Calculate the optimal pole angle for the given chain and pole position.
    
    This ensures the IK chain bends toward the pole correctly.
    """
    # Vector from start to end (the "straight" chain direction)
    chain_dir = (chain_end - chain_start).normalized()
    
    # Vector from middle joint to pole
    to_pole = (pole_pos - middle_joint).normalized()
    
    # Project to_pole onto the plane perpendicular to the chain
    # This gives us the "bend direction"
    to_pole_projected = to_pole - chain_dir * to_pole.dot(chain_dir)
    if to_pole_projected.length < 0.0001:
        return -math.pi * 0.5  # Default angle
    to_pole_projected.normalize()
    
    # Calculate the current bend direction from the chain geometry
    upper_bone_dir = (middle_joint - chain_start).normalized()
    current_bend = upper_bone_dir - chain_dir * upper_bone_dir.dot(chain_dir)
    if current_bend.length < 0.0001:
        return -math.pi * 0.5
    current_bend.normalize()
    
    # Calculate angle between current bend and desired pole direction
    dot = current_bend.dot(to_pole_projected)
    dot = max(-1.0, min(1.0, dot))  # Clamp for numerical stability
    angle = math.acos(dot)
    
    # Determine sign using cross product
    cross = current_bend.cross(to_pole_projected)
    if cross.dot(chain_dir) < 0:
        angle = -angle
    
    return angle - math.pi * 0.5


def remove_ik_config(ao: "bpy.types.Object", tail_bone: "bpy.types.PoseBone") -> None:
    """remove all ik constraints and utility bones for the given chain tail.

    this function removes the ik constraint from the tail bone and deletes any
    temporary target/pole bones that were previously created.
    Also removes any stretch bones, drivers, copy constraints, and IK-FK switch that were set up.
    """
    # First, gather chain bones and remove any stretch drivers
    for constraint in [c for c in tail_bone.constraints if c.type == "IK"]:
        chain_count = constraint.chain_count
        chain_bones = [tail_bone]
        current = tail_bone.parent
        while current and len(chain_bones) < chain_count:
            chain_bones.append(current)
            current = current.parent
        
        # Remove stretch drivers from chain bones
        for bone in chain_bones:
            pose_bone = ao.pose.bones.get(bone.name)
            if pose_bone:
                # Remove Y scale driver
                try:
                    pose_bone.driver_remove("scale", 1)
                except:
                    pass
        
        # Remove IK constraint influence driver (from IK-FK switch)
        try:
            constraint.driver_remove("influence")
        except:
            pass
        
        # Remove Copy Rotation/Location constraints and their drivers from child bones (foot/hand)
        if tail_bone.children:
            child_bone = tail_bone.children[0]
            constraints_to_remove = [
                c for c in child_bone.constraints 
                if c.name in ("IK_CopyRotation", "IK_CopyLocation") or
                   (c.type in ("COPY_ROTATION", "COPY_LOCATION") and 
                    c.subtarget and c.subtarget.endswith("-IKTarget"))
            ]
            for c in constraints_to_remove:
                # Remove driver before removing constraint
                try:
                    c.driver_remove("influence")
                except:
                    pass
                child_bone.constraints.remove(c)
            
            # Unhide the child bone if it was hidden by IK setup
            if constraints_to_remove:
                child_data_bone = ao.data.bones.get(child_bone.name)
                if child_data_bone:
                    child_pose_bone = ao.pose.bones.get(child_bone.name)
                    if child_pose_bone:
                        child_pose_bone.hide = False
    
    to_clear = []
    ik_target_names = []
    ik_pole_names = []
    for constraint in [c for c in tail_bone.constraints if c.type == "IK"]:
        if constraint.target and constraint.subtarget:
            to_clear.append((constraint.target, constraint.subtarget))
            ik_target_names.append(constraint.subtarget)
        if constraint.pole_target and constraint.pole_subtarget:
            to_clear.append((constraint.pole_target, constraint.pole_subtarget))
            ik_pole_names.append(constraint.pole_subtarget)
        
        # Also check for stretch bone
        ik_target_name = constraint.subtarget
        if ik_target_name:
            stretch_bone_name = ik_target_name.replace("-IKTarget", "-IKStretch")
            if ao.pose.bones.get(stretch_bone_name):
                to_clear.append((ao, stretch_bone_name))

        tail_bone.constraints.remove(constraint)
    
    # Remove hide drivers from IK target and pole bones (from IK-FK switch)
    for name in ik_target_names + ik_pole_names:
        bone = ao.data.bones.get(name)
        if bone:
            try:
                bone.driver_remove("hide")
            except:
                pass

    # ensure we're operating on the right object
    bpy.context.view_layer.objects.active = ao
    bpy.ops.object.mode_set(mode="EDIT")

    for util_bone in to_clear:
        edit_bone = util_bone[0].data.edit_bones.get(util_bone[1])
        if edit_bone:
            util_bone[0].data.edit_bones.remove(edit_bone)

    bpy.ops.object.mode_set(mode="POSE")


def setup_ik_stretch(
    ao: "bpy.types.Object",
    chain_bones: list,
    ik_target_name: str,
    stretch_bone_name: str,
    max_stretch: float = 1.05,
) -> None:
    """Set up stretch drivers for IK chain to prevent knee/elbow popping.
    
    Uses a dedicated stretch bone that spans from chain root to IK target.
    The stretch bone's Y scale is used to drive the chain bones' scale.
    
    Args:
        ao: The armature object.
        chain_bones: List of pose bones in the IK chain (from tail to root).
        ik_target_name: Name of the IK target bone.
        stretch_bone_name: Name of the stretch measurement bone.
        max_stretch: Maximum stretch factor (1.05 = 5% stretch).
    """
    if len(chain_bones) < 2:
        return
    
    # Calculate the total rest length of the chain
    total_rest_length = 0
    for bone in chain_bones:
        total_rest_length += bone.bone.length
    
    # Get the stretch bone
    stretch_bone = ao.pose.bones.get(stretch_bone_name)
    if not stretch_bone:
        return
    
    # Add Stretch To constraint to the stretch bone so it always points to IK target
    stretch_constraint = stretch_bone.constraints.new(type='STRETCH_TO')
    stretch_constraint.target = ao
    stretch_constraint.subtarget = ik_target_name
    # rest_length = 0 means use the bone's actual rest length
    stretch_constraint.rest_length = 0
    stretch_constraint.bulge = 0  # No volume preservation
    stretch_constraint.keep_axis = 'PLANE_X'
    stretch_constraint.volume = 'NO_VOLUME'  # Don't scale X/Z, only Y
    
    # For each bone in the chain, add a driver that reads the stretch bone's scale
    for pose_bone in chain_bones:
        # Add driver to scale Y (bone length axis)
        try:
            pose_bone.driver_remove("scale", 1)
        except:
            pass
        
        driver = pose_bone.driver_add("scale", 1)
        if not driver:
            continue
            
        fcurve = driver
        drv = fcurve.driver
        drv.type = 'SCRIPTED'
        
        # Variable: Y scale of the stretch bone
        var_scale = drv.variables.new()
        var_scale.name = "s"
        var_scale.type = 'TRANSFORMS'
        var_scale.targets[0].id = ao
        var_scale.targets[0].bone_target = stretch_bone_name
        var_scale.targets[0].transform_type = 'SCALE_Y'
        var_scale.targets[0].transform_space = 'LOCAL_SPACE'
        
        # Smooth IK stretch formula with no hard threshold:
        # Uses a smooth blend that:
        # - Returns ~1.0 when s < 1.0 (chain not fully extended)
        # - Smoothly ramps up when s > 1.0 (chain over-extended)
        # - Caps at max_stretch
        #
        # Formula: 1 + (max-1) * smoothstep(s)
        # where smoothstep = max(0, s-1)^2 / (softness + max(0, s-1)^2)
        # This is C1 continuous (smooth derivative) at all points
        max_s = max_stretch
        soft = 0.02  # Controls how quickly it ramps up (lower = sharper)
        
        # Expression: smooth quadratic blend
        # x = max(0, s - 1)  -- how far past full extension
        # blend = x*x / (soft + x*x)  -- smooth 0->1 as x increases
        # result = 1 + (max-1) * blend
        drv.expression = (
            f"1.0 + {max_s - 1:.4f} * (max(0, s - 1)**2) / "
            f"({soft} + max(0, s - 1)**2)"
        )


def setup_ik_fk_switch(
    ao: "bpy.types.Object",
    ik_target_name: str,
    ik_pole_name: Optional[str],
    constrained_bone_name: str,
    copy_rot_bone_name: Optional[str] = None,
) -> None:
    """Set up an IK-FK switch with a custom property and drivers.
    
    Creates an 'IK_FK' custom property on the IK target bone that controls:
    - IK constraint influence (1 = IK, 0 = FK)
    - Copy Rotation/Location constraint influences
    - IK target and pole bone visibility
    
    Args:
        ao: The armature object.
        ik_target_name: Name of the IK target bone.
        ik_pole_name: Name of the IK pole bone (optional).
        constrained_bone_name: Name of the bone with the IK constraint.
        copy_rot_bone_name: Name of the bone with Copy Rotation/Location constraints (optional).
    """
    ik_target_pose = ao.pose.bones.get(ik_target_name)
    if not ik_target_pose:
        return
    
    # Add custom property for IK-FK switch
    # 1.0 = Full IK, 0.0 = Full FK
    ik_target_pose["IK_FK"] = 1.0
    
    # Set up property with min/max and description
    id_props = ik_target_pose.id_properties_ui("IK_FK")
    id_props.update(min=0.0, max=1.0, soft_min=0.0, soft_max=1.0, 
                    description="IK-FK Blend (1=IK, 0=FK)")
    
    # Get the bone with IK constraint
    constrained_pose_bone = ao.pose.bones.get(constrained_bone_name)
    if not constrained_pose_bone:
        return
    
    # Find the IK constraint and add driver to its influence
    for constraint in constrained_pose_bone.constraints:
        if constraint.type == "IK":
            _add_ikfk_driver(ao, constraint, "influence", ik_target_name)
            break
    
    # Add drivers to Copy Rotation/Location constraints if they exist
    if copy_rot_bone_name:
        copy_rot_pose_bone = ao.pose.bones.get(copy_rot_bone_name)
        if copy_rot_pose_bone:
            for constraint in copy_rot_pose_bone.constraints:
                if constraint.name in ("IK_CopyRotation", "IK_CopyLocation"):
                    _add_ikfk_driver(ao, constraint, "influence", ik_target_name)
    
    # Add driver to hide IK target bone when in FK mode
    ik_target_bone = ao.data.bones.get(ik_target_name)
    if ik_target_bone:
        _add_ikfk_hide_driver(ao, ik_target_bone, ik_target_name)
    
    # Add driver to hide pole bone when in FK mode
    if ik_pole_name:
        ik_pole_bone = ao.data.bones.get(ik_pole_name)
        if ik_pole_bone:
            _add_ikfk_hide_driver(ao, ik_pole_bone, ik_target_name)


def _add_ikfk_driver(
    ao: "bpy.types.Object",
    constraint: "bpy.types.Constraint",
    prop_name: str,
    ik_target_name: str,
) -> None:
    """Add a driver to a constraint property that reads the IK_FK custom property."""
    try:
        constraint.driver_remove(prop_name)
    except:
        pass
    
    driver = constraint.driver_add(prop_name)
    if not driver:
        return
    
    drv = driver.driver
    drv.type = 'AVERAGE'  # Simple copy of the value
    
    var = drv.variables.new()
    var.name = "ikfk"
    var.type = 'SINGLE_PROP'
    var.targets[0].id = ao
    var.targets[0].data_path = f'pose.bones["{ik_target_name}"]["IK_FK"]'


def _add_ikfk_hide_driver(
    ao: "bpy.types.Object",
    bone: "bpy.types.Bone",
    ik_target_name: str,
) -> None:
    """Add a driver to hide a bone when IK_FK < 0.5."""
    try:
        bone.driver_remove("hide")
    except:
        pass
    
    driver = bone.driver_add("hide")
    if not driver:
        return
    
    drv = driver.driver
    drv.type = 'SCRIPTED'
    
    var = drv.variables.new()
    var.name = "ikfk"
    var.type = 'SINGLE_PROP'
    var.targets[0].id = ao
    var.targets[0].data_path = f'pose.bones["{ik_target_name}"]["IK_FK"]'
    
    # Hide when IK_FK < 0.5 (FK mode)
    drv.expression = "ikfk < 0.5"


def create_ik_config(
    ao: "bpy.types.Object",
    tail_bone: "bpy.types.PoseBone",
    chain_count: int,
    create_pose_bone: bool,
    lock_tail: bool,
    copy_rotation: bool = False,
    enable_stretch: bool = False,
    max_stretch: float = 1.05,
    enable_ik_fk_switch: bool = False,
) -> Tuple[str, Optional[str]]:
    """create ik target (and optional pole) and apply an ik constraint.

    returns (ik_target_bone_name, ik_pole_bone_name_or_none).
    
    Args:
        ao: The armature object.
        tail_bone: The bone at the end of the IK chain.
        chain_count: Number of bones in the chain.
        create_pose_bone: Whether to create a pole target bone.
        lock_tail: Whether to lock the tail bone orientation.
        copy_rotation: Whether to add a Copy Rotation constraint to copy IK target rotation.
        enable_stretch: Whether to add stretch drivers to prevent knee/elbow popping.
        max_stretch: Maximum stretch factor when enable_stretch is True (1.05 = 5% stretch).
        enable_ik_fk_switch: Whether to add an IK-FK switch property with drivers.
    """
    # sanitize inputs
    chain_count = max(1, int(chain_count))

    # ensure correct active object
    bpy.context.view_layer.objects.active = ao
    bpy.ops.object.mode_set(mode="EDIT")

    amt = ao.data
    ik_target_src = tail_bone if not lock_tail else (tail_bone.parent or tail_bone)

    ik_target_bone_name = ik_target_src.name
    ik_name = f"{ik_target_bone_name}-IKTarget"
    ik_name_pole = f"{ik_target_bone_name}-IKPole"
    ik_name_stretch = f"{ik_target_bone_name}-IKStretch"

    # Gather the chain bones first (we need this for stretch bone creation)
    chain_bones_edit = [amt.edit_bones.get(tail_bone.name)]
    current_edit = chain_bones_edit[0].parent if chain_bones_edit[0] else None
    while current_edit and len(chain_bones_edit) < chain_count:
        chain_bones_edit.append(current_edit)
        current_edit = current_edit.parent

    # Check if we should create the IK control as a duplicate of the child bone (e.g., foot)
    child_bone_edit = None
    if copy_rotation:
        # Find the child bone in edit mode to duplicate its shape
        tail_edit_bone = amt.edit_bones.get(tail_bone.name)
        if tail_edit_bone and tail_edit_bone.children:
            child_bone_edit = tail_edit_bone.children[0]

    # Create target bone - either as a dupe of child bone or default control shape
    ik_bone = amt.edit_bones.new(ik_name)
    if child_bone_edit:
        # Duplicate the child bone's position and orientation (e.g., foot bone)
        ik_bone.head = child_bone_edit.head.copy()
        ik_bone.tail = child_bone_edit.tail.copy()
        ik_bone.roll = child_bone_edit.roll
        ik_bone.bbone_x = child_bone_edit.bbone_x * 3.0
        ik_bone.bbone_z = child_bone_edit.bbone_z * 3.0
    else:
        # Default: create target bone roughly offset in local z-
        ik_bone.head = ik_target_src.tail
        ik_bone.tail = (
            Matrix.Translation(ik_bone.head) @ ik_target_src.matrix.to_3x3().to_4x4()
        ) @ Vector((0, 0, -0.5))
        ik_bone.bbone_x *= 1.5
        ik_bone.bbone_z *= 1.5

    ik_pole_name: Optional[str] = None
    ik_stretch_name: Optional[str] = None
    
    if create_pose_bone:
        # gather the actual chain bones up to chain_count
        chain_bones = [tail_bone]
        current = tail_bone.parent
        while current and len(chain_bones) < chain_count:
            chain_bones.append(current)
            current = current.parent

        effective_depth = len(chain_bones)
        pos_low = chain_bones[0].tail
        pos_high = chain_bones[-1].head if effective_depth > 0 else chain_bones[0].head
        dist = (pos_low - pos_high).length

        basal = chain_bones[-1] if effective_depth > 0 else tail_bone
        basal_mat = basal.bone.matrix_local

        ik_pole = amt.edit_bones.new(ik_name_pole)
        pole_offset = Vector((0, 0, -0.25 * dist))
        ik_pole.head = basal_mat @ pole_offset
        ik_pole.tail = basal_mat @ (pole_offset + Vector((0, 0, -0.3)))
        ik_pole.bbone_x *= 0.5
        ik_pole.bbone_z *= 0.5
        ik_pole_name = ik_pole.name

        # pre-bend: add small offset to joint bones so IK solver knows which way to bend
        # when bones are perfectly aligned, the solver has no preferred direction
        prebend_offset = 0.001  # small offset to hint bending direction
        for i, bone in enumerate(chain_bones[:-1]):  # skip the root bone of the chain
            edit_bone = amt.edit_bones.get(bone.name)
            if edit_bone and edit_bone.parent:
                # offset the head slightly in the local Y direction (forward bend)
                local_y = (edit_bone.tail - edit_bone.head).normalized()
                # use the bone's local Z axis for the bend direction (flipped)
                local_z = edit_bone.z_axis.normalized()
                edit_bone.head = edit_bone.head - local_z * prebend_offset

    # Create stretch bone if stretch is enabled
    # This bone spans from chain root to chain end and uses Stretch To constraint
    if enable_stretch and len(chain_bones_edit) >= 2:
        chain_root = chain_bones_edit[-1]  # Root of chain (e.g., hip)
        chain_end = chain_bones_edit[0]    # End of chain (e.g., ankle)
        
        ik_stretch = amt.edit_bones.new(ik_name_stretch)
        ik_stretch.head = chain_root.head.copy()
        ik_stretch.tail = chain_end.tail.copy()
        # Parent to chain root so it moves with the rig
        ik_stretch.parent = chain_root
        ik_stretch.use_connect = False
        # Make it thin so it doesn't clutter the view
        ik_stretch.bbone_x = 0.01
        ik_stretch.bbone_z = 0.01
        # Don't deform - this is just a measurement bone
        ik_stretch.use_deform = False
        ik_stretch_name = ik_stretch.name

    bpy.ops.object.mode_set(mode="POSE")

    # Set up bone colors for IK controls (makes them easier to identify)
    ik_target_pose = ao.pose.bones.get(ik_name)
    if ik_target_pose:
        # Use a distinct color for IK target (yellow/gold)
        ik_target_pose.color.palette = 'THEME06'
    
    if ik_pole_name:
        ik_pole_pose = ao.pose.bones.get(ik_pole_name)
        if ik_pole_pose:
            # Use a different color for pole (cyan)
            ik_pole_pose.color.palette = 'THEME04'

    # Hide stretch bone in pose mode (must be done via armature.bones, not pose.bones)
    if ik_stretch_name:
        stretch_pose_bone = ao.pose.bones.get(ik_stretch_name)
        if stretch_pose_bone:
            stretch_pose_bone.hide = True

    pose_bone = ao.pose.bones.get(ik_target_bone_name)
    if not pose_bone:
        # fallback: no pose bone found
        return (ik_name, ik_pole_name)

    constraint = pose_bone.constraints.new(type="IK")
    constraint.target = ao
    constraint.subtarget = ik_name
    if create_pose_bone and ik_pole_name:
        constraint.pole_target = ao
        constraint.pole_subtarget = ik_pole_name
        constraint.pole_angle = math.pi * -0.5
    constraint.chain_count = chain_count

    # Add copy rotation constraint if requested (useful for foot/hand controls)
    copy_rot_bone_name = None
    if copy_rotation:
        # Get the child of the last bone in the chain (e.g., foot bone for leg IK)
        tail_pose_bone = ao.pose.bones.get(tail_bone.name)
        if tail_pose_bone and tail_pose_bone.children:
            # Apply to the first child of the tail bone
            copy_rot_bone = tail_pose_bone.children[0]
            copy_rot_bone_name = copy_rot_bone.name
            
            # Copy Rotation - foot matches IK control orientation
            copy_rot = copy_rot_bone.constraints.new(type="COPY_ROTATION")
            copy_rot.name = "IK_CopyRotation"
            copy_rot.target = ao
            copy_rot.subtarget = ik_name
            copy_rot.mix_mode = 'REPLACE'
            copy_rot.owner_space = 'WORLD'
            copy_rot.target_space = 'WORLD'
            
            # Copy Location - foot stays attached to IK control
            copy_loc = copy_rot_bone.constraints.new(type="COPY_LOCATION")
            copy_loc.name = "IK_CopyLocation"
            copy_loc.target = ao
            copy_loc.subtarget = ik_name
            copy_loc.head_tail = 0  # Use head of IK target
            copy_loc.owner_space = 'WORLD'
            copy_loc.target_space = 'WORLD'
            
            # Hide the controlled bone since the IK target now represents it
            controlled_pose_bone = ao.pose.bones.get(copy_rot_bone_name)
            if controlled_pose_bone:
                controlled_pose_bone.hide = True

    # Set up stretch drivers if requested (prevents knee/elbow popping)
    if enable_stretch and ik_stretch_name:
        # Gather pose bones for the chain
        chain_pose_bones = [ao.pose.bones.get(tail_bone.name)]
        current = tail_bone.parent
        count = 1
        while current and count < chain_count:
            pb = ao.pose.bones.get(current.name)
            if pb:
                chain_pose_bones.append(pb)
            current = current.parent
            count += 1
        
        setup_ik_stretch(ao, chain_pose_bones, ik_name, ik_stretch_name, max_stretch=max_stretch)

    # Set up IK-FK switch if requested
    if enable_ik_fk_switch:
        setup_ik_fk_switch(
            ao,
            ik_name,
            ik_pole_name,
            ik_target_bone_name,
            copy_rot_bone_name,
        )

    return (ik_name, ik_pole_name)
