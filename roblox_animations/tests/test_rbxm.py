"""Round-trip tests for the rbxm binary parser.

Builds a synthetic .rbxm in-memory (with a matching encoder that mirrors the
parser's transforms) and asserts the parser recovers the exact scene context.
"""

from __future__ import annotations

import math
import struct
import unittest
from unittest import mock

from roblox_animations.core.rbxm import (
    RbxmError,
    _decode_attributes_blob,
    parse_rbxm,
    rbxm_to_part_aux,
)


# ---------------------------------------------------------------------------
# Encoder helpers (mirror the parser's transforms)
# ---------------------------------------------------------------------------


def _transform_i32(value: int) -> int:
    return ((value << 1) ^ (value >> 31)) & 0xFFFFFFFF


def _interleave(values: bytes, count: int, size: int) -> bytes:
    out = bytearray(count * size)
    for value_index in range(count):
        for byte_index in range(size):
            src = value_index * size + byte_index
            dst = byte_index * count + value_index
            out[dst] = values[src]
    return bytes(out)


def _ieee_to_roblox_float(f: float) -> int:
    bits = struct.unpack("<I", struct.pack("<f", f))[0]
    # Inverse of parser's rotate-right: rotate left by one.
    return (((bits << 1) & 0xFFFFFFFF) | (bits >> 31)) & 0xFFFFFFFF


def _enc_interleaved_i32(values):
    raw = b"".join(struct.pack(">I", _transform_i32(v)) for v in values)
    return _interleave(raw, len(values), 4)


def _enc_interleaved_f32(values):
    raw = b"".join(struct.pack(">I", _ieee_to_roblox_float(v)) for v in values)
    return _interleave(raw, len(values), 4)


def _enc_referents(values):
    deltas = []
    prev = 0
    for v in values:
        deltas.append(v - prev)
        prev = v
    return _enc_interleaved_i32(deltas)


def _enc_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


_BASIC_ROT_IDS = {
    (1, 0, 0, 0, 1, 0, 0, 0, 1): 0x02,
}


def _enc_cframes(cframes):
    # Each cframe is (x, y, z, r00..r22). Encode identity via id, others raw.
    rot_bytes = bytearray()
    positions = []
    for cf in cframes:
        rot = tuple(round(c, 6) for c in cf[3:12])
        ident = _BASIC_ROT_IDS.get(rot)
        if ident is not None:
            rot_bytes.append(ident)
        else:
            rot_bytes.append(0)
            rot_bytes += struct.pack("<9f", *cf[3:12])
        positions.append((cf[0], cf[1], cf[2]))
    out = bytes(rot_bytes)
    out += _enc_interleaved_f32([p[0] for p in positions])
    out += _enc_interleaved_f32([p[1] for p in positions])
    out += _enc_interleaved_f32([p[2] for p in positions])
    return out


def _enc_vector3s(vectors):
    out = _enc_interleaved_f32([v[0] for v in vectors])
    out += _enc_interleaved_f32([v[1] for v in vectors])
    out += _enc_interleaved_f32([v[2] for v in vectors])
    return out


def _enc_contents(values):
    source_types = [1 if v else 0 for v in values]
    uris = [v for v in values if v]
    out = _enc_interleaved_i32(source_types)
    out += struct.pack("<I", len(uris))
    for uri in uris:
        out += _enc_string(uri)
    out += struct.pack("<I", 0)  # object count
    out += struct.pack("<I", 0)  # external count
    return out


def _enc_sstr(blobs):
    out = struct.pack("<II", 0, len(blobs))
    for blob in blobs:
        out += b"\0" * 16  # MD5 hash (unused)
        out += struct.pack("<I", len(blob))
        out += blob
    return out


def _enc_binary(value: bytes) -> bytes:
    return struct.pack("<I", len(value)) + value


def _enc_attribute_blob(attrs):
    """Encode string-valued attributes only (what the exporter stamps)."""
    out = struct.pack("<I", len(attrs))
    for key, value in attrs:
        out += struct.pack("<I", len(key)) + key.encode("utf-8")
        out += struct.pack("<B", 0x02)  # attribute string type id
        out += struct.pack("<I", len(value)) + value.encode("utf-8")
    return out


def _enc_shared_string_indices(indices):
    return _interleave(
        b"".join(struct.pack(">I", i) for i in indices), len(indices), 4
    )


def _enc_enums(values):
    return _interleave(
        b"".join(struct.pack(">I", v) for v in values), len(values), 4
    )


def _enc_interleaved_i64(values):
    return _interleave(
        b"".join(struct.pack(">q", v) for v in values), len(values), 8
    )


def _transform_i64(value):
    return ((value << 1) ^ (value >> 63)) & 0xFFFFFFFFFFFFFFFF


def _enc_interleaved_i64_zigzag(values):
    return _interleave(
        b"".join(struct.pack(">Q", _transform_i64(v)) for v in values), len(values), 8
    )


def _chunk(name: bytes, body: bytes) -> bytes:
    header = name.ljust(4, b"\0")[:4]
    return header + struct.pack("<II", 0, len(body)) + b"\0\0\0\0" + body


def _inst_chunk(class_id, class_name, referents, object_format=0):
    body = struct.pack("<I", class_id)
    body += _enc_string(class_name)
    body += struct.pack("<B", object_format)
    body += struct.pack("<I", len(referents))
    body += _enc_referents(referents)
    if object_format == 1:
        body += b"\x01" * len(referents)
    return _chunk(b"INST", body)


def _prop_chunk(class_id, prop_name, type_id, encoded_values):
    body = struct.pack("<I", class_id)
    body += _enc_string(prop_name)
    body += struct.pack("<B", type_id)
    body += encoded_values
    return _chunk(b"PROP", body)


def _prnt_chunk(child_refs, parent_refs):
    body = struct.pack("<B", 0)
    body += struct.pack("<I", len(child_refs))
    body += _enc_referents(child_refs)
    body += _enc_referents(parent_refs)
    return _chunk(b"PRNT", body)


