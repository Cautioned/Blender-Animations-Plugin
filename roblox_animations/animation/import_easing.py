"""Evaluate Pose easing in Roblox space before importing frame-rate samples.

Pose easing is not interchangeable with Blender F-curve easing. In particular,
legacy Cubic reverses In/Out, Bounce InOut uses bounce-in on both halves, and
Elastic uses fixed periods. Rotations use shortest-path spherical interpolation.
"""

import bisect
import math

from mathutils import Matrix, Quaternion, Vector

from ..core.utils import cf_to_mat, mat_to_cf


def _bounce_out(t):
    if t < 1 / 2.75:
        return 7.5625 * t * t
    if t < 2 / 2.75:
        t -= 1.5 / 2.75
        return 7.5625 * t * t + 0.75
    if t < 2.5 / 2.75:
        t -= 2.25 / 2.75
        return 7.5625 * t * t + 0.9375
    t -= 2.625 / 2.75
    return 7.5625 * t * t + 0.984375


def pose_easing_value(style, direction, t):
    """Fraction reached by a Roblox Pose segment, including overshoot."""
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    if style == "Constant":
        return float(direction == "In" or (direction == "InOut" and t >= 0.5))
    if style == "Linear":
        return t
    if style == "Cubic" and direction != "InOut":
        direction = "Out" if direction == "In" else "In"

    def ease_in(x):
        if style in {"Cubic", "CubicV2"}:
            return x ** 3
        if style == "Bounce":
            return 1 - _bounce_out(1 - x)
        if style == "Elastic":
            period = 0.45 if direction == "InOut" else 0.3
            return -(2 ** (10 * (x - 1))) * math.sin(
                (x - 1 - period / 4) * 2 * math.pi / period
            )
        # Retain support for older payloads with Tween-style easing names.
        if style in {"Quad", "Quart", "Quint"}:
            return x ** {"Quad": 2, "Quart": 4, "Quint": 5}[style]
        if style == "Sine":
            return 1 - math.cos(x * math.pi / 2)
        if style == "Expo":
            return 2 ** (10 * (x - 1))
        if style == "Circular":
            return 1 - math.sqrt(max(0, 1 - x * x))
        if style == "Back":
            overshoot = 1.70158 * (1.525 if direction == "InOut" else 1)
            return x * x * ((overshoot + 1) * x - overshoot)
        return x

    if direction == "In":
        return ease_in(t)
    if direction == "InOut":
        if t < 0.5:
            return 0.5 * ease_in(2 * t)
        if style == "Bounce":
            return 0.5 + 0.5 * ease_in(2 * t - 1)
        return 1 - 0.5 * ease_in(2 - 2 * t)
    return 1 - ease_in(1 - t)


def _decode_pose(pose):
    if isinstance(pose, dict):
        return (pose.get("components", []), pose.get("easingStyle", "Linear"),
                pose.get("easingDirection", "In"))
    if len(pose) == 3 and isinstance(pose[0], (list, tuple)):
        return pose
    return pose, "Linear", "In"


def _slerp(a, b, factor):
    # Quaternion.slerp is limited to [0, 1]; Elastic requires extrapolation.
    if a.dot(b) < 0:
        b = -b
    delta = a.conjugated() @ b
    delta.normalize()
    axis, angle = delta.to_axis_angle()
    return a @ Quaternion(axis, angle * factor)


def sample_import_keyframes(keyframes, fps):
    """Sample sparse per-bone tracks at scene frames and authored boundaries.

    All active tracks share sample times so parent transforms are available when
    children are converted. Fractional source keys and Constant InOut midpoints
    survive. An immediate Constant In jump uses a tiny positive frame offset to
    keep the exact starting pose without duplicate-time Blender keys.
    """
    tracks = {}
    times = set()
    for key in keyframes:
        frame = float(key["t"]) * fps
        if abs(frame - round(frame)) < 1e-6:
            frame = float(round(frame))
        times.add(frame)
        for name, pose in key["kf"].items():
            components, style, direction = _decode_pose(pose)
            if not components:
                continue
            matrix = cf_to_mat(components)
            tracks.setdefault(name, {})[frame] = (
                matrix.to_translation(), matrix.to_quaternion(), style, direction,
            )
    prepared = {}
    for name, track in tracks.items():
        frames = sorted(track)
        prepared[name] = (frames, [track[f] for f in frames])
        for start, end in zip(frames, frames[1:]):
            times.update(range(math.ceil(start), math.floor(end) + 1))
            _, _, style, direction = track[start]
            if style == "Constant" and direction == "InOut":
                times.add((start + end) / 2)
            elif style == "Constant" and direction == "In":
                # Keep the offset representable in Blender's float32 key times.
                times.add(start + min((end - start) / 4, max(5e-4, abs(start) * 2 ** -22)))
            elif style == "Bounce" and direction == "InOut":
                times.add((start + end) / 2)

    result = []
    for frame in sorted(times):
        poses = {}
        for name, (frames, values) in prepared.items():
            index = bisect.bisect_right(frames, frame) - 1
            if index < 0:
                continue
            location, rotation, style, direction = values[index]
            constant = index == len(frames) - 1 or style == "Constant"
            if index < len(frames) - 1:
                next_location, next_rotation, _, _ = values[index + 1]
                alpha = (frame - frames[index]) / (frames[index + 1] - frames[index])
                factor = pose_easing_value(style, direction, alpha)
                location = location.lerp(next_location, factor)
                rotation = _slerp(rotation, next_rotation, factor)
            matrix = Matrix.LocRotScale(location, rotation, Vector((1, 1, 1)))
            poses[name] = [mat_to_cf(matrix), "Constant" if constant else "Linear", "Out"]
        result.append({"t": frame / fps, "kf": poses})
    return result
