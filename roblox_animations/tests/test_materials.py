"""Tests for the built-in material pipeline (textures.build_part_material).

Covers:
- node graph wiring for built-in materials (colour map, tint chain, shared UV node)
- white-tint fast path (no gamma/multiply chain allocated)
- material cache sharing/splitting across entries
- timing budgets for per-object work: material builds, cache hits, and the
  stud-density UV layer generation (the places where per-object work is
  easy to accidentally do in O(loops) python).

All timings are printed so import regressions show up as perf numbers, not
just failures. Budgets are deliberately loose so weak machines still pass.
"""

import bpy
import time
import unittest
from unittest import mock

from ..rig import creation
from ..rig import textures
from ..operators import import_ops


def _fake_image(size=32):
    image = bpy.data.images.new("rbx_test_tex", width=size, height=size, alpha=True)
    # Colorspace FIRST: Blender zeroes a generated image's RGB buffer on
    # any post-upload colorspace assignment (even a same-value one).
    image.colorspace_settings.name = "sRGB"
    image.pixels = [0.5] * (size * size * 4)
    image.update()
    return image


def _tiny_png(r, g, b):
    """A 1x1 RGB png with a distinct payload per colour tuple."""
    import struct
    import zlib

    def chunk(tag, data):
        header = struct.pack(">I", len(data)) + tag + data
        return header + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + bytes([r, g, b]))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def _entry(color=(0.35, 0.30, 0.20), material=816, use_2022=True):
    return {
        "name": "Slab",
        "class_name": "Part",
        "color": list(color) if color is not None else None,
        "transparency": 0.0,
        "reflectance": 0.0,
        "material": material,
        "_use_2022_materials": use_2022,
        "texture_id": None,
        "surface_appearance": {},
    }


def _scratch_node_ops(material):
    nodes = material.node_tree.nodes
    nodes.clear()
    for _ in range(4):
        nodes.new("ShaderNodeTexImage")
    for _ in range(2):
        nodes.new("ShaderNodeGamma")


def _live_check(material):
    try:
        return material.name in bpy.data.materials
    except ReferenceError:
        return False


