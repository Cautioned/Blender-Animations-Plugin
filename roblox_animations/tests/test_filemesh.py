import gzip
import struct
import unittest
from unittest import mock

from ..animation.face_controls import (
    apply_facs_properties_to_armature,
    compute_facs_bone_transforms,
    compute_facs_state_weights,
    face_control_property_name,
    facs_payload_from_mesh_data,
    grouped_face_controls,
)
from ..ui import properties as ui_properties
from ..rig import filemesh
from ..rig.filemesh import extract_asset_id, parse_filemesh


class _FakeFaceControls:
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)


class _FakePoseBone:
    def __init__(self):
        self.rotation_mode = "QUATERNION"
        self.location = (0.0, 0.0, 0.0)
        self.rotation_euler = (0.0, 0.0, 0.0)


class _FakePose:
    def __init__(self, bones):
        self.bones = bones


class _FakeArmature:
    def __init__(self, face_controls, face_bones):
        self.type = "ARMATURE"
        self.rbx_face_controls = face_controls
        self.pose = _FakePose(face_bones)


def _make_vertex(px, py, pz):
    return struct.pack(
        "<8f4b4B",
        px,
        py,
        pz,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0,
        0,
        0,
        127,
        255,
        255,
        255,
        255,
    )


def _make_bone(name_index):
    return struct.pack(
        "<IHHf9f3f",
        name_index,
        0xFFFF,
        0xFFFF,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
    )


def _make_subset():
    return struct.pack(
        "<IIIII26H",
        0,
        1,
        0,
        2,
        2,
        0,
        1,
        *([0xFFFF] * 24),
    )


def _make_skinning_block():
    return bytes(
        [0, 0, 0, 0, 255, 0, 0, 0]
        + [1, 0, 0, 0, 255, 0, 0, 0]
    )


def _make_faces_block():
    return struct.pack("<III", 0, 1, 1)


def _make_name_table():
    return b"Root\0Jaw\0"


def _flatten_matrix_rows(rows):
    return [value for row in rows for value in row]


def _pack_quantized_matrix_v1(rows, cols, values):
    return struct.pack("<HII", 1, rows, cols) + struct.pack(
        f"<{rows * cols}f", *values
    )


def _pack_quantized_matrix_v2(rows, cols, values):
    min_value = float(min(values)) if values else 0.0
    max_value = float(max(values)) if values else 0.0
    if not values or max_value == min_value:
        quantized = [0] * len(values)
    else:
        precision = (max_value - min_value) / 65535.0
        quantized = [
            max(0, min(65535, int(round((float(value) - min_value) / precision))))
            for value in values
        ]
    return struct.pack("<HIIff", 2, rows, cols, min_value, max_value) + struct.pack(
        f"<{rows * cols}H", *quantized
    )


def _make_facs_channel_values():
    return {
        "px": [
            [0.0, 1.0, 2.0, 3.0, 4.0],
            [10.0, 11.0, 12.0, 13.0, 14.0],
        ],
        "py": [
            [0.0, 0.1, 0.2, 0.3, 0.4],
            [1.0, 1.1, 1.2, 1.3, 1.4],
        ],
        "pz": [
            [-1.0, -2.0, -3.0, -4.0, -5.0],
            [-6.0, -7.0, -8.0, -9.0, -10.0],
        ],
        "rx": [
            [0.0, 5.0, 10.0, 15.0, 20.0],
            [25.0, 30.0, 35.0, 40.0, 45.0],
        ],
        "ry": [
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [6.0, 7.0, 8.0, 9.0, 10.0],
        ],
        "rz": [
            [-9.0, -8.0, -7.0, -6.0, -5.0],
            [-4.0, -3.0, -2.0, -1.0, 0.0],
        ],
    }


