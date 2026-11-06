"""Display-space color correction shared by the compositor and regressions.

Roblox brightness is an additive color offset, separate from Lighting's
exposure stops. This affine approximation is checked against paired native
captures; renderer/tonemapper differences still limit exact agreement.
"""


def active_corrections(effects):
    effects = effects or {}
    candidates = effects.get('color_corrections')
    if candidates is None:
        effect = effects.get('color_correction')
        candidates = [effect] if effect else []
    return [effect for effect in candidates if effect.get('enabled', True)]


def correction_matrix(effects):
    """Compose enabled corrections; last column is the additive offset."""
    matrix = [[float(i == j) for j in range(3)] + [0.] for i in range(3)]
    for effect in active_corrections(effects):
        saturation = 1 + max(-1., min(1., float(effect.get('saturation', 0))))
        contrast = 1 + max(-1., min(1., float(effect.get('contrast', 0))))
        brightness = max(-1., min(1., float(effect.get('brightness', 0))))
        tint = effect.get('tint') or (1., 1., 1.)
        luminance = (.299, .587, .114)
        transform = [[contrast * float(tint[i]) *
                      ((saturation if i == j else 0) + (1 - saturation) * luminance[j])
                      for j in range(3)] for i in range(3)]
        offset = [(brightness + .5 * (1 - contrast)) * float(tint[i]) for i in range(3)]
        matrix = [[sum(transform[i][k] * matrix[k][j] for k in range(3))
                   + (offset[i] if j == 3 else 0) for j in range(4)] for i in range(3)]
    return matrix
