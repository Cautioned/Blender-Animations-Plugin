"""Content migration regressions using binary model fixtures and XML assets."""
import struct
import unittest
from unittest import mock

from ..core import rbxm
from ..rig import clothing


def _string(value):
    value = value.encode('utf-8')
    return struct.pack('<I', len(value)) + value


def _i32s(values):
    # Roblox stores zigzag integers in big-endian byte planes.
    words = [struct.pack('>I', (v << 1) ^ (v >> 31)) for v in values]
    return bytes(word[lane] for lane in range(4) for word in words)


def _refs(values):
    previous = 0
    deltas = []
    for value in values:
        deltas.append(value - previous)
        previous = value
    return _i32s(deltas)


def _content(values):
    # None, URI and Object sources deliberately interleave in tests.
    sources = [0 if v is None else 1 if isinstance(v, str) else 2 for v in values]
    uris = [v for v in values if isinstance(v, str)]
    objects = [v for v in values if isinstance(v, int)]
    return (_i32s(sources) + struct.pack('<I', len(uris))
            + b''.join(_string(v) for v in uris)
            + struct.pack('<I', len(objects)) + _refs(objects)
            + struct.pack('<I', 0))


def _chunk(name, body):
    return name.ljust(4, b'\0') + struct.pack('<III', 0, len(body), 0) + body


def _model(nodes):
    """Each node has its own class group, matching the binary file layout."""
    chunks = []
    for ref, (class_name, parent, props) in enumerate(nodes):
        chunks.append(_chunk(b'INST', struct.pack('<I', ref) + _string(class_name)
                             + b'\0' + struct.pack('<I', 1) + _refs([ref])))
        for name, (type_id, payload) in props.items():
            chunks.append(_chunk(b'PROP', struct.pack('<I', ref) + _string(name)
                                 + bytes([type_id]) + payload))
    chunks.append(_chunk(b'PRNT', b'\0' + struct.pack('<I', len(nodes))
                         + _refs(range(len(nodes))) + _refs([n[1] for n in nodes])))
    chunks.append(_chunk(b'END', b'</roblox>'))
    return (b'<roblox!\x89\xff\r\n\x1a\n' + struct.pack('<HII', 0, len(nodes), len(nodes))
            + b'\0' * 8 + b''.join(chunks))