def _make_quantized_transforms_block():
    rows = 2
    cols = 5
    channels = _make_facs_channel_values()
    return b"".join(
        [
            _pack_quantized_matrix_v1(rows, cols, _flatten_matrix_rows(channels["px"])),
            _pack_quantized_matrix_v2(rows, cols, _flatten_matrix_rows(channels["py"])),
            _pack_quantized_matrix_v1(rows, cols, _flatten_matrix_rows(channels["pz"])),
            _pack_quantized_matrix_v2(rows, cols, _flatten_matrix_rows(channels["rx"])),
            _pack_quantized_matrix_v1(rows, cols, _flatten_matrix_rows(channels["ry"])),
            _pack_quantized_matrix_v2(rows, cols, _flatten_matrix_rows(channels["rz"])),
        ]
    )


def _make_v4_mesh():
    name_table = _make_name_table()
    header = struct.pack("<HHIIHHIHBB", 24, 0, 2, 1, 1, 2, len(name_table), 1, 0, 0)
    body = b"".join(
        [
            _make_vertex(0.0, 0.0, 0.0),
            _make_vertex(1.0, 0.0, 0.0),
            _make_skinning_block(),
            _make_faces_block(),
            struct.pack("<I", 0),
            _make_bone(0),
            _make_bone(5),
            name_table,
            _make_subset(),
        ]
    )
    return b"version 4.00\n" + header + body


def _make_v5_facs_block():
    face_bone_names = b"FaceJaw\0FaceBrow\0"
    face_control_names = b"c_JD\0l_BL\0r_BL\0"
    quantized_transforms = _make_quantized_transforms_block()
    two_pose_correctives = struct.pack("<HH", 0, 1)
    three_pose_correctives = struct.pack("<HHH", 0, 1, 2)
    header = struct.pack(
        "<IIQII",
        len(face_bone_names),
        len(face_control_names),
        len(quantized_transforms),
        len(two_pose_correctives),
        len(three_pose_correctives),
    )
    return b"".join(
        [
            header,
            face_bone_names,
            face_control_names,
            quantized_transforms,
            two_pose_correctives,
            three_pose_correctives,
        ]
    )


def _make_v5_mesh():
    name_table = _make_name_table()
    facs_block = _make_v5_facs_block()
    header = struct.pack(
        "<HHIIHHIHBBII",
        32,
        0,
        2,
        1,
        1,
        2,
        len(name_table),
        1,
        0,
        0,
        1,
        len(facs_block),
    )
    body = b"".join(
        [
            _make_vertex(0.0, 0.0, 0.0),
            _make_vertex(1.0, 0.0, 0.0),
            _make_skinning_block(),
            _make_faces_block(),
            struct.pack("<I", 0),
            _make_bone(0),
            _make_bone(5),
            name_table,
            _make_subset(),
            facs_block,
        ]
    )
    return b"version 5.00\n" + header + body


def _make_v5_mesh_with_invalid_facs_format():
    name_table = _make_name_table()
    facs_block = _make_v5_facs_block()
    header = struct.pack(
        "<HHIIHHIHBBII",
        32,
        0,
        2,
        1,
        1,
        2,
        len(name_table),
        1,
        0,
        0,
        2,
        len(facs_block),
    )
    body = b"".join(
        [
            _make_vertex(0.0, 0.0, 0.0),
            _make_vertex(1.0, 0.0, 0.0),
            _make_skinning_block(),
            _make_faces_block(),
            struct.pack("<I", 0),
            _make_bone(0),
            _make_bone(5),
            name_table,
            _make_subset(),
            facs_block,
        ]
    )
    return b"version 5.00\n" + header + body


