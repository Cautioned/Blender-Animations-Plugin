"""Adversarial test for the lazy material-hydration queue.

Drives the REAL ``_process_lazy_place_imports`` tick against a synthetic
state (no network, no geometry) with every failure mode the queue has
encountered, in one pass:

  * deferred materials whose texture bytes have not landed yet
  * face-split primitive materials (base + per-face cache variants)
  * a signature whose material was never built (geometry error path)
  * a stale ``RBXTextureHydrated`` flag that disagrees with the graph
  * assets already terminal when the consumer registers (late path)
  * assets that complete mid-pump (ready-consumer drain path)
  * an asset whose fetch stays blocked across the attempt cap
  * the finalize rehydration sweep repairing everything it can

The whole scenario is deterministic: budgets are shrunk to one signature
per tick so the requeue path is forced through dozens of ticks, and the
verdict at the end is the node-based audit, never a flag.
"""

import bpy
import time
import unittest
from collections import deque
from types import SimpleNamespace
from uuid import uuid4

from ..rig import textures
from ..operators import import_ops as import_ops_mod
from ..core.asset_pipeline import AssetRegistry


def _deferred_state(job_future_done):
    """A scheduler state shaped exactly like _schedule_lazy_place_meshes'."""
    return {
        "pool": SimpleNamespace(shutdown=lambda *a, **k: None),
        "auth_headers": {},
        "mesh_lod": 0,
        "jobs": [{"future": SimpleNamespace(done=lambda: job_future_done)}],
        "count": 0,
        "queued_meshes": [],
        "ready_meshes": deque(),
        "static_batches": deque(),
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
        "texture_assets": AssetRegistry(),
        "material_hydration_started": False,
        "phase_times": {
            "fetch_drain": 0.0, "static": 0.0, "geometry": 0.0, "materials": 0.0,
            "fetch_network": 0.0, "fetch_parse": 0.0, "fetch_prep": 0.0,
            "textures": 0.0, "terrain_prepare": 0.0, "finalize": 0.0,
        },
        "texture_seconds_consumed": False,
        "texture_stats": {},
        "texture_completion_count": 0,
        "terrain_consumed": False,
        "prepared_terrain": None,
        "terrain_builder": None,
        "terrain_built": 0,
        "terrain_build_done": True,
        "terrain_build_seconds": 0.0,
        "finalize_steps": {},
        "beam_image_jobs": [],
        "sky_assets_ready": True,
        "beam_assets_ready": True,
        "terrain_assets_ready": True,
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
        "pre_work_seconds": 0.0,
        "finalize": None,
        "finalize_done": True,
        "hidden_collections": [],
        "started": time.perf_counter(),
        "texture_future": None,
        "terrain_future": None,
    }


