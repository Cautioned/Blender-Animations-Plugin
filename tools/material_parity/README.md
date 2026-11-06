# Material round-trip suite

Ten controlled scenes contain 240 material samples. The three diagnostic image assets were uploaded through Dominus to the explicitly supplied **test creator 7255061823**. The upload receipts and usable texture URIs are recorded in `build/material_parity/assets/uploads.json`. These tests do not upload to the main Studio account.

Coverage includes four SurfaceAppearance alpha modes, six tint colors including black and gray, texture alpha from 0 to 1, six BasePart transparency levels, decal transparency, reversed creation order versus ZIndex, positive/negative UV offsets, rotation, unequal U/V tiling, all six part faces under rotation, classic MeshPart texture precedence, missing albedo, multiple SurfaceAppearance children, filtering, colored scalar maps, shared roughness/metalness maps, normals, emission strength/tint, and six built-in materials.

## Actual round trip

`scenes.py` generates declarative inputs and per-case Studio controllers. Each controller is guarded by `RBXParityFixtureMarker`, and belongs in a separate generated parity place. Previous fixtures are retained in ServerStorage. It applies known inputs, reads properties back, and uses **Studio SerializationService.SerializeInstancesAsync** to export the actual live model. No synthetic model serializer is used for the measured round trip. Lune is also suitable for fixture authoring, but the native export tests the current Studio serialization directly.

The localhost data server receives the native RBXM bytes and property snapshot in `build/material_parity/roundtrip/`. CaptureService supplies reference images through the existing bounded base64-stripe receiver; cameras, vertical FOV, resolution and hashes are recorded. A fresh, rendering Studio viewport is required: an inactive window returned a blank capture during development. ContentProvider's URI preload callback also reported Failure for resources that visibly rendered; its raw status is retained, and images are inspected rather than treating that callback as proof of readiness.

The Blender runner uses the real binary parser, static primitive importer, FileMesh importer, and production material/lighting functions. It preserves asset URIs from the Studio export. It seeds the byte cache with the **exact local diagnostic upload sources** to separate shader correctness from private-asset delivery permissions. This does not validate arbitrary asset authentication or Studio's texture recompression. Public built-in maps use the normal retrieval path.

## Reproduce

Run from the repository root. Python with NumPy is required for the data server and comparison report; Blender's bundled Python works.

```sh
python3 tools/material_parity/scenes.py
python3 tools/material_parity/server.py
python3 tools/visual_parity/receiver.py --output build/material_parity/studio
```

Run generated `<case>.luau` files through the connected Studio plugin, followed by `tools/visual_parity/capture.luau` with `__CASE_ID__` replaced. Capture all Studio references before starting EEVEE renders.

```sh
blender --background --factory-startup --threads 2 --online-mode \
  --python-exit-code 1 --python tools/material_parity/render.py -- \
  --scale .5 --samples 32 --save-cases 05_decal_uv 06_sa_pbr 10_surface_controls
python3 tools/material_parity/compare.py
blender --background --factory-startup --threads 2 --python-exit-code 1 \
  --python tools/visual_parity/run_tests.py
```

The report shows Studio, the pre-fix importer, current EEVEE, amplified differences, and per-card raw RGB error. The camera must match; missing texture nodes, blank references and resolution mismatches fail comparison. Frozen modules in `build/material_parity/baseline_source/` support `render.py --baseline --output before` without reverting production files. Native metadata fixtures are also checked into `roblox_animations/tests/fixtures/material_roundtrip/` for offline regressions.

## Limits

This is fixture evidence, not a claim of general 1:1 rendering. EEVEE and Studio still differ in BRDFs, environment reflections, transmission, dither patterns, bloom and post-processing. MaterialVariant alpha modes, terrain, layered clothing, arbitrary mesh decal projection and newer PBR decal maps need further visual coverage. Primitive decals now composite onto the host surface; MeshPart decals retain the existing projected-plane approximation. The harness bypasses UI registration because this checkout contains existing empty `weapon_ops.py` and `avatar_scale.py` modules; those files are preserved.

Sources: [SurfaceAppearance](https://create.roblox.com/docs/reference/engine/classes/SurfaceAppearance), [AlphaMode](https://create.roblox.com/docs/reference/engine/enums/AlphaMode), [Decal](https://create.roblox.com/docs/reference/engine/classes/Decal), [Texture](https://create.roblox.com/docs/reference/engine/classes/Texture), [SerializationService](https://create.roblox.com/docs/reference/engine/classes/SerializationService), [PBR maps](https://create.roblox.com/docs/art/modeling/surface-appearance).
