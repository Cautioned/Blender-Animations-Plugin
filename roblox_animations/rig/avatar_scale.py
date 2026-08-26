"""HumanoidDescription limb scaling (R15).

This module implements the R15 limb-scale algorithm (SampleScales /
ComputeLimbScale). The algorithm interpolates three canonical body forms —
classic R15, Rthro Normal, and Rthro Slender — using the
HumanoidDescription's BodyType and Proportion sliders. It then multiplies
by the (Width, Height, Depth) base scale and the Head slider.

Each body part carries an ``AvatarPartScaleType`` marker ("Classic",
"Normal", or "Slender" — "Proportions" is stripped at parse time) that
selects which canonical form the part is currently authored in; the
computation re-bases it onto the target form.

bpy-free so it is headless-testable.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

Vec3 = Tuple[float, float, float]

# Scale R15 -> Rthro Normal
_R15_TO_NORMAL: Dict[str, Vec3] = {
    "Head": (0.942, 0.942, 0.942),
    "UpperTorso": (1.033, 1.310, 1.140),
    "LowerTorso": (1.033, 1.309, 1.140),
    "LeftUpperArm": (1.129, 1.342, 1.132),
    "LeftLowerArm": (1.129, 1.342, 1.132),
    "LeftHand": (1.066, 1.174, 1.231),
    "RightUpperArm": (1.129, 1.342, 1.132),
    "RightLowerArm": (1.129, 1.342, 1.132),
    "RightHand": (1.066, 1.174, 1.231),
    "LeftUpperLeg": (1.023, 1.506, 1.023),
    "LeftLowerLeg": (1.023, 1.506, 1.023),
    "LeftFoot": (1.079, 1.267, 1.129),
    "RightUpperLeg": (1.023, 1.506, 1.023),
    "RightLowerLeg": (1.023, 1.506, 1.023),
    "RightFoot": (1.079, 1.267, 1.129),
}

# Scale R15 -> Rthro Slender
_R15_TO_SLENDER: Dict[str, Vec3] = {
    "Head": (0.896, 0.942, 0.896),
    "UpperTorso": (0.905, 1.204, 1.013),
    "LowerTorso": (0.986, 1.004, 1.013),
    "LeftUpperArm": (1.004, 1.207, 1.006),
    "LeftLowerArm": (1.004, 1.207, 1.006),
    "LeftHand": (0.948, 1.174, 1.094),
    "RightUpperArm": (1.004, 1.208, 1.006),
    "RightLowerArm": (1.004, 1.208, 1.006),
    "RightHand": (0.948, 1.174, 1.094),
    "LeftUpperLeg": (0.976, 1.401, 0.909),
    "LeftLowerLeg": (0.976, 1.301, 0.909),
    "LeftFoot": (1.030, 1.133, 1.004),
    "RightUpperLeg": (0.976, 1.401, 0.909),
    "RightLowerLeg": (0.976, 1.301, 0.909),
    "RightFoot": (1.030, 1.133, 1.004),
}


def _div(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[0] / b[0] if abs(b[0]) > 1e-8 else 1.0,
        a[1] / b[1] if abs(b[1]) > 1e-8 else 1.0,
        a[2] / b[2] if abs(b[2]) > 1e-8 else 1.0,
    )


def _mul(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] * b[0], a[1] * b[1], a[2] * b[2])


def _lerp(a: Vec3, b: Vec3, t: float) -> Vec3:
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return (
        a[0] + (b[0] - a[0]) * t,
        a[1] + (b[1] - a[1]) * t,
        a[2] + (b[2] - a[2]) * t,
    )


def _sample_scales(limb_name: str, scale_type: str):
    """Return (scale_r15, scale_normal, scale_slender) — the factor that
    converts the part from its authored form INTO each canonical form."""
    one: Vec3 = (1.0, 1.0, 1.0)
    to_normal = _R15_TO_NORMAL.get(limb_name, one)
    to_slender = _R15_TO_SLENDER.get(limb_name, one)
    normal_to_r15 = _div(one, to_normal)
    slender_to_r15 = _div(one, to_slender)
    normal_to_slender = _div(to_slender, to_normal)
    slender_to_normal = _div(to_normal, to_slender)

    if scale_type == "Normal":
        return normal_to_r15, one, normal_to_slender
    if scale_type == "Slender":
        return slender_to_r15, slender_to_normal, one
    return one, to_normal, to_slender


def compute_limb_scale(limb_name: str, scale_type: str, hd_scale: Optional[dict]) -> Vec3:
    """Per-axis geometry scale for one R15 body part, in part-local space."""
    one: Vec3 = (1.0, 1.0, 1.0)
    if limb_name not in _R15_TO_NORMAL:
        return one
    if not hd_scale:
        return one

    scale_r15, scale_normal, scale_slender = _sample_scales(limb_name, scale_type or "Classic")

    proportion = float(hd_scale.get("proportion", 0.0))
    body_type = float(hd_scale.get("body_type", 0.0))
    scale_proportions = _lerp(scale_normal, scale_slender, proportion)
    result = _lerp(scale_r15, scale_proportions, body_type)

    base: Vec3 = (
        float(hd_scale.get("width", 1.0)),
        float(hd_scale.get("height", 1.0)),
        float(hd_scale.get("depth", 1.0)),
    )
    if limb_name == "Head":
        result = _mul(result, (float(hd_scale.get("head", 1.0)),) * 3)
    else:
        result = _mul(result, base)
    return result


def is_r15_part_name(part_name: str) -> bool:
    return (part_name or "") in _R15_TO_NORMAL


# ---------------------------------------------------------------------------
# Import-time context (set once per import from meta["hd_scale"])
# ---------------------------------------------------------------------------

_HD_SCALE_CONTEXT: Optional[dict] = None


def set_hd_scale_context(hd_scale: Optional[dict]) -> None:
    global _HD_SCALE_CONTEXT
    _HD_SCALE_CONTEXT = dict(hd_scale) if hd_scale else None


def entry_limb_scale(entry: Optional[dict]) -> Vec3:
    """Limb scale for a part entry under the current import context."""
    one: Vec3 = (1.0, 1.0, 1.0)
    if not _HD_SCALE_CONTEXT or not entry:
        return one
    part_name = entry.get("name") or ""
    if not is_r15_part_name(part_name):
        return one
    return compute_limb_scale(part_name, entry.get("scale_type") or "Classic", _HD_SCALE_CONTEXT)
