"""Generate saved, constant, and linear playback cases for every Sample Rigs blend.

Run headless Blender with --python this_file -- OUTPUT_DIRECTORY.
The sample files are opened read-only in effect: never saved.
"""

import collections
import json
import math
import sys
import traceback
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import roblox_animations  # noqa: E402
from roblox_animations.animation import serialization as s  # noqa: E402
from roblox_animations.core.evaluation import rig_evaluation_context  # noqa: E402
from roblox_animations.core.utils import (  # noqa: E402
    get_action_fcurves,
    get_animation_data_action_slot,
)

EXPORTS = {
    "Blender R6 (1).blend": "InternalArmature",
    "ImproveR6IK(1).blend": "__Rig",
    "leftrightwmastercontroller.blend": "MC(DONTIMPORT)",
    "OldR15IK_1.blend": "__Rig",
    "r15_block_ik.blend": "__Rig",
    "R15_Rig.blend": "__Rig",
    "R15Rig.blend": "R15ExportArmature",
    "R6.blend": "_Rig",
    "R6IkRig_1.blend": "__Rig",
    "Rigky_V.1.2.1 (1).blend": "__Rig",
}
R6 = {"Torso", "Head", "Left Arm", "Right Arm", "Left Leg", "Right Leg"}
R15 = {"LowerTorso", "UpperTorso", "Head"} | {
    side + part
    for side in ("Left", "Right")
    for part in ("UpperArm", "LowerArm", "Hand", "UpperLeg", "LowerLeg", "Foot")
}


