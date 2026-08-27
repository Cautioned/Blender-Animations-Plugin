# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportInvalidTypeForm=false
"""
Import operators for rig and animation data.
"""

import json
import base64
import os
import re
import math
import time
import bpy
from collections import deque
from mathutils import Matrix, Vector
from bpy_extras.io_utils import ImportHelper
from ..core.utils import get_unique_name, get_object_by_name, iter_scene_objects
from ..core.utils import cf_to_mat, mat_to_cf, find_constraint_driven_armature
from ..core.schema import WeaponImportPayload
from typing import Any, Dict
from ..core.constants import get_transform_to_blender
from ..rig.creation import create_rig, get_unique_collection_name
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor


_LAZY_PLACE_IMPORTS = []
_LAZY_IMPORT_OBJECT_BUDGET = 512
# Each tick can run three of these slices (static, geometry, materials), so a
# tick may keep the UI busy for up to 3x this value.  The viewport stays
# hidden during the fill, and the per-tick loop overhead (timer dispatch,
# events, redraws) is fixed per tick, so a longer slice both shortens the
# wall clock and costs little interactivity.
_LAZY_IMPORT_SLICE_SECONDS = 0.250
# A timer interval is measured *after* its callback returns.  With bounded
# slices, a long delay would leave Blender idle for most of a place import.
# A one-millisecond handoff still yields to the UI between bounded slices.
# Imported collections are hidden from the viewport while they fill, so the
# per-tick cost is object/mesh work, not a redraw of a scene that grows by
# tens of thousands of objects.  The old 64-object/30 ms budgets spent most
# of the wall clock in viewport redraws between ticks, not in the import
# phases themselves.
_LAZY_IMPORT_TIMER_INTERVAL = 0.001
# When every remaining task is worker-bound, repeatedly polling futures at
# 1 kHz produces measurable idle CPU use without advancing the import.  A
# 25 ms poll still feels immediate once bytes arrive, while letting Blender
# and the OS sleep between network/decoder completions.
_LAZY_IMPORT_WORKER_POLL_INTERVAL = 0.025
_LAZY_STATIC_BATCH_BUDGET = 512
_LAZY_MESHPART_BATCH_THRESHOLD = 4
# Batching an asset copies its complete vertex, loop, and UV buffers once for
# every instance.  Larger geometry must stay as linked Blender mesh data,
# otherwise a map full of repeated meshes silently turns instancing into an
# O(instances * vertices) import.  The total cap bounds that blowup: even a
# mid-size prop merged dozens of times stays under one large mesh's memory.
_LAZY_MESHPART_BATCH_MAX_LOOPS = 4096
_LAZY_MESHPART_BATCH_MAX_TOTAL_LOOPS = 262144
# Cold place imports are network-bound: Citytemplate has 533 unique assets.
# Too few in-flight deliveries turn even modest per-request latency into a
# multi-minute import. The ready queue below independently bounds decoded RAM.
_LAZY_FILEMESH_FETCH_CONCURRENCY = 16
# Mesh and texture delivery share the same CDN/bandwidth. Twenty texture
# workers gave the best observed overlap: higher limits starve FileMesh
# delivery, while staged limits create a long texture-only tail.
_LAZY_TEXTURE_FETCH_CONCURRENCY = 20
# Parsed FileMesh dictionaries are large. Do not let network workers decode
# an unbounded number while Blender is still constructing earlier geometry.
_LAZY_READY_MESH_BACKLOG_CAP = 48
# Material hydration is constrained by the timer slice: the deadline check
# bounds each tick even when a single refresh includes an image decode. A
# larger budget matters for already-decoded images, which hydrate in about
# a millisecond each.
_LAZY_MATERIAL_REFRESH_BUDGET = 64
# While geometry is still landing, hydration still refreshes a healthy chunk
# per slice: it runs FIRST in the tick, and image decodes then hide behind
# the network prefetch instead of stacking into a laggy end-phase.
_LAZY_HYDRATION_OVERLAP_BUDGET = 32
_SKY_ASSET_CONSUMER = ("scene", "sky")
_BEAM_ASSET_CONSUMER = ("scene", "beams")
_TERRAIN_ASSET_CONSUMER = ("scene", "terrain")
_UNION_ASSET_MAX_BYTES = 64 * 1024 * 1024


def _fetch_legacy_union_mesh(asset_id, auth_headers, rbxm_mod, filemesh_mod):
    """Download a 2012-era union render-mesh asset and decode its CSGMDL.

    The asset is a gzip'd legacy rbxm document whose PartOperationAsset
    carries a MeshData property: a lz4-wrapped, XOR-obfuscated CSGMDL v2
    blob (the legacy chunk parser + tolerant lz4 decoder in core/rbxm.py
    handle the framing).  Returns a filemesh-style mesh dict or None.
    """
    try:
        from ..rig.filemesh import _fetch_url_bytes

        # The modern minting endpoint returns a signed contentdelivery
        # location for OLD content types (SolidModel union meshes) that the
        # legacy assetdelivery.roblox.com/v1 route 401s.  _fetch_url_bytes
        # follows the JSON "location" hop anonymously once the bearer has
        # been used.
        raw = _fetch_url_bytes(
            f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{int(asset_id)}",
            timeout=20.0,
            extra_headers=dict(auth_headers or {}),
            max_bytes=_UNION_ASSET_MAX_BYTES,
            trim_mesh_header=False,
        )
        import gzip

        blob = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        instances, _roots = rbxm_mod._parse_chunks(blob)
        for inst in instances.values():
            if inst.class_name not in (
                "PartOperationAsset", "PartOperation",
                "UnionOperation", "NegateOperation",
            ):
                continue
            mesh = rbxm_mod._extract_union_mesh(inst)
            if isinstance(mesh, dict) and mesh.get("faces"):
                return mesh
    except Exception as exc:  # noqa: BLE001 - unions degrade gracefully
        print(f"[RbxmImport] legacy union asset {asset_id} failed: {exc}")
    return None


def _resolve_legacy_union_assets(part_aux, auth_headers):
    """Replace union_unsupported placeholders with decoded legacy union meshes.

    Fetches each distinct union render-mesh asset once and stamps the decoded
    CSGMDL onto every entry that referenced it, mirroring how Studio resolves
    PartOperation AssetIds through the CSG dictionary.
    """
    try:
        from ..core import rbxm as rbxm_mod
        from ..rig import filemesh as filemesh_mod
    except ImportError:
        return 0
    ids = sorted({
        int(entry["union_asset_id"])
        for entry in part_aux
        if isinstance(entry, dict)
        and isinstance(entry.get("union_asset_id"), (int, float, str))
        and not isinstance(entry.get("union_mesh"), dict)
    })
    if not ids:
        return 0
    resolved = {}
    for asset_id in ids:
        mesh = _fetch_legacy_union_mesh(asset_id, auth_headers, rbxm_mod, filemesh_mod)
        if isinstance(mesh, dict):
            resolved[asset_id] = mesh
    applied = 0
    for entry in part_aux:
        if not isinstance(entry, dict) or isinstance(entry.get("union_mesh"), dict):
            continue
        try:
            asset_id = int(entry.get("union_asset_id"))
        except (TypeError, ValueError):
            continue
        mesh = resolved.get(asset_id)
        if isinstance(mesh, dict):
            if entry.get("union_use_part_color"):
                # UsePartColor unions render with the part's Color3, never
                # the CSG mesher's baked vertex colors.  The strip has to
                # happen AFTER the asset fetch, which is where legacy
                # unions get their mesh from.
                mesh = dict(mesh)
                mesh["colors"] = []
            entry["union_mesh"] = mesh
            entry.pop("union_unsupported", None)
            # Drop the placeholder block fallback now that the real CSG
            # render mesh is attached.
            entry.pop("shape", None)
            applied += 1
    if applied:
        print(
            f"[RbxmImport] resolved {applied} legacy union(s) from "
            f"{len(ids)} render-mesh asset(s)"
        )
    return applied


def _lazy_fetch_filemesh(mesh_id, auth_headers, lod_index=0):
    """Fetch, parse, and prep one asset entirely off the main thread.

    Parsing uses the bulk numpy decoder (array fields, zero per-vertex python
    objects); LOD compaction and loop-UV flattening are pure dict/array work
    that run here alongside the network fetch instead of consuming Blender's
    bounded geometry-slice budget."""
    from ..rig.filemesh import fetch_and_parse_filemesh
    from ..rig.creation import _slice_mesh_data_to_lod

    full_mesh_data = fetch_and_parse_filemesh(
        mesh_id,
        auth_headers=auth_headers,
        use_arrays=True,
        allow_local_paths=False,
        # Jobs are grouped by asset before submission, so no later lazy job
        # needs the full decoded source. Retaining it until import completion
        # needlessly doubles peak RAM beside the compacted LOD hand-off.
        retain_parsed_cache=False,
    )
    if not isinstance(full_mesh_data, dict):
        return full_mesh_data
    net_seconds = float(full_mesh_data.get("_rbx_net_seconds", 0.0) or 0.0)
    parse_seconds = float(full_mesh_data.get("_rbx_parse_seconds", 0.0) or 0.0)
    mesh_data = _slice_mesh_data_to_lod(full_mesh_data, lod_index)
    if mesh_data is not full_mesh_data:
        mesh_data["lod_offsets"] = []
        mesh_data["_rbx_net_seconds"] = net_seconds
        mesh_data["_rbx_parse_seconds"] = parse_seconds
    prep_started = time.perf_counter()
    # UV topology depends only on this asset's selected LOD, not on a part
    # transform/material. Reuse it across every visual group instead of
    # reconstructing it from Blender loops.
    if "loop_uvs" not in mesh_data:
        uvs = mesh_data.get("uvs") or []
        faces = mesh_data.get("faces") or []
        try:
            import numpy as np  # bundled with Blender

            if (
                isinstance(uvs, np.ndarray)
                and isinstance(faces, np.ndarray)
                and uvs.shape[0] > 0
                and faces.shape[0] > 0
            ):
                # Flat float buffer instead of one python tuple per loop:
                # tuple-per-loop is millions of object allocations across a
                # place and re-traversed on the main thread.
                flat = faces.astype(np.int64, copy=False).ravel()
                valid = flat < len(uvs)
                rows = uvs[np.clip(flat, 0, len(uvs) - 1)]
                loop_uvs = np.zeros(2 * len(flat), dtype=np.float32)
                filled = np.flatnonzero(valid)
                if filled.size:
                    loop_uvs[2 * filled] = rows[filled, 0].astype(np.float32)
                    loop_uvs[2 * filled + 1] = rows[filled, 1].astype(np.float32)
                mesh_data["loop_uvs"] = loop_uvs
                mesh_data["_rbx_fetch_duration"] = time.perf_counter() - prep_started
                return mesh_data
        except ImportError:
            pass
        mesh_data["loop_uvs"] = [
            uvs[index] if 0 <= index < len(uvs) else None
            for face in faces
            for index in face
        ]
    mesh_data["_rbx_fetch_duration"] = time.perf_counter() - prep_started
    return mesh_data


def _lazy_prefetch_texture_bytes(
    texture_refs, auth_headers, raw_decode_refs=(), asset_registry=None
):
    """Warm image bytes off the main thread before material construction."""
    started = time.perf_counter()
    from ..rig.textures import prefetch_texture_bytes

    stats = prefetch_texture_bytes(
        texture_refs,
        max_workers=_LAZY_TEXTURE_FETCH_CONCURRENCY,
        timeout=4.0,
        auth_headers=auth_headers,
        raw_decode_refs=raw_decode_refs,
        asset_registry=asset_registry,
    )
    stats = stats or {}
    stats["elapsed"] = time.perf_counter() - started
    return stats


def _register_deferred_material(state, signature, entry):
    """Register one material and enqueue it exactly once when assets finish."""
    # Every equivalent entry must publish graph readiness when it is built,
    # even though only the first entry owns the canonical hydration job.
    entry["_rbx_lazy_hydration_signature"] = signature
    if signature in state["material_hydration_seen"]:
        return
    from ..rig import textures as textures_mod

    state["material_hydration_seen"].add(signature)
    state["deferred_materials"][signature] = entry
    keys = tuple(
        textures_mod._texture_asset_key(ref)
        for ref in textures_mod.material_texture_refs(entry)
    )
    if state["texture_assets"].subscribe(signature, keys):
        state["material_assets_ready"].add(signature)
    if textures_mod.part_material_is_built(entry):
        state["material_graphs_ready"].add(signature)
    _queue_ready_material(state, signature)


def _queue_ready_material(state, signature):
    """Queue a material once both its graph and all byte assets exist."""
    if (
        signature in state["material_assets_ready"]
        and signature in state["material_graphs_ready"]
        and signature not in state["material_ready_seen"]
    ):
        state["material_ready_seen"].add(signature)
        state["material_jobs"].append(signature)


def _mark_deferred_material_built(state, entry):
    """Publish the graph-ready half of a deferred material dependency."""
    signature = entry.get("_rbx_lazy_hydration_signature")
    if signature is None or signature not in state["deferred_materials"]:
        return
    state["material_graphs_ready"].add(signature)
    _queue_ready_material(state, signature)


def _lazy_prepare_terrain(terrain_meta):
    """Run terrain decode and surface-net generation off Blender's thread."""
    started = time.perf_counter()
    from ..rig import terrain as terrain_mod

    return terrain_mod.prepare_terrain(terrain_meta), time.perf_counter() - started


def _resolve_lazy_collection(collection_ref):
    """Resolve legacy name jobs while keeping new jobs on direct RNA refs."""
    if not collection_ref:
        return None
    if isinstance(collection_ref, str):
        return bpy.data.collections.get(collection_ref)
    try:
        # Touching the name detects an RNA reference invalidated while a lazy
        # import was running without paying a global lookup for valid refs.
        _ = collection_ref.name
        return collection_ref
    except ReferenceError:
        return None


def _lazy_material_signature(entry):
    """Return the material subsystem's canonical content identity."""
    from ..rig.textures import part_material_identity

    return part_material_identity(entry)


def _lazy_material_needs_hydration(entry):
    """Return whether the deferred pass can add an image to this material."""
    from ..rig import textures as _textures_mod

    if _textures_mod.material_texture_refs(entry):
        return True
    # Classic clothing images live in global import context rather than the
    # part entry, so they are the sole non-explicit dependency.
    from ..rig import clothing as _clothing_mod
    return bool(
        _clothing_mod.clothing_available()
        and _clothing_mod.is_clothing_limb(str(entry.get("name") or ""))
    )


def _lazy_mesh_instance_key(entry, collection=None):
    """Return the geometry key for a lazy FileMesh prototype.

    A Blender object can override a shared mesh's material slot. Combined
    with per-object transforms, that means every non-rigged use of an asset
    shares one mesh datablock regardless of its size, color, or texture.
    """
    mesh_id = entry.get("mesh_id")
    return (str(mesh_id),) if mesh_id else ("unique", entry.get("inst_ref"))


def _can_batch_lazy_filemesh(mesh_data, instance_count):
    """Return whether duplicating this mesh is cheaper than linked objects."""
    if instance_count < _LAZY_MESHPART_BATCH_THRESHOLD:
        return False
    loop_count = 0
    for face in mesh_data.get("faces") or ():
        loop_count += len(face)
        if loop_count > _LAZY_MESHPART_BATCH_MAX_LOOPS:
            return False
    if loop_count <= 0:
        return False
    return loop_count * instance_count <= _LAZY_MESHPART_BATCH_MAX_TOTAL_LOOPS


def _lazy_mesh_object_name(entry):
    name = entry.get("name") or "MeshPart"
    model_tag = entry.get("model_tag")
    class_name = entry.get("class_name") or "MeshPart"
    return f"{model_tag}/{name}.{class_name}" if model_tag else name


def _set_object_material_override(mesh_obj, material):
    """Assign ``material`` without mutating a shared mesh datablock."""
    if mesh_obj is None or not mesh_obj.material_slots:
        return
    try:
        slot = mesh_obj.material_slots[0]
        slot.link = "OBJECT"
        slot.material = material
    except (AttributeError, TypeError, ValueError):
        pass


def _apply_lazy_instance_material(
    mesh_obj, entry, material_cache=None, material_state=None
):
    """Resolve a material without mutating the instance's shared mesh data."""
    try:
        from ..rig import textures as _textures_mod

        signature = entry.get("_rbx_lazy_material_signature")
        if _textures_mod.entry_uses_baked_tint(entry):
            # Linked FileMesh instances can have different Roblox colours.
            # Object Info Color keeps the tint per object without cloning a
            # full material graph for every colour.
            material = _textures_mod.object_tinted_builtin_material(entry)
            _textures_mod.set_object_rbx_tint(mesh_obj, entry)
        else:
            material = material_cache.get(signature) if material_cache is not None else None
            if material is None:
                material = _textures_mod.get_part_material(mesh_obj.name, entry)
                if material_cache is not None and signature is not None:
                    material_cache[signature] = material
        _set_object_material_override(mesh_obj, material)
        if material_state is not None:
            _mark_deferred_material_built(material_state, entry)
    except Exception as exc:
        print(f"[RbxmImport] Deferred material failed for '{mesh_obj.name}': {exc}")


def _remove_lazy_map_identity(mesh_obj):
    """Drop rig-only metadata from non-rigged place geometry instances."""
    for key in ("RBXPartIdx", "RBXInstRef"):
        if key in mesh_obj:
            del mesh_obj[key]


def _lazy_instance_transform(entry):
    if not entry.get("part_cf"):
        return Matrix.Identity(4)
    return get_transform_to_blender() @ cf_to_mat(entry["part_cf"])


def _apply_lazy_mesh_job_impl(job, object_budget, deadline):
    """Create a bounded number of objects from one completed mesh download."""
    if job.get("batch"):
        from ..rig.creation import _create_batched_filemesh_instances
        entries = [entry for entry, _collection in job["entries"]]
        collection = _resolve_lazy_collection(job["collection"])
        if collection is None:
            return True, 0
        mesh_obj = _create_batched_filemesh_instances(
            collection,
            _lazy_mesh_object_name(entries[0]),
            job["mesh_data"],
            entries,
            lod_index=job.get("lod_index", 0),
        )
        if mesh_obj is not None and len(mesh_obj.data.materials):
            signature = entries[0].get("_rbx_lazy_material_signature")
            if signature is not None:
                job["material_cache"][signature] = mesh_obj.data.materials[0]
            _mark_deferred_material_built(job["material_state"], entries[0])
        return True, 1
    created = 0
    if "base_obj" not in job:
        mesh_data = job.get("mesh_data")
        if mesh_data is None:
            mesh_data = job["future"].result()
        from ..rig.creation import _create_mesh_object_from_filemesh
        collection = _resolve_lazy_collection(job["collection"])
        if collection is None:
            return True, 0
        base_obj = _create_mesh_object_from_filemesh(
            collection, job["entry"].get("name"), mesh_data, job["entry"],
            lod_index=job.get("lod_index", 0), native_transform=True,
            native_object_transform=True, skip_custom_normals=True,
        )
        job["base_obj"] = base_obj
        job["instance_entries"] = job["entries"][1:]
        job["instance_index"] = 0
        created = 1
        if base_obj is None:
            return True, created
        _remove_lazy_map_identity(base_obj)
        # OBJECT-linked override on the prototype too: several jobs can
        # share ONE mesh datablock (same FileMesh asset), and whoever
        # rewrites the data slot last silently swaps every earlier
        # DATA-linked wearer's material.  Never rely on the data slot.
        _apply_lazy_instance_material(
            base_obj, job["entry"], job["material_cache"], job["material_state"]
        )

    base_obj = job["base_obj"]
    from ..rig.creation import _get_filemesh_native_transform
    instance_entries = job["instance_entries"]
    index = job["instance_index"]
    while (
        index < len(instance_entries)
        and created < object_budget
        and time.perf_counter() < deadline
    ):
        entry, collection_ref = instance_entries[index]
        collection = _resolve_lazy_collection(collection_ref)
        if collection is None:
            index += 1
            continue
        instance = bpy.data.objects.new(_lazy_mesh_object_name(entry), base_obj.data)
        instance.matrix_world = _get_filemesh_native_transform(entry, job["mesh_data"])
        instance["RBXSynthesizedPart"] = True
        # The common case is a cache hit: the signature cache already holds
        # the material and only the OBJECT slot link is rewritten.  Override
        # unconditionally — a data-linked slot on a shared mesh datablock
        # can be rewritten by a later job and take every wearer with it.
        _apply_lazy_instance_material(
            instance, entry, job["material_cache"], job["material_state"]
        )
        collection.objects.link(instance)
        index += 1
        created += 1
    job["instance_index"] = index
    return index >= len(instance_entries), created


def _apply_lazy_mesh_job(job, object_budget, deadline):
    """Build map geometry now; hydrate texture images after it is usable."""
    from ..rig.textures import defer_texture_image_loading
    with defer_texture_image_loading():
        return _apply_lazy_mesh_job_impl(job, object_budget, deadline)


def _release_completed_lazy_state(state):
    """Break import-owned references immediately after the final report."""
    registry = state.get("texture_assets")
    if registry is not None:
        registry.clear()
    for key in (
        "jobs", "queued_meshes", "ready_meshes", "static_batches",
        "deferred_materials", "lazy_materials", "material_jobs",
        "material_hydration_seen", "material_hydrated_seen",
        "material_assets_ready", "material_graphs_ready",
        "material_ready_seen", "beam_image_jobs", "hidden_collections",
    ):
        value = state.get(key)
        if hasattr(value, "clear"):
            value.clear()
    # These futures and callbacks retain prepared terrain arrays, scene
    # metadata, collections, and the state itself through closure cycles.
    for key in (
        "pool", "texture_assets", "texture_future", "terrain_future",
        "prepared_terrain", "terrain_builder", "finalize",
        "post_texture_finalize",
    ):
        state[key] = None


def _process_lazy_place_imports():
    """Apply completed FileMesh downloads on Blender's main thread."""
    from ..rig import textures as _textures_mod

    active = []
    for state in _LAZY_PLACE_IMPORTS:
        deadline = time.perf_counter() + _LAZY_IMPORT_SLICE_SECONDS
        objects_applied = 0
        t_fetch = time.perf_counter()
        # Completed futures stay in ``jobs`` until the ready backlog has
        # room (the second half of backpressure), but they must not count
        # against the in-flight limit: the old gate let 16 completed
        # results block ALL further submissions whenever ``ready_meshes``
        # reached its cap, idling the pool through each drain cycle.  Only
        # genuinely running futures throttle new launches; the backlog cap
        # still bounds decoded RAM on the drain side, so a full ready queue
        # never stops the network workers themselves.
        in_flight = sum(1 for job in state["jobs"] if not job["future"].done())
        while (
            state["queued_meshes"]
            and in_flight < _LAZY_FILEMESH_FETCH_CONCURRENCY
        ):
            asset = state["queued_meshes"].pop()
            state["jobs"].append({
                "groups": asset["groups"],
                "future": state["pool"].submit(
                    _lazy_fetch_filemesh,
                    asset["mesh_id"],
                    state["auth_headers"],
                    state["mesh_lod"],
                ),
            })
            in_flight += 1
        # Sample once per timer callback. This exposes an under-filled
        # delivery window without adding locks or per-request timing work.
        state["mesh_in_flight_peak"] = max(state["mesh_in_flight_peak"], in_flight)
        state["mesh_in_flight_samples"] += in_flight
        state["mesh_in_flight_sample_count"] += 1
        if in_flight >= _LAZY_FILEMESH_FETCH_CONCURRENCY:
            state["mesh_full_window_samples"] += 1
        fetch_pending = []
        for job in state["jobs"]:
            # Keep a completed future intact until there is room to turn its
            # parsed payload into Blender work. This is the second half of
            # backpressure: limiting launches alone still lets the final few
            # in-flight assets overfill ``ready_meshes``.  A single completed
            # asset can fan out into many ready entries (one per collection
            # group), so the job's spill list caps that fan-out: the backlog
            # can never overshoot its bound by a fat asset.
            spill = job.get("_ready_spill")
            if spill:
                room = _LAZY_READY_MESH_BACKLOG_CAP - len(state["ready_meshes"])
                while spill and room > 0:
                    state["ready_meshes"].append(spill.pop())
                    room -= 1
                if spill:
                    fetch_pending.append(job)
                    continue
                if job.get("_future_consumed"):
                    # The future was already turned into entries; the job is
                    # finished once its spill drains.
                    continue
            if len(state["ready_meshes"]) >= _LAZY_READY_MESH_BACKLOG_CAP:
                fetch_pending.append(job)
                continue
            if not job["future"].done():
                fetch_pending.append(job)
                continue
            try:
                # LOD slicing and loop-UV flattening already ran in the fetch
                # worker; the main thread only turns the result into objects.
                lod_index = state["mesh_lod"]
                mesh_data = job["future"].result()
                job["_future_consumed"] = True
                state["meshes_ready"] += 1
                if isinstance(mesh_data, dict):
                    state["phase_times"]["fetch_network"] += float(
                        mesh_data.get("_rbx_net_seconds", 0.0)
                    )
                    state["phase_times"]["fetch_parse"] += float(
                        mesh_data.get("_rbx_parse_seconds", 0.0)
                    )
                    state["phase_times"]["fetch_prep"] += float(
                        mesh_data.get("_rbx_fetch_duration", 0.0)
                    )
                # Entries land in a local list and are enqueued up to the
                # backlog cap; anything past it waits in the job's spill
                # instead of overfilling the queue in one tick.
                new_entries = []
                for entries in job["groups"]:
                    for entry, _collection_ref in entries:
                        # A generated workbench-only alpha composite requires
                        # a Python loop over every texture pixel. It is not
                        # needed for rendered/material preview and costs about
                        # 53 seconds on citytemplate.
                        entry["_rbx_skip_solid_alpha_composite"] = True
                        material_signature = _lazy_material_signature(entry)
                        entry["_rbx_lazy_material_signature"] = material_signature
                        # Batches retain colour in their signature because
                        # one material slot belongs to one shared mesh.
                        # Linked objects read Object Info Color, so their
                        # hydration shares the colour-free node graph.
                        if _lazy_material_needs_hydration(entry):
                            _register_deferred_material(
                                state, material_signature, entry
                            )
                    # Batched geometry has one data-level material slot, so
                    # keep material variants separate there. Non-batched
                    # objects below use per-object material overrides and can
                    # all share the same asset prototype.
                    from ..rig import textures as _textures_mod

                    by_collection = {}
                    for entry, collection_ref in entries:
                        # Built-in materials use a stud-density UV layer that
                        # lives on the mesh datablock; a shared batched mesh
                        # built from many entries cannot express per-part
                        # material UVs, so keep those instances separate.
                        batchable = not _textures_mod.builtin_material_texture_refs(entry)
                        key = (
                            collection_ref.as_pointer(),
                            entry["_rbx_lazy_material_signature"],
                            batchable,
                        )
                        by_collection.setdefault(key, (collection_ref, batchable, []))[2].append(
                            (entry, collection_ref)
                        )
                    remainder = []
                    for collection_ref, batchable, collection_entries in by_collection.values():
                        if batchable and _can_batch_lazy_filemesh(mesh_data, len(collection_entries)):
                            new_entries.append({
                                "entries": collection_entries,
                                "collection": collection_ref,
                                "mesh_data": mesh_data,
                                "lod_index": lod_index,
                                "batch": True,
                                "material_cache": state["lazy_materials"],
                                "material_state": state,
                            })
                        else:
                            remainder.extend(collection_entries)
                    if remainder:
                        entry, collection_ref = remainder[0]
                        new_entries.append({
                            "entry": entry,
                            "entries": remainder,
                            "collection": collection_ref,
                            "mesh_data": mesh_data,
                            "lod_index": lod_index,
                            "material_cache": state["lazy_materials"],
                            "material_state": state,
                        })
                room = _LAZY_READY_MESH_BACKLOG_CAP - len(state["ready_meshes"])
                if room < len(new_entries):
                    job["_ready_spill"] = new_entries[room:]
                    del new_entries[room:]
                state["ready_meshes"].extend(new_entries)
                if job.get("_ready_spill"):
                    fetch_pending.append(job)
            except Exception as exc:
                print(f"[RbxmImport] Deferred mesh failed: {exc}")
        state["jobs"] = fetch_pending
        state["ready_mesh_peak"] = max(
            state["ready_mesh_peak"], len(state["ready_meshes"])
        )
        state["phase_times"]["fetch_drain"] += time.perf_counter() - t_fetch
        if (
            state["fetch_makespan"] is None
            and not state["jobs"]
            and not state["queued_meshes"]
        ):
            # Every FileMesh fetch has completed (results may still wait in
            # ready_meshes).  This is the worker-side wall time of the mesh
            # pipeline, printed with the summary to expose whether the
            # post-prefetch tail is network or hydration.
            state["fetch_makespan"] = time.perf_counter() - state["started"]
        texture_future = state.get("texture_future")
        terrain_future = state.get("terrain_future")
        geometry_pending = bool(
            state["jobs"] or state["ready_meshes"] or state["queued_meshes"]
            or state["static_batches"]
        )
        texture_bytes_ready = texture_future is None or texture_future.done()
        terrain_ready = terrain_future is None or terrain_future.done()
        texture_completions = state["texture_assets"].drain_completions()
        state["texture_completion_count"] += len(texture_completions)
        for signature in state["texture_assets"].drain_ready_consumers():
            if signature == _SKY_ASSET_CONSUMER:
                state["sky_assets_ready"] = True
            elif signature == _BEAM_ASSET_CONSUMER:
                state["beam_assets_ready"] = True
            elif signature == _TERRAIN_ASSET_CONSUMER:
                state["terrain_assets_ready"] = True
            elif signature in state["deferred_materials"]:
                state["material_assets_ready"].add(signature)
                _queue_ready_material(state, signature)
        if (
            texture_bytes_ready
            and texture_future is not None
            and not state["texture_seconds_consumed"]
        ):
            try:
                texture_result = texture_future.result() or {}
                if isinstance(texture_result, dict):
                    state["phase_times"]["textures"] = float(
                        texture_result.get("elapsed", 0.0)
                    )
                    state["texture_stats"] = texture_result
                else:
                    state["phase_times"]["textures"] = float(texture_result)
            except Exception:
                pass
            state["texture_seconds_consumed"] = True
        if terrain_ready and terrain_future is not None and not state["terrain_consumed"]:
            try:
                prepared, seconds = terrain_future.result()
                state["prepared_terrain"] = prepared
                state["phase_times"]["terrain_prepare"] = float(seconds)
            except Exception as exc:
                print(f"[RbxmImport] terrain preparation failed: {exc}")
            state["terrain_consumed"] = True
        if (
            terrain_ready
            and state["terrain_assets_ready"]
            and not state["terrain_build_done"]
            and callable(state.get("terrain_builder"))
        ):
            started = time.perf_counter()
            try:
                state["terrain_built"] = state["terrain_builder"](
                    state.get("prepared_terrain")
                )
            except Exception as exc:
                print(f"[RbxmImport] terrain skipped: {exc}")
            state["terrain_build_seconds"] = time.perf_counter() - started
            state["terrain_build_done"] = True
        if not state["material_hydration_started"] and state["deferred_materials"]:
            # Hydrate incrementally: entries whose bytes have already landed
            # refresh while the prefetch worker is still fetching the rest,
            # so hydration is not a serial tail behind a slow network.  It
            # runs FIRST in the tick so it can claim the slice budget while
            # bytes keep landing, instead of inheriting only the leftover
            # time after static/geometry.
            state["material_hydration_started"] = True
            signatures = tuple(state["deferred_materials"])
            base_count = len({signature[:2] for signature in signatures})
            binding_count = len({signature[5:10] for signature in signatures})
            colour_count = len({signature[2:5] for signature in signatures})
            print(
                f"[RbxmImport] Hydrating material variants as texture bytes "
                f"land ({len(state['deferred_materials'])} registered so far; "
                f"more arrive as meshes load; {base_count} base, "
                f"{binding_count} texture binding, {colour_count} colour variants)."
            )

        # Do not cycle the same variants while the shared texture prefetch or
        # their geometry is still in flight. On large places this formerly
        # recorded thousands of failed "refreshes" (and several seconds of
        # main-thread CPU) before the relevant bytes/material existed. The
        # post-import byte cache makes this pass local, deterministic, and
        # one-shot per material signature.
        if state["material_hydration_started"] and state["material_jobs"]:
            from ..rig import textures as _textures_mod

            t_materials = time.perf_counter()
            refresh_count = 0
            refresh_budget = (
                _LAZY_MATERIAL_REFRESH_BUDGET
                if not geometry_pending
                else _LAZY_HYDRATION_OVERLAP_BUDGET
            )
            while (
                state["material_jobs"]
                and refresh_count < refresh_budget
                and time.perf_counter() < deadline
            ):
                signature = state["material_jobs"].popleft()
                entry = state["deferred_materials"].get(signature)
                if entry is None:
                    state["material_hydrated_seen"].add(signature)
                elif not _textures_mod.part_material_is_built(entry):
                    # The geometry job for this entry has not run yet.  While
                    # geometry is still pending, leave it un-seen so the
                    # re-queue pass retries; once geometry is done a missing
                    # material is final (a skipped or errored build).  This
                    # also stops warm-cache re-imports from spinning tens of
                    # thousands of refresh attempts on unbuilt materials.
                    if not geometry_pending:
                        state["material_hydrated_seen"].add(signature)
                else:
                    refreshed = _textures_mod.refresh_cached_part_material(entry)
                    if refreshed:
                        state["material_hydrated_seen"].add(signature)
                    elif geometry_pending:
                        # Built but not hydrated: the comment above promised a
                        # requeue pass that never existed, so a signature
                        # whose refresh came up empty was dropped forever.
                        # Requeue with a bounded attempt count so a genuinely
                        # dead asset cannot churn the budget indefinitely.
                        attempts = state["material_refresh_attempts"].get(signature, 0) + 1
                        state["material_refresh_attempts"][signature] = attempts
                        if attempts >= 4:
                            print(
                                "[RbxmImport] hydration refresh repeatedly "
                                f"failed for '{entry.get('name')}'"
                            )
                            state["material_hydrated_seen"].add(signature)
                        else:
                            state["material_jobs"].append(signature)
                    else:
                        print(
                            "[RbxmImport] hydration refresh failed after "
                            f"texture prefetch completed for '{entry.get('name')}'"
                        )
                        state["material_hydrated_seen"].add(signature)
                refresh_count += 1
            state["materials_refreshed"] += refresh_count
            state["phase_times"]["materials"] += time.perf_counter() - t_materials
            if texture_bytes_ready:
                # Baked tint materials can be built after their signature was
                # already marked hydrated (geometry lags hydration in the same
                # tick); sweep the baked cache directly for anything still
                # missing its images.
                state["baked_hydrated"] += (
                    _textures_mod.hydrate_pending_baked_materials()
                )
        static_applied = 0
        t_static = time.perf_counter()
        # Citytemplate has hundreds of tiny collection/material static
        # batches. Applying only one per 50ms imposed a 45-second artificial
        # floor before geometry creation even began. Use the same bounded
        # callback budget, so common tiny batches coalesce into useful work.
        while (
            state["static_batches"]
            and static_applied < _LAZY_STATIC_BATCH_BUDGET
            and time.perf_counter() < deadline
        ):
            collection, batch_name, entries, material_key = state["static_batches"].popleft()
            entry = entries[0]
            from ..rig.textures import _effective_material_id, variant_signature

            # get_part_material's own cache already collapses colour for
            # built-in materials, but resolving it once per BATCH still
            # costs hundreds of python/RNA round-trips that crawl under
            # worker-thread contention.  Resolve once per visual key and
            # make every batch a plain dict lookup instead.
            try:
                shared_key = (
                    _effective_material_id(entry),
                    round(float(entry.get("transparency", 0.0)), 5),
                    round(float(entry.get("reflectance", 0.0)), 5),
                    bool(entry.get("_use_2022_materials", True)),
                    variant_signature(entry),
                )
            except (TypeError, ValueError):
                shared_key = (entry.get("material"), 0.0, 0.0, True, "")
            material = state["static_shared_materials"].get(shared_key)
            t_material = time.perf_counter()
            if material is None:
                try:
                    from ..rig import textures as _textures_mod

                    # The built-in map images may still be in flight on the
                    # prefetch worker; building without them keeps the main
                    # thread off the network (one synchronous fetch per miss
                    # cost ~600 ms per material).  The hydration pass, which
                    # only starts after the prefetch completes, rebuilds
                    # these materials in place with their images.
                    with _textures_mod.defer_texture_image_loading():
                        if not entry.get("material"):
                            # Plain Color3 parts share ONE attribute-tinted
                            # material; per-batch colour lands in the mesh's
                            # RBXColor attribute instead of a new datablock.
                            material = _textures_mod.plain_tint_material(
                                entry.get("transparency", 0.0)
                            )
                        else:
                            material = _textures_mod.get_part_material(batch_name, entry)
                    if material is not None:
                        state["static_shared_materials"][shared_key] = material
                        if _textures_mod.builtin_material_texture_refs(entry):
                            _register_deferred_material(
                                state, _lazy_material_signature(entry), entry
                            )
                            _mark_deferred_material_built(state, entry)
                    state["static_material_builds"] += 1
                except Exception as exc:
                    print(f"[RbxmImport] Deferred static material failed: {exc}")
            if material is not None and material.get("RBXPlainTint"):
                state["static_plain_tint_batches"] += 1
            state["static_material_seconds"] += time.perf_counter() - t_material
            t_build = time.perf_counter()
            try:
                from ..rig.creation import _create_batched_static_primitives
                mesh_obj = _create_batched_static_primitives(
                    collection,
                    batch_name,
                    entries,
                    material=material,
                )
                if (
                    mesh_obj is not None
                    and material is not None
                    and material.get("RBXPlainTint")
                ):
                    from ..rig import textures as _textures_mod
                    _textures_mod.ensure_plain_color_attribute(
                        mesh_obj.data,
                        entry.get("color") or (1.0, 1.0, 1.0),
                    )
            except Exception as exc:
                print(f"[RbxmImport] Deferred static batch failed: {exc}")
            state["static_build_seconds"] += time.perf_counter() - t_build
            static_applied += 1
            state["static_batches_done"] += 1
        state["phase_times"]["static"] += time.perf_counter() - t_static
        t_geometry = time.perf_counter()
        # A city-sized place can have thousands of visual groups waiting here.
        # The former list/filter pass rescanned every deferred job each timer
        # tick, including jobs we could not touch within this slice.  A FIFO
        # queue preserves the same bounded work while making each callback
        # proportional to the jobs it actually starts.
        while (
            state["ready_meshes"]
            and objects_applied < _LAZY_IMPORT_OBJECT_BUDGET
            and time.perf_counter() < deadline
        ):
            job = state["ready_meshes"].popleft()
            try:
                complete, created = _apply_lazy_mesh_job(
                    job,
                    _LAZY_IMPORT_OBJECT_BUDGET - objects_applied,
                    deadline,
                )
                objects_applied += created
                if not complete:
                    # An incomplete job has exhausted the remaining object or
                    # time budget, so defer it to the next callback without
                    # blocking unrelated groups behind it.
                    state["ready_meshes"].appendleft(job)
                    break
            except Exception as exc:
                print(f"[RbxmImport] Deferred mesh failed: {exc}")
        state["created_objects"] += objects_applied
        state["phase_times"]["geometry"] += time.perf_counter() - t_geometry

        if (
            not geometry_pending
            and terrain_ready
            and state["terrain_build_done"]
            and not state["finalize_done"]
            and callable(state.get("finalize"))
        ):
            # Run the CPU-heavy scene tail while texture workers are still
            # waiting on the network. Sky and beam refs are in the initial
            # prefetch set, so this does not create a second download phase.
            try:
                state["finalize"]()
            except Exception as exc:
                print(f"[RbxmImport] Place scene finalize failed: {exc}")
                state["finalize_done"] = True

        if (
            state["beam_assets_ready"]
            and state["finalize_done"]
            and not state["beam_images_hydrated"]
        ):
            started = time.perf_counter()
            state["beam_images_hydrated_count"] = _hydrate_rbxl_beam_images(
                state["beam_image_jobs"]
            )
            state["finalize_steps"]["beam image hydration"] = (
                time.perf_counter() - started
            )
            state["beam_images_hydrated"] = True

        if (
            state["sky_assets_ready"]
            and state["finalize_done"]
            and not state["post_texture_finalized"]
        ):
            started = time.perf_counter()
            try:
                callback = state.get("post_texture_finalize")
                if callable(callback):
                    callback()
            except Exception as exc:
                print(f"[RbxmImport] Post-texture scene finalize failed: {exc}")
            state["phase_times"]["finalize"] += time.perf_counter() - started
            state["post_texture_finalized"] = True

        # The material sweep + audit run LAST, after beam hydration and the
        # sky bake, so the report reflects the settled scene.  Materials
        # still missing images here are genuine failures: their refs failed
        # every fetch attempt and the audit names the nodes.
        if (
            not state.get("material_audit_done")
            and state["finalize_done"]
            and state["beam_images_hydrated"]
            and state["post_texture_finalized"]
            and not state["material_jobs"]
        ):
            started = time.perf_counter()
            state["material_rehydrated"] = (
                _textures_mod.refresh_all_unhydrated_materials()
            )
            state["finalize_steps"]["material rehydration"] = (
                time.perf_counter() - started
            )
            started = time.perf_counter()
            state["material_audit_missing"] = (
                _textures_mod.audit_unhydrated_materials()
            )
            state["finalize_steps"]["material audit"] = (
                time.perf_counter() - started
            )
            state["material_audit_done"] = True

        if (
            geometry_pending
            or state["material_jobs"]
            or (texture_future is not None and not texture_future.done())
            or (terrain_future is not None and not terrain_future.done())
            or not state["finalize_done"]
            or not state["beam_images_hydrated"]
            or not state["post_texture_finalized"]
            or not state.get("material_audit_done")
        ):
            active.append(state)
        else:
            state["pool"].shutdown(wait=False, cancel_futures=False)
            # Reveal the imported collections; they were hidden so Blender
            # could not spend the whole import redrawing a growing scene.
            # The update here is the first evaluation of all the new objects,
            # so time it: it is part of what the user waits for even though
            # it happens after the last background task.
            t_reveal = time.perf_counter()
            for collection, was_hidden in state.get("hidden_collections") or ():
                try:
                    collection.hide_viewport = was_hidden
                except ReferenceError:
                    pass
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            state["reveal_seconds"] = time.perf_counter() - t_reveal
            elapsed = time.perf_counter() - state["started"]
            pt = state["phase_times"]
            main_thread_seconds = sum(
                pt[key]
                for key in (
                    "fetch_drain", "static", "geometry", "materials", "finalize"
                )
            ) + state["reveal_seconds"] + state["terrain_build_seconds"]
            print(
                f"[RbxmImport] Deferred mesh loading complete ({state['count']} meshparts)."
            )
            print(
                f"[RbxmImport] Timing: total {elapsed + state['pre_work_seconds']:.2f}s "
                f"(sync pre-work {state['pre_work_seconds']:.2f}s + lazy {elapsed:.2f}s) | "
                f"fetch(worker) {pt['fetch_network']:.2f}s network + "
                f"{pt['fetch_parse']:.2f}s parse + "
                f"{pt['fetch_prep']:.2f}s LOD/UV prep for {state['meshes_ready']} meshes"
                f" (makespan {state['fetch_makespan']:.2f}s) | "
                f"textures(worker) {pt['textures']:.2f}s | "
                f"terrain(worker) {pt['terrain_prepare']:.2f}s | "
                f"main-thread: fetch-drain {pt['fetch_drain']:.2f}s, "
                f"static {pt['static']:.2f}s ({state['static_batches_done']} batches: "
                f"build {state['static_build_seconds']:.2f}s + "
                f"material {state['static_material_seconds']:.2f}s / "
                f"{state['static_material_builds']} builds, "
                f"{state['static_plain_tint_batches']} tinted), "
                f"geometry {pt['geometry']:.2f}s ({state['created_objects']} objects), "
                f"materials {pt['materials']:.2f}s ({state['materials_refreshed']} refreshed), "
                f"finalize {pt['finalize']:.2f}s, "
                f"terrain build {state['terrain_build_seconds']:.2f}s, "
                f"reveal {state['reveal_seconds']:.2f}s | "
                f"other {elapsed - main_thread_seconds:.2f}s (worker waits + UI loop)"
            )
            sample_count = state["mesh_in_flight_sample_count"]
            average_in_flight = (
                state["mesh_in_flight_samples"] / sample_count
                if sample_count else 0.0
            )
            full_window_percent = (
                100.0 * state["mesh_full_window_samples"] / sample_count
                if sample_count else 0.0
            )
            print(
                "[RbxmImport] Scheduler detail: "
                f"mesh window peak {state['mesh_in_flight_peak']}/"
                f"{_LAZY_FILEMESH_FETCH_CONCURRENCY}, "
                f"avg {average_in_flight:.1f}, "
                f"full {full_window_percent:.0f}% of {sample_count} timer samples | "
                f"ready backlog peak {state['ready_mesh_peak']}/"
                f"{_LAZY_READY_MESH_BACKLOG_CAP}"
            )
            signatures = tuple(state["deferred_materials"])
            if signatures:
                asset_counts = state["texture_assets"].counts()
                print(
                    "[RbxmImport] Material dependency detail: "
                    f"{len(signatures)} signatures | "
                    f"{len({signature[:2] for signature in signatures})} base, "
                    f"{len({signature[5:10] for signature in signatures})} texture binding, "
                    f"{len({signature[2:5] for signature in signatures})} colour variants | "
                    + ", ".join(
                        f"{state_key.name} {count}"
                        for state_key, count in asset_counts.items()
                        if count
                    )
                )
            texture_stats = state.get("texture_stats") or {}
            if texture_stats:
                payload_mib = float(texture_stats.get("bytes", 0.0)) / 1048576.0
                texture_wall = float(texture_stats.get("elapsed", 0.0))
                print(
                    "[RbxTexture] Prefetch detail: "
                    f"{int(texture_stats.get('assets', 0))} assets | "
                    f"{payload_mib:.2f} MiB at "
                    f"{payload_mib / max(texture_wall, 1e-9):.2f} MiB/s wall | "
                    f"fetch {texture_stats.get('fetch', 0.0):.2f}s "
                    f"(delivery {texture_stats.get('delivery', 0.0):.2f}s, "
                    f"cdn {texture_stats.get('cdn', 0.0):.2f}s, "
                    f"thumbnail {texture_stats.get('thumbnail', 0.0):.2f}s) | "
                    f"cache write {texture_stats.get('cache_write', 0.0):.2f}s"
                )
            if state["finalize_steps"]:
                detail = ", ".join(
                    f"{name} {seconds:.2f}s"
                    for name, seconds in state["finalize_steps"].items()
                )
                print(f"[RbxmImport] Finalize detail: {detail}")
            _release_completed_lazy_state(state)
    _LAZY_PLACE_IMPORTS[:] = active
    if not active:
        # execute() cannot release these caches while a place timer is still
        # using them; release them immediately after the final deferred batch.
        from ..rig import filemesh, textures
        filemesh.release_import_cache()
        textures.release_import_byte_cache()
    if not active:
        return None
    # Keep the rapid handoff only while there is Blender-main-thread work to
    # consume.  In the worker-bound phase this avoids a 1 kHz timer wake-up
    # (and repeated material-job requeues while texture bytes are in flight).
    for state in active:
        texture_future = state.get("texture_future")
        texture_ready = texture_future is None or texture_future.done()
        if (
            state["ready_meshes"]
            or state["static_batches"]
            or state["queued_meshes"]
            or (
                state["material_jobs"]
                and texture_ready
                and not state["jobs"]
            )
        ):
            return _LAZY_IMPORT_TIMER_INTERVAL
    return _LAZY_IMPORT_WORKER_POLL_INTERVAL