class LazyQueueTortureTests(unittest.TestCase):
    """Drive the real tick function through every queue gap in one pass."""

    def _pump(self, state, max_ticks=400):
        ticks = 0
        while ticks < max_ticks and (
            state["material_jobs"]
            or state["ready_meshes"]
            or state["static_batches"]
            or not state["material_audit_done"]
            or any(not job["future"].done() for job in state["jobs"])
        ):
            import_ops_mod._process_lazy_place_imports()
            ticks += 1
        return ticks

    def test_queue_survives_every_adversary_at_once(self):
        suffix = uuid4().hex[:8]
        state = _deferred_state(job_future_done=False)
        import_ops_mod._LAZY_PLACE_IMPORTS.append(state)

        # Shrink the budgets so every signature forces its own tick; this
        # makes requeues and the attempt cap actually observable.
        saved_budgets = (
            import_ops_mod._LAZY_MATERIAL_REFRESH_BUDGET,
            import_ops_mod._LAZY_HYDRATION_OVERLAP_BUDGET,
        )
        import_ops_mod._LAZY_MATERIAL_REFRESH_BUDGET = 1
        import_ops_mod._LAZY_HYDRATION_OVERLAP_BUDGET = 1
        self.addCleanup(
            setattr,
            import_ops_mod, "_LAZY_MATERIAL_REFRESH_BUDGET", saved_budgets[0],
        )
        self.addCleanup(
            setattr,
            import_ops_mod, "_LAZY_HYDRATION_OVERLAP_BUDGET", saved_budgets[1],
        )
        self.addCleanup(lambda: import_ops_mod._LAZY_PLACE_IMPORTS.clear())

        # --- controllable fetch -------------------------------------------------
        # blocked refs return None (bytes not landed); the rest return a real
        # image datablock.  Released refs flip between the two behaviours.
        blocked = set()
        img = bpy.data.images.new(f"queue_probe_{suffix}", 4, 4, alpha=True)
        img.colorspace_settings.name = "sRGB"
        img.pixels = [0.5] * 64
        img.update()
        original_fetch = textures.fetch_texture_image

        def fetch(ref, name=None, non_color=False):
            if textures._DEFER_TEXTURE_IMAGES:
                # Honor the production defer gate: a deferred build must not
                # fetch, exactly like the real fetch_texture_image.
                return None
            if str(ref) in blocked:
                return None
            return img

        textures.fetch_texture_image = fetch
        self.addCleanup(setattr, textures, "fetch_texture_image", original_fetch)

        def entry(name, **kwargs):
            base = {
                "name": name, "class_name": "Part", "shape": "block",
                "color": [0.4, 0.3, 0.2], "transparency": 0.0, "reflectance": 0.0,
                "_use_2022_materials": True,
            }
            base.update(kwargs)
            return base

        def build_deferred(name, entry_dict):
            with textures.defer_texture_image_loading():
                return textures.get_part_material(name, entry_dict)

        def register(state_dict, entry_dict):
            signature = import_ops_mod._lazy_material_signature(entry_dict)
            import_ops_mod._register_deferred_material(state_dict, signature, entry_dict)
            import_ops_mod._mark_deferred_material_built(state_dict, entry_dict)
            return signature

        # A: primitive with face-split Texture instances (base + 2 face
        # variants, built the way apply_part_material's branch builds them).
        a_refs = [f"rbxassetid://faceA{suffix}", f"rbxassetid://faceB{suffix}"]
        a_entry = entry("queue_prim", texture_instances=[
            {"texture": a_refs[0], "face": 0}, {"texture": a_refs[1], "face": 5},
        ])
        a_base = dict(a_entry)
        a_base.pop("texture_instances", None)
        a_base.pop("texture_instance", None)
        build_deferred("queue_prim", a_base)
        for face, ref in ((0, a_refs[0]), (5, a_refs[1])):
            face_entry = dict(a_entry)
            face_entry["texture_instances"] = [{"texture": ref, "face": face}]
            face_entry.pop("texture_instance", None)
            build_deferred(f"queue_prim_face{face}", face_entry)
        for ref in a_refs:
            state["texture_assets"].register(
                textures._texture_asset_key(ref), str(ref), "texture"
            )
            state["texture_assets"].finish(textures._texture_asset_key(ref))
        sig_a = register(state, a_entry)

        # B: built-in material with maps, bytes ready.
        b_entry = entry("queue_brick", material=816, color=[0.41, 0.3, 0.2])
        b_material = build_deferred("queue_brick", b_entry)
        for ref in textures.builtin_material_texture_refs(b_entry):
            state["texture_assets"].register(
                textures._texture_asset_key(ref), str(ref), "texture"
            )
            state["texture_assets"].finish(textures._texture_asset_key(ref))
        sig_b = register(state, b_entry)

        # C: ghost — registered but its material was never built.  Its graph
        # readiness is forced so the queue actually reaches the empty-cache
        # refresh path (in production this mirrors "built at registration,
        # evicted before refresh").
        c_ref = f"rbxassetid://ghost{suffix}"
        c_entry = entry("queue_ghost", surface_appearance={"color_map": c_ref})
        state["texture_assets"].register(
            textures._texture_asset_key(c_ref), str(c_ref), "texture"
        )
        state["texture_assets"].finish(textures._texture_asset_key(c_ref))
        sig_c = register(state, c_entry)
        state["material_graphs_ready"].add(sig_c)
        import_ops_mod._queue_ready_material(state, sig_c)

        # D: healthy — built deferred, then hydrated before the pump.
        d_entry = entry("queue_healthy", material=512)
        build_deferred("queue_healthy", d_entry)
        for ref in textures.builtin_material_texture_refs(d_entry):
            state["texture_assets"].register(
                textures._texture_asset_key(ref), str(ref), "texture"
            )
            state["texture_assets"].finish(textures._texture_asset_key(ref))
        sig_d = register(state, d_entry)

        # E: stale flag — genuinely hydrated graph, but the flag lies (False).
        e_ref = f"rbxassetid://stale{suffix}"
        e_entry = entry("queue_staleflag", surface_appearance={"color_map": e_ref})
        e_material = build_deferred("queue_staleflag", e_entry)
        state["texture_assets"].register(
            textures._texture_asset_key(e_ref), str(e_ref), "texture"
        )
        state["texture_assets"].finish(textures._texture_asset_key(e_ref))
        sig_e = register(state, e_entry)
        textures.hydrate_material_images(e_material, e_entry)
        self.assertFalse(textures._image_less_node_names(e_material))
        e_material["RBXTextureHydrated"] = False  # lie

        # F: late consumer — asset already terminal BEFORE registration.
        f_ref = f"rbxassetid://late{suffix}"
        f_entry = entry("queue_late", surface_appearance={"color_map": f_ref})
        build_deferred("queue_late", f_entry)
        state["texture_assets"].register(
            textures._texture_asset_key(f_ref), str(f_ref), "texture"
        )
        state["texture_assets"].finish(textures._texture_asset_key(f_ref))
        sig_f = register(state, f_entry)

        # G: blocked forever in phase 1 — a built-in whose maps are all
        # blocked.  The colour map degrades via the missing-map fallback, but
        # the NORMAL map stays image-less, so the refresh keeps failing and
        # the bounded requeue path is what gets exercised.
        g_entry = entry("queue_blocked", material=848, transparency=0.1)
        g_material = build_deferred("queue_blocked", g_entry)
        for ref in textures.builtin_material_texture_refs(g_entry):
            blocked.add(ref)
            state["texture_assets"].register(
                textures._texture_asset_key(ref), str(ref), "texture"
            )
            state["texture_assets"].finish(textures._texture_asset_key(ref))
        sig_g = register(state, g_entry)

        # H: mid-flight — its asset is still pending and completes mid-pump.
        h_ref = f"rbxassetid://midflight{suffix}"
        h_entry = entry("queue_midflight", surface_appearance={"color_map": h_ref})
        h_material = build_deferred("queue_midflight", h_entry)
        h_key = textures._texture_asset_key(h_ref)
        state["texture_assets"].register(h_key, str(h_ref), "texture")
        sig_h = import_ops_mod._lazy_material_signature(h_entry)
        import_ops_mod._register_deferred_material(state, sig_h, h_entry)
        import_ops_mod._mark_deferred_material_built(state, h_entry)
        # Not finished: subscribe must have left it pending, not queued.

        # ---- phase 1: pump with the fetch blocked for G only ------------------
        # The zombie in-flight job keeps geometry_pending True so failed
        # refreshes go down the bounded requeue path, not the final drop.
        self.assertEqual(
            len(state["deferred_materials"]), 8,
            "every adversary must register a distinct signature",
        )
        ticks = 0
        while ticks < 30:
            import_ops_mod._process_lazy_place_imports()
            ticks += 1
            if ticks == 10:
                # H's bytes land mid-pump through the real ready-consumer path.
                state["texture_assets"].finish(h_key)
        print(
            "[Torture] phase1: seen="
            f"{sorted(str(s)[:24] for s in state['material_hydrated_seen'])}"
            f" jobs={len(state['material_jobs'])}"
            f" attempts={dict(state['material_refresh_attempts'])}"
        )
        for sig, deferred_entry in state["deferred_materials"].items():
            print(
                f"[Torture]   sig={str(sig)[:60]} name={deferred_entry.get('name')}"
                f" built={textures.part_material_is_built(deferred_entry)}"
            )

        # The blocked signature must have been requeued and capped at 4.
        self.assertIn(sig_g, state["material_refresh_attempts"])
        self.assertEqual(state["material_refresh_attempts"][sig_g], 4)
        # The ghost was never built: popped once, dropped from the unbuilt
        # branch (production: its geometry job never re-marks it built).
        self.assertNotIn(sig_c, state["material_hydrated_seen"])
        self.assertNotIn(sig_c, state["material_refresh_attempts"])
        # The stale-flag signature must NOT have been requeued (node verdict).
        self.assertNotIn(sig_e, state["material_refresh_attempts"])
        self.assertIn(sig_e, state["material_hydrated_seen"])
        # Late and mid-flight consumers must both have hydrated in phase 1.
        self.assertIn(sig_f, state["material_hydrated_seen"])
        self.assertIn(sig_h, state["material_hydrated_seen"])
        # The healthy paths must all have completed in the first pass.
        self.assertIn(sig_a, state["material_hydrated_seen"])
        self.assertIn(sig_b, state["material_hydrated_seen"])
        self.assertIn(sig_d, state["material_hydrated_seen"])

        # Everything except G must be image-less-free by now.
        for material in (b_material, e_material, h_material):
            self.assertFalse(textures._image_less_node_names(material))
        self.assertTrue(textures._image_less_node_names(g_material))

        # ---- phase 2: bytes finally land for G --------------------------------
        blocked.clear()
        # The finalize sweep must repair G from its build-stamped entry.
        recovered = textures.refresh_all_unhydrated_materials()
        self.assertGreaterEqual(recovered, 1)
        self.assertFalse(textures._image_less_node_names(g_material))

        # ---- settle: let the zombie retire and the audit run -------------------
        state["jobs"].clear()
        ticks2 = self._pump(state)
        self.assertLess(ticks2, 200, "queue failed to settle after recovery")
        self.assertTrue(state["material_audit_done"])
        self.assertEqual(
            textures.audit_unhydrated_materials(), 0,
            "audit must be clean after the full adversarial pass",
        )
        print(
            f"[Torture] {ticks}+{ticks2} ticks, "
            f"{state['materials_refreshed']} refreshes, "
            f"attempts={dict(state['material_refresh_attempts'])}, "
            f"recovered={recovered}"
        )


if __name__ == "__main__":
    unittest.main()
