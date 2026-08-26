import ctypes
import gzip
import io
import struct
import unittest
import urllib.error
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


class _FakeDracoDll:
    def __init__(
        self,
        vertex_count=2,
        index_count=3,
        index_values=(0, 1, 1),
        read_indices=True,
        index_byte_length=None,
    ):
        self.vertex_count = vertex_count
        self.index_count = index_count
        self.read_indices = read_indices
        self.position_bytes = struct.pack("<6f", 0.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        self.index_bytes = struct.pack(f"<{len(index_values)}I", *index_values) if index_values else b""
        self.index_byte_length = len(self.index_bytes) if index_byte_length is None else index_byte_length

    def decoderCreate(self):
        return object()

    def decoderRelease(self, _decoder):
        return None

    def decoderDecode(self, _decoder, _buffer, _size):
        return True

    def decoderGetVertexCount(self, _decoder):
        return self.vertex_count

    def decoderGetIndexCount(self, _decoder):
        return self.index_count

    def decoderReadAttribute(self, _decoder, attr_id, _component_type, _attr_type):
        return attr_id == 0

    def decoderGetAttributeByteLength(self, _decoder, _attr_id):
        return len(self.position_bytes)

    def decoderCopyAttribute(self, _decoder, _attr_id, buffer):
        ctypes.memmove(ctypes.addressof(buffer), self.position_bytes, len(self.position_bytes))

    def decoderReadIndices(self, _decoder, _component_type):
        return self.read_indices

    def decoderGetIndicesByteLength(self, _decoder):
        return self.index_byte_length

    def decoderCopyIndices(self, _decoder, buffer):
        ctypes.memmove(
            ctypes.addressof(buffer),
            self.index_bytes,
            min(len(self.index_bytes), ctypes.sizeof(buffer)),
        )


class _FakeHttpResponse:
    def __init__(self, data=b"", headers=None):
        self._data = data
        self.headers = headers or {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False


class _FakeHttpOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def open(self, _request, timeout=None):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _make_http_error(code, headers=None, body=b""):
    return urllib.error.HTTPError(
        "https://example.test/mesh",
        code,
        "error",
        headers or {},
        io.BytesIO(body),
    )


def _make_vertex(px, py, pz, tangent=(127, 127, 0, 254), color=(255, 255, 255, 255)):
    return struct.pack(
        "<8f4B4B",
        px,
        py,
        pz,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        *tangent,
        *color,
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


def _make_reordered_name_table():
    # Bones use name_index=0 ("Jaw") and name_index=5 ("Root"). Pad so
    # "Root" lands at byte offset 5, matching _make_bone(5).
    return b"Jaw\0\0Root\0"


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


def _make_v4_mesh(name_table=None):
    name_table = name_table if name_table is not None else _make_name_table()
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


def _make_v4_mesh_with_reordered_name_table():
    return _make_v4_mesh(name_table=_make_reordered_name_table())


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


def _make_v5_mesh(name_table=None):
    name_table = name_table if name_table is not None else _make_name_table()
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


def _make_v6_mesh(name_table=None):
    name_table = name_table if name_table is not None else _make_name_table()
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


def _make_v6_mesh_with_reordered_name_table():
    return _make_v6_mesh(name_table=_make_reordered_name_table())


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


def _make_v6_mesh_with_facs_reordered():
    name_table = _make_reordered_name_table()
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


def _make_v7_mesh(name_table=None):
    name_table = name_table if name_table is not None else _make_name_table()
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

    def test_parse_v1_ascii_comma_separated_groups(self):
        # Old v1.00 assets (e.g. the classic "Spiky Hair Mesh", asset
        # 16627529) pack one comma-separated triplet per bracket group:
        # "[x,y,z][nx,ny,nz][u,v,0]".  The tokenizer must split each group
        # on commas or every value is silently dropped and the mesh parses
        # to zero geometry.  Spec quirks also apply: v1.00 positions are
        # halved and tex_V is flipped.
        data = (
            b"version 1.00\n"
            b"1\n"
            b"[0,0,0][0,0,1][0,0,0]"
            b"[1,0,0][0,0,1][1,0,0]"
            b"[0,1,0][0,0,1][0,1,0]\n"
        )
        parsed = parse_filemesh(data)
        self.assertEqual(parsed["num_vertices"], 3)
        self.assertEqual(len(parsed["faces"]), 1)
        self.assertEqual(parsed["positions"][1], (0.5, 0.0, 0.0))
        self.assertEqual(parsed["uvs"][2], (0.0, 0.0))
        self.assertEqual(parsed["normals"][1], (0.0, 0.0, 1.0))

    def test_parse_v1_ascii_single_float_groups(self):
        # The even older layout wraps EACH float in its own brackets.
        data = (
            b"version 1.00\n"
            b"1\n"
            b"[0][0][0][0][0][1][0][0][0]"
            b"[1][0][0][0][0][1][1][0][0]"
            b"[0][1][0][0][0][1][0][1][0]\n"
        )
        parsed = parse_filemesh(data)
        self.assertEqual(parsed["num_vertices"], 3)
        self.assertEqual(len(parsed["faces"]), 1)
        self.assertEqual(parsed["positions"][2], (0.0, 0.5, 0.0))
        self.assertEqual(parsed["uvs"][0], (0.0, 1.0))
        self.assertEqual(parsed["normals"][2], (0.0, 0.0, 1.0))

    def test_parse_v1_01_keeps_scale_and_flips_v(self):
        # v1.01 corrected the 2x size quirk but kept the upside-down tex_V.
        data = (
            b"version 1.01\n"
            b"1\n"
            b"[0,0,0][0,0,1][0,0,0]"
            b"[2,0,0][0,0,1][1,0,0]"
            b"[0,2,0][0,0,1][0,1,0]\n"
        )
        parsed = parse_filemesh(data)
        self.assertEqual(parsed["num_vertices"], 3)
        self.assertEqual(len(parsed["faces"]), 1)
        self.assertEqual(parsed["positions"][1], (2.0, 0.0, 0.0))
        self.assertEqual(parsed["uvs"][1], (1.0, 1.0))
        self.assertEqual(parsed["normals"][0], (0.0, 0.0, 1.0))

    def test_bundled_rbxasset_meshes_resolve_without_install(self):
        # Every builtin uri the pipeline synthesizes must resolve to the
        # addon's bundled copy (no local Roblox install required).
        uris = [
            "rbxasset://fonts/head.mesh",
            "rbxasset://fonts/torso.mesh",
            "rbxasset://fonts/leftarm.mesh",
            "rbxasset://fonts/rightarm.mesh",
            "rbxasset://fonts/leftleg.mesh",
            "rbxasset://fonts/rightleg.mesh",
            "rbxasset://avatar/compositing/R15CompositTorsoBase.mesh",
            "rbxasset://avatar/compositing/R15CompositLeftArmBase.mesh",
            "rbxasset://avatar/compositing/R15CompositRightArmBase.mesh",
            "rbxasset://avatar/compositing/CompositTorsoBase.mesh",
            "rbxasset://avatar/compositing/CompositLeftArmBase.mesh",
            "rbxasset://avatar/compositing/CompositRightArmBase.mesh",
            "rbxasset://avatar/compositing/CompositLeftLegBase.mesh",
            "rbxasset://avatar/compositing/CompositRightLegBase.mesh",
            "rbxasset://avatar/compositing/CompositShirtTemplate.mesh",
            "rbxasset://avatar/compositing/CompositPantsTemplate.mesh",
            "rbxasset://textures/face.png",
        ]
        for uri in uris:
            path = filemesh._resolve_rbxasset_path(uri)
            self.assertIsNotNone(path, f"no bundled copy for {uri}")
            self.assertTrue(path.is_file(), f"bundled copy missing for {uri}")
            self.assertIn("assets", path.parts)

    def test_release_import_cache_discards_raw_and_parsed_meshes(self):
        filemesh._FILEMESH_BYTES_CACHE["raw"] = b"mesh"
        filemesh._FILEMESH_CACHE["parsed"] = {"vertices": [(0, 0, 0)]}
        filemesh.release_import_cache()
        self.assertFalse(filemesh._FILEMESH_BYTES_CACHE)
        self.assertFalse(filemesh._FILEMESH_CACHE)

    def test_imported_content_cannot_fetch_non_roblox_url(self):
        self.assertTrue(filemesh.is_trusted_roblox_asset_url("https://tr.rbxcdn.com/asset"))
        self.assertFalse(filemesh.is_trusted_roblox_asset_url("http://localhost/asset"))
        self.assertFalse(filemesh.is_trusted_roblox_asset_url("https://roblox.com.evil.test/asset"))
        with self.assertRaisesRegex(ValueError, "non-Roblox mesh URL"):
            filemesh.fetch_filemesh_bytes("http://127.0.0.1:31337/private")
        with self.assertRaisesRegex(ValueError, "local mesh path"):
            filemesh.fetch_filemesh_bytes(
                "file:///C:/not-a-roblox-mesh.mesh", allow_local_paths=False
            )

    def test_fetch_url_response_retries_rate_limit_with_retry_after(self):
        opener = _FakeHttpOpener(
            [
                _make_http_error(429, {"Retry-After": "0"}, b"rate limited"),
                _FakeHttpResponse(b"version 4.00\n", {}),
            ]
        )

        with mock.patch.object(filemesh.urllib.request, "build_opener", return_value=opener):
            with mock.patch.object(filemesh.time, "sleep") as sleep_mock:
                _response, data = filemesh._fetch_url_response("https://example.test/mesh")

        self.assertEqual(data, b"version 4.00\n")
        self.assertEqual(opener.calls, 2)
        sleep_mock.assert_called_once_with(0.0)

    def test_fetch_url_response_does_not_retry_forbidden(self):
        opener = _FakeHttpOpener([_make_http_error(403, body=b"forbidden")])

        with mock.patch.object(filemesh.urllib.request, "build_opener", return_value=opener):
            with mock.patch.object(filemesh.time, "sleep") as sleep_mock:
                with self.assertRaises(urllib.error.HTTPError):
                    filemesh._fetch_url_response("https://example.test/mesh")

        self.assertEqual(opener.calls, 1)
        sleep_mock.assert_not_called()

    def test_authenticated_redirect_does_not_forward_bearer_to_cdn(self):
        delivery = _FakeHttpOpener(
            [_FakeHttpResponse(b"", {"Location": "https://cdn.example.test/mesh"})]
        )
        cdn = _FakeHttpOpener([_FakeHttpResponse(b"version 4.00\\n", {})])
        openers = []

        def build_opener(*handlers):
            opener = delivery if handlers else cdn
            openers.append((handlers, opener))
            return opener

        with mock.patch.object(filemesh.urllib.request, "build_opener", side_effect=build_opener):
            data = filemesh._fetch_url_bytes(
                "https://apis.roblox.com/asset-delivery-api/v1/assetId/123",
                extra_headers={"Authorization": "Bearer secret"},
            )

        self.assertEqual(data, b"version 4.00\\n")
        self.assertEqual(len(openers), 2)
        self.assertTrue(openers[0][0])  # delivery request has redirects disabled
        self.assertFalse(openers[1][0])  # signed CDN request is anonymous

    def test_failed_authenticated_fetch_does_not_repeat_public_endpoints(self):
        with mock.patch.object(
            filemesh, "_try_delivery_urls", return_value=None
        ) as delivery_mock:
            with mock.patch.object(
                filemesh, "_fetch_url_bytes", side_effect=RuntimeError("missing")
            ):
                with self.assertRaises(RuntimeError):
                    filemesh.fetch_filemesh_bytes(
                        "rbxassetid://123",
                        auth_headers={"Authorization": "Bearer secret"},
                    )

        self.assertEqual(delivery_mock.call_count, 2)
        first_urls = delivery_mock.call_args_list[0].args[0]
        public_urls = delivery_mock.call_args_list[1].args[0]
        self.assertEqual(first_urls, [
            "https://apis.roblox.com/asset-delivery-api/v1/assetId/123"
        ])
        self.assertEqual(public_urls, [
            "https://assetdelivery.roblox.com/v2/assetId/123",
            "https://assetdelivery.roblox.com/v1/asset/?id=123",
        ])

    def test_parse_v4_skinning(self):
        parsed = parse_filemesh(_make_v4_mesh())
        self.assertEqual(parsed["version"], "version 4.00")
        self.assertEqual(parsed["bone_names"], ["Root", "Jaw"])
        self.assertEqual(len(parsed["positions"]), 2)
        self.assertEqual(parsed["faces"], [(0, 1, 1)])
        self.assertEqual(parsed["normals"][0], (0.0, 1.0, 0.0))
        self.assertEqual(parsed["uvs"][1], (0.0, 0.0))
        self.assertEqual(parsed["tangent_bytes"][0], (127, 127, 0, 254))
        self.assertEqual(parsed["tangents"][0], (0.0, 0.0, -1.0, 1.0))
        self.assertEqual(parsed["tangent_signs"][0], 1.0)
        self.assertEqual(parsed["tangent_sign_bytes"][0], 254)
        self.assertEqual(parsed["color_bytes"][1], (255, 255, 255, 255))
        self.assertEqual(parsed["colors"][1], (1.0, 1.0, 1.0, 1.0))
        self.assertAlmostEqual(parsed["vertex_weights"][0]["Root"], 1.0)
        self.assertAlmostEqual(parsed["vertex_weights"][1]["Jaw"], 1.0)

    def test_parse_v4_skinning_uses_bone_array_index_not_name_table_order(self):
        parsed = parse_filemesh(_make_v4_mesh_with_reordered_name_table())
        self.assertEqual(parsed["bone_names"], ["Jaw", "Root"])
        self.assertAlmostEqual(parsed["vertex_weights"][0]["Jaw"], 1.0)
        self.assertAlmostEqual(parsed["vertex_weights"][1]["Root"], 1.0)

    def test_parse_v6_skinning_chunk(self):
        parsed = parse_filemesh(_make_v6_mesh())
        self.assertEqual(parsed["version"], "version 6.00")
        self.assertEqual(parsed["bone_names"], ["Root", "Jaw"])
        self.assertEqual(len(parsed["positions"]), 2)
        self.assertEqual(parsed["faces"], [(0, 1, 1)])
        self.assertEqual(parsed["normals"][1], (0.0, 1.0, 0.0))
        self.assertAlmostEqual(parsed["vertex_weights"][0]["Root"], 1.0)
        self.assertAlmostEqual(parsed["vertex_weights"][1]["Jaw"], 1.0)

    def test_parse_v6_skinning_uses_bone_array_index_not_name_table_order(self):
        parsed = parse_filemesh(_make_v6_mesh_with_reordered_name_table())
        self.assertEqual(parsed["bone_names"], ["Jaw", "Root"])
        self.assertAlmostEqual(parsed["vertex_weights"][0]["Jaw"], 1.0)
        self.assertAlmostEqual(parsed["vertex_weights"][1]["Root"], 1.0)

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
            {
                "position": (0.0, 0.0, 0.0),
                "normal": (0.0, 1.0, 0.0),
                "uv": (0.0, 0.0),
                "tangent": (0.0, 0.0, -1.0, 1.0),
                "tangent_bytes": (127, 127, 0, 254),
                "tangent_sign": 1.0,
                "tangent_sign_byte": 254,
                "color": (1.0, 128.0 / 255.0, 0.0, 1.0),
                "color_bytes": (255, 128, 0, 255),
            },
            {
                "position": (1.0, 0.0, 0.0),
                "normal": (0.0, 1.0, 0.0),
                "uv": (1.0, 0.0),
                "tangent": (0.0, 0.0, -1.0, 1.0),
                "tangent_bytes": (127, 127, 0, 254),
                "tangent_sign": 1.0,
                "tangent_sign_byte": 254,
                "color": (0.0, 128.0 / 255.0, 1.0, 1.0),
                "color_bytes": (0, 128, 255, 255),
            },
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
            {
                "position": (0.0, 0.0, 0.0),
                "normal": (0.0, 1.0, 0.0),
                "uv": (0.0, 0.0),
                "tangent": (0.0, 0.0, -1.0, 1.0),
                "tangent_bytes": (127, 127, 0, 254),
                "tangent_sign": 1.0,
                "tangent_sign_byte": 254,
                "color": (1.0, 128.0 / 255.0, 0.0, 1.0),
                "color_bytes": (255, 128, 0, 255),
            },
            {
                "position": (1.0, 0.0, 0.0),
                "normal": (0.0, 1.0, 0.0),
                "uv": (1.0, 0.0),
                "tangent": (0.0, 0.0, -1.0, 1.0),
                "tangent_bytes": (127, 127, 0, 254),
                "tangent_sign": 1.0,
                "tangent_sign_byte": 254,
                "color": (0.0, 128.0 / 255.0, 1.0, 1.0),
                "color_bytes": (0, 128, 255, 255),
            },
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
        self.assertEqual(parsed["tangent_bytes"][0], (127, 127, 0, 254))
        self.assertEqual(parsed["tangents"][0], (0.0, 0.0, -1.0, 1.0))
        self.assertEqual(parsed["tangent_signs"][0], 1.0)
        self.assertEqual(parsed["tangent_sign_bytes"][0], 254)
        self.assertEqual(parsed["color_bytes"][1], (0, 128, 255, 255))
        self.assertEqual(parsed["colors"][1], (0.0, 128.0 / 255.0, 1.0, 1.0))
        self.assertEqual(parsed["faces"], [(0, 1, 1)])

    def test_parse_v7_rejects_trailing_draco_coremesh_payload(self):
        coremesh = struct.pack("<I", 4) + b"DRCO" + b"x"
        data = b"version 7.00\n" + b"COREMESH" + struct.pack("<II", 2, len(coremesh)) + coremesh

        with self.assertRaisesRegex(ValueError, "unexpected trailing bytes"):
            parse_filemesh(data)

    def test_parse_v7_rejects_unreadable_draco_indices(self):
        fake_dll = _FakeDracoDll(read_indices=False)

        with mock.patch.object(filemesh, "_load_blender_draco_dll", return_value=fake_dll):
            with self.assertRaisesRegex(RuntimeError, "indices but they could not be read"):
                filemesh._decode_draco_coremesh_v2(struct.pack("<I", 4) + b"DRCO")

    def test_parse_v7_rejects_short_draco_index_buffer(self):
        fake_dll = _FakeDracoDll(index_byte_length=4)

        with mock.patch.object(filemesh, "_load_blender_draco_dll", return_value=fake_dll):
            with self.assertRaisesRegex(RuntimeError, "index buffer length"):
                filemesh._decode_draco_coremesh_v2(struct.pack("<I", 4) + b"DRCO")

    def test_parse_v7_rejects_out_of_range_draco_index(self):
        fake_dll = _FakeDracoDll(index_values=(0, 1, 2))

        with mock.patch.object(filemesh, "_load_blender_draco_dll", return_value=fake_dll):
            with self.assertRaisesRegex(RuntimeError, "missing vertex"):
                filemesh._decode_draco_coremesh_v2(struct.pack("<I", 4) + b"DRCO")

    def test_parse_v7_rejects_skinning_coremesh_vertex_count_mismatch(self):
        decoded_vertices = [
            {
                "position": (0.0, 0.0, 0.0),
                "normal": None,
                "uv": None,
                "tangent": None,
                "tangent_bytes": None,
                "tangent_sign": None,
                "tangent_sign_byte": None,
                "color": None,
                "color_bytes": None,
            }
        ]

        with mock.patch.object(
            filemesh,
            "_decode_draco_coremesh_v2",
            return_value=(decoded_vertices, [], 1),
        ):
            with self.assertRaisesRegex(ValueError, "skinning vertex count 2 does not match coremesh vertex count 1"):
                parse_filemesh(_make_v7_mesh())

    def test_parse_v6_rejects_truncated_chunk_payload(self):
        data = b"version 6.00\n" + b"COREMESH" + struct.pack("<II", 1, 16) + b"abc"

        with self.assertRaisesRegex(ValueError, "truncated COREMESH chunk payload"):
            parse_filemesh(data)

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

    def test_parse_v6_facs_chunk_keeps_name_keyed_weights_with_reordered_name_table(self):
        parsed = parse_filemesh(_make_v6_mesh_with_facs_reordered())
        self.assertEqual(parsed["bone_names"], ["Jaw", "Root"])
        self.assertAlmostEqual(parsed["vertex_weights"][0]["Jaw"], 1.0)
        self.assertAlmostEqual(parsed["vertex_weights"][1]["Root"], 1.0)

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

    def test_depsgraph_face_controls_handler_skips_during_playback(self):
        scene = mock.Mock()
        scene.frame_current_final = 12.0

        previous_guard = ui_properties._FACE_CONTROL_DEPSGRAPH_APPLYING
        ui_properties._FACE_CONTROL_DEPSGRAPH_APPLYING = False
        try:
            with mock.patch.object(ui_properties, "_face_controls_playback_active", return_value=True):
                with mock.patch.object(ui_properties, "iter_active_facs_armatures", return_value=[mock.Mock()]):
                    with mock.patch.object(ui_properties, "apply_facs_properties_to_armature") as apply_mock:
                        ui_properties._depsgraph_face_controls_handler(scene, mock.Mock())

            apply_mock.assert_not_called()
        finally:
            ui_properties._FACE_CONTROL_DEPSGRAPH_APPLYING = previous_guard

    def test_parse_gzipped_filemesh(self):
        parsed = parse_filemesh(gzip.compress(_make_v4_mesh()))
        self.assertEqual(parsed["version"], "version 4.00")
        self.assertAlmostEqual(parsed["vertex_weights"][0]["Root"], 1.0)

    def test_parse_filemesh_with_prefixed_bytes(self):
        parsed = parse_filemesh(b"junk-prefix" + _make_v6_mesh())
        self.assertEqual(parsed["version"], "version 6.00")
        self.assertAlmostEqual(parsed["vertex_weights"][1]["Jaw"], 1.0)


class TestContentDirDetection(unittest.TestCase):
    """Cross-platform install-layout detection (no real Studio required)."""

    def _content(self, base):
        path = base / "content"
        path.mkdir(parents=True, exist_ok=True)
        (path / "fonts").mkdir(parents=True, exist_ok=True)
        return path

    def test_windows_newest_version_first(self):
        import os
        import tempfile
        import time
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = self._content(root / "Roblox" / "Versions" / "old")
            new = self._content(root / "Roblox" / "Versions" / "newer")
            future = time.time() + 3600
            os.utime(new.parent, (future, future))
            candidates = filemesh._install_content_dirs(
                "win32", root, {"LOCALAPPDATA": tmp}
            )
            self.assertEqual(candidates[0], new)
            self.assertIn(old, candidates)

    def test_linux_vinegar_versions(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = self._content(
                root / ".vinegar" / "data" / "vinegar" / "versions" / "abc123"
            )
            candidates = filemesh._install_content_dirs("linux", root, {})
            self.assertEqual(candidates[0], content)

    def test_linux_bare_wine_users(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = self._content(
                root / ".wine" / "drive_c" / "users" / "bob"
                / "AppData" / "Local" / "Roblox" / "Versions" / "v"
            )
            candidates = filemesh._install_content_dirs("linux", root, {})
            self.assertEqual(candidates[0], content)

    def test_linux_grapejuice_prefixes(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = self._content(
                root / ".local" / "share" / "grapejuice" / "prefixes" / "player"
                / "drive_c" / "users" / "alice"
                / "AppData" / "Local" / "Roblox" / "Versions" / "v"
            )
            candidates = filemesh._install_content_dirs("linux", root, {})
            self.assertEqual(candidates[0], content)

    def test_darwin_vinegar_versions(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = self._content(
                root / "Library" / "Application Support" / "Vinegar"
                / "Versions" / "abc"
            )
            candidates = filemesh._install_content_dirs("darwin", root, {})
            self.assertIn(content, candidates)


if __name__ == "__main__":
    unittest.main()
