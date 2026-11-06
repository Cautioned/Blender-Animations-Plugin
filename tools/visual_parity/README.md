# Studio / EEVEE visual comparisons

These tools generate controlled Studio scenes, import the same properties through the real binary parser and primitive/material builders, and compare actual images. They do not certify renderer parity.

The suite contains 12 built-in materials plus grayscale and saturated-color patches across 16 conditions: daylight, dawn, dusk, night, diffuse/specular environment controls, ambient-only lighting, shadows, an interior, local lights, zero brightness, attachment spotlights, exposure, and color correction. All Studio reference scenes use Realistic lighting, `PrioritizeLightingQuality = true`, and the recorded editor quality level. Soft lighting, custom skies, arbitrary SurfaceAppearance assets, terrain, water, and atmospheric scattering need additional reference scenes.

## Generate and capture

From the repository root:

```sh
python3 tools/visual_parity/scenes.py
python3 tools/visual_parity/receiver.py
```

Open `build/visual_parity/01_daylight.rbxlx` in a separate Studio window. Execute each generated `build/visual_parity/<case>.luau` in that window. The controller requires the generated fixture marker, keeps previous fixture geometry in ServerStorage, and disables migration-added post effects before applying the case's explicit inputs. Studio serializes the restricted material-generation property as `Use2022MaterialsXml`. Existing captures used legacy materials, as recorded in `reference_materials.json`; newly generated fixtures declare the native alias correctly. See `tools/post_material_parity/README.md` for the separate 2022-material verification.

After applying a case, execute `capture.luau` with `__CASE_ID__` replaced by its case ID. It uses CaptureService, loads the temporary image into EditableImage, base64-encodes its RGBA pixels, and sends bounded stripes to the localhost receiver. The receiver decodes them into a PNG and records the camera, vertical FOV, actual screenshot resolution, graphics settings, and pixel hash. Nothing is uploaded to an external service. Stop the receiver with Ctrl-C when finished.

Capture Studio and render Blender in separate phases. Simultaneous GPU workloads produced stale or blank Studio frames during development. Inspect references, reject blank images, and check repeated hashes before using them for calibration.

## Render, compare, test

Use your Blender executable in place of `blender`:

```sh
blender --background --factory-startup --online-mode --python-exit-code 1 \
  --python tools/visual_parity/render.py -- --scale 0.5 --samples 32
blender --background --factory-startup --python-exit-code 1 \
  --python tools/visual_parity/run_tests.py
```

`render.py` adopts each capture's exact camera and aspect ratio. A half-size render is compared with a 2×2 area average of the retinal Studio capture. Both use the same vertical FOV. Public material texture retrieval needs Blender online mode; image download failures are recorded in each render's JSON.

Run `compare.py` with Python plus NumPy (Blender's bundled Python includes NumPy):

```sh
python3 tools/visual_parity/compare.py
```

Add `--cases 01_daylight --save-blend` to save a packed, editable Blender material/lighting scene.

Open `build/visual_parity/report.html` for Studio / EEVEE / amplified-difference images. `comparison.json` contains whole-image RGB MAE/RMSE and ten fixed color/surface probes, in byte values from 0–255. The report checks camera metadata and rejects blank references. Probe scores cover selected visible regions; inspect complete images for reflections, transparency, texture density, shadow shape, and occlusion.

The headless bootstrap loads individual add-on modules without registering UI/operators. It neither registers the add-on nor changes authentication behavior. The fixture runner exercises the real static primitive import path, parser, materials, lighting and post-processing helpers. Full add-on registration is also checked by the regression runner.

## Calibration scope

The production fit is deliberately scoped to EEVEE, Realistic lighting, and no imported sky image. It separates visible sky from illumination, preserves ambient fill at night, honors the diffuse-scale input, and uses measured direct-sun and display mappings. Installed Roblox default sky textures are used when available; a procedural sky remains the fallback. Custom sky and legacy-style conversion paths retain their previous mapping.

`sun_direction()` is checked against six captured GetSunDirection samples. Shader regressions cover sRGB/linear color handling, baked/object tint consistency, SurfaceAppearance tint and alpha modes, emission hydration, missing-image fallback, attachment light transforms, and zero-valued lights.

`sweep.py`, `validate.py`, and `validate_ambient.py` retain the earlier calibration experiments. Their legacy wrapper selects the pre-calibration lighting branch without modifying product files. Image comparisons remain necessary: environment specular separation, spatial indoor/outdoor fill, local-light falloff, material texture conventions, reflections and transmission remain approximate. Neon, explicit scene bloom and display-space color correction now have managed compositor stages; see the supplemental post/material suite for coverage and limits.

Source references: [Lighting](https://create.roblox.com/docs/reference/engine/classes/Lighting), [SurfaceAppearance](https://create.roblox.com/docs/reference/engine/classes/SurfaceAppearance), [PBR textures](https://create.roblox.com/docs/art/modeling/surface-appearance), [CaptureService](https://create.roblox.com/docs/reference/engine/classes/CaptureService), and [EEVEE node support](https://docs.blender.org/manual/en/4.5/render/eevee/limitations/nodes_support.html).