class MaterialPipelineTests(unittest.TestCase):
    def setUp(self):
        self._created_meshes = []
        self._created_materials = []
        self._created_images = []
        self._image = _fake_image()
        self._created_images.append(self._image)

        def defer_aware_fetch(ref, name="texture", non_color=False):
            # Respect the deferred-image counter so deferral-context builds
            # leave texture nodes image-less, exactly like the real fetch.
            if textures._DEFER_TEXTURE_IMAGES:
                return None
            return self._image

        self._fetch_patch = mock.patch.object(
            textures, "fetch_texture_image", side_effect=defer_aware_fetch
        )
        self._fetch_patch.start()

    def tearDown(self):
        self._fetch_patch.stop()
        for mesh in self._created_meshes:
            try:
                if mesh.name in bpy.data.meshes:
                    bpy.data.meshes.remove(mesh, do_unlink=True)
            except ReferenceError:
                pass
        for material in self._created_materials:
            try:
                if material.name in bpy.data.materials:
                    bpy.data.materials.remove(material)
            except ReferenceError:
                pass
        for image in self._created_images:
            try:
                if image.name in bpy.data.images:
                    bpy.data.images.remove(image)
            except ReferenceError:
                pass
        textures._PART_MATERIAL_CACHE.clear()
        textures._OBJECT_TINT_BUILTIN_CACHE.clear()
        textures._BAKED_BUILTIN_TINT_CACHE.clear()
        textures._IMAGE_BYTES_CACHE.clear()
        textures._IMAGE_COLOR_USES.clear()
        textures._IMAGE_DATA_USES.clear()
        textures._ROLE_COPY_CACHE.clear()

    def _build(self, entry, name="rbx_test_mat"):
        material = textures.build_part_material(name, entry, material_name=name)
        self._created_materials.append(material)
        return material

    def test_builtin_material_graph_wiring(self):
        entry = _entry(color=(0.35, 0.30, 0.20), material=816)  # Concrete
        material = self._build(entry)
        tree = material.node_tree
        names = {node.name for node in tree.nodes}
        self.assertIn("RBX Material ColorMap", names)
        self.assertIn("RBX Material UV", names)
        self.assertIn("RBX Material Tint", names)
        self.assertIn("RBX Material ColorAttr", names)
        # Exactly one shared UV node for the whole material, not one per map.
        uv_nodes = [n for n in tree.nodes if n.name == "RBX Material UV"]
        self.assertEqual(len(uv_nodes), 1)
        # The tint chain drives Base Color (not the raw map and not flat colour).
        principled = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
        source = principled.inputs["Base Color"].links[0].from_node
        self.assertEqual(source.name, "RBX Material Tint")
        # The part colour read must be a Vertex Color node: the generic
        # Attribute node does not expose colour attributes in this build.
        color_attr = tree.nodes.get("RBX Material ColorAttr")
        self.assertIsNotNone(color_attr)
        self.assertEqual(color_attr.type, "VERTEX_COLOR")
        self.assertEqual(color_attr.layer_name, "RBXColor")
        # Base-material colour maps are sRGB (not Non-Color): only
        # MaterialVariant colour maps bind as linear data.
        color_map = tree.nodes.get("RBX Material ColorMap")
        self.assertIsNotNone(color_map)
        self.assertIsNotNone(color_map.image)
        self.assertEqual(color_map.image.colorspace_settings.name, "sRGB")

    def test_colormap_bind_forces_srgb(self):
        # A colour-map bind must restore sRGB even when a shared,
        # content-addressed datablock was flipped to Non-Color earlier in
        # the import (a PBR data bind built first).  Eevee samples colour
        # maps through the sRGB transfer curve and distorts Non-Color data
        # immediately; Cycles forgives the same mistake.
        # Simulate the earlier flip through a packed image: a raw
        # post-upload assignment on an unpacked generated image wipes the
        # buffer, so the test itself must not use that path.
        self._image.pack()
        self._image.colorspace_settings.name = "Non-Color"
        entry = _entry(color=(0.9, 0.1, 0.2), material=256)
        entry["surface_appearances"] = [
            {"color_map": "rbxassetid://111", "alpha_mode": 0}
        ]
        material = self._build(entry)
        node = material.node_tree.nodes.get("RBX ColorMap")
        self.assertIsNotNone(node)
        self.assertIsNotNone(node.image)
        self.assertEqual(node.image.colorspace_settings.name, "sRGB")
        # The restore must not zero the buffer (Blender drops generated
        # image pixels on ANY post-upload colorspace assignment).  0.5
        # quantizes to 128/255 in the byte buffer.
        self.assertAlmostEqual(node.image.pixels[0], 0.5, places=2)

    def test_shared_image_gets_per_role_views(self):
        # One image serving both a colour map and a normal map (same ref,
        # payload-keyed dedup) must never cross roles: the colour bind owns
        # the sRGB original and the normal bind gets a pixel-preserving
        # Non-Color copy of its own.
        entry = _entry(color=(0.9, 0.1, 0.2), material=256)
        entry["surface_appearances"] = [
            {"color_map": "rbxassetid://111", "alpha_mode": 0},
            {"normal_map": "rbxassetid://111"},
        ]
        material = self._build(entry, name="rbx_test_shared_cs")
        color_node = material.node_tree.nodes.get("RBX ColorMap")
        normal_node = material.node_tree.nodes.get("RBX NormalMapTex")
        self.assertIsNotNone(color_node)
        self.assertIsNotNone(normal_node)
        self.assertIsNotNone(color_node.image)
        self.assertIsNotNone(normal_node.image)
        self.assertIsNot(color_node.image, normal_node.image)
        self.assertEqual(color_node.image.colorspace_settings.name, "sRGB")
        self.assertEqual(normal_node.image.colorspace_settings.name, "Non-Color")
        # The copy keeps the pixels (0.5 quantizes to 128/255).
        self.assertAlmostEqual(normal_node.image.pixels[0], 0.5, places=2)

    def test_pbr_data_maps_strict_non_color(self):
        # Roughness/Metallic/Normal maps are DATA: strict Non-Color so Eevee
        # never applies the sRGB transfer curve to them.
        normal_img = _fake_image()
        rough_img = _fake_image()
        metal_img = _fake_image()
        self._created_images.extend([normal_img, rough_img, metal_img])
        images = {
            "rbxassetid://222": normal_img,
            "rbxassetid://333": rough_img,
            "rbxassetid://444": metal_img,
        }
        self._fetch_patch.stop()

        def per_ref_fetch(ref, name="texture", non_color=False):
            if textures._DEFER_TEXTURE_IMAGES:
                return None
            return images.get(ref)

        fetch_patch = mock.patch.object(
            textures, "fetch_texture_image", side_effect=per_ref_fetch
        )
        fetch_patch.start()
        try:
            entry = _entry(color=(0.5, 0.5, 0.5), material=256)
            entry["surface_appearances"] = [
                {
                    "normal_map": "rbxassetid://222",
                    "roughness_map": "rbxassetid://333",
                    "metalness_map": "rbxassetid://444",
                }
            ]
            material = self._build(entry, name="rbx_test_data_maps")
        finally:
            fetch_patch.stop()
            self._fetch_patch.start()
        nodes = material.node_tree.nodes
        normal_node = nodes.get("RBX NormalMapTex")
        self.assertIsNotNone(normal_node)
        self.assertIsNotNone(normal_node.image)
        self.assertEqual(normal_node.image.colorspace_settings.name, "Non-Color")
        # The flip must not wipe the buffer: Blender zeroes generated image
        # pixels on ANY post-upload colorspace assignment, so the setter
        # packs first (packed images survive flips).  0.5 quantizes to
        # 128/255 in the byte buffer.
        self.assertAlmostEqual(normal_node.image.pixels[0], 0.5, places=2)
        for node_name in ("RBX Surface metalness_map", "RBX Surface roughness_map"):
            node = nodes.get(node_name)
            self.assertIsNotNone(node)
            self.assertIsNotNone(node.image)
            self.assertEqual(node.image.colorspace_settings.name, "Non-Color")
            self.assertAlmostEqual(node.image.pixels[0], 0.5, places=2)

    def test_face_projection_axes_keep_patterns_upright(self):
        # X-normal walls: U runs along Z (horizontal), V along Y (up) — the
        # old (Y, Z) choice rotated every pattern 90° on half a building's
        # walls.  U also flips with the face normal sign so opposite faces
        # read correctly from outside.
        self.assertEqual(
            textures._face_projection_axes([(0, 0, 0), (0, 0, 4), (0, 2, 4)]),
            (2, 1, 1.0),  # -X face: U=+Z
        )
        self.assertEqual(
            textures._face_projection_axes([(0, 0, 4), (2, 0, 4), (2, 3, 4)]),
            (0, 1, 1.0),  # +Z face: U=+X
        )
        self.assertEqual(
            textures._face_projection_axes([(0, 4, 0), (2, 4, 0), (2, 4, 3)]),
            (0, 2, 1.0),  # -Y face
        )

    def test_builtin_material_uvs_keep_x_faces_upright(self):
        # Regression for the numpy path: a +X face must sample U from -Z
        # (outward-facing orientation) and V from Y at the built-in stud
        # density (10 studs/tile).
        mesh = bpy.data.meshes.new("rbx_test_uv_orient")
        self._created_meshes.append(mesh)
        verts = [
            (x, y, z)
            for x in (-1.0, 1.0)
            for y in (-1.0, 1.0)
            for z in (-1.0, 1.0)
        ]
        faces = [
            (4, 6, 7, 5),  # +X
            (0, 1, 3, 2),  # -X
            (2, 3, 7, 6),  # +Y
            (0, 4, 5, 1),  # -Y
            (1, 5, 7, 3),  # +Z
            (0, 2, 6, 4),  # -Z
        ]
        mesh.from_pydata(verts, [], faces)
        mesh.update()
        uv_layer = textures._ensure_builtin_material_uvs(mesh)
        self.assertIsNotNone(uv_layer)
        target = next(
            i
            for i, v in enumerate(mesh.vertices)
            if tuple(round(c, 4) for c in v.co) == (1.0, 1.0, -1.0)
        )
        found = []
        for poly in mesh.polygons:
            if poly.normal.x < 0.9:
                continue
            for loop_index in poly.loop_indices:
                if mesh.loops[loop_index].vertex_index == target:
                    found.append(uv_layer.data[loop_index].uv[:])
        self.assertEqual(len(found), 1)
        u, v = found[0]
        self.assertAlmostEqual(u, 0.2, places=5)  # +X face: U=-Z, z=-1 is U max
        self.assertAlmostEqual(v, 0.2, places=5)  # y=1  -> (y+1)/10

        # -X face keeps U=+Z: the matching corner (1, 1, -1) is U max there
        # too, but the sign flip must not touch it.
        target_m = next(
            i
            for i, v in enumerate(mesh.vertices)
            if tuple(round(c, 4) for c in v.co) == (-1.0, 1.0, -1.0)
        )
        found_m = []
        for poly in mesh.polygons:
            if poly.normal.x > -0.9:
                continue
            for loop_index in poly.loop_indices:
                if mesh.loops[loop_index].vertex_index == target_m:
                    found_m.append(uv_layer.data[loop_index].uv[:])
        self.assertEqual(len(found_m), 1)
        u_m, v_m = found_m[0]
        self.assertAlmostEqual(u_m, 0.0, places=5)  # -X face: U=+Z, z=-1 is U min
        self.assertAlmostEqual(v_m, 0.2, places=5)

    def test_no_color_skips_tint_chain(self):
        # Parts without a Color3 have nothing to tint with: the map links
        # straight through and no tint/attribute nodes are allocated.
        entry = _entry(color=None, material=816)
        material = self._build(entry)
        names = {node.name for node in material.node_tree.nodes}
        self.assertNotIn("RBX Material Tint", names)
        self.assertNotIn("RBX Material ColorAttr", names)
        principled = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        source = principled.inputs["Base Color"].links[0].from_node
        self.assertEqual(source.name, "RBX Material ColorMap")

    def test_material_cache_shares_and_splits(self):
        # Colour does NOT split built-in materials (tint comes from the
        # RBXColor attribute); the material id does.
        entry_a = _entry(color=(0.35, 0.30, 0.20), material=816)
        entry_b = _entry(color=(0.9, 0.1, 0.1), material=816)
        entry_c = _entry(color=(0.35, 0.30, 0.20), material=848)
        material_1 = textures.get_part_material("slab_1", entry_a)
        material_2 = textures.get_part_material("slab_2", entry_b)
        material_3 = textures.get_part_material("slab_3", entry_c)
        self._created_materials.extend([material_1, material_2, material_3])
        self.assertIs(material_1, material_2)
        self.assertIsNot(material_1, material_3)

    def test_cached_material_hits_are_cheap(self):
        entry = _entry()
        first = textures.get_part_material("slab", entry)
        self._created_materials.append(first)
        started = time.perf_counter()
        for _ in range(200):
            textures.get_part_material("slab", entry)
        elapsed = time.perf_counter() - started
        print(f"  [materials] 200 cache hits: {elapsed * 1000:.2f} ms")
        self.assertLess(elapsed, 1.0)

    def test_map_less_builtin_uses_attribute_tint_and_baked_variant(self):
        # Plastic has no texture maps, but it must still tint by part colour:
        # the shared material reads RBXColor through a Vertex Color node.
        entry = _entry(color=(0.2, 0.4, 0.9), material=256)
        material = textures.get_part_material("slab_plastic", entry)
        self._created_materials.append(material)
        names = {node.name for node in material.node_tree.nodes}
        self.assertIn("RBX Material ColorAttr", names)
        color_attr = material.node_tree.nodes.get("RBX Material ColorAttr")
        self.assertEqual(color_attr.type, "VERTEX_COLOR")
        principled = next(
            node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
        )
        source = principled.inputs["Base Color"].links[0].from_node
        self.assertEqual(source.name, "RBX Material ColorAttr")
        # The baked variant replaces the vertex-colour read with a
        # colour-managed RGB constant and must be a DIFFERENT datablock.
        baked = textures.baked_builtin_tint_material(entry)
        self._created_materials.append(baked)
        self.assertIsNot(baked, material)
        baked_attr = baked.node_tree.nodes.get("RBX Material ColorAttr")
        self.assertIsNotNone(baked_attr)
        self.assertEqual(baked_attr.type, "RGB")
        default = baked_attr.outputs["Color"].default_value
        self.assertAlmostEqual(default[0], 0.2, places=4)
        self.assertAlmostEqual(default[2], 0.9, places=4)

    def test_lazy_builtin_tint_uses_one_object_color_material(self):
        first_entry = _entry(color=(0.2, 0.4, 0.9), material=848)
        second_entry = _entry(color=(0.8, 0.1, 0.3), material=848)
        first = textures.object_tinted_builtin_material(first_entry)
        second = textures.object_tinted_builtin_material(second_entry)
        self._created_materials.append(first)
        self.assertIs(first, second)
        tint = first.node_tree.nodes.get("RBX Material Object Tint")
        self.assertIsNotNone(tint)
        self.assertEqual(tint.type, "OBJECT_INFO")
        self.assertIsNone(first.node_tree.nodes.get("RBX Material ColorAttr"))

        mesh = bpy.data.meshes.new("rbx_object_tint_mesh")
        obj = bpy.data.objects.new("rbx_object_tint_object", mesh)
        bpy.context.collection.objects.link(obj)
        try:
            textures.set_object_rbx_tint(obj, second_entry)
            self.assertAlmostEqual(obj.color[0], 0.8, places=5)
            self.assertAlmostEqual(obj.color[1], 0.1, places=5)
            self.assertAlmostEqual(obj.color[2], 0.3, places=5)
        finally:
            bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.meshes.remove(mesh)

    def test_baked_material_created_deferred_is_hydrated_by_sweep(self):
        # Baked tint materials are built inside the deferred-image pass while
        # geometry applies, and their entries' signatures can already be
        # marked hydrated by then.  The end-of-import sweep must still give
        # their texture nodes image datablocks, or they render black.
        entry = _entry(color=(0.7, 0.4, 0.1), material=848)  # Brick (mapped)
        for ref in textures.builtin_material_texture_refs(entry):
            key = textures._texture_asset_key(ref)
            textures._IMAGE_BYTES_CACHE[key] = b"stub"
        with textures.defer_texture_image_loading():
            baked = textures.baked_builtin_tint_material(entry)
        self._created_materials.append(baked)
        self.assertFalse(baked.get("RBXTextureHydrated", False))
        self.assertTrue(baked.get("RBXDeferredEntryJson"))
        color_node = baked.node_tree.nodes.get("RBX Material ColorMap")
        self.assertIsNotNone(color_node)
        self.assertIsNone(color_node.image)
        # The sweep hydrates it once the bytes are available.
        self.assertEqual(textures.hydrate_pending_baked_materials(), 1)
        self.assertTrue(baked.get("RBXTextureHydrated", False))
        self.assertIs(color_node.image, self._image)
        # Already-hydrated materials are not processed again.
        self.assertEqual(textures.hydrate_pending_baked_materials(), 0)

    def test_surface_appearance_overlay_composites_over_part_color(self):
        # AlphaMode.Overlay (0): final = map x a + partColour x (1-a).
        # Feeding the raw map RGB renders masked trim sheets (black RGB,
        # pattern in alpha) as solid black.
        entry = _entry(color=(0.2, 0.4, 0.9), material=256)
        entry["surface_appearance"] = {
            "color_map": "rbxassetid://111", "alpha_mode": 0
        }
        material = self._build(entry)
        names = {node.name for node in material.node_tree.nodes}
        self.assertIn("RBX Overlay Mix", names)
        mix = material.node_tree.nodes.get("RBX Overlay Mix")
        tex = material.node_tree.nodes.get("RBX ColorMap")
        factor = textures._socket_by_type(mix, "input", "Factor", "VALUE").links[0]
        # The factor samples a dedicated copy datablock so the tint multiply
        # and the mix factor never read the same image (Blender 5.1
        # mis-evaluates that combination).
        self.assertEqual(factor.from_node.name, "RBX Overlay Alpha")
        self.assertEqual(factor.from_socket.name, "Alpha")
        alpha_node = material.node_tree.nodes.get("RBX Overlay Alpha")
        self.assertIsNotNone(alpha_node)
        self.assertIs(alpha_node.image, textures._overlay_alpha_image_copy(tex.image))
        self.assertEqual(
            textures._socket_by_type(mix, "input", "B", "RGBA").links[0].from_node.name,
            "RBX ColorMap",
        )
        a_default = textures._socket_by_type(mix, "input", "A", "RGBA").default_value
        self.assertAlmostEqual(a_default[0], 0.2, places=3)
        self.assertAlmostEqual(a_default[2], 0.9, places=3)
        principled = next(
            n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"
        )
        source = principled.inputs["Base Color"].links[0].from_node
        self.assertEqual(source.name, "RBX Overlay Mix")

    def test_union_with_surface_appearance_uses_overlay(self):
        # A SurfaceAppearance colour map overrides CSG-baked vertex colours;
        # the material must build the overlay chain, not the per-vertex
        # colour chain.
        entry = _entry(color=(0.2, 0.4, 0.9), material=256)
        entry["union_mesh"] = {
            "colors": [(1.0, 0.0, 0.0, 1.0), (0.0, 1.0, 0.0, 1.0)],
        }
        entry["surface_appearance"] = {
            "color_map": "rbxassetid://111", "alpha_mode": 0
        }
        material = self._build(entry)
        names = {node.name for node in material.node_tree.nodes}
        self.assertNotIn("RBX VertexColor", names)
        self.assertIn("RBX ColorMap", names)
        self.assertIn("RBX Overlay Mix", names)

    def test_overlay_composites_stay_opaque(self):
        # An overlay composite (Texture instance or Overlay SA) reveals the
        # layer beneath IN-SHADER and never touches Principled Alpha, so the
        # material must stay opaque.  Forcing BLEND here made Eevee render
        # walls markedly darker than Cycles under world light (the
        # dark-wall symptom).  A Transparency SA uses dithered/hashed alpha
        # to avoid sorted-blend overlap artifacts.
        entry = _entry(color=(0.5, 0.5, 0.5), material=256)
        entry["texture_instances"] = [{"texture": "rbxassetid://111"}]
        material = self._build(entry)
        blend = getattr(material, "blend_method", None)
        if blend is not None:
            self.assertNotEqual(blend, "BLEND")
        render_method = getattr(material, "surface_render_method", None)
        if render_method is not None:
            self.assertNotEqual(render_method, "BLENDED")

        trans = _entry(color=(0.5, 0.5, 0.5), material=256)
        trans["surface_appearance"] = {
            "color_map": "rbxassetid://222", "alpha_mode": 1
        }
        trans_material = self._build(trans)
        render_method = getattr(trans_material, "surface_render_method", None)
        if render_method is not None:
            self.assertEqual(render_method, "DITHERED")
        blend = getattr(trans_material, "blend_method", None)
        if blend is not None:
            self.assertEqual(blend, "HASHED")

        # Actual BasePart transparency remains blended; dithering that would
        # turn a uniformly translucent part into visible screen-door noise.
        translucent = _entry(color=(0.5, 0.5, 0.5), material=256)
        translucent["transparency"] = 0.5
        translucent_material = self._build(translucent)
        render_method = getattr(translucent_material, "surface_render_method", None)
        if render_method is not None:
            self.assertEqual(render_method, "BLENDED")
        blend = getattr(translucent_material, "blend_method", None)
        if blend is not None:
            self.assertEqual(blend, "BLEND")

    def test_missing_color_map_falls_back_to_part_color(self):
        # A colour map that never arrives must not leave an image-less
        # texture node (black in Eevee).  SA chains fall back to the part
        # colour default; built-in chains swap white into the multiply.
        entry = _entry(color=(0.2, 0.4, 0.9), material=256)
        entry["surface_appearance"] = {
            "color_map": "rbxassetid://111", "alpha_mode": 0
        }
        with mock.patch.object(textures, "fetch_texture_image", return_value=None):
            with textures.defer_texture_image_loading():
                material = textures.build_part_material(
                    "sa_miss", entry, material_name="sa_miss"
                )
            self._created_materials.append(material)
            textures.hydrate_material_images(material, entry)
        # The dead overlay keeps its mix, but the factor is forced to zero,
        # so Result = A = the part colour set during the build — the render
        # equivalent of the old unlink-everything fallback (Base Color may
        # stay linked through that neutral mix).
        mix = material.node_tree.nodes.get("RBX Overlay Mix")
        self.assertIsNotNone(mix)
        factor_socket = textures._socket_by_type(mix, "input", "Factor", "VALUE")
        self.assertFalse(factor_socket.links)
        self.assertAlmostEqual(factor_socket.default_value, 0.0, places=5)
        a_socket = textures._socket_by_type(mix, "input", "A", "RGBA")
        self.assertAlmostEqual(a_socket.default_value[0], 0.2, places=3)
        self.assertAlmostEqual(a_socket.default_value[2], 0.9, places=3)

        # Built-in mapped chain (Brick): the dead texture becomes white so
        # the multiply keeps the tint instead of blacking it out.
        entry2 = _entry(color=(0.3, 0.3, 0.3), material=848)
        with mock.patch.object(textures, "fetch_texture_image", return_value=None):
            with textures.defer_texture_image_loading():
                material2 = textures.build_part_material(
                    "bm_miss", entry2, material_name="bm_miss"
                )
            self._created_materials.append(material2)
            textures.hydrate_material_images(material2, entry2)
        multiply = material2.node_tree.nodes.get("RBX Material Tint")
        source = multiply.inputs[0].links[0].from_node
        self.assertEqual(source.name, "RBX Material ColorMap Fallback")
        self.assertEqual(source.type, "RGB")
        self.assertAlmostEqual(
            source.outputs["Color"].default_value[0], 1.0, places=4
        )

    def test_baked_tint_applies_only_to_attribute_tinted_builtins(self):
        # SA/TextureID materials keep colour in their cache key, so only
        # bare built-ins may take the baked per-colour override; otherwise
        # two SA entries sharing material+colour would collide on one baked
        # datablock and render each other's texture.
        builtin = _entry(color=(0.5, 0.5, 0.5), material=848)
        self.assertTrue(textures.entry_uses_baked_tint(builtin))
        sa = _entry(color=(0.5, 0.5, 0.5), material=256)
        sa["surface_appearance"] = {"color_map": "rbxassetid://111"}
        self.assertFalse(textures.entry_uses_baked_tint(sa))
        texid = _entry(color=(0.5, 0.5, 0.5), material=256)
        texid["texture_id"] = "rbxassetid://222"
        self.assertFalse(textures.entry_uses_baked_tint(texid))
        plain = _entry(color=(0.5, 0.5, 0.5), material=None)
        self.assertFalse(textures.entry_uses_baked_tint(plain))

    def test_baked_tint_names_are_key_unique(self):
        # The baked datablock name must derive from the FULL key: the old
        # 24-bit hash() naming could map two different keys onto one name,
        # clobbering each other's graph on large maps (mixed-up colours).
        entry_a = _entry(color=(0.1, 0.2, 0.3), material=848)
        entry_b = _entry(color=(0.4, 0.5, 0.6), material=848)
        a = textures.baked_builtin_tint_material(entry_a)
        b = textures.baked_builtin_tint_material(entry_b)
        self._created_materials.extend([a, b])
        self.assertIsNot(a, b)
        self.assertNotEqual(a.name, b.name)
        na = a.node_tree.nodes.get("RBX Material ColorAttr")
        nb = b.node_tree.nodes.get("RBX Material ColorAttr")
        self.assertAlmostEqual(na.outputs["Color"].default_value[0], 0.1, places=3)
        self.assertAlmostEqual(nb.outputs["Color"].default_value[0], 0.4, places=3)

    def test_baked_cache_ignored_for_sa_entries(self):
        # An SA/TextureID entry shares (material, colour, transparency) with
        # a tint entry but never uses its baked material; the hydration pass
        # must not reach it, or the SA entry floods the baked datablock with
        # its own refs / lets the fallback white-swap its colour map.
        tint_entry = _entry(color=(0.3, 0.4, 0.5), material=848)
        baked = textures.baked_builtin_tint_material(tint_entry)
        self._created_materials.append(baked)
        sa_entry = _entry(color=(0.3, 0.4, 0.5), material=848)
        sa_entry["surface_appearance"] = {"color_map": "rbxassetid://999"}
        found = textures._cached_materials_for_entry(sa_entry)
        self.assertTrue(all(material is not baked for material in found))
        found_tint = textures._cached_materials_for_entry(tint_entry)
        self.assertIn(baked, found_tint)

    def test_stale_cached_materials_rebuild_in_place(self):
        # A datablock cached by an older add-on build lacks the graph-version
        # stamp; the next lookup must rebuild it in place rather than serve
        # the stale graph (a re-import in the same session used to keep the
        # pre-Vertex-Color chains alive).
        entry = _entry(color=(0.7, 0.4, 0.1), material=848)
        first = textures.get_part_material("slab", entry)
        self._created_materials.append(first)
        name = first.name
        del first["RBXMaterialGraphVersion"]
        second = textures.get_part_material("slab", entry)
        self.assertEqual(second.name, name)
        self.assertEqual(
            int(second["RBXMaterialGraphVersion"]),
            textures._MATERIAL_GRAPH_VERSION,
        )
        # Baked copies rebuild too and keep their RGB colour constant.
        baked_first = textures.baked_builtin_tint_material(entry)
        self._created_materials.append(baked_first)
        baked_name = baked_first.name
        del baked_first["RBXMaterialGraphVersion"]
        baked_second = textures.baked_builtin_tint_material(entry)
        self.assertEqual(baked_second.name, baked_name)
        self.assertEqual(
            int(baked_second["RBXMaterialGraphVersion"]),
            textures._MATERIAL_GRAPH_VERSION,
        )
        self.assertEqual(
            baked_second.node_tree.nodes.get("RBX Material ColorAttr").type, "RGB"
        )

    def test_plain_tint_material_shares_and_attributes(self):
        # Plain Color3 parts share one attribute-tinted material per
        # transparency; the per-mesh RBXColor attribute carries the colour.
        first = textures.plain_tint_material(0.0)
        second = textures.plain_tint_material(0.0)
        translucent = textures.plain_tint_material(0.4)
        self._created_materials.extend([first, translucent])
        self.assertIs(first, second)
        self.assertIsNot(first, translucent)
        self.assertTrue(first.get("RBXPlainTint"))
        names = {node.name for node in first.node_tree.nodes}
        self.assertIn("RBX Plain Color", names)
        principled = next(
            node for node in first.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
        )
        source = principled.inputs["Base Color"].links[0].from_node
        self.assertEqual(source.name, "RBX Plain Color")
        # Re-calls must not duplicate the attribute node.
        self.assertEqual(
            len([node for node in first.node_tree.nodes if node.name == "RBX Plain Color"]),
            1,
        )

        mesh = bpy.data.meshes.new("rbx_test_plain_attr")
        self._created_meshes.append(mesh)
        self.assertTrue(
            creation._populate_mesh_geometry(
                mesh,
                [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                [(0, 1, 2)],
            )
        )
        textures.ensure_plain_color_attribute(mesh, (1.0, 0.5, 0.25))
        layer = mesh.color_attributes.get("RBXColor")
        self.assertIsNotNone(layer)
        self.assertEqual(len(layer.data), 3)
        # Storage is plain sRGB (decode-on-read by the Vertex Color node):
        # 1.0 stays 1.0, and the raw Color3 value is stored unmodified.
        first_corner = [round(component, 3) for component in layer.data[0].color]
        self.assertEqual(first_corner[0], 1.0)
        # A second call OVERWRITES: asset-baked vertex colours must not win
        # over the part colour on plain/builtin tint parts.
        textures.ensure_plain_color_attribute(mesh, (0.0, 0.0, 0.0))
        self.assertEqual(len(mesh.color_attributes), 1)
        self.assertAlmostEqual(layer.data[0].color[0], 0.0, places=4)

    def test_entry_texture_bytes_ready(self):
        # Incremental hydration gate: an entry is only refreshable when its
        # bytes have landed (or are known-failed).
        entry = {
            "name": "Prop",
            "material": None,
            "color": [1.0, 1.0, 1.0],
            "transparency": 0.0,
            "reflectance": 0.0,
            "texture_id": "rbxassetid://99999999",
            "surface_appearance": {},
            "_use_2022_materials": True,
        }
        self.assertFalse(textures.entry_texture_bytes_ready(entry))
        textures._IMAGE_BYTES_CACHE["asset:99999999"] = b"png-bytes"
        try:
            self.assertTrue(textures.entry_texture_bytes_ready(entry))
        finally:
            textures._IMAGE_BYTES_CACHE.pop("asset:99999999", None)
        # A known prefetch failure counts as ready: the entry must be
        # finalized without its image, never retried forever.
        textures._IMAGE_PREFETCH_FAILURES["asset:99999999"] = "unavailable"
        try:
            self.assertTrue(textures.entry_texture_bytes_ready(entry))
        finally:
            textures._IMAGE_PREFETCH_FAILURES.pop("asset:99999999", None)
        # No refs at all: always ready.
        plain = dict(entry, texture_id=None)
        self.assertTrue(textures.entry_texture_bytes_ready(plain))

    def test_deferred_build_hydrates_images_in_place(self):
        # The deferred pass builds the full graph with empty image nodes; the
        # refresh assigns datablocks without a clear+rebuild of the graph.
        entry = _entry(color=(1.0, 1.0, 1.0), material=816)
        # The setUp mock ignores the defer flag; swap in one that honors it,
        # falling through to the real fetch (byte-cache driven) otherwise.
        self._fetch_patch.stop()
        real_fetch = textures.fetch_texture_image
        self._fetch_patch.start()

        def defer_aware_fetch(ref, name="texture", non_color=False):
            if textures._DEFER_TEXTURE_IMAGES:
                return None
            return real_fetch(ref, name=name, non_color=non_color)

        defer_patch = mock.patch.object(
            textures, "fetch_texture_image", side_effect=defer_aware_fetch
        )
        defer_patch.start()
        try:
            with textures.defer_texture_image_loading():
                material = textures.get_part_material("slab_defer", entry)
        finally:
            defer_patch.stop()
        # Hydration must run under the REAL fetch, not the setUp mock: the
        # mock answers every ref with one shared datablock, which the colour
        # bind would mark and thereby (correctly) refuse the normal flip.
        self._fetch_patch.stop()
        self._created_materials.append(material)
        node = material.node_tree.nodes.get("RBX Material ColorMap")
        self.assertIsNotNone(node)
        self.assertIsNone(node.image)
        self.assertFalse(material.get("RBXTextureHydrated"))
        # Seed the byte cache and hydrate through the real fetch path.  Each
        # map role gets DISTINCT bytes: identical payloads dedup into one
        # datablock, which the colour bind would mark and thereby (correctly)
        # refuse the normal map's Non-Color flip.
        maps = textures._builtin_material_entry(816)["maps"]
        color_png = _tiny_png(200, 40, 40)
        data_png = _tiny_png(128, 128, 255)
        for index, map_id in enumerate(maps):
            if not map_id:
                continue
            png = color_png if index == 0 else data_png
            textures._IMAGE_BYTES_CACHE[f"asset:{map_id}"] = png
            textures._IMAGE_BYTES_CACHE[f"rbxassetid://{map_id}"] = png
        before_images = {image.name for image in bpy.data.images}
        try:
            self.assertTrue(textures.refresh_cached_part_material(entry))
            node = material.node_tree.nodes.get("RBX Material ColorMap")
            self.assertIsNotNone(node)
            self.assertIsNotNone(node.image)
            self.assertTrue(material.get("RBXTextureHydrated"))
            # A second refresh is a cheap early return, not another build.
            self.assertTrue(textures.refresh_cached_part_material(entry))
            normal_node = material.node_tree.nodes.get("RBX Material Normal")
            self.assertIsNotNone(normal_node)
            if normal_node.image is not None:
                self.assertEqual(
                    normal_node.image.colorspace_settings.name, "Non-Color"
                )
        finally:
            for map_id in maps:
                if map_id:
                    textures._IMAGE_BYTES_CACHE.pop(f"asset:{map_id}", None)
                    textures._IMAGE_BYTES_CACHE.pop(f"rbxassetid://{map_id}", None)
            for image in list(bpy.data.images):
                if image.name not in before_images:
                    try:
                        bpy.data.images.remove(image)
                    except ReferenceError:
                        pass
            self._fetch_patch.start()

    def test_multiple_surface_appearances_composite_in_order(self):
        # Two stacked SAs (bottom first) must BOTH survive the build: the
        # first drives Base Color through its mix, the second's mix chains
        # over it.  Dropping every layer after the first (the old bug)
        # rendered the stacked trim sheets as one dark, wrong-looking mesh.
        entry = _entry(color=(0.2, 0.4, 0.9), material=256)
        entry["surface_appearances"] = [
            {"color_map": "rbxassetid://111", "alpha_mode": 0},
            {"color_map": "rbxassetid://222", "alpha_mode": 0},
        ]
        material = self._build(entry)
        nodes = material.node_tree.nodes
        first = nodes.get("RBX ColorMap")
        second = nodes.get("RBX ColorMap.1")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        mix0 = nodes.get("RBX Overlay Mix")
        mix1 = nodes.get("RBX Overlay Mix.1")
        self.assertIsNotNone(mix0)
        self.assertIsNotNone(mix1)
        principled = next(
            n for n in nodes if n.type == "BSDF_PRINCIPLED"
        )
        base_source = principled.inputs["Base Color"].links[0].from_socket
        self.assertEqual(base_source.node.name, "RBX Overlay Mix.1")
        # Layer 1 composites OVER layer 0: mix1.A = mix0.Result.
        a_socket = textures._socket_by_type(mix1, "input", "A", "RGBA")
        self.assertEqual(
            a_socket.links[0].from_socket,
            textures._socket_by_type(mix0, "output", "Result", "RGBA"),
        )
        # Registry covers both layers for hydration.
        registry = textures._overlay_layer_registry(material)
        self.assertEqual(
            [layer["node"] for layer in registry],
            ["RBX ColorMap", "RBX ColorMap.1"],
        )

    def test_multiple_texture_instances_stack_over_surface_appearance(self):
        # Texture children draw OVER the SA layers, bottom-first, so their
        # overlays must chain after the SA mix chain.
        entry = _entry(color=(0.2, 0.4, 0.9), material=256)
        entry["surface_appearances"] = [
            {"color_map": "rbxassetid://111", "alpha_mode": 0},
        ]
        entry["texture_instances"] = [
            {"texture": "rbxassetid://333"},
            {"texture": "rbxassetid://444"},
        ]
        material = self._build(entry)
        nodes = material.node_tree.nodes
        self.assertIsNotNone(nodes.get("RBX ColorMap"))
        self.assertIsNotNone(nodes.get("RBX TextureInstance.1"))
        self.assertIsNotNone(nodes.get("RBX TextureInstance.2"))
        mix0 = nodes.get("RBX Overlay Mix")       # SA layer
        mix1 = nodes.get("RBX Overlay Mix.1")     # first Texture child
        mix2 = nodes.get("RBX Overlay Mix.2")     # second Texture child
        self.assertIsNotNone(mix0)
        self.assertIsNotNone(mix1)
        self.assertIsNotNone(mix2)
        # The second decal chains over the first decal's result.
        a_socket = textures._socket_by_type(mix2, "input", "A", "RGBA")
        self.assertEqual(
            a_socket.links[0].from_socket,
            textures._socket_by_type(mix1, "output", "Result", "RGBA"),
        )
        registry = textures._overlay_layer_registry(material)
        self.assertEqual(
            [layer["node"] for layer in registry],
            ["RBX ColorMap", "RBX TextureInstance.1", "RBX TextureInstance.2"],
        )

    def test_last_surface_appearance_wins_pbr_maps(self):
        # Two SAs, each with its own normal/roughness/metalness: the LAST
        # layer providing each map drives the socket (topmost wins).
        entry = _entry(color=(0.2, 0.4, 0.9), material=256)
        entry["surface_appearances"] = [
            {
                "color_map": "rbxassetid://111",
                "alpha_mode": 0,
                "normal_map": "rbxassetid://n1",
                "roughness_map": "rbxassetid://r1",
                "metalness_map": "rbxassetid://m1",
            },
            {
                "color_map": "rbxassetid://222",
                "alpha_mode": 0,
                "roughness_map": "rbxassetid://r2",
            },
        ]
        material = self._build(entry)
        nodes = material.node_tree.nodes
        rough_tex = nodes.get("RBX Surface roughness_map")
        metal_tex = nodes.get("RBX Surface metalness_map")
        normal_tex = nodes.get("RBX NormalMapTex")
        # Normal/metalness come only from the FIRST layer.
        self.assertIsNotNone(rough_tex)
        self.assertIsNotNone(metal_tex)
        self.assertIsNotNone(normal_tex)

    def test_deferred_hydration_assigns_every_layer_and_alpha_factor(self):
        # The deferred pass must hydrate ALL registry layers and create one
        # alpha-copy factor node per overlay layer.
        entry = _entry(color=(0.2, 0.4, 0.9), material=256)
        entry["surface_appearances"] = [
            {"color_map": "rbxassetid://111", "alpha_mode": 0},
            {"color_map": "rbxassetid://222", "alpha_mode": 0},
        ]
        with textures.defer_texture_image_loading():
            material = textures.build_part_material(
                "multi_defer", entry, material_name="multi_defer"
            )
        self._created_materials.append(material)
        nodes = material.node_tree.nodes
        self.assertIsNone(nodes.get("RBX ColorMap").image)
        self.assertIsNone(nodes.get("RBX ColorMap.1").image)
        textures.hydrate_material_images(material, entry)
        first = nodes.get("RBX ColorMap")
        second = nodes.get("RBX ColorMap.1")
        self.assertIsNotNone(first.image)
        self.assertIsNotNone(second.image)
        # Each overlay mix gets its own alpha factor node, with the UV link
        # mirrored from its colour node.
        for tag, color_node in (("", first), (".1", second)):
            alpha_node = nodes.get(f"RBX Overlay Alpha{tag}")
            self.assertIsNotNone(alpha_node)
            self.assertIsNotNone(alpha_node.image)
            self.assertIsNot(alpha_node.image, color_node.image)
        self.assertTrue(material.get("RBXTextureHydrated"))

    def test_cache_key_splits_by_full_layer_lists(self):
        # Two parts with the same FIRST layer but different LOWER layers
        # must never share a material datablock.
        base = _entry(color=(0.2, 0.4, 0.9), material=256)
        base["surface_appearances"] = [
            {"color_map": "rbxassetid://111", "alpha_mode": 0},
            {"color_map": "rbxassetid://222", "alpha_mode": 0},
        ]
        different = dict(base)
        different["surface_appearances"] = [
            {"color_map": "rbxassetid://111", "alpha_mode": 0},
            {"color_map": "rbxassetid://333", "alpha_mode": 0},
        ]
        self.assertNotEqual(
            textures._part_material_cache_key(base),
            textures._part_material_cache_key(different),
        )

    def test_material_variant_swaps_color_map_and_keeps_tint(self):
        base = _entry(color=(0.3, 0.6, 0.9), material=816)
        variant = dict(
            base,
            material_variant="Concrete_Light",
            material_variant_data={
                "base_material": 816,
                "color_map": "rbxassetid://77777777",
                "normal_map": "",
                "metalness_map": "",
                "roughness_map": "",
            },
        )
        material_base = textures.get_part_material("slab_mv_base", base)
        material_variant = textures.get_part_material("slab_mv_var", variant)
        self._created_materials.extend([material_base, material_variant])
        self.assertIsNot(material_base, material_variant)
        node = material_variant.node_tree.nodes.get("RBX Material ColorMap")
        self.assertIsNotNone(node)
        self.assertIn("MaterialVariant", node.label)
        # The variant rides the same attribute tint chain.
        names = {n.name for n in material_variant.node_tree.nodes}
        self.assertIn("RBX Material ColorAttr", names)
        # The variant maps are prefetched/hydrated like builtin maps.
        refs = textures.builtin_material_texture_refs(variant)
        self.assertIn("rbxassetid://77777777", refs)

    def test_material_variant_colormap_binds_non_color(self):
        # MaterialVariant colour maps arrive linear-encoded: the node must
        # sample them Non-Color, not sRGB, or the tint chain double-gammas
        # them (the black/dark variant walls under Eevee).
        entry = dict(
            _entry(color=(0.3, 0.6, 0.9), use_2022=True),
            material_variant="Concrete_Light",
            material_variant_data={
                "base_material": 816,
                "color_map": "rbxassetid://77777777",
                "normal_map": "",
                "metalness_map": "",
                "roughness_map": "",
            },
        )
        variant_image = _fake_image()
        self._created_images.append(variant_image)
        self._fetch_patch.stop()

        def fetch(ref, name="texture", non_color=False):
            if textures._DEFER_TEXTURE_IMAGES:
                return None
            return variant_image if str(ref).endswith("77777777") else self._image

        patch = mock.patch.object(
            textures, "fetch_texture_image", side_effect=fetch
        )
        patch.start()
        try:
            material = textures.get_part_material("slab_mv_cs", entry)
        finally:
            patch.stop()
            self._fetch_patch.start()
        self._created_materials.append(material)
        node = material.node_tree.nodes.get("RBX Material ColorMap")
        self.assertIsNotNone(node)
        self.assertIsNotNone(node.image)
        self.assertEqual(node.image.colorspace_settings.name, "Non-Color")
        self.assertAlmostEqual(node.image.pixels[0], 0.5, places=2)

    def test_material_variant_overrides_unset_default_material(self):
        # Parts referencing a MaterialVariant often leave their own Material
        # at the default/unset.  The variant's BaseMaterial must still drive
        # the built-in pipeline: tint attribute, UV mode, map hydration.
        entry = dict(
            _entry(color=(0.3, 0.6, 0.9), use_2022=True),
            material=None,
            material_variant="Concrete_Light",
            material_variant_data={
                "base_material": 848,
                "color_map": "rbxassetid://88888888",
                "normal_map": "",
                "metalness_map": "",
                "roughness_map": "",
            },
        )
        self.assertTrue(textures.entry_uses_shared_builtin_tint(entry))
        self.assertEqual(textures._effective_material_id(entry), 848)
        material = textures.get_part_material("slab_mv_default", entry)
        self._created_materials.append(material)
        node = material.node_tree.nodes.get("RBX Material ColorMap")
        self.assertIsNotNone(node)
        self.assertIn("MaterialVariant", node.label)
        names = {n.name for n in material.node_tree.nodes}
        self.assertIn("RBX Material ColorAttr", names)
        # The collapse condition shares the same effective-material view.
        key = textures._part_material_cache_key(entry)
        self.assertIsNotNone(key)
        self.assertEqual(key[0], 848)
        self.assertEqual(key[1], textures.variant_signature(entry))
        self.assertEqual(key[2], ())

    def test_material_variant_beats_material_and_scales_uvs(self):
        # The variant takes COMPLETE priority: the part's own Enum.Material
        # is ignored for maps, tinting, and UV density alike.
        entry = dict(
            _entry(color=(0.25, 0.5, 0.75), material=256),  # Plastic
            material_variant="Brick_Retexture",
            material_variant_data={
                "base_material": 848,  # Brick
                "color_map": "rbxassetid://99999999",
                "normal_map": "",
                "metalness_map": "",
                "roughness_map": "",
                "studs_per_tile": 5.0,
            },
        )
        self.assertEqual(textures._effective_material_id(entry), 848)
        self.assertEqual(textures._builtin_material_for_entry(entry)["name"], "Brick")
        material = textures.get_part_material("slab_mv_prio", entry)
        self._created_materials.append(material)
        tree = material.node_tree
        node = tree.nodes.get("RBX Material ColorMap")
        self.assertIsNotNone(node)
        self.assertIn("MaterialVariant", node.label)
        # Built-ins default to 10 studs per tile; the variant scales the
        # shared UV layer by 10/5 through a VectorMath node.
        self.assertEqual(textures._BUILTIN_MATERIAL_STUDS_PER_TILE, 10.0)
        scale_node = tree.nodes.get("RBX Material UV Scale")
        self.assertIsNotNone(scale_node)
        self.assertEqual(scale_node.operation, "SCALE")
        self.assertAlmostEqual(scale_node.inputs["Scale"].default_value, 2.0)
        source = node.inputs["Vector"].links[0].from_node
        self.assertEqual(source.name, "RBX Material UV Scale")
        # Without the variant the part's own material is used untouched.
        no_variant = dict(entry, material_variant=None, material_variant_data=None)
        self.assertEqual(textures._effective_material_id(no_variant), 256)
        self.assertNotEqual(
            textures._part_material_cache_key(entry),
            textures._part_material_cache_key(no_variant),
        )

    def test_classic_texture_child_overrides_variant_and_tiles(self):
        # A classic Texture child replaces the material look entirely — the
        # variant's maps must not appear — and tiles at the child's own
        # studs-per-tile density through the material UV layer.
        entry = dict(
            _entry(color=(0.9, 0.9, 0.9), material=816),
            material_variant="PaintedBrick",
            material_variant_data={
                "base_material": 848,
                "color_map": "rbxassetid://99999999",
                "normal_map": "",
                "metalness_map": "",
                "roughness_map": "",
                "studs_per_tile": 5.0,
            },
            texture_id="rbxassetid://11254907942",
            texture_studs_per_tile=30.0,
        )
        material = textures.get_part_material("wall_texchild", entry)
        self._created_materials.append(material)
        tree = material.node_tree
        tex = tree.nodes.get("RBX TextureID")
        self.assertIsNotNone(tex)
        self.assertIsNone(tree.nodes.get("RBX Material ColorMap"))
        uv = tree.nodes.get("RBX Material UV")
        self.assertIsNotNone(uv)
        scale = tree.nodes.get("RBX Material UV Scale")
        self.assertIsNotNone(scale)
        self.assertAlmostEqual(scale.inputs["Scale"].default_value, 1.0 / 3.0)
        source = tex.inputs["Vector"].links[0].from_node
        self.assertEqual(source.name, "RBX Material UV Scale")
        # The texture's density splits the cache key.
        key_a = textures._part_material_cache_key(entry)
        key_b = textures._part_material_cache_key(
            dict(entry, texture_studs_per_tile=None)
        )
        self.assertNotEqual(key_a, key_b)

    def test_texture_instance_composites_over_material(self):
        # A child Texture instance draws OVER the part's material; the
        # material's maps must stay bound underneath, and the instance must
        # composite by image alpha rather than driving Principled Alpha.
        entry = dict(
            _entry(color=(0.9, 0.9, 0.9), material=816),  # Concrete
            texture_instance={
                "texture": "rbxassetid://11254907942",
                "studs_per_tile": 30.0,
                "transparency": 0.0,
            },
        )
        material = textures.get_part_material("wall_texinst", entry)
        self._created_materials.append(material)
        tree = material.node_tree
        self.assertIsNotNone(tree.nodes.get("RBX Material ColorMap"))
        tex = tree.nodes.get("RBX TextureInstance")
        self.assertIsNotNone(tex)
        mix = tree.nodes.get("RBX Overlay Mix")
        self.assertIsNotNone(mix)
        # The mix's underneath layer is the material tint chain, not a flat
        # part colour default: input A must be driven by a link.
        self.assertTrue(
            textures._socket_by_type(mix, "input", "A", "RGBA").links
        )
        # Factor comes from the texture alpha, so no Principled Alpha link.
        principled = next(
            n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"
        )
        alpha_input = principled.inputs.get("Alpha")
        self.assertIsNotNone(alpha_input)
        self.assertFalse(alpha_input.links)
        # Instances tile at their own studs-per-tile density through a
        # dedicated scale chain; the material's own maps keep the default
        # density and must not inherit the instance's.
        uv = tree.nodes.get("RBX Material UV")
        self.assertIsNotNone(uv)
        self.assertTrue(tex.inputs["Vector"].links)
        instance_scale = tree.nodes.get("RBX Instance UV Scale")
        self.assertIsNotNone(instance_scale)
        self.assertAlmostEqual(instance_scale.inputs["Scale"].default_value, 1.0 / 3.0)
        self.assertIsNone(tree.nodes.get("RBX Material UV Scale"))
        mat_tex = tree.nodes.get("RBX Material ColorMap")
        mat_vector_source = mat_tex.inputs["Vector"].links[0].from_node
        self.assertEqual(mat_vector_source.name, "RBX Material UV")
        # The instance splits the cache key.
        key_a = textures._part_material_cache_key(entry)
        key_b = textures._part_material_cache_key(dict(entry, texture_instance=None))
        self.assertNotEqual(key_a, key_b)

    def test_texture_instance_density_does_not_retile_variant(self):
        # MaterialVariant density scales the material maps; the instance's
        # own density must not compound into that chain.
        entry = dict(
            _entry(color=(0.9, 0.9, 0.9), material=816),
            material_variant="PaintedBrick",
            material_variant_data={
                "base_material": 848,
                "color_map": "rbxassetid://99999999",
                "normal_map": "",
                "metalness_map": "",
                "roughness_map": "",
                "studs_per_tile": 5.0,
            },
            texture_instance={
                "texture": "rbxassetid://11254907942",
                "studs_per_tile": 30.0,
            },
        )
        material = textures.get_part_material("wall_texinst_variant", entry)
        self._created_materials.append(material)
        tree = material.node_tree
        mat_scale = tree.nodes.get("RBX Material UV Scale")
        self.assertIsNotNone(mat_scale)
        self.assertAlmostEqual(mat_scale.inputs["Scale"].default_value, 2.0)
        inst_scale = tree.nodes.get("RBX Instance UV Scale")
        self.assertIsNotNone(inst_scale)
        self.assertAlmostEqual(inst_scale.inputs["Scale"].default_value, 1.0 / 3.0)
        # Both chains feed from the raw shared UV layer, never each other.
        mat_source = mat_scale.inputs[0].links[0].from_node
        inst_source = inst_scale.inputs[0].links[0].from_node
        self.assertEqual(mat_source.name, "RBX Material UV")
        self.assertEqual(inst_source.name, "RBX Material UV")
        inst_tex = tree.nodes.get("RBX TextureInstance")
        self.assertEqual(
            inst_tex.inputs["Vector"].links[0].from_node.name,
            "RBX Instance UV Scale",
        )

    def test_texture_instance_fade_scales_alpha(self):
        # Child Texture.Transparency fades the whole overlay by scaling the
        # composite factor, leaving the material visible underneath.
        entry = dict(
            _entry(color=(0.5, 0.5, 0.5), material=816),
            texture_instance={
                "texture": "rbxassetid://11254907942",
                "transparency": 0.4,
            },
        )
        material = textures.get_part_material("wall_texinst_fade", entry)
        self._created_materials.append(material)
        tree = material.node_tree
        fade = tree.nodes.get("RBX Overlay Alpha Scale")
        self.assertIsNotNone(fade)
        self.assertAlmostEqual(fade.inputs[1].default_value, 0.6)

    def test_texture_instance_color_tints_overlay(self):
        # Child Texture.Color3 multiplies into the texture RGB before the
        # overlay composite; a white instance colour skips the tint node.
        entry = dict(
            _entry(color=(0.5, 0.5, 0.5), material=816),
            texture_instance={
                "texture": "rbxassetid://11254907942",
                "color": (1.0, 0.5, 0.25),
            },
        )
        material = textures.get_part_material("wall_texinst_tint", entry)
        self._created_materials.append(material)
        tree = material.node_tree
        tint_mix = tree.nodes.get("RBX Overlay Tint")
        self.assertIsNotNone(tint_mix)
        tint_a = textures._socket_by_type(tint_mix, "input", "A", "RGBA")
        self.assertAlmostEqual(tint_a.default_value[0], 1.0)
        self.assertAlmostEqual(tint_a.default_value[1], 0.5)
        mix = tree.nodes.get("RBX Overlay Mix")
        b_source = textures._socket_by_type(mix, "input", "B", "RGBA").links[0].from_node
        self.assertEqual(b_source.name, "RBX Overlay Tint")
        # White instance colour skips the tint node entirely.
        entry_white = dict(
            entry,
            texture_instance={
                "texture": "rbxassetid://11254907942",
                "color": (1.0, 1.0, 1.0),
            },
        )
        material_white = textures.get_part_material("wall_texinst_untinted", entry_white)
        self._created_materials.append(material_white)
        tree_white = material_white.node_tree
        self.assertIsNone(tree_white.nodes.get("RBX Overlay Tint"))
        mix_white = tree_white.nodes.get("RBX Overlay Mix")
        self.assertEqual(
            textures._socket_by_type(mix_white, "input", "B", "RGBA").links[0].from_node.name,
            "RBX TextureInstance",
        )

    def test_classic_texture_id_still_replaces_material(self):
        # MeshPart.TextureID keeps its classic semantics: it replaces the
        # material look, and a co-present child instance is not bound.
        entry = dict(
            _entry(color=(0.9, 0.9, 0.9), material=816),
            texture_id="rbxassetid://11111111",
            texture_instance={"texture": "rbxassetid://22222222"},
        )
        material = textures.get_part_material("wall_texid_prio", entry)
        self._created_materials.append(material)
        tree = material.node_tree
        self.assertIsNotNone(tree.nodes.get("RBX TextureID"))
        self.assertIsNone(tree.nodes.get("RBX TextureInstance"))
        self.assertIsNone(tree.nodes.get("RBX Material ColorMap"))

    def test_builtin_map_ref_normalization(self):
        self.assertEqual(textures._builtin_map_ref(816), "rbxassetid://816")
        self.assertEqual(textures._builtin_map_ref("9920484153"), "rbxassetid://9920484153")
        self.assertEqual(
            textures._builtin_map_ref("rbxassetid://123"), "rbxassetid://123"
        )
        self.assertEqual(textures._builtin_map_ref(""), "")
        self.assertEqual(textures._builtin_map_ref(None), "")

    def test_builtin_build_fetches_normalized_refs(self):
        # Guards against the regression where bare map ints or double-
        # prefixed variant strings reached the fetcher and killed the build.
        seen = []

        def recording(ref, name="texture", non_color=False):
            seen.append(str(ref))
            return self._image

        patch = mock.patch.object(
            textures, "fetch_texture_image", side_effect=recording
        )
        patch.start()
        try:
            material = textures.get_part_material("slab_refs", _entry(material=816))
            self._created_materials.append(material)
        finally:
            patch.stop()
        self.assertTrue(seen)
        for ref in seen:
            self.assertTrue(ref.startswith("rbxassetid://"), ref)

    def test_material_build_cost_report(self):
        # Worst case: every entry is a new visual variant, so every build
        # pays the full node-graph cost. Prints the per-material cost.
        started = time.perf_counter()
        count = 20
        for index in range(count):
            entry = _entry(color=(0.2 + index * 0.02, 0.4, 0.6))
            self._build(entry, name=f"rbx_test_mat_{index}")
        elapsed = time.perf_counter() - started
        per_material = elapsed / count
        print(f"  [materials] {count} full builds: {elapsed * 1000:.2f} ms "
              f"({per_material * 1000:.2f} ms each)")
        self.assertLess(per_material, 0.25)

    def test_uv_layer_generation_timing(self):
        # A flat 100x100 grid -> 20k triangles -> 60k loops. The UV layer
        # build must be a bulk numpy pass, not per-loop RNA writes.
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy unavailable")
        n = 100
        xs, ys = np.meshgrid(np.arange(n, dtype=np.float32), np.arange(n, dtype=np.float32))
        positions = np.stack([xs.ravel(), ys.ravel(), np.zeros(n * n, dtype=np.float32)], axis=1)
        faces = []
        for y in range(n - 1):
            for x in range(n - 1):
                v = y * n + x
                faces.append((v, v + 1, v + n))
                faces.append((v + 1, v + n + 1, v + n))
        mesh = bpy.data.meshes.new("rbx_test_uv")
        self._created_meshes.append(mesh)
        self.assertTrue(creation._populate_mesh_geometry(mesh, positions, faces))
        started = time.perf_counter()
        uv_layer = textures._ensure_builtin_material_uvs(mesh)
        elapsed = time.perf_counter() - started
        loop_count = len(mesh.loops)
        print(f"  [materials] UV layer for {loop_count} loops: {elapsed * 1000:.2f} ms "
              f"({elapsed / loop_count * 1e9:.0f} ns/loop)")
        self.assertIsNotNone(uv_layer)
        self.assertEqual(len(uv_layer.data), loop_count)
        self.assertLess(elapsed, 5.0)

    def test_scale_and_gc_report(self):
        # Place-scale experiment: hundreds of unique variants plus thousands
        # of cache hits, run once with gc enabled and once disabled. The
        # delta is the cost of python GC churn during material construction;
        # a large delta means allocations (not builds) are the lag.
        import gc

        variants = 150
        colors = [(0.1 + (i % 37) * 0.02, 0.3 + (i % 17) * 0.03, 0.5) for i in range(variants)]
        entries = [_entry(color=color) for color in colors]

        def run_loop():
            started = time.perf_counter()
            for index, entry in enumerate(entries):
                material = textures.get_part_material(f"scale_{index}", entry)
                self._created_materials.append(material)
            for index in range(variants * 3):
                textures.get_part_material(f"scale_{index % variants}", entries[index % variants])
            return time.perf_counter() - started

        textures._PART_MATERIAL_CACHE.clear()
        gc.enable()
        gc.collect()
        with_gc = run_loop()
        textures._PART_MATERIAL_CACHE.clear()
        gc.collect()
        gc.disable()
        without_gc = run_loop()
        gc.enable()
        print(f"  [materials] {variants} colour variants (1 build + {variants * 3} hits): "
              f"gc on {with_gc * 1000:.2f} ms | gc off {without_gc * 1000:.2f} ms "
              f"| gc overhead {max(0.0, with_gc - without_gc) * 1000:.2f} ms")

        # Phase isolation: does the cost live in datablock creation, node
        # tree rebuild, or the build logic itself?
        existing = len(bpy.data.materials)
        started = time.perf_counter()
        for index in range(variants):
            material = bpy.data.materials.new(f"rbx_test_raw_{index}")
            self._created_materials.append(material)
        t_new = time.perf_counter() - started
        print(f"  [materials] raw materials.new x{variants} (with {existing} existing): "
              f"{t_new * 1000:.2f} ms ({t_new / variants * 1000:.2f} ms each)")

        # Full build cost at three total-material counts to expose
        # count-dependent growth. Fresh names each round so nothing is reused.
        for round_index, existing_count in enumerate((0, 100, 300)):
            if existing_count:
                for index in range(existing_count):
                    self._created_materials.append(
                        bpy.data.materials.new(f"rbx_test_fill_{round_index}_{index}")
                    )
            started = time.perf_counter()
            for index in range(40):
                entry = _entry(color=(0.1 + (index % 37) * 0.02, 0.3 + (index % 17) * 0.03, 0.5))
                material = textures.build_part_material(
                    f"scale_iso_{round_index}_{index}", entry,
                    material_name=f"rbx_iso_{round_index}_{index}",
                )
                self._created_materials.append(material)
            elapsed = time.perf_counter() - started
            total = len(bpy.data.materials)
            print(f"  [materials] 40 full builds at {total} total materials: "
                  f"{elapsed * 1000:.2f} ms ({elapsed / 40 * 1000:.2f} ms each)")

        # Micro-bisect: which bpy primitive carries the count-dependent cost?
        def micro(label, func, count=400):
            started = time.perf_counter()
            for index in range(count):
                func(index)
            elapsed = time.perf_counter() - started
            total = len(bpy.data.materials)
            print(f"  [materials] {label} x{count} at {total} materials: "
                  f"{elapsed * 1000:.2f} ms ({elapsed / count * 1000:.3f} ms each)")
            return elapsed

        micro("materials.get(missing)", lambda i: bpy.data.materials.get(f"rbx_missing_{i}"))
        micro("materials.new(unique)", lambda i: bpy.data.materials.new(f"rbx_micro_new_{i}"))
        scratch = bpy.data.materials.get("rbx_micro_new_0")
        scratch.use_nodes = True
        micro("nodes.clear+new x6", lambda i: _scratch_node_ops(scratch))
        micro("live check (name in collection)", lambda i: _live_check(scratch))

        self.assertLess(with_gc, 12.0)

    def test_plain_material_does_not_register_fake_clothing_hydration(self):
        from roblox_animations.rig import clothing

        clothing.set_clothing_context()
        plain = _entry(material=None)
        plain["_rbx_lazy_material_signature"] = import_ops._lazy_material_signature(plain)
        self.assertFalse(import_ops._lazy_material_needs_hydration(plain))

        torso = _entry(material=None)
        torso["name"] = "UpperTorso"
        torso["_rbx_lazy_material_signature"] = import_ops._lazy_material_signature(torso)
        clothing.set_clothing_context(face_texture="rbxassetid://1")
        try:
            self.assertTrue(import_ops._lazy_material_needs_hydration(torso))
        finally:
            clothing.set_clothing_context()

    def test_clothing_dependencies_and_cache_identity_follow_context(self):
        from roblox_animations.rig import clothing

        torso = _entry(material=None)
        torso["name"] = "UpperTorso"
        clothing.set_clothing_context(
            shirt_template="rbxassetid://10",
            pants_template="rbxassetid://20",
        )
        try:
            refs = textures.material_texture_refs(torso)
            self.assertIn("rbxassetid://10", refs)
            self.assertIn("rbxassetid://20", refs)
            first_key = textures.part_material_identity(torso)
            clothing.set_clothing_context(
                shirt_template="rbxassetid://11",
                pants_template="rbxassetid://20",
            )
            self.assertNotEqual(first_key, textures.part_material_identity(torso))
        finally:
            clothing.set_clothing_context()

    def test_clothing_and_stud_nodes_bind_uvmap_explicitly(self):
        from roblox_animations.rig import clothing

        clothing.set_clothing_context(body_colors={"torso": (0.2, 0.4, 0.6)})
        try:
            torso = _entry(material=None)
            torso["name"] = "UpperTorso"
            with mock.patch.object(
                clothing, "get_limb_texture", return_value=self._image
            ):
                material = self._build(torso, name="rbx_test_clothing_uv")
            clothing_node = material.node_tree.nodes.get("RBX ClothingBake")
            self.assertIsNotNone(clothing_node)
            self.assertEqual(
                clothing_node.inputs["Vector"].links[0].from_node.uv_map,
                "UVMap",
            )
        finally:
            clothing.set_clothing_context()

        stud = textures._primitive_surface_material((0.5, 0.5, 0.5), 3)
        self._created_materials.append(stud)
        for node_name in ("RBX Stud Diffuse", "RBX Stud Normal"):
            node = stud.node_tree.nodes.get(node_name)
            self.assertIsNotNone(node)
            self.assertEqual(
                node.inputs["Vector"].links[0].from_node.uv_map,
                "UVMap",
            )


if __name__ == "__main__":
    unittest.main()
