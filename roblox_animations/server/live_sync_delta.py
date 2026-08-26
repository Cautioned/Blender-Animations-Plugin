"""Revisioned, deterministic animation keyframe patches for live sync."""

from __future__ import annotations

import hashlib
import json
from typing import Any


_STRUCTURAL_KEYS = (
    "t",
    "is_deform_bone_rig",
    "is_deform_rig",
    "bone_hierarchy",
    "export_info",
)


def animation_revision(animation: dict[str, Any]) -> str:
    """Return a stable revision for an already serialized animation payload."""
    encoded = json.dumps(animation, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _time_key(keyframe: dict[str, Any]) -> str:
    return format(float(keyframe["t"]), ".9g")


def create_delta(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any] | None:
    """Create a replace/remove-keyframe patch, or ``None`` for a full sync.

    Whole keyframes are the smallest safe initial unit: they preserve pose,
    easing, deform markers, and face-control state without a lossy translation.
    """
    if any(previous.get(key) != current.get(key) for key in _STRUCTURAL_KEYS):
        return None
    old_frames = {_time_key(frame): frame for frame in previous.get("kfs", [])}
    new_frames = {_time_key(frame): frame for frame in current.get("kfs", [])}
    upsert = [frame for key, frame in new_frames.items() if old_frames.get(key) != frame]
    remove = [float(old_frames[key]["t"]) for key in old_frames.keys() - new_frames.keys()]
    if not upsert and not remove:
        return {"type": "animation_delta", "base_hash": animation_revision(
            previous), "hash": animation_revision(current), "upsert": [], "remove": []}
    return {
        "type": "animation_delta",
        "base_hash": animation_revision(previous),
        "hash": animation_revision(current),
        "upsert": sorted(upsert, key=lambda frame: float(frame["t"])),
        "remove": sorted(remove),
    }


def apply_delta(animation: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any] | None:
    """Apply a validated patch to a serialized payload; ``None`` means resync."""
    if animation_revision(animation) != delta.get("base_hash"):
        return None
    frames = {_time_key(frame): frame for frame in animation.get("kfs", [])}
    for time_value in delta.get("remove", []):
        frames.pop(format(float(time_value), ".9g"), None)
    for frame in delta.get("upsert", []):
        if not isinstance(frame, dict) or "t" not in frame:
            return None
        frames[_time_key(frame)] = frame
    patched = dict(animation)
    patched["kfs"] = sorted(frames.values(), key=lambda frame: float(frame["t"]))
    return patched if animation_revision(patched) == delta.get("hash") else None