def _schedule_lazy_place_meshes(
    jobs, static_batches=(), texture_refs=(), terrain_meta=None, pre_work_seconds=0.0,
    raw_decode_refs=(), beam_texture_refs=(), terrain_texture_refs=(), mesh_lod=0,
):
    if not jobs and not static_batches and not terrain_meta:
        return
    # Reuse TLS connections across the hundreds of AssetDelivery/CDN requests
    # in a cold place import. Older Blender builds without requests retain the
    # dependency-free urllib path.
    from ..rig import filemesh as _filemesh_mod
    _filemesh_mod.enable_http_pooling()
    # Fetch enough cold assets concurrently to hide AssetDelivery latency. The
    # Blender-side ready queue bounds parsed mesh memory independently, so this
    # increases network throughput without allowing unbounded build backlog.
    # One extra worker coordinates texture-byte prefetching in parallel.
    pool = ThreadPoolExecutor(
        max_workers=_LAZY_FILEMESH_FETCH_CONCURRENCY + 2,
        thread_name_prefix="rbx-filemesh",
    )
    # Resolve authentication on Blender's main thread; workers must never
    # refresh a rotating OAuth token concurrently.
    from ..core.auth import get_auth_headers
    from ..core.asset_pipeline import AssetRegistry
    from ..rig import textures as textures_mod
    auth_headers = get_auth_headers()
    texture_assets = AssetRegistry()
    # Material consumers can be discovered before the prefetch worker starts.
    # Establish the complete key set here so a subscription cannot mistake an
    # as-yet-unregistered dependency for an already-complete one.
    for texture_ref in texture_refs:
        if texture_ref:
            texture_assets.register(
                textures_mod._texture_asset_key(texture_ref),
                str(texture_ref),
                "texture",
            )
    sky_asset_keys = tuple(
        textures_mod._texture_asset_key(ref) for ref in raw_decode_refs if ref
    )
    beam_asset_keys = tuple(
        textures_mod._texture_asset_key(ref) for ref in beam_texture_refs if ref
    )
    terrain_asset_keys = tuple(
        textures_mod._texture_asset_key(ref) for ref in terrain_texture_refs if ref
    )
    sky_assets_ready = (
        not sky_asset_keys
        or texture_assets.subscribe(_SKY_ASSET_CONSUMER, sky_asset_keys)
    )
    beam_assets_ready = (
        not beam_asset_keys
        or texture_assets.subscribe(_BEAM_ASSET_CONSUMER, beam_asset_keys)
    )
    terrain_assets_ready = (
        not terrain_asset_keys
        or texture_assets.subscribe(_TERRAIN_ASSET_CONSUMER, terrain_asset_keys)
    )
    grouped_jobs = {}
    for entry, collection in jobs:
        grouped_jobs.setdefault(_lazy_mesh_instance_key(entry), []).append((entry, collection))
    asset_groups = {}
    for entries in grouped_jobs.values():
        entry, _collection = entries[0]
        asset_groups.setdefault(str(entry["mesh_id"]), {
            "mesh_id": entry["mesh_id"], "groups": [],
        })["groups"].append(entries)
    # Hide every target collection while it fills.  Blender redraws the
    # viewport after each timer slice, and a redraw of a partially imported
    # place costs far more than the slice itself once thousands of objects
    # are linked.  Hiding defers all of that to a single reveal at the end.
    hidden_collections = []
    seen_collections = set()
    for collection in [
        collection for _entry, collection in jobs
    ] + [batch[0] for batch in static_batches]:
        try:
            pointer = collection.as_pointer()
        except (AttributeError, ReferenceError):
            continue
        if pointer in seen_collections:
            continue
        seen_collections.add(pointer)
        hidden_collections.append((collection, bool(collection.hide_viewport)))
        collection.hide_viewport = True
    state = {
        "pool": pool,
        # One immutable snapshot is sufficient for a bounded import and avoids
        # refreshing a rotating OAuth token once per unique mesh asset.
        "auth_headers": auth_headers,
        "mesh_lod": max(0, int(mesh_lod)),
        "jobs": [],
        "count": len(jobs),
        "queued_meshes": list(asset_groups.values()),
        "ready_meshes": deque(),
        "static_batches": deque(static_batches),
        "static_shared_materials": {},
        "deferred_materials": {},
        "material_hydration_seen": set(),
        "material_hydrated_seen": set(),
        "lazy_materials": {},
        "material_jobs": deque(),
        "material_assets_ready": set(),
        "material_graphs_ready": set(),
        "material_ready_seen": set(),
        "material_refresh_attempts": {},
        "texture_assets": texture_assets,
        "material_hydration_started": False,
        "phase_times": {
            "fetch_drain": 0.0,
            "static": 0.0,
            "geometry": 0.0,
            "materials": 0.0,
            "fetch_network": 0.0,
            "fetch_parse": 0.0,
            "fetch_prep": 0.0,
            "textures": 0.0,
            "terrain_prepare": 0.0,
            "finalize": 0.0,
        },
        "texture_seconds_consumed": False,
        "texture_stats": {},
        "texture_completion_count": 0,
        "terrain_consumed": False,
        "prepared_terrain": None,
        "terrain_builder": None,
        "terrain_built": 0,
        "terrain_build_done": False,
        "terrain_build_seconds": 0.0,
        "finalize_steps": {},
        "beam_image_jobs": [],
        "sky_assets_ready": sky_assets_ready,
        "beam_assets_ready": beam_assets_ready,
        "terrain_assets_ready": terrain_assets_ready,
        "beam_images_hydrated": True,
        "beam_images_hydrated_count": 0,
        "material_audit_done": False,
        "material_rehydrated": 0,
        "material_audit_missing": 0,
        "post_texture_finalize": None,
        "post_texture_finalized": True,
        "fetch_makespan": None,
        "mesh_in_flight_peak": 0,
        "mesh_in_flight_samples": 0,
        "mesh_in_flight_sample_count": 0,
        "mesh_full_window_samples": 0,
        "ready_mesh_peak": 0,
        "static_batches_done": 0,
        "static_build_seconds": 0.0,
        "static_material_seconds": 0.0,
        "static_material_builds": 0,
        "static_plain_tint_batches": 0,
        "created_objects": 0,
        "meshes_ready": 0,
        "materials_refreshed": 0,
        "baked_hydrated": 0,
        "reveal_seconds": 0.0,
        "pre_work_seconds": pre_work_seconds,
        "finalize": None,
        "finalize_done": True,
        "hidden_collections": hidden_collections,
        "started": time.perf_counter(),
        "texture_future": (
            pool.submit(
                _lazy_prefetch_texture_bytes,
                tuple(texture_refs),
                auth_headers,
                tuple(raw_decode_refs),
                texture_assets,
            )
            if texture_refs else None
        ),
        "terrain_future": (
            pool.submit(_lazy_prepare_terrain, terrain_meta)
            if terrain_meta and terrain_meta.get("smoothgrid") else None
        ),
    }
    _LAZY_PLACE_IMPORTS.append(state)
    if not bpy.app.timers.is_registered(_process_lazy_place_imports):
        bpy.app.timers.register(_process_lazy_place_imports, first_interval=0.05)


@contextmanager
def _ensure_all_bone_collections_visible(armature):
    """Temporarily unhide all bone collections on an armature so that
    edit_bones can access bones in hidden collections.  Restores original
    visibility on exit.  Safe on pre-4.0 builds that lack bone collections."""
    saved = {}
    collections = getattr(armature.data, "collections", None)
    if collections is not None:
        for bc in collections:
            saved[bc.name] = bc.is_visible
            bc.is_visible = True
    try:
        yield
    finally:
        if collections is not None:
            for bc in collections:
                if bc.name in saved:
                    bc.is_visible = saved[bc.name]


def _strip_suffix(name: str) -> str:
    return re.sub(r"\.\d+$", "", name or "")


def _resolve_imported_obj_name(name: str, known_names=None) -> str:
    """Resolve Blender OBJ-import numeric suffixes against known target names.

    Blender's OBJ importer often appends a trailing digit when duplicate object
    names collide, e.g. "Sword" -> "Sword1" or "AccessoryDW3" ->
    "Accessorydw31". Only collapse that suffix when doing so matches a known
    metadata target name.
    """
    base_name = _strip_suffix(name).lower()
    if not known_names or base_name in known_names:
        return base_name

    match = re.match(r"^(.*?)(\d+)$", base_name)
    if not match:
        return base_name

    prefix, digits = match.groups()
    for trim_count in range(1, len(digits) + 1):
        candidate = prefix + digits[:-trim_count]
        if candidate in known_names:
            return candidate

    return base_name


def _dict_get_any(data, keys):
    """Get first present non-empty value for any key (case/underscore-insensitive)."""
    if not isinstance(data, dict):
        return None

    for key in keys:
        if key in data:
            value = data.get(key)
            if value not in (None, ""):
                return value

    normalized = {}
    for k, v in data.items():
        if not isinstance(k, str):
            continue
        nk = k.replace("_", "").lower()
        if nk not in normalized:
            normalized[nk] = v

    for key in keys:
        nk = key.replace("_", "").lower()
        if nk in normalized:
            value = normalized[nk]
            if value not in (None, ""):
                return value
    return None


def _coerce_cf12(value):
    """Best-effort coercion to a 12-number CFrame array."""
    if value is None:
        return None
    try:
        if hasattr(value, "to_list"):
            value = value.to_list()
        elif not isinstance(value, (list, tuple)):
            value = list(value)
    except Exception:
        return None

    if len(value) < 12:
        return None
    return [value[i] for i in range(12)]


def _joint_transform_key(node, *keys):
    return _coerce_cf12(_dict_get_any(node, keys))


def _get_joint_part_world_matrix(node):
    transform = _joint_transform_key(node, "transform")
    if not transform:
        return None
    return cf_to_mat(transform)


def _get_joint_anchor_world_matrix(node):
    part_world = _get_joint_part_world_matrix(node)
    if part_world is None:
        return None

    child_offset = _joint_transform_key(node, "jointtransform1", "jointTransform1")
    if child_offset:
        return part_world @ cf_to_mat(child_offset)
    return part_world


def _matrix_difference_score(left, right):
    if left is None or right is None:
        return float("inf")

    translation_error = (left.to_translation() - right.to_translation()).length
    rotation_error = 0.0
    for row in range(3):
        for col in range(3):
            rotation_error += abs(left[row][col] - right[row][col])

    return translation_error + rotation_error


def _annotate_weapon_original_parents(joints_tree, attachment_parent_name, attachment_parent_transform):
    """Recover the original Motor6D parent for imported weapon bones."""
    assignments = {}

    if not isinstance(joints_tree, dict) or not attachment_parent_name or attachment_parent_transform is None:
        return assignments

    def recurse(node, candidates):
        if not isinstance(node, dict):
            return

        joint_name = node.get("jname")
        node["originalParentBone"] = attachment_parent_name
        if joint_name:
            assignments[joint_name] = attachment_parent_name

        child_anchor_world = _get_joint_anchor_world_matrix(node)
        parent_offset = _joint_transform_key(node, "jointtransform0", "jointTransform0")
        if child_anchor_world is not None and parent_offset and candidates:
            parent_offset_mat = cf_to_mat(parent_offset)
            best_parent_name = attachment_parent_name
            best_score = float("inf")

            for candidate_name, candidate_part_world in candidates:
                predicted_child_anchor = candidate_part_world @ parent_offset_mat
                score = _matrix_difference_score(predicted_child_anchor, child_anchor_world)
                if score < best_score:
                    best_score = score
                    best_parent_name = candidate_name

            node["originalParentBone"] = best_parent_name
            if joint_name:
                assignments[joint_name] = best_parent_name

        for child in node.get("children", []) or []:
            recurse(child, candidates)

    recurse(joints_tree, [(attachment_parent_name, attachment_parent_transform)])
    return assignments


def _norm_name(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s_]+", "", value).lower()


def _iter_dicts_recursive(node, depth=0, max_depth=24):
    """Yield (dict_node, depth) for nested dict/list payloads."""
    if depth > max_depth:
        return
    if isinstance(node, dict):
        yield node, depth
        for child in node.values():
            if isinstance(child, (dict, list, tuple)):
                yield from _iter_dicts_recursive(child, depth + 1, max_depth)
    elif isinstance(node, (list, tuple)):
        for child in node:
            if isinstance(child, (dict, list, tuple)):
                yield from _iter_dicts_recursive(child, depth + 1, max_depth)


def _iter_part_aux_entries(meta_loaded):
    if not isinstance(meta_loaded, dict):
        return []
    part_aux = meta_loaded.get("partAux") or []
    if isinstance(part_aux, dict):
        return list(part_aux.values())
    return list(part_aux)


def _normalize_accessory_handle_jnames(meta_loaded):
    """Rewrite stale accessory Handle joint names to their exported part names.

    Older Studio exports often encode accessory weld nodes with joint names like
    Handle/Handle1 while the actual exported part name lives in pname. That leaks
    into Blender bone creation and fallback matching. Normalize those nodes early
    so the importer consistently uses the exported accessory part name.
    """
    if not isinstance(meta_loaded, dict):
        return 0

    renamed = 0

    def recurse(node):
        nonlocal renamed

        if not isinstance(node, dict):
            return

        joint_type = str(node.get("jointType") or "")
        jname = node.get("jname")
        pname = node.get("pname")
        if (
            joint_type in {"Weld", "WeldConstraint"}
            and isinstance(jname, str)
            and isinstance(pname, str)
            and jname.lower().startswith("handle")
            and not pname.lower().startswith("handle")
            and pname.strip()
        ):
            node["jname"] = pname
            renamed += 1

        for child in node.get("children") or []:
            recurse(child)

    recurse(meta_loaded.get("rig"))
    recurse(meta_loaded.get("joints"))
    for attachment in meta_loaded.get("weaponAttachments") or []:
        if isinstance(attachment, dict):
            recurse(attachment.get("joints"))

    return renamed


def _meta_has_skinned_meshes(meta_loaded):
    """Return True when metadata explicitly marks any skinned mesh part entries."""
    for entry in _iter_part_aux_entries(meta_loaded):
        if isinstance(entry, dict) and entry.get("has_skinning") and entry.get("mesh_id"):
            return True
    return False


def _meta_is_majority_skinned(meta_loaded):
    """Return True when most body-part MeshParts have explicit skinning data.

    Distinguishes proper skinned rigs (most limbs skinned -> CONNECT) from
    hybrid rigs (only a few parts skinned, e.g. head -> LOCAL_YAXIS_EXTEND).
    """
    total = 0
    skinned = 0
    for entry in _iter_part_aux_entries(meta_loaded):
        if not isinstance(entry, dict):
            continue
        if not entry.get("mesh_id"):
            continue
        mesh_class = entry.get("mesh_class")
        if mesh_class not in (None, "", "MeshPart"):
            continue
        # skip accessories (wrap layers) — only count body parts
        if entry.get("wrap_layer"):
            continue
        total += 1
        if entry.get("has_skinning"):
            skinned += 1
    return total > 0 and skinned > total / 2


def _meta_has_filemesh_candidates(meta_loaded):
    """Return True when metadata includes any MeshPart filemesh candidates.

    Studio does not always expose skinning via Bone descendants, so Blender must
    sometimes inspect the FileMesh directly to determine whether weights exist.
    """
    for entry in _iter_part_aux_entries(meta_loaded):
        if not isinstance(entry, dict):
            continue
        if not entry.get("mesh_id"):
            continue
        mesh_class = entry.get("mesh_class")
        if mesh_class in (None, "", "MeshPart"):
            return True
    return False


def _rig_contains_deform_bones(node):
    """Return True when the exported rig tree contains deform bone nodes."""
    if not isinstance(node, dict):
        return False
    if node.get("isDeformBone") or node.get("jointType") == "Bone":
        return True
    for child in node.get("children", []):
        if _rig_contains_deform_bones(child):
            return True
    return False


