# Building the Blender Addon

This project includes build scripts to package the addon without the tests directory.

## Build Script

### Batch File
```batch
# Build with auto-detected version from __init__.py
build_addon.bat

# Build with custom version
build_addon.bat v2.2.0
```

## Output

The scripts will create a zip file named `rbx_anim_v[major].[minor].[patch].zip` (e.g., `rbx_anim_v2.1.1.zip`) containing the addon without the tests directory.

## What gets excluded

- `roblox_animations/tests/` directory
- All dot directories (`.git/`, `.vscode/`, `.ruff_cache/`, etc.)
- `__pycache__/` directories
- Common cache directories (`.ruff_cache/`, `.pytest_cache/`, `.mypy_cache/`)
- `*.pyc`, `*.pyo`, `*.pyd` files

## Version Detection

If no version is specified, the script automatically extracts the version from `roblox_animations/__init__.py` in the `bl_info` dictionary.