def _make_v6_mesh():
    name_table = _make_name_table()
    coremesh = b"".join(
        [
            struct.pack("<I", 2),
            _make_vertex(0.0, 0.0, 0.0),
            _make_vertex(1.0, 0.0, 0.0),
            struct.pack("<I", 1),
            _make_faces_block(),
        ]
    )
    skinning = b"".join(
        [
            struct.pack("<I", 2),
            _make_skinning_block(),
            struct.pack("<I", 2),
            _make_bone(0),
            _make_bone(5),
            struct.pack("<I", len(name_table)),
            name_table,
            struct.pack("<I", 1),
            _make_subset(),
        ]
    )
    chunks = b"".join(
        [
            b"COREMESH" + struct.pack("<II", 1, len(coremesh)) + coremesh,
            b"SKINNING" + struct.pack("<II", 1, len(skinning)) + skinning,
        ]
    )
    return b"version 6.00\n" + chunks


def _make_lods_chunk(lod_type=1, num_high_quality_lods=1, lod_offsets=(0,)):
    body = struct.pack("<HB", lod_type, num_high_quality_lods)
    body += struct.pack("<I", len(lod_offsets))
    if lod_offsets:
        body += struct.pack(f"<{len(lod_offsets)}I", *lod_offsets)
    return b"LODS\0\0\0\0" + struct.pack("<II", 1, len(body)) + body


def _make_v6_mesh_with_lods():
    name_table = _make_name_table()
    coremesh = b"".join(
        [
            struct.pack("<I", 2),
            _make_vertex(0.0, 0.0, 0.0),
            _make_vertex(1.0, 0.0, 0.0),
            struct.pack("<I", 1),
            _make_faces_block(),
        ]
    )
    skinning = b"".join(
        [
            struct.pack("<I", 2),
            _make_skinning_block(),
            struct.pack("<I", 2),
            _make_bone(0),
            _make_bone(5),
            struct.pack("<I", len(name_table)),
            name_table,
            struct.pack("<I", 1),
            _make_subset(),
        ]
    )
    chunks = b"".join(
        [
            b"COREMESH" + struct.pack("<II", 1, len(coremesh)) + coremesh,
            _make_lods_chunk(lod_type=2, num_high_quality_lods=1, lod_offsets=(0, 24)),
            b"SKINNING" + struct.pack("<II", 1, len(skinning)) + skinning,
        ]
    )
    return b"version 6.00\n" + chunks


def _make_v6_mesh_with_facs():
    name_table = _make_name_table()
    facs_block = _make_v5_facs_block()
    coremesh = b"".join(
        [
            struct.pack("<I", 2),
            _make_vertex(0.0, 0.0, 0.0),
            _make_vertex(1.0, 0.0, 0.0),
            struct.pack("<I", 1),
            _make_faces_block(),
        ]
    )
    skinning = b"".join(
        [
            struct.pack("<I", 2),
            _make_skinning_block(),
            struct.pack("<I", 2),
            _make_bone(0),
            _make_bone(5),
            struct.pack("<I", len(name_table)),
            name_table,
            struct.pack("<I", 1),
            _make_subset(),
        ]
    )
    facs_chunk = struct.pack("<I", len(facs_block)) + facs_block
    chunks = b"".join(
        [
            b"COREMESH" + struct.pack("<II", 1, len(coremesh)) + coremesh,
            b"SKINNING" + struct.pack("<II", 1, len(skinning)) + skinning,
            b"FACS\0\0\0\0" + struct.pack("<II", 1, len(facs_chunk)) + facs_chunk,
        ]
    )
    return b"version 6.00\n" + chunks


def _make_v7_mesh():
    name_table = _make_name_table()
    coremesh = struct.pack("<I", 4) + b"DRCO"
    skinning = b"".join(
        [
            struct.pack("<I", 2),
            _make_skinning_block(),
            struct.pack("<I", 2),
            _make_bone(0),
            _make_bone(5),
            struct.pack("<I", len(name_table)),
            name_table,
            struct.pack("<I", 1),
            _make_subset(),
        ]
    )
    chunks = b"".join(
        [
            b"COREMESH" + struct.pack("<II", 2, len(coremesh)) + coremesh,
            b"SKINNING" + struct.pack("<II", 1, len(skinning)) + skinning,
        ]
    )
    return b"version 7.00\n" + chunks


