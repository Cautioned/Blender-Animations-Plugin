"""Fit Roblox pose segments to evaluated constraints, rather than input curves."""

import heapq
from math import ceil, log2
from bisect import bisect_left, bisect_right, insort

import numpy as np
from mathutils import Matrix
from ..core.constants import identity_cf
from .ir import KeyframePayload, PoseEntry, keyframes_equivalent


def bake_at_frame_rate(collected, names, sample, frame_start, frame_end, fps,
                       diagnostics=None, seed_samples=None, keyframes=None,
                       sample_subset=None):
    """Emit the sampled trajectory, preserving source times and evaluated holds.

    Each changing interval needs at most one extra scene evaluation to distinguish
    a hold followed by a jump from continuous motion. There is no recursive
    accuracy search. The planner adds subframe samples for short source segments.
    """
    names = sorted(names & {name for key in collected for name in key.poses})
    if not names or frame_end <= frame_start:
        return collected
    rows = {round(frame_start + key.time * fps, 6): key for key in collected}
    samples = seed_samples if seed_samples is not None else {}
    frames = sorted(frame for frame in set(samples) | set(keyframes or ()) |
                    {frame_start, frame_end} if frame_start <= frame <= frame_end)
    if not samples:
        frames = sorted(set(frames) | set(range(frame_start, frame_end + 1)))
    for row in rows.values():
        for name in names:
            row.poses.pop(name, None)
    evaluated = 0
    evaluated_bones = 0

    def fetch(frame, selected):
        nonlocal evaluated, evaluated_bones
        if frame in samples:
            return samples[frame]
        evaluated += 1
        evaluated_bones += len(selected)
        return sample_subset(frame, selected) if sample_subset is not None else sample(frame)

    def emit(frame, poses, styles):
        row = rows.setdefault(frame, KeyframePayload((frame - frame_start) / fps, {}))
        for index, name in enumerate(names):
            row.poses[name] = PoseEntry(poses[index].tolist(), styles[index], "Out")

    first = fetch(frames[0], set(names))
    left = np.asarray([first.get(name, identity_cf) for name in names])
    styles = ["Linear"] * len(names)
    for a, b in zip(frames, frames[1:]):
        poses = fetch(b, set(names))
        right = np.asarray([poses.get(name, identity_cf) for name in names])
        change = np.max(np.abs(right - left), axis=1)
        changed = np.flatnonzero(change > 2e-6)
        styles = ["Constant"] * len(names)
        if len(changed):
            selected = {names[index] for index in changed}
            probe = fetch(a + (b - a) * 0.999, selected)
            for index in changed:
                tolerance = max(1e-8, min(2e-6, float(change[index]) * 1e-5))
                held = np.max(np.abs(np.asarray(probe.get(names[index], identity_cf)) - left[index])) <= tolerance
                styles[index] = "Constant" if held else "Linear"
        emit(a, left, styles)
        left = right
    emit(frames[-1], left, styles)
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
        diagnostics.update(limited_segments=0, evaluated_samples=evaluated,
                           evaluated_bones=evaluated_bones)
    return result


