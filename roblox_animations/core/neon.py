"""Measured Realistic Neon response and source BloomEffect controls.

Constants are fitted to native Studio captures; this is an EEVEE
approximation of the engine's display response, not an engine shader.
"""
import math

NEON_RADIANCE = 4.0
NEON_RESPONSE_POWER = 3.17
NEON_RESPONSE_K = .00362 * NEON_RADIANCE**NEON_RESPONSE_POWER
NEON_EXPOSURE_SCALE = .52
NEON_AOV = 'RBX_NeonRadiance'
NEON_PIPELINE_VERSION = 2


def neon_response(value):
    value=max(0.0,float(value))**NEON_RESPONSE_POWER
    return value/(value+NEON_RESPONSE_K)


def bloom_settings(effects):
    """An enabled zero-intensity effect suppresses the implicit Neon glow."""
    effects=effects or {}
    candidates=effects.get('blooms')
    if candidates is None:
        effect=effects.get('bloom')
        candidates=[effect] if effect else []
    active=[dict(effect) for effect in candidates if effect.get('enabled',True)]
    if active:
        return [dict(intensity=max(0,float(e.get('intensity',1))),
                     size=max(0,float(e.get('size',24))),
                     threshold=max(0,float(e.get('threshold',.95))),implicit=False)
                for e in active]
    return [dict(intensity=.70,size=24.0,threshold=1.0,implicit=True)]