def _rbxm(chunks, class_count, instance_count):
    header = b"<roblox!" + b"\x89\xff\x0d\x0a\x1a\x0a"
    header += struct.pack("<Hii", 0, class_count, instance_count)
    header += b"\0" * 8
    end = _chunk(b"END", b"</roblox>")
    return header + b"".join(chunks) + end


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRbxmParsing(unittest.TestCase):
    def _build_character(self):
        """Build a tiny character: Model > UpperTorso (MeshPart) + Head (MeshPart w/ WrapLayer)."""
        # referents: 0 model, 1 torso, 2 head, 3 wraplayer (child of head)
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "MeshPart", [1, 2]))
        chunks.append(_inst_chunk(3, "WrapLayer", [3]))
        chunks.append(_prop_chunk(1, "PrimaryPart", 0x13, _enc_ref_prop([1])))

        # Names
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Character") * 1))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("UpperTorso") + _enc_string("Head")))
        chunks.append(_prop_chunk(3, "Name", 0x01, _enc_string("JacketLayer")))

        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
        torso_cf = (0.0, 1.0, 0.0) + identity
        head_cf = (0.0, 2.5, 0.0) + identity
        chunks.append(_prop_chunk(2, "CFrame", 0x10, _enc_cframes([torso_cf, head_cf])))

        torso_size = [2.0, 2.0, 1.0]
        head_size = [1.2, 1.2, 1.2]
        chunks.append(_prop_chunk(2, "Size", 0x0E, _enc_vector3s([torso_size, head_size])))
        chunks.append(_prop_chunk(2, "Transparency", 0x04, _enc_interleaved_f32([0.0, 0.35])))

        torso_meshsize = [2.0, 2.0, 1.0]
        head_meshsize = [1.0, 1.0, 1.0]
        chunks.append(_prop_chunk(2, "MeshSize", 0x0E, _enc_vector3s([torso_meshsize, head_meshsize])))

        chunks.append(
            _prop_chunk(
                2,
                "MeshId",
                0x22,
                _enc_contents(["rbxassetid://111", "rbxassetid://222"]),
            )
        )

        # WrapLayer props on head
        chunks.append(_prop_chunk(3, "Enabled", 0x02, b"\x01"))
        chunks.append(
            _prop_chunk(
                3,
                "ReferenceMeshId",
                0x22,
                _enc_contents(["rbxassetid://333"]),
            )
        )
        chunks.append(
            _prop_chunk(
                3,
                "CageMeshId",
                0x22,
                _enc_contents(["rbxassetid://444"]),
            )
        )

        # Parenting: model is root (-1), torso+head -> model, wraplayer -> head
        chunks.append(_prnt_chunk([0, 1, 2, 3], [-1, 0, 0, 2]))

        return _rbxm(chunks, class_count=3, instance_count=4)

    def test_round_trip_part_aux(self):
        data = self._build_character()
        entries = rbxm_to_part_aux(data)
        by_name = {e["name"]: e for e in entries}

        self.assertIn("UpperTorso", by_name)
        self.assertIn("Head", by_name)

        torso = by_name["UpperTorso"]
        self.assertEqual(torso["mesh_class"], "MeshPart")
        self.assertEqual(torso["mesh_id"], "rbxassetid://111")
        self.assertAlmostEqual(torso["part_cf"][1], 1.0)
        self.assertEqual(torso["part_size"], [2.0, 2.0, 1.0])
        self.assertEqual(torso["mesh_size"], [2.0, 2.0, 1.0])
        self.assertAlmostEqual(torso["vol_fp"], 4.0)
        self.assertEqual(torso["transparency"], 0.0)
        self.assertTrue(torso["is_primary_part"])

        head = by_name["Head"]
        self.assertEqual(head["mesh_id"], "rbxassetid://222")
        self.assertAlmostEqual(head["part_cf"][1], 2.5)
        self.assertAlmostEqual(head["transparency"], 0.35)
        self.assertNotIn("is_primary_part", head)
        self.assertIn("wrap_layer", head)
        wrap = head["wrap_layer"]
        self.assertEqual(wrap["name"], "JacketLayer")
        self.assertTrue(wrap["enabled"])
        self.assertEqual(wrap["reference_mesh_id"], "rbxassetid://333")
        self.assertEqual(wrap["cage_mesh_id"], "rbxassetid://444")

    def _build_r6_character(self, with_humanoid=True):
        """Plain-Part classic R6 body: Model (+ Humanoid) > Torso/limbs + Brick.

        Classic R6 files carry no mesh data on the body parts; the renderer
        substitutes the fonts meshes by part name at runtime.  The parser
        must do the same so imports get the character geometry.
        """
        # referents: 0 model, 1 humanoid, 2..7 the six parts
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "Part", [2, 3, 4, 5, 6, 7]))
        if with_humanoid:
            chunks.append(_inst_chunk(3, "Humanoid", [1]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Character")))
        chunks.append(
            _prop_chunk(
                2,
                "Name",
                0x01,
                _enc_string("Torso")
                + _enc_string("Left Arm")
                + _enc_string("Right Arm")
                + _enc_string("Left Leg")
                + _enc_string("Right Leg")
                + _enc_string("Brick"),
            )
        )
        size = [2.0, 2.0, 1.0]
        chunks.append(_prop_chunk(2, "Size", 0x0E, _enc_vector3s([size] * 6)))
        children = [0, 1, 2, 3, 4, 5, 6, 7]
        parents = [-1, 0, 0, 0, 0, 0, 0, 0]
        chunks.append(_prnt_chunk(children, parents))
        class_count = 3 if with_humanoid else 2
        return _rbxm(chunks, class_count=class_count, instance_count=8)

    def test_r6_classic_body_parts_get_fonts_meshes(self):
        entries = rbxm_to_part_aux(self._build_r6_character())
        by_name = {e["name"]: e for e in entries}
        expected = {
            "Torso": "rbxasset://fonts/torso.mesh",
            "Left Arm": "rbxasset://fonts/leftarm.mesh",
            "Right Arm": "rbxasset://fonts/rightarm.mesh",
            "Left Leg": "rbxasset://fonts/leftleg.mesh",
            "Right Leg": "rbxasset://fonts/rightleg.mesh",
        }
        for part_name, mesh_id in expected.items():
            entry = by_name[part_name]
            self.assertEqual(entry["mesh_id"], mesh_id)
            self.assertEqual(entry["mesh_class"], "MeshPart")
            self.assertNotIn("shape", entry)
            self.assertEqual(entry["part_size"], [2.0, 2.0, 1.0])
        # Non-body parts under the same model stay primitives.
        self.assertNotIn("mesh_id", by_name["Brick"])
        self.assertEqual(by_name["Brick"].get("shape"), "block")

    def test_r6_parts_without_humanoid_stay_primitives(self):
        entries = rbxm_to_part_aux(self._build_r6_character(with_humanoid=False))
        by_name = {e["name"]: e for e in entries}
        self.assertNotIn("mesh_id", by_name["Torso"])
        self.assertEqual(by_name["Torso"].get("shape"), "block")

    def _build_r6_character_mesh(self, mesh_id=None, body_part=None):
        """A Part with a CharacterMesh child (the SpecialMesh replacement)."""
        # referents: 0 model, 1 humanoid, 2 torso part, 3 charactermesh
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "Part", [2]))
        chunks.append(_inst_chunk(3, "Humanoid", [1]))
        chunks.append(_inst_chunk(4, "CharacterMesh", [3]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Character")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("Torso")))
        chunks.append(
            _prop_chunk(2, "Size", 0x0E, _enc_vector3s([[2.0, 2.0, 1.0]]))
        )
        if mesh_id is not None:
            chunks.append(_prop_chunk(4, "MeshId", 0x22, _enc_contents([mesh_id])))
        if body_part is not None:
            chunks.append(_prop_chunk(4, "BodyPart", 0x12, _enc_enums([body_part])))
        chunks.append(_prnt_chunk([0, 1, 2, 3], [-1, 0, 0, 2]))
        return _rbxm(chunks, class_count=4, instance_count=4)

    def test_character_mesh_explicit_meshid_wins_over_classic(self):
        entries = rbxm_to_part_aux(
            self._build_r6_character_mesh(mesh_id="rbxassetid://999", body_part=1)
        )
        by_name = {e["name"]: e for e in entries}
        torso = by_name["Torso"]
        self.assertEqual(torso["mesh_id"], "rbxassetid://999")
        self.assertEqual(torso["mesh_class"], "MeshPart")
        self.assertNotIn("shape", torso)
        self.assertEqual(torso["character_mesh"]["body_part"], 1)

    def test_character_mesh_without_meshid_falls_back_to_bodypart_mesh(self):
        entries = rbxm_to_part_aux(self._build_r6_character_mesh(body_part=3))
        by_name = {e["name"]: e for e in entries}
        torso = by_name["Torso"]
        self.assertEqual(torso["mesh_id"], "rbxasset://fonts/rightarm.mesh")
        self.assertEqual(torso["mesh_class"], "MeshPart")
        self.assertEqual(torso["character_mesh"]["body_part"], 3)

    def _build_root_character_mesh_character(self, mesh_id=None, body_part=None):
        """Model (+Humanoid) with Parts and a CharacterMesh at the MODEL ROOT.

        CharacterMesh instances target their body part through BodyPart and
        sit beside the parts, not under them (R6CharacterAssembler layout).
        """
        # referents: 0 model, 1 humanoid, 2 torso, 3 right arm, 4 charactermesh
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "Part", [2, 3]))
        chunks.append(_inst_chunk(3, "Humanoid", [1]))
        chunks.append(_inst_chunk(4, "CharacterMesh", [4]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Character")))
        chunks.append(
            _prop_chunk(2, "Name", 0x01, _enc_string("Torso") + _enc_string("Right Arm"))
        )
        chunks.append(
            _prop_chunk(2, "Size", 0x0E, _enc_vector3s([[2, 2, 1], [1, 2, 1]]))
        )
        if mesh_id is not None:
            chunks.append(_prop_chunk(4, "MeshId", 0x22, _enc_contents([mesh_id])))
        if body_part is not None:
            chunks.append(_prop_chunk(4, "BodyPart", 0x12, _enc_enums([body_part])))
        chunks.append(_prnt_chunk([0, 1, 2, 3, 4], [-1, 0, 0, 0, 0]))
        return _rbxm(chunks, class_count=4, instance_count=5)

    def test_root_level_character_mesh_applies_to_its_body_part(self):
        entries = rbxm_to_part_aux(
            self._build_root_character_mesh_character(
                mesh_id="rbxassetid://999", body_part=1
            )
        )
        by_name = {e["name"]: e for e in entries}
        torso = by_name["Torso"]
        self.assertEqual(torso["mesh_id"], "rbxassetid://999")
        self.assertEqual(torso["mesh_class"], "MeshPart")
        self.assertNotIn("shape", torso)
        self.assertEqual(torso["character_mesh"]["body_part"], 1)
        # The other limb has no CharacterMesh: classic name fallback applies.
        self.assertEqual(by_name["Right Arm"]["mesh_id"], "rbxasset://fonts/rightarm.mesh")

    def test_root_level_character_mesh_without_meshid_uses_bodypart_mesh(self):
        entries = rbxm_to_part_aux(
            self._build_root_character_mesh_character(body_part=3)
        )
        by_name = {e["name"]: e for e in entries}
        arm = by_name["Right Arm"]
        self.assertEqual(arm["mesh_id"], "rbxasset://fonts/rightarm.mesh")
        self.assertEqual(arm["mesh_class"], "MeshPart")
        self.assertEqual(arm["character_mesh"]["body_part"], 3)
        # Torso is untouched: classic name fallback.
        self.assertEqual(by_name["Torso"]["mesh_id"], "rbxasset://fonts/torso.mesh")

    def _build_character_mesh_prop_character(self, prop_chunks):
        """Model (+Humanoid) with a Torso and one root CharacterMesh whose
        mesh id is stored via caller-supplied property chunks."""
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "Part", [1]))
        chunks.append(_inst_chunk(3, "Humanoid", [2]))
        chunks.append(_inst_chunk(4, "CharacterMesh", [3]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Character")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("Torso")))
        chunks.append(_prop_chunk(2, "Size", 0x0E, _enc_vector3s([[2, 2, 1]])))
        chunks.append(_prop_chunk(4, "BodyPart", 0x12, _enc_enums([1])))
        chunks.extend(prop_chunks)
        chunks.append(_prnt_chunk([0, 1, 2, 3], [-1, 0, 0, 0]))
        return _rbxm(chunks, class_count=4, instance_count=4)

    def test_character_mesh_meshcontent_content_prop_is_read(self):
        entries = rbxm_to_part_aux(
            self._build_character_mesh_prop_character(
                [_prop_chunk(4, "MeshContent", 0x22, _enc_contents(["rbxassetid://888"]))]
            )
        )
        torso = {e["name"]: e for e in entries}["Torso"]
        self.assertEqual(torso["mesh_id"], "rbxassetid://888")
        self.assertEqual(torso["mesh_class"], "MeshPart")
        self.assertEqual(torso["character_mesh"]["body_part"], 1)

    def test_character_mesh_legacy_meshid_int64_is_read(self):
        entries = rbxm_to_part_aux(
            self._build_character_mesh_prop_character(
                [_prop_chunk(4, "MeshId", 0x1B, _enc_interleaved_i64_zigzag([94152122489071]))]
            )
        )
        torso = {e["name"]: e for e in entries}["Torso"]
        self.assertEqual(torso["mesh_id"], "rbxassetid://94152122489071")
        self.assertEqual(torso["mesh_class"], "MeshPart")

    def test_character_mesh_meshcontent_shared_string_is_read(self):
        entries = rbxm_to_part_aux(
            self._build_character_mesh_prop_character(
                [
                    _chunk(b"SSTR", _enc_sstr([b"rbxassetid://777"])),
                    _prop_chunk(
                        4, "MeshContent", 0x1C, _enc_shared_string_indices([0])
                    ),
                ]
            )
        )
        torso = {e["name"]: e for e in entries}["Torso"]
        self.assertEqual(torso["mesh_id"], "rbxassetid://777")
        self.assertEqual(torso["mesh_class"], "MeshPart")

    def _build_head_part(self, with_special_mesh=True, with_decal=True):
        """Head Part with a SpecialMesh (dynamic head) and/or a face Decal."""
        # referents: 0 model, 1 head, 2 specialmesh, 3 decal, 4 humanoid
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "Part", [1]))
        chunks.append(_inst_chunk(3, "Humanoid", [4]))
        class_count = 3
        refs = [0, 1, 4]
        parents = [-1, 0, 0]
        if with_special_mesh:
            chunks.append(_inst_chunk(4, "SpecialMesh", [2]))
            refs.append(2)
            parents.append(1)
            class_count = 4
        if with_decal:
            chunks.append(_inst_chunk(5, "Decal", [3]))
            refs.append(3)
            parents.append(1)
            class_count = 5
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Character")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("Head")))
        chunks.append(_prop_chunk(2, "Size", 0x0E, _enc_vector3s([[2.0, 1.0, 1.0]])))
        if with_special_mesh:
            chunks.append(
                _prop_chunk(4, "MeshId", 0x22, _enc_contents(["rbxassetid://999"]))
            )
            chunks.append(
                _prop_chunk(4, "TextureId", 0x22, _enc_contents(["rbxassetid://555"]))
            )
        if with_decal:
            chunks.append(
                _prop_chunk(
                    5, "Texture", 0x01, _enc_string("rbxasset://textures/face.png")
                )
            )
            chunks.append(_prop_chunk(5, "Face", 0x12, _enc_enums([5])))
            chunks.append(
                _prop_chunk(5, "Transparency", 0x04, _enc_interleaved_f32([0.25]))
            )
        chunks.append(_prnt_chunk(refs, parents))
        return _rbxm(chunks, class_count=class_count, instance_count=len(refs))

    def test_dynamic_head_face_decal_is_suppressed(self):
        entries = rbxm_to_part_aux(
            self._build_head_part(with_special_mesh=True, with_decal=True)
        )
        head = {e["name"]: e for e in entries}["Head"]
        self.assertEqual(head["mesh_id"], "rbxassetid://999")
        self.assertEqual(head["texture_id"], "rbxassetid://555")
        self.assertNotIn("face_decal", head)

    def test_classic_head_keeps_face_decal(self):
        entries = rbxm_to_part_aux(
            self._build_head_part(with_special_mesh=False, with_decal=True)
        )
        head = {e["name"]: e for e in entries}["Head"]
        self.assertIn("face_decal", head)
        self.assertEqual(head["face_decal"]["texture"], "rbxasset://textures/face.png")
        self.assertAlmostEqual(head["face_decal"]["transparency"], 0.25, places=6)
        self.assertNotIn("texture_id", head)

    def test_classic_body_colors_instance_populates_hd_body_colors(self):
        # Classic characters keep body colors in a BodyColors instance, not
        # the HumanoidDescription. Legacy saves use Color3uint8 (0-255).
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "BodyColors", [1]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Character")))
        chunks.append(_prop_chunk(2, "TorsoColor3", 0x1A, bytes([200, 100, 50])))
        chunks.append(_prnt_chunk([0, 1], [-1, 0]))
        data = _rbxm(chunks, class_count=2, instance_count=2)
        meta = parse_rbxm(data)
        self.assertIn("hd_body_colors", meta)
        torso = meta["hd_body_colors"].get("torso")
        self.assertIsNotNone(torso)
        self.assertAlmostEqual(torso[0], 200 / 255, places=6)
        self.assertAlmostEqual(torso[1], 100 / 255, places=6)
        self.assertAlmostEqual(torso[2], 50 / 255, places=6)

    def test_texture_child_color3_type_0x0c(self):
        # Modern saves write Texture.Color3 under property type id 0x0c
        # (planar float32 triple).  The parser must decode it and keep it in
        # the texture_instance entry so the importer can tint the overlay.
        chunks = []
        chunks.append(_inst_chunk(1, "Part", [0]))
        chunks.append(_inst_chunk(2, "Texture", [1]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Wall")))
        chunks.append(_prop_chunk(1, "Size", 0x0E, _enc_vector3s([[8.0, 8.0, 1.0]])))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("WallTexture")))
        chunks.append(
            _prop_chunk(2, "Texture", 0x01, _enc_string("rbxassetid://11254907942"))
        )
        chunks.append(_prop_chunk(2, "StudsPerTileU", 0x04, _enc_interleaved_f32([30.0])))
        chunks.append(_prop_chunk(2, "Transparency", 0x04, _enc_interleaved_f32([0.0])))
        chunks.append(_prop_chunk(2, "Face", 0x12, _enc_enums([1])))
        r, g, b = [0.09803922474384308], [0.062745101749897], [0.02352941408753395]
        chunks.append(
            _prop_chunk(
                2,
                "Color3",
                0x0C,
                _enc_interleaved_f32(r) + _enc_interleaved_f32(g) + _enc_interleaved_f32(b),
            )
        )
        chunks.append(_prnt_chunk([1], [0]))

        data = _rbxm(chunks, class_count=2, instance_count=2)
        entries = rbxm_to_part_aux(data)
        self.assertEqual(len(entries), 1)
        instance = entries[0].get("texture_instance")
        self.assertIsNotNone(instance)
        self.assertEqual(instance["texture"], "rbxassetid://11254907942")
        self.assertEqual(instance["face"], 1)
        self.assertAlmostEqual(instance["studs_per_tile"], 30.0)
        self.assertEqual(len(instance["color"]), 3)
        self.assertAlmostEqual(instance["color"][0], 0.09803922474384308, places=6)
        self.assertAlmostEqual(instance["color"][1], 0.062745101749897, places=6)
        self.assertAlmostEqual(instance["color"][2], 0.02352941408753395, places=6)

    def test_multiple_texture_and_surfaceappearance_children_are_all_kept(self):
        # A part with three stacked Texture children and two stacked
        # SurfaceAppearance children must keep EVERY layer, bottom-first.
        # Dropping all but the first layer (the old _find_descendant_of_class
        # behaviour) rendered the stacked trim sheets as one wrong-looking
        # mesh.
        chunks = []
        chunks.append(_inst_chunk(1, "Part", [0]))
        chunks.append(_inst_chunk(2, "Texture", [1, 2, 3]))
        chunks.append(_inst_chunk(3, "SurfaceAppearance", [4, 5]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Wall")))
        chunks.append(_prop_chunk(1, "Size", 0x0E, _enc_vector3s([[8.0, 8.0, 1.0]])))
        chunks.append(
            _prop_chunk(
                2,
                "Name",
                0x01,
                _enc_string("LayerA") + _enc_string("LayerB") + _enc_string("LayerC"),
            )
        )
        chunks.append(
            _prop_chunk(
                2,
                "Texture",
                0x01,
                _enc_string("rbxassetid://111")
                + _enc_string("rbxassetid://222")
                + _enc_string("rbxassetid://333"),
            )
        )
        chunks.append(
            _prop_chunk(
                3,
                "Name",
                0x01,
                _enc_string("Bottom") + _enc_string("Top"),
            )
        )
        chunks.append(
            _prop_chunk(
                3,
                "ColorMap",
                0x01,
                _enc_string("rbxassetid://444") + _enc_string("rbxassetid://555"),
            )
        )
        chunks.append(_prop_chunk(3, "AlphaMode", 0x03, _enc_interleaved_i32([0, 1])))
        chunks.append(_prnt_chunk([1, 2, 3, 4, 5], [0, 0, 0, 0, 0]))

        data = _rbxm(chunks, class_count=3, instance_count=6)
        entries = rbxm_to_part_aux(data)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        texture_instances = entry.get("texture_instances")
        self.assertIsNotNone(texture_instances)
        self.assertEqual(
            [ti["texture"] for ti in texture_instances],
            ["rbxassetid://111", "rbxassetid://222", "rbxassetid://333"],
        )
        # No Face props: Roblox's default surface (Front, NormalId 5).
        self.assertEqual([ti["face"] for ti in texture_instances], [5, 5, 5])
        self.assertEqual(entry["texture_instance"], texture_instances[0])
        surfaces = entry.get("surface_appearances")
        self.assertIsNotNone(surfaces)
        self.assertEqual(
            [sa["color_map"] for sa in surfaces],
            ["rbxassetid://444", "rbxassetid://555"],
        )
        self.assertEqual([sa["alpha_mode"] for sa in surfaces], [0, 1])
        self.assertEqual(entry["surface_appearance"], surfaces[0])

    def test_parts_outside_workspace_are_skipped(self):
        # A place keeps geometry under Workspace; parts stored under other
        # services (ReplicatedStorage here) must not become scene objects.
        chunks = []
        chunks.append(_inst_chunk(1, "Workspace", [0]))
        chunks.append(_inst_chunk(2, "ReplicatedStorage", [1]))
        chunks.append(_inst_chunk(3, "Part", [2, 3]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Workspace")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("ReplicatedStorage")))
        chunks.append(
            _prop_chunk(3, "Name", 0x01, _enc_string("InWorld") + _enc_string("InStorage"))
        )
        chunks.append(
            _prop_chunk(3, "Size", 0x0E, _enc_vector3s([[2, 2, 2], [2, 2, 2]]))
        )
        chunks.append(_prnt_chunk([0, 1, 2, 3], [-1, -1, 0, 1]))

        data = _rbxm(chunks, class_count=3, instance_count=4)
        entries = rbxm_to_part_aux(data)
        self.assertEqual([e["name"] for e in entries], ["InWorld"])

        meta = parse_rbxm(data)
        node_names = {n["name"] for n in meta["scene_nodes"]}
        self.assertIn("Workspace", node_names)
        self.assertIn("InWorld", node_names)
        self.assertNotIn("ReplicatedStorage", node_names)
        self.assertNotIn("InStorage", node_names)

    def test_model_without_workspace_imports_all_parts(self):
        # .rbxm models have no Workspace root; every part is a candidate.
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "Part", [1, 2]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Prop")))
        chunks.append(
            _prop_chunk(2, "Name", 0x01, _enc_string("A") + _enc_string("B"))
        )
        chunks.append(
            _prop_chunk(2, "Size", 0x0E, _enc_vector3s([[1, 1, 1], [1, 1, 1]]))
        )
        chunks.append(_prnt_chunk([0, 1, 2], [-1, 0, 0]))

        data = _rbxm(chunks, class_count=2, instance_count=3)
        entries = rbxm_to_part_aux(data)
        self.assertEqual([e["name"] for e in entries], ["A", "B"])

    def test_non_identity_rotation(self):
        # A part rotated 90 degrees about Y should decode its matrix, not use id.
        chunks = []
        chunks.append(_inst_chunk(1, "MeshPart", [0]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Rotated")))
        angle = math.radians(90)
        c, s = math.cos(angle), math.sin(angle)
        # rotation about Y: (c,0,s),(0,1,0),(-s,0,c)
        rot = (c, 0, s, 0, 1, 0, -s, 0, c)
        cf = (5.0, 0.0, 0.0) + rot
        chunks.append(_prop_chunk(1, "CFrame", 0x10, _enc_cframes([cf])))
        chunks.append(_prop_chunk(1, "Size", 0x0E, _enc_vector3s([[1, 1, 1]])))
        chunks.append(_prnt_chunk([0], [-1]))
        data = _rbxm(chunks, class_count=1, instance_count=1)

        entries = rbxm_to_part_aux(data)
        self.assertEqual(len(entries), 1)
        decoded_cf = entries[0]["part_cf"]
        self.assertAlmostEqual(decoded_cf[0], 5.0)
        self.assertAlmostEqual(decoded_cf[3], c, places=5)
        self.assertAlmostEqual(decoded_cf[5], s, places=5)

    def test_metadata_shape(self):
        data = self._build_character()
        meta = parse_rbxm(data)
        self.assertEqual(meta["source"], "rbxm")
        self.assertIn("partAux", meta)
        self.assertEqual(len(meta["partAux"]), 2)

    def test_rejects_chunk_with_impossible_declared_size(self):
        data = _rbxm([], class_count=0, instance_count=0)
        # Replace END's uncompressed length with an over-large value.  The
        # parser must reject it before slicing or allocating the fake body.
        offset = len(data) - (16 + len(b"</roblox>")) + 8
        corrupted = bytearray(data)
        corrupted[offset: offset + 4] = struct.pack("<I", 0xFFFFFFFF)
        with self.assertRaises(RbxmError):
            parse_rbxm(bytes(corrupted))

    def test_rejects_excessive_total_decoded_chunks(self):
        data = _rbxm([_inst_chunk(1, "Part", [0])], class_count=1, instance_count=1)
        with mock.patch("roblox_animations.core.rbxm._MAX_TOTAL_CHUNK_BYTES", 1):
            with self.assertRaisesRegex(RbxmError, "decoded chunks"):
                parse_rbxm(data)


def _enc_ref_prop(referents):
    return _enc_referents(referents)


class TestRbxmRigTree(unittest.TestCase):
    def _build_jointed_rig(self):
        # Model > HumanoidRootPart -(Motor6D)-> UpperTorso
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "MeshPart", [1, 2]))
        chunks.append(_inst_chunk(3, "Motor6D", [3]))

        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Rig")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("HumanoidRootPart") + _enc_string("UpperTorso")))
        chunks.append(_prop_chunk(3, "Name", 0x01, _enc_string("Root")))

        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
        hrp_cf = (0.0, 2.0, 0.0) + identity
        torso_cf = (0.0, 2.5, 0.0) + identity
        chunks.append(_prop_chunk(2, "CFrame", 0x10, _enc_cframes([hrp_cf, torso_cf])))
        chunks.append(_prop_chunk(2, "Size", 0x0E, _enc_vector3s([[2, 1, 1], [2, 2, 1]])))

        # Motor6D: Part0=HumanoidRootPart(1), Part1=UpperTorso(2), C0/C1
        chunks.append(_prop_chunk(3, "Part0", 0x13, _enc_ref_prop([1])))
        chunks.append(_prop_chunk(3, "Part1", 0x13, _enc_ref_prop([2])))
        c0 = (0.0, 0.5, 0.0) + identity
        c1 = (0.0, -0.5, 0.0) + identity
        chunks.append(_prop_chunk(3, "C0", 0x10, _enc_cframes([c0])))
        chunks.append(_prop_chunk(3, "C1", 0x10, _enc_cframes([c1])))

        chunks.append(_prnt_chunk([0, 1, 2, 3], [-1, 0, 0, 1]))
        return _rbxm(chunks, class_count=3, instance_count=4)

    def test_rig_tree_extraction(self):
        data = self._build_jointed_rig()
        meta = parse_rbxm(data)
        self.assertIn("rig", meta)
        rig = meta["rig"]

        self.assertEqual(rig["jname"], "HumanoidRootPart")
        self.assertAlmostEqual(rig["transform"][1], 2.0)
        self.assertEqual(len(rig["children"]), 1)

        torso = rig["children"][0]
        self.assertEqual(torso["jname"], "UpperTorso")
        self.assertEqual(torso["pname"], "HumanoidRootPart")
        self.assertEqual(torso["jointType"], "Motor6D")
        self.assertAlmostEqual(torso["transform"][1], 2.5)
        # parent is Part0, so jointtransform0 == C0, jointtransform1 == C1
        self.assertAlmostEqual(torso["jointtransform0"][1], 0.5)
        self.assertAlmostEqual(torso["jointtransform1"][1], -0.5)

    def _build_two_rig_model(self):
        # Two independent Motor6D rigs packed in one model file (no Humanoid).
        # referents: 0,1 models; 2,3 rig A parts; 4,5 rig B parts; 6,7 joints
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0, 1]))
        chunks.append(_inst_chunk(2, "MeshPart", [2, 3, 4, 5]))
        chunks.append(_inst_chunk(3, "Motor6D", [6, 7]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("RigA") + _enc_string("RigB")))
        chunks.append(
            _prop_chunk(
                2,
                "Name",
                0x01,
                _enc_string("RootA") + _enc_string("LimbA")
                + _enc_string("RootB") + _enc_string("LimbB"),
            )
        )
        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
        chunks.append(
            _prop_chunk(
                2,
                "CFrame",
                0x10,
                _enc_cframes([
                    (0.0, 2.0, 0.0) + identity,
                    (0.0, 3.0, 0.0) + identity,
                    (5.0, 2.0, 0.0) + identity,
                    (5.0, 3.0, 0.0) + identity,
                ]),
            )
        )
        chunks.append(_prop_chunk(2, "Size", 0x0E, _enc_vector3s([[1, 1, 1]] * 4)))
        chunks.append(_prop_chunk(3, "Part0", 0x13, _enc_referents([2, 4])))
        chunks.append(_prop_chunk(3, "Part1", 0x13, _enc_referents([3, 5])))
        c0 = (0.0, 0.5, 0.0) + identity
        chunks.append(_prop_chunk(3, "C0", 0x10, _enc_cframes([c0, c0])))
        chunks.append(_prop_chunk(3, "C1", 0x10, _enc_cframes([c0, c0])))
        chunks.append(_prnt_chunk(
            [0, 1, 2, 3, 4, 5, 6, 7],
            [-1, -1, 0, 0, 1, 1, 0, 1],
        ))
        return _rbxm(chunks, class_count=3, instance_count=8)

    def test_model_file_multiple_rigs_all_enumerated(self):
        meta = parse_rbxm(self._build_two_rig_model())
        self.assertIn("rig", meta)
        rigs = meta["scene_rigs"]
        self.assertEqual(len(rigs), 2)
        self.assertEqual(sorted(rig["name"] for rig in rigs), ["RigA", "RigB"])
        for rig in rigs:
            self.assertIsInstance(rig["rig"], dict)
            self.assertEqual(len(rig["part_refs"]), 2)
            self.assertTrue(rig["meshToBone"])

    def _build_humanoid_plus_gadget_model(self):
        # One Humanoid character and one jointed gadget in a single model file.
        # referents: 0 Char model, 1 Humanoid, 2,3 Char parts,
        #            4 Gadget model, 5,6 gadget parts, 7,8 joints
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0, 4]))
        chunks.append(_inst_chunk(2, "Humanoid", [1]))
        chunks.append(_inst_chunk(3, "MeshPart", [2, 3, 5, 6]))
        chunks.append(_inst_chunk(4, "Motor6D", [7, 8]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Char") + _enc_string("Gadget")))
        chunks.append(
            _prop_chunk(
                3,
                "Name",
                0x01,
                _enc_string("Root") + _enc_string("Torso")
                + _enc_string("Base") + _enc_string("Arm"),
            )
        )
        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
        chunks.append(
            _prop_chunk(
                3,
                "CFrame",
                0x10,
                _enc_cframes([
                    (0.0, 2.0, 0.0) + identity,
                    (0.0, 3.0, 0.0) + identity,
                    (5.0, 2.0, 0.0) + identity,
                    (5.0, 3.0, 0.0) + identity,
                ]),
            )
        )
        chunks.append(_prop_chunk(3, "Size", 0x0E, _enc_vector3s([[1, 1, 1]] * 4)))
        chunks.append(_prop_chunk(4, "Part0", 0x13, _enc_referents([2, 5])))
        chunks.append(_prop_chunk(4, "Part1", 0x13, _enc_referents([3, 6])))
        c0 = (0.0, 0.5, 0.0) + identity
        chunks.append(_prop_chunk(4, "C0", 0x10, _enc_cframes([c0, c0])))
        chunks.append(_prop_chunk(4, "C1", 0x10, _enc_cframes([c0, c0])))
        chunks.append(_prnt_chunk(
            [0, 1, 2, 3, 4, 5, 6, 7, 8],
            [-1, 0, 0, 0, -1, 4, 4, 0, 4],
        ))
        return _rbxm(chunks, class_count=4, instance_count=9)

    def test_model_file_humanoid_and_component_rigs_both_enumerated(self):
        meta = parse_rbxm(self._build_humanoid_plus_gadget_model())
        rigs = meta["scene_rigs"]
        self.assertEqual(len(rigs), 2)
        self.assertEqual(sorted(rig["name"] for rig in rigs), ["Char", "Gadget"])
        self.assertEqual({rig["model_ref"] for rig in rigs}, {0, None})

    def test_bone_instances_become_deform_bones(self):
        # Skinned rig: MeshPart with a Bone child (and a nested Bone grandchild).
        # Bones are not joints — they must be attached to their owning part's
        # rig node as isDeformBone children.
        chunks = []
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "MeshPart", [1]))
        chunks.append(_inst_chunk(3, "Bone", [2, 3]))

        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Skinned")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("Body")))
        chunks.append(_prop_chunk(3, "Name", 0x01, _enc_string("Root") + _enc_string("Tip")))

        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
        chunks.append(_prop_chunk(2, "CFrame", 0x10, _enc_cframes([(0.0, 1.0, 0.0) + identity])))
        chunks.append(_prop_chunk(2, "Size", 0x0E, _enc_vector3s([[2, 2, 1]])))
        chunks.append(
            _prop_chunk(
                3,
                "CFrame",
                0x10,
                _enc_cframes([(0.0, 0.5, 0.0) + identity, (0.0, 1.0, 0.0) + identity]),
            )
        )

        # Body -> Model, Root bone -> Body, Tip bone -> Root bone
        chunks.append(_prnt_chunk([0, 1, 2, 3], [-1, 0, 1, 2]))
        data = _rbxm(chunks, class_count=3, instance_count=4)

        meta = parse_rbxm(data)
        self.assertIn("rig", meta)
        rig = meta["rig"]
        self.assertEqual(rig["jname"], "Body")

        bone_children = [c for c in rig["children"] if c.get("jointType") == "Bone"]
        self.assertEqual(len(bone_children), 1)
        root_bone = bone_children[0]
        self.assertEqual(root_bone["jname"], "Root")
        self.assertEqual(root_bone["pname"], "Body")
        self.assertTrue(root_bone["isDeformBone"])
        # transform is world space: part at y=1.0 + bone local y=0.5
        self.assertAlmostEqual(root_bone["transform"][1], 1.5)
        # jointtransform0 is the parent-relative local CFrame (matches
        # RigPart.Encode); jointtransform1 is identity.
        self.assertAlmostEqual(root_bone["jointtransform0"][1], 0.5)
        self.assertAlmostEqual(root_bone["jointtransform1"][1], 0.0)

        self.assertEqual(len(root_bone["children"]), 1)
        tip_bone = root_bone["children"][0]
        self.assertEqual(tip_bone["jname"], "Tip")
        self.assertTrue(tip_bone["isDeformBone"])
        # world: part y=1.0 + root local y=0.5 + tip local y=1.0
        self.assertAlmostEqual(tip_bone["transform"][1], 2.5)
        self.assertAlmostEqual(tip_bone["jointtransform0"][1], 1.0)

        # meshToBone must treat Bone nodes as deform bones (identity mapping).
        mesh_to_bone = meta.get("meshToBone") or {}
        self.assertEqual(mesh_to_bone.get("Root"), "Root")
        self.assertEqual(mesh_to_bone.get("Tip"), "Tip")


