"""Fit Roblox pose segments to evaluated constraints, rather than input curves."""

import heapq

import numpy as np
from mathutils import Matrix
from ..core.constants import identity_cf
from .ir import KeyframePayload, PoseEntry, keyframes_equivalent


def refine_visual_bake(collected, names, sample, frame_start, frame_end, fps, diagnostics=None,
                       seed_samples=None, keyframes=None):
    """Preserve evaluated holds and subdivide nonlinear constraint motion.

    The callback samples Blender at a fractional frame. A linear control can
    produce nonlinear IK motion, and a driver can preserve a constant hold.
    Neither can be inferred reliably from the output bone's own keyframes.
    Existing keys for other bones and face controls are left intact.
    """
    if not names or frame_end <= frame_start:
        return collected
    names = sorted(names & {name for key in collected for name in key.poses})
    if not names:
        return collected
    # Bound total transform work as well as sample count. A 200-bone IK chain
    # is substantially more expensive than an ordinary avatar limb.
    sample_budget = max(16, min(256, 8192 // max(1, len(names))))
    rows = {round(frame_start + key.time * fps, 6): key for key in collected}
    # Refit visual channels over source keys and whole-frame intervals, not
    # over every sample already inserted by the input-curve planner. Retain
    # other channels at their original times without subdividing them again.
    frames = sorted(set(keyframes if keyframes is not None else rows) |
                    set(range(frame_start, frame_end + 1)))
    frames = [frame for frame in frames if frame_start <= frame <= frame_end]
    for row in rows.values():
        for name in names:
            row.poses.pop(name, None)
    seeds = seed_samples if seed_samples is not None else {}
    cache = {}
    limited = 0

    def evaluate(frame):
        if frame not in cache:
            poses = seeds[frame] if frame in seeds else sample(frame)
            components = np.asarray([poses.get(name, identity_cf) for name in names], dtype=np.float64)
            rotations = np.asarray([
                tuple(Matrix((cf[3:6], cf[6:9], cf[9:12])).to_quaternion())
                for cf in components
            ], dtype=np.float64)
            rotations /= np.linalg.norm(rotations, axis=1, keepdims=True)
            cache[frame] = components, rotations
        return cache[frame]

    def emit(frame, pose, styles):
        row = rows.setdefault(frame, KeyframePayload((frame - frame_start) / fps, {}))
        for index, name in enumerate(names):
            row.poses[name] = PoseEntry(pose[0][index].tolist(), styles.get(name, "Linear"), "Out")

    def assess(a, b):
        left, left_q = evaluate(a)
        right, right_q = evaluate(b)
        # Dyadic samples are shared by child intervals and by the initial
        # bake. Keep an irrational probe to avoid periodic aliasing, plus a
        # near-end probe to distinguish holds from late continuous motion.
        fractions = np.asarray((0.25, 0.5, 0.75, 0.2113248654, 0.999))
        probes = [evaluate(a + (b - a) * float(fraction)) for fraction in fractions]
        actual = np.stack([pose[0] for pose in probes])
        actual_q = np.stack([pose[1] for pose in probes])
        change = np.max(np.abs(right - left), axis=1)
        hold_tolerance = np.maximum(1e-8, np.minimum(2e-6, change * 1e-5))
        holds = np.all(np.max(np.abs(actual - left), axis=2) <= hold_tolerance, axis=0)
        styles = {names[index]: "Constant" for index in np.flatnonzero(holds)}

        # Batch the per-bone chord checks in float64. Python loops and float32
        # quaternion dot noise used to dominate large-rig refinement.
        alpha = fractions[:, None, None]
        predicted_position = left[None, :, :3] + alpha * (right - left)[None, :, :3]
        position_error = np.linalg.norm(actual[:, :, :3] - predicted_position, axis=2)
        dot = np.sum(left_q * right_q, axis=1)
        right_q = right_q * np.where(dot < 0, -1, 1)[:, None]
        theta = np.arccos(np.clip(np.abs(dot), 0, 1))
        sine = np.sin(theta)
        denominator = np.where(sine > 1e-8, sine, 1)
        first = np.sin((1 - fractions[:, None]) * theta) / denominator
        second = np.sin(fractions[:, None] * theta) / denominator
        predicted_q = first[:, :, None] * left_q + second[:, :, None] * right_q
        linear = left_q + alpha * (right_q - left_q)
        predicted_q = np.where((sine <= 1e-8)[None, :, None], linear, predicted_q)
        predicted_q /= np.linalg.norm(predicted_q, axis=2, keepdims=True)
        rotation_error = 2 * np.arccos(np.clip(np.abs(np.sum(actual_q * predicted_q, axis=2)), 0, 1))
        errors = np.maximum(position_error / 0.0005, rotation_error / 0.001)
        errors[:, holds] = 0
        return float(np.max(errors)), styles

    for a, b in zip(frames, frames[1:]):
        error, styles = assess(a, b)
        pending = [(-error, a, b, 0, styles)]
        leaves = []
        # Refine the worst interval first. The evaluation cap prevents noisy
        # or unstable IK solvers from expanding into millions of samples;
        # it must not spend the entire budget on the left half of a frame.
        while pending:
            negative_error, start, end, depth, styles = heapq.heappop(pending)
            if negative_error < -1 and depth < 12 and end - start > 0.0001 and len(cache) < sample_budget:
                middle = round((start + end) * 0.5, 6)
                for lo, hi in ((start, middle), (middle, end)):
                    error, new_styles = assess(lo, hi)
                    heapq.heappush(pending, (-error, lo, hi, depth + 1, new_styles))
            else:
                limited += negative_error < -1
                leaves.append((start, styles))
        for frame, styles in sorted(leaves):
            emit(frame, evaluate(frame), styles)
        # Bound memory to one base interval, even for long exports.
        last = cache[b]
        cache.clear()
        cache[b] = last
    last_styles = {}
    for frame in sorted(rows):
        if frame < frames[-1]:
            last_styles.update({name: pose.style for name, pose in rows[frame].poses.items() if name in names})
    emit(frames[-1], evaluate(frames[-1]), last_styles)
    result = []
    for frame in sorted(rows):
        row = rows[frame]
        if not row.poses and not row.face:
            continue
        if (result and frame != frames[-1]
                and all(pose.style == "Constant" for pose in result[-1].poses.values())
                and keyframes_equivalent(result[-1], row, tol=2e-6)):
            continue
        result.append(row)
    if diagnostics is not None:
        diagnostics["limited_segments"] = limited
    return result
