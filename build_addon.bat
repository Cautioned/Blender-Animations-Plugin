@echo off
REM Build Blender addon without tests
REM Usage: build_addon.bat [version] [--dev]

set DEV_BUILD=0
set VERSION=

:args_loop
if "%~1"=="" goto args_done
if /i "%~1"=="--dev" (
    set DEV_BUILD=1
) else (
    set VERSION=%~1
)
shift
goto args_loop
:args_done

if "%VERSION%"=="" (
    REM Extract version from __init__.py
    for /f "tokens=*" %%i in ('powershell -Command "$version = Select-String -Path 'roblox_animations\__init__.py' -Pattern '\"version\":\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)'; if ($version) { $major = $version.Matches[0].Groups[1].Value; $minor = $version.Matches[0].Groups[2].Value; $patch = $version.Matches[0].Groups[3].Value; 'v' + $major + '.' + $minor + '.' + $patch } else { 'dev' }"') do set VERSION=%%i
)

set ADDON_NAME=roblox_animations
set ZIP_NAME3=rbx_anims_%VERSION%_legacy.zip
set ZIP_NAME4=rbx_anims_%VERSION%.zip

echo Building %ZIP_NAME3% and %ZIP_NAME4% without tests...

REM Remove existing zip if it exists
if exist "%ZIP_NAME3%" del "%ZIP_NAME3%"
if exist "%ZIP_NAME4%" del "%ZIP_NAME4%"

REM Create temporary directory
if exist "temp_build" rmdir /s /q "temp_build"
mkdir "temp_build"

REM Copy addon files excluding tests (unless --dev) and cache folders using PowerShell
powershell -Command "$devBuild = [bool]%DEV_BUILD%; $addonName = '%ADDON_NAME%'; $tempDir = 'temp_build'; function ShouldIncludeItem { param([string]$FullName, [string]$Name, [bool]$IsContainer) $excludedNames = @('__pycache__', '.ruff_cache', '.pytest_cache', '.mypy_cache', '.vscode', '.git', '.idea'); if (-not $devBuild) { $excludedNames += 'tests' }; if ($excludedNames -contains $Name) { return $false } if ($FullName -match '[\\/]__pycache__[\\/]' -or $FullName -match '[\\/]\.ruff_cache[\\/]') { return $false } if ((-not $devBuild) -and ($FullName -match '[\\/]tests[\\/]')) { return $false } return $true }; Get-ChildItem -Path $addonName -Recurse | Where-Object { ShouldIncludeItem -FullName $_.FullName -Name $_.Name -IsContainer $_.PSIsContainer } | ForEach-Object { $targetPath = $_.FullName -replace \"^$addonName\", \"$tempDir\$addonName\"; if ($_.PSIsContainer) { New-Item -ItemType Directory -Path $targetPath -Force | Out-Null } else { Copy-Item $_.FullName -Destination $targetPath -Force } }"

REM Create zips for both layouts
REM Note: Requires 7zip (7z.exe) to be in PATH
REM 1) Blender 3.x: keep roblox_animations as the root folder inside the zip
cd temp_build
7z a -tzip "..\%ZIP_NAME3%" "roblox_animations\*" -mx=9
if errorlevel 1 (
    echo ERROR: Failed to create zip file. Make sure 7zip is installed and in PATH.
    cd ..
    rmdir /s /q "temp_build"
    pause
    exit /b 1
)
cd ..

REM 2) Blender 4.x+: place addon contents at the zip root (manifest at top level)
cd temp_build\roblox_animations
7z a -tzip "..\..\%ZIP_NAME4%" "*" -mx=9
if errorlevel 1 (
    echo ERROR: Failed to create zip file. Make sure 7zip is installed and in PATH.
    cd ..\..
    rmdir /s /q "temp_build"
    pause
    exit /b 1
)
cd ..\..

REM Clean up
rmdir /s /q "temp_build"

echo Built %ZIP_NAME3% and %ZIP_NAME4% successfully (excluded tests and cache directories)
pause