def _scene(migrated):
    def asset(old, new, uri):
        return {new: (0x22, _content([uri]))} if migrated else {old: (1, _string(uri))}

    def maps():
        result = {}
        for name in ('ColorMap', 'NormalMap', 'RoughnessMap', 'MetalnessMap', 'TexturePack'):
            result.update(asset(name, name + 'Content', 'rbxassetid://' + str(100 + len(result))))
        return result

    cf = bytes([2]) + b'\0' * 12  # identity rotation, position (0,0,0)
    return _model([
        ('Model', -1, {'Name': (1, _string('Character'))}),
        ('MeshPart', 0, {**asset('MeshId', 'MeshContent', 'rbxassetid://11'),
                         **asset('TextureID', 'TextureContent', 'rbxassetid://12'),
                         'MaterialVariant': (1, _string('Stone'))}),
        ('SurfaceAppearance', 1, maps()),
        ('WrapLayer', 1, {
            **asset('ReferenceMeshId', 'ReferenceMeshContent', 'rbxassetid://21'),
            **asset('CageMeshId', 'CageMeshContent', 'rbxassetid://22'),
            **asset('TemporaryReferenceId', 'TemporaryReferenceMeshContent', 'rbxassetid://23'),
            **asset('TemporaryCageMeshId', 'TemporaryCageMeshContent', 'rbxassetid://24'),
            **asset('HSRAssetId', 'HSRContent', 'rbxassetid://25')}),
        ('WrapTarget', 1, {**asset('CageMeshId', 'CageMeshContent', 'rbxassetid://26'),
                            **asset('TemporaryCageMeshId', 'TemporaryCageMeshContent', 'rbxassetid://27'),
                            **asset('HSRAssetId', 'HSRContent', 'rbxassetid://28')}),
        ('Part', 0, {}),
        ('SpecialMesh', 5, {**asset('MeshId', 'MeshContent', 'rbxassetid://31'),
                            **asset('TextureId', 'TextureContent', 'rbxassetid://32')}),
        ('Texture', 5, asset('Texture', 'TextureContent', 'rbxassetid://41')),
        ('Decal', 5, asset('Texture', 'TextureContent', 'rbxassetid://42')),
        ('Shirt', 0, asset('ShirtTemplate', 'ShirtTemplateContent', 'rbxassetid://51')),
        ('Pants', 0, asset('PantsTemplate', 'PantsTemplateContent', 'rbxassetid://52')),
        ('Sky', -1, {key: value for old, new in (
            ('SkyboxBk', 'SkyboxBackContent'), ('SkyboxDn', 'SkyboxDownContent'),
            ('SkyboxFt', 'SkyboxFrontContent'), ('SkyboxLf', 'SkyboxLeftContent'),
            ('SkyboxRt', 'SkyboxRightContent'), ('SkyboxUp', 'SkyboxUpContent'))
            for key, value in asset(old, new, 'rbxassetid://61').items()}),
        ('MaterialVariant', -1, {**maps(), 'Name': (1, _string('Stone')),
                                 'BaseMaterial': (0x12, bytes(4))}),
        ('Attachment', 1, {'CFrame': (0x10, cf)}),
        ('Attachment', 5, {'CFrame': (0x10, cf)}),
        ('Beam', 1, {**asset('Texture', 'TextureContent', 'rbxassetid://71'),
                      'Enabled': (2, b'\1'), 'Attachment0': (0x13, _refs([13])),
                      'Attachment1': (0x13, _refs([14]))}),
        ('Motor6D', 0, {'Part0': (0x13, _refs([1])), 'Part1': (0x13, _refs([5])),
                         'C0': (0x10, cf), 'C1': (0x10, cf)}),
        ('Humanoid', 0, {}),
    ])