# ---------------------------------------------------------------------------
# UnionOperation (CSG) tests
# ---------------------------------------------------------------------------


def _enc_solid_mesh(positions, faces):
    """Build a legacy SolidMeshHolder blob (pre-CSGMDL union mesh)."""
    body = bytearray(b"SolidMesh\0\0\0\0")
    body += struct.pack("<II", len(positions), 0)
    for position in positions:
        body += struct.pack("<3f", *position)
    body += struct.pack("<II", len(faces), 0)
    for _ in faces:
        body += struct.pack("<3f", 0.0, 1.0, 0.0)  # per-face normal
    body += struct.pack("<IIII", 0, 0, 0, 0)  # padding + color_count
    indices = [index for face in faces for index in face]
    encoded = bytearray()
    index_out = 0
    for index in indices:
        delta = index - index_out
        encoded.append(delta if 0 <= delta < 64 else delta + 128)
        index_out = index
    body += struct.pack("<II", len(indices), 0)
    body += encoded
    return bytes((3, 1)) + struct.pack("<III", len(body), 0, len(body)) + bytes(body)


def _enc_csgmdl5(positions, faces):
    """Build a CSGMDL5 blob (only the 10-byte magic is obfuscated)."""
    cycle = (86, 46, 110, 88, 49, 32, 48, 4, 52, 105, 12, 119, 12, 1, 94, 0,
             26, 96, 55, 105, 29, 82, 43, 7, 79, 36, 89, 101, 83, 4, 122)
    body = bytearray(b"CSGMDL" + struct.pack("<I", 5))
    # positions
    body += struct.pack("<H", len(positions))
    for position in positions:
        body += struct.pack("<3f", *position)
    # quantized normals (stored = round(n * 32767) + 32767, wrapped to i16)
    body += struct.pack("<HI", len(positions), len(positions) * 6)
    for _ in positions:
        body += struct.pack("<3h", 0, 0x7FFF, 0)  # +Y normal
    # colors (red) + normal ids (Top)
    body += struct.pack("<H", len(positions))
    for _ in positions:
        body += bytes((255, 0, 0, 255))
    body += struct.pack("<H", len(positions))
    for _ in positions:
        body += bytes((2,))
    # uvs + tangents (none)
    body += struct.pack("<H", len(positions))
    for _ in positions:
        body += struct.pack("<2f", 0.0, 0.0)
    body += struct.pack("<HI", 0, 0)
    # delta-encoded indices with range markers [0, index_count]
    indices = [index for face in faces for index in face]
    encoded = bytearray()
    index_out = 0
    for index in indices:
        delta = index - index_out
        if 0 <= delta < 64:
            encoded.append(delta)
        elif -64 <= delta < 0:
            encoded.append(delta + 128)
        else:
            raise ValueError("test delta out of range")
        index_out = index
    body += struct.pack("<II", len(indices), len(encoded))
    body += encoded
    body += struct.pack("<B", 2)
    body += struct.pack("<II", 0, len(indices))
    obfuscated = bytearray(body)
    for index in range(10):
        obfuscated[index] ^= cycle[index % 31]
    return bytes(obfuscated)