def refine_visual_bake(collected, names, sample, frame_start, frame_end, fps, diagnostics=None,
                       seed_samples=None, keyframes=None, sample_subset=None):
    """Preserve evaluated holds and subdivide nonlinear constraint motion.

    The callback samples Blender at a fractional frame. A linear control can
    produce nonlinear IK motion, and a driver can preserve a constant hold.
    Neither can be inferred reliably from the output bone's own keyframes.
    Existing keys for other bones and face controls are left intact.
    Optional sample_subset(frame, names) avoids sampling accepted channels.
    """
    if not names or frame_end <= frame_start:
        return collected
    names = sorted(names & {name for key in collected for name in key.poses})
    if not names:
        return collected
    # Bound total transform work as well as sample count. A 200-bone IK chain
    # is substantially more expensive than an ordinary avatar limb.
    sample_budget = 256 if sample_subset is not None else max(16, min(256, 8192 // len(names)))
    rows = {round(frame_start + key.time * fps, 6): key for key in collected}
    # Fit the already sampled trajectory across source-key intervals. Whole
    # frames are validation samples, not mandatory fresh refinement roots.
    frames = set(keyframes if keyframes is not None else rows)
    frames.update((frame_start, frame_end))
    frames.update(range(frame_start + 8, frame_end, 8))
    if not seed_samples:
        frames.update(range(frame_start, frame_end + 1))
    frames = sorted(frame for frame in frames if frame_start <= frame <= frame_end)
    for row in rows.values():
        for name in names:
            row.poses.pop(name, None)
    seeds = seed_samples if seed_samples is not None else {}
    cache = {}
    coverage = {}
    cached_frames = []
    guard_frames = set()
    all_indices = np.arange(len(names))
    seed_frames = sorted(seeds)
    sampled = 0
    sampled_bones = 0
    limited = 0

    def evaluate(frame, active=None):
        nonlocal sampled, sampled_bones
        active = all_indices if active is None else active
        present = coverage.get(frame, set())
        missing = [int(index) for index in active if index not in present]
        if missing:
            if frame in seeds:
                poses = seeds[frame]
                missing = list(range(len(names)))
            elif sample_subset is None:
                poses = sample(frame)
                sampled += 1
                missing = list(range(len(names)))
                sampled_bones += len(missing)
            else:
                poses = sample_subset(frame, {names[index] for index in missing})
                sampled += 1
                sampled_bones += len(missing)
            components = np.asarray([poses.get(names[index], identity_cf) for index in missing], dtype=np.float64)
            rotations = np.asarray([
                tuple(Matrix((cf[3:6], cf[6:9], cf[9:12])).to_quaternion())
                for cf in components
            ], dtype=np.float64)
            rotations /= np.linalg.norm(rotations, axis=1, keepdims=True)
            if frame not in cache:
                insort(cached_frames, frame)
                cache[frame] = (np.empty((len(names), 12)), np.empty((len(names), 4)))
            cache[frame][0][missing] = components
            cache[frame][1][missing] = rotations
            present.update(missing)
            coverage[frame] = present
        return cache[frame]

    def emit(frame, pose, styles, indices=None):
        row = rows.setdefault(frame, KeyframePayload((frame - frame_start) / fps, {}))
        for index in range(len(names)) if indices is None else indices:
            name = names[index]
            row.poses[name] = PoseEntry(pose[0][index].tolist(), styles.get(name, "Linear"), "Out")

    def assess(a, b, active, root=False):
        left, left_q = (values[active] for values in evaluate(a, active))
        right, right_q = (values[active] for values in evaluate(b, active))
        # Reuse every known interior sample, including probes from ancestors.
        # Only fill gaps in temporal coverage. A nonuniform guard prevents
        # periodic curves from aliasing a dyadic/whole-frame sample grid.
        probes_at = set(seed_frames[bisect_right(seed_frames, a):bisect_left(seed_frames, b)])
        probes_at.update(cached_frames[bisect_right(cached_frames, a):bisect_left(cached_frames, b)])
        probes_at.add(round((a + b) * 0.5, 6))
        # Keep the original broad/late guards at the root, then reuse them
        # in descendants rather than repeating five probes at every split.
        if root:
            probes_at.update(a + (b - a) * fraction for fraction in (0.25, 0.75, 0.999))
        relative = [(frame - a) / (b - a) for frame in probes_at]
        if not any(0.1 <= fraction <= 0.45 for fraction in relative):
            guard = a + (b - a) * 0.2113248654
            probes_at.add(guard)
            guard_frames.add(guard)
        if not any(0.55 <= fraction <= 0.9 for fraction in relative):
            guard = a + (b - a) * 0.7886751346
            probes_at.add(guard)
            guard_frames.add(guard)
        if not any(a + (b - a) * 0.1 <= frame <= a + (b - a) * 0.9 for frame in guard_frames):
            guard = a + (b - a) * 0.2113248654
            probes_at.add(guard)
            guard_frames.add(guard)

        def read_probes():
            times = sorted(probes_at)
            fractions = np.asarray([(frame - a) / (b - a) for frame in times])
            poses = [tuple(values[active] for values in evaluate(frame, active)) for frame in times]
            return fractions, np.stack([pose[0] for pose in poses]), np.stack([pose[1] for pose in poses])

        fractions, actual, actual_q = read_probes()
        change = np.max(np.abs(right - left), axis=1)
        hold_tolerance = np.maximum(1e-8, np.minimum(2e-6, change * 1e-5))
        holds = np.all(np.max(np.abs(actual - left), axis=2) <= hold_tolerance, axis=0)
        # Only a possible hold ending in a jump needs a near-end test. Continuous
        # channels already have interior evidence; do not probe every tail.
        if np.any(holds & (change > hold_tolerance)):
            probes_at.add(a + (b - a) * 0.999)
            fractions, actual, actual_q = read_probes()
            holds = np.all(np.max(np.abs(actual - left), axis=2) <= hold_tolerance, axis=0)
        styles = {names[active[index]]: "Constant" for index in np.flatnonzero(holds)}

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
        all_errors = np.zeros(len(names))
        all_errors[active] = np.max(errors, axis=0)
        return all_errors, styles

    def emit_segment(start, end, styles, active):
        emit(start, evaluate(start, active), styles, active)
        # Retain the original whole-frame coverage without evaluating again.
        for frame in seed_frames[bisect_right(seed_frames, start):bisect_left(seed_frames, end)]:
            if abs(frame - round(frame)) < 1e-6:
                emit(frame, evaluate(frame, active), styles, active)

    for a, b in zip(frames, frames[1:]):
        sample_start = sampled
        bones_start = sampled_bones
        transform_budget = max(8192, 16 * len(names)) * max(1.0, b - a)
        interval_budget = sample_budget * max(1.0, b - a)
        max_depth = 12 + max(0, ceil(log2(b - a)))
        errors, styles = assess(a, b, all_indices, root=True)
        pending = [(-float(np.max(errors)), a, b, 0, styles, errors, np.arange(len(names)))]
        # Refine the worst interval first. The evaluation cap prevents noisy
        # or unstable IK solvers from expanding into millions of samples;
        # it must not spend the entire budget on the left half of a frame.
        while pending:
            negative_error, start, end, depth, styles, errors, active = heapq.heappop(pending)
            if negative_error < -1 and depth < max_depth and end - start > 0.0001 and sampled - sample_start < interval_budget and sampled_bones - bones_start < transform_budget:
                # A curved limb must not insert keys into every other channel.
                # Once a bone passes the chord check it keeps this whole segment.
                accepted = active[errors[active] <= 1]
                if len(accepted):
                    emit_segment(start, end, styles, accepted)
                active = active[errors[active] > 1]
                middle = round((start + end) * 0.5, 6)
                for lo, hi in ((start, middle), (middle, end)):
                    new_errors, new_styles = assess(lo, hi, active)
                    heapq.heappush(pending, (-float(np.max(new_errors[active])), lo, hi,
                                            depth + 1, new_styles, new_errors, active))
            else:
                limited += negative_error < -1
                emit_segment(start, end, styles, active)
        # Bound memory to one base interval, even for long exports.
        last = cache[b]
        last_coverage = coverage[b]
        cache.clear()
        coverage.clear()
        guard_frames.clear()
        cached_frames[:] = [b]
        cache[b] = last
        coverage[b] = last_coverage
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
        diagnostics["evaluated_samples"] = sampled
        diagnostics["evaluated_bones"] = sampled_bones
    return result
