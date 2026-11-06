# Roblox Neon / EEVEE

This suite measures 456 source/control samples across 19 Studio scenarios. Native CaptureService images are paired with actual SerializationService RBXM exports, imported through the production parser, geometry and material functions. Cases cover dark/daylight scenes, saturated colors, grayscale levels, source exposure ±1, transparency, thin/wide strips, an occluder, and default/disabled/zero-intensity/size-zero/wide/multiple BloomEffects.

## Implementation

Neon emits an unlit radiance AOV. A managed compositor stage applies a measured display response and a bounded-color, peak-preserving halo. The response is fitted to isolated native grayscale/exposure captures; it approximates the engine output and does not claim to implement its proprietary shader. Bloom settings retain explicit zeros and all effects; a disabled effect restores the implicit default, while an enabled zero-intensity effect suppresses glow.

The compositor changes only the Neon contribution and halo. It inserts before existing render-layer image consumers, leaving user grading/output nodes and other render passes connected. Repeated imports reuse one managed stage. Later material hydration does not reset imported BloomEffect settings. A custom compositor without a scene render input is left alone. The effect is enabled for rendered/material EEVEE viewports with the viewport compositor set to Always. Radius follows the image domain in Blender 5.x, so viewport/render resolutions use the same relative spread.

Verified environment: Blender 5.2.1 / EEVEE and Studio 0.746.0.7461264, Realistic lighting, editor quality 21. The native no-effect and disabled-Bloom captures have identical pixel hashes. No new uploaded assets are needed.

## Reproduce

From the repository root:

```sh
python3 tools/neon_parity/scenes.py
python3 tools/neon_parity/server.py
python3 tools/visual_parity/receiver.py --output build/neon_parity/studio
```

Run generated `<case>.luau` controllers only in the marker-guarded parity Studio place. Run `tools/visual_parity/capture.luau` with the case ID substituted. The controller retains previous geometry in ServerStorage and exports the actual active BloomEffects alongside the model. Capture references before rendering Blender to avoid stale Studio frames.

```sh
blender --background --factory-startup --threads 2 --online-mode \
  --python-exit-code 1 --python tools/neon_parity/render.py -- \
  --scale .5 --samples 32 --save-cases 01_dark_default 03_bloom_default 09_neon_transparency
python3 tools/neon_parity/compare.py
blender --background --factory-startup --threads 2 --python-exit-code 1 \
  --python tools/visual_parity/run_tests.py
```

Use Python with NumPy for the server and report; Blender's bundled Python includes it. Stop both loopback servers when captures finish. `core_fit.json` records the response fit. `render.py --baseline --output before` uses the retained pre-Neon modules without reverting product files. `report.html` displays source/before/current/difference images; `comparison.json` measures source cores and halo profiles separately in 0–255 RGB units. Those are fixture metrics, not a general parity score.

## Limits

Halo kernels, very thin emitters, transparency dithering and exposure extremes still differ from Studio. The native engine's background/other-material rendering is approximated by the existing lighting conversion. The effect targets ordinary Neon material, not a general recreation of every emissive SurfaceAppearance, textured Neon variant or camera/post-processing stack. Cycles and older viewport compositors without EEVEE AOV support are not verified. Transparent-film halo export and ray-traced emissive indirect lighting need separate coverage.

The tests use the modular headless harness because this checkout has existing empty `weapon_ops.py` and `avatar_scale.py` modules that block normal addon registration/individual avatar-path validation. Those files are preserved.

Sources: [Roblox BloomEffect](https://create.roblox.com/docs/reference/engine/classes/BloomEffect), [post-processing](https://create.roblox.com/docs/environment/post-processing-effects), [Blender 5.0 compositor migration](https://developer.blender.org/docs/release_notes/5.0/migration/compositor_migration/), [EEVEE compositor passes](https://docs.blender.org/manual/en/4.5/compositing/types/input/scene/render_layers.html).
