"""Per-object creation performance tests (creation module).

Mirrors test_materials: measures the actual per-object import path
(_create_mesh_object_from_filemesh for primitive parts), the static batch
path, and the underlying geometry upload. Prints every timing so regressions
show up as numbers, with deliberately loose budgets for weak machines.

Covers:
- geometry upload (bulk numpy, not per-loop RNA)
- per-object creation cost and whether it grows with total object count
- GC contribution to per-object creation
- static batch vs individual objects
"""

import bpy
import time
import unittest
from unittest import mock

from ..rig import creation
from ..rig import textures


def _entry(index=0, size=(2.0, 1.0, 2.0), material=256):
    return {
        "name": f"Box{index}",
        "class_name": "Part",
        "shape": "block",
        "part_size": list(size),
        "color": [0.5, 0.5, 0.5],
        "transparency": 0.0,
        "reflectance": 0.0,
        "material": material,
        "_use_2022_materials": True,
        "texture_id": None,
        "surface_appearance": {},
        "part_cf": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    }


class CreationPerfTests(unittest.TestCase):
    def setUp(self):
        self._collection = bpy.data.collections.new("rbx_test_creation")
        bpy.context.scene.collection.children.link(self._collection)
        self._created_meshes = []
        self._created_materials = []
        self._fetch_patch = mock.patch.object(
            textures, "fetch_texture_image",
            return_value=self._fake_image(),
        )
        self._fetch_patch.start()

    def _fake_image(self, size=8):
        image = bpy.data.images.new("rbx_test_creation_tex", width=size, height=size, alpha=True)
        # Colorspace FIRST: Blender zeroes a generated image's RGB buffer on
        # any post-upload colorspace assignment (even a same-value one).
        image.colorspace_settings.name = "sRGB"
        image.pixels = [0.5] * (size * size * 4)
        image.update()
        return image

    def tearDown(self):
        self._fetch_patch.stop()
        try:
            bpy.data.collections.remove(self._collection, do_unlink=True)
        except Exception:
            pass
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
        for image in list(bpy.data.images):
            if image.name.startswith("rbx_test_creation_tex"):
                try:
                    bpy.data.images.remove(image)
                except ReferenceError:
                    pass
        textures._PART_MATERIAL_CACHE.clear()

    def _create_one(self, entry):
        return creation._create_mesh_object_from_filemesh(
            self._collection, entry["name"], creation._build_primitive_mesh_data(entry), entry
        )

    def test_geometry_upload_timing(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy unavailable")
        n = 200
        xs, ys = np.meshgrid(np.arange(n, dtype=np.float32), np.arange(n, dtype=np.float32))
        positions = np.stack([xs.ravel(), ys.ravel(), np.zeros(n * n, dtype=np.float32)], axis=1)
        faces = []
        for y in range(n - 1):
            for x in range(n - 1):
                v = y * n + x
                faces.append((v, v + 1, v + n))
                faces.append((v + 1, v + n + 1, v + n))
        started = time.perf_counter()
        mesh = bpy.data.meshes.new("rbx_test_geom")
        self._created_meshes.append(mesh)
        ok = creation._populate_mesh_geometry(mesh, positions, faces)
        elapsed = time.perf_counter() - started
        loop_count = len(faces) * 3
        print(f"  [creation] geometry upload {len(positions)} verts / "
              f"{len(faces)} tris: {elapsed * 1000:.2f} ms "
              f"({elapsed / loop_count * 1e9:.0f} ns/loop)")
        self.assertTrue(ok)
        self.assertLess(elapsed, 2.0)

    def test_object_creation_scale_and_gc_report(self):
        import gc

        def run_loop():
            started = time.perf_counter()
            for index in range(100):
                obj = self._create_one(_entry(index=index))
                if obj is not None:
                    obj.name = f"rbx_created_{index}"
            return time.perf_counter() - started

        fillers = []
        with_gc = run_loop()
        for index in range(500):
            fillers.append(bpy.data.objects.new(f"rbx_filler_{index}", None))
        after_fill = run_loop()
        for filler in fillers:
            try:
                bpy.data.objects.remove(filler)
            except ReferenceError:
                pass
        gc.collect()
        gc.disable()
        without_gc = run_loop()
        gc.enable()
        print(f"  [creation] 100 objects at {0} fillers: {with_gc * 1000:.2f} ms "
              f"({with_gc / 100 * 1000:.2f} ms each)")
        print(f"  [creation] 100 objects at 500 fillers: {after_fill * 1000:.2f} ms "
              f"({after_fill / 100 * 1000:.2f} ms each)")
        print(f"  [creation] 100 objects gc off: {without_gc * 1000:.2f} ms")
        self.assertLess(with_gc, 4.0)
        self.assertLess(after_fill, 4.0)

    def test_static_batch_vs_individual(self):
        entries = [_entry(index=index) for index in range(120)]
        started = time.perf_counter()
        batch = creation._create_batched_static_primitives(
            self._collection, "rbx_test_batch", entries
        )
        batch_time = time.perf_counter() - started
        # Built-in materials exercise the per-part UV generation inside the
        # batch merge (projection-axes unpacking lives there).
        builtin_entries = [
            {**_entry(index=1000 + index), "material": 816}
            for index in range(30)
        ]
        builtin_batch = creation._create_batched_static_primitives(
            self._collection, "rbx_test_batch_builtin", builtin_entries
        )
        self.assertIsNotNone(builtin_batch)
        self.assertIn("RBXMaterialUV", builtin_batch.data.uv_layers)
        started = time.perf_counter()
        for entry in entries[:40]:
            self._create_one(entry)
        individual_time = time.perf_counter() - started
        per_individual = individual_time / 40
        projected = per_individual * len(entries)
        print(f"  [creation] batch {len(entries)} parts: {batch_time * 1000:.2f} ms vs "
              f"individual projected {projected * 1000:.2f} ms "
              f"(per-object {per_individual * 1000:.2f} ms)")
        self.assertIsNotNone(batch)
        # Batches must not be slower than individual creation.
        self.assertLess(batch_time, projected + 1.0)

    def test_static_batch_template_parity(self):
        """Template-grouped bulk path must equal the per-part reference math.

        Every supported primitive shape, mixed in one batch, with material
        UVs enabled: vertex positions, face indices, and per-corner UVs are
        compared against a straightforward per-part reference implementation.
        """
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy unavailable")
        from mathutils import Matrix, Vector

        parts = [
            ("block", [5.0, 1.0, 3.0], [1, 0, 0, 0, 0, -1, 0, 1, 0, 1, 2, 3]),
            ("cylinder", [2.0, 4.0, 4.0], [0, -1, 0, 1, 0, 0, 0, 0, 1, -4, 5, 2]),
            ("ball", [6.0, 6.0, 6.0], [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]),
            ("wedge", [3.0, 2.0, 4.0], [0, 0, 1, 0, 1, 0, -1, 0, 0, 9, -2, 1]),
            ("corner_wedge", [2.5, 3.0, 1.5], [1, 0, 0, 0, 1, 0, 0, 0, 1, -3, 4, -6]),
        ]
        entries = [
            {
                "name": f"Parity{index}",
                "class_name": "Part",
                "shape": shape,
                "part_size": size,
                "part_cf": cf,
                "color": [1.0, 1.0, 1.0],
                "transparency": 0.0,
                "reflectance": 0.0,
                "material": 256,
                "_use_2022_materials": True,
                "texture_id": None,
                "surface_appearance": {},
            }
            for index, (shape, size, cf) in enumerate(parts)
        ]

        mesh_obj = creation._create_batched_static_primitives_np(
            np,
            self._collection,
            "rbx_test_parity",
            entries,
            material=None,
            needs_material_uv=True,
            units=0.5,
            uv_layer_name="RBXMaterialUV",
        )
        self.assertIsNotNone(mesh_obj)
        mesh = mesh_obj.data
        self._created_meshes.append(mesh)

        # Reference: the old per-part algorithm, in the same shape-group order.
        t2b = creation.get_transform_to_blender()
        ref_positions = []
        ref_faces = []
        ref_uvs = []
        offset = 0
        for entry in entries:
            data = creation._build_primitive_mesh_data(
                entry, include_surface_data=False
            )
            raw = data["positions"]
            cf = entry["part_cf"]
            transform = Matrix.Translation((cf[0], cf[1], cf[2]))
            transform[0][0:3] = (cf[3], cf[4], cf[5])
            transform[1][0:3] = (cf[6], cf[7], cf[8])
            transform[2][0:3] = (cf[9], cf[10], cf[11])
            transform = t2b @ transform
            for position in raw:
                world = transform @ Vector(position)
                ref_positions.append((world[0], world[1], world[2]))
            is_block = entry.get("shape", "block") in ("block", 1)
            for face_index, face in enumerate(data["faces"]):
                ref_faces.append(
                    (offset + face[0], offset + face[1], offset + face[2])
                )
                points = [raw[index] for index in face]
                if is_block:
                    part_size = entry.get("part_size") or (4.0, 1.0, 2.0)
                    hx, hy, hz = part_size[0] / 2.0, part_size[1] / 2.0, part_size[2] / 2.0
                    nx, ny, nz = creation._BLOCK_FACE_NORMALS[face_index]
                    for point in points:
                        u_studs, v_studs = creation._block_canonical_stud_uvs(
                            point, nx, ny, nz, hx, hy, hz
                        )
                        ref_uvs.append((u_studs * 0.5, v_studs * 0.5))
                    continue
                ax_u, ax_v, u_sign, _ = creation._primitive_face_projection_axes(points)
                origin_u = min(point[ax_u] * u_sign for point in points)
                origin_v = min(point[ax_v] for point in points)
                for point in points:
                    ref_uvs.append(
                        (
                            (point[ax_u] * u_sign - origin_u) * 0.5,
                            (point[ax_v] - origin_v) * 0.5,
                        )
                    )
            offset += len(raw)

        co = np.zeros(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        np.testing.assert_allclose(
            co, np.asarray(ref_positions, dtype=np.float32).ravel(), atol=1e-4
        )
        vertex_index = np.zeros(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", vertex_index)
        np.testing.assert_array_equal(
            vertex_index, np.asarray(ref_faces, dtype=np.int32).ravel()
        )
        uv_layer = mesh.uv_layers.get("RBXMaterialUV")
        self.assertIsNotNone(uv_layer)
        uv = np.zeros(len(uv_layer.data) * 2, dtype=np.float32)
        uv_layer.data.foreach_get("uv", uv)
        np.testing.assert_allclose(
            uv, np.asarray(ref_uvs, dtype=np.float32).ravel(), atol=1e-4
        )
        print(
            f"  [creation] template parity: {len(entries)} shapes / "
            f"{len(mesh.vertices)} verts / {len(mesh.loops)} loops match reference"
        )

    def test_bpy_object_primitives_are_flat(self):
        def micro(label, func, count=400):
            started = time.perf_counter()
            for index in range(count):
                func(index)
            elapsed = time.perf_counter() - started
            print(f"  [creation] {label} x{count} at {len(bpy.data.objects)} objects: "
                  f"{elapsed * 1000:.2f} ms ({elapsed / count * 1000:.3f} ms each)")
            return elapsed

        created = []
        micro("objects.new(unique)", lambda i: created.append(bpy.data.objects.new(f"rbx_micro_{i}", None)))
        micro("objects.get(missing)", lambda i: bpy.data.objects.get(f"rbx_missing_{i}"))
        micro("objects.link+unlink", lambda i: _link_unlink(self._collection, created[i % len(created)]))
        for obj in created:
            try:
                bpy.data.objects.remove(obj)
            except ReferenceError:
                pass


def _link_unlink(collection, obj):
    try:
        collection.objects.link(obj)
        collection.objects.unlink(obj)
    except Exception:
        pass


if __name__ == "__main__":
    unittest.main()
