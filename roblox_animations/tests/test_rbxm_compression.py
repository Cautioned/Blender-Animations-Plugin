"""Both Roblox binary extensions use per-chunk raw/LZ4/Zstandard dispatch."""
import hashlib
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from ..core import rbxm, zstd_codec


def string(value):
    value = value.encode()
    return struct.pack('<I', len(value)) + value


def chunk(name, data, compression):
    if compression == 'zstd':
        packed = zstd_codec._load_codec().compress(data)
    elif compression == 'lz4':
        # A legal raw LZ4 block containing a literal-only sequence.
        packed = bytes([len(data) << 4]) + data if len(data) < 15 else b'\xf0' + bytes([len(data)-15]) + data
    else:
        packed = data
    return name.ljust(4, b'\0') + struct.pack('<III', len(packed) if compression else 0, len(data), 0) + packed


def model(compressions):
    bodies = [
        (b'INST', struct.pack('<I', 1) + string('Part') + b'\0' + struct.pack('<I', 1) + b'\0'*4),
        (b'PROP', struct.pack('<I', 1) + string('Name') + b'\x01' + string('CompressedPart')),
    ]
    header = rbxm.FILE_MAGIC + rbxm.FILE_SIGNATURE + struct.pack('<HII', 0, 1, 1) + b'\0'*8
    return header + b''.join(chunk(n, b, c) for (n,b),c in zip(bodies,compressions)) + chunk(b'END', rbxm.END_MAGIC, None)


class TestRobloxCompression(unittest.TestCase):
    def test_raw_lz4_zstd_and_mixed_chunks_have_identical_metadata(self):
        expected = rbxm.parse_rbxm(model((None,None)))
        self.assertEqual(expected['partAux'][0]['name'], 'CompressedPart')
        for modes in [('lz4','lz4'),('zstd','zstd'),('lz4','zstd'),('zstd',None)]:
            with self.subTest(modes=modes):
                self.assertEqual(rbxm.parse_rbxm(model(modes)), expected)

    def test_unknown_content_size_is_bounded_and_supported(self):
        codec = zstd_codec._load_codec()
        payload = b'Roblox chunk' * 100
        packed = codec.compress(payload, options={codec.CompressionParameter.content_size_flag: 0})
        self.assertIsNone(codec.get_frame_info(packed).decompressed_size)
        self.assertEqual(rbxm._decompress_chunk_body(packed,len(packed),len(payload)),payload)
        with self.assertRaises(rbxm.RbxmError):
            rbxm._decompress_chunk_body(packed,len(packed),1)

    def test_truncated_corrupt_trailing_and_wrong_size_frames_fail(self):
        codec = zstd_codec._load_codec()
        payload = b'Roblox chunk' * 100
        packed = codec.compress(payload, options={codec.CompressionParameter.checksum_flag: 1})
        corrupt = packed[:-1] + bytes([packed[-1] ^ 0xff])
        for data, size in [(packed[:-1],len(payload)),(corrupt,len(payload)),
                           (packed+b'extra',len(payload)),(packed+packed,len(payload)),
                           (packed,len(payload)-1),(packed,len(payload)+1),
                           (rbxm._ZSTD_MAGIC+b'bad',len(payload))]:
            with self.subTest(size=size, tail=data[-8:]):
                with self.assertRaises(rbxm.RbxmError):
                    rbxm._decompress_chunk_body(data,len(data),size)

    def test_empty_frame(self):
        packed=zstd_codec._load_codec().compress(b'')
        self.assertEqual(rbxm._decompress_chunk_body(packed,len(packed),0),b'')

    def test_size_cap_is_checked_before_loading_codec(self):
        with patch.object(zstd_codec, '_load_codec') as load:
            with self.assertRaises(rbxm.RbxmError):
                rbxm._decompress_chunk_body(rbxm._ZSTD_MAGIC,4,rbxm._MAX_CHUNK_BYTES+1)
            load.assert_not_called()

    def test_missing_decoder_has_an_import_error(self):
        with patch.object(zstd_codec, '_load_codec', side_effect=ImportError('missing wheel')):
            with self.assertRaisesRegex(rbxm.RbxmError, 'missing wheel'):
                rbxm._decompress_chunk_body(rbxm._ZSTD_MAGIC,4,1)

    def test_bundled_wheels_match_declared_platforms_and_hashes(self):
        root=Path(zstd_codec.__file__).resolve().parents[1]
        lock=json.loads((root/'wheels/zstd-lock.json').read_text())
        self.assertEqual(len(lock['files']),18)
        manifest=(root/'blender_manifest.toml').read_text()
        for entry in lock['files']:
            self.assertEqual(hashlib.sha256((root/'wheels'/entry['filename']).read_bytes()).hexdigest(),entry['sha256'])
            self.assertIn(entry['filename'],manifest)
        for python in ('cp311','cp312','cp313'):
            for system,machines in [('win32',('amd64','arm64')),('darwin',('x86_64','arm64')),('linux',('x86_64','aarch64'))]:
                for machine in machines:
                    matches=[e for e in lock['files'] if zstd_codec._wheel_matches(e['filename'],python,system,machine)]
                    self.assertEqual(len(matches),1,(python,system,machine))
        self.assertFalse(zstd_codec._wheel_matches(lock['files'][0]['filename'],'cp39','linux','x86_64'))
