# Bundled Zstandard decoder

`backports.zstd` 1.8.0 is the backport of Python's `compression.zstd` module.
Upstream: https://github.com/Rogdham/backports.zstd
Distribution: https://pypi.org/project/backports.zstd/1.8.0/

The official PyPI wheels cover CPython 3.11–3.13 on Windows x64/ARM64,
macOS Intel/Apple Silicon, and Linux x64/ARM64 (glibc 2.17+). Python 3.14+
uses its standard-library decoder when available. Unsupported runtimes receive
an explicit decoder-availability error. LZ4 imports do not load this dependency.

Blender Extensions installs the compatible wheel declared in the manifest.
Legacy and source installs unpack only the matching wheel into a temporary
process-owned directory. Runtime imports never download packages, run pip,
invoke external decompressors, or change the addon installation directory.

`zstd-lock.json` records each original download URL, byte size, and SHA-256.
Each unmodified wheel includes the upstream package and dependency licenses
under its `.dist-info/licenses` directory (PSF, BSD, and zstd licensing).

To verify or restore the pinned artifacts:

```sh
python3 tools/vendor_zstd.py --check
python3 tools/vendor_zstd.py --download
```

The decoder limits the history window and output size and requires exactly
one complete frame matching the Roblox chunk's declared decompressed length.