def generate(output, selected=()):
    output.mkdir(parents=True, exist_ok=True)
    reports, manifest = [], []
    for index, path in enumerate(sorted((ROOT / "Sample Rigs").glob("*.blend"))):
        if selected and path.name not in selected:
            continue
        modes = ["saved", "CONSTANT", "LINEAR"]
        if path.name == "leftrightwmastercontroller.blend":
            modes.append("CONSTANT_EXTERNAL")
        for mode in modes:
            case_id = f"sample_{index}_{mode}"
            try:
                bpy.ops.wm.open_mainfile(filepath=str(path.resolve()))
                scene = bpy.context.scene
                rig = bpy.data.objects[EXPORTS[path.name]]
                names = R15 if "LowerTorso" in rig.data.bones else R6
                if mode != "saved":
                    scene.frame_start, scene.frame_end = 1, 25
                    scene.render.fps, scene.render.fps_base = 30, 1
                    scene.frame_set(1)
                    # Replace only actions in this disposable process. Keep
                    # drivers, constraints, custom properties, and rig hierarchy.
                    external_controls = any(
                        getattr(c, "target", None)
                        and c.target != rig
                        and c.target.type == "ARMATURE"
                        for b in rig.pose.bones
                        for c in b.constraints
                    )
                    for obj in list(bpy.data.objects):
                        if obj.type != "ARMATURE":
                            continue
                        if obj.animation_data:
                            obj.animation_data.action = None
                            for track in obj.animation_data.nla_tracks:
                                track.mute = True
                        if mode == "CONSTANT_EXTERNAL" and obj == rig:
                            continue
                        for n, bone in enumerate(obj.pose.bones):
                            # Leave externally driven export bones unkeyed,
                            # while retaining controls on the same armature.
                            if obj == rig and external_controls and bone.name in names:
                                continue
                            if any(not c.mute for c in bone.constraints):
                                continue
                            location = bone.location.copy()
                            rotation = (
                                bone.rotation_quaternion.copy()
                                if bone.rotation_mode == "QUATERNION"
                                else bone.rotation_euler.to_quaternion()
                            )
                            for frame, amount in (
                                (1, 0),
                                (9, 0.12),
                                (17, -0.08),
                                (25, 0),
                            ):
                                bone.location = location + Vector(
                                    (amount, amount * 0.3, 0)
                                )
                                bone.keyframe_insert("location", frame=frame)
                                q = rotation @ Quaternion((0, 0, 1), amount)
                                if bone.rotation_mode == "QUATERNION":
                                    bone.rotation_quaternion = q
                                    bone.keyframe_insert(
                                        "rotation_quaternion", frame=frame
                                    )
                                elif bone.rotation_mode != "AXIS_ANGLE":
                                    bone.rotation_euler = q.to_euler(bone.rotation_mode)
                                    bone.keyframe_insert("rotation_euler", frame=frame)
                        a = obj.animation_data
                        if a and a.action:
                            for curve in get_action_fcurves(
                                a.action, slot=get_animation_data_action_slot(a)
                            ):
                                for key in curve.keyframe_points:
                                    key.interpolation = "CONSTANT" if mode == "CONSTANT_EXTERNAL" else mode
                scene.frame_step = 1
                scene.rbx_anim_settings.rbx_full_range_bake = True
                payload = s.serialize(rig)
                assert payload["kfs"], "Empty export"
                frames = set(
                    scene.frame_start + i / 4
                    for i in range((scene.frame_end - scene.frame_start) * 4)
                )
                for key in payload["kfs"]:
                    frame = scene.frame_start + key["t"] * (
                        scene.render.fps / scene.render.fps_base
                    )
                    frames.update(
                        f
                        for f in (frame - 0.001, frame + 0.001)
                        if scene.frame_start <= f < scene.frame_end
                    )
                expected = []
                with rig_evaluation_context(rig):
                    a = s._analyze_export(rig, None)
                    for frame in sorted(frames):
                        scene.frame_set(math.floor(frame), subframe=frame % 1)
                        poses = s.serialize_combined_animation_state(
                            rig,
                            rig.evaluated_get(a.depsgraph),
                            a.run_deform_path,
                            a.is_skinned_rig,
                            a.back_trans_cached,
                            a.world_transform_cached,
                            a.scale_factor_cached,
                            {},
                            set(),
                        )
                        expected.append(
                            {
                                "t": (frame - scene.frame_start) / a.desired_fps,
                                "poses": {
                                    n: cf for n, cf in poses.items() if n in names
                                },
                            }
                        )
                first = expected[0]["poses"]
                motion = max(
                    (
                        max(abs(x - y) for x, y in zip(cf, first[name]))
                        for row in expected
                        for name, cf in row["poses"].items()
                        if name in first
                    ),
                    default=0,
                )
                case = {
                    "motionMaxComponent": motion,
                    "id": case_id,
                    "file": path.name,
                    "mode": mode,
                    "rig": "TemplateR15" if names == R15 else "TemplateR6",
                    "baked": payload,
                    "expected": expected,
                }
                (output / (case_id + ".json")).write_text(json.dumps(case))
                report = {
                    "motionMaxComponent": motion,
                    "id": case_id,
                    "file": path.name,
                    "mode": mode,
                    "frames": len(payload["kfs"]),
                    "expectedSamples": len(expected),
                    "styles": dict(
                        collections.Counter(
                            p[1] for k in payload["kfs"] for p in k["kf"].values()
                        )
                    ),
                }
                manifest.append(case_id)
            except Exception:
                report = {
                    "id": case_id,
                    "file": path.name,
                    "mode": mode,
                    "error": traceback.format_exc(),
                }
            reports.append(report)
            print("CASE", json.dumps(report), flush=True)
    (output / "manifest.json").write_text(json.dumps(manifest))
    (output / "generation.json").write_text(json.dumps(reports, indent=2))
    components = ROOT / "src/ServerScriptService/BlenderAnimationsInternal/Components"
    (output / "sources.json").write_text(
        json.dumps(
            {
                n: (components / (n + ".lua")).read_text(encoding="utf-8-sig")
                for n in ("Rig", "RigPart", "Pose")
            }
        )
    )


if __name__ == "__main__":
    roblox_animations.register()
    generate(
        Path(sys.argv[sys.argv.index("--") + 1]), sys.argv[sys.argv.index("--") + 2 :]
    )