def _enc_csgmdl2(positions, faces):
    """Build a CSGMDL2 blob (obfuscated) the way Studio writes MeshData2."""
    body = bytearray()
    body += b"CSGMDL" + struct.pack("<I", 2)
    body += b"\0" * 32  # hash
    body += struct.pack("<II", len(positions), 84)
    for i, (x, y, z) in enumerate(positions):
        body += struct.pack("<3f", x, y, z)
        body += struct.pack("<3f", 0.0, 1.0, 0.0)  # normal
        body += bytes((255, 0, 0, 255))  # color (red)
        body += struct.pack("<I", 2)  # normal id
        body += struct.pack("<2f", 0.0, 0.0)  # uv
        body += struct.pack("<3f", 1.0, 0.0, 0.0)  # tangent
        body += b"\0" * 32  # reserved/padding (84-byte stride)
    body += struct.pack("<I", len(faces) * 3)
    for face in faces:
        body += struct.pack("<3I", *face)
    # Obfuscate with the 31-byte noise cycle.
    cycle = (86, 46, 110, 88, 49, 32, 48, 4, 52, 105, 12, 119, 12, 1, 94, 0,
             26, 96, 55, 105, 29, 82, 43, 7, 79, 36, 89, 101, 83, 4, 122)
    return bytes(b ^ cycle[i % 31] for i, b in enumerate(body))


