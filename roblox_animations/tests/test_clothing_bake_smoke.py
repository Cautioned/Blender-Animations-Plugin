"""Headless smoke test for the clothing bake (not a unit test; run directly)."""
import importlib
import sys
import types
from pathlib import Path

ws = Path(__file__).resolve().parents[2]
pkg_dir = ws / "roblox_animations"

pkg = types.ModuleType("roblox_animations")
pkg.__path__ = [str(pkg_dir)]
sys.modules["roblox_animations"] = pkg
rig = types.ModuleType("roblox_animations.rig")
rig.__path__ = [str(pkg_dir / "rig")]
sys.modules["roblox_animations.rig"] = rig

clothing = importlib.import_module("roblox_animations.rig.clothing")

for g in ("torso", "left_arm", "right_arm", "left_leg", "right_leg"):
    lu = clothing._get_guide(g)
    print(g, "guide tris:", None if lu is None else len(lu.triangles))

TW, TH = 585, 559
tpl = [0.0] * (TW * TH * 4)
for y in range(TH):
    fy = y / (TH - 1)
    row = y * TW
    for x in range(TW):
        i = (row + x) * 4
        tpl[i] = x / (TW - 1)
        tpl[i + 1] = fy
        tpl[i + 2] = 0.25
        tpl[i + 3] = 1.0


def provider(ref):
    return (tpl, TW, TH, True)


clothing.set_clothing_context(shirt_template="fake_shirt", pants_template="fake_pants")

for g in ("torso", "left_arm"):
    w, h, buf = clothing._bake_group_pixels(g, (1.0, 0.0, 1.0, 1.0), provider)
    covered = 0
    minu = 2.0
    maxu = -1.0
    minv = 2.0
    maxv = -1.0
    for p in range(w * h):
        i = p * 4
        if abs(buf[i] - 1.0) < 1e-6 and abs(buf[i + 1]) < 1e-6 and abs(buf[i + 2] - 1.0) < 1e-6:
            continue
        covered += 1
        minu = min(minu, buf[i])
        maxu = max(maxu, buf[i])
        minv = min(minv, buf[i + 1])
        maxv = max(maxv, buf[i + 1])
    print(
        f"{g}: {w}x{h} covered={covered}/{w * h} ({100.0 * covered / (w * h):.1f}%) "
        f"tpl_u=[{minu:.3f},{maxu:.3f}] tpl_v=[{minv:.3f},{maxv:.3f}]"
    )