class ContentSerializationTests(unittest.TestCase):
    def test_migrated_scene_matches_legacy_metadata(self):
        legacy = rbxm.parse_rbxm(_scene(False))
        migrated = rbxm.parse_rbxm(_scene(True))
        self.assertEqual(migrated, legacy)
        parts = migrated['partAux']
        self.assertEqual(parts[0]['mesh_id'], 'rbxassetid://11')
        self.assertEqual(parts[0]['texture_id'], 'rbxassetid://12')
        self.assertEqual(parts[0]['surface_appearance']['color_map'], 'rbxassetid://100')
        self.assertEqual(parts[0]['material_variant_data']['normal_map'], 'rbxassetid://101')
        self.assertEqual(parts[0]['wrap_layer']['temporary_reference_id'], 'rbxassetid://23')
        self.assertEqual(parts[0]['wrap_target']['hsr_asset_id'], 'rbxassetid://28')
        self.assertEqual(parts[1]['mesh_id'], 'rbxassetid://31')
        self.assertEqual(parts[1]['texture_id'], 'rbxassetid://32')
        self.assertEqual(parts[1]['texture_instance']['texture'], 'rbxassetid://41')
        self.assertEqual(parts[1]['decals'][0]['texture'], 'rbxassetid://42')
        self.assertEqual(migrated['shirt_template'], 'rbxassetid://51')
        self.assertEqual(migrated['pants_template'], 'rbxassetid://52')
        self.assertEqual(migrated['scene_sky']['back'], 'rbxassetid://61')
        self.assertEqual(migrated['beams'][0]['texture'], 'rbxassetid://71')
        self.assertTrue(any(p.get('shirt_template') == 'rbxassetid://51' for p in parts))

    def test_interleaved_content_sources_do_not_shift_uri_assignments(self):
        values = [None, 'rbxassetid://1', 123, 'rbxassetid://2', None, 456]
        self.assertEqual(rbxm._read_contents(rbxm._Reader(_content(values)), len(values)),
                         ['', 'rbxassetid://1', '', 'rbxassetid://2', '', ''])

    def test_empty_and_object_content_override_stale_legacy_uri(self):
        for value in (None, '', 99, 'rbxassetid://new'):
            with self.subTest(value=value):
                data = _model([('MeshPart', -1, {
                    'MeshId': (1, _string('rbxassetid://old')),
                    'MeshContent': (0x22, _content([value])),
                    'TextureID': (1, _string('rbxassetid://old')),
                    'TextureContent': (0x22, _content([value])),
                })])
                part = rbxm.parse_rbxm(data)['partAux'][0]
                expected = value if isinstance(value, str) else ''
                self.assertEqual(part.get('mesh_id', ''), expected)
                self.assertEqual(part.get('texture_id', ''), expected)

    def test_character_mesh_legacy_numeric_ids_and_new_content(self):
        old = rbxm._Instance(1, 'CharacterMesh')
        old.props.update(MeshId=123, BaseTextureId=456, OverlayTextureId=789, BodyPart=1)
        new = rbxm._Instance(1, 'CharacterMesh')
        new.props.update(MeshContent='rbxassetid://123', BaseTextureContent='rbxassetid://456',
                         OverlayTextureContent='rbxassetid://789', BodyPart=1)
        self.assertEqual(rbxm._build_character_mesh(old), rbxm._build_character_mesh(new))
        new.props.update(MeshContent='', MeshId=999)
        self.assertNotIn('mesh_id', rbxm._build_character_mesh(new))

    def test_decal_color_map_content_and_uri_whitespace(self):
        data = _model([('Part', -1, {}), ('Decal', 0, {
            'ColorMapContent': (0x22, _content([' rbxassetid://42  '])),
            'Texture': (1, _string('rbxassetid://old')),
        })])
        self.assertEqual(rbxm.parse_rbxm(data)['partAux'][0]['decals'][0]['texture'],
                         'rbxassetid://42')

    def test_xml_clothing_legacy_uri_null_and_escaping(self):
        for cls, name in (('Shirt', 'ShirtTemplate'), ('Pants', 'PantsTemplate')):
            for tag, suffix in (('url', ''), ('uri', 'Content')):
                with self.subTest(cls=cls, tag=tag):
                    xml = (f'<roblox><Item class="{cls}"><Properties>'
                           f'<Content name="{name}{suffix}"><{tag}>'
                           f'https://www.roblox.com/asset/?id=123&amp;version=1'
                           f'</{tag}></Content></Properties></Item></roblox>').encode()
                    self.assertEqual(clothing._xml_clothing_template(xml),
                                     'https://www.roblox.com/asset/?id=123&version=1')
            for source in ('<null />', '<uri />', '<Object>RBX123</Object>'):
                xml = (f'<roblox><Item class="{cls}"><Properties>'
                       f'<Content name="{name}"><url>rbxassetid://old</url></Content>'
                       f'<Content name="{name}Content">{source}</Content>'
                       f'</Properties></Item></roblox>').encode()
                self.assertIsNone(clothing._xml_clothing_template(xml))

    def test_template_resolver_reads_migrated_xml(self):
        payload = (b'<roblox><Item class="Shirt"><Properties>'
                   b'<Content name="ShirtTemplateContent"><uri>rbxassetid://123</uri>'
                   b'</Content></Properties></Item></roblox>')
        response = mock.Mock(headers={})
        with mock.patch.dict(clothing._TEMPLATE_ID_CACHE, {}, clear=True), \
                mock.patch.object(clothing, '_get_auth_headers', return_value={}), \
                mock.patch.object(clothing, '_fetch_url_response', return_value=(response, payload)), \
                mock.patch.object(clothing, '_extract_locations_from_payload', return_value=[]):
            self.assertEqual(clothing.resolve_clothing_template_id(987), 'rbxassetid://123')


if __name__ == '__main__':
    unittest.main()