class TestRbxmUnion(unittest.TestCase):
    def _build_union(self, use_part_color=False):
        # Model > UnionOperation with a triangle CSGMDL2 mesh in MeshData2.
        positions = [(-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 2.0, 0.0)]
        faces = [(0, 1, 2)]
        blob = _enc_csgmdl2(positions, faces)

        chunks = []
        chunks.append(_chunk(b"SSTR", _enc_sstr([blob])))
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "UnionOperation", [1]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Unions")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("X")))
        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
        chunks.append(_prop_chunk(2, "CFrame", 0x10, _enc_cframes([(3.0, 4.0, 5.0) + identity])))
        chunks.append(_prop_chunk(2, "size", 0x0E, _enc_vector3s([[2.0, 2.0, 1.0]])))
        if use_part_color:
            chunks.append(_prop_chunk(2, "UsePartColor", 0x02, b"\x01"))
        chunks.append(
            _prop_chunk(2, "MeshData2", 0x1C, _enc_shared_string_indices([0]))
        )
        chunks.append(_prnt_chunk([0, 1], [-1, 0]))
        return _rbxm(chunks, class_count=2, instance_count=2)

    def test_union_mesh_extraction(self):
        data = self._build_union()
        entries = rbxm_to_part_aux(data)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["name"], "X")
        self.assertEqual(entry["mesh_class"], "UnionOperation")
        mesh = entry.get("union_mesh")
        self.assertIsNotNone(mesh)
        self.assertEqual(len(mesh["positions"]), 3)
        self.assertEqual(len(mesh["faces"]), 1)
        self.assertAlmostEqual(mesh["positions"][2][1], 2.0)
        self.assertAlmostEqual(mesh["colors"][0][0], 1.0)  # red
        self.assertAlmostEqual(mesh["colors"][0][1], 0.0)
        # mesh is part-local: bbox must match part_size
        xs = [p[0] for p in mesh["positions"]]
        self.assertAlmostEqual(max(xs) - min(xs), 2.0)

    def test_union_use_part_color_strips_baked_colors(self):
        data = self._build_union(use_part_color=True)
        entries = rbxm_to_part_aux(data)
        self.assertEqual(len(entries), 1)
        mesh = entries[0].get("union_mesh")
        self.assertIsNotNone(mesh)
        self.assertEqual(mesh.get("colors"), [])  # falls back to part Color3
        self.assertEqual(len(mesh["faces"]), 1)

    def test_union_without_meshdata_marks_unsupported(self):
        chunks = []
        chunks.append(_inst_chunk(1, "UnionOperation", [0]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Legacy")))
        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
        chunks.append(_prop_chunk(1, "CFrame", 0x10, _enc_cframes([(0, 0, 0) + identity])))
        chunks.append(_prop_chunk(1, "size", 0x0E, _enc_vector3s([[1, 1, 1]])))
        chunks.append(_prnt_chunk([0], [-1]))
        data = _rbxm(chunks, class_count=1, instance_count=1)

        entries = rbxm_to_part_aux(data)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].get("union_unsupported"))
        self.assertNotIn("union_mesh", entries[0])

    def test_union_asset_id_reference_is_stamped(self):
        # 2012-era unions store no mesh inline; the AssetId content URL
        # references the render-mesh asset the importer fetches later.
        chunks = []
        chunks.append(_inst_chunk(1, "UnionOperation", [0]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("OldUnion")))
        chunks.append(
            _prop_chunk(
                1, "AssetId", 0x01,
                _enc_string("http://www.roblox.com//asset/?id=361773430"),
            )
        )
        chunks.append(_prnt_chunk([0], [-1]))
        data = _rbxm(chunks, class_count=1, instance_count=1)

        entries = rbxm_to_part_aux(data)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].get("union_unsupported"))
        self.assertEqual(entries[0].get("union_asset_id"), 361773430)

    def test_lz4_decoder_tolerates_legacy_length_miscount(self):
        # Legacy union assets declare an uncompressed length that is SHORT of
        # the real block (observed +39 on ChildData chunks).  A clean decode
        # that consumed the whole input is trusted over the declared size.
        from roblox_animations.core.rbxm import _lz4_block_decompress

        payload = bytes(range(1, 64)) * 3
        # lz4 block: 15+ ext literals is enough for a short run.
        block = bytes([0xF0, len(payload) - 15]) + payload
        out = _lz4_block_decompress(block, len(payload) - 7)
        self.assertEqual(bytes(out), payload)

    def test_union_csgmdl5_extraction(self):
        # CSGMDL5: deinterleaved arrays + delta-encoded indices + markers.
        positions = [(-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 2.0, 0.0)]
        faces = [(0, 1, 2)]
        blob = _enc_csgmdl5(positions, faces)

        chunks = []
        chunks.append(_chunk(b"SSTR", _enc_sstr([blob])))
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "UnionOperation", [1]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Unions")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("V5")))
        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
        chunks.append(_prop_chunk(2, "CFrame", 0x10, _enc_cframes([(3.0, 4.0, 5.0) + identity])))
        chunks.append(_prop_chunk(2, "size", 0x0E, _enc_vector3s([[2.0, 2.0, 1.0]])))
        chunks.append(
            _prop_chunk(2, "MeshData2", 0x1C, _enc_shared_string_indices([0]))
        )
        chunks.append(_prnt_chunk([0, 1], [-1, 0]))
        data = _rbxm(chunks, class_count=2, instance_count=2)

        entries = rbxm_to_part_aux(data)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["name"], "V5")
        self.assertEqual(entry["mesh_class"], "UnionOperation")
        mesh = entry.get("union_mesh")
        self.assertIsNotNone(mesh)
        self.assertEqual(len(mesh["positions"]), 3)
        self.assertEqual(len(mesh["faces"]), 1)
        self.assertEqual(mesh["faces"][0], (0, 1, 2))
        self.assertAlmostEqual(mesh["positions"][2][1], 2.0)
        self.assertAlmostEqual(mesh["colors"][0][0], 1.0)  # red
        # normal ids take priority: Top (+Y) flat normals for CSG
        self.assertAlmostEqual(mesh["normals"][0][1], 1.0)

    def test_union_solid_mesh_extraction(self):
        # Legacy unions embed their render mesh in SolidMeshHolder.
        positions = [(-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 2.0, 0.0)]
        faces = [(0, 1, 2)]
        blob = _enc_solid_mesh(positions, faces)

        chunks = []
        chunks.append(_chunk(b"SSTR", _enc_sstr([blob])))
        chunks.append(_inst_chunk(1, "Model", [0]))
        chunks.append(_inst_chunk(2, "UnionOperation", [1]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Unions")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("Legacy")))
        identity = (1, 0, 0, 0, 1, 0, 0, 0, 1)
        chunks.append(_prop_chunk(2, "CFrame", 0x10, _enc_cframes([(3.0, 4.0, 5.0) + identity])))
        chunks.append(_prop_chunk(2, "size", 0x0E, _enc_vector3s([[2.0, 2.0, 1.0]])))
        chunks.append(
            _prop_chunk(2, "SolidMeshHolder", 0x1C, _enc_shared_string_indices([0]))
        )
        chunks.append(_prnt_chunk([0, 1], [-1, 0]))
        data = _rbxm(chunks, class_count=2, instance_count=2)

        entries = rbxm_to_part_aux(data)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["mesh_class"], "UnionOperation")
        self.assertNotIn("union_unsupported", entry)
        mesh = entry.get("union_mesh")
        self.assertIsNotNone(mesh)
        self.assertEqual(len(mesh["positions"]), 3)
        self.assertEqual(mesh["faces"], [(0, 1, 2)])
        self.assertAlmostEqual(mesh["positions"][2][1], 2.0)
        # flat normals rebuilt from the face (cross of edges 0->1, 0->2)
        self.assertAlmostEqual(mesh["normals"][0][2], 1.0, places=5)

    def test_binary_string_props_survive(self):
        # A String prop containing invalid UTF-8 must come back as bytes,
        # not mangled by errors="replace".
        chunks = []
        chunks.append(_inst_chunk(1, "Part", [0]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Blobby")))
        payload = b"\xff\xfe\x00binary\x01"
        chunks.append(
            _prop_chunk(1, "AttributesSerialize", 0x01, struct.pack("<I", len(payload)) + payload)
        )
        chunks.append(_prop_chunk(1, "size", 0x0E, _enc_vector3s([[1, 1, 1]])))
        chunks.append(_prnt_chunk([0], [-1]))
        data = _rbxm(chunks, class_count=1, instance_count=1)

        entries = rbxm_to_part_aux(data)
        self.assertEqual(entries[0]["name"], "Blobby")


class TestRbxmAttributes(unittest.TestCase):
    """Attribute blob decoding and weapon grip metadata extraction."""

    # Ground truth from rbx-dom's own attributes round-trip test (Studio
    # serialization of a Folder with 13 attributes).
    RBX_DOM_ATTRIBUTES_BASE64 = (
        "DQAAAAcAAABCb29sZWFuAwEKAAAAQnJpY2tDb2xvcg7sAwAABgAAAENvbG9yMw+joiI/AAAA"
        "AAAAgD8NAAAAQ29sb3JTZXF1ZW5jZRkDAAAAAAAAAAAAAAAAAIA/AAAAAAAAAAAAAAAAAAAA"
        "PwAAAAAAAIA/AAAAAAAAAAAAAIA/AAAAAAAAAAAAAIA/BgAAAE51bWJlcgYAAAAAgBzIQAsA"
        "AABOdW1iZXJSYW5nZRsAAKBAAAAgQQ4AAABOdW1iZXJTZXF1ZW5jZRcDAAAAAAAAAAAAAAAA"
        "AIA/AAAAAAAAAD8AAAAAAAAAAAAAgD8AAIA/BAAAAFJlY3QcAACAPwAAAEAAAEBAAACAQAYA"
        "AABTdHJpbmcCDQAAAEhlbGxvLCB3b3JsZCEEAAAAVURpbQkAAAA/ZAAAAAUAAABVRGltMgoA"
        "AAA/CgAAADMzMz8eAAAABwAAAFZlY3RvcjIQAAAgQQAASEIHAAAAVmVjdG9yMxEAAIA/AAAA"
        "QAAAQEA="
    )

    def test_attributes_blob_matches_rbxdom_sample(self):
        import base64

        blob = base64.b64decode(self.RBX_DOM_ATTRIBUTES_BASE64)
        attrs = _decode_attributes_blob(blob)
        self.assertEqual(attrs["Boolean"], True)
        self.assertEqual(attrs["String"], "Hello, world!")
        self.assertEqual(attrs["BrickColor"], 1004)
        self.assertAlmostEqual(attrs["Color3"][0], 0.635, places=3)
        self.assertAlmostEqual(attrs["Color3"][2], 1.0, places=3)
        self.assertEqual(attrs["Vector3"], [1.0, 2.0, 3.0])
        self.assertEqual(len(attrs), 13)

    def test_weapon_grip_attributes_surface_in_meta(self):
        chunks = []
        chunks.append(_inst_chunk(1, "Tool", [0]))
        chunks.append(_inst_chunk(2, "Part", [1]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Sword")))
        chunks.append(_prop_chunk(2, "Name", 0x01, _enc_string("Handle")))
        c0 = "0,1,2,3,4,5,6,7,8,9,10,11"
        c1 = "11,10,9,8,7,6,5,4,3,2,1,0"
        blob = _enc_attribute_blob([
            ("BlenderGripCount", "1"),
            ("BlenderGripVersion", "1"),
            ("BlenderGrip0_Root", "Handle"),
            ("BlenderGrip0_Bone", "RightHand"),
            ("BlenderGrip0_JointType", "Weld"),
            ("BlenderGrip0_JointName", "SwordGrip"),
            ("BlenderGrip0_C0", c0),
            ("BlenderGrip0_C1", c1),
        ])
        chunks.append(_prop_chunk(1, "Attributes", 0x01, _enc_binary(blob)))
        chunks.append(_prop_chunk(2, "size", 0x0E, _enc_vector3s([[1, 4, 1]])))
        chunks.append(_prnt_chunk([0, 1], [-1, 0]))
        data = _rbxm(chunks, class_count=2, instance_count=2)

        meta = parse_rbxm(data)
        self.assertEqual(meta.get("weaponGripVersion"), "1")
        grips = meta.get("weaponGrip")
        self.assertIsNotNone(grips)
        self.assertEqual(len(grips), 1)
        grip = grips[0]
        self.assertEqual(grip["root"], "Handle")
        self.assertEqual(grip["bone"], "RightHand")
        self.assertEqual(grip["jointType"], "Weld")
        self.assertEqual(grip["jointName"], "SwordGrip")
        self.assertEqual(grip["connectionC0"], [float(v) for v in range(12)])
        self.assertEqual(
            grip["connectionC1"], [float(11 - v) for v in range(12)]
        )

    def test_attributes_blob_unknown_types_stop_cleanly(self):
        # A blob with an unknown type id must not blow up the parser.
        blob = struct.pack("<I", 1)
        blob += struct.pack("<I", 4) + b"Wait"
        blob += struct.pack("<B", 0x7F)  # unknown attribute type
        blob += b"\xff" * 8
        attrs = _decode_attributes_blob(blob)
        self.assertEqual(attrs, {})

    def test_legacy_attributes_serialize_name_is_ignored(self):
        # Old saves used AttributesSerialize; the parser must not choke on
        # it even when the payload is not a decodable attribute blob.
        chunks = []
        chunks.append(_inst_chunk(1, "Part", [0]))
        chunks.append(_prop_chunk(1, "Name", 0x01, _enc_string("Blobby")))
        payload = b"\xff\xfe\x00binary\x01"
        chunks.append(
            _prop_chunk(1, "AttributesSerialize", 0x01, struct.pack("<I", len(payload)) + payload)
        )
        chunks.append(_prop_chunk(1, "size", 0x0E, _enc_vector3s([[1, 1, 1]])))
        chunks.append(_prnt_chunk([0], [-1]))
        data = _rbxm(chunks, class_count=1, instance_count=1)

        entries = rbxm_to_part_aux(data)
        self.assertEqual(entries[0]["name"], "Blobby")


if __name__ == "__main__":
    unittest.main()
