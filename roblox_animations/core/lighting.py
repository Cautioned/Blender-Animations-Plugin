"""Renderer-independent Roblox lighting geometry."""
import math


def sun_direction(clock_time, geographic_latitude):
    """Return the Roblox Y-up direction toward the sun.

    Studio rotates its daily solar arc in XY and offsets the arc in Z by
    latitude minus Earth's 23.5-degree tilt. This matches GetSunDirection
    samples from Studio 0.746 at midnight, sunrise, noon and afternoon;
    it is not the terrestrial equinox-altitude approximation.
    """
    hour = math.radians((float(clock_time) % 24.0 - 12.0) * 15.0)
    tilt = math.radians(float(geographic_latitude) - 23.5)
    radius = math.cos(tilt)
    return (-math.sin(hour) * radius, math.cos(hour) * radius, math.sin(tilt))


def fallback_environment_color(lighting, daylight):
    """Calibrated no-Sky Realistic fill, independent of the visible sky.

    Fitted against Studio 0.746 daylight, night, ambient and diffuse-scale
    controls. This remains an approximation of indoor/outdoor occlusion;
    imported sky images use their separate environment path.
    """
    ambient = lighting.get("ambient") or (0.0, 0.0, 0.0)
    outdoor = lighting.get("outdoor_ambient") or ambient
    if lighting.get("global_shadows", True):
        source = [0.2 * ambient[i] + 0.8 * max(ambient[i], outdoor[i]) for i in range(3)]
    else:
        source = ambient
    def linear(value):
        value = max(0.0, min(1.0, float(value)))
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    diffuse = max(0.0, min(1.0, float(lighting.get("environment_diffuse_scale", 1.0))))
    day = max(0.0, min(1.0, float(daylight)))
    return tuple(2.3 * linear(source[i]) + day * diffuse * (0.18, 0.45, 0.70)[i] for i in range(3))
