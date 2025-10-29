@echo off
REM Build Blender addon without tests
REM Usage: build_addon.bat [version]

if "%~1"=="" (
    REM Extract version from __init__.py
    for /f "tokens=*" %%i in ('powershell -Command "$version = Select-String -Path 'roblox_animations\__init__.py' -Pattern '\"version\":\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)'; if ($version) { $major = $version.Matches[0].Groups[1].Value; $minor = $version.Matches[0].Groups[2].Value; $patch = $version.Matches[0].Groups[3].Value; 'v' + $major + '.' + $minor + '.' + $patch } else { 'dev' }"') do set VERSION=%%i
) else (
    set VERSION=%~1
)

set ADDON_NAME=roblox_animations
set ZIP_NAME_EXTENSION=rbx_anims_%VERSION%.zip
set ZIP_NAME_LEGACY=rbx_anims_%VERSION%_legacy.zip

echo Building zip files without tests...

REM Remove existing zips if they exist
if exist "%ZIP_NAME_EXTENSION%" del "%ZIP_NAME_EXTENSION%"
if exist "%ZIP_NAME_LEGACY%" del "%ZIP_NAME_LEGACY%"

REM Create temporary directories
if exist "temp_build_extension" rmdir /s /q "temp_build_extension"
if exist "temp_build_legacy" rmdir /s /q "temp_build_legacy"
mkdir "temp_build_extension"
mkdir "temp_build_legacy"

REM Copy addon files excluding tests and cache folders using PowerShell
powershell -Command "$addonName = '%ADDON_NAME%'; $tempDirExt = 'temp_build_extension'; $tempDirLeg = 'temp_build_legacy'; function ShouldIncludeItem { param([string]$FullName, [string]$Name, [bool]$IsContainer) $excludedNames = @('tests', '__pycache__', '.ruff_cache', '.pytest_cache', '.mypy_cache', '.vscode', '.git', '.idea'); if ($excludedNames -contains $Name) { return $false } if ($FullName -match '[\\/]tests[\\/]' -or $FullName -match '[\\/]__pycache__[\\/]' -or $FullName -match '[\\/]\.ruff_cache[\\/]') { return $false } return $true }; Get-ChildItem -Path $addonName -Recurse | Where-Object { ShouldIncludeItem -FullName $_.FullName -Name $_.Name -IsContainer $_.PSIsContainer } | ForEach-Object { $targetPathExt = $_.FullName -replace \"^$addonName\", \"$tempDirExt\$addonName\"; $targetPathLeg = $_.FullName -replace \"^$addonName\", \"$tempDirLeg\$addonName\"; if ($_.PSIsContainer) { New-Item -ItemType Directory -Path $targetPathExt -Force | Out-Null; New-Item -ItemType Directory -Path $targetPathLeg -Force | Out-Null } else { Copy-Item $_.FullName -Destination $targetPathExt -Force; Copy-Item $_.FullName -Destination $targetPathLeg -Force } }"

REM Copy manifest to extension build root (for Blender Extensions 4.2+)
copy "%ADDON_NAME%\blender_manifest.toml" "temp_build_extension\blender_manifest.toml"

REM Create zip for Extensions (Blender 4.2+) - manifest at root, addon folder inside
powershell -Command "Compress-Archive -Path 'temp_build_extension\*' -DestinationPath '%ZIP_NAME_EXTENSION%'"

REM Create zip for Legacy (Blender 3.4) - just addon folder (no manifest)
powershell -Command "Compress-Archive -Path 'temp_build_legacy\roblox_animations' -DestinationPath '%ZIP_NAME_LEGACY%'"

REM Clean up
rmdir /s /q "temp_build_extension"
rmdir /s /q "temp_build_legacy"

echo Built %ZIP_NAME_EXTENSION% (for Blender Extensions 4.2+ with manifest)
echo Built %ZIP_NAME_LEGACY% (for legacy Blender 3.4 without manifest)
pause
