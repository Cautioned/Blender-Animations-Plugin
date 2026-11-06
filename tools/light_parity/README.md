# Studio / EEVEE local-light validation

Run inside the repository root. The source test place must contain
`Workspace.RBXParityFixtureMarker`; the controller refuses other places. Prior
fixtures move to ServerStorage history. No cloud uploads are needed.

`scenes.py` generates isolated PointLight, SpotLight and SurfaceLight sweeps,
including brightness, range 0–120, angle 0–180, colors, emitter height, thick
hosts, attachments, shadow controls, daylight and colored overlap. Each source
model is serialized by Studio's native SerializationService, not reconstructed
from the expected values. CaptureService images are decoded from base64 RGBA
stripes by `tools/visual_parity/receiver.py`. Property snapshots and capture
camera/FOV/quality metadata accompany the images.

1. Generate fixtures: `python3 tools/light_parity/scenes.py`.
2. Run the receiver with `--output build/light_parity/studio` (localhost 18765)
   and `tools/light_parity/server.py` (localhost 18766).
3. Execute each generated controller through Dominus, then execute
   `tools/visual_parity/capture.luau` with its case ID substituted. Do not run
   GPU renders concurrently with Studio captures.
4. Render `tools/light_parity/render.py` with Blender background, factory
   startup, two threads, `--python-exit-code 1`; script arguments include
   `--root build/light_parity --scale .5 --samples 32`. `--baseline --output
   before` loads the frozen pre-change modules. Selected scenes can be saved
   with `--save-cases`. Run before and after GPU jobs sequentially.
5. Run `compare.py` using a Python with numpy. `image_io.py` uses macOS sips to
   decode PNGs without the slow Python Paeth loop. The report checks matching
   camera/FOV and compares the union of lit pixels so black backgrounds do
   not conceal differences. The difference view is absolute RGB error ×3.
6. Run `tools/visual_parity/run_tests.py` in Blender for importer regressions.

## Conversion

Studio captures show a nearly flat interior irradiance envelope, followed by
an approximately quadratic fade over the last quarter of Range. A single
EEVEE lamp adds inverse-square attenuation. A bounded set of signed coincident
native lamps approximates the Roblox envelope while retaining actual material
BRDFs, normal maps, occlusion, colored-light addition and native shadows.
The calibrated coefficients and power scale live in `rig/local_lights.py`.
This is an approximation; its near-field fit is reliable from approximately
0.02 Range. Very close surfaces, grazing reflections and different renderer
shadow filters remain imperfect.

Point lights use ten native lamps. Spotlights use two soft native cones with
ten distance components each; their full source angle is retained through
180 degrees. The source return count excludes these helper objects. Helpers
are parented and tagged `RBXLightHelper`; keep each group together. Imported
light states are static; editing an individual helper changes the fitted
response. Do not interpret signed helper powers as Roblox Brightness units.

Source captures distinguish part-centered SpotLights from face-emitting
SurfaceLights. Face is honored inside attachments, including rotated and
translated frames. Attachment-mounted SurfaceLights use spot behavior.
Native defaults read back from this Studio version are Point Range 8,
Spot/Surface Range 16, Spot/Surface Angle 90, Face Front, Brightness 1 and
Shadows false. Explicit zero properties remain zero.

SurfaceLights approximate finite rectangular faces with a bounded sample grid.
For nonzero spread, virtual spot apexes behind the emitting plane retain the
cell footprint at short distances. Native cutoff and near clip are adjusted
for that offset; source Range and the actual face origin are stored separately.
The light's own host is excluded through a per-source shadow-blocker collection;
it remains a shadow caster for other lights. Light-host parts are kept out of
static geometry batches so that exclusion cannot accidentally remove an entire
batch from the shadow map. Local lights are imported from model RBXMs as well
as place RBXLs, without replacing a model import's existing world settings.
EEVEE cannot exactly reproduce Roblox's rectangular projector or zero-angle
beam; shadow softness and very narrow or unusually large emitters retain
visible differences. A surface can require up to 180 native lamps, which
increases light and shadow cost in large imports. This pass validates Blender
5.2.1 EEVEE against Studio 0.746 Realistic at editor quality 21; it does not
certify other renderer versions, Soft lighting, animation, or Cycles.

Primary references:
- [Roblox light sources](https://create.roblox.com/docs/effects/light-sources)
- [SurfaceLight attachment behavior](https://create.roblox.com/docs/reference/engine/classes/SurfaceLight)
- [EEVEE light limitations](https://docs.blender.org/manual/en/latest/render/eevee/limitations/limitations.html)
- [EEVEE native attenuation implementation](https://github.com/blender/blender/blob/v4.3.0/source/blender/draw/engines/eevee_next/shaders/eevee_light_lib.glsl)

The render harness calls the production static geometry, material, global-light
and local-light builders. It bypasses complete add-on UI registration, which is
currently obstructed by pre-existing empty weapon/avatar modules in this checkout.
Full interactive operator/rig registration is outside this validation claim.
