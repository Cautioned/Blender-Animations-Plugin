"""Generate evaluated Blender references for the Studio Animator integration test.

Generate with: blender --background --factory-startup --python
    roblox_animations/tests/easing_playback_cases.py -- <output_directory>
Serve that directory: python -m http.server 18763 --bind 127.0.0.1 --directory <output_directory>
Run easing_playback_batch.lua in Studio's command bar with TemplateR6/TemplateR15
in Workspace. Results include strict on-frame and interpolated pose tolerances.
The output is intentionally separate from the repository (several MB).
"""

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "roblox_animations.tests"

from .test_studio_template_roundtrip import build_export_case, templates
from ..animation.serialization import serialize
from ..core.constants import get_transform_to_blender
from ..core.utils import get_action_fcurves, mat_to_cf, to_matrix


def easing_combinations():
    enums = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items
    for item in enums:
        directions = (
            ("AUTO",)
            if item.identifier in {"CONSTANT", "LINEAR", "BEZIER"}
            else ("AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT")
        )
        for direction in directions:
            yield item.identifier, direction


def generate_cases(output_directory):
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for rig_name, fixture in templates().items():
        for mode in ("direct", "proxy"):
            build_export_case(rig_name, fixture)
            source = bpy.data.objects[rig_name]
            controls = bpy.data.objects[rig_name + "_Controls"]
            hand = "RightHand" if "RightHand" in source.data.bones else "Right Arm"
            left = "LeftHand" if "LeftHand" in source.data.bones else "Left Arm"
            if mode == "direct":
                for bone in source.pose.bones:
                    for constraint in list(bone.constraints):
                        bone.constraints.remove(constraint)
            animated = controls if mode == "proxy" else source
            names = (
                ["CTRL_" + hand, "CTRL_" + left, "VerificationWeapon"]
                if mode == "proxy"
                else [hand, left, "VerificationWeapon"]
            )
            scene = bpy.context.scene
            scene.render.fps = 30
            scene.render.fps_base = 1
            scene.frame_start = 1
            scene.frame_end = 61
            scene.frame_step = 1
            scene.rbx_anim_settings.rbx_full_range_bake = True
            for interpolation, direction in easing_combinations():
                for obj in (source, controls):
                    obj.animation_data_clear()
                    for bone in obj.pose.bones:
                        bone.matrix_basis = Matrix.Identity(4)
                for index, name in enumerate(names):
                    bone = animated.pose.bones[name]
                    bone.rotation_mode = "XYZ"
                    # Nonzero endpoints expose missing poses and rest-pose gaps.
                    # Staggered times force the importer to handle sibling keys.
                    middle = (31, 20, 43)[index]
                    for frame, value in ((1, 0.2), (middle, 0.8), (61, 0.2)):
                        bone.location = (value, 0, 0)
                        bone.rotation_euler = (0, 0, value * 0.5)
                        bone.keyframe_insert("location", frame=frame)
                        bone.keyframe_insert("rotation_euler", frame=frame)
                for curve in get_action_fcurves(animated.animation_data.action):
                    for key in curve.keyframe_points:
                        key.interpolation = interpolation
                        key.easing = direction
                        key.handle_left_type = "AUTO_CLAMPED"
                        key.handle_right_type = "AUTO_CLAMPED"
                scene.frame_set(1)
                baked = serialize(source)
                expected = []
                # Integer frames, half frames, and immediately around hold edges.
                frames = {1 + i * 0.5 for i in range(121)}
                frames.update(
                    f + offset for f in (20, 31, 43) for offset in (-0.001, 0.001)
                )
                back = get_transform_to_blender().inverted()
                for frame in sorted(frames):
                    whole = math.floor(frame)
                    scene.frame_set(whole, subframe=frame - whole)
                    evaluated = source.evaluated_get(
                        bpy.context.evaluated_depsgraph_get()
                    )
                    poses = {}
                    for name in (hand, left, "VerificationWeapon"):
                        bone = evaluated.pose.bones[name]
                        world = (
                            back
                            @ bone.matrix
                            @ to_matrix(bone.bone["nicetransform"]).inverted()
                            @ to_matrix(bone.bone["transform1"]).inverted()
                        )
                        poses[name] = [round(v, 7) for v in mat_to_cf(world)]
                    expected.append(
                        {
                            "t": (frame - 1) / 30,
                            "poses": poses,
                            "onFrame": abs(frame - round(frame)) < 1e-6,
                        }
                    )
                case_id = f"{rig_name}_{mode}_{interpolation}_{direction}"
                case = {
                    "id": case_id,
                    "rig": rig_name,
                    "mode": mode,
                    "interpolation": interpolation,
                    "direction": direction,
                    "weaponParent": hand,
                    "baked": baked,
                    "expected": expected,
                }
                (output / (case_id + ".json")).write_text(
                    json.dumps(case), encoding="utf-8"
                )
                manifest.append(case_id)
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    components = root / "src/ServerScriptService/BlenderAnimationsInternal/Components"
    sources = {
        name: (components / (name + ".lua")).read_text(encoding="utf-8-sig")
        for name in ("Rig", "RigPart", "Pose")
    }
    sources["Runner"] = (
        Path(__file__)
        .with_name("easing_playback_studio.lua")
        .read_text(encoding="utf-8")
    )
    (output / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    import roblox_animations

    roblox_animations.register()
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(arguments) != 1:
        raise SystemExit("Pass one output directory after --")
    print("Generated easing cases:", len(generate_cases(arguments[0])))
