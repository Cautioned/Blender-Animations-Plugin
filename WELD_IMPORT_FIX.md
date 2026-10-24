# Welded clothing import fix — PR notes

Suggested PR title: Fix welded clothing constraints using Roblox instance references

## Problem and intended behavior

A character can have many separate clothes parts welded to the same animated
body part. Several of those parts may all be named `Part`. Every clothes mesh
must follow its connected animation bone, retaining its imported placement.
For example, the supplied BlacksmithPinguin model's referent 42 should follow
UpperDoor; multiple Beak accessories should all follow Beak.

The parser missed WeldConstraint endpoints stored as Part0Internal and
Part1Internal. Subsequent name-keyed maps collapsed duplicate clothes names,
and a one-mesh-per-bone rule also restricted explicit reference mappings.
Match-context refresh discarded the fallback reference map. WeldConstraint
structural bones without C0/C1 were created without their tree parent.

## Why retain structural bones and legacy matching rules?

The importer represents rigid joints as structural bones, preserving joint
type, hierarchy and reconstruction data. Existing `_configure_weld_bones`
locks those bones and normally hides them. Keeping this representation avoids
changing reconstruction and other rig workflows. Their existence does not
require welded clothing meshes to use them as independent motion targets.

Commit 1638f7e4 (v2.4.0) introduced `used_bones` alongside nearest-position
matching to disambiguate duplicate left/right bone names. That rule remains
useful for uncertain legacy name matching. Applying it to explicit mappings
was inappropriate: many welded clothes meshes can share one known target.

## Implementation

- Resolve public joint endpoints first, then internal endpoints, retaining
  attachment-based joints.
- Orient rigid connections outward from the animation skeleton, including
  reversed accessory chains. Visit each part once; report conflicting owners.
- Add `meshToBoneRefs`: JSON string keys are source part referents; values are
  animation-owner referents. Preserve it in model, scene and weapon imports.
- Resolve targets through actual created Blender bone names indexed by
  referent, including Blender-generated suffixes. Match meshes by RBXInstRef
  within the imported rig/weapon collection, never by partAux array position.
- Allow explicit many-mesh-to-one-bone assignments. Reserve authoritative
  meshes across context refresh, fallback and retry passes. Missing targets
  produce diagnostics instead of guessing another same-named structural bone.
- Route unweighted MeshParts out of skin-binding preparation and into the
  same authoritative rigid constraint pass as Parts. Previously that pass
  selected a rigid target from the legacy name map and excluded the MeshPart
  from reference constraints. Preserve authored skin weights when present.
- Preserve world placement when creating/replacing CHILD_OF constraints,
  including on posed armatures; remove duplicate assignments per mesh.
- Retain legacy name metadata for older workflows and wrap-layer fitting;
  derive reference mappings from older rig trees when referents are available.
- Keep structural weld bones parented even when no C0/C1 is serialized.

## Validation

197 tests passed with Blender 5.1.2 in background mode. Python syntax checks
and `git diff --check` also passed. Coverage includes parser
endpoints, attachment joints, reversed chains, cycles, duplicate names,
multiple clothes meshes per bone, actual suffixed bone names, inverse/world
transforms, repeated constraint creation, internal weapon rigs, legacy
metadata, and R6/R15 reconstruction across all rig modes.

The private BlacksmithPinguin model has 177 parts and 165 WeldConstraints.
Its 177 reference mappings include 42 -> 2 (UpperDoor). The optional model
integration test builds all 133 native Part meshes and checks original world
placement and pose-following in RAW, LOCAL_AXIS_EXTEND, LOCAL_YAXIS_EXTEND and
CONNECT. A second check substitutes unweighted proxy geometry for the 44
MeshParts and validates all 177 object identities, initial transforms and
pose-following in those four modes. Duplicate MeshPart regressions cover both
new and older reference metadata, and authored skin-weight preservation has
separate coverage. The actual remote MeshPart assets and materials remain
excluded; remote asset rendering and interactive visual inspection are not
validated here.

Set `RBX_WELD_TEST_MODEL` to the private RBXM path to enable
`roblox_animations.tests.test_welded_model`. The model is not included in Git.
Parser graph diagnostics use the `roblox_animations.core.rbxm` logger at DEBUG;
rig creation reports referent/object/armature/bone associations and failures.
