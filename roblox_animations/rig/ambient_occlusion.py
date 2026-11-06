"""Regular EEVEE screen-space occlusion for imported Roblox lighting."""


def configure_ambient_occlusion(scene, global_shadows=True):
    """Enable the standard AO pass, preserving sampling and tracing quality.

    EEVEE Next exposes AO through its ray-tracing/Fast GI controls; older
    EEVEE versions use GTAO. No probe bakes or material changes are needed.
    Source GlobalShadows=false disables this additional occlusion.
    """
    if not str(scene.render.engine).startswith('BLENDER_EEVEE'):
        return False
    eevee = getattr(scene, 'eevee', None)
    if eevee is None:
        return False
    enabled = bool(global_shadows)
    if all(hasattr(eevee, key) for key in ('use_raytracing', 'use_fast_gi', 'fast_gi_method')):
        if enabled:
            eevee.fast_gi_method = 'AMBIENT_OCCLUSION_ONLY'
            eevee.use_fast_gi = True
            eevee.use_raytracing = True
        else:
            eevee.use_fast_gi = False
        return enabled
    if hasattr(eevee, 'use_gtao'):
        eevee.use_gtao = enabled
        return enabled
    return False