def _make_v7_mesh_with_lods():
    name_table = _make_name_table()
    coremesh = struct.pack("<I", 4) + b"DRCO"
    skinning = b"".join(
        [
            struct.pack("<I", 2),
            _make_skinning_block(),
            struct.pack("<I", 2),
            _make_bone(0),
            _make_bone(5),
            struct.pack("<I", len(name_table)),
            name_table,
            struct.pack("<I", 1),
            _make_subset(),
        ]
    )
    chunks = b"".join(
        [
            b"COREMESH" + struct.pack("<II", 2, len(coremesh)) + coremesh,
            _make_lods_chunk(lod_type=3, num_high_quality_lods=2, lod_offsets=(0, 12, 24)),
            b"SKINNING" + struct.pack("<II", 1, len(skinning)) + skinning,
        ]
    )
    return b"version 7.00\n" + chunks


class TestFileMeshParsing(unittest.TestCase):
    def test_extract_asset_id(self):
        self.assertEqual(extract_asset_id("rbxassetid://12345"), 12345)
        self.assertEqual(extract_asset_id("https://www.roblox.com/asset/?id=456"), 456)
        self.assertEqual(extract_asset_id("789"), 789)

    def test_parse_v4_skinning(self):
        parsed = parse_filemesh(_make_v4_mesh())
        self.assertEqual(parsed["version"], "version 4.00")
        self.assertEqual(parsed["bone_names"], ["Root", "Jaw"])
        self.assertEqual(len(parsed["positions"]), 2)
        self.assertEqual(parsed["faces"], [(0, 1, 1)])
        self.assertEqual(parsed["normals"][0], (0.0, 1.0, 0.0))
        self.assertEqual(parsed["uvs"][1], (0.0, 0.0))
        self.assertAlmostEqual(parsed["vertex_weights"][0]["Root"], 1.0)
        self.assertAlmostEqual(parsed["vertex_weights"][1]["Jaw"], 1.0)

    def test_parse_v6_skinning_chunk(self):
        parsed = parse_filemesh(_make_v6_mesh())
        self.assertEqual(parsed["version"], "version 6.00")
        self.assertEqual(parsed["bone_names"], ["Root", "Jaw"])
        self.assertEqual(len(parsed["positions"]), 2)
        self.assertEqual(parsed["faces"], [(0, 1, 1)])
        self.assertEqual(parsed["normals"][1], (0.0, 1.0, 0.0))
        self.assertAlmostEqual(parsed["vertex_weights"][0]["Root"], 1.0)
        self.assertAlmostEqual(parsed["vertex_weights"][1]["Jaw"], 1.0)

    def test_parse_v6_reads_lods_chunk_metadata(self):
        parsed = parse_filemesh(_make_v6_mesh_with_lods())

        self.assertEqual(parsed["lod_type"], 2)
        self.assertEqual(parsed["num_high_quality_lods"], 1)
        self.assertEqual(parsed["lod_offsets"], [0, 24])

    def test_parse_v7_requires_draco_geometry(self):
        with mock.patch.object(filemesh, "_load_blender_draco_dll", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "draco decoder is unavailable"):
                parse_filemesh(_make_v7_mesh())

    def test_parse_v7_reads_lods_chunk_metadata(self):
        decoded_vertices = [
            {"position": (0.0, 0.0, 0.0), "normal": (0.0, 1.0, 0.0), "uv": (0.0, 0.0)},
            {"position": (1.0, 0.0, 0.0), "normal": (0.0, 1.0, 0.0), "uv": (1.0, 0.0)},
        ]

        with mock.patch.object(
            filemesh,
            "_decode_draco_coremesh_v2",
            return_value=(decoded_vertices, [(0, 1, 1)], 2),
        ):
            parsed = parse_filemesh(_make_v7_mesh_with_lods())

        self.assertEqual(parsed["lod_type"], 3)
        self.assertEqual(parsed["num_high_quality_lods"], 2)
        self.assertEqual(parsed["lod_offsets"], [0, 12, 24])

    def test_parse_v7_uses_draco_geometry_when_decoder_available(self):
        decoded_vertices = [
            {"position": (0.0, 0.0, 0.0), "normal": (0.0, 1.0, 0.0), "uv": (0.0, 0.0)},
            {"position": (1.0, 0.0, 0.0), "normal": (0.0, 1.0, 0.0), "uv": (1.0, 0.0)},
        ]

        with mock.patch.object(
            filemesh,
            "_decode_draco_coremesh_v2",
            return_value=(decoded_vertices, [(0, 1, 1)], 2),
        ):
            parsed = parse_filemesh(_make_v7_mesh())

        self.assertEqual(parsed["version"], "version 7.00")
        self.assertEqual(parsed["positions"], [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)])
        self.assertEqual(parsed["normals"][0], (0.0, 1.0, 0.0))
        self.assertEqual(parsed["uvs"][1], (1.0, 0.0))
        self.assertEqual(parsed["faces"], [(0, 1, 1)])

    def test_parse_v5_facs_metadata(self):
        parsed = parse_filemesh(_make_v5_mesh())
        self.assertEqual(parsed["version"], "version 5.00")
        self.assertTrue(parsed["has_facs"])
        self.assertEqual(parsed["face_bone_names"], ["FaceJaw", "FaceBrow"])
        self.assertEqual(parsed["face_control_abbreviations"], ["c_JD", "l_BL", "r_BL"])
        self.assertEqual(
            parsed["face_control_names"],
            ["JawDrop", "LeftBrowLowerer", "RightBrowLowerer"],
        )
        self.assertEqual(parsed["facs_data"]["two_pose_correctives_size"], 4)
        self.assertEqual(parsed["facs_data"]["three_pose_correctives_size"], 6)
        self.assertEqual(
            parsed["facs_data"]["facs_pose_names"],
            [
                "JawDrop",
                "LeftBrowLowerer",
                "RightBrowLowerer",
                "x2_JawDrop_LeftBrowLowerer",
                "x3_JawDrop_LeftBrowLowerer_RightBrowLowerer",
            ],
        )
        self.assertEqual(
            parsed["facs_data"]["two_pose_correctives"][0]["control_names"],
            ("JawDrop", "LeftBrowLowerer"),
        )
        self.assertEqual(
            parsed["facs_data"]["three_pose_correctives"][0]["control_names"],
            ("JawDrop", "LeftBrowLowerer", "RightBrowLowerer"),
        )
        self.assertEqual(parsed["facs_data"]["quantized_transforms"]["rows"], 2)
        self.assertEqual(parsed["facs_data"]["quantized_transforms"]["cols"], 5)

        expected_channels = _make_facs_channel_values()
        jaw_drop = parsed["facs_data"]["bone_pose_transforms"]["FaceJaw"]["JawDrop"]
        self.assertEqual(jaw_drop["position"][0], expected_channels["px"][0][0])
        self.assertAlmostEqual(jaw_drop["position"][1], expected_channels["py"][0][0], places=4)
        self.assertEqual(jaw_drop["position"][2], expected_channels["pz"][0][0])

        brow_corrective = parsed["facs_data"]["bone_pose_transforms"]["FaceBrow"][
            "x3_JawDrop_LeftBrowLowerer_RightBrowLowerer"
        ]
        self.assertEqual(brow_corrective["position"][0], expected_channels["px"][1][4])
        self.assertAlmostEqual(brow_corrective["rotation"][0], expected_channels["rx"][1][4], places=3)
        self.assertEqual(brow_corrective["rotation"][1], expected_channels["ry"][1][4])
        self.assertAlmostEqual(brow_corrective["rotation"][2], expected_channels["rz"][1][4], places=4)

    def test_parse_v5_ignores_unsupported_facs_format(self):
        parsed = parse_filemesh(_make_v5_mesh_with_invalid_facs_format())

        self.assertFalse(parsed["has_facs"])
        self.assertEqual(parsed["face_bone_names"], [])
        self.assertEqual(parsed["face_control_names"], [])
        self.assertEqual(parsed["facs_data"]["format"], 2)
        self.assertIn("unsupported facs data format", parsed["facs_data"]["parse_error"])

    def test_parse_v6_facs_chunk(self):
        parsed = parse_filemesh(_make_v6_mesh_with_facs())
        self.assertEqual(parsed["version"], "version 6.00")
        self.assertTrue(parsed["has_facs"])
        self.assertIn(
            "x3_JawDrop_LeftBrowLowerer_RightBrowLowerer",
            parsed["facs_data"]["bone_pose_transforms"]["FaceJaw"],
        )

    def test_compute_facs_state_weights_matches_roblox_corrective_products(self):
        parsed = parse_filemesh(_make_v5_mesh())
        payload = facs_payload_from_mesh_data(parsed)

        state = compute_facs_state_weights(
            payload,
            {
                "JawDrop": 0.5,
                "LeftBrowLowerer": 0.25,
                "RightBrowLowerer": 0.75,
            },
        )

        self.assertEqual(state["JawDrop"], 0.5)
        self.assertEqual(state["LeftBrowLowerer"], 0.25)
        self.assertEqual(state["RightBrowLowerer"], 0.75)
        self.assertEqual(state["x2_JawDrop_LeftBrowLowerer"], 0.125)
        self.assertEqual(state["x3_JawDrop_LeftBrowLowerer_RightBrowLowerer"], 0.09375)

    def test_compute_facs_bone_transforms_accumulates_base_and_correctives(self):
        parsed = parse_filemesh(_make_v5_mesh())
        payload = facs_payload_from_mesh_data(parsed)
        face_jaw_poses = payload["bone_pose_transforms"]["FaceJaw"]
        face_brow_poses = payload["bone_pose_transforms"]["FaceBrow"]
        solved = compute_facs_bone_transforms(
            payload,
            {
                "JawDrop": 0.5,
                "LeftBrowLowerer": 0.25,
                "RightBrowLowerer": 0.75,
            },
        )

        expected_position_x = (
            (face_brow_poses["JawDrop"]["position"][0] * 0.5)
            + (face_brow_poses["LeftBrowLowerer"]["position"][0] * 0.25)
            + (face_brow_poses["RightBrowLowerer"]["position"][0] * 0.75)
            + (face_brow_poses["x2_JawDrop_LeftBrowLowerer"]["position"][0] * 0.125)
            + (face_brow_poses["x3_JawDrop_LeftBrowLowerer_RightBrowLowerer"]["position"][0] * 0.09375)
        )
        expected_rotation_z = (
            (face_jaw_poses["JawDrop"]["rotation"][2] * 0.5)
            + (face_jaw_poses["LeftBrowLowerer"]["rotation"][2] * 0.25)
            + (face_jaw_poses["RightBrowLowerer"]["rotation"][2] * 0.75)
            + (face_jaw_poses["x2_JawDrop_LeftBrowLowerer"]["rotation"][2] * 0.125)
            + (face_jaw_poses["x3_JawDrop_LeftBrowLowerer_RightBrowLowerer"]["rotation"][2] * 0.09375)
        )

        self.assertAlmostEqual(solved["FaceBrow"]["position"][0], expected_position_x)
        self.assertAlmostEqual(solved["FaceJaw"]["rotation"][2], expected_rotation_z)

    def test_face_control_property_name_and_grouping(self):
        self.assertEqual(
            face_control_property_name("LeftEyeUpperLidRaiser"),
            "rbx_facs_left_eye_upper_lid_raiser",
        )

        grouped = grouped_face_controls(
            [
                "JawDrop",
                "LeftEyeClosed",
                "Pucker",
                "UnknownControl",
            ]
        )
        self.assertEqual(
            grouped,
            [
                ("Eyes", ["LeftEyeClosed"]),
                ("Mouth", ["Pucker"]),
                ("Jaw and Tongue", ["JawDrop"]),
                ("Other", ["UnknownControl"]),
            ],
        )

    def test_apply_facs_properties_reapplies_held_state_on_new_frame(self):
        jaw_bone = _FakePoseBone()
        armature = _FakeArmature(
            _FakeFaceControls(rbx_facs_jaw_drop=1.0),
            {"FaceJaw": jaw_bone},
        )
        payload = {
            "face_bone_names": ["FaceJaw"],
            "face_control_names": ["JawDrop"],
            "facs_pose_names": ["JawDrop"],
            "two_pose_correctives": [],
            "three_pose_correctives": [],
            "bone_pose_transforms": {
                "FaceJaw": {
                    "JawDrop": {
                        "position": (1.0, 2.0, 3.0),
                        "rotation": (0.0, 0.0, 10.0),
                    }
                }
            },
        }

        apply_facs_properties_to_armature(
            armature,
            payload=payload,
            persist_state=False,
            apply_token=("frame", 10.0),
        )
        self.assertEqual(jaw_bone.location, (1.0, 2.0, 3.0))

        jaw_bone.location = (0.0, 0.0, 0.0)
        jaw_bone.rotation_euler = (0.0, 0.0, 0.0)

        apply_facs_properties_to_armature(
            armature,
            payload=payload,
            persist_state=False,
            apply_token=("frame", 11.0),
        )

        self.assertEqual(jaw_bone.location, (1.0, 2.0, 3.0))
        self.assertAlmostEqual(jaw_bone.rotation_euler[2], 0.17453292519943295)

    def test_depsgraph_face_controls_handler_applies_driver_updates(self):
        scene = mock.Mock()
        scene.frame_current_final = 12.0
        armature = mock.Mock()

        previous_sequence = ui_properties._FACE_CONTROL_DEPSGRAPH_SEQUENCE
        previous_guard = ui_properties._FACE_CONTROL_DEPSGRAPH_APPLYING
        ui_properties._FACE_CONTROL_DEPSGRAPH_SEQUENCE = 0
        ui_properties._FACE_CONTROL_DEPSGRAPH_APPLYING = False
        try:
            with mock.patch.object(ui_properties, "iter_active_facs_armatures", return_value=[armature]):
                with mock.patch.object(ui_properties, "apply_facs_properties_to_armature") as apply_mock:
                    ui_properties._depsgraph_face_controls_handler(scene, mock.Mock())

            apply_mock.assert_called_once()
            _, kwargs = apply_mock.call_args
            self.assertFalse(kwargs["persist_state"])
            self.assertEqual(kwargs["apply_token"][0], "depsgraph")
            self.assertEqual(kwargs["apply_token"][2], ("frame", 12.0))
        finally:
            ui_properties._FACE_CONTROL_DEPSGRAPH_SEQUENCE = previous_sequence
            ui_properties._FACE_CONTROL_DEPSGRAPH_APPLYING = previous_guard

    def test_parse_gzipped_filemesh(self):
        parsed = parse_filemesh(gzip.compress(_make_v4_mesh()))
        self.assertEqual(parsed["version"], "version 4.00")
        self.assertAlmostEqual(parsed["vertex_weights"][0]["Root"], 1.0)

    def test_parse_filemesh_with_prefixed_bytes(self):
        parsed = parse_filemesh(b"junk-prefix" + _make_v6_mesh())
        self.assertEqual(parsed["version"], "version 6.00")
        self.assertAlmostEqual(parsed["vertex_weights"][1]["Jaw"], 1.0)


if __name__ == "__main__":
    unittest.main()