# Sheltered-surface lighting comparisons

The shipped change enables ordinary EEVEE screen-space ambient occlusion when imported Roblox lighting is built. There is no add-on toggle, quality preset, probe object, bake, or material-graph change. Sampling counts, resolution, trace quality and thickness retain their existing values. Roblox `GlobalShadows=false` disables the extra occlusion. Older EEVEE uses its ordinary GTAO switch; Cycles stays unchanged.

The investigation compared visibility-only probes, irradiance probes, denser irradiance probes, screen-space AO, screen-space GI, and an experimental higher-quality AO preset. The probe approaches did not improve these native Studio references sufficiently and introduced outdoor/interpolation differences. The higher-quality preset and its UI were removed at the user's request. Only the standard AO method is in the packaged add-on.

`experiment.py -- product` renders all 16 original native-reference cases through the production importer, using normal renderer sampling settings. Other explicit modes reproduce the retained research experiments on daylight and the roofed scene. They are diagnostic tooling, not product options. Captures use the original Studio camera/FOV and half-resolution renders are compared with 2x2 area averages of the native screenshots. No score weights or captured references were changed.

Run with Blender 5.2.1, for example:

```
blender -b -t 2 --factory-startup --online-mode -Y --python-exit-code 1 --python tools/shelter_parity/experiment.py -- product
```

`city_benchmark.py` compares AO off/on/off on the existing citytemplate bounds proxy at 640x360, eight render samples. It uses 15,164 proxy parts, 121,312 vertices and 1,341 lights including the sun. It is a bounded performance check, not full textured-map viewport FPS. First frames are excluded from each warm average; two runs per mode reduce order bias. No extra lights, probes, geometry or shaders are created by the AO helper.

The main report retains the prior images in `build/visual_parity/ssao_before/`. `build/shelter_parity/` holds experiment images, comparison results and benchmark data. Native reference captures remain in `build/visual_parity/studio/`.

Limits: screen-space occlusion depends on visible geometry and can miss blockers outside the image. It improves the measured sheltered scene but does not reproduce Roblox's complete spatial Ambient/OutdoorAmbient system or remove all renderer differences. The rendered verification is Blender 5.2.1 EEVEE; legacy GTAO compatibility also has a unit regression.

References: [Blender EEVEE world lighting](https://docs.blender.org/manual/en/latest/render/eevee/world_settings.html), [Blender probe volumes](https://docs.blender.org/manual/en/5.0/render/eevee/light_probes/volume.html), [Roblox Lighting](https://create.roblox.com/docs/reference/engine/classes/Lighting).