def _extract_motor6d_connection(meta_loaded, weapon_root_name, preferred_parent_name=None):
    """Find Motor6D-like connection data anywhere in metadata payload.

    Returns dict with parent_name/connectionC0/connectionC1 when found.
    """
    if not isinstance(meta_loaded, dict):
        return None

    root_norm = _norm_name(weapon_root_name)
    pref_norm = _norm_name(preferred_parent_name)
    if not root_norm:
        return None

    part0_keys = ("part0", "Part0", "parentPart", "parent_part", "parent", "from")
    part1_keys = ("part1", "Part1", "childPart", "child_part", "child", "to")

    c0_keys = (
        "connectionC0",
        "connection_c0",
        "C0",
        "c0",
        "jointtransform0",
        "jointTransform0",
    )
    c1_keys = (
        "connectionC1",
        "connection_c1",
        "C1",
        "c1",
        "jointtransform1",
        "jointTransform1",
    )

    best = None

    for node, depth in _iter_dicts_recursive(meta_loaded):
        c0 = _coerce_cf12(_dict_get_any(node, c0_keys))
        c1 = _coerce_cf12(_dict_get_any(node, c1_keys))
        if not (c0 and c1):
            continue

        part0 = _dict_get_any(node, part0_keys)
        part1 = _dict_get_any(node, part1_keys)
        if not (isinstance(part0, str) and isinstance(part1, str)):
            continue

        p0 = _norm_name(part0)
        p1 = _norm_name(part1)

        reverse = False
        score = 0

        if p1 == root_norm:
            score += 8
        elif p0 == root_norm:
            score += 6
            reverse = True
        else:
            continue

        parent_name = part0 if not reverse else part1
        parent_norm = p0 if not reverse else p1
        if pref_norm and parent_norm == pref_norm:
            score += 4

        jt = _dict_get_any(node, ("jointType", "joint_type", "type", "ClassName", "className"))
        if isinstance(jt, str) and "motor6d" in jt.lower():
            score += 1

        # Prefer shallower nodes when score ties.
        score -= depth * 0.01

        if reverse:
            # Swapped relation: root*C0 = parent*C1  -> parent*C1 = root*C0
            use_c0, use_c1 = c1, c0
        else:
            use_c0, use_c1 = c0, c1

        candidate = {
            "parent_name": parent_name,
            "connectionC0": use_c0,
            "connectionC1": use_c1,
            "jointType": jt or "Motor6D",
            "score": score,
            "depth": depth,
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate

    return best


def _collect_weapon_suggested_bones(meta_loaded):
    """Collect unique suggested attachment bones from weapon metadata."""
    if not isinstance(meta_loaded, dict):
        return []

    suggested = []
    top = _dict_get_any(
        meta_loaded,
        (
            "suggestedBone",
            "suggested_bone",
            "attachmentBone",
            "attachBone",
            "parentBone",
            "parent_bone",
        ),
    ) or ""
    if isinstance(top, str) and top:
        suggested.append(top)

    attachments = meta_loaded.get("weaponAttachments")
    if isinstance(attachments, list):
        for att in attachments:
            if not isinstance(att, dict):
                continue
            sb = _dict_get_any(
                att,
                (
                    "suggestedBone",
                    "suggested_bone",
                    "attachmentBone",
                    "attachBone",
                    "parentBone",
                    "parent_bone",
                ),
            ) or ""
            if isinstance(sb, str) and sb:
                suggested.append(sb)

    # rbxm weapon exports stamp grip data as attributes (weaponGrip), not
    # under the OBJ weaponAttachments schema.  Each grip's rig-side bone is
    # the suggested attach point.
    grips = meta_loaded.get("weaponGrip")
    if isinstance(grips, list):
        for grip in grips:
            if not isinstance(grip, dict):
                continue
            bone = grip.get("bone")
            if isinstance(bone, str) and bone:
                suggested.append(bone)

    unique = []
    seen = set()
    for name in suggested:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique


def _find_bone_case_insensitive(armature, suggested):
    """Return matched bone name from armature or None."""
    if not armature or armature.type != "ARMATURE" or not suggested:
        return None
    if suggested in armature.data.bones:
        return suggested
    suggested_lower = suggested.lower()
    for bone in armature.data.bones:
        if bone.name.lower() == suggested_lower:
            return bone.name
    return None


def _infer_weapon_parent_bone_from_transform(armature, joints_tree):
    """Infer likely parent bone by nearest rest-transform position.

    Uses Roblox transform props when available (preferred), falls back to
    armature-space bone heads for older/partial rigs.
    """
    from mathutils import Matrix

    if (
        not armature
        or armature.type != "ARMATURE"
        or not isinstance(joints_tree, dict)
        or not joints_tree.get("transform")
    ):
        return None, None

    try:
        t2b = get_transform_to_blender()
        weapon_root_pos = (t2b @ cf_to_mat(joints_tree["transform"])).to_translation()
    except Exception:
        return None, None

    best_name = None
    best_dist = None

    for bone in armature.data.bones:
        bone_pos = None
        tf_prop = bone.get("transform")
        if tf_prop:
            try:
                bone_mat = Matrix([list(row) for row in tf_prop])
                bone_pos = (t2b @ bone_mat).to_translation()
            except Exception:
                bone_pos = None

        if bone_pos is None:
            try:
                bone_pos = armature.matrix_world @ bone.head_local
            except Exception:
                continue

        dist = (weapon_root_pos - bone_pos).length
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_name = bone.name

    return best_name, best_dist


def _should_redirect_weapon_import(selected_armature, source_armature, meta_loaded):
    """Decide whether to redirect weapon import from selected rig to source rig.
    Redirect when the selected rig looks like a proxy/control rig, or when it
    is missing suggested attach bones that exist on the detected source rig."""
    if not selected_armature or not source_armature:
        return False

    # Prefer armatures that carry imported Roblox bone transforms.
    # Proxy/control rigs typically do not have these custom props.
    selected_has_transform = any(
        "transform" in b for b in selected_armature.data.bones
    )
    source_has_transform = any(
        "transform" in b for b in source_armature.data.bones
    )
    if source_has_transform and not selected_has_transform:
        return True

    suggested_bones = _collect_weapon_suggested_bones(meta_loaded)
    if not suggested_bones:
        return False

    missing_on_selected = [
        bone_name for bone_name in suggested_bones
        if not _find_bone_case_insensitive(selected_armature, bone_name)
    ]
    if not missing_on_selected:
        return False

    return all(
        _find_bone_case_insensitive(source_armature, bone_name)
        for bone_name in missing_on_selected
    )


def _dims_to_ratios(sorted_dims):
    """Compute scale-invariant aspect ratios from sorted dimensions.

    Returns (r1, r2) where r1 = dim[0]/dim[2], r2 = dim[1]/dim[2].
    These are invariant to uniform scaling and highly discriminating
    for small parts that share similar absolute sizes.
    """
    if len(sorted_dims) < 3 or sorted_dims[2] < 1e-9:
        return (1.0, 1.0)
    return (sorted_dims[0] / sorted_dims[2], sorted_dims[1] / sorted_dims[2])


def _hungarian_assign(cost_matrix, n_targets, n_cands):
    """Optimal assignment via hungarian algorithm with fallback.

    Tries scipy first, falls back to a pure-python implementation
    bc blender's bundled python may not have scipy.
    """
    try:
        from scipy.optimize import linear_sum_assignment
        row_ind, col_ind = linear_sum_assignment(cost_matrix)
        return list(zip(row_ind.tolist(), col_ind.tolist()))
    except ImportError:
        pass

    # fallback: greedy assignment by ascending cost (not optimal but
    # still globally-aware — much better than per-target greedy)
    import numpy as np
    n_rows, n_cols = cost_matrix.shape
    flat_indices = np.argsort(cost_matrix, axis=None)
    used_rows = set()
    used_cols = set()
    assignments = []
    for flat_idx in flat_indices:
        r = int(flat_idx // n_cols)
        c = int(flat_idx % n_cols)
        if r in used_rows or c in used_cols:
            continue
        assignments.append((r, c))
        used_rows.add(r)
        used_cols.add(c)
        if len(assignments) >= min(n_rows, n_cols):
            break
    return assignments


def _rename_parts_by_size_fingerprint(meta_loaded, parts_collection):
    """Rename parts using size fingerprints, aspect ratios, and position data.

    Uses global optimal assignment (hungarian/munkres) instead of greedy
    matching, with scale-invariant aspect ratios for better discrimination
    of small parts.
    """
    import bmesh
    import numpy as np
    from collections import defaultdict

    part_aux_raw = meta_loaded.get("partAux")
    if not part_aux_raw:
        return 0

    # lua arrays with numeric keys come through as dicts {"1": ..., "2": ...}
    if isinstance(part_aux_raw, dict):
        part_aux_list = list(part_aux_raw.values())
    else:
        part_aux_list = part_aux_raw

    rig_name = meta_loaded.get("rigName", "Rig")
    num_targets = len(part_aux_list)
    print(f"[RigImport] Fingerprint matching {num_targets} targets...")

    # Build expected position map from rig definition.
    # Maps lowercase name -> list[Vector] to handle duplicate names
    # (e.g. multiple joints named "Part").
    expected_loc_by_name = defaultdict(list)
    rig_def = meta_loaded.get("rig")
    t2b = get_transform_to_blender()
    if rig_def:
        def collect_expected_positions(node, depth=0):
            if not node:
                return
            jname = node.get("jname")
            pname = node.get("pname")
            node_transform = node.get("transform")
            if node_transform:
                if jname:
                    try:
                        expected_loc = (t2b @ cf_to_mat(node_transform)).to_translation()
                        expected_loc_by_name[jname.lower()].append(expected_loc)
                    except Exception:
                        pass
                if pname and pname != jname:
                    try:
                        expected_loc = (t2b @ cf_to_mat(node_transform)).to_translation()
                        expected_loc_by_name[pname.lower()].append(expected_loc)
                    except Exception:
                        pass

            aux_names = node.get("aux") or []
            aux_transforms = node.get("auxTransform") or []
            for idx, aux_name in enumerate(aux_names):
                if not aux_name:
                    continue
                cf = aux_transforms[idx] if idx < len(aux_transforms) else None
                if not cf:
                    continue
                try:
                    expected_loc = (t2b @ cf_to_mat(cf)).to_translation()
                    expected_loc_by_name[aux_name.lower()].append(expected_loc)
                except Exception:
                    pass

            for child in (node.get("children") or []):
                collect_expected_positions(child, depth + 1)

        collect_expected_positions(rig_def)

    # Pre-process targets — now includes aspect ratios
    fp_targets = []
    for item in part_aux_list:
        if not item or not isinstance(item, dict) or "idx" not in item:
            continue

        idx = item["idx"]
        target_name = item.get("name", f"{rig_name}{idx}")
        target_lower = str(target_name).lower()
        target_family = "accessory" if (target_lower.startswith("handle") or item.get("wrap_layer")) else "body"

        dims = item.get("dims_fp")
        if dims and len(dims) == 3:
            sorted_dims = tuple(sorted([float(x) for x in dims]))
            ratios = _dims_to_ratios(sorted_dims)
            fp_targets.append({
                "target": target_name,
                "family": target_family,
                "dims": sorted_dims,
                "ratios": ratios,
                "sig": sum(sorted_dims),
                "is_vol": False,
                "is_wrap_layer": bool(item.get("wrap_layer")),
            })
        elif "vol_fp" in item:
            vol = float(item["vol_fp"])
            fp_targets.append({
                "target": target_name,
                "family": target_family,
                "dims": (vol,),
                "ratios": (1.0, 1.0),
                "sig": vol,
                "is_vol": True,
                "is_wrap_layer": bool(item.get("wrap_layer")),
            })

    if not fp_targets:
        return 0

    known_target_names = {item["target"].lower(): item.get("family") or "body" for item in fp_targets}

    mesh_objects = [o for o in parts_collection.objects if o.type == "MESH"]
    mesh_centers = {obj: _get_mesh_world_center(obj) for obj in mesh_objects}

    # Build candidate data — now includes aspect ratios
    all_candidates = []

    for obj in mesh_objects:
        base_name = _resolve_imported_obj_name(obj.name, known_target_names)
        candidate_family = known_target_names.get(
            base_name,
            "accessory" if base_name.startswith("handle") else "body",
        )
        d = obj.dimensions
        sorted_dims = tuple(sorted([d.x, d.y, d.z]))
        sig = sum(sorted_dims)
        ratios = _dims_to_ratios(sorted_dims)

        cand = {
            "obj": obj,
            "family": candidate_family,
            "dims": sorted_dims,
            "ratios": ratios,
            "sig": sig,
            "resolved_name": base_name,
        }
        all_candidates.append(cand)

    # Scoring parameters — tightened to reduce false positives
    SIZE_WEIGHT = 1.0
    RATIO_WEIGHT = 4.0       # aspect ratios are scale-invariant, very reliable
    POS_WEIGHT = 5.0          # position is the most trustworthy signal
    SIDE_MISMATCH_PENALTY = 100.0  # wrong-side-of-rig penalty (was 50)
    # wrap_layer/body family mismatch is now blocked below unless name-confirmed

    MAX_ACCEPTABLE_REL_DIFF = 0.06  # tightened (was 0.08)
    MAX_ACCEPTABLE_DIFF_VOL = 0.02
    MIN_SCALE = 0.1
    MAX_SCALE = 10.0

    PROHIBITIVE_COST = 1e6   # "impossible" assignment cost
    MAX_ACCEPTABLE_COST = 2.5  # reject matches above this (was 3.0)

    n_targets = len(fp_targets)
    n_cands = len(all_candidates)

    if n_cands == 0:
        return 0

    # --- estimate rig scale BEFORE scoring ---
    # compare each target sig against ALL candidate sigs to find the
    # most common scale factor. this lets us scale position comparisons
    # correctly without chicken-and-egg problems.
    scale_votes = []
    for target in fp_targets:
        if target.get("is_vol", False) or target["sig"] <= 0:
            continue
        for cand in all_candidates:
            s = cand["sig"] / target["sig"]
            if MIN_SCALE <= s <= MAX_SCALE:
                # only vote if shape roughly matches (quick aspect ratio check)
                t_ratios = target["ratios"]
                c_ratios = cand["ratios"]
                if abs(t_ratios[0] - c_ratios[0]) + abs(t_ratios[1] - c_ratios[1]) < 0.5:
                    scale_votes.append(s)

    from statistics import median
    if scale_votes:
        estimated_rig_scale = median(scale_votes)
    else:
        estimated_rig_scale = 1.0

    # position distance threshold scales with rig scale but capped
    MAX_POS_DIST = min(max(0.5, 0.5 * estimated_rig_scale), 3.0)

    print(f"[RigImport] Pre-estimated rig scale: {estimated_rig_scale:.4f}, pos threshold: {MAX_POS_DIST:.3f}")

    # Build full cost matrix: targets (rows) x candidates (cols)
    cost_matrix = np.full((n_targets, n_cands), PROHIBITIVE_COST, dtype=np.float64)
    scale_matrix = np.ones((n_targets, n_cands), dtype=np.float64)

    for ti, target in enumerate(fp_targets):
        target_name = target["target"]
        target_lower = target_name.lower()
        target_family = target.get("family") or "body"
        target_dims = target["dims"]
        target_sig = target["sig"]
        target_ratios = target["ratios"]
        is_vol = target.get("is_vol", False)
        is_wrap_layer = bool(target.get("is_wrap_layer"))
        expected_locs = expected_loc_by_name.get(target_lower)

        for ci, cand in enumerate(all_candidates):
            obj = cand["obj"]
            mesh_center = mesh_centers[obj]
            name_confirmed_wrap = is_wrap_layer and cand.get("resolved_name") == target_lower
            family_penalty = 0.0

            if cand.get("family") != target_family:
                if is_wrap_layer and name_confirmed_wrap:
                    # name-confirmed wrap_layer (e.g. Handle->Pants) gets no penalty
                    family_penalty = 0.0
                elif is_wrap_layer:
                    # allow wrap_layer targets to reclaim body-named candidates
                    family_penalty = 0.25
                else:
                    # block family cross-over for non-wrap targets
                    continue

            # --- size compatibility check ---
            if is_vol:
                if "vol" not in cand:
                    bm = bmesh.new()
                    try:
                        bm.from_mesh(obj.data)
                        cand["vol"] = abs(bm.calc_volume())
                    except Exception:
                        dd = cand["dims"]
                        cand["vol"] = dd[0] * dd[1] * dd[2]
                    finally:
                        bm.free()
                vol_diff = abs(target_dims[0] - cand["vol"])
                if vol_diff > MAX_ACCEPTABLE_DIFF_VOL:
                    continue
                size_norm = vol_diff / max(MAX_ACCEPTABLE_DIFF_VOL, 1e-9)
                scale = 1.0
                ratio_norm = 0.0  # no ratio info for volume-only
            else:
                if target_sig <= 0:
                    continue
                scale = cand["sig"] / target_sig
                if scale < MIN_SCALE or scale > MAX_SCALE:
                    continue
                scaled_target = [d * scale for d in target_dims]
                size_diff = sum(abs(a - b) for a, b in zip(scaled_target, cand["dims"]))
                size_diff = size_diff / max(cand["sig"], 1e-6)
                if size_diff > MAX_ACCEPTABLE_REL_DIFF:
                    continue
                size_norm = size_diff / max(MAX_ACCEPTABLE_REL_DIFF, 1e-9)

                # aspect ratio difference — scale-invariant, crucial for small parts
                cand_ratios = cand["ratios"]
                ratio_diff = abs(target_ratios[0] - cand_ratios[0]) + abs(target_ratios[1] - cand_ratios[1])
                ratio_norm = ratio_diff  # already 0-based, typically 0-2 range

            # --- position component ---
            # use global estimated_rig_scale for expected positions, NOT
            # per-candidate scale (which would distort world positions)
            pos_norm = 1.0  # neutral if no position data
            side_penalty = 0.0

            if expected_locs:
                # find the nearest expected position for this name
                best_dist = float('inf')
                best_scaled = None
                for eloc in expected_locs:
                    es = eloc * estimated_rig_scale
                    d = (mesh_center - es).length
                    if d < best_dist:
                        best_dist = d
                        best_scaled = es
                pos_dist = best_dist
                if name_confirmed_wrap:
                    pos_norm = 0.0
                else:
                    pos_norm = min(pos_dist / max(MAX_POS_DIST, 1e-6), 3.0)

                # side mismatch: penalize when mesh and expected position
                # disagree on which side of the rig they're on (x-sign).
                expected_x = best_scaled.x
                mesh_x = mesh_center.x
                tolerance = max(0.02, 0.05 * estimated_rig_scale)
                if (not name_confirmed_wrap
                        and abs(expected_x) >= tolerance
                        and abs(mesh_x) >= tolerance):
                    if (expected_x > 0) != (mesh_x > 0):
                        side_penalty = SIDE_MISMATCH_PENALTY

            score = (SIZE_WEIGHT * size_norm
                     + RATIO_WEIGHT * ratio_norm
                     + POS_WEIGHT * pos_norm
                     + side_penalty
                     + family_penalty)

            cost_matrix[ti, ci] = score
            scale_matrix[ti, ci] = scale

    # --- global optimal assignment ---
    assignments = _hungarian_assign(cost_matrix, n_targets, n_cands)

    # fingerprint_object_map maps FINAL blender object name -> obj ref
    # We build it AFTER renames so that blender's auto-suffixes (.001 etc)
    # are captured correctly. This is critical for duplicate target names
    # (e.g. multiple parts all called "bonnie left hand").
    fingerprint_object_map = {}
    renamed_count = 0
    rejected_count = 0
    skipped_count = 0
    scale_samples = []

    # Collect all renames first, then apply in two passes to avoid
    # blender's auto-suffixing (.001) when a target name already exists.
    # Without this, renaming obj_A to "LeftHand" when "LeftHand" already
    # exists causes blender to silently rename the EXISTING "LeftHand"
    # to "LeftHand.001", corrupting downstream name-based matching.
    pending_fp_renames = []  # (obj, target_name)
    accepted_assignments = []  # (obj, target_name, pos_confirmed) — all accepted, incl. already-correct

    # Position-lock threshold: only FP-lock matches whose mesh center
    # is within this distance of the expected position. matches with
    # poor position agreement are still renamed but left unlocked so
    # pass 2 can override them via position matching.
    FP_LOCK_POS_THRESHOLD = MAX_POS_DIST * 1.5

    for ti, ci in assignments:
        cost = cost_matrix[ti, ci]
        if cost >= PROHIBITIVE_COST:
            continue  # no valid match for this target
        if cost > MAX_ACCEPTABLE_COST:
            target_name = fp_targets[ti]["target"]
            obj_name = all_candidates[ci]["obj"].name
            print(f"[RigImport]   rejected '{target_name}' -> '{obj_name}' (cost={cost:.3f} > {MAX_ACCEPTABLE_COST})")
            rejected_count += 1
            continue

        target = fp_targets[ti]
        cand = all_candidates[ci]
        obj = cand["obj"]
        target_name = target["target"]
        scale = scale_matrix[ti, ci]
        wrap_name_confirmed = bool(target.get("is_wrap_layer")) and cand.get("resolved_name") == target_name.lower()

        current_name = _strip_suffix(obj.name)
        if not target.get("is_vol", False) and scale > 0:
            scale_samples.append(scale)

        # check position agreement for lock decision
        expected_locs = expected_loc_by_name.get(target_name.lower())
        pos_dist = float('inf')
        if expected_locs:
            mc = mesh_centers[obj]
            for eloc in expected_locs:
                es = eloc * estimated_rig_scale
                d = (mc - es).length
                if d < pos_dist:
                    pos_dist = d
            pos_info = f"pos_dist={pos_dist:.3f}"
        else:
            pos_info = "no_pos_data"

        pos_confirmed = wrap_name_confirmed or pos_dist < FP_LOCK_POS_THRESHOLD
        lock_tag = "LOCK" if pos_confirmed else "TENTATIVE"

        if current_name == target_name:
            skipped_count += 1
            accepted_assignments.append((obj, target_name, pos_confirmed))
        else:
            print(
                f"[RigImport]   matched '{target_name}' -> '{obj.name}' (cost={cost:.3f}, scale={scale:.3f}, {pos_info}, {lock_tag})")
            pending_fp_renames.append((obj, target_name))
            accepted_assignments.append((obj, target_name, pos_confirmed))
            renamed_count += 1

    # Two-pass rename: temp names first, then final names.
    # Only rename position-confirmed matches. Tentative matches keep
    # their original OBJ names so pass 2 can match them by position.
    confirmed_objs = {id(obj) for obj, _, pc in accepted_assignments if pc}
    confirmed_renames = [(obj, tgt) for obj, tgt in pending_fp_renames
                         if id(obj) in confirmed_objs]

    for i, (obj, _) in enumerate(confirmed_renames):
        obj.name = f"__rbxfp_{i}__"
    for obj, target_name in confirmed_renames:
        obj.name = target_name

    # Build the fingerprint map AFTER renames. Only FP-lock matches
    # where position was confirmed — tentative matches stay unlocked
    # so pass 2 can reassign them if it finds a better position match.
    tentative_count = 0
    for obj, target_name, pos_confirmed in accepted_assignments:
        if pos_confirmed:
            fingerprint_object_map[obj.name] = obj
        else:
            tentative_count += 1
            print(f"[RigImport]   TENTATIVE (not locked): '{target_name}' -> '{obj.name}' — poor position agreement")
    if tentative_count:
        print(f"[RigImport] {tentative_count} matches left tentative (unlocked for pass 2 override)")

    # count targets that got no candidate at all (prohibitive cost)
    assigned_targets = {ti for ti, ci in assignments if cost_matrix[ti, ci] < PROHIBITIVE_COST}
    for ti, target in enumerate(fp_targets):
        if ti not in assigned_targets:
            print(f"[RigImport]   '{target['target']}' unmatched: no size-compatible candidate")
            rejected_count += 1

    if scale_samples:
        rig_scale = median(scale_samples)
        meta_loaded["_rig_scale"] = rig_scale
        print(f"[RigImport] Estimated rig scale: {rig_scale:.4f}")

    print(
        f"[RigImport] Fingerprinting: {renamed_count} renamed, {skipped_count} already correct, {rejected_count} rejected")

    # --- axis debug: compare expected vs actual positions ---
    print("[RigImport] === POSITION COMPARISON (pass 1) ===")
    for obj, target_name, pos_confirmed in accepted_assignments:
        mesh_c = mesh_centers.get(obj)
        if mesh_c is None:
            mesh_c = _get_mesh_world_center(obj)
        exp_list = expected_loc_by_name.get(target_name.lower())
        if exp_list:
            best_dist = float('inf')
            best_exp_s = None
            for eloc in exp_list:
                es = eloc * estimated_rig_scale
                d = (mesh_c - es).length
                if d < best_dist:
                    best_dist = d
                    best_exp_s = es
            print(f"[RigImport]   {target_name:30s}  mesh=({mesh_c.x:+8.3f}, {mesh_c.y:+8.3f}, {mesh_c.z:+8.3f})  "
                  f"expected=({best_exp_s.x:+8.3f}, {best_exp_s.y:+8.3f}, {best_exp_s.z:+8.3f})  dist={best_dist:.4f}")
        else:
            print(
                f"[RigImport]   {target_name:30s}  mesh=({mesh_c.x:+8.3f}, {mesh_c.y:+8.3f}, {mesh_c.z:+8.3f})  expected=N/A")
    print("[RigImport] === END POSITION COMPARISON ===")

    meta_loaded["_fingerprint_object_map"] = fingerprint_object_map
    return renamed_count + skipped_count


def _get_mesh_world_center(obj):
    """Get the geometric center of a mesh in world space (from actual vertices)."""
    if obj.type != "MESH" or not obj.data.vertices:
        return obj.matrix_world.to_translation()

    # Calculate bounding box center in local space
    verts = obj.data.vertices
    min_co = [float('inf')] * 3
    max_co = [float('-inf')] * 3

    for v in verts:
        for i in range(3):
            min_co[i] = min(min_co[i], v.co[i])
            max_co[i] = max(max_co[i], v.co[i])

    # Local center
    local_center = [(min_co[i] + max_co[i]) / 2.0 for i in range(3)]

    # Transform to world space
    from mathutils import Vector
    world_center = obj.matrix_world @ Vector(local_center)
    return world_center


# Grid cell size for spatial hashing (in blender units).
# 0.1 keeps buckets small enough that the 27-neighbor query stays fast,
# but large enough to absorb typical OBJ precision loss.
_GRID_CELL = 0.1


def _grid_key(loc):
    """Integer grid cell for a world-space location."""
    from math import floor
    return (
        floor(loc.x / _GRID_CELL),
        floor(loc.y / _GRID_CELL),
        floor(loc.z / _GRID_CELL),
    )


class _SpatialHash:
    """Simple 3D spatial hash for O(1)-amortized nearest-neighbor queries."""

    def __init__(self):
        self._buckets: dict[tuple, list] = {}

    def insert(self, obj, loc):
        key = _grid_key(loc)
        self._buckets.setdefault(key, []).append((obj, loc))

    def query_nearest(self, target_loc, exclude, max_distance=0.5):
        """Return (obj, dist) for the nearest non-excluded object, or (None, inf).

        Searches the 27 neighboring cells (3³) around the target, which
        guarantees finding anything within one cell width. If max_distance
        exceeds the cell size we also check an expanded shell.
        """
        cx, cy, cz = _grid_key(target_loc)
        # How many extra rings of cells to check beyond the immediate 27
        extra = max(0, int(max_distance / _GRID_CELL))
        r = 1 + extra

        best_obj = None
        best_dist = max_distance

        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    bucket = self._buckets.get((cx + dx, cy + dy, cz + dz))
                    if not bucket:
                        continue
                    for obj, loc in bucket:
                        if obj in exclude:
                            continue
                        dist = (loc - target_loc).length
                        if dist < best_dist:
                            best_dist = dist
                            best_obj = obj

        return best_obj, best_dist


def _rename_parts_by_fingerprint(rig_def, parts_collection, renamed_via_fingerprint=0,
                                 fingerprint_object_map=None, scale_factor=1.0, meta_loaded=None):
    """Rename meshes using transform position matching from rig metadata.

    Uses name matching first, then spatial-hash position lookup.
    Size data from partAux (when available) gates position matches so tiny
    meshes aren't grabbed by distant bones.
    """
    if not rig_def:
        print("[RigImport] No rig definition provided")
        return False

    allow_aux_renames = not bool(meta_loaded and meta_loaded.get("partAux"))
    if not allow_aux_renames:
        print("[RigImport] partAux present - skipping aux-name rename targets")

    t2b = get_transform_to_blender()
    used = set()

    # Build a set of all bone/part names in the rig definition (case-insensitive)
    all_rig_names = set()

    def collect_names(node):
        if not node:
            return
        jname = node.get("jname") or node.get("pname") or ""
        if jname:
            all_rig_names.add(jname.lower())
        if allow_aux_renames:
            for aux_name in (node.get("aux") or []):
                if aux_name:
                    all_rig_names.add(aux_name.lower())
        for child in (node.get("children") or []):
            collect_names(child)
    collect_names(rig_def)
    print(f"[RigImport] Rig contains {len(all_rig_names)} named parts")

    mesh_objects = [obj for obj in parts_collection.objects if obj.type == "MESH"]
    print(f"[RigImport] Building spatial index for {len(mesh_objects)} mesh objects")

    # Parts already matched by size fingerprinting are authoritative —
    # don't let this pass reassign them.
    fp_matched_objs = set()
    fp_matched_names = set()  # base bone names (lowered) that are fully covered by fp
    if fingerprint_object_map:
        for obj_name, obj in fingerprint_object_map.items():
            fp_matched_objs.add(obj)
            # The object was renamed to the target bone name (possibly with .001 suffix)
            # so strip suffix to get the base bone name
            fp_matched_names.add(_strip_suffix(obj_name).lower())
        print(f"[RigImport] {len(fp_matched_objs)} parts locked from fingerprint pass")

    # Build name index for direct name matching (case-insensitive)
    known_target_names = set(all_rig_names)
    part_aux_raw = meta_loaded.get("partAux") if meta_loaded else None
    if part_aux_raw:
        pa_list = list(part_aux_raw.values()) if isinstance(part_aux_raw, dict) else part_aux_raw
        for item in (pa_list or []):
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    known_target_names.add(str(name).lower())

    name_index = {}
    for obj in mesh_objects:
        base_name = _resolve_imported_obj_name(obj.name, known_target_names)
        name_index.setdefault(base_name, []).append(obj)

    # Precompute geometric centers and build spatial hash
    mesh_centers = {}
    spatial = _SpatialHash()
    for obj in mesh_objects:
        center = _get_mesh_world_center(obj)
        mesh_centers[obj] = center
        spatial.insert(obj, center)

    # Build expected-size map from partAux so position matching can gate
    # on size compatibility — prevents tiny meshes from being grabbed by
    # distant or wrong-sized bones.
    expected_dims_by_name = {}  # target_name_lower -> sorted dims tuple
    synth_preferred_targets = set()
    wrap_layer_target_names = set()
    if meta_loaded:
        part_aux_raw = meta_loaded.get("partAux")
        if part_aux_raw:
            pa_list = list(part_aux_raw.values()) if isinstance(part_aux_raw, dict) else part_aux_raw
            for item in (pa_list or []):
                if not item or not isinstance(item, dict):
                    continue
                name = item.get("name", "")
                dims = item.get("dims_fp")
                if name and dims and len(dims) == 3:
                    sd = tuple(sorted([float(x) for x in dims]))
                    expected_dims_by_name[name.lower()] = sd
                mesh_class = item.get("mesh_class")
                if (
                    name
                    and item.get("mesh_id")
                    and mesh_class in (None, "", "MeshPart")
                    and item.get("wrap_target")
                ):
                    synth_preferred_targets.add(name.lower())
                if name and item.get("wrap_layer"):
                    wrap_layer_target_names.add(name.lower())
    if expected_dims_by_name:
        print(f"[RigImport] Loaded expected sizes for {len(expected_dims_by_name)} parts")

    wrap_layer_candidate_objs = {
        obj
        for obj in mesh_objects
        if _resolve_imported_obj_name(obj.name, known_target_names) in wrap_layer_target_names
    }

    # Precompute mesh sizes for size gating
    mesh_dims = {}  # obj -> sorted dims tuple
    for obj in mesh_objects:
        d = obj.dimensions
        mesh_dims[obj] = tuple(sorted([d.x, d.y, d.z]))

    # Precompute reserved-name exclusion: for each target name, which objects
    # are "reserved" (already named for a DIFFERENT rig bone)?
    # This replaces the per-query O(n) scan with an O(1) lookup.
    _obj_rig_name = {}  # obj -> lowered rig name it matches (if any)
    for obj in mesh_objects:
        base = _resolve_imported_obj_name(obj.name, known_target_names)
        if base in all_rig_names:
            _obj_rig_name[obj] = base

    reserved_by_name: dict[str, set] = {}  # target_lower -> set of excluded objs
    for target_lower in all_rig_names:
        excluded = set()
        for obj, obj_name in _obj_rig_name.items():
            if obj_name != target_lower:
                excluded.add(obj)
        reserved_by_name[target_lower] = excluded

    def match_by_name(target_name):
        candidates = name_index.get(target_name.lower(), [])
        available = [o for o in candidates if o not in used]
        return available[0] if available else None

    def match_by_position(cf, target_name, allow_reserved_override=False,
                          max_distance_override=None, extra_exclude=None):
        """Match by spatial-hash nearest-neighbor lookup with size-aware tolerance.

        Tolerance scales with the expected part size — tiny parts need to be
        very close to their expected position, large parts get more slack.
        Also rejects matches where mesh dims diverge wildly from expected.
        """
        if not cf:
            return None

        try:
            expected_loc = (t2b @ cf_to_mat(cf)).to_translation()
            if scale_factor and scale_factor != 1.0:
                expected_loc = expected_loc * scale_factor
        except Exception as e:
            print(f"[RigImport]   '{target_name}' Failed to convert CFrame: {e}")
            return None

        # Build exclude set: already-used + fingerprint-locked + reserved names
        exclude = set(used) | fp_matched_objs
        if not allow_reserved_override:
            exclude.update(reserved_by_name.get(target_name.lower(), frozenset()))
        if extra_exclude:
            exclude.update(extra_exclude)

        # Adaptive tolerance: scale by expected part size.
        # A part 2 studs across can be 0.5 units away; a part 0.01 studs across
        # should be within ~0.05 units. Cap at 2.0 to avoid huge tolerances.
        target_lower = target_name.lower()
        exp_dims = expected_dims_by_name.get(target_lower)
        if exp_dims:
            exp_size = max(exp_dims) * (scale_factor if scale_factor else 1.0)
            pos_tolerance = min(2.0, max(0.05, min(0.5, exp_size * 0.5)) *
                                max(1.0, scale_factor if scale_factor else 1.0))
        else:
            pos_tolerance = min(2.0, max(0.5, 0.5 * scale_factor) if scale_factor else 0.5)

        query_max_distance = max_distance_override if max_distance_override is not None else pos_tolerance
        best, dist = spatial.query_nearest(expected_loc, exclude, max_distance=query_max_distance)
        if best:
            # Size gate: reject if mesh dims are wildly incompatible with expected
            if exp_dims:
                m_dims = mesh_dims.get(best)
                if m_dims:
                    exp_sig = sum(exp_dims) * (scale_factor if scale_factor else 1.0)
                    mesh_sig = sum(m_dims)
                    if exp_sig > 1e-6 and mesh_sig > 1e-6:
                        ratio = mesh_sig / exp_sig
                        if ratio < 0.3 or ratio > 3.0:
                            print(
                                f"[RigImport]   '{target_name}' REJECTED '{best.name}' (size ratio={ratio:.2f}, dist={dist:.4f})")
                            return None

            print(
                f"[RigImport]   '{target_name}' MATCHED (dist={dist:.4f}, tol={query_max_distance:.3f}) -> '{best.name}'")
            return best

        print(
            f"[RigImport]   '{target_name}' NO POSITION MATCH at ({expected_loc.x:.4f}, {expected_loc.y:.4f}, {expected_loc.z:.4f}) tol={query_max_distance:.3f}")
        return None

    def match_wrap_target_by_strong_position(cf, target_name):
        target_lower = target_name.lower()
        exp_dims = expected_dims_by_name.get(target_lower)
        if exp_dims:
            exp_size = max(exp_dims) * (scale_factor if scale_factor else 1.0)
            strong_tolerance = max(0.03, min(0.16, exp_size * 0.12))
        else:
            strong_tolerance = 0.08
        return match_by_position(
            cf,
            target_name,
            allow_reserved_override=False,
            max_distance_override=strong_tolerance,
            extra_exclude=wrap_layer_candidate_objs,
        )

    # Pre-mark fingerprint-matched objects as used so they don't get stolen
    for tname, obj in (fingerprint_object_map or {}).items():
        used.add(obj)

    matched_count = 0
    locked_match_count = 0
    unmatched_names = []
    pending_renames = []  # List of (obj, target_name)
    matched_objects = []  # List of (obj, target_name)

    # Collect all nodes that need matching (excluding root)
    nodes_to_match = []  # List of (jname, transform, is_aux)

    def collect_nodes(node, depth=0):
        """First pass: collect all bone/part names and their transforms."""
        jname = node.get("jname") or node.get("pname") or ""
        children = node.get("children") or []
        node_transform = node.get("transform")
        aux_transforms = node.get("auxTransform") or []
        aux_names = node.get("aux") or []

        is_root = (depth == 0)

        if jname and not is_root:
            nodes_to_match.append((jname, node_transform, False))

        if allow_aux_renames and not is_root:
            for idx, aux_name in enumerate(aux_names):
                if aux_name:
                    cf = aux_transforms[idx] if idx < len(aux_transforms) else None
                    nodes_to_match.append((aux_name, cf, True))

        for child in children:
            collect_nodes(child, depth + 1)

    collect_nodes(rig_def)
    print(f"[RigImport] Collected {len(nodes_to_match)} nodes to match")

    if synth_preferred_targets:
        print(f"[RigImport] Metadata-only hidden wrap targets detected for {len(synth_preferred_targets)} body parts")

    # Check if meshes already have names matching the rig bones
    # If so, use name-based matching. If not, use position-based matching.
    meshes_with_rig_names = 0
    # for obj in mesh_objects:
    #     base_name = _strip_suffix(obj.name).lower()
    #     if base_name in all_rig_names:
    #         meshes_with_rig_names += 1

    # Force use of rename map if fingerprints were used
    # If we successfully renamed parts via fingerprints, we should trust those names
    if renamed_via_fingerprint > 0:
        use_name_matching = True
        print("[RigImport] Fingerprinting successful - running NAME matching on corrected parts")
    else:
        for obj in mesh_objects:
            base_name = _resolve_imported_obj_name(obj.name, known_target_names)
            if base_name in all_rig_names:
                meshes_with_rig_names += 1
        use_name_matching = meshes_with_rig_names > 0
        print(
            f"[RigImport] Found {meshes_with_rig_names} meshes with rig bone names - using {'NAME' if use_name_matching else 'POSITION'} matching")

    for target_name, transform, is_aux in nodes_to_match:
        # Skip parts already locked by fingerprint pass
        target_lower = target_name.lower()
        if target_lower in fp_matched_names:
            locked_match_count += 1
            continue

        obj = None
        prefix = "AUX " if is_aux else ""
        is_hidden_wrap_target = target_lower in synth_preferred_targets and not is_aux

        if use_name_matching:
            # Use name matching
            obj = match_by_name(target_name)
            if obj:
                print(f"[RigImport] {prefix}'{target_name}' matched by NAME -> '{obj.name}'")
            elif transform and not is_hidden_wrap_target:
                obj = match_by_position(transform, target_name, allow_reserved_override=True)
                if obj:
                    print(f"[RigImport] {prefix}'{target_name}' matched by POSITION -> '{obj.name}'")
            elif transform and is_hidden_wrap_target:
                # Two-tier: try tight tolerance first, then normal tolerance.
                # Truly absent parts (no nearby mesh) fail both tiers.
                obj = match_wrap_target_by_strong_position(transform, target_name)
                if obj:
                    print(f"[RigImport] {prefix}'{target_name}' matched by STRONG POSITION -> '{obj.name}'")
                else:
                    obj = match_by_position(
                        transform,
                        target_name,
                        allow_reserved_override=True,
                        extra_exclude=wrap_layer_candidate_objs,
                    )
                    if obj:
                        print(
                            f"[RigImport] {prefix}'{target_name}' matched by POSITION (wrap target fallback) -> '{obj.name}'")
        else:
            # Use position matching
            if transform and not is_hidden_wrap_target:
                obj = match_by_position(transform, target_name)
                if obj:
                    print(f"[RigImport] {prefix}'{target_name}' matched by POSITION -> '{obj.name}'")
            elif transform and is_hidden_wrap_target:
                obj = match_wrap_target_by_strong_position(transform, target_name)
                if obj:
                    print(f"[RigImport] {prefix}'{target_name}' matched by STRONG POSITION -> '{obj.name}'")
                else:
                    obj = match_by_position(
                        transform,
                        target_name,
                        extra_exclude=wrap_layer_candidate_objs,
                    )
                    if obj:
                        print(
                            f"[RigImport] {prefix}'{target_name}' matched by POSITION (wrap target fallback) -> '{obj.name}'")

        if obj is None and transform and is_hidden_wrap_target:
            print(f"[RigImport] {prefix}'{target_name}' no mesh nearby -> keep hidden wrap target absent")

        if obj:
            current_base = _strip_suffix(obj.name)
            if current_base != target_name:
                pending_renames.append((obj, target_name))
                matched_count += 1
                matched_objects.append((obj, target_name))
            else:
                matched_objects.append((obj, current_base))
            used.add(obj)
        else:
            unmatched_names.append(target_name)

    # Two-pass rename to avoid name collisions (e.g., Handle2->Handle1 when Handle1 exists)
    # Pass 1: Rename all to temporary unique names
    print(f"[RigImport] Applying {len(pending_renames)} renames (two-pass to avoid collisions)")
    temp_names = []
    for i, (obj, _) in enumerate(pending_renames):
        temp_name = f"__rbxtemp_{i}__"
        temp_names.append((obj, temp_name))
        obj.name = temp_name

    # Pass 2: Rename to final target names
    for i, (obj, target_name) in enumerate(pending_renames):
        print(f"[RigImport]   RENAME: '{temp_names[i][1]}' -> '{target_name}'")
        obj.name = target_name

    print("[RigImport] " + "=" * 50)
    print(
        f"[RigImport] SUMMARY: {matched_count} parts renamed, {locked_match_count} prelocked, {len(unmatched_names)} unmatched")
    if unmatched_names:
        print(f"[RigImport] Unmatched parts: {unmatched_names}")

    # --- axis debug: compare expected vs actual positions (pass 2) ---
    print("[RigImport] === POSITION COMPARISON (pass 2) ===")
    for target_name, transform, is_aux in nodes_to_match:
        if not transform:
            continue
        try:
            exp = (t2b @ cf_to_mat(transform)).to_translation()
            if scale_factor and scale_factor != 1.0:
                exp = exp * scale_factor
        except Exception:
            continue
        # find the mesh currently named target_name (or target_name.NNN)
        mesh_obj = None
        tl = target_name.lower()
        for obj in mesh_objects:
            if _strip_suffix(obj.name).lower() == tl:
                mesh_obj = obj
                break
        if mesh_obj:
            mc = _get_mesh_world_center(mesh_obj)
            dist = (mc - exp).length
            tag = "FP-LOCKED" if tl in fp_matched_names else "pass2"
            print(f"[RigImport]   [{tag}] {target_name:30s}  mesh=({mc.x:+8.3f}, {mc.y:+8.3f}, {mc.z:+8.3f})  "
                  f"expected=({exp.x:+8.3f}, {exp.y:+8.3f}, {exp.z:+8.3f})  dist={dist:.4f}")
        else:
            print(f"[RigImport]   [MISSING] {target_name:30s}  expected=({exp.x:+8.3f}, {exp.y:+8.3f}, {exp.z:+8.3f})")
    print("[RigImport] === END POSITION COMPARISON ===")

    print("[RigImport] " + "=" * 50)

    if meta_loaded is not None:
        updated_fp_map = dict(fingerprint_object_map or {})
        for obj, _target_name in matched_objects:
            stale_keys = [key for key, mapped_obj in updated_fp_map.items() if mapped_obj is obj and key != obj.name]
            for stale_key in stale_keys:
                updated_fp_map.pop(stale_key, None)
            updated_fp_map[obj.name] = obj
        meta_loaded["_fingerprint_object_map"] = updated_fp_map

    return bool(matched_objects or locked_match_count)


def _parts_list_from_rig_def(rig_def):
    """Derive a parts list from rig metadata in depth-first traversal order.

    Must match the order that lua's GetDescendants() produces, which is
    depth-first. Alphabetical sorting would mismatch the p<N>x indices.
    """
    if not rig_def:
        return []
    parts = []
    seen = set()

    def walk(node):
        if not node:
            return
        local_pname = node.get("pname") or node.get("jname")
        if local_pname and local_pname not in seen:
            parts.append(local_pname)
            seen.add(local_pname)

        aux = node.get("aux") or []
        for aux_name in aux:
            if aux_name and aux_name not in seen:
                parts.append(aux_name)
                seen.add(aux_name)

        for child in node.get("children") or []:
            walk(child)

    walk(rig_def)

    return parts


def _rename_indexed_parts(meta_loaded, parts_collection):
    """Rename OBJ-exported meshes from indexed placeholders to real part names.

    Tries two naming schemes:
    1. New unambiguous: 'p<N>x' (+ optional OBJ group suffix '1' + optional dedup '.001')
    2. Legacy: '<rigName><N>' (+ optional OBJ suffix '1' + optional dedup '.001')
    """
    parts_list = None

    # Best source: partAux has authoritative idx→name mapping from the export.
    # This is always correct regardless of how 'parts' is serialized.
    part_aux_raw = meta_loaded.get("partAux")
    if part_aux_raw:
        if isinstance(part_aux_raw, dict):
            aux_items = list(part_aux_raw.values())
        else:
            aux_items = list(part_aux_raw)
        # sort by idx to get correct export order
        aux_with_idx = []
        for item in aux_items:
            if isinstance(item, dict) and "idx" in item and "name" in item:
                aux_with_idx.append((int(item["idx"]), item["name"]))
        if aux_with_idx:
            aux_with_idx.sort(key=lambda t: t[0])
            parts_list = [name for _, name in aux_with_idx]

    # Fallback: try the 'parts' payload directly
    if not parts_list:
        parts_payload = meta_loaded.get("parts")
        if isinstance(parts_payload, list):
            parts_list = parts_payload
        elif isinstance(parts_payload, dict):
            first_key = next(iter(parts_payload), "")
            if first_key.isdigit():
                parts_list = [parts_payload[k] for k in sorted(parts_payload.keys(), key=lambda k: int(k))]

    # Last resort: derive from rig tree (may not match GetDescendants order)
    if not parts_list:
        parts_list = _parts_list_from_rig_def(meta_loaded.get("rig"))

    if not parts_list:
        return False, "Missing 'parts' in rig metadata"

    print(f"[RigImport] parts_list ({len(parts_list)} entries):")
    for i, name in enumerate(parts_list):
        print(f"[RigImport]   [{i + 1}] = {name!r}")

    # New unambiguous pattern: p<N>x (with optional OBJ group suffix "1" and dedup suffix)
    # This is the ONLY pattern we trust for indexed rename, because both the
    # naming (p<N>x) and the partAux idx are assigned by the same partCount
    # in the same GetDescendants loop on the lua side.
    #
    # Legacy <rigName><N> patterns are NOT used — Roblox's OBJ exporter
    # assigns its own index order which doesn't match GetDescendants order,
    # so the mapping would be wrong. Those rigs fall through to fingerprint
    # matching instead.
    new_pattern = re.compile(r"^p(\d+)x1?(\.\d+)?$", re.IGNORECASE)
    new_indexed = [
        obj for obj in parts_collection.objects
        if obj.type == "MESH" and new_pattern.match(obj.name)
    ]
    if new_indexed:
        _autoname_from_pattern(parts_list, new_pattern, new_indexed)
        return True, None

    return False, None


def _parts_already_named(parts_list, parts_collection):
    if not isinstance(parts_list, list) or not parts_list:
        return False

    mesh_names = {obj.name for obj in parts_collection.objects if obj.type == "MESH"}
    expected_names = {name for name in parts_list if isinstance(name, str) and name}
    if not expected_names:
        return False

    return expected_names.issubset(mesh_names)


def _autoname_from_pattern(partnames, pattern, objects_to_rename):
    """Rename objects whose names match `pattern` (group 1 = index) to partnames[index-1].

    Uses two-pass temp-name approach to avoid blender's auto-suffixing
    when a target name already exists as another object's name.
    """

    pending = []
    print(f"[RigImport] _autoname mapping ({len(objects_to_rename)} meshes):")
    for obj in objects_to_rename:
        match = pattern.match(obj.name)
        if match:
            try:
                index = int(match.group(1))
                if 0 < index <= len(partnames):
                    target = partnames[index - 1]
                    print(f"[RigImport]   '{obj.name}' → idx={index} → '{target}'")
                    pending.append((obj, target))
                else:
                    print(
                        f"Warning: Index {index} out of range for partnames list (length: {len(partnames)})"
                    )
            except Exception as e:
                print(f"Error renaming part {obj.name}: {str(e)}")

    # Pass 1: temp names to clear the namespace
    for i, (obj, _) in enumerate(pending):
        obj.name = f"__rbxidx_{i}__"
    # Pass 2: final names
    for obj, target_name in pending:
        obj.name = target_name

    # --- axis debug: compare expected vs actual positions (indexed import) ---
    print("[RigImport] === POSITION COMPARISON (indexed import) ===")
    for obj, target_name in pending:
        mesh_c = _get_mesh_world_center(obj)
        print(f"[RigImport]   {target_name:30s}  mesh=({mesh_c.x:+8.3f}, {mesh_c.y:+8.3f}, {mesh_c.z:+8.3f})")
    print("[RigImport] === END POSITION COMPARISON ===")


# ---------------------------------------------------------------------------
# weapon import confirmation popup
# ---------------------------------------------------------------------------

# stash dict for passing data between ImportModel and the confirm dialog
_pending_weapon_import: Dict[str, Any] = {}
# Bump when the pending payload shape changes; Apply validates against it.
_WEAPON_IMPORT_PAYLOAD_VERSION = 1


class _Reporter:
    """Lightweight proxy so _import_weapon can call self.report()
    without holding a reference to the (now-dead) ImportModel instance."""

    def __init__(self, report_fn):
        self.report = report_fn


def _weapon_target_rig_items(self, context):
    """Enum items callback listing all armatures in the scene.
    The currently-selected armature (from settings) is first."""
    from ..core.utils import get_cached_armatures
    items = []
    settings = getattr(context.scene, "rbx_anim_settings", None)
    current = settings.rbx_anim_armature if settings else ""
    seen = set()
    # put current rig first so it's the default
    if current:
        items.append((current, current, "Currently active rig"))
        seen.add(current)
    for name in get_cached_armatures():
        if name not in seen:
            items.append((name, name, ""))
            seen.add(name)
    if not items:
        items.append(("NONE", "(no armatures)", ""))
    return items


class OBJECT_OT_ConfirmWeaponTarget(bpy.types.Operator):
    bl_idname = "object.rbxanims_confirm_weapon_target"
    bl_label = "Import Weapon"
    bl_description = "Confirm target rig for weapon import"
    bl_options = {"REGISTER", "INTERNAL"}

    target_rig: bpy.props.EnumProperty(
        name="Target Rig",
        description="Armature to attach the weapon to",
        items=_weapon_target_rig_items,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=350)

    @staticmethod
    def _find_source_armature(armature):
        """Detect if this armature is a proxy/control rig by scanning for
        Copy Transform/Location/Rotation constraints pointing to another armature.
        Returns (source_armature, constraint_map) or (None, {}).
        constraint_map: {bone_name: (target_armature, subtarget_bone)} for bones
        that have copy constraints."""
        return find_constraint_driven_armature(armature)

    def _get_suggested_bones(self):
        data = _pending_weapon_import.get("data")
        if not data:
            return []
        return _collect_weapon_suggested_bones(data["meta_loaded"])

    def _find_bone_case_insensitive(self, armature, suggested):
        return _find_bone_case_insensitive(armature, suggested)

    def _check_bone_matches(self, context):
        """Return (armature, matches) where matches is:
        [{"suggested": str, "found": str|None, "source_arm": Object|None}]"""
        suggested_bones = self._get_suggested_bones()
        armature = None
        if self.target_rig and self.target_rig != "NONE":
            armature = get_object_by_name(self.target_rig, context.scene)
        if not armature or armature.type != "ARMATURE":
            return armature, [{"suggested": s, "found": None, "source_arm": None} for s in suggested_bones]

        source_arm, _ = self._find_source_armature(armature)
        matches = []
        for suggested in suggested_bones:
            found = self._find_bone_case_insensitive(armature, suggested)
            if found:
                matches.append({"suggested": suggested, "found": found, "source_arm": None})
                continue
            source_found = self._find_bone_case_insensitive(source_arm, suggested) if source_arm else None
            if source_found:
                matches.append({"suggested": suggested, "found": source_found, "source_arm": source_arm})
            else:
                matches.append({"suggested": suggested, "found": None, "source_arm": None})
        return armature, matches

    def draw(self, context):
        layout = self.layout
        weapon_name = _pending_weapon_import.get("weapon_name", "Weapon")
        layout.label(text=f"Importing: {weapon_name}", icon="OBJECT_DATA")
        layout.separator()
        layout.prop(self, "target_rig", icon="ARMATURE_DATA")

        armature, matches = self._check_bone_matches(context)
        if matches:
            any_source = any(m["source_arm"] for m in matches)
            if any_source:
                src = next((m["source_arm"] for m in matches if m["source_arm"]), None)
                box = layout.box()
                col = box.column(align=True)
                col.label(text="Proxy rig detected", icon="INFO")
                if src:
                    col.label(text=f"Source rig: {src.name}")
                col.label(text="Weapon bones will be created on the source rig")
                col.label(text="with copy constraints mirrored to the proxy")

            box = layout.box()
            col = box.column(align=True)
            col.label(text=f"Suggested attachment bones ({len(matches)}):", icon="BONE_DATA")
            for m in matches:
                suggested = m["suggested"]
                found = m["found"]
                source_arm = m["source_arm"]
                if found and not source_arm:
                    col.label(text=f"{suggested} -> {found}", icon="CHECKMARK")
                elif found and source_arm:
                    col.label(text=f"{suggested} -> {found} (source rig)", icon="INFO")
                else:
                    row = col.row()
                    row.alert = True
                    row.label(text=f"{suggested} (missing)", icon="ERROR")

            missing = [m for m in matches if not m["found"]]
            if missing and armature:
                col.separator()
                warn = col.row()
                warn.alert = True
                warn.label(text="Some suggested bones were not found on this rig", icon="ERROR")
                bone_names = [b.name for b in armature.data.bones]
                if bone_names:
                    preview = bone_names[:8]
                    col.alert = False
                    col.label(text=f"Available bones ({len(bone_names)}):", icon="BONE_DATA")
                    for bn in preview:
                        col.label(text=f"  - {bn}")
                    if len(bone_names) > 8:
                        col.label(text=f"  ... and {len(bone_names) - 8} more")

    def execute(self, context):
        armature, matches = self._check_bone_matches(context)
        if armature and matches:
            missing = [m["suggested"] for m in matches if not m["found"]]
            if missing:
                self.report(
                    {"ERROR"},
                    f"Missing suggested bone(s) on \"{self.target_rig}\": {', '.join(missing)}. Pick a different rig."
                )
                return {"CANCELLED"}

        return bpy.ops.object.rbxanims_apply_weapon_import(target_rig=self.target_rig)


class OBJECT_OT_ApplyWeaponImport(bpy.types.Operator):
    bl_idname = "object.rbxanims_apply_weapon_import"
    bl_label = "Apply Weapon Import"
    bl_description = "Apply the selected target rig for weapon import"
    bl_options = {"REGISTER", "INTERNAL", "UNDO"}

    target_rig: bpy.props.StringProperty(name="Target Rig", default="NONE")

    def execute(self, context):
        # Undo safety: normalize to object mode before any ID/collection edits.
        try:
            from ..rig.creation import _safe_mode_set
            _safe_mode_set("OBJECT")
        except Exception:
            pass

        data = _pending_weapon_import.pop("data", None)
        pending_mode = _pending_weapon_import.pop("mode", None)
        if not data:
            self.report({"ERROR"}, "No pending weapon import data")
            return {"CANCELLED"}
        if data.get("schema") != _WEAPON_IMPORT_PAYLOAD_VERSION:
            self.report(
                {"ERROR"},
                "Pending weapon import payload is stale; re-import the weapon.",
            )
            return {"CANCELLED"}

        meta_loaded = data["meta_loaded"]
        # re-fetch objects by name — the live refs stored earlier are
        # potentially stale bc they crossed an operator / undo boundary.
        rig_part_obj_names = data.get("rig_part_obj_names", [])
        rig_part_objs = [
            bpy.data.objects[n] for n in rig_part_obj_names
            if n in bpy.data.objects
        ]
        if len(rig_part_objs) != len(rig_part_obj_names):
            # A rename between stash and apply lost the name match.  rbxm
            # imports carry file-scoped RBXInstRefs on every mesh, which are
            # rename-proof: resolve the stragglers by referent instead of
            # silently dropping them.
            refs = {
                int(entry["inst_ref"])
                for entry in (meta_loaded.get("partAux") or [])
                if isinstance(entry, dict) and isinstance(entry.get("inst_ref"), int)
            }
            for obj in bpy.data.objects:
                ref = obj.get("RBXInstRef")
                if isinstance(ref, int) and ref in refs and obj not in rig_part_objs:
                    rig_part_objs.append(obj)

        # detect proxy rig — if so, import onto the source armature
        actual_rig_name = self.target_rig
        proxy_armature = None
        if actual_rig_name and actual_rig_name != "NONE":
            selected_arm = get_object_by_name(actual_rig_name, context.scene)
            if selected_arm and selected_arm.type == "ARMATURE":
                source_arm, constraint_map = OBJECT_OT_ConfirmWeaponTarget._find_source_armature(selected_arm)
                if source_arm and _should_redirect_weapon_import(selected_arm, source_arm, meta_loaded):
                    print(
                        f"[WeaponImport] Proxy rig redirect: {selected_arm.name} -> {source_arm.name} "
                        f"({len(constraint_map)} constrained bones)"
                    )
                    proxy_armature = selected_arm
                    actual_rig_name = source_arm.name
                elif source_arm:
                    print(
                        f"[WeaponImport] Keeping selected rig '{selected_arm.name}' "
                        "for import (suggested bones resolved on selected rig)"
                    )

        # override the armature setting so _import_weapon picks it up
        settings = getattr(context.scene, "rbx_anim_settings", None)
        old_arm = settings.rbx_anim_armature if settings else None
        if settings and actual_rig_name != "NONE":
            settings.rbx_anim_armature = actual_rig_name

        # use a lightweight proxy so _import_weapon can call self.report()
        # (the original ImportModel instance is already dead)
        proxy = _Reporter(self.report)

        # bl_options already includes 'UNDO', so blender handles the undo
        # step automatically.  do NOT call undo_push manually — doubling up
        # corrupts the undo stack and causes a build_materials crash on
        # ctrl+z (null material pointer after partial undo restore).

        try:
            if pending_mode == "rbxm":
                # rbxm weapon exports carry grip attributes instead of the
                # OBJ weapon schema — attach through the grip-based path.
                result = self._apply_rbxm_weapon(context, meta_loaded, rig_part_objs)
            else:
                # call as unbound method — proxy duck-types as `self`
                result = OBJECT_OT_ImportModel._import_weapon(proxy, context, meta_loaded, rig_part_objs)

            # if proxy rig detected, clone weapon bones onto the proxy
            # with copy constraints mirroring the source
            if result == {"FINISHED"} and proxy_armature and pending_mode != "rbxm":
                source_arm_obj = get_object_by_name(actual_rig_name, context.scene)
                if source_arm_obj:
                    OBJECT_OT_ApplyWeaponImport._clone_weapon_bones_to_proxy(
                        context, source_arm_obj, proxy_armature, meta_loaded
                    )
        finally:
            # restore original setting
            if settings and old_arm is not None:
                settings.rbx_anim_armature = old_arm

        return result

    def _apply_rbxm_weapon(self, context, meta_loaded, rig_part_objs):
        """Attach an rbxm-imported weapon to the confirmed rig.

        The Studio plugin stamps grip metadata as attributes on the weapon
        clone (weaponGrip entries: root part, rig bone, joint C0/C1).  Each
        grip's part subtree attaches to its rig bone through the shared
        attach operator (weapon bone + CHILD_OF constraints), keeping the
        weapon's imported world placement relative to the bone.
        """
        settings = getattr(context.scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        armature = get_object_by_name(arm_name, context.scene) if arm_name else None
        if not armature or armature.type != "ARMATURE":
            self.report({"ERROR"}, "No target armature available for weapon attach.")
            return {"CANCELLED"}

        meshes = [obj for obj in rig_part_objs if obj and obj.type == "MESH"]
        if not meshes:
            self.report({"WARNING"}, "No weapon meshes found after import.")
            return {"CANCELLED"}

        grips = [g for g in (meta_loaded.get("weaponGrip") or []) if isinstance(g, dict)]
        if not grips:
            self.report({"ERROR"}, "Weapon has no grip metadata to attach.")
            return {"CANCELLED"}

        weapon_name = meta_loaded.get("rigName") or "Weapon"
        part_aux = meta_loaded.get("partAux") or []

        # Objects carry their parse index (RBXPartIdx); partAux entries link
        # to their parent through the instance parent referent.
        obj_by_idx = {}
        for obj in meshes:
            idx = obj.get("RBXPartIdx")
            if isinstance(idx, int):
                obj_by_idx[idx] = obj
        entry_by_ref = {}
        entry_by_name = {}
        for entry in part_aux:
            if not isinstance(entry, dict):
                continue
            ref = entry.get("inst_ref")
            if isinstance(ref, int):
                entry_by_ref[ref] = entry
            name = entry.get("name")
            if isinstance(name, str):
                entry_by_name.setdefault(name.lower(), entry)

        def subtree_idxs(root_entry):
            found = set()
            stack = [root_entry]
            while stack:
                entry = stack.pop()
                idx = entry.get("idx")
                if isinstance(idx, int):
                    found.add(idx)
                ref = entry.get("inst_ref")
                if not isinstance(ref, int):
                    continue
                for child in part_aux:
                    if isinstance(child, dict) and child.get("parent_ref") == ref:
                        stack.append(child)
            return found

        groups = []
        for grip in grips:
            bone_name = _find_bone_case_insensitive(armature, grip.get("bone"))
            if not bone_name:
                self.report(
                    {"WARNING"},
                    f"Bone \"{grip.get('bone')}\" not found on \"{armature.name}\"; skipping grip.",
                )
                continue
            root_entry = entry_by_name.get((grip.get("root") or "").lower())
            group_meshes = []
            if root_entry is not None:
                group_meshes = [
                    obj_by_idx[i] for i in subtree_idxs(root_entry) if i in obj_by_idx
                ]
            groups.append({
                "bone": bone_name,
                "root": grip.get("root") or weapon_name,
                "joint_name": grip.get("jointName"),
                "joint_type": grip.get("jointType") or "Motor6D",
                "meshes": group_meshes,
                "root_entry": root_entry,
                "c0": _coerce_cf12(grip.get("connectionC0")),
                "c1": _coerce_cf12(grip.get("connectionC1")),
            })

        if not groups:
            return {"CANCELLED"}

        # Any mesh not covered by a resolved grip follows the first group.
        covered = set()
        for group in groups:
            covered.update(obj.name for obj in group["meshes"])
        if any(obj.name not in covered for obj in meshes):
            groups[0]["meshes"].extend(
                obj for obj in meshes if obj.name not in covered
            )

        # ---- Relocate each grip group to its equipped position ----
        # Roblox joint equation:  parent.CFrame * C0 = root.CFrame * C1
        #   equipped root = parent.CFrame * C0 * C1^-1
        # Shift the group by equipped @ current^-1 (full rigid transform) so
        # the weapon lands on the hand the same way the OBJ path does.
        from mathutils import Matrix
        from ..core.constants import get_transform_to_blender
        from ..core.utils import cf_to_mat, to_matrix, mat_to_cf, solve_equipped_joint_matrix

        t2b = get_transform_to_blender()

        def bone_roblox_world_mat(bone_name):
            bone = armature.data.bones.get(bone_name) if bone_name else None
            if bone is None:
                return None
            stored = bone.get("transform")
            if stored is not None:
                try:
                    return to_matrix(stored)
                except Exception:
                    pass
            head_world = armature.matrix_world @ bone.head
            return t2b.inverted() @ Matrix.Translation(head_world)

        for group in groups:
            root_entry = group.get("root_entry")
            root_cf = root_entry.get("part_cf") if isinstance(root_entry, dict) else None
            if not (isinstance(root_cf, (list, tuple)) and len(root_cf) >= 12):
                continue
            root_mat = cf_to_mat(root_cf)
            parent_mat = bone_roblox_world_mat(group["bone"])
            if parent_mat is None:
                continue
            c0 = group.get("c0")
            c1 = group.get("c1")
            if c0 is not None and c1 is not None:
                equipped = solve_equipped_joint_matrix(parent_mat, c0, c1)
            else:
                equipped = root_mat
            # Remember the equipped CFrame for the weapon bone: its `transform`
            # prop must be the relocated (equipped) Roblox CFrame so animation
            # serialization matches the OBJ weapon flow.
            group["equipped_cf"] = mat_to_cf(equipped)
            # Stash the relocation; mesh objects are moved only AFTER skin
            # binding — binding compares source filemesh positions against
            # the ORIGINAL mesh positions, so an early move breaks the
            # tight index bind and forces fuzzy position matching.
            group["relocation"] = equipped @ root_mat.inverted()

        from ..rig.creation import _safe_mode_set, create_joint_bone
        from ..rig.constraints import link_object_to_bone_rigid
        from ..core.utils import (
            find_master_collection_for_object,
            find_parts_collection_in_master,
        )

        _IDENTITY_CF12 = [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]

        # Hybrid weapons carry their own joint trees (Motor6D/Bone rigs)
        # alongside grip metadata.  Each internal rig hangs off the grip's
        # attachment bone so the weapon's own animation stays intact.
        weapon_rigs = [
            rig for rig in (meta_loaded.get("scene_rigs") or [])
            if isinstance(rig, dict) and isinstance(rig.get("rig"), dict)
        ]
        if not weapon_rigs:
            whole_rig = meta_loaded.get("rig")
            if isinstance(whole_rig, dict):
                weapon_rigs = [{
                    "rig": whole_rig,
                    "meshToBone": meta_loaded.get("meshToBone") or {},
                }]
        rig_by_root_name = {}
        for weapon_rig in weapon_rigs:
            root_jname = (weapon_rig.get("rig") or {}).get("jname")
            if root_jname:
                rig_by_root_name[str(root_jname).lower()] = weapon_rig

        _safe_mode_set("OBJECT")
        rig_parts_coll = find_parts_collection_in_master(
            find_master_collection_for_object(armature),
            create_if_missing=False,
        )
        # rbxm instance referents are FILE-scoped: the weapon's refs collide
        # with the rig's refs.  All weapon-side matching must run against the
        # weapon's own collection (where the meshes still live), never the
        # rig's Parts collection.
        weapon_coll = (
            meshes[0].users_collection[0]
            if meshes and meshes[0].users_collection
            else bpy.context.scene.collection
        )

        # ---- Phase 1: attachment bones.  Pure grip groups get a dedicated
        # joint bone; groups backed by an internal rig defer to phase 2,
        # where the rig's own hierarchy provides the root bone.
        for group in groups:
            group_rig = rig_by_root_name.get((group.get("root") or "").lower())
            if group_rig is not None:
                group["weapon_rig"] = group_rig
                continue
            group_meshes = [m for m in group["meshes"] if m and m.type == "MESH"]
            if not group_meshes:
                continue
            bone_label = group["root"] if len(groups) > 1 else weapon_name
            joint_label = group.get("joint_name")
            if isinstance(joint_label, str) and joint_label:
                bone_label = joint_label

            equipped_cf = group.get("equipped_cf")
            if equipped_cf is None:
                # No part CFrame was parsed — land the joint on the parent
                # bone's own transform instead of failing the attach.
                parent_mat = bone_roblox_world_mat(group["bone"])
                if parent_mat is None:
                    self.report(
                        {"WARNING"},
                        f"Grip '{group['root']}' has no position data; skipped.",
                    )
                    continue
                equipped_cf = mat_to_cf(parent_mat)

            # Shared joint-bone builder (mirrors load_rigbone's non-root
            # branch): head at the joint position (equipped * C1) with the
            # Motor6D props stamped for the animation serializer.  No
            # operator calls — plain functions only, so behavior can't drift
            # with UI polish.
            with _ensure_all_bone_collections_visible(armature):
                created_name = create_joint_bone(
                    armature,
                    group["bone"],
                    equipped_cf,
                    group.get("c0") or _IDENTITY_CF12,
                    group.get("c1") or _IDENTITY_CF12,
                    bone_label,
                    joint_type=group.get("joint_type") or "Motor6D",
                )
            if not created_name or not armature.data.bones.get(created_name):
                self.report(
                    {"WARNING"},
                    f"Failed to create weapon bone for grip '{group['root']}'.",
                )
                continue
            group["created_bone"] = created_name

        # ---- Phase 2: internal weapon rigs, through the same load_rigbone
        # flow the rig importer uses.  The rig root's joint transforms come
        # from the grip's C0/C1, so the root bone lands exactly on the
        # attachment while the rest of the weapon hierarchy stays intact.
        if weapon_rigs:
            from ..rig.creation import (
                load_rigbone,
                _collect_all_bone_names,
                _build_match_context,
            )

            match_ctx = _build_match_context(weapon_coll)
            context.view_layer.objects.active = armature
            armature.select_set(True)
            for group in groups:
                weapon_rig = group.get("weapon_rig")
                if weapon_rig is None:
                    continue
                rig_def = weapon_rig["rig"]
                parent_bone_name = (
                    group.get("created_bone")
                    or next(
                        (
                            g.get("created_bone")
                            for g in groups
                            if g.get("created_bone")
                        ),
                        None,
                    )
                    or group["bone"]
                )
                if "jointtransform0" not in rig_def:
                    rig_def["jointtransform0"] = group.get("c0") or _IDENTITY_CF12
                    rig_def["jointtransform1"] = group.get("c1") or _IDENTITY_CF12
                # Relocate the rig tree so bones rest at their EQUIPPED
                # positions (same semantics as the OBJ weapon path); the
                # mesh objects catch up after skin binding.
                relocation = group.get("relocation")
                if relocation is not None:

                    def _relocate_node(node, reloc):
                        tf = node.get("transform")
                        if tf and len(tf) >= 12:
                            new_cf = mat_to_cf(reloc @ cf_to_mat(tf))
                            for index in range(len(new_cf)):
                                tf[index] = new_cf[index]
                        for child in node.get("children") or []:
                            _relocate_node(child, reloc)

                    _relocate_node(rig_def, relocation)
                with _ensure_all_bone_collections_visible(armature):
                    entered = _safe_mode_set("EDIT", armature)
                    if not entered:
                        continue
                    parent_edit_bone = armature.data.edit_bones.get(parent_bone_name)
                    if parent_edit_bone is None:
                        _safe_mode_set("OBJECT", armature)
                        continue
                    all_bone_names = _collect_all_bone_names(rig_def)
                    try:
                        load_rigbone(
                            armature,
                            "RAW",
                            rig_def,
                            parent_edit_bone,
                            weapon_coll,
                            match_ctx,
                            all_bone_names,
                        )
                    except Exception as exc:
                        print(f"[RbxmWeapon] internal rig build failed: {exc}")
                _safe_mode_set("OBJECT", armature)
                root_bone_name = rig_def.get("jname")
                if root_bone_name and armature.data.bones.get(root_bone_name):
                    group["created_bone"] = root_bone_name

        # ---- Phase 3: bind meshes.  Skinned weapon meshes (Bone-authored
        # weights) bind like rig meshes; everything else gets a CHILD_OF to
        # its internal bone (or the group's attachment bone as fallback).
        skinned_bound = set()
        try:
            from ..rig.creation import (
                _prepare_skinned_mesh_bindings,
                _apply_skinned_mesh_bindings,
            )

            bindings = _prepare_skinned_mesh_bindings(meta_loaded, weapon_coll)
            if bindings:
                _apply_skinned_mesh_bindings(armature, bindings)
                skinned_bound = set(bindings.keys())
        except Exception as exc:
            print(f"[RbxmWeapon] skinned binding skipped: {exc}")

        # Move the meshes to their equipped positions AFTER skin binding so
        # the bind saw the original geometry.  Skinned meshes are already
        # parented to the armature: assigning matrix_world re-solves the
        # parent inverse, keeping the world transform exact.
        moved_count = 0
        for group in groups:
            relocation = group.get("relocation")
            if relocation is None:
                continue
            reloc_blender = t2b @ relocation @ t2b.inverted()
            for obj in group["meshes"]:
                if obj is not None and obj.type == "MESH":
                    obj.matrix_world = reloc_blender @ obj.matrix_world
                    moved_count += 1
        if moved_count:
            print(
                f"[RbxmWeapon] Relocated {moved_count} weapon mesh(es) "
                "to equipped position after skin binding"
            )

        name_to_ref = {}
        for entry in part_aux:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            ref = entry.get("inst_ref")
            if isinstance(name, str) and isinstance(ref, int):
                name_to_ref[name] = ref

        handled = set()
        for group in groups:
            group_meshes = [m for m in group["meshes"] if m and m.type == "MESH"]
            bone_name = group.get("created_bone")
            bone = armature.data.bones.get(bone_name) if bone_name else None
            if bone is None:
                continue
            weapon_rig = group.get("weapon_rig")
            if weapon_rig is not None:
                obj_by_ref = {}
                for obj in group_meshes:
                    ref = obj.get("RBXInstRef")
                    if isinstance(ref, int):
                        obj_by_ref[ref] = obj
                for part_name, mapped_bone in (weapon_rig.get("meshToBone") or {}).items():
                    ref = name_to_ref.get(part_name)
                    obj = obj_by_ref.get(ref) if ref is not None else None
                    if obj is None or obj in skinned_bound or obj in handled:
                        continue
                    mapped = armature.data.bones.get(mapped_bone)
                    if mapped is None:
                        continue
                    link_object_to_bone_rigid(obj, armature, mapped)
                    handled.add(obj)
            for obj in group_meshes:
                if obj in handled or obj in skinned_bound:
                    continue
                link_object_to_bone_rigid(obj, armature, bone)
                handled.add(obj)

        if rig_parts_coll:
            for obj in list(handled) + list(skinned_bound):
                for coll in list(obj.users_collection):
                    if coll != rig_parts_coll:
                        coll.objects.unlink(obj)
                if rig_parts_coll not in obj.users_collection:
                    rig_parts_coll.objects.link(obj)
                # The weapon's file-local referents would collide with the
                # rig's own refs in any later inst_ref lookup (rebuilds,
                # re-binds). Drop them once matching is done.
                for key in ("RBXInstRef", "RBXPartIdx"):
                    if key in obj:
                        del obj[key]

        attached = len(handled) + len(skinned_bound)
        if attached:
            print(
                f"[RbxmWeapon] Attached {attached}/{len(meshes)} weapon "
                f"mesh(es) to '{armature.name}'"
            )

        # The rbxm importer scaffolded a __<name>Meta object and a master
        # collection for this file; once every mesh is attached, that
        # scaffold is dead weight.
        if attached == len(meshes):
            try:
                meta_prefix = f"__{weapon_name}Meta"
                for obj in list(bpy.data.objects):
                    if (
                        obj.get("RigMeta") is not None
                        and (
                            obj.name == meta_prefix
                            or obj.name.startswith(meta_prefix + ".")
                        )
                    ):
                        scaffold_coll = find_master_collection_for_object(obj)
                        if scaffold_coll and not [
                            o for o in scaffold_coll.all_objects if o != obj
                        ]:
                            bpy.data.objects.remove(obj, do_unlink=True)
                            for child in list(scaffold_coll.children):
                                bpy.data.collections.remove(child)
                            bpy.data.collections.remove(scaffold_coll)
                        break
            except Exception as exc:
                print(f"[RbxmWeapon] scaffold cleanup skipped: {exc}")

        if attached:
            self.report(
                {"INFO"},
                f"Attached weapon '{weapon_name}' ({attached} mesh(es)) to \"{armature.name}\".",
            )
        return {"FINISHED"}

    @staticmethod
    def _clone_weapon_bones_to_proxy(context, source_armature, proxy_armature, meta_loaded):
        """Create matching weapon bones on the proxy armature with COPY_TRANSFORMS
        constraints pointing back to the source armature's weapon bones."""
        from ..rig.creation import _safe_mode_set

        def _ensure_object_in_view_layer(ctx, obj):
            """Ensure object is available in current window view layer.

            Returns True if available (or switched to a view layer that has it),
            else False.
            """
            if not obj:
                return False
            try:
                if ctx.view_layer.objects.get(obj.name) == obj:
                    return True
            except Exception:
                pass

            scene = getattr(ctx, "scene", None)
            win = getattr(ctx, "window", None)
            if scene is None:
                return False

            for vl in scene.view_layers:
                try:
                    if vl.objects.get(obj.name) == obj:
                        if win is not None:
                            try:
                                win.view_layer = vl
                            except Exception:
                                pass
                        return True
                except Exception:
                    continue
            return False

        # find weapon bones on source (they were just created by _import_weapon)
        # weapon bones are parented under the suggested bone
        suggested = meta_loaded.get("suggestedBone", "")
        suggested_bones = set()
        if suggested:
            suggested_bones.add(suggested.lower())
        attachments = meta_loaded.get("weaponAttachments")
        if isinstance(attachments, list):
            for att in attachments:
                if isinstance(att, dict):
                    sb = att.get("suggestedBone")
                    if isinstance(sb, str) and sb:
                        suggested_bones.add(sb.lower())
        weapon_bones = []
        for bone in source_armature.data.bones:
            # weapon bones are typically named after the weapon parts
            # and are children (direct or indirect) of the suggested bone
            parent = bone.parent
            while parent:
                if parent.name.lower() in suggested_bones:
                    weapon_bones.append(bone.name)
                    break
                parent = parent.parent

        if not weapon_bones:
            print(
                f"[WeaponImport] No weapon bones found under suggested roots {sorted(suggested_bones)} to clone to proxy")
            return

        print(
            f"[WeaponImport] Cloning {len(weapon_bones)} weapon bones to proxy '{proxy_armature.name}': {weapon_bones}")

        if not _ensure_object_in_view_layer(context, source_armature):
            print(
                f"[WeaponImport] Cannot clone weapon bones: source armature "
                f"'{source_armature.name}' is not in any accessible view layer."
            )
            return
        if not _ensure_object_in_view_layer(context, proxy_armature):
            print(
                f"[WeaponImport] Skipping proxy clone: armature "
                f"'{proxy_armature.name}' is not in any accessible view layer."
            )
            return

        # collect bone data from SOURCE in edit mode (only way to get real roll)
        context.view_layer.objects.active = source_armature
        source_armature.select_set(True)
        with _ensure_all_bone_collections_visible(source_armature):
            _safe_mode_set("EDIT", source_armature)

            bone_data = {}  # name -> {head, tail, roll, parent_name}
            for bone_name in weapon_bones:
                eb = source_armature.data.edit_bones.get(bone_name)
                if eb:
                    bone_data[bone_name] = {
                        "head": eb.head.copy(),
                        "tail": eb.tail.copy(),
                        "roll": eb.roll,
                        "parent": eb.parent.name if eb.parent else None,
                    }

            _safe_mode_set("OBJECT", source_armature)

        # collect custom properties from source bones (object mode)
        # these are critical for serialization (transform, transform0/1, nicetransform, etc.)
        def _deep_convert_idprop(val):
            """recursively convert IDPropertyArray/IDPropertyGroup to plain python types
            so blender can re-create them as new IDProperties without crashing."""
            if hasattr(val, "to_dict"):
                # IDPropertyGroup → dict with recursively converted values
                return {k: _deep_convert_idprop(v) for k, v in val.items()}
            if hasattr(val, "to_list"):
                # IDPropertyArray → list with recursively converted elements
                return [_deep_convert_idprop(x) for x in val.to_list()]
            if isinstance(val, (list, tuple)):
                return [_deep_convert_idprop(x) for x in val]
            # scalar: int, float, str, bool — pass through
            return val

        bone_props = {}  # name -> dict of custom props
        for bone_name in weapon_bones:
            src_bone = source_armature.data.bones.get(bone_name)
            if src_bone:
                props = {}
                for key in src_bone.keys():
                    if key.startswith("_"):
                        continue  # skip internal blender props
                    props[key] = _deep_convert_idprop(src_bone[key])
                bone_props[bone_name] = props

        # create matching bones on PROXY in edit mode
        context.view_layer.objects.active = proxy_armature
        proxy_armature.select_set(True)
        with _ensure_all_bone_collections_visible(proxy_armature):
            _safe_mode_set("EDIT", proxy_armature)

            for bone_name in weapon_bones:
                bd = bone_data.get(bone_name)
                if not bd:
                    continue
                if bone_name not in proxy_armature.data.edit_bones:
                    new_bone = proxy_armature.data.edit_bones.new(bone_name)
                    new_bone.head = bd["head"]
                    new_bone.tail = bd["tail"]
                    new_bone.roll = bd["roll"]
                    # parent to the matching parent if it exists on proxy
                    if bd["parent"] and bd["parent"] in proxy_armature.data.edit_bones:
                        new_bone.parent = proxy_armature.data.edit_bones[bd["parent"]]

            _safe_mode_set("POSE", proxy_armature)

        # copy custom properties to proxy bones (must be done after edit mode)
        # values are already deep-converted to plain python types
        for bone_name, props in bone_props.items():
            proxy_bone = proxy_armature.data.bones.get(bone_name)
            if proxy_bone:
                for key, value in props.items():
                    try:
                        proxy_bone[key] = value
                    except Exception as e:
                        print(
                            f"[WeaponImport] Failed to copy prop '{key}' to proxy bone '{bone_name}': {type(value).__name__} = {value!r}: {e}")

        # add copy transforms constraints
        for bone_name in weapon_bones:
            if bone_name in proxy_armature.pose.bones:
                pose_bone = proxy_armature.pose.bones[bone_name]
                # skip if already has a copy constraint for this bone
                has_copy = any(
                    c.type == "COPY_TRANSFORMS" and c.target == source_armature and c.subtarget == bone_name
                    for c in pose_bone.constraints
                )
                if not has_copy:
                    c = pose_bone.constraints.new(type="COPY_TRANSFORMS")
                    c.target = source_armature
                    c.subtarget = bone_name
                    c.name = f"WeaponCopy_{bone_name}"

        _safe_mode_set("OBJECT", proxy_armature)
        print("[WeaponImport] Cloned weapon bones to proxy with COPY_TRANSFORMS constraints")


class OBJECT_OT_ImportModel(bpy.types.Operator, ImportHelper):
    bl_label = "Import rig data (.obj)"
    bl_idname = "object.rbxanims_importmodel"
    bl_description = "NOT RECOMMENDED Import rig data (.obj)"

    filename_ext = ".obj"
    filter_glob: bpy.props.StringProperty(default="*.obj", options={"HIDDEN"})
    filepath: bpy.props.StringProperty(name="File Path", maxlen=1024, default="")

    def execute(self, context):
        # Do not clear objects
        objnames_before_import = {obj.name for obj in iter_scene_objects(context.scene)}
        if bpy.app.version >= (5, 0, 0):
            bpy.ops.wm.obj_import(
                filepath=self.properties.filepath,
                use_split_groups=True,
                forward_axis="NEGATIVE_Z",
                up_axis="Y",
            )
        elif bpy.app.version >= (4, 0, 0):
            bpy.ops.wm.obj_import(
                filepath=self.properties.filepath,
                use_split_groups=True,
            )
        else:
            bpy.ops.import_scene.obj(
                filepath=self.properties.filepath, use_split_groups=True
            )

        # Get the actual newly imported OBJECTS
        imported_objs = [
            obj for obj in iter_scene_objects(context.scene) if obj.name not in objnames_before_import
        ]

        # Extract meta...
        encodedmeta = ""
        partial = {}
        meta_objs_to_delete = []
        for obj in imported_objs:
            # Case-insensitive match for Meta part names (Roblox/OBJ idiosyncrasies)
            match = re.search(r"^meta(\d+)q1(.*?)q1\d*(\.\d+)?$", obj.name, re.IGNORECASE)
            if match:
                partial[int(match.group(1))] = match.group(2)
                meta_objs_to_delete.append(obj)

        # Check if this is actually a rig file (has metadata)
        if not meta_objs_to_delete:
            self.report(
                {"ERROR"},
                "This OBJ file does not contain Roblox rig metadata. "
                "Please use Blender's standard OBJ importer for regular 3D models, "
                "or export the rig from Roblox Studio using the Roblox Animations plugin.",
            )
            return {"CANCELLED"}

        # The rig parts are simply the imported objects that are not meta objects.
        # This is done before deleting, ensuring we have valid object references.
        meta_set = set(meta_objs_to_delete)
        # capture names BEFORE removal — live refs become stale after
        # bpy.data.objects.remove() re-allocates the container.
        rig_part_names = [obj.name for obj in imported_objs if obj not in meta_set]

        # Batch-remove meta objects: collect orphan mesh data, then purge once.
        # Calling bpy.data.objects.remove() in a loop is O(n²) bc each call
        # triggers depsgraph invalidation. Instead we unlink + batch purge.
        orphan_meshes = []
        for obj in meta_objs_to_delete:
            mesh = obj.data if obj.type == "MESH" else None
            for coll in list(obj.users_collection):
                coll.objects.unlink(obj)
            # Use do_unlink=True to handle edge-cases where Blender still
            # tracks a hidden user after manual collection unlinks.
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh and mesh.users == 0:
                orphan_meshes.append(mesh)
        for mesh in orphan_meshes:
            bpy.data.meshes.remove(mesh)

        # re-fetch by name now that the container is stable
        rig_part_objs = [bpy.data.objects[n] for n in rig_part_names if n in bpy.data.objects]

        try:
            for i in range(1, len(partial) + 1):
                if i in partial:  # Check if the key exists
                    encodedmeta += partial[i]
                else:
                    self.report(
                        {"ERROR"},
                        f"Missing metadata part {i}. The rig file may be corrupted.",
                    )
                    return {"CANCELLED"}

            encodedmeta = encodedmeta.replace("0", "=")

            # Validate encoded metadata is not empty
            if not encodedmeta.strip():
                self.report(
                    {"ERROR"},
                    "Rig metadata is empty or corrupted. The rig file may be corrupted.",
                )
                return {"CANCELLED"}

            try:
                meta = base64.b32decode(encodedmeta, True).decode("utf-8")
            except Exception as e:
                self.report(
                    {"ERROR"},
                    f"Failed to decode rig metadata: {str(e)}. The rig file may be corrupted.",
                )
                return {"CANCELLED"}

            try:
                meta_loaded = json.loads(meta)
            except Exception as e:
                self.report(
                    {"ERROR"},
                    f"Failed to parse rig metadata JSON: {str(e)}. The rig file may be corrupted.",
                )
                return {"CANCELLED"}

            normalized_handle_count = _normalize_accessory_handle_jnames(meta_loaded)
            if normalized_handle_count:
                print(
                    f"[RigImport] normalized {normalized_handle_count} accessory Handle joint name(s) to pname"
                )
                meta = json.dumps(meta_loaded, separators=(",", ":"))

            print(
                f"[RigImport] import_ops build=2.4.7 export_version={meta_loaded.get('version', 'unknown')} "
                f"has_skinned_mesh_metadata={_meta_has_skinned_meshes(meta_loaded)} "
                f"has_filemesh_candidates={_meta_has_filemesh_candidates(meta_loaded)}"
            )

            # --- WEAPON IMPORT PATH ---
            if meta_loaded.get("exportType") == "weapon":
                weapon_name = meta_loaded.get("weaponName", "Weapon")
                _pending_weapon_import.clear()
                _pending_weapon_import["weapon_name"] = weapon_name
                # store object NAMES, not live refs — these cross an
                # operator / undo boundary and live bpy.data refs become
                # stale after undo-step creation (dangling C pointers →
                # build_materials null deref on ctrl+z).
                payload: WeaponImportPayload = {
                    "schema": _WEAPON_IMPORT_PAYLOAD_VERSION,
                    "meta_loaded": meta_loaded,
                    "rig_part_obj_names": [obj.name for obj in rig_part_objs],
                }
                _pending_weapon_import["data"] = payload
                bpy.ops.object.rbxanims_confirm_weapon_target("INVOKE_DEFAULT")
                return {"FINISHED"}

            # Store meta in an empty (direct data API, no operator overhead).
            ob = bpy.data.objects.new("Meta", None)
            context.scene.collection.objects.link(ob)
            rig_name = meta_loaded.get("rigName", "Rig")
            ob.name = get_unique_name(f"__{rig_name}Meta")
            ob["RigMeta"] = meta

            # Create a unique master collection for this rig
            master_collection_name = get_unique_collection_name(f"RIG: {rig_name}")
            master_collection = bpy.data.collections.new(master_collection_name)
            context.scene.collection.children.link(master_collection)

            # Create a sub-collection for the parts
            parts_collection = bpy.data.collections.new("Parts")
            master_collection.children.link(parts_collection)

            # Move the meta object to the master collection
            for coll in list(ob.users_collection):
                coll.objects.unlink(ob)
            master_collection.objects.link(ob)

            # Move all imported parts to the rig's parts collection
            for obj in rig_part_objs:
                if obj:  # Check if object still exists
                    for coll in list(obj.users_collection):
                        coll.objects.unlink(obj)
                    parts_collection.objects.link(obj)

            renamed_by_index, index_warn = _rename_indexed_parts(meta_loaded, parts_collection)
            if index_warn:
                self.report({"WARNING"}, index_warn)

            if not renamed_by_index:
                # Indexed rename didn't fire (old export or non-standard OBJ names),
                # fall back to size/position fingerprinting.
                renamed_via_fp = _rename_parts_by_size_fingerprint(meta_loaded, parts_collection)

                fp_map = meta_loaded.get("_fingerprint_object_map", {})
                rig_scale = meta_loaded.get("_rig_scale", 1.0)
                _rename_parts_by_fingerprint(meta_loaded.get("rig"), parts_collection,
                                             renamed_via_fp, fp_map, rig_scale, meta_loaded=meta_loaded)

                fp_map = meta_loaded.get("_fingerprint_object_map", {})
                if fp_map:
                    fp_map_names = {obj_name: obj_name for obj_name in fp_map.keys()}
                    ob["_FingerprintMap"] = json.dumps(fp_map_names)
                    print(f"[RigImport] Stored {len(fp_map_names)} authoritative part mappings")

            else:
                print("[RigImport] Indexed rename succeeded, skipping fingerprint passes")

            has_skinned_mesh_metadata = _meta_has_skinned_meshes(meta_loaded)
            has_filemesh_candidates = _meta_has_filemesh_candidates(meta_loaded)
            has_deform_bones = _rig_contains_deform_bones(meta_loaded.get("rig"))

            print(
                f"[RigImport] deform-detect has_deform_bones={has_deform_bones} "
                f"has_skinned_mesh_metadata={has_skinned_mesh_metadata} "
                f"has_filemesh_candidates={has_filemesh_candidates}"
            )

            if has_deform_bones or has_filemesh_candidates:
                try:
                    majority_skinned = _meta_is_majority_skinned(meta_loaded)
                    bone_mode = "CONNECT" if majority_skinned else "LOCAL_YAXIS_EXTEND"
                    print(
                        f"[RigImport] auto-generating armature for deform/filemesh-candidate rig (mode={bone_mode}, majority_skinned={majority_skinned})")
                    create_rig(bone_mode, ob.name)
                    if has_skinned_mesh_metadata:
                        print("[RigImport] automatic skinning path completed")
                        self.report({"INFO"}, "skinned rig detected: armature generated and skinning applied")
                    elif has_filemesh_candidates:
                        print(
                            "[RigImport] mesh file candidates detected without explicit Studio skinning signal; "
                            "FileMesh parsing will determine whether weights can be reconstructed"
                        )
                    else:
                        export_version = meta_loaded.get("version", "unknown")
                        self.report(
                            {"WARNING"},
                            "deform rig detected and armature generated, but this export is missing skin metadata; "
                            f"re-export from the updated studio plugin to reconstruct weights (export version: {export_version})",
                        )
                        print(
                            "[RigImport] deform bones detected, but partAux has no mesh_id/has_skinning data; "
                            f"skinning cannot be rebuilt from this export (version={export_version})"
                        )
                except Exception as exc:
                    self.report(
                        {"WARNING"},
                        f"imported deform/skinned rig, but automatic armature generation failed: {exc}",
                    )

            return {"FINISHED"}
        except KeyError as e:
            self.report(
                {"ERROR"},
                f"KeyError: {str(e)} - The rig file may be corrupted or incompatible.",
            )
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Error importing rig: {str(e)}")
            return {"CANCELLED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def _import_weapon(self: Any, context, meta_loaded, rig_part_objs):
        """Handle weapon/accessory OBJ import (exportType == 'weapon').

        Two paths:
          1. Motor6D weapon (meta has 'joints'):  Build a bone sub-tree on the
             existing armature mirroring the weapon's Motor6D hierarchy, then
             constrain each mesh to its corresponding bone.
          2. Simple weapon (no Motor6Ds):  Rename meshes, select them, and
             invoke the single-bone attach dialog.
        """
        from mathutils import Matrix
        from ..core.constants import get_transform_to_blender
        from ..core.utils import cf_to_mat, solve_equipped_joint_matrix
        from ..rig.creation import (
            load_rigbone,
            _collect_all_bone_names,
            _build_match_context,
            _safe_mode_set,
        )

        weapon_name = meta_loaded.get("weaponName", "Weapon")
        parts_map = meta_loaded.get("parts", {})
        suggested_bone = _dict_get_any(
            meta_loaded,
            (
                "suggestedBone",
                "suggested_bone",
                "attachmentBone",
                "attachBone",
                "parentBone",
                "parent_bone",
            ),
        ) or ""
        joints_tree = _dict_get_any(meta_loaded, ("joints", "jointsTree", "jointTree"))
        # present only for Motor6D weapons
        weapon_attachments = meta_loaded.get("weaponAttachments")

        print(f"[WeaponImport] Starting import: weapon='{weapon_name}', "
              f"parts_map type={type(parts_map).__name__} len={len(parts_map) if parts_map else 0}, "
              f"has_joints={joints_tree is not None}, suggested_bone='{suggested_bone}'")
        print(f"[WeaponImport] Imported objects: {[o.name for o in rig_part_objs]}")

        # Single-attachment exports may place authoritative attach metadata under
        # weaponAttachments[0] while top-level fields are empty. Normalize here
        # so parent selection and relocation math use the same data path.
        if (
            not meta_loaded.get("_split_import_pass")
            and isinstance(weapon_attachments, list)
            and len(weapon_attachments) == 1
            and isinstance(weapon_attachments[0], dict)
        ):
            attachment = weapon_attachments[0]

            att_joints = _dict_get_any(attachment, ("joints", "jointsTree", "jointTree"))
            if not isinstance(joints_tree, dict) and isinstance(att_joints, dict):
                joints_tree = att_joints
                meta_loaded["joints"] = joints_tree

            if not suggested_bone:
                suggested_bone = _dict_get_any(
                    attachment,
                    (
                        "suggestedBone",
                        "suggested_bone",
                        "attachmentBone",
                        "attachBone",
                        "parentBone",
                        "parent_bone",
                    ),
                ) or ""
                if not suggested_bone and isinstance(joints_tree, dict):
                    suggested_bone = _dict_get_any(
                        joints_tree,
                        (
                            "parentBone",
                            "parent_bone",
                            "parentName",
                            "parentPart",
                            "parentPartName",
                            "attachTo",
                            "attachedTo",
                        ),
                    ) or ""
                if suggested_bone:
                    meta_loaded["suggestedBone"] = suggested_bone

            conn_c0 = _coerce_cf12(
                meta_loaded.get("connectionC0")
                or _dict_get_any(meta_loaded, ("connection_c0", "c0", "C0"))
                or _dict_get_any(attachment, ("connectionC0", "connection_c0", "c0", "C0"))
                or _dict_get_any(joints_tree, ("connectionC0", "connection_c0", "c0", "C0", "jointtransform0", "jointTransform0"))
            )
            conn_c1 = _coerce_cf12(
                meta_loaded.get("connectionC1")
                or _dict_get_any(meta_loaded, ("connection_c1", "c1", "C1"))
                or _dict_get_any(attachment, ("connectionC1", "connection_c1", "c1", "C1"))
                or _dict_get_any(joints_tree, ("connectionC1", "connection_c1", "c1", "C1", "jointtransform1", "jointTransform1"))
            )
            if conn_c0 and not meta_loaded.get("connectionC0"):
                meta_loaded["connectionC0"] = conn_c0
            if conn_c1 and not meta_loaded.get("connectionC1"):
                meta_loaded["connectionC1"] = conn_c1
            if not meta_loaded.get("connectionJointType"):
                joint_type = _dict_get_any(
                    attachment,
                    ("connectionJointType", "connection_joint_type", "jointType", "joint_type"),
                ) or _dict_get_any(joints_tree, ("jointType", "joint_type"))
                if joint_type:
                    meta_loaded["connectionJointType"] = joint_type

            print(
                f"[WeaponImport] Normalized single attachment metadata: "
                f"has_joints={joints_tree is not None}, suggested_bone='{suggested_bone}', "
                f"has_connection={bool(meta_loaded.get('connectionC0') and meta_loaded.get('connectionC1'))}"
            )

        # Additional compatibility pass: some exporters store Motor6D data in
        # nested arrays/tables not covered by the top-level/attachment schema.
        if isinstance(joints_tree, dict):
            root_lookup = joints_tree.get("pname") or joints_tree.get("jname") or weapon_name
            if not isinstance(root_lookup, str):
                root_lookup = weapon_name
            extracted_conn = _extract_motor6d_connection(
                meta_loaded,
                root_lookup,
                suggested_bone or None,
            )
            if extracted_conn:
                if not suggested_bone and extracted_conn.get("parent_name"):
                    suggested_bone = extracted_conn["parent_name"]
                    meta_loaded["suggestedBone"] = suggested_bone
                if not meta_loaded.get("connectionC0") and extracted_conn.get("connectionC0"):
                    meta_loaded["connectionC0"] = extracted_conn["connectionC0"]
                if not meta_loaded.get("connectionC1") and extracted_conn.get("connectionC1"):
                    meta_loaded["connectionC1"] = extracted_conn["connectionC1"]
                if not meta_loaded.get("connectionJointType") and extracted_conn.get("jointType"):
                    meta_loaded["connectionJointType"] = extracted_conn["jointType"]
                print(
                    f"[WeaponImport] Extracted Motor6D connection from nested metadata: "
                    f"parent='{meta_loaded.get('suggestedBone', suggested_bone)}', "
                    f"score={extracted_conn['score']:.2f}, depth={extracted_conn['depth']}"
                )

        # New multi-piece weapon payload (v2.5+): split into independent
        # single-root imports so each piece can attach to a different rig bone.
        if (
            not meta_loaded.get("_split_import_pass")
            and isinstance(weapon_attachments, list)
            and len(weapon_attachments) > 1
        ):
            print(f"[WeaponImport] Multi-attachment import: {len(weapon_attachments)} attachment roots")

            # name -> idx map from partAux for p<idx>x object lookup
            part_name_to_idx = {}
            part_aux = meta_loaded.get("partAux")
            if isinstance(part_aux, dict):
                part_aux = list(part_aux.values())
            if isinstance(part_aux, list):
                for entry in part_aux:
                    if isinstance(entry, dict):
                        idx = entry.get("idx")
                        name = entry.get("name")
                        if isinstance(idx, str) and idx.isdigit():
                            idx = int(idx)
                        if isinstance(idx, int) and isinstance(name, str):
                            part_name_to_idx[name] = idx
                            part_name_to_idx[name.lower()] = idx

            def _extract_import_part_index(obj_name: str):
                """Extract export part index from importer-renamed object names.
                Supports patterns like p12x, p12x.001, P12x1, P12x1.001."""
                base = _strip_suffix(obj_name or "")
                m = re.match(r"(?i)^p(\d+)x(?:\d+)?$", base)
                if m:
                    return int(m.group(1))
                # very defensive fallback for odd importer variants
                m = re.match(r"(?i)^p(\d+)x", base)
                if m:
                    return int(m.group(1))
                return None

            # parse imported object name -> idx
            obj_by_idx = {}
            for obj in rig_part_objs:
                idx = _extract_import_part_index(obj.name)
                if idx is not None:
                    obj_by_idx[idx] = obj

            def _collect_part_names(node, out):
                if not isinstance(node, dict):
                    return
                pname = node.get("pname")
                if isinstance(pname, str):
                    out.add(pname)
                jname = node.get("jname")
                if isinstance(jname, str):
                    out.add(jname)
                for ch in node.get("children", []):
                    _collect_part_names(ch, out)

            imported_count = 0
            for i, attachment in enumerate(weapon_attachments, start=1):
                if not isinstance(attachment, dict):
                    continue
                att_joints = attachment.get("joints")
                if not isinstance(att_joints, dict):
                    continue

                part_names = set()
                _collect_part_names(att_joints, part_names)

                subset_objs = []
                for part_name in part_names:
                    idx = part_name_to_idx.get(part_name)
                    if idx is None and isinstance(part_name, str):
                        idx = part_name_to_idx.get(part_name.lower())
                    if idx is None:
                        continue
                    obj = obj_by_idx.get(idx)
                    if obj:
                        subset_objs.append(obj)

                if not subset_objs:
                    print(f"[WeaponImport] Attachment #{i} skipped: no matching imported objs")
                    continue

                sub_meta = dict(meta_loaded)
                sub_meta["_split_import_pass"] = True
                sub_meta["weaponAttachments"] = None
                sub_meta["joints"] = att_joints
                sub_meta["suggestedBone"] = _dict_get_any(
                    attachment,
                    (
                        "suggestedBone",
                        "suggested_bone",
                        "attachmentBone",
                        "attachBone",
                        "parentBone",
                        "parent_bone",
                    ),
                ) or suggested_bone
                sub_meta["connectionC0"] = _coerce_cf12(
                    _dict_get_any(attachment, ("connectionC0", "connection_c0", "c0", "C0"))
                    or _dict_get_any(att_joints, ("connectionC0", "connection_c0", "c0", "C0", "jointtransform0", "jointTransform0"))
                )
                sub_meta["connectionC1"] = _coerce_cf12(
                    _dict_get_any(attachment, ("connectionC1", "connection_c1", "c1", "C1"))
                    or _dict_get_any(att_joints, ("connectionC1", "connection_c1", "c1", "C1", "jointtransform1", "jointTransform1"))
                )
                sub_meta["connectionJointType"] = _dict_get_any(
                    attachment,
                    ("connectionJointType", "connection_joint_type", "jointType", "joint_type"),
                ) or _dict_get_any(att_joints, ("jointType", "joint_type"))

                # self may be a lightweight reporter proxy (no bound method),
                # so recurse via the class method explicitly.
                result = OBJECT_OT_ImportModel._import_weapon(
                    self, context, sub_meta, subset_objs
                )
                if result == {"CANCELLED"}:
                    return {"CANCELLED"}
                if result == {"FINISHED"}:
                    imported_count += 1

            if imported_count > 0:
                self.report({"INFO"}, f"Imported {imported_count} weapon attachment root(s).")
                return {"FINISHED"}
            return {"CANCELLED"}

        # ---- Early bone validation (before any scene mutations) ----
        # For Motor6D weapons we need the suggested bone to exist on the
        # target armature.  Validate NOW so we can bail cleanly without
        # leaving orphaned collections/objects that crash on undo.
        settings_early = getattr(context.scene, "rbx_anim_settings", None)
        arm_name_early = settings_early.rbx_anim_armature if settings_early else None
        armature_early = None
        if arm_name_early:
            armature_early = get_object_by_name(arm_name_early, context.scene)
            if armature_early and armature_early.type != "ARMATURE":
                armature_early = None

        if joints_tree and armature_early and suggested_bone:
            resolved = None
            if suggested_bone in armature_early.data.bones:
                resolved = suggested_bone
            else:
                for b in armature_early.data.bones:
                    if b.name.lower() == suggested_bone.lower():
                        resolved = b.name
                        break
            if not resolved:
                self.report(
                    {"ERROR"},
                    f"Bone \"{suggested_bone}\" not found on \"{armature_early.name}\".",
                )
                return {"CANCELLED"}

        # ---- Create a temporary collection for weapon parts ----
        # (mirrors the rig import flow: collection → indexed rename → fingerprint)
        from ..rig.creation import get_unique_collection_name
        weapon_coll_name = get_unique_collection_name(f"WEAPON: {weapon_name}")
        weapon_coll = bpy.data.collections.new(weapon_coll_name)
        context.scene.collection.children.link(weapon_coll)

        parts_coll = bpy.data.collections.new("Parts")
        weapon_coll.children.link(parts_coll)

        # move all imported mesh objects into the parts collection
        for obj in rig_part_objs:
            if obj:
                for coll in list(obj.users_collection):
                    coll.objects.unlink(obj)
                parts_coll.objects.link(obj)

        # ---- Rename indexed exports when needed; otherwise accept direct names ----
        renamed_by_index, index_warn = _rename_indexed_parts(meta_loaded, parts_coll)
        if index_warn:
            print(f"[WeaponImport] Index rename warning: {index_warn}")

        if renamed_by_index:
            print("[WeaponImport] Indexed rename succeeded")
        elif _parts_already_named(meta_loaded.get("parts", {}), parts_coll):
            print("[WeaponImport] Imported meshes already use exported part names")
        else:
            # fallback to fingerprint matching (same as rig)
            renamed_via_fp = _rename_parts_by_size_fingerprint(meta_loaded, parts_coll)
            fp_map = meta_loaded.get("_fingerprint_object_map", {})
            # weapon has no "rig" key, so skip tree-based fingerprinting
            print(f"[WeaponImport] Indexed rename failed, fingerprint renamed {renamed_via_fp} parts")

        # collect weapon meshes after rename
        weapon_meshes = [obj for obj in parts_coll.objects if obj.type == "MESH"]
        mesh_by_name = {obj.name: obj for obj in weapon_meshes}

        print(f"[WeaponImport] Renamed {len(weapon_meshes)} weapon meshes: {[o.name for o in weapon_meshes]}")

        if not weapon_meshes:
            self.report({"WARNING"}, "No weapon meshes found in import")
            return {"CANCELLED"}

        # ---- locate the target armature ----
        settings = getattr(context.scene, "rbx_anim_settings", None)
        arm_name = settings.rbx_anim_armature if settings else None
        armature = None
        if arm_name:
            armature = get_object_by_name(arm_name, context.scene)
            if armature and armature.type != "ARMATURE":
                armature = None

        if not armature:
            print(f"[WeaponImport] WARNING: No armature found (arm_name={arm_name!r}). "
                  f"Weapon bones cannot be created without an active rig.")

        # ==================================================================
        # PATH 1:  Motor6D weapon — build bone sub-tree
        # ==================================================================
        if joints_tree and armature:
            print(f"[WeaponImport] Motor6D weapon '{weapon_name}' — building bone sub-tree")

            # find the parent bone on the existing armature
            # (already validated in early check above, this is just resolution)
            parent_bone_name = suggested_bone
            if parent_bone_name and parent_bone_name not in armature.data.bones:
                # try case-insensitive lookup
                for b in armature.data.bones:
                    if b.name.lower() == parent_bone_name.lower():
                        parent_bone_name = b.name
                        break
                else:
                    # should never happen — early validation catches this
                    print(f"[WeaponImport] BUG: bone '{suggested_bone}' passed "
                          f"early check but not found now")
                    parent_bone_name = None

            if not parent_bone_name:
                # Metadata from some exporter variants omits suggestedBone.
                # Infer the best parent from transform proximity before any
                # blind fallback to the first armature bone.
                inferred_parent, inferred_dist = _infer_weapon_parent_bone_from_transform(
                    armature, joints_tree
                )
                if inferred_parent:
                    parent_bone_name = inferred_parent
                    print(
                        f"[WeaponImport] Inferred parent bone '{parent_bone_name}' "
                        f"from weapon root transform (distance={inferred_dist:.4f})"
                    )
                elif armature.data.bones:
                    parent_bone_name = armature.data.bones[0].name
                    print(f"[WeaponImport] WARNING: falling back to "
                          f"'{parent_bone_name}' (unexpected)")
                else:
                    self.report({"ERROR"}, "Target armature has no bones")
                    return {"CANCELLED"}

            print(f"[WeaponImport] Parent bone: '{parent_bone_name}'")

            # try to move weapon meshes into the rig's existing Parts collection
            from ..core.utils import (
                find_master_collection_for_object,
                find_parts_collection_in_master,
            )
            master_coll = find_master_collection_for_object(armature)
            rig_parts_coll = find_parts_collection_in_master(master_coll, create_if_missing=False)
            target_coll = rig_parts_coll or parts_coll  # use rig's if available, else weapon's own
            if rig_parts_coll and rig_parts_coll != parts_coll:
                for obj in weapon_meshes:
                    for coll in list(obj.users_collection):
                        coll.objects.unlink(obj)
                    rig_parts_coll.objects.link(obj)
                # remove the now-empty weapon parts collection
                weapon_coll.children.unlink(parts_coll)
                bpy.data.collections.remove(parts_coll)
                # move the weapon_coll under the rig master instead of scene root
                if master_coll:
                    context.scene.collection.children.unlink(weapon_coll)
                    master_coll.children.link(weapon_coll)

            # build a match_ctx so load_rigbone can link meshes to bones
            match_ctx = _build_match_context(target_coll)

            # populate fingerprint_object_map so load_rigbone finds our meshes
            fp_map = {}
            for name, obj in mesh_by_name.items():
                fp_map[name] = obj
            match_ctx["fingerprint_object_map"] = fp_map
            print(f"[WeaponImport] fp_map keys: {list(fp_map.keys())}")

            # deselect everything and ensure object mode before switching
            _safe_mode_set("OBJECT")
            try:
                bpy.ops.object.select_all(action="DESELECT")
            except Exception:
                pass

            # enter edit mode on the armature
            prev_active = context.view_layer.objects.active
            context.view_layer.objects.active = armature
            armature.select_set(True)
            # unhide all bone collections so edit_bones can see hidden bones
            with _ensure_all_bone_collections_visible(armature):
                entered = _safe_mode_set("EDIT", armature)
                if not entered:
                    self.report({"ERROR"}, "Failed to enter edit mode on armature")
                    return {"CANCELLED"}

                parent_edit_bone = armature.data.edit_bones.get(parent_bone_name)
                if not parent_edit_bone:
                    _safe_mode_set("OBJECT", armature)
                    self.report({"ERROR"}, f"Bone '{parent_bone_name}' not found on armature in edit mode")
                    return {"CANCELLED"}

            # ---- Weapon bone strategy ----
            # Use the SAME load_rigbone flow as normal rig import so that all
            # position / rotation math is handled identically.  The weapon
            # root gets its own bone (e.g. "Handle") parented to the existing
            # parent bone (e.g. "RightHand").  Child weapon parts get bones
            # parented to the weapon root bone.
            #
            # The weapon root needs jointtransform0/1 so load_rigbone can
            # compute its offset from the parent.  If the exporter provided
            # connectionC0/C1, those ARE the Motor6D transforms.  Otherwise
            # we use identity (weapon root lands on parent bone head).

            from ..rig.constraints import link_object_to_bone_rigid
            t2b = get_transform_to_blender()

            root_jname = joints_tree.get("jname", weapon_name)
            weapon_children = joints_tree.get("children", [])
            parent_transform_prop = parent_edit_bone.get("transform")
            if parent_transform_prop:
                parent_part_world_mat = Matrix([list(row) for row in parent_transform_prop])
            else:
                parent_part_world_mat = t2b.inverted() @ Matrix.Translation(parent_edit_bone.head)
                print("[WeaponImport] WARNING: no stored transform on parent bone, using bone head")

            print(f"[WeaponImport] Weapon root '{root_jname}' will be parented "
                  f"to bone '{parent_bone_name}'")
            print(f"[WeaponImport] {len(weapon_children)} child joint(s)")

            # ---- Relocate weapon transforms to parent bone's coordinate space ----
            # The weapon's `transform` fields are absolute Roblox world CFrames.
            # The rig bones are also at their Roblox world positions.  If the
            # weapon was not co-located with the rig (common — the weapon sits
            # in Workspace or StarterPack, not equipped on the character), we
            # need to shift all weapon transforms so the weapon root aligns
            # with where it WOULD be if it were attached to the parent bone.
            #
            # We compute the delta in Roblox space and apply it to every
            # `transform` field in the joint tree.  This way load_rigbone
            # (which reads `transform` to compute bone head) places everything
            # in the right spot — same as if the weapon had been at that
            # position during export.
            weapon_root_cf = joints_tree.get("transform")
            if weapon_root_cf:
                # Where the weapon root CFrame IS (roblox world → blender)
                weapon_root_mat = cf_to_mat(weapon_root_cf)
                weapon_root_pos_blender = (t2b @ weapon_root_mat).to_translation()

                # Where we WANT the weapon root: its EQUIPPED position.
                #
                # Roblox joint equation:
                #   ParentPart.CFrame * C0 = WeaponRoot.CFrame * C1
                #   WeaponRoot.CFrame = ParentPart.CFrame * C0 * C1^-1
                #
                # After relocation, load_rigbone applies C1 (jointtransform1):
                #   bone.head = equipped_cf * C1
                #             = parent * C0 * C1^-1 * C1
                #             = parent * C0   (correct joint position)
                #
                # The parent bone stores its Roblox CFrame in the "transform"
                # custom property (set by load_rigbone during rig import).
                parent_cf_mat = parent_part_world_mat

                conn_c0 = _coerce_cf12(
                    meta_loaded.get("connectionC0")
                    or _dict_get_any(meta_loaded, ("connection_c0", "c0", "C0"))
                    or _dict_get_any(joints_tree, ("connectionC0", "connection_c0", "c0", "C0", "jointtransform0", "jointTransform0"))
                )
                conn_c1 = _coerce_cf12(
                    meta_loaded.get("connectionC1")
                    or _dict_get_any(meta_loaded, ("connection_c1", "c1", "C1"))
                    or _dict_get_any(joints_tree, ("connectionC1", "connection_c1", "c1", "C1", "jointtransform1", "jointTransform1"))
                )
                inferred_conn = False
                # When exporter metadata omits connectionC0/C1, derive a stable
                # local joint from current parent/world transforms.
                # Prefer a parent bone endpoint (head/tail) nearest the weapon
                # root so the inferred pivot is at the limb/hand contact point,
                # not the weapon center.
                if not (conn_c0 and conn_c1):
                    try:
                        weapon_root_pos_bl = (t2b @ weapon_root_mat).to_translation()
                        cand_head = parent_edit_bone.head.copy()
                        cand_tail = parent_edit_bone.tail.copy()
                        if (cand_tail - cand_head).length > 1e-5:
                            if (weapon_root_pos_bl - cand_tail).length <= (weapon_root_pos_bl - cand_head).length:
                                joint_anchor_bl = cand_tail
                                anchor_name = "tail"
                            else:
                                joint_anchor_bl = cand_head
                                anchor_name = "head"
                        else:
                            joint_anchor_bl = cand_head
                            anchor_name = "head"

                        joint_anchor_rb = (t2b.inverted() @ Matrix.Translation(joint_anchor_bl)).to_translation()
                        joint_world_mat = weapon_root_mat.copy()
                        joint_world_mat.translation = joint_anchor_rb

                        inferred_c0_mat = parent_cf_mat.inverted() @ joint_world_mat
                        inferred_c1_mat = weapon_root_mat.inverted() @ joint_world_mat
                        conn_c0 = mat_to_cf(inferred_c0_mat)
                        conn_c1 = mat_to_cf(inferred_c1_mat)
                        if not meta_loaded.get("connectionC0"):
                            meta_loaded["connectionC0"] = conn_c0
                        if not meta_loaded.get("connectionC1"):
                            meta_loaded["connectionC1"] = conn_c1
                        if not meta_loaded.get("connectionJointType"):
                            meta_loaded["connectionJointType"] = "Motor6D"
                        inferred_conn = True
                        print(
                            f"[WeaponImport] Inferred joint anchor from parent bone {anchor_name} "
                            f"for missing C0/C1"
                        )
                    except Exception:
                        conn_c0 = None
                        conn_c1 = None

                equipped_cf = weapon_root_mat
                if conn_c0 and conn_c1:
                    equipped_cf = solve_equipped_joint_matrix(
                        parent_cf_mat, conn_c0, conn_c1
                    )
                    if inferred_conn:
                        print("[WeaponImport] Inferred missing C0/C1 from parent and weapon root transform")
                    print("[WeaponImport] Target = parent * C0 * C1^-1 "
                          "(equipped position)")
                else:
                    print("[WeaponImport] No C0/C1 — keeping exported weapon root transform")

                target_pos = (t2b @ equipped_cf).to_translation()

                # Delta for BONE TRANSFORMS (CFrame-based, in roblox space)
                delta_blender = target_pos - weapon_root_pos_blender

                # Delta for MESHES — use the actual mesh vertex center rather
                # than the CFrame, since OBJ axis handling may produce a
                # slightly different position than t2b @ CFrame.
                root_pname_lookup = joints_tree.get("pname") or root_jname
                root_mesh_for_delta = mesh_by_name.get(root_pname_lookup)
                if not root_mesh_for_delta:
                    for n, o in mesh_by_name.items():
                        if n.lower() == (root_pname_lookup or "").lower():
                            root_mesh_for_delta = o
                            break
                if root_mesh_for_delta:
                    from ..rig.creation import _get_mesh_world_center
                    actual_mesh_center = _get_mesh_world_center(root_mesh_for_delta)
                    _ = target_pos - actual_mesh_center
                else:
                    _ = delta_blender

                if delta_blender.length > 0.0001:
                    # Compute the FULL rigid relocation matrix in roblox space.
                    # This handles both translation AND rotation so the weapon
                    # aligns to the rig no matter which direction it faces.
                    #   relocation = equipped_cf @ weapon_root_cf^-1
                    # Applied to each node:
                    #   new_transform = relocation @ old_transform
                    relocation_mat = equipped_cf @ weapon_root_mat.inverted()

                    def _relocate_joint_transforms(node, reloc):
                        tf = node.get("transform")
                        if tf and len(tf) >= 12:
                            old_mat = cf_to_mat(tf)
                            new_mat = reloc @ old_mat
                            new_cf = mat_to_cf(new_mat)
                            for i in range(len(new_cf)):
                                tf[i] = new_cf[i]
                        for child in node.get("children", []):
                            _relocate_joint_transforms(child, reloc)

                    _relocate_joint_transforms(joints_tree, relocation_mat)

                    # Move + rotate mesh objects in blender space
                    reloc_blender = t2b @ relocation_mat @ t2b.inverted()
                    _safe_mode_set("OBJECT", armature)
                    for obj in weapon_meshes:
                        # Apply the full rigid transform to each mesh
                        obj.matrix_world = reloc_blender @ obj.matrix_world
                    print(f"[WeaponImport] Relocated weapon: delta={delta_blender.length:.4f}")

                    # re-enter edit mode
                    context.view_layer.objects.active = armature
                    armature.select_set(True)
                    _safe_mode_set("EDIT", armature)
                    parent_edit_bone = armature.data.edit_bones.get(parent_bone_name)

            # Inject connection joint transforms into the weapon root node
            # so load_rigbone knows how to offset it from the parent bone.
            if "jointtransform0" not in joints_tree:
                conn_c0 = meta_loaded.get("connectionC0")
                conn_c1 = meta_loaded.get("connectionC1")
                if conn_c0 and conn_c1:
                    joints_tree["jointtransform0"] = conn_c0
                    joints_tree["jointtransform1"] = conn_c1
                    joints_tree["jointType"] = meta_loaded.get(
                        "connectionJointType", "Motor6D")
                    print("[WeaponImport] Using connectionC0/C1 for root joint")
                else:
                    # Identity — weapon root bone sits exactly on parent bone
                    joints_tree["jointtransform0"] = [
                        0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]
                    joints_tree["jointtransform1"] = [
                        0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]
                    print("[WeaponImport] No connection data — identity joint")

            original_parent_map = _annotate_weapon_original_parents(
                joints_tree,
                parent_bone_name,
                parent_part_world_mat,
            )
            if original_parent_map:
                print(
                    f"[WeaponImport] Preserved original Motor6D parents for {len(original_parent_map)} weapon bone(s)"
                )

            # ---- Build weapon bones via load_rigbone (same as rig import) ----
            rigging_type = "RAW"
            all_bone_names = _collect_all_bone_names(joints_tree)
            try:
                load_rigbone(
                    armature, rigging_type, joints_tree,
                    parent_edit_bone, target_coll, match_ctx, all_bone_names,
                )
            except Exception as e:
                import traceback
                traceback.print_exc()
                _safe_mode_set("OBJECT", armature)
                self.report({"ERROR"}, f"Failed to build weapon bones: {e}")
                return {"CANCELLED"}

            _safe_mode_set("OBJECT", armature)

            # Verify root bone was created
            if root_jname not in armature.data.bones:
                print(f"[WeaponImport] WARNING: root bone '{root_jname}' not "
                      f"found. Bones: {[b.name for b in armature.data.bones]}")
            else:
                print(f"[WeaponImport] Root bone '{root_jname}' created ok")

            for bone_name, original_parent_name in original_parent_map.items():
                data_bone = armature.data.bones.get(bone_name)
                if data_bone and original_parent_name:
                    data_bone["rbx_original_parent"] = original_parent_name

            # ---- Apply pending constraints (mesh → bone CHILD_OF) ----
            pending = match_ctx.get("pending_constraints", [])
            applied = 0
            for obj, bone_name in pending:
                bone = armature.data.bones.get(bone_name)
                if bone:
                    link_object_to_bone_rigid(obj, armature, bone)
                    applied += 1
                    print(f"[WeaponImport] Constrained '{obj.name}' -> "
                          f"bone '{bone_name}'")
                else:
                    print(f"[WeaponImport] WARNING: bone '{bone_name}' not "
                          f"found for '{obj.name}'")

            # Constrain any remaining orphan meshes to the weapon root bone
            constrained_objs = {obj for obj, _ in pending}
            root_bone_obj = armature.data.bones.get(root_jname)
            for obj in weapon_meshes:
                if obj not in constrained_objs:
                    target_bone = root_bone_obj or (
                        armature.data.bones.get(parent_bone_name))
                    if target_bone:
                        link_object_to_bone_rigid(obj, armature, target_bone)
                        applied += 1
                        print(f"[WeaponImport] Constrained orphan "
                              f"'{obj.name}' -> '{target_bone.name}'")

            if prev_active:
                context.view_layer.objects.active = prev_active

            self.report(
                {"INFO"},
                f"Imported weapon '{weapon_name}': root '{root_jname}' → bone "
                f"'{parent_bone_name}', {len(weapon_children)} sub-bones, "
                f"{applied} mesh(es) constrained.",
            )
            return {"FINISHED"}

        # ==================================================================
        # PATH 2:  Simple weapon (no Motor6Ds) — single-bone attach
        # ==================================================================
        bpy.ops.object.select_all(action="DESELECT")
        for obj in weapon_meshes:
            obj.select_set(True)
        context.view_layer.objects.active = weapon_meshes[0]

        if armature:
            return bpy.ops.object.rbxanims_attach_to_bone(
                "INVOKE_DEFAULT",
                bone_name=suggested_bone,
                weapon_bone_name=weapon_name,
            )

        names = ", ".join(o.name for o in weapon_meshes)
        hint = f" (suggested bone: {suggested_bone})" if suggested_bone else ""
        self.report(
            {"INFO"},
            f"Imported weapon '{weapon_name}': {names}{hint}. "
            "Select an armature and use Attach to Bone to finish.",
        )
        return {"FINISHED"}


def _infer_rbxm_rig_name(filepath: str) -> str:
    base = os.path.splitext(os.path.basename(filepath or ""))[0]
    return base or "Rig"


_RBXL_COLLECTION_CLASSES = {"Model", "Folder", "Accessory", "Workspace", "WorldModel"}


def _build_rbxl_scene_collections(master_collection, scene_nodes, part_entries=None):
    """Rebuild the supported Roblox container hierarchy as collections.

    This intentionally excludes scripts, terrain, GUI, and services: they are
    not Blender scene geometry. Every supported BasePart is placed under its
    nearest Model/Folder/Accessory collection by referent, never by name.
    Containers that hold no importable part are skipped entirely, so pure
    structure folders (scripts, organisers) leave no empty collections.
    """
    node_by_ref = {
        node.get("inst_ref"): node
        for node in scene_nodes or []
        if isinstance(node, dict) and node.get("inst_ref") is not None
    }
    collection_by_ref = {}
    parent_collection_cache = {}

    def nearest_parent_collection(parent_ref):
        cached = parent_collection_cache.get(parent_ref)
        if cached is not None:
            return cached
        original_parent_ref = parent_ref
        seen = set()
        while parent_ref is not None and parent_ref not in seen:
            seen.add(parent_ref)
            collection = collection_by_ref.get(parent_ref)
            if collection is not None:
                parent_collection_cache[original_parent_ref] = collection
                return collection
            node = node_by_ref.get(parent_ref)
            parent_ref = node.get("parent_ref") if node else None
        parent_collection_cache[original_parent_ref] = master_collection
        return master_collection

    # Only containers on a kept part's ancestor chain earn a collection.
    kept_refs = set()
    for entry in part_entries or []:
        ref = entry.get("parent_ref")
        while ref is not None and ref not in kept_refs:
            kept_refs.add(ref)
            node = node_by_ref.get(ref)
            ref = node.get("parent_ref") if node else None

    pending = [
        node for node in node_by_ref.values()
        if node.get("class_name") in _RBXL_COLLECTION_CLASSES
        and node.get("inst_ref") in kept_refs
    ]
    while pending:
        progressed = False
        for node in pending[:]:
            parent_ref = node.get("parent_ref")
            parent_node = node_by_ref.get(parent_ref)
            if (
                parent_node is not None
                and parent_node.get("class_name") in _RBXL_COLLECTION_CLASSES
                and parent_ref not in collection_by_ref
            ):
                continue
            name = node.get("name") or node.get("class_name") or "Container"
            collection = bpy.data.collections.new(name)
            nearest_parent_collection(parent_ref).children.link(collection)
            collection_by_ref[node["inst_ref"]] = collection
            pending.remove(node)
            progressed = True
        if not progressed:
            # Malformed/cyclic parenting: retain the instances under the scene
            # root instead of losing them.
            for node in pending:
                collection = bpy.data.collections.new(node.get("name") or "Container")
                master_collection.children.link(collection)
                collection_by_ref[node["inst_ref"]] = collection
            break

    def collection_for_part(entry):
        return nearest_parent_collection((entry or {}).get("parent_ref"))

    return collection_for_part


def _create_rbxl_scene_rig_scaffolds(scene_rigs, part_entries, parent_collection):
    """Build the per-rig scaffold for each scene rig under parent_collection.

    Each scaffold is a master collection ("<rig>.model") holding Parts and
    Rig subcollections plus a hidden meta object with the rig's own meta.
    Callers route meshes and armatures into it; returns records of
    {"refs", "parts", "meta_name"}.
    """
    entries_by_ref = {}
    for entry in part_entries:
        if not isinstance(entry, dict):
            continue
        ref = entry.get("inst_ref")
        if ref is not None:
            entries_by_ref[int(ref)] = entry
    scaffolds = []
    for scene_rig in scene_rigs or []:
        refs = {int(ref) for ref in scene_rig.get("part_refs") or []}
        rig_def = scene_rig.get("rig")
        if not refs or not isinstance(rig_def, dict):
            continue
        rig_name = scene_rig.get("name") or "Rig"
        master = bpy.data.collections.new(get_unique_collection_name(f"{rig_name}.model"))
        parent_collection.children.link(master)
        parts = bpy.data.collections.new(f"{master.name} Parts")
        master.children.link(parts)
        rig_coll = bpy.data.collections.new(f"{master.name} Rig")
        master.children.link(rig_coll)
        rig_meta = {
            "rigName": rig_name,
            "rig": rig_def,
            "meshToBone": scene_rig.get("meshToBone") or {},
            "partAux": [entries_by_ref[ref] for ref in refs if ref in entries_by_ref],
            "source": "rbxm",
        }
        meta_mesh = bpy.data.objects.new(get_unique_name(f"__{rig_name}Meta"), None)
        meta_mesh.empty_display_type = "PLAIN_AXES"
        meta_mesh["RigMeta"] = json.dumps(rig_meta, separators=(",", ":"))
        meta_mesh.hide_viewport = True
        meta_mesh.hide_render = True
        rig_coll.objects.link(meta_mesh)
        scaffolds.append({
            "refs": refs,
            "parts": parts,
            "meta_name": meta_mesh.name,
        })
    return scaffolds


def _create_rbxl_scene_rigs(scene_rigs, part_entries, parent_collection=None):
    """Build one Blender rig per scene rig entry (place path).

    Place meshes stay in their scene collections and are additionally linked
    into the rig's Parts collection, mirroring Studio's model/rig split."""
    parent = parent_collection or bpy.context.scene.collection
    objects_by_ref = {}
    for obj in bpy.data.objects:
        ref = obj.get("RBXInstRef")
        if ref is not None:
            objects_by_ref[int(ref)] = obj
    built = 0
    for scaffold in _create_rbxl_scene_rig_scaffolds(scene_rigs, part_entries, parent):
        for ref in scaffold["refs"]:
            obj = objects_by_ref.get(ref)
            if obj is not None:
                scaffold["parts"].objects.link(obj)
        try:
            create_rig("CONNECT", scaffold["meta_name"])
            built += 1
        except Exception as exc:
            print(
                f"[RbxmImport] rig build failed for scene model "
                f"'{scaffold['meta_name']}': {exc}"
            )
    return built


def _clamp(value, low, high):
    return max(low, min(high, value))


def _enable_imported_scene_lighting(context):
    """Make imported World/Sun lighting visible in open Material Preview views."""
    screens = {getattr(context, "screen", None), getattr(bpy.context, "screen", None)}
    for screen in screens:
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            shading = area.spaces.active.shading
            try:
                shading.use_scene_world = True
                shading.use_scene_lights = True
            except (AttributeError, TypeError):
                pass


def _roblox_sky_uv(face, u, v):
    """Map a canonical cube-face coordinate to a Roblox Sky asset's UVs.

    Roblox Sky assets are already pre-rotated at upload: Dn is counter-
    clockwise and Up is clockwise relative to a canonical cubemap.  Reading
    them back therefore requires the inverse rotations below.  Keeping this
    here means the visible cube and generated environment cannot diverge.
    """
    if face == "up":
        return v, 1.0 - u       # undo Roblox's 90 degrees clockwise upload
    if face == "down":
        return 1.0 - v, u       # undo Roblox's 90 degrees counter-clockwise upload
    return u, v


def _srgb_decode_array(pixels):
    """Decode an sRGB-encoded RGBA buffer's rgb channels to linear.

    Roblox sky faces are 8-bit sRGB images; the equirectangular output is
    tagged Linear, so the transfer curve must be applied when the faces are
    read — otherwise the world background renders with the encoded values
    (too bright in midtones) instead of the decoded ones.
    """
    try:
        import numpy as np

        arr = np.asarray(pixels, dtype=np.float32).reshape(-1, 4)
        rgb = arr[:, :3]
        low = rgb <= 0.04045
        arr = arr.copy()
        arr[:, :3] = np.where(
            low, rgb * (1.0 / 12.92), ((rgb + 0.055) * (1.0 / 1.055)) ** 2.4
        )
        return arr.reshape(-1)
    except ImportError:
        pass
    out = list(pixels)
    for i in range(0, len(out) - 3, 4):
        for c in (i, i + 1, i + 2):
            v = out[c]
            out[c] = v * (1.0 / 12.92) if v <= 0.04045 else ((v + 0.055) * (1.0 / 1.055)) ** 2.4
    return out


def _create_equirectangular_sky_image(
    images, name="RBX Sky Environment", timing=None, return_face_pixels=False,
    raw_face_pixels=None,
):
    """Convert Roblox's six cubemap faces to Blender's equirectangular world map."""
    from array import array
    from mathutils import Vector

    face_images = {key: image for key, image in images if image is not None}
    if not face_images and not raw_face_pixels:
        return None
    face_started = time.perf_counter()
    face_pixels = dict(raw_face_pixels or {})
    for key, image in face_images.items():
        if key in face_pixels:
            continue
        try:
            # `images.load` normally has pixels ready already. Reload only
            # genuinely unloaded datablocks: unconditional reloads decode all
            # six cached sky files a second time and dominate skybox setup.
            if (
                bpy.app.version[:2] < (5, 2)
                and image.filepath
                and not getattr(image, "has_data", True)
            ):
                image.reload()
            image.update()
            width, height = (int(value) for value in image.size[:2])
            if width < 1 or height < 1:
                continue
            face_pixels[key] = (width, height, _srgb_decode_array(image.pixels[:]))
        except (AttributeError, TypeError, ReferenceError, RuntimeError):
            continue
    if not face_pixels:
        return None
    if timing is not None:
        timing["face pixels"] = time.perf_counter() - face_started

    def sample(face, u, v):
        image_data = face_pixels.get(face)
        if image_data is None:
            return (0.0, 0.0, 0.0, 1.0)
        width, height, pixels = image_data
        fx = _clamp(u, 0.0, 1.0) * (width - 1)
        fy = _clamp(v, 0.0, 1.0) * (height - 1)
        x0, y0 = int(fx), int(fy)
        x1, y1 = min(width - 1, x0 + 1), min(height - 1, y0 + 1)
        tx, ty = fx - x0, fy - y0

        def rgba(x, y):
            index = 4 * (y * width + x)
            return tuple(pixels[index + channel] for channel in range(4))

        top = tuple(rgba(x0, y0)[c] * (1.0 - tx) + rgba(x1, y0)[c] * tx for c in range(4))
        bottom = tuple(rgba(x0, y1)[c] * (1.0 - tx) + rgba(x1, y1)[c] * tx for c in range(4))
        return tuple(top[c] * (1.0 - ty) + bottom[c] * ty for c in range(4))

    def cubemap_sample(direction):
        x, y, z = direction
        major = max(abs(x), abs(y), abs(z), 1e-12)
        if abs(x) == major:
            face, u, v = (("right", -z / major, y / major) if x > 0 else ("left", z / major, y / major))
        elif abs(z) == major:
            face, u, v = (("back", x / major, y / major) if z > 0 else ("front", -x / major, y / major))
        else:
            face, u, v = (("up", x / major, -z / major) if y > 0 else ("down", x / major, z / major))
        return sample(face, *_roblox_sky_uv(face, (u + 1.0) * 0.5, (v + 1.0) * 0.5))

    # 512px cubemap faces under-sample the horizon when spread across a
    # 1024px equirectangular ring; 2048x1024 keeps every output texel within
    # the source's Nyquist rate while the bilinear tap below smooths the
    # remaining fractional offsets.
    width, height = 2048, 1024
    resample_started = time.perf_counter()
    inverse_basis = get_transform_to_blender().to_3x3().inverted_safe()
    try:
        # Blender bundles NumPy. Converting all 2,097,152 environment pixels
        # in vectorized blocks is orders of magnitude cheaper than crossing
        # the Python interpreter for every direction and colour channel.
        import numpy as np

        longitude = np.linspace(
            -math.pi + math.pi / width,
            math.pi - math.pi / width,
            width,
            dtype=np.float32,
        )
        latitude = np.linspace(
            -math.pi * 0.5 + math.pi * 0.5 / height,
            math.pi * 0.5 - math.pi * 0.5 / height,
            height,
            dtype=np.float32,
        )
        cos_latitude = np.cos(latitude)[:, None]
        directions = np.empty((height, width, 3), dtype=np.float32)
        directions[:, :, 0] = np.sin(longitude)[None, :] * cos_latitude
        directions[:, :, 1] = np.cos(longitude)[None, :] * cos_latitude
        directions[:, :, 2] = np.sin(latitude)[:, None]
        basis = np.asarray(
            [[inverse_basis[row][column] for column in range(3)] for row in range(3)],
            dtype=np.float32,
        )
        directions = directions @ basis.T
        absolute = np.abs(directions)
        major_axis = np.argmax(absolute, axis=2)
        major = np.maximum(np.max(absolute, axis=2), 1e-12)
        # Any: numpy-stub flow analysis loses the ndarray narrowing through
        # the sample_face closure and falls back to the array() fallback type.
        output_pixels: Any = np.zeros((height, width, 4), dtype=np.float32)
        output_pixels[:, :, 3] = 1.0

        def sample_face(face, mask, u, v):
            image_data = face_pixels.get(face)
            if image_data is None or not np.any(mask):
                return
            source_width, source_height, pixels = image_data
            source = np.asarray(pixels, dtype=np.float32).reshape(
                source_height, source_width, 4
            )
            # Bilinear tap: point sampling here is what made imported skies
            # look crunchy at the horizon.  The floor/lerp pair costs nothing
            # compared with the scalar fallback and keeps face edges smooth.
            fx = (u[mask] + 1.0) * 0.5 * (source_width - 1)
            fy = (v[mask] + 1.0) * 0.5 * (source_height - 1)
            x0 = np.clip(np.floor(fx).astype(np.int32), 0, source_width - 1)
            y0 = np.clip(np.floor(fy).astype(np.int32), 0, source_height - 1)
            x1 = np.clip(x0 + 1, 0, source_width - 1)
            y1 = np.clip(y0 + 1, 0, source_height - 1)
            tx = (fx - x0)[:, None]
            ty = (fy - y0)[:, None]
            top = source[y0, x0] * (1.0 - tx) + source[y0, x1] * tx
            bottom = source[y1, x0] * (1.0 - tx) + source[y1, x1] * tx
            output_pixels[mask] = top * (1.0 - ty) + bottom * ty

        x, y, z = (directions[:, :, index] for index in range(3))
        x_major = major_axis == 0
        z_major = major_axis == 2
        y_major = major_axis == 1
        sample_face("right", x_major & (x > 0), -z / major, y / major)
        sample_face("left", x_major & (x <= 0), z / major, y / major)
        sample_face("back", z_major & (z > 0), x / major, y / major)
        sample_face("front", z_major & (z <= 0), -x / major, y / major)
        # Undo the rotations Roblox applies to its top and bottom assets.
        sample_face("up", y_major & (y > 0), -z / major, -(x / major))
        sample_face("down", y_major & (y <= 0), -(z / major), x / major)
        output_pixels = np.ascontiguousarray(output_pixels.reshape(-1))
    except (ImportError, MemoryError):
        output_pixels = array("f", [0.0]) * (width * height * 4)
        for y in range(height):
            latitude = math.pi * ((y + 0.5) / height - 0.5)
            cos_latitude = math.cos(latitude)
            for x in range(width):
                longitude = math.tau * ((x + 0.5) / width - 0.5)
                direction_blender = Vector((
                    math.sin(longitude) * cos_latitude,
                    math.cos(longitude) * cos_latitude,
                    math.sin(latitude),
                ))
                rgba = cubemap_sample(inverse_basis @ direction_blender)
                index = 4 * (y * width + x)
                output_pixels[index:index + 4] = array("f", rgba)

    output_started = time.perf_counter()
    if timing is not None:
        timing["resample"] = time.perf_counter() - resample_started
    image = bpy.data.images.new(name, width=width, height=height, alpha=True, float_buffer=True)
    # Blender's exact linear-space label varies with the installed OCIO config.
    for color_space in ("Linear", "Linear Rec.709", "Non-Color"):
        try:
            image.colorspace_settings.name = color_space
            break
        except TypeError:
            continue
    image.pixels.foreach_set(output_pixels)
    image.update()
    if timing is not None:
        timing["conversion + upload"] = time.perf_counter() - output_started
    return (image, face_pixels) if return_face_pixels else image


def _rbxl_collection_extent(collection):
    """Largest world-space extent of a collection's mesh objects."""
    objects = [o for o in collection.all_objects if o.type == "MESH"]
    if not objects:
        return 0.0
    mins = [float("inf")] * 3
    maxs = [float("-inf")] * 3
    from mathutils import Vector

    for obj in objects:
        try:
            matrix = obj.matrix_world
            corners = [matrix @ Vector(corner) for corner in obj.bound_box]
        except (AttributeError, RuntimeError):
            continue
        for corner in corners:
            for i in range(3):
                mins[i] = min(mins[i], corner[i])
                maxs[i] = max(maxs[i], corner[i])
    if not all(math.isfinite(mins[i]) and math.isfinite(maxs[i]) for i in range(3)):
        return 0.0
    return max(maxs[i] - mins[i] for i in range(3))


def _set_view_clip_to_extent(collection):
    """Widen every 3D viewport's clip end to fit the imported map.

    Blender's default far clip (1000 m) cuts off large place imports; the
    new clip is the map's largest world extent with a generous minimum so
    even small maps get a comfortable view distance.
    """
    extent = _rbxl_collection_extent(collection)
    if extent <= 0.0:
        return
    clip = max(extent * 2.0, 10000.0)
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != "VIEW_3D":
                    continue
                space = area.spaces.active
                if space is not None:
                    try:
                        space.clip_end = clip
                    except (AttributeError, TypeError):
                        pass
    except (AttributeError, ReferenceError):
        pass


def _create_rbxl_global_lighting(collection, lighting, skybox=None, atmosphere=None, post_effects=None):
    """Approximate Roblox Lighting with a Blender World and directional Sun.

    Roblox has separate indoor/outdoor ambient terms and a proprietary
    environment renderer, so this is intentionally an artistic conversion,
    not a claim of renderer parity.
    """
    if not lighting:
        return None

    scene = bpy.context.scene
    is_eevee = str(getattr(scene.render, "engine", "")).startswith("BLENDER_EEVEE")
    ambient = lighting.get("ambient") or (0.0, 0.0, 0.0)
    outdoor = lighting.get("outdoor_ambient") or ambient
    clock_time = float(lighting.get("clock_time", 14.0)) % 24.0
    latitude = math.radians(_clamp(float(lighting.get("geographic_latitude", 41.733)), -89.0, 89.0))
    hour_angle = math.radians((clock_time - 12.0) * 15.0)
    altitude = math.asin(math.cos(latitude) * math.cos(hour_angle))
    daylight = _clamp(math.sin(altitude), 0.0, 1.0)
    # Blender has one world irradiance.  Most imported place geometry is
    # outdoors, where Roblox uses OutdoorAmbient; retain a little Ambient as
    # a stand-in for its indoor/shadowed fill.  This is an *addition* to the
    # sky, not a replacement for it: a dim Ambient value in Roblox does not
    # extinguish the sky illumination.
    ambient_color = tuple(
        _clamp(float(ambient[i]) * 0.2 + float(outdoor[i]) * 0.8, 0.0, 1.0)
        for i in range(3)
    )
    brightness = max(0.0, float(lighting.get("brightness", 1.0)))
    diffuse = _clamp(float(lighting.get("environment_diffuse_scale", 1.0)), 0.0, 1.0)
    specular = _clamp(float(lighting.get("environment_specular_scale", 1.0)), 0.0, 1.0)

    # An emission skybox is visible to the camera but does not become world
    # irradiance in Eevee/material preview.  Its sampled colour remains the
    # base environmental contribution even when Ambient is nonzero.
    sky_color = tuple((skybox or {}).get("rbx_average_color", (0.42, 0.55, 0.82)))[:3]
    sky_fill_color = tuple(
        _clamp(float(component) * (0.35 + 0.65 * daylight), 0.0, 1.0)
        for component in sky_color
    )
    # Ambient is deliberately restrained here: Roblox's Future renderer keeps
    # the sky as the dominant outdoor source, even with a coloured Ambient.
    # It must still be additive, otherwise setting Ambient darker than the
    # sky produces the backwards result of making Blender's scene darker.
    world_color = tuple(
        _clamp(sky_fill_color[i] + ambient_color[i] * 0.12, 0.0, 1.0)
        for i in range(3)
    )

    world = bpy.data.worlds.new("RBX Lighting")
    world.use_nodes = True
    nodes = world.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputWorld")
    background = nodes.new("ShaderNodeBackground")
    background.name = "RBX Environment Lighting"
    glossy_background = nodes.new("ShaderNodeBackground")
    glossy_background.name = "RBX Environment Reflections"
    camera_background = nodes.new("ShaderNodeBackground")
    camera_background.name = "RBX Sky Background"
    sky_image = skybox.get("rbx_environment_image") if skybox else None
    sky_environment_image = bpy.data.images.get(sky_image) if sky_image else None
    if sky_image:
        environment = nodes.new("ShaderNodeTexEnvironment")
        environment.name = "RBX Sky Environment"
        environment.image = sky_environment_image
        if environment.image is not None:
            world.node_tree.links.new(environment.outputs["Color"], background.inputs["Color"])
            world.node_tree.links.new(environment.outputs["Color"], glossy_background.inputs["Color"])
            world.node_tree.links.new(environment.outputs["Color"], camera_background.inputs["Color"])
        else:
            background.inputs["Color"].default_value = (*world_color, 1.0)
            glossy_background.inputs["Color"].default_value = (*world_color, 1.0)
            camera_background.inputs["Color"].default_value = (*world_color, 1.0)
    else:
        # No imported Sky: build a Nishita sky from the Lighting's sun
        # geometry.  Its built-in scattering reads as the atmospheric haze a
        # volume approach was fighting to reproduce.  Blender 5.x removed the
        # NISHITA enum value; its physical replacement is MULTIPLE_SCATTERING.
        nishita = nodes.new("ShaderNodeTexSky")
        nishita.name = "RBX Nishita Sky"
        try:
            nishita.sky_type = "MULTIPLE_SCATTERING"
        except TypeError:
            nishita.sky_type = "NISHITA"
        nishita.sun_elevation = math.degrees(altitude)
        nishita.sun_rotation = math.degrees(hour_angle)
        try:
            if atmosphere:
                nishita.air_density = 1.0 + float(atmosphere.get("density", 0.0)) * 0.5
                nishita.dust_density = float(atmosphere.get("haze", 0.0)) * 0.5
        except (AttributeError, TypeError, ValueError):
            pass
        world.node_tree.links.new(nishita.outputs["Color"], background.inputs["Color"])
        world.node_tree.links.new(nishita.outputs["Color"], glossy_background.inputs["Color"])
        world.node_tree.links.new(nishita.outputs["Color"], camera_background.inputs["Color"])
    if is_eevee:
        # Eevee stores world lighting in an internal indirect-light probe.  It
        # cannot evaluate the Cycles Light Path ray split below, which left
        # imported HDR skies visibly present but absent from surface lighting.
        # Feed the assembled sky straight to World Output so Eevee builds the
        # probe from it.  The locked Roblox client-camera calibration chose a
        # nominal strength of 1.0; the former fake Sun dome is not needed.
        # Side-by-side against Studio's default Lighting.Brightness (2), a
        # world strength of 1.0 matches the client's sky and surface fill.
        # The earlier 0.76 Crossroads camera fit under-lit everything that was
        # not in direct sun; drop it rather than keep dimming imported scenes.
        background.inputs["Strength"].default_value = 1.0
        world.node_tree.links.new(background.outputs["Background"], output.inputs["Surface"])
        world["rbx_eevee_world_probe"] = True
    else:
        # Cycles supports distinct diffuse, glossy and camera ray paths.
        # Keep the camera sky independent from illumination. Roblox's
        # EnvironmentDiffuseScale/EnvironmentSpecularScale modulate the sky's
        # *lighting* contribution, not its visible texture. A baseline avoids
        # turning the world black at scale zero; Cycles then handles actual
        # occlusion through geometry rather than an Eevee world probe.
        background.inputs["Strength"].default_value = 0.50 + 0.15 * diffuse
        glossy_background.inputs["Strength"].default_value = 0.10 + 0.40 * specular
        camera_background.inputs["Strength"].default_value = 1.0
        ambient_background = nodes.new("ShaderNodeBackground")
        ambient_background.name = "RBX Ambient Fill"
        ambient_background.inputs["Color"].default_value = (*ambient_color, 1.0)
        # Ambient and OutdoorAmbient are deliberately a modest separate fill,
        # never a multiplier on the visible or environment sky.
        ambient_background.inputs["Strength"].default_value = 0.15
        light_path = nodes.new("ShaderNodeLightPath")
        light_path.name = "RBX Camera Sky Split"
        diffuse_fill = nodes.new("ShaderNodeAddShader")
        diffuse_fill.name = "RBX Diffuse Sky + Ambient"
        world.node_tree.links.new(background.outputs["Background"], diffuse_fill.inputs[0])
        world.node_tree.links.new(ambient_background.outputs["Background"], diffuse_fill.inputs[1])
        environment_mix = nodes.new("ShaderNodeMixShader")
        environment_mix.name = "RBX Diffuse / Specular Environment"
        world.node_tree.links.new(light_path.outputs["Is Glossy Ray"], environment_mix.inputs[0])
        world.node_tree.links.new(diffuse_fill.outputs["Shader"], environment_mix.inputs[1])
        world.node_tree.links.new(glossy_background.outputs["Background"], environment_mix.inputs[2])
        camera_mix = nodes.new("ShaderNodeMixShader")
        camera_mix.name = "RBX Camera Sky / Environment Lighting"
        world.node_tree.links.new(light_path.outputs["Is Camera Ray"], camera_mix.inputs[0])
        world.node_tree.links.new(environment_mix.outputs["Shader"], camera_mix.inputs[1])
        world.node_tree.links.new(camera_background.outputs["Background"], camera_mix.inputs[2])
        world.node_tree.links.new(camera_mix.outputs["Shader"], output.inputs["Surface"])
    world["rbx_ambient"] = list(ambient[:3])
    world["rbx_outdoor_ambient"] = list(outdoor[:3])
    world["rbx_environment_fill_color"] = list(world_color)
    world["rbx_environment_diffuse_scale"] = diffuse
    world["rbx_environment_specular_scale"] = specular
    world["rbx_fog_color"] = list((lighting.get("fog_color") or (0.75, 0.75, 0.75))[:3])
    world["rbx_fog_start"] = float(lighting.get("fog_start", 0.0))
    world["rbx_fog_end"] = float(lighting.get("fog_end", 100000.0))
    scene.world = world
    _set_view_clip_to_extent(collection)
    eevee = getattr(scene, "eevee", None)
    # Blender 5.0.0-5.1.0 on the Vulkan backend renders Eevee surfaces black
    # on NVIDIA 595.71+ drivers (projects.blender.org issues/155371).  The
    # add-on cannot fix a driver bug; warn once per import so the imported
    # scene is not mistaken for broken materials.
    _app_version = getattr(bpy.app, "version", (0,))
    if getattr(bpy.app, "backend", "") == "VULKAN" and (
        _app_version[:2] == (5, 0) or _app_version[:3] == (5, 1, 0)
    ):
        print(
            "[RbxLighting] warning: Blender %d.%d Vulkan renders Eevee "
            "surfaces black on NVIDIA drivers 595.71+ (Blender issue 155371, "
            "fixed in 5.1.1). Update Blender or switch the GPU backend to "
            "OpenGL if imported parts look black."
            % (bpy.app.version[0], bpy.app.version[1])
        )
    # Shadows, AO, and their resolutions are deliberately left alone: forcing
    # 4096 cascades and 2048 cube shadows per light blew GPU memory on
    # place-sized scenes, and forced GTAO doubled up on the world probe's
    # contact shading. Only ray-tracing options are tuned, and only when the
    # user already has ray tracing enabled.
    if eevee is not None:
        if is_eevee and hasattr(eevee, "use_raytracing"):
            # Ray tracing is deliberately left to the user: enabling it on
            # import made the viewport sluggish on modest GPUs and surprised
            # people who had it off. Only tune when already opted in.
            if eevee.use_raytracing:
                try:
                    if hasattr(eevee, "ray_tracing_method"):
                        eevee.ray_tracing_method = "SCREEN"
                except (TypeError, ValueError):
                    pass
                options = getattr(eevee, "ray_tracing_options", None)
                if options is not None:
                    for name, value in (
                        ("resolution_scale", "1"),
                        ("screen_trace_quality", 0.75),
                        ("screen_trace_thickness", 0.5),
                        # Rough Roblox Plastic should use Fast GI's ambient-
                        # occlusion fallback. A max roughness of 1.0 traces
                        # every BSDF and disables that fallback completely.
                        ("trace_max_roughness", 0.65),
                        ("use_denoise", True),
                    ):
                        if hasattr(options, name):
                            try:
                                setattr(options, name, value)
                            except (TypeError, ValueError):
                                pass
                # Screen traces retain accurate glossy/smoother surfaces;
                # Fast GI supplies the missing diffuse contact occlusion for
                # rough parts.  Blender 5.2 rebalanced Fast GI energy
                # conservation (projects.blender.org issues/161472, PR
                # 156597): the settings tuned against 5.1 render noticeably
                # darker there.  The sanctioned mitigation is higher step/ray
                # counts; parity with the 5.1 look is not guaranteed by
                # design.
                gi_is_52 = getattr(bpy.app, "version", (0,)) >= (5, 2, 0)
                gi_ray_count = 12 if gi_is_52 else 8
                gi_step_count = 32 if gi_is_52 else 16
                gi_quality = 1.0 if gi_is_52 else 0.75
                for name, value in (
                    ("use_fast_gi", True),
                    ("fast_gi_method", "AMBIENT_OCCLUSION_ONLY"),
                    ("fast_gi_resolution", "1"),
                    ("fast_gi_ray_count", gi_ray_count),
                    ("fast_gi_step_count", gi_step_count),
                    ("fast_gi_quality", gi_quality),
                    # Keep this as local contact occlusion. Large thickness
                    # values make distant map geometry over-block the
                    # environment light.
                    ("fast_gi_distance", 12.0),
                    ("fast_gi_thickness_near", 0.12),
                    ("fast_gi_thickness_far", 0.45),
                ):
                    if hasattr(eevee, name):
                        try:
                            setattr(eevee, name, value)
                        except (TypeError, ValueError):
                            pass
            world["rbx_eevee_raytracing"] = bool(eevee.use_raytracing)

    # Brightness drives direct-light strength.  Do not add a global display
    # exposure offset here: a Crossroads-specific sweep improved that map but
    # overexposed the controlled fixture, proving it is not a general mapping.
    correction = (post_effects or {}).get("color_correction") or {}
    # Studio's ExposureCompensation and ColorCorrection.Brightness are both
    # stop-like offsets that Blender's scene exposure reproduces 1:1, so the
    # values pass through unchanged.  Contrast/saturation stay metadata:
    # Blender's scene color management cannot apply them to the interactive
    # material-preview path without taking over the user's compositor.
    exposure = float(lighting.get("exposure_compensation", 0.0))
    if correction.get("enabled", True):
        exposure += float(correction.get("brightness", 0.0))
    scene.view_settings.exposure = _clamp(exposure, -10.0, 10.0)
    # Khronos PBR Neutral renders the imported PBR materials the way they
    # were authored, with Medium Contrast for a tame highlight rolloff.
    # Fall back to Standard on older bundled OCIO configs that lack it.
    try:
        scene.view_settings.view_transform = "Khronos PBR Neutral"
        scene.view_settings.look = "Medium Contrast"
    except (TypeError, ValueError):
        try:
            scene.view_settings.view_transform = "Standard"
        except (TypeError, ValueError):
            pass
    for effect_name, effect in (post_effects or {}).items():
        for key, value in effect.items():
            world[f"rbx_{effect_name}_{key}"] = value

    data = bpy.data.lights.new("RBX Sun", "SUN")
    # Anchor Roblox's default Lighting.Brightness (2) to Blender's sensible
    # daylight Sun strength (1), while retaining intentional brightness edits
    # as a simple linear scale in either renderer.
    data.energy = max(0.0, brightness) * 0.5
    data.use_shadow = bool(lighting.get("global_shadows", True))
    data.angle = math.radians(0.5 + 10.0 * _clamp(float(lighting.get("shadow_softness", 0.0)), 0.0, 1.0))
    sun = bpy.data.objects.new("RBX Sun", data)
    collection.objects.link(sun)

    # An equinox sun model is close enough for Roblox's time-of-day look.
    # Convert the source direction through the same coordinate basis as parts.
    from mathutils import Vector
    source_rbx = Vector((
        -math.sin(hour_angle) * math.cos(altitude),
        math.sin(altitude),  # Roblox is Y-up (Blender's Z after conversion).
        -math.cos(hour_angle) * math.cos(altitude),
    ))
    source_blender = get_transform_to_blender().to_3x3() @ source_rbx
    # Rig import maps Roblox +Z to Blender -Y.  Roblox's time-of-day azimuth
    # uses the opposite north/south handedness, so mirror only that horizontal
    # Blender component; do not touch the already-correct vertical Z axis.
    source_blender.y *= -1.0
    source_blender.normalize()
    sun.rotation_mode = "QUATERNION"
    sun.rotation_quaternion = (-source_blender).to_track_quat("-Z", "Y")
    return sun


def _create_rbxl_lights(collection, scene_lights, part_entries):
    from mathutils import Vector

    # Roblox local lights (PointLight and SpotLight) share the same influence
    # envelope: Range is not a hard photometric cutoff.  The light remains
    # useful until roughly twice that distance, with the visible falloff
    # beginning around the configured range.  Blender's custom distance is a
    # hard outer boundary over its already inverse-square illumination, so the
    # cutoff is pushed out to where the envelope contributes almost nothing;
    # the in-range falloff then reads as a smooth fade rather than a hard edge.
    _BLENDER_RANGE_ENVELOPE_SCALE = 2.0

    def spotlight_brightness_response(brightness):
        """Map Roblox SpotLight.Brightness to calibrated Blender intensity.

        The two engines do not share a photometric unit or display response.
        These knots are an empirical fit to the sealed LightingTest.rbxl
        Brightness sweep (Range=16, Angle=45) at the matching Studio camera.
        Interpolation keeps normal user values smooth while still preserving a
        linear high-brightness tail instead of silently crushing bright lights.
        """
        knots = (
            (0.0, 0.0), (0.25, 0.50), (0.5, 0.55), (1.0, 0.65),
            (2.0, 1.00), (5.0, 2.70), (10.0, 9.50), (20.0, 19.20),
        )
        value = max(0.0, float(brightness))
        for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
            if value <= x1:
                return y0 + (y1 - y0) * ((value - x0) / (x1 - x0))
        # The final two knots establish the high-intensity slope; extrapolate
        # it rather than clipping valid Brightness values above 20.
        x0, y0 = knots[-2]
        x1, y1 = knots[-1]
        return y1 + (value - x1) * ((y1 - y0) / (x1 - x0))

    def spotlight_range_response(range_studs):
        """Empirical endpoint-energy correction for Roblox SpotLight.Range.

        Roblox's Range is a filtered influence envelope, whereas Blender uses
        inverse-square illumination plus a cutoff.  The sealed client-renderer
        sweep measured the probe exactly one Roblox range from the source.
        The large high-range multipliers exist because the spot fit was taken
        with the cone probe at the old 1x cutoff kill-edge; they remain the
        user-validated spot calibration.

        The short-range knots are floored at 0.15 rather than the raw fit
        (~0): the sweep only measured at exactly one range from the source,
        and zeroing the curve there would make short-range lights import as
        invisible even though they light nearer surfaces in Roblox.
        """
        knots = (
            (0.0, 0.0), (4.0, 0.15), (8.0, 0.15),
            (16.0, 0.90), (24.0, 12.8), (32.0, 16.8),
        )
        value = max(0.0, float(range_studs))
        for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
            if value <= x1:
                return y0 + (y1 - y0) * ((value - x0) / (x1 - x0))
        return min(20.0, knots[-1][1] + (value - 32.0) * 0.25)

    def pointlight_range_response(range_studs):
        """Empirical endpoint-energy correction for Roblox PointLight.Range.

        Fitted from an unconfounded EEVEE sweep of the sealed 04_point_range
        fixture (host parts excluded, 2x cutoff, current energy scale): the
        client pools read about 1.4x and 1.75x brighter than Blender at Range
        24 and 32, so the tail rises gently.  Points spread over the full
        sphere and do not carry the spot cone's concentration, so the
        multipliers stay far below the spot curve's.

        Short ranges are floored at 0.15 for the same reason as the spot
        curve: the client sweep reads nearly black at exactly one range for
        Range <= 8, but zeroing the curve would kill near-field light.
        """
        knots = (
            (0.0, 0.0), (4.0, 0.15), (8.0, 0.15),
            (16.0, 1.00), (24.0, 1.40), (32.0, 1.80),
        )
        value = max(0.0, float(range_studs))
        for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
            if value <= x1:
                return y0 + (y1 - y0) * ((value - x0) / (x1 - x0))
        return min(4.0, knots[-1][1] + (value - 32.0) * 0.05)

    def spotlight_angle_response(angle_degrees):
        """Return Blender cone size, energy correction, and edge softness.

        The client-renderer angle fixture (15/30/45/60 degrees, five lateral
        probes at a 15.75-stud throw) shows Roblox's cone is wider and has a
        much softer penumbra than Blender's equivalent spot_size.  Roblox's
        90-degree fixture did not render a stable local-light result, so keep
        the measured curve bounded at 60 degrees instead of extrapolating a
        made-up wide-cone response.
        """
        knots = (
            (0.0, 0.0, 0.80),
            (15.0, 0.38, 0.80),
            (30.0, 0.78, 1.20),
            (45.0, 1.12, 1.55),
            (60.0, 1.50, 1.70),
        )
        value = _clamp(float(angle_degrees), 0.0, 60.0)
        for (x0, size0, energy0), (x1, size1, energy1) in zip(knots, knots[1:]):
            if value <= x1:
                t = (value - x0) / (x1 - x0)
                return size0 + (size1 - size0) * t, energy0 + (energy1 - energy0) * t, 0.70
        return knots[-1][1], knots[-1][2], 0.70

    # Enum.NormalId: Right, Top, Back, Left, Bottom, Front.
    face_normals = {
        0: Vector((1, 0, 0)), 1: Vector((0, 1, 0)), 2: Vector((0, 0, 1)),
        3: Vector((-1, 0, 0)), 4: Vector((0, -1, 0)), 5: Vector((0, 0, -1)),
    }
    entries_by_ref = {entry.get("inst_ref"): entry for entry in part_entries if isinstance(entry, dict)}
    created = 0
    for light in scene_lights or []:
        if not light.get("enabled", True):
            continue
        parent = entries_by_ref.get(light.get("parent_ref"))
        if parent is None:
            continue
        kind = light.get("class_name")
        blender_type = "POINT" if kind == "PointLight" else ("SPOT" if kind == "SpotLight" else "AREA")
        data = bpy.data.lights.new(light.get("name") or kind, blender_type)
        data.color = tuple(light.get("color") or (1.0, 1.0, 1.0))
        brightness = max(0.0, float(light.get("brightness", 1.0)))
        # Point and spot lights have different Blender emitter models, but the
        # Roblox-side envelope (Range, Brightness) is shared and the sweeps
        # were compared against sealed client-renderer captures; the client
        # lights run slightly brighter and blendier, so the Blender scales
        # carry a small upward correction.
        roblox_range = max(0.01, float(light.get("range", 16.0)))
        roblox_angle = float(light.get("angle", 45.0))
        spot_size, angle_energy, spot_blend = spotlight_angle_response(roblox_angle)
        if blender_type == "SPOT":
            data.energy = (
                spotlight_brightness_response(brightness)
                * spotlight_range_response(roblox_range)
                * angle_energy
                * 135.0
            )
        elif blender_type == "POINT":
            data.energy = brightness * 850.0 * pointlight_range_response(roblox_range)
        else:
            data.energy = brightness * 850.0
        data.use_shadow = bool(light.get("shadows"))
        if hasattr(data, "cutoff_distance"):
            data.cutoff_distance = roblox_range * _BLENDER_RANGE_ENVELOPE_SCALE
            data.use_custom_distance = True
        if blender_type == "SPOT":
            data.spot_size = min(3.14159, max(0.01745, spot_size))
            data.spot_blend = spot_blend
            data["rbx_range"] = roblox_range
            data["rbx_angle"] = roblox_angle
            data["rbx_cutoff_scale"] = _BLENDER_RANGE_ENVELOPE_SCALE
        if blender_type == "AREA":
            data.shape = "DISK"
            data.size = max(0.01, float(light.get("range", 16.0)))
        obj = bpy.data.objects.new(light.get("name") or kind, data)
        try:
            parent_matrix = get_transform_to_blender() @ cf_to_mat(parent.get("part_cf"))
            obj.matrix_world = parent_matrix
            if blender_type in ("SPOT", "AREA"):
                # Blender emits its Spot/Area light down local -Z.  Reorient it
                # to Roblox's requested Face after applying the part CFrame.
                normal_rbx = face_normals.get(int(light.get("face", 5)), face_normals[5])
                normal_world = (parent_matrix.to_3x3() @ normal_rbx).normalized()
                # A light parented to a BasePart originates at the selected
                # face, not in the middle of the part.  Range is measured from
                # that emitting face, so moving this origin is essential for
                # lamps, headlights, and wall-mounted spots to agree with
                # Roblox without arbitrarily altering Range.
                size = parent.get("part_size") or (0.0, 0.0, 0.0)
                try:
                    axis = max(range(3), key=lambda index: abs(normal_rbx[index]))
                    face_offset = normal_rbx * (max(0.0, float(size[axis])) * 0.5 + 0.001)
                    obj.location = parent_matrix @ face_offset
                except (TypeError, ValueError, IndexError):
                    pass
                obj.rotation_mode = "QUATERNION"
                obj.rotation_quaternion = normal_world.to_track_quat("-Z", "Y")
        except Exception:
            pass
        collection.objects.link(obj)
        created += 1
    return created


def _skybox_average_color(images):
    """Return an alpha-aware, evenly sampled linear colour from Sky faces."""
    samples = []
    for _, image in images:
        if image is None:
            continue
        try:
            width, height = (int(value) for value in image.size[:2])
            if width < 1 or height < 1:
                continue
            pixels = image.pixels[:]
        except (AttributeError, TypeError, ReferenceError, RuntimeError):
            continue
        step_x = max(1, width // 32)
        step_y = max(1, height // 32)
        for y in range(step_y // 2, height, step_y):
            for x in range(step_x // 2, width, step_x):
                offset = 4 * (y * width + x)
                alpha = pixels[offset + 3] if offset + 3 < len(pixels) else 1.0
                samples.append((pixels[offset] * alpha, pixels[offset + 1] * alpha, pixels[offset + 2] * alpha))
    if not samples:
        return None
    return tuple(sum(color[index] for color in samples) / len(samples) for index in range(3))


def _skybox_average_color_from_pixels(face_pixels):
    """Average already-decoded linear sky faces without another RNA copy."""
    samples = []
    for width, height, pixels in face_pixels.values():
        step_x = max(1, width // 32)
        step_y = max(1, height // 32)
        for y in range(step_y // 2, height, step_y):
            for x in range(step_x // 2, width, step_x):
                offset = 4 * (y * width + x)
                alpha = pixels[offset + 3]
                samples.append((
                    pixels[offset] * alpha,
                    pixels[offset + 1] * alpha,
                    pixels[offset + 2] * alpha,
                ))
    if not samples:
        return None
    return tuple(sum(color[index] for color in samples) / len(samples) for index in range(3))


def _create_rbxl_skybox(collection, sky, part_entries):
    """Resolve a Roblox Sky instance into the world environment image.

    The six face images feed the equirectangular environment image that the
    World shader samples directly.  The old six-face emission cube rendered
    the SAME sky a second time (and caused the black-sky-inside-the-cube
    class of bugs); it no longer exists.
    Returns a metadata dict consumed by the lighting builder, or None.
    """
    from ..rig.textures import (
        fetch_texture_image, prefetched_raw_pixels, release_prefetched_raw_pixels,
    )

    faces = ["right", "left", "front", "back", "up", "down"]
    timing = {}
    fetch_started = time.perf_counter()
    images = []
    raw_faces = {}
    for name in faces:
        texture_ref = sky.get(name)
        if not texture_ref:
            continue
        raw = prefetched_raw_pixels(texture_ref)
        if raw is not None:
            width, height, _alpha, pixels = raw
            raw_faces[name] = (width, height, _srgb_decode_array(pixels))
            release_prefetched_raw_pixels(texture_ref)
            images.append((name, None))
        else:
            images.append((name, fetch_texture_image(texture_ref, name=f"sky_{name}")))
    timing["image bind"] = time.perf_counter() - fetch_started
    if not images:
        return None
    environment_result = _create_equirectangular_sky_image(
        images, timing=timing, return_face_pixels=True, raw_face_pixels=raw_faces
    )
    if environment_result is None:
        return None
    environment_image, face_pixels = environment_result
    meta = {"rbx_environment_image": environment_image.name}
    average_started = time.perf_counter()
    average_color = _skybox_average_color_from_pixels(face_pixels)
    timing["average"] = time.perf_counter() - average_started
    if average_color is not None:
        meta["rbx_average_color"] = average_color
    print(
        "[RbxSky] Build detail: "
        + ", ".join(f"{key} {value:.2f}s" for key, value in timing.items())
    )
    return meta


def _sample_sequence(seq, t, default):
    """Sample a roblox NumberSequence/ColorSequence keypoint list at t."""
    if not isinstance(seq, (list, tuple)) or not seq:
        return default
    try:
        if isinstance(seq[0], (list, tuple)) and len(seq[0]) >= 2 and isinstance(seq[0][0], (int, float)):
            keypoints = [(float(k[0]), k[1]) for k in seq]
            if len(keypoints) == 1:
                return keypoints[0][1]
            for (t0, v0), (t1, v1) in zip(keypoints, keypoints[1:]):
                if t0 <= t <= t1:
                    f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
                    if isinstance(v0, (list, tuple)):
                        return tuple(v0[c] * (1.0 - f) + v1[c] * f for c in range(3))
                    return float(v0) * (1.0 - f) + float(v1) * f
            return keypoints[-1][1] if t >= keypoints[-1][0] else keypoints[0][1]
    except (TypeError, ValueError, IndexError):
        return default
    return default


def _create_rbx_decals(collection, part_entries):
    """Create projected planes for supported Roblox Decal instances."""
    from ..rig.textures import fetch_texture_image, _color_image_view

    # Roblox NormalId enum: Right, Top, Back, Left, Bottom, Front.
    # Each entry is (normal, axis_u, axis_v, normal_axis, size_u_axis,
    # size_v_axis).  The U/V axes follow Roblox's own face basis — the same
    # one its OBJ export uses (see primitive_shapes._block_canonical_stud_uvs):
    # side faces run V up (+Y); the top and bottom faces anchor the image top
    # at the part's Back (+Z) edge and run U toward -X.
    faces = {
        0: ((1, 0, 0), (0, 0, -1), (0, 1, 0), 0, 2, 1),
        1: ((0, 1, 0), (-1, 0, 0), (0, 0, 1), 1, 0, 2),
        2: ((0, 0, 1), (1, 0, 0), (0, 1, 0), 2, 0, 1),
        3: ((-1, 0, 0), (0, 0, 1), (0, 1, 0), 0, 2, 1),
        4: ((0, -1, 0), (-1, 0, 0), (0, 0, 1), 1, 0, 2),
        5: ((0, 0, -1), (-1, 0, 0), (0, 1, 0), 2, 0, 1),
    }
    created = 0
    transform_to_blender = get_transform_to_blender()
    for entry in part_entries:
        # Classic heads (plain Part, no SpecialMesh) carry the decal as
        # face_decal and composite it through the clothing path.  Dynamic
        # heads never get face_decal, so their Decal instances must still
        # build as projected planes here.
        if entry.get("name") == "Head" and entry.get("face_decal"):
            continue
        for index, decal in enumerate(entry.get("decals") or ()):
            if not isinstance(decal, dict):
                continue
            face = faces.get(int(decal.get("face", 5)))
            texture_ref = decal.get("texture")
            if face is None or not texture_ref or not entry.get("part_cf"):
                continue
            normal, axis_u, axis_v, normal_axis, size_u_axis, size_v_axis = face
            size = entry.get("part_size") or (1.0, 1.0, 1.0)
            try:
                half_u = float(size[size_u_axis]) * 0.5
                half_v = float(size[size_v_axis]) * 0.5
                half_n = float(size[normal_axis]) * 0.5
                transform = transform_to_blender @ cf_to_mat(entry["part_cf"])
            except (TypeError, ValueError, IndexError):
                continue
            offset = Vector(normal) * (half_n + 0.001)
            corners = [
                offset - Vector(axis_u) * half_u - Vector(axis_v) * half_v,
                offset + Vector(axis_u) * half_u - Vector(axis_v) * half_v,
                offset + Vector(axis_u) * half_u + Vector(axis_v) * half_v,
                offset - Vector(axis_u) * half_u + Vector(axis_v) * half_v,
            ]
            mesh = bpy.data.meshes.new(f"decal_{entry.get('name', 'part')}")
            mesh.from_pydata([transform @ corner for corner in corners], [], [(0, 1, 2, 3)])
            mesh.uv_layers.new(name="UVMap")
            mesh.uv_layers[0].data.foreach_set("uv", (0, 0, 1, 0, 1, 1, 0, 1))
            material = bpy.data.materials.new(f"decal_{entry.get('name', 'part')}")
            material.use_nodes = True
            principled = material.node_tree.nodes.get("Principled BSDF")
            image = fetch_texture_image(texture_ref, name=f"{entry.get('name', 'part')}_decal")
            if image is not None:
                image = _color_image_view(image)
            tex = material.node_tree.nodes.new("ShaderNodeTexImage")
            tex.image = image
            links = material.node_tree.links
            # A linked socket ignores its default value, so the decal's tint
            # and transparency must ride explicit multiply nodes — the same
            # structure the texture-instance materials use.
            tint_mix = material.node_tree.nodes.new("ShaderNodeMix")
            tint_mix.data_type = "RGBA"
            tint_mix.blend_type = "MULTIPLY"
            tint_mix.name = "RBX Decal Tint"
            tint_mix.label = "RBX decal tint (texture x instance Color3)"
            tint_mix.inputs["Factor"].default_value = 1.0
            tint_raw: Any = decal.get("color") or (1.0, 1.0, 1.0)
            try:
                tint_vals = tuple(
                    max(0.0, min(1.0, float(component)))
                    for component in list(tint_raw)[:3]
                )
            except (TypeError, ValueError, IndexError):
                tint_vals = (1.0, 1.0, 1.0)
            tint_mix.inputs["A"].default_value = (*tint_vals, 1.0)
            if image is not None:
                links.new(tex.outputs["Color"], tint_mix.inputs["B"])
            else:
                tint_mix.inputs["B"].default_value = (1.0, 1.0, 1.0, 1.0)
            links.new(
                tint_mix.outputs["Result"],
                principled.inputs["Base Color"],
            )
            alpha_math = material.node_tree.nodes.new("ShaderNodeMath")
            alpha_math.operation = "MULTIPLY"
            alpha_math.name = "RBX Decal Alpha"
            alpha_math.label = "RBX decal alpha (texture a x (1 - transparency))"
            try:
                decal_alpha = 1.0 - max(
                    0.0, min(1.0, float(decal.get("transparency", 0.0)))
                )
            except (TypeError, ValueError):
                decal_alpha = 1.0
            alpha_math.inputs[1].default_value = decal_alpha
            if image is not None:
                links.new(tex.outputs["Alpha"], alpha_math.inputs[0])
            else:
                alpha_math.inputs[0].default_value = 1.0
            links.new(alpha_math.outputs["Value"], principled.inputs["Alpha"])
            # Decals always sample texture alpha: sorted blending (BLENDED),
            # matching the other transparent materials — dithered sorting
            # fights overlapping decals and reads as overdraw artifacts.
            try:
                material.surface_render_method = "BLENDED"
            except Exception:
                pass
            try:
                material.blend_method = "BLEND"
            except Exception:
                pass
            try:
                material.shadow_method = "HASHED"
            except (AttributeError, TypeError, ValueError):
                pass
            obj = bpy.data.objects.new(f"{entry.get('name', 'Part')}_Decal.{index:03d}", mesh)
            mesh.materials.append(material)
            collection.objects.link(obj)
            obj["RBXDecal"] = True
            created += 1
    return created


def _create_rbxl_beams(
    collection, beams, defer_images=False, deferred_image_materials=None
):
    """Build tapered emission strips for Roblox Beam instances.

    Static recreation: endpoint widths and the cubic Bezier curve defined by
    CurveSize0/1 and each attachment's local X axis,
    Segment count and TextureLength tiling.  FaceCamera beams are true
    billboards: their ribbon plane is oriented toward the render camera while
    both attachment endpoints remain fixed. ColorSequence gradients bake into vertex colours, the
    NumberSequence transparency bakes into vertex alpha, and LightInfluence
    mixes the pure emission (0) toward a lit Principled shader (1).
    TextureSpeed scrolling is not represented yet.
    Returns the number of beams built.
    """
    from mathutils import Vector

    from ..core.utils import cf_to_mat
    from ..rig.creation import _new_mesh_color_attribute, _populate_mesh_geometry
    from ..rig.textures import fetch_texture_image

    t2b = get_transform_to_blender()
    created = 0
    material_cache = {}
    beam_timing = {
        "geometry": 0.0, "image": 0.0, "material": 0.0, "link": 0.0,
    }

    def finish_beam_object(mesh, name, material):
        mesh.materials.append(material)
        obj = bpy.data.objects.new(f"RBX Beam {name}", mesh)
        collection.objects.link(obj)
        if hasattr(obj, "visible_shadow"):
            obj.visible_shadow = False

    for beam in beams or []:
        name = str(beam.get("name") or f"beam_{created}")
        try:
            geometry_started = time.perf_counter()

            def endpoint_matrix(endpoint):
                matrix = cf_to_mat(endpoint["cf"])
                if endpoint.get("part_cf"):
                    matrix = cf_to_mat(endpoint["part_cf"]) @ matrix
                return t2b @ matrix

            m0 = endpoint_matrix(beam["attachment0"])
            m1 = endpoint_matrix(beam["attachment1"])
            p0 = m0.translation
            p1 = m1.translation
            chord = p1 - p0
            length = chord.length
            if length < 1e-6:
                continue
            axis = chord / length
            up = Vector((0.0, 0.0, 1.0))
            if abs(axis.dot(up)) > 0.99:
                up = Vector((1.0, 0.0, 0.0))
            perp = axis.cross(up).normalized()

            segments = int(beam.get("segments") or 10)
            face_camera = bool(beam.get("face_camera"))

            def beam_float(key, default):
                value = beam.get(key)
                try:
                    return float(value) if value is not None else default
                except (TypeError, ValueError):
                    return default

            # Roblox CFrame axes are its *columns*.  ``cf_to_mat`` preserves
            # the serialized row-major rotation matrix, so transform a local
            # basis vector with the matrix directly; transposing here reads a
            # row as an axis and mirrors/twists rotated attachments.
            def world_x_axis(matrix):
                return matrix.to_3x3() @ Vector((1.0, 0.0, 0.0))

            # The four cubic-Bezier control points used by Roblox.  CurveSize
            # controls position, not width.  The old code added it to width,
            # which also hid the attachment-rotation bug for curved beams.
            control0 = p0
            control1 = p0 + world_x_axis(m0) * beam_float("curve_size0", 0.0)
            control2 = p1 - world_x_axis(m1) * beam_float("curve_size1", 0.0)
            control3 = p1

            def curve_position(t):
                inv_t = 1.0 - t
                return (
                    control0 * (inv_t * inv_t * inv_t)
                    + control1 * (3.0 * inv_t * inv_t * t)
                    + control2 * (3.0 * inv_t * t * t)
                    + control3 * (t * t * t)
                )

            # Without FaceCamera, roblox draws the strip in the plane
            # spanned by the chord and the attachment's X axis, falling back
            # to its Y axis when the beam runs along X (city attachments are
            # axis-aligned either way, so authors face window beams outward
            # by rotating the attachments).  The cross-section twists from
            # Attachment0 to Attachment1 across the beam. FaceCamera needs a
            # camera-facing width vector, but must still retain BOTH endpoint
            # positions. A Blender tracking constraint rotates the whole
            # object and therefore moves its second endpoint; use world-space
            # geometry instead.
            side0 = None
            side1 = None
            twist = 0.0
            if not face_camera:
                def side_axis(matrix):
                    # CFrame axes are matrix columns.  The
                    # projection must keep a meaningful length: a beam
                    # running along the axis leaves only float noise, which
                    # would otherwise become an arbitrary collapsed plane.
                    for axis_vector in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
                        w = matrix.to_3x3() @ Vector(axis_vector)
                        w = w - axis * w.dot(axis)
                        if w.length > 0.05:
                            return w.normalized()
                    return None

                side0 = side_axis(m0)
                side1 = side_axis(m1)
                if side0 is not None and side1 is not None:
                    twist = math.atan2(
                        side0.cross(side1).dot(axis), side0.dot(side1)
                    )
            if side0 is None:
                side0 = perp
            if face_camera:
                try:
                    camera = bpy.context.scene.camera
                    to_camera = camera.matrix_world.translation - p0.lerp(p1, 0.5)
                    to_camera -= axis * to_camera.dot(axis)
                    if to_camera.length > 1e-6:
                        # side × axis points toward the camera, so this is
                        # the width axis of the camera-facing ribbon.
                        side0 = axis.cross(to_camera).normalized()
                except (AttributeError, ReferenceError, TypeError, ValueError):
                    pass

            beam_width0 = beam_float("width0", 0.1)
            beam_width1 = beam_float("width1", 0.1)
            texture_length = beam_float("texture_length", 0.0)
            texture_mode = int(beam.get("texture_mode") or 0)
            # TextureMode: Static=0, Stretch=1, Wrap=2.  Wrap tiles the
            # texture every TextureLength studs; both others stretch it over
            # the full length (roblox stretches a zero TextureLength too).
            wrap = texture_mode == 2 and texture_length > 1e-4
            verts_per_ring = 2
            vertices = []
            uvs = []
            ring_colors = []
            for ring in range(segments + 1):
                t = ring / segments
                width = beam_width0 * (1.0 - t) + beam_width1 * t
                half = width * 0.5
                u = t * (length / texture_length) if wrap else t
                pos = curve_position(t)
                if not face_camera and abs(twist) > 1e-9:
                    angle = t * twist
                    ca, sa = math.cos(angle), math.sin(angle)
                    w = side0 * ca + axis.cross(side0) * sa
                    ring_offsets = [w, -w]
                else:
                    ring_offsets = [side0, -side0]
                for side, offset in enumerate(ring_offsets):
                    vertices.append((pos + offset * half)[:])
                    uvs.append((u, 1.0 if side == 0 else 0.0))
                color = _sample_sequence(beam.get("color_seq"), t, (1.0, 1.0, 1.0))
                transparency = _sample_sequence(
                    beam.get("transparency_seq"), t, (0.0, 0.0, 0.0)
                )
                try:
                    # Scalar sequences sample to a bare float; colour
                    # sequences and the fallback sample to a tuple.
                    transparency_value = (
                        transparency[0]
                        if isinstance(transparency, (list, tuple))
                        else transparency
                    )
                    alpha = 1.0 - max(0.0, min(1.0, float(transparency_value)))
                except (TypeError, ValueError, IndexError):
                    alpha = 1.0
                color_values = (
                    tuple(color)
                    if isinstance(color, (list, tuple)) and len(color) >= 3
                    else (1.0, 1.0, 1.0)
                )
                ring_colors.append((
                    max(0.0, min(1.0, float(color_values[0]))),
                    max(0.0, min(1.0, float(color_values[1]))),
                    max(0.0, min(1.0, float(color_values[2]))),
                    alpha,
                ))
            faces = []
            for ring in range(segments):
                a = ring * verts_per_ring
                b = ring * verts_per_ring + 1
                d = (ring + 1) * verts_per_ring + 1
                c = (ring + 1) * verts_per_ring
                faces.append((a, b, d, c))

            mesh = bpy.data.meshes.new(f"RBX Beam {name}")
            _populate_mesh_geometry(mesh, vertices, faces)
            uv_layer = mesh.uv_layers.new(name="UVMap")
            # Do not set loop UVs one RNA object at a time.  That becomes a
            # surprisingly expensive Python↔Blender round-trip for a scene
            # with many segmented beams; ``foreach_set`` performs one bulk
            # transfer instead.
            uv_values = [
                component
                for face in faces
                for vertex in face
                for component in uvs[vertex]
            ]
            uv_layer.data.foreach_set("uv", uv_values)

            # Colour/transparency gradients bake into vertex colours: the
            # shader multiplies the texture by the colour and alpha-cuts the
            # transparency, sampled per ring.
            color_layer = _new_mesh_color_attribute(mesh, name="RBXBeamColor")
            if color_layer is not None:
                try:
                    values = [
                        component
                        for face in faces
                        for vertex in face
                        for component in ring_colors[vertex // verts_per_ring]
                    ]
                    color_layer.data.foreach_set("color", values)
                except Exception:
                    pass

            beam_timing["geometry"] += time.perf_counter() - geometry_started

            # The mesh owns the variable properties (gradient and alpha).
            # Its shader only varies by the texture/wrap mode and lighting
            # parameters, so rebuilding an identical node tree per Beam is
            # needless RNA/depsgraph work on places with hundreds of them.
            texture_ref = str(beam.get("texture") or "")
            influence = max(0.0, min(1.0, beam_float("light_influence", 0.0)))
            emission_strength = max(0.0, beam_float("light_emission", 1.0))
            material_key = (texture_ref, wrap, influence, emission_strength)
            image_seconds = 0.0
            material_started = time.perf_counter()
            material = material_cache.get(material_key)
            if material is not None:
                beam_timing["material"] += time.perf_counter() - material_started
                link_started = time.perf_counter()
                finish_beam_object(mesh, name, material)
                beam_timing["link"] += time.perf_counter() - link_started
                created += 1
                continue

            material = bpy.data.materials.new(f"RBX Beam {name}")
            material.use_nodes = True
            nodes = material.node_tree.nodes
            nodes.clear()
            links = material.node_tree.links
            output = nodes.new("ShaderNodeOutputMaterial")
            uv_map = nodes.new("ShaderNodeUVMap")
            uv_map.uv_map = "UVMap"
            vertex_color = nodes.new("ShaderNodeVertexColor")
            vertex_color.name = "RBX Beam Gradient"
            try:
                vertex_color.layer_name = "RBXBeamColor"
            except (AttributeError, TypeError, ValueError):
                pass

            def math_node(node_name, operation, value0, value1):
                node = nodes.new("ShaderNodeMath")
                node.name = node_name
                node.operation = operation
                node.inputs[0].default_value = value0
                node.inputs[1].default_value = value1
                return node

            sep_uv = nodes.new("ShaderNodeSeparateXYZ")
            links.new(uv_map.outputs["UV"], sep_uv.inputs["Vector"])

            image = None
            if texture_ref and not defer_images:
                image_started = time.perf_counter()
                image = fetch_texture_image(texture_ref, name=f"beam_{name}")
                image_seconds = time.perf_counter() - image_started
                beam_timing["image"] += image_seconds
            if texture_ref:
                tex = nodes.new("ShaderNodeTexImage")
                tex.name = "RBX Beam Texture"
                tex.image = image
                tex.extension = "REPEAT" if wrap else "EXTEND"
                tint = nodes.new("ShaderNodeVectorMath")
                tint.operation = "MULTIPLY"
                links.new(tex.outputs["Color"], tint.inputs[0])
                links.new(vertex_color.outputs["Color"], tint.inputs[1])
                color_source = tint.outputs["Vector"]
                tex_alpha = nodes.new("ShaderNodeMath")
                tex_alpha.name = "RBX Beam Texture Alpha"
                tex_alpha.operation = "MULTIPLY"
                links.new(tex.outputs["Alpha"], tex_alpha.inputs[0])
                links.new(vertex_color.outputs["Alpha"], tex_alpha.inputs[1])
                alpha_source = tex_alpha.outputs["Value"]
                # Roblox beam textures are commonly opaque RGB masks: their
                # black background means "nothing" even though their source
                # file has no useful alpha channel. Blender otherwise renders
                # that background as a dark halo. Proper RGBA textures retain
                # their authored alpha unchanged.
                try:
                    source_has_alpha = image is None or int(image.depth) >= 32
                except (AttributeError, TypeError, ValueError):
                    source_has_alpha = True
                if not source_has_alpha:
                    rgb_mask = nodes.new("ShaderNodeRGBToBW")
                    rgb_mask.name = "RBX Beam RGB Alpha Mask"
                    links.new(tex.outputs["Color"], rgb_mask.inputs["Color"])
                    masked_alpha = nodes.new("ShaderNodeMath")
                    masked_alpha.name = "RBX Beam Masked Alpha"
                    masked_alpha.operation = "MULTIPLY"
                    links.new(alpha_source, masked_alpha.inputs[0])
                    links.new(rgb_mask.outputs["Val"], masked_alpha.inputs[1])
                    alpha_source = masked_alpha.outputs["Value"]
            else:
                # No texture (roblox falls back to a soft built-in gradient):
                # fake a soft volumetric core with uv falloffs so beams still
                # read as light shafts rather than hard ribbons.
                def soft_falloff(channel_out, name, exponent):
                    recenter = math_node(name + " Center", "MULTIPLY_ADD", 2.0, -1.0)
                    links.new(channel_out, recenter.inputs[0])
                    absolute = math_node(name + " Abs", "ABSOLUTE", 0.0, 0.0)
                    links.new(recenter.outputs["Value"], absolute.inputs[0])
                    edge = math_node(name + " Edge", "SUBTRACT", 1.0, 0.0)
                    links.new(absolute.outputs["Value"], edge.inputs[1])
                    power = math_node(name + " Falloff", "POWER", 0.0, exponent)
                    links.new(edge.outputs["Value"], power.inputs[0])
                    return power.outputs["Value"]

                v_falloff = soft_falloff(sep_uv.outputs["Y"], "Beam V", 2.0)
                u_wrap = nodes.new("ShaderNodeMath")
                u_wrap.name = "Beam U Wrap"
                u_wrap.operation = "WRAP"
                u_wrap.inputs[1].default_value = 0.0
                u_wrap.inputs[2].default_value = 1.0
                links.new(sep_uv.outputs["X"], u_wrap.inputs[0])
                u_falloff = soft_falloff(u_wrap.outputs["Value"], "Beam U", 2.0)
                core = nodes.new("ShaderNodeMath")
                core.name = "RBX Beam Core"
                core.operation = "MULTIPLY"
                links.new(v_falloff, core.inputs[0])
                links.new(u_falloff, core.inputs[1])
                soft_alpha = nodes.new("ShaderNodeMath")
                soft_alpha.name = "RBX Beam Soft Alpha"
                soft_alpha.operation = "MULTIPLY"
                links.new(core.outputs["Value"], soft_alpha.inputs[0])
                links.new(vertex_color.outputs["Alpha"], soft_alpha.inputs[1])
                color_source = vertex_color.outputs["Color"]
                alpha_source = soft_alpha.outputs["Value"]

            # LightInfluence: 0 = light-independent (unlit, like emission),
            # 1 = fully lit by the environment via Principled.
            emission = nodes.new("ShaderNodeEmission")
            emission.inputs["Strength"].default_value = emission_strength
            links.new(color_source, emission.inputs["Color"])
            principled = nodes.new("ShaderNodeBsdfPrincipled")
            links.new(color_source, principled.inputs["Base Color"])
            lit_mix = nodes.new("ShaderNodeMixShader")
            lit_mix.name = "RBX Beam Light Influence"
            lit_mix.inputs[0].default_value = influence
            links.new(emission.outputs["Emission"], lit_mix.inputs[1])
            links.new(principled.outputs["BSDF"], lit_mix.inputs[2])

            # Alpha blending drives the soft look: roblox beams fade with the
            # texture alpha and the Transparency sequence, even when unlit.
            transparent = nodes.new("ShaderNodeBsdfTransparent")
            alpha_mix = nodes.new("ShaderNodeMixShader")
            alpha_mix.name = "RBX Beam Alpha"
            links.new(alpha_source, alpha_mix.inputs[0])
            links.new(transparent.outputs["BSDF"], alpha_mix.inputs[1])
            links.new(lit_mix.outputs["Shader"], alpha_mix.inputs[2])
            links.new(alpha_mix.outputs["Shader"], output.inputs["Surface"])
            # Eevee only honours the Transparent-BSDF mix when the material
            # is marked blended; without it the strips render as invisible
            # (or opaque, depending on version) until switched by hand.
            try:
                material.surface_render_method = "BLENDED"
            except (AttributeError, TypeError, ValueError):
                try:
                    material.blend_method = "BLEND"
                except (AttributeError, TypeError, ValueError):
                    pass
            try:
                material.shadow_method = "NONE"
            except (AttributeError, TypeError, ValueError):
                pass
            material_cache[material_key] = material
            if defer_images and texture_ref and deferred_image_materials is not None:
                deferred_image_materials.append((material, texture_ref, wrap))
            beam_timing["material"] += (
                time.perf_counter() - material_started - image_seconds
            )
            link_started = time.perf_counter()
            finish_beam_object(mesh, name, material)
            beam_timing["link"] += time.perf_counter() - link_started
            created += 1
        except Exception as exc:
            print(f"[RbxBeam] failed to build beam '{name}': {exc}")
    if created:
        print(
            f"[RbxBeam] Built {created} beams using "
            f"{len(material_cache)} shared shader material(s): "
            f"geometry {beam_timing['geometry']:.2f}s, "
            f"image {beam_timing['image']:.2f}s, "
            f"material {beam_timing['material']:.2f}s, "
            f"link {beam_timing['link']:.2f}s."
        )
    return created


def _hydrate_rbxl_beam_images(deferred_image_materials):
    """Bind prefetched Beam images after their worker-side bytes are ready."""
    from ..rig.textures import fetch_texture_image

    print(
        "[RbxBeam] hydrating "
        f"{len(deferred_image_materials or ())} deferred beam image job(s)"
    )
    hydrated = 0
    for material, texture_ref, wrap in deferred_image_materials or ():
        try:
            image = fetch_texture_image(texture_ref, name=f"beam_{material.name}")
            if image is None:
                print(
                    "[RbxTexture] beam hydration miss: "
                    f"'{material.name}' ref '{texture_ref}' returned no image"
                )
                continue
            nodes = material.node_tree.nodes
            links = material.node_tree.links
            tex = nodes.get("RBX Beam Texture")
            if tex is None:
                continue
            tex.image = image
            tex.extension = "REPEAT" if wrap else "EXTEND"
            try:
                source_has_alpha = int(image.depth) >= 32
            except (AttributeError, TypeError, ValueError):
                source_has_alpha = True
            if not source_has_alpha and nodes.get("RBX Beam RGB Alpha Mask") is None:
                rgb_mask = nodes.new("ShaderNodeRGBToBW")
                rgb_mask.name = "RBX Beam RGB Alpha Mask"
                links.new(tex.outputs["Color"], rgb_mask.inputs["Color"])
                masked_alpha = nodes.new("ShaderNodeMath")
                masked_alpha.name = "RBX Beam Masked Alpha"
                masked_alpha.operation = "MULTIPLY"
                tex_alpha = nodes.get("RBX Beam Texture Alpha")
                if tex_alpha is not None:
                    links.new(tex_alpha.outputs["Value"], masked_alpha.inputs[0])
                links.new(rgb_mask.outputs["Val"], masked_alpha.inputs[1])
                alpha_mix = nodes.get("RBX Beam Alpha")
                if alpha_mix is not None:
                    links.new(masked_alpha.outputs["Value"], alpha_mix.inputs[0])
            hydrated += 1
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            continue
    return hydrated


class OBJECT_OT_ImportRbxm(bpy.types.Operator, ImportHelper):
    """Import a character/rig directly from a Roblox .rbxm binary model.

    Replaces the OBJ+server-metadata path: scene context (part CFrames, sizes,
    joint tree) comes from the .rbxm itself, and geometry+skinning come from the
    AssetDelivery FileMesh for each MeshPart. No FBX/OBJ intermediate.
    """

    bl_label = "Import Roblox model/place (.rbxm/.rbxl)"
    bl_idname = "object.rbxanims_import_rbxm"
    bl_description = "Import supported geometry from a Roblox .rbxm model or .rbxl place"

    filename_ext = ".rbxm"
    filter_glob: bpy.props.StringProperty(default="*.rbxm;*.rbxl", options={"HIDDEN"})
    filepath: bpy.props.StringProperty(name="File Path", maxlen=1024, default="")
    terrain_decimate: bpy.props.EnumProperty(
        name="Terrain LOD",
        description=(
            "Reduce imported terrain triangles: each level halves the voxel "
            "resolution per axis (2x2x2 cells per block)"
        ),
        items=[
            ("0", "0 - High", "Full voxel resolution"),
            ("1", "1 - Medium", "Half resolution per axis (2x2x2 cells per block)"),
            ("2", "2 - Low", "Quarter resolution per axis (4x4x4 cells per block)"),
            ("3", "3 - Lowest", "Eighth resolution per axis (8x8x8 cells per block)"),
        ],
        default="0",
    )
    mesh_lod: bpy.props.EnumProperty(
        name="Mesh LOD",
        description=(
            "FileMesh level of detail to import. LOD 0 is the highest-quality "
            "mesh; lower levels reduce geometry when the asset provides them."
        ),
        items=[
            ("0", "0 - Highest", "Use the highest-detail mesh"),
            ("1", "1 - High", "Use the first lower-detail mesh, when available"),
            ("2", "2 - Medium", "Use the second lower-detail mesh, when available"),
            ("3", "3 - Low", "Use the third lower-detail mesh, when available"),
            ("4", "4 - Lowest", "Use the fourth lower-detail mesh, when available"),
        ],
        default="0",
    )
    terrain_smooth: bpy.props.BoolProperty(
        name="Smooth Terrain",
        description=(
            "Mesh terrain with Roblox's surface-nets algorithm (smooth "
            "rolling surfaces) instead of blocky cell faces"
        ),
        default=True,
    )
    terrain_blend: bpy.props.BoolProperty(
        name="Blend Materials",
        description=(
            "Blend terrain materials with per-vertex texture splatting and "
            "organic Voronoi variation (game-engine style) instead of hard "
            "per-material seams. Requires Smooth Terrain."
        ),
        default=True,
    )
    import_beams: bpy.props.BoolProperty(
        name="Beams",
        description="Import Roblox Beam effects from .rbxl places",
        default=True,
    )
    import_decals: bpy.props.BoolProperty(
        name="Decals",
        description="Import supported classic face decals",
        default=True,
    )
    material_defaults: bpy.props.EnumProperty(
        name="Material Defaults",
        description=(
            "Material system to use when the file does not declare one "
            "(MaterialService.Use2022Materials)"
        ),
        items=[
            ("2022", "2022", "PBR-style materials (current Roblox default)"),
            ("old", "Old", "Legacy pre-2022 textures and materials"),
        ],
        default="2022",
    )
    # Multi-select: `files` holds every picked file, `directory` their folder.
    files: bpy.props.CollectionProperty(
        type=bpy.types.OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"}
    )
    directory: bpy.props.StringProperty(maxlen=1024, default="", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        geometry = layout.box()
        geometry.label(text="Geometry", icon="MESH_DATA")
        geometry.prop(self, "mesh_lod")
        terrain = layout.box()
        terrain.label(text="Terrain", icon="GRID")
        terrain.prop(self, "terrain_decimate")
        terrain.prop(self, "terrain_smooth")
        terrain.prop(self, "terrain_blend")
        effects = layout.box()
        effects.label(text="Visuals", icon="TEXTURE")
        effects.prop(self, "import_beams")
        effects.prop(self, "import_decals")
        materials = layout.box()
        materials.label(text="Materials", icon="MATERIAL")
        materials.prop(self, "material_defaults")

    def execute(self, context):
        import os

        def release_transient_import_memory():
            # These caches contain raw network payloads and parsed Python data,
            # not scene-owned Blender geometry.  Keep them across a multi-file
            # selection for deduplication, then release them as one batch.
            from ..rig import filemesh, textures

            # Place geometry/material jobs continue after this operator
            # returns. Clearing their byte caches here forces synchronous
            # refetches on Blender's main thread.
            if _LAZY_PLACE_IMPORTS:
                return
            filemesh.release_import_cache()
            textures.release_import_byte_cache()

        # Resolve the full list of files to import (single pick => filepath
        # only; multi-pick => one entry per `files` item under `directory`).
        paths = []
        if self.files and len(self.files) > 0:
            for f in self.files:
                paths.append(os.path.join(self.directory, f.name))
        elif self.properties.filepath:
            paths.append(self.properties.filepath)
        # De-dupe while preserving order (filepath is also echoed into files[0]
        # by some file browsers).
        seen = set()
        paths = [p for p in paths if not (p in seen or seen.add(p))]

        imported = 0
        failures = []
        for path in paths:
            result = self._import_one(context, path)
            if result == "FINISHED":
                imported += 1
            else:
                failures.append(os.path.basename(path))

        if imported == 0:
            release_transient_import_memory()
            self.report({"ERROR"}, "No models imported. Check the .rbxm files contain parts/meshes.")
            return {"CANCELLED"}
        if failures:
            release_transient_import_memory()
            self.report(
                {"WARNING"},
                f"Imported {imported} model(s); skipped {len(failures)}: {', '.join(failures)}",
            )
        else:
            release_transient_import_memory()
            self.report({"INFO"}, f"Imported {imported} model(s).")
        return {"FINISHED"}

    def _import_one(self, context, filepath):
        from ..core.rbxm import MAX_RBXM_SOURCE_BYTES, parse_rbxm, RbxmError
        from ..rig.filemesh import fetch_and_parse_filemesh
        from ..rig.creation import _create_mesh_object_from_filemesh
        from ..rig.mesh_surface import _is_hidden_primary_part

        is_place = os.path.splitext(filepath)[1].lower() == ".rbxl"
        mesh_lod = int(self.mesh_lod)
        import_beams = bool(self.import_beams)
        import_decals = bool(self.import_decals)
        print(f"[RbxmImport] Reading {'place' if is_place else 'model'}: {os.path.basename(filepath)}")
        import_started = time.perf_counter()
        sync_timing = {}

        try:
            started = time.perf_counter()
            if os.path.getsize(filepath) > MAX_RBXM_SOURCE_BYTES:
                self.report({"ERROR"}, "File exceeds the 512 MiB import safety limit.")
                return "CANCELLED"
            with open(filepath, "rb") as handle:
                raw = handle.read()
            sync_timing["read"] = time.perf_counter() - started
        except OSError as exc:
            self.report({"ERROR"}, f"Could not read file: {exc}")
            return "CANCELLED"

        try:
            t_parse = time.perf_counter()
            meta_loaded = parse_rbxm(raw)
            sync_timing["parse"] = time.perf_counter() - t_parse
            print(f"[RbxmImport] rbxm parse took {sync_timing['parse']:.2f}s")
        except RbxmError as exc:
            self.report({"ERROR"}, f"Failed to parse .rbxm: {exc}")
            return "CANCELLED"
        except Exception as exc:  # pragma: no cover - defensive
            self.report({"ERROR"}, f"Unexpected .rbxm parse failure: {exc}")
            return "CANCELLED"
        finally:
            # parse_rbxm returns only supported metadata, so the original
            # place/model byte buffer is no longer needed after parsing.
            raw = None

        print(f"[RbxmImport] Parsed {len(meta_loaded.get('partAux') or [])} supported parts.")

        part_aux = meta_loaded.get("partAux") or []
        if not import_decals:
            for entry in part_aux:
                entry.pop("face_decal", None)
                entry.pop("decals", None)
        if not part_aux and not meta_loaded.get("terrain"):
            self.report(
                {"ERROR"},
                "No supported MeshPart/Part instances or terrain found in this file.",
            )
            return "CANCELLED"
        if not is_place and not meta_loaded.get("rig") and part_aux:
            self.report(
                {"WARNING"},
                "No Motor6D joint tree found; the rig will have no articulation.",
            )

        # --- Scaffold: meta empty + master/parts collections (mirrors OBJ path).
        started = time.perf_counter()
        rig_name = meta_loaded.get("rigName") or _infer_rbxm_rig_name(filepath)
        meta_loaded["rigName"] = rig_name
        # Studio-style model tag, stamped on entries so object names become
        # "<Model>/<Part>.<class>" and stay unique across a multi-model scene.
        model_tag = f"{rig_name}.place" if is_place else f"{rig_name}.model"
        meta_loaded["model_tag"] = model_tag
        # The file's declared material system always wins; the dropdown
        # only fills the gap when the file has no MaterialService flag.
        use_2022_materials = bool(
            meta_loaded.get("use_2022_materials", self.material_defaults == "2022")
        )
        for entry in part_aux:
            entry["model_tag"] = model_tag
            entry["_use_2022_materials"] = use_2022_materials
        if meta_loaded.get("terrain"):
            meta_loaded["terrain"]["decimate"] = int(self.terrain_decimate)
            meta_loaded["terrain"]["smooth"] = bool(self.terrain_smooth)
            # Named colour attributes readable by shader nodes only exist
            # from Blender 3.2; older builds get hard per-material seams.
            version_tuple = getattr(bpy.app, "version", (99, 99))
            if isinstance(version_tuple, tuple) and len(version_tuple) >= 2:
                version_major = int(version_tuple[0])
                version_minor = int(version_tuple[1])
            else:
                version_major, version_minor = 99, 99
            meta_loaded["terrain"]["blend"] = (
                bool(self.terrain_blend)
                and (version_major, version_minor) >= (3, 2)
            )

        meta_obj = bpy.data.objects.new("Meta", None)
        bpy.context.scene.collection.objects.link(meta_obj)
        meta_obj.name = get_unique_name(f"__{rig_name}Meta")
        # A place's root meta object is never used to create a rig (scene rigs
        # get their own compact metadata below). Serializing every PartAux
        # entry and embedded union buffer into one IDProperty can take minutes
        # and blocks the UI before the first import message.
        meta_payload = (
            {"rigName": rig_name, "source": "rbxl", "partCount": len(part_aux)}
            if is_place else meta_loaded
        )
        meta_obj["RigMeta"] = json.dumps(meta_payload, separators=(",", ":"))

        master_collection = bpy.data.collections.new(get_unique_collection_name(model_tag))
        parent_for_master = context.scene.collection
        if not is_place:
            # Rigs are place content: when a .place was already imported,
            # nest the model's master collection under it so the scene keeps
            # one world-level root.  The model master keeps its own Parts/Rig
            # children, so rig lookups (find_master_collection_for_object,
            # find_parts_collection_in_master) still resolve inside it.
            # Accept Blender's dedup suffixes ("City.place.001").
            place_candidates = [
                coll
                for coll in context.scene.collection.children
                if re.match(r".+\.place(?:\.\d+)?$", coll.name)
            ]
            if place_candidates:
                parent_for_master = place_candidates[-1]
        parent_for_master.children.link(master_collection)
        # Layout by content: places rebuild their container hierarchy; model
        # files either keep the flat single-rig scaffold (no empty extras)
        # or get one master per rig nested under the file umbrella.  Rig
        # parts route into their rig's Parts collection at build time (no
        # double-linking); stray parts stay in the umbrella's Parts, which
        # only exists when something will actually live in it.
        def _safe_inst_ref(entry):
            try:
                return int(entry.get("inst_ref", -1))
            except (TypeError, ValueError):
                return -1

        parts_collection = None
        rig_collection = None
        rig_scaffolds = []
        place_collection_for_entry = None
        if is_place:
            parts_collection = bpy.data.collections.new(f"{model_tag} Parts")
            master_collection.children.link(parts_collection)
            place_collection_for_entry = _build_rbxl_scene_collections(
                parts_collection, meta_loaded.get("scene_nodes"), part_aux
            )
        else:
            # Weapon files carry grip metadata instead of a rig tree: build
            # no per-rig scaffolds for them (the attach flow moves meshes to
            # the target rig and tears the umbrella down).
            weapon_grips = meta_loaded.get("weaponGrip")
            is_weapon_file = isinstance(weapon_grips, list) and bool(weapon_grips)
            scene_rigs_for_layout = (
                [] if is_weapon_file else (meta_loaded.get("scene_rigs") or [])
            )
            rig_ref_set = {
                int(ref)
                for scene_rig in scene_rigs_for_layout
                for ref in (scene_rig.get("part_refs") or [])
            }
            stray_count = sum(
                1 for entry in part_aux if _safe_inst_ref(entry) not in rig_ref_set
            )
            flat_single_rig = len(scene_rigs_for_layout) == 1 and stray_count == 0
            needs_umbrella_parts = (
                flat_single_rig or stray_count > 0 or not scene_rigs_for_layout
            )
            if needs_umbrella_parts:
                parts_collection = bpy.data.collections.new(f"{model_tag} Parts")
                master_collection.children.link(parts_collection)
            if flat_single_rig:
                # The umbrella IS the rig's master: same layout a single-rig
                # file always had, no nested duplicate.
                rig_collection = bpy.data.collections.new(f"{model_tag} Rig")
                master_collection.children.link(rig_collection)
            else:
                rig_scaffolds = _create_rbxl_scene_rig_scaffolds(
                    scene_rigs_for_layout, part_aux, master_collection
                )

        rig_parts_by_ref = {}
        for scaffold in rig_scaffolds:
            for ref in scaffold["refs"]:
                rig_parts_by_ref[ref] = scaffold["parts"]

        def part_collection_for_entry(entry):
            if place_collection_for_entry is not None:
                return place_collection_for_entry(entry)
            coll = rig_parts_by_ref.get(_safe_inst_ref(entry))
            return coll or parts_collection

        sync_timing["scene scaffold"] = time.perf_counter() - started

        for coll in list(meta_obj.users_collection):
            coll.objects.unlink(meta_obj)
        master_collection.objects.link(meta_obj)
        if is_place:
            meta_obj.hide_viewport = True
            meta_obj.hide_render = True

        # --- Classic clothing context (shirt/pants templates from the .rbxm,
        # HumanoidDescription asset ids as fallback, face decal from any part).
        started = time.perf_counter()
        try:
            from ..rig import clothing

            shirt_template = meta_loaded.get("shirt_template")
            pants_template = meta_loaded.get("pants_template")
            if not shirt_template and meta_loaded.get("hd_shirt_id"):
                shirt_template = clothing.resolve_clothing_template_id(
                    meta_loaded["hd_shirt_id"]
                )
            if not pants_template and meta_loaded.get("hd_pants_id"):
                pants_template = clothing.resolve_clothing_template_id(
                    meta_loaded["hd_pants_id"]
                )
            face_decal_entry = next(
                (
                    entry.get("face_decal")
                    for entry in part_aux
                    if (entry.get("face_decal") or {}).get("texture")
                ),
                None,
            )
            face_ref = (face_decal_entry or {}).get("texture")
            face_transparency = 0.0
            if face_decal_entry is not None:
                try:
                    face_transparency = max(
                        0.0,
                        min(1.0, float(face_decal_entry.get("transparency", 0.0) or 0.0)),
                    )
                except (TypeError, ValueError):
                    pass
            if not face_ref and meta_loaded.get("hd_face_id"):
                face_ref = f"rbxassetid://{meta_loaded['hd_face_id']}"
            if not face_ref:
                # R6 dynamic heads: the parser suppresses the legacy face
                # decal when the Head carries a SpecialMesh; the engine
                # draws that SpecialMesh.TextureId OVER the head color, so
                # it must ride the clothing bake, not the opaque TextureID
                # path.
                face_ref = next(
                    (
                        entry.get("texture_id")
                        for entry in part_aux
                        if (entry.get("name") or "").strip().lower() == "head"
                        and entry.get("class_name") == "Part"
                        and entry.get("texture_id")
                    ),
                    None,
                )
            clothing.set_clothing_context(
                shirt_template=shirt_template,
                pants_template=pants_template,
                face_texture=face_ref,
                face_transparency=face_transparency,
                body_colors=meta_loaded.get("hd_body_colors"),
            )
            try:
                from ..rig import avatar_scale

                # rbxm part sizes are the FINAL render sizes (the file bakes
                # in HD scale and AvatarPartScaleType proportions already).
                # Re-running the canonical-form conversion here would scale
                # the meshes away from their joints — the detached-limb bug
                # on muscled rigs.  Clear any context a server-export import
                # left behind and keep limb scale at unity.
                avatar_scale.set_hd_scale_context(None)
                if meta_loaded.get("hd_scale"):
                    print(f"[RbxmImport] HD scale: {meta_loaded['hd_scale']}")
                scale_types = {
                    entry.get("name"): entry.get("scale_type")
                    for entry in part_aux
                    if entry.get("scale_type")
                }
                if scale_types:
                    print(f"[RbxmImport] AvatarPartScaleTypes: {scale_types}")
            except Exception:
                pass
            if clothing.clothing_available():
                print(
                    f"[RbxmImport] clothing detected: "
                    f"shirt={bool(shirt_template)} "
                    f"pants={bool(pants_template)} "
                    f"face={bool(face_ref)} "
                    f"hdColors={bool(meta_loaded.get('hd_body_colors'))}"
                )
        except Exception as exc:
            print(f"[RbxmImport] clothing context skipped: {exc}")

        # --- Build geometry for every MeshPart from its AssetDelivery FileMesh.
        # Warm the network caches concurrently first — the serial loop below
        # then runs at cache speed (bpy work must stay on this thread anyway).
        try:
            import time as _time

            from ..rig import filemesh as _filemesh_mod
            from ..rig import textures as _textures_mod

            _t0 = _time.time()
            mesh_ids = []
            texture_refs = []
            sky_refs = []
            beam_texture_refs = []
            terrain_texture_refs = []
            shirt_template = locals().get("shirt_template")
            pants_template = locals().get("pants_template")
            face_ref = locals().get("face_ref")
            for entry in part_aux:
                if entry.get("mesh_id"):
                    mesh_ids.append(entry["mesh_id"])
                for ti in _textures_mod._entry_texture_instances(entry):
                    if ti.get("texture"):
                        texture_refs.append(ti["texture"])
                for sa in _textures_mod._entry_surface_appearances(entry):
                    for key in (
                        "color_map", "normal_map", "roughness_map", "metalness_map"
                    ):
                        if sa.get(key):
                            texture_refs.append(sa[key])
                for ref in (
                    entry.get("texture_id"),
                    (entry.get("face_decal") or {}).get("texture"),
                ):
                    if ref:
                        texture_refs.append(ref)
                texture_refs.extend(
                    decal.get("texture")
                    for decal in (entry.get("decals") or ())
                    if decal.get("texture")
                )
                texture_refs.extend(_textures_mod.builtin_material_texture_refs(entry))
            for ref in (shirt_template, pants_template, face_ref):
                if ref:
                    texture_refs.append(ref)
            if is_place:
                from ..rig.terrain import smooth_grid_material_enums

                terrain_meta = meta_loaded.get("terrain") or {}
                for material_enum in smooth_grid_material_enums(
                    terrain_meta.get("smoothgrid") or b""
                ):
                    terrain_texture_refs.extend(
                        _textures_mod.builtin_material_texture_refs({
                            "material": material_enum,
                            "color": (1.0, 1.0, 1.0),
                            "name": f"Terrain_{material_enum}",
                        })
                    )
            # Beams are finalized after mesh geometry, but their images must
            # join the same worker prefetch or each imported beam can start a
            # synchronous request on Blender's main thread.
            beam_texture_refs = [
                beam.get("texture")
                for beam in (meta_loaded.get("beams") or ())
                if import_beams and beam.get("texture")
            ]
            texture_refs.extend(beam_texture_refs)
            # Sky faces were previously fetched one-at-a-time after all scene
            # geometry completed, making place import appear to stall at 99%.
            # Front-load them so the deferred scene tail (skybox + lighting)
            # finds their bytes cached when it runs, and no synchronous
            # network request remains on the main thread.
            if is_place:
                sky_refs = [
                    ref for ref in (meta_loaded.get("scene_sky") or {}).values() if ref
                ]
                # Scene-critical assets start first. Their exact completion
                # gates let terrain and sky construction overlap the long
                # tail of unrelated decorative textures.
                texture_refs[:0] = sky_refs + terrain_texture_refs
            if is_place:
                print("[RbxmImport] Place meshes will load incrementally in the background.")
            else:
                _filemesh_mod.prefetch_filemeshes(mesh_ids, allow_local_paths=False)
                _textures_mod.prefetch_texture_bytes(texture_refs)
                print(f"[RbxmImport] prefetch done in {_time.time() - _t0:.1f}s")
        except Exception as exc:
            print(f"[RbxmImport] prefetch skipped: {exc}")

        sync_timing["context + asset refs"] = time.perf_counter() - started

        # Characters must remain individual objects for the rig pass. Ordinary
        # untextured primitives, however, dominate many place files and can be
        # safely baked into one mesh per collection/material combination.
        rig_part_refs = {
            int(ref)
            for scene_rig in meta_loaded.get("scene_rigs") or []
            for ref in scene_rig.get("part_refs") or []
        }

        def static_batch_key(entry):
            if not is_place or not entry.get("shape") or entry.get("has_studs"):
                return None
            if any(entry.get(key) for key in (
                "mesh_id", "texture_id", "texture_instance", "face_decal",
                "surface_appearance", "texture_instances", "surface_appearances",
                "wrap_layer", "wrap_target", "union_mesh",
            )):
                return None
            try:
                if int(entry.get("inst_ref", -1)) in rig_part_refs:
                    return None
            except (TypeError, ValueError):
                return None
            color = entry.get("color") or (1.0, 1.0, 1.0)
            try:
                color = tuple(round(float(component), 5) for component in color[:3])
                transparency = round(float(entry.get("transparency", 0.0)), 5)
                reflectance = round(float(entry.get("reflectance", 0.0)), 5)
            except (TypeError, ValueError):
                return None
            from ..rig.textures import _effective_material_id, variant_signature

            collection = part_collection_for_entry(entry)
            return (
                collection.as_pointer(), _effective_material_id(entry),
                variant_signature(entry),
                color, transparency, reflectance,
            )

        started = time.perf_counter()
        # Legacy (2012-era) unions reference their CSGMDL render mesh by
        # AssetId.  Resolve those before job planning so the union branch
        # below builds them like embedded meshes.
        try:
            from ..core.auth import get_auth_headers

            _resolve_legacy_union_assets(part_aux, get_auth_headers())
        except Exception as exc:  # noqa: BLE001 - offline imports degrade
            print(f"[RbxmImport] union asset resolution skipped: {exc}")
        static_batches = {}
        lazy_mesh_jobs = []
        sync_texture_entries = []
        built = 0
        built_objs = []
        skipped = []

        def create_sync_mesh(collection, part_name, mesh_data, entry):
            # These local/union place objects used to fetch their material
            # maps synchronously during planning. Build their graph now and
            # let the existing post-prefetch hydration pass assign images.
            if is_place:
                from ..rig.textures import defer_texture_image_loading

                with defer_texture_image_loading():
                    obj = _create_mesh_object_from_filemesh(
                        collection, part_name, mesh_data, entry, lod_index=mesh_lod
                    )
                sync_texture_entries.append(entry)
                return obj
            return _create_mesh_object_from_filemesh(
                collection, part_name, mesh_data, entry, lod_index=mesh_lod
            )
        for entry in part_aux:
            part_name = entry.get("name")
            if not part_name:
                continue
            # A fully transparent Model.PrimaryPart (the invisible
            # HumanoidRootPart pattern) is structural rig plumbing: the
            # rig tree still contributes its bone, but no mesh object is
            # built for it.
            if _is_hidden_primary_part(entry):
                skipped.append(f"{part_name} (hidden primary part)")
                continue
            batch_key = static_batch_key(entry)
            if batch_key is not None:
                static_batches.setdefault(
                    batch_key, (part_collection_for_entry(entry), [])
                )[1].append(entry)
                continue
            union_mesh = entry.get("union_mesh")
            if isinstance(union_mesh, dict):
                # UnionOperation: the render mesh was decoded from the .rbxm
                # itself (CSGMDL) — no asset fetch required.
                union_data = dict(union_mesh)
                union_data["embedded_union"] = True
                mesh_obj = create_sync_mesh(
                    part_collection_for_entry(entry), part_name, union_data, entry
                )
                if mesh_obj is not None:
                    built += 1
                    built_objs.append(mesh_obj)
                else:
                    skipped.append(f"{part_name} (union build failed)")
                continue
            mesh_id = entry.get("mesh_id")
            if not mesh_id:
                # Try primitive Part shape (Block/Cylinder/Sphere/Wedge/CornerWedge).
                shape = entry.get("shape")
                if shape is not None:
                    from ..rig.creation import _build_primitive_mesh_data
                    mesh_data = _build_primitive_mesh_data(entry)
                    if mesh_data is not None:
                        mesh_obj = create_sync_mesh(
                            part_collection_for_entry(entry), part_name, mesh_data, entry
                        )
                        if mesh_obj is not None:
                            built += 1
                            built_objs.append(mesh_obj)
                            continue
                skipped.append(f"{part_name} (no mesh)")
                continue
            try:
                is_rigged_place_part = int(entry.get("inst_ref", -1)) in rig_part_refs
            except (TypeError, ValueError):
                is_rigged_place_part = True
            if is_place and not is_rigged_place_part:
                lazy_mesh_jobs.append((entry, part_collection_for_entry(entry)))
                built += 1
                continue
            try:
                mesh_data = fetch_and_parse_filemesh(mesh_id, allow_local_paths=False)
            except Exception as exc:
                skipped.append(f"{part_name} ({exc})")
                print(f"[RbxmImport] Failed to fetch FileMesh for '{part_name}': {exc}")
                continue
            mesh_obj = create_sync_mesh(
                part_collection_for_entry(entry), part_name, mesh_data, entry
            )
            if mesh_obj is not None:
                built += 1
                built_objs.append(mesh_obj)

        static_batch_jobs = []
        # Keep each main-thread timer slice bounded. One enormous mesh still
        # freezes Blender even when its source parts were grouped correctly.
        # The batch builder now uses compact position-only buffers, so 1,024
        # basic parts stay below a frame-sized main-thread slice while cutting
        # datablock and dependency-graph churn by 4x versus the old chunks.
        static_chunk_size = 1024
        for _key, (collection, entries) in static_batches.items():
            for chunk_index in range(0, len(entries), static_chunk_size):
                chunk = entries[chunk_index:chunk_index + static_chunk_size]
                # Name like the individual parts: citytemplate.place/<Part>.<Class>.
                # Duplicate names get Blender's automatic .001 suffixing.
                batch_name = _lazy_mesh_object_name(chunk[0])
                static_batch_jobs.append((collection, batch_name, chunk, _key))
                built += len(chunk)

        sync_timing["job planning"] = time.perf_counter() - started
        print(
            "[RbxmImport] Sync detail: "
            + ", ".join(
                f"{name} {seconds:.2f}s" for name, seconds in sync_timing.items()
            )
        )

        _lazy_state_count_before = len(_LAZY_PLACE_IMPORTS)
        _schedule_lazy_place_meshes(
            lazy_mesh_jobs,
            static_batch_jobs,
            texture_refs=texture_refs if is_place else (),
            terrain_meta=meta_loaded.get("terrain") if is_place else None,
            pre_work_seconds=time.perf_counter() - import_started,
            raw_decode_refs=sky_refs if is_place else (),
            beam_texture_refs=beam_texture_refs if is_place else (),
            terrain_texture_refs=terrain_texture_refs if is_place else (),
            mesh_lod=mesh_lod,
        )
        if is_place and sync_texture_entries and _LAZY_PLACE_IMPORTS:
            state = _LAZY_PLACE_IMPORTS[-1]
            for entry in sync_texture_entries:
                material_signature = _lazy_material_signature(entry)
                if _lazy_material_needs_hydration(entry):
                    _register_deferred_material(state, material_signature, entry)
                    _mark_deferred_material_built(state, entry)
        if is_place and static_batch_jobs:
            print(
                f"[RbxmImport] Deferred {len(static_batch_jobs)} static batches "
                f"and {len(lazy_mesh_jobs)} meshparts."
            )

        print(
            f"[RbxmImport] rig='{rig_name}' partAux={len(part_aux)} "
            f"built={built} skipped={len(skipped)}"
        )
        for note in skipped:
            print(f"[RbxmImport]   skipped: {note}")

        if built == 0 and not meta_loaded.get("terrain"):
            self.report(
                {"ERROR"},
                f"'{rig_name}': no MeshPart or Part geometry could be built.",
            )
            return "CANCELLED"

        # --- Weapon attach prompt: the Studio plugin stamps grip metadata as
        # attributes on the exported weapon clone (weaponGrip entries), so an
        # rbxm weapon has no Motor6D tree of its own.  Ask the user which rig
        # to attach to instead of running the rig pass on bare meshes.
        grips = meta_loaded.get("weaponGrip")
        if not is_place and isinstance(grips, list) and grips and built_objs:
            weapon_name = rig_name
            _pending_weapon_import.clear()
            _pending_weapon_import["weapon_name"] = weapon_name
            _pending_weapon_import["mode"] = "rbxm"
            payload: WeaponImportPayload = {
                "schema": _WEAPON_IMPORT_PAYLOAD_VERSION,
                "meta_loaded": meta_loaded,
                "rig_part_obj_names": [obj.name for obj in built_objs],
            }
            _pending_weapon_import["data"] = payload
            bpy.ops.object.rbxanims_confirm_weapon_target("INVOKE_DEFAULT")
            return "FINISHED"

        if is_place:
            if len(_LAZY_PLACE_IMPORTS) > _lazy_state_count_before:
                # Defer the scene tail (rigs, skybox, lighting, lights) into
                # the lazy pump: it then overlaps the texture prefetch
                # instead of serializing ~5-9 s of synchronous work before
                # the background pipeline even starts.
                sky = meta_loaded.get("scene_sky") or {}
                state = _LAZY_PLACE_IMPORTS[-1]

                def build_place_terrain(prepared):
                    from ..rig import terrain as terrain_mod

                    return terrain_mod.build_prepared_terrain(
                        parts_collection, prepared, model_tag=model_tag
                    )

                state["terrain_builder"] = build_place_terrain

                def finalize_place_scene():
                    tail_started = time.perf_counter()
                    finalize_steps = state["finalize_steps"]

                    def final_step(name, func, *args):
                        started = time.perf_counter()
                        try:
                            return func(*args)
                        finally:
                            finalize_steps[name] = time.perf_counter() - started

                    terrain_built = state["terrain_built"]
                    rig_count = final_step("rigs", _create_rbxl_scene_rigs,
                                           meta_loaded.get("scene_rigs"), part_aux
                                           )
                    beam_count = 0
                    if import_beams:
                        beam_count = final_step("beams", _create_rbxl_beams,
                                                parts_collection,
                                                meta_loaded.get("beams"),
                                                True,
                                                state["beam_image_jobs"],
                                                )
                    decal_count = final_step("decals", _create_rbx_decals,
                                             parts_collection, part_aux
                                             ) if import_decals else 0
                    state["beam_images_hydrated"] = not state["beam_image_jobs"]
                    final_step("lighting enable", _enable_imported_scene_lighting, context)
                    light_count = final_step("lights", _create_rbxl_lights,
                                             parts_collection, meta_loaded.get("scene_lights"), part_aux
                                             )
                    try:
                        from ..rig import textures as _textures_mod

                        # Transient prefetch failures finalize materials
                        # without their images (black texture nodes); give
                        # each failed asset one retry before hydration.
                        state["texture_retries"] = final_step(
                            "texture retries",
                            _textures_mod.retry_failed_texture_fetches,
                        )
                        state["baked_hydrated"] += final_step(
                            "baked hydration",
                            _textures_mod.hydrate_pending_baked_materials,
                        )
                    except Exception:
                        pass
                    state["phase_times"]["finalize"] += (
                        time.perf_counter() - tail_started
                    )
                    state["finalize_done"] = True

                    def finish_texture_scene():
                        skybox = final_step(
                            "skybox", _create_rbxl_skybox,
                            parts_collection, sky, part_aux,
                        )
                        sun = final_step(
                            "global lighting", _create_rbxl_global_lighting,
                            parts_collection,
                            meta_loaded.get("scene_lighting") or {},
                            skybox,
                            meta_loaded.get("scene_atmosphere"),
                            meta_loaded.get("scene_post_effects"),
                        )
                        print(
                            f"[RbxmImport] Imported place '{rig_name}' ({built} supported meshes, "
                            f"{light_count} lights, rigs={rig_count}, sun={bool(sun)}, "
                            f"skybox={bool(skybox)}, beams={beam_count}, decals={decal_count}, "
                            f"terrain={terrain_built} terrain object(s))."
                        )

                    state["post_texture_finalize"] = finish_texture_scene
                    state["post_texture_finalized"] = not bool(sky)

                state["finalize"] = finalize_place_scene
                state["finalize_done"] = False
                return "FINISHED"
            # No lazy work was scheduled (tiny or all-sync place): run the
            # scene tail inline as before.
            try:
                from ..rig import terrain as terrain_mod

                terrain_built = terrain_mod.import_terrain(
                    parts_collection,
                    meta_loaded.get("terrain") or {},
                    model_tag=model_tag,
                )
            except Exception as exc:
                terrain_built = 0
                print(f"[RbxmImport] terrain skipped: {exc}")
            sky = meta_loaded.get("scene_sky") or {}
            rig_count = _create_rbxl_scene_rigs(
                meta_loaded.get("scene_rigs"), part_aux
            )
            skybox = _create_rbxl_skybox(parts_collection, sky, part_aux)
            beam_count = (
                _create_rbxl_beams(parts_collection, meta_loaded.get("beams"))
                if import_beams
                else 0
            )
            decal_count = _create_rbx_decals(parts_collection, part_aux) if import_decals else 0
            sun = _create_rbxl_global_lighting(
                parts_collection,
                meta_loaded.get("scene_lighting") or {},
                skybox,
                meta_loaded.get("scene_atmosphere"),
                meta_loaded.get("scene_post_effects"),
            )
            _enable_imported_scene_lighting(context)
            light_count = _create_rbxl_lights(
                parts_collection, meta_loaded.get("scene_lights"), part_aux
            )
            print(
                f"[RbxmImport] Imported place '{rig_name}' ({built} supported meshes, "
                f"{light_count} lights, rigs={rig_count}, sun={bool(sun)}, skybox={bool(skybox)}, "
                f"beams={beam_count}, decals={decal_count}, terrain={terrain_built} terrain object(s))."
            )
            return "FINISHED"

        if import_decals:
            _create_rbx_decals(parts_collection, part_aux)

        # --- Generate armatures + bind, reusing the standard pipeline.
        # Every rig owns a scaffold built during collection setup; each gets
        # its armature there.  The fallback covers hand-built meta without
        # scene_rigs (unreachable for parsed files, which enumerate every
        # joint component).
        try:
            for scaffold in rig_scaffolds:
                try:
                    create_rig("CONNECT", scaffold["meta_name"])
                except Exception as exc:
                    print(
                        f"[RbxmImport] rig build failed for "
                        f"'{scaffold['meta_name']}': {exc}"
                    )
            if not rig_scaffolds and meta_loaded.get("rig"):
                create_rig("CONNECT", meta_obj.name)
        except Exception as exc:
            self.report({"WARNING"}, f"'{rig_name}': geometry imported, but rig generation failed: {exc}")

        print(f"[RbxmImport] Imported '{rig_name}' ({built} meshes built).")
        return "FINISHED"


class OBJECT_OT_ImportFbxAnimation(bpy.types.Operator, ImportHelper):
    bl_label = "Import animation data (.fbx)"
    bl_idname = "object.rbxanims_importfbxanimation"
    bl_description = "Import animation data (.fbx) --- FBX file should contain an armature, which will be mapped onto the generated rig by bone names."

    filename_ext = ".fbx"
    filter_glob: bpy.props.StringProperty(default="*.fbx", options={"HIDDEN"})
    filepath: bpy.props.StringProperty(name="File Path", maxlen=1024, default="")

    @classmethod
    def poll(cls, context):
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None
        return get_object_by_name(armature_name)

    def execute(self, context):
        from ..animation.import_export import (
            get_mapping_error_bones,
            prepare_for_kf_map,
            copy_anim_state,
            apply_ao_transform,
        )
        from ..core.utils import get_action_fcurves
        import math

        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        armature_name = settings.rbx_anim_armature if settings else None

        # Get target armature early to fail fast
        armature = get_object_by_name(armature_name)
        if not armature:
            self.report(
                {"ERROR"},
                f"No armature named '{armature_name}' found. Please ensure the correct rig is selected.",
            )
            return {"CANCELLED"}

        # Ensure active keying set exists, create one if needed
        if not bpy.context.scene.keying_sets.active:
            bpy.ops.anim.keying_set_add()
            self.report({"INFO"}, "Created new keying set for animation import.")

        # Import and keep track of what is imported (use set for faster lookup)
        objnames_before_import = {obj.name for obj in iter_scene_objects(context.scene)}
        bpy.ops.import_scene.fbx(filepath=self.properties.filepath)
        objnames_imported = [
            obj.name for obj in iter_scene_objects(context.scene) if obj.name not in objnames_before_import
        ]

        def clear_imported():
            """Clean up all objects imported from the FBX file."""
            for obj_name in objnames_imported:
                obj = get_object_by_name(obj_name)
                if obj:
                    bpy.data.objects.remove(obj)

        # Check that there's exactly 1 armature in the imported file
        armatures_imported = [
            obj for obj in iter_scene_objects(context.scene)
            if obj.type == "ARMATURE" and obj.name in objnames_imported
        ]
        if len(armatures_imported) == 0:
            self.report({"ERROR"}, "Imported FBX file contains no armature.")
            clear_imported()
            return {"CANCELLED"}
        if len(armatures_imported) > 1:
            self.report(
                {"ERROR"},
                f"Imported FBX file contains {len(armatures_imported)} armatures, expected 1.",
            )
            clear_imported()
            return {"CANCELLED"}

        ao_imp = armatures_imported[0]

        # Validate bone mapping between source and target
        err_mappings = get_mapping_error_bones(armature, ao_imp)
        if err_mappings:
            self.report(
                {"ERROR"},
                f"Cannot map rig, the following bones are missing from the source rig: {', '.join(err_mappings)}.",
            )
            clear_imported()
            return {"CANCELLED"}

        # Validate imported armature has animation data
        if not ao_imp.animation_data or not ao_imp.animation_data.action:
            self.report({"ERROR"}, "Imported FBX armature contains no animation data.")
            clear_imported()
            return {"CANCELLED"}

        fcurves = get_action_fcurves(ao_imp.animation_data.action)
        if not fcurves:
            self.report({"ERROR"}, "Imported FBX armature contains no animation curves.")
            clear_imported()
            return {"CANCELLED"}

        # Get keyframes and set frame range
        kp_frames = [kp.co.x for fcurve in fcurves for kp in fcurve.keyframe_points]
        if not kp_frames:
            self.report({"ERROR"}, "Imported FBX armature contains no keyframes.")
            clear_imported()
            return {"CANCELLED"}

        bpy.context.scene.frame_start = math.floor(min(kp_frames))
        bpy.context.scene.frame_end = math.ceil(max(kp_frames))

        # Apply transforms and prepare for keyframe mapping
        bpy.context.view_layer.objects.active = ao_imp
        apply_ao_transform(ao_imp)
        prepare_for_kf_map()

        # Ensure the target armature has animation_data initialized
        if armature.animation_data is None:
            armature.animation_data_create()

        # Copy animation state from imported armature to target
        copy_anim_state(armature, ao_imp)

        clear_imported()
        self.report({"INFO"}, f"Successfully imported animation with {len(kp_frames)} keyframes.")
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}
