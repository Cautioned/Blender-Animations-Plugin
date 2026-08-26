#!/usr/bin/env bash
# Build Blender addon without tests
# Usage: ./build_addon.sh [version] [--dev]

set -euo pipefail

DEV_BUILD=0
VERSION=""

# Parse arguments
for arg in "$@"; do
    if [[ "$arg" == "--dev" ]]; then
        DEV_BUILD=1
    else
        VERSION="$arg"
    fi
done

ADDON_NAME="roblox_animations"

if [[ -z "$VERSION" ]]; then
    # Extract version from __init__.py
    VERSION=$(python3 - <<'PY'
import re
from pathlib import Path
content = Path('roblox_animations/__init__.py').read_text(encoding='utf-8')
m = re.search(r'"version"\s*:\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', content)
if m:
    print(f"v{m.group(1)}.{m.group(2)}.{m.group(3)}")
else:
    print("dev")
PY
)
fi

ZIP_NAME3="rbx_anims_${VERSION}_legacy.zip"
ZIP_NAME4="rbx_anims_${VERSION}.zip"

echo "Building $ZIP_NAME3 and $ZIP_NAME4 without tests..."

# Remove existing zip if it exists
rm -f "$ZIP_NAME3" "$ZIP_NAME4"

# Create temporary directory
rm -rf "temp_build"
mkdir -p "temp_build"

# Copy addon files excluding tests (unless --dev) and cache folders
python3 - "$DEV_BUILD" "$ADDON_NAME" "temp_build" <<'PY'
import sys
import shutil
from pathlib import Path

dev_build = bool(int(sys.argv[1]))
addon_name = sys.argv[2]
temp_dir = sys.argv[3]

excluded_names = {
    '__pycache__', '.ruff_cache', '.pytest_cache', '.mypy_cache',
    '.vscode', '.git', '.idea'
}
if not dev_build:
    excluded_names.add('tests')

src = Path(addon_name)
dst_root = Path(temp_dir) / addon_name

for item in src.rglob('*'):
    if item.name in excluded_names:
        continue
    if any(part in excluded_names for part in item.parts):
        continue
    # The native decoder (_rbxdec) is a pre-compiled C extension: extensions
    # platform builds must be pure Python (ToS 3.6).  It's a best-effort
    # speedup with Pillow/bpy fallbacks, so drop the whole directory — the
    # .bat build already does.
    if 'native' in item.parts:
        continue

    rel = item.relative_to(src)
    target = dst_root / rel
    if item.is_dir():
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
PY

# Create zips for both layouts
# 1) Blender 3.x: keep roblox_animations as the root folder inside the zip
(
    cd "temp_build"
    zip -r -9 "../$ZIP_NAME3" "roblox_animations"
)

# 2) Blender 4.x+: place addon contents at the zip root (manifest at top level)
(
    cd "temp_build/roblox_animations"
    zip -r -9 "../../$ZIP_NAME4" .
)

# Clean up
rm -rf "temp_build"

echo "Built $ZIP_NAME3 and $ZIP_NAME4 successfully (excluded tests and cache directories)"
