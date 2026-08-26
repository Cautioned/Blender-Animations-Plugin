"""
Typed intermediate representation for animation export.

The bake pipeline produces these records internally and converts them to
plain JSON-serializable payloads only in the final emit stage.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PoseEntry:
    """One bone pose in a keyframe: CFrame components plus easing metadata."""

    components: List[float]
    style: str
    direction: str

    def to_payload(self) -> List[Any]:
        return [list(self.components), self.style, self.direction]


@dataclass
class KeyframePayload:
    """One exported keyframe: time, bone poses, optional face-control state."""

    time: float
    poses: Dict[str, PoseEntry] = field(default_factory=dict)
    face: Optional[Dict[str, Dict[str, Any]]] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "t": self.time,
            "kf": {name: entry.to_payload() for name, entry in self.poses.items()},
        }
        if self.face:
            payload["fc"] = self.face
        return payload


def pose_entries_equivalent(
    prev: PoseEntry,
    new: PoseEntry,
    tol: float = 1e-6,
) -> bool:
    if prev.style != new.style or prev.direction != new.direction:
        return False
    if len(prev.components) != len(new.components):
        return False
    for prev_value, new_value in zip(prev.components, new.components):
        if abs(prev_value - new_value) > tol:
            return False
    return True


def keyframes_equivalent(
    prev: KeyframePayload,
    new: KeyframePayload,
    tol: float = 1e-6,
) -> bool:
    if prev.poses.keys() != new.poses.keys():
        return False
    for bone_name, prev_entry in prev.poses.items():
        new_entry = new.poses.get(bone_name)
        if new_entry is None or not pose_entries_equivalent(prev_entry, new_entry, tol):
            return False

    prev_face = prev.face or {}
    new_face = new.face or {}
    if prev_face.keys() != new_face.keys():
        return False
    for control_name, prev_values in prev_face.items():
        new_values = new_face.get(control_name)
        if new_values is None:
            return False
        if prev_values.get("easingStyle") != new_values.get("easingStyle"):
            return False
        if prev_values.get("easingDirection") != new_values.get("easingDirection"):
            return False
        if (
            abs(float(prev_values.get("value", 0.0)) - float(new_values.get("value", 0.0)))
            > tol
        ):
            return False
    return True
