"""Verify/restore the exact upstream PyPI Zstandard wheels in the lock file."""
import argparse
import hashlib
import json
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--download', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'roblox_animations'
    wheels = root / 'wheels'
    lock = json.loads((wheels / 'zstd-lock.json').read_text())
    manifest = (root / 'blender_manifest.toml').read_text()
    for entry in lock['files']:
        filename = entry['filename']
        if Path(filename).name != filename or not filename.endswith('.whl'):
            raise ValueError('Invalid wheel filename in lock file')
        path = wheels / filename
        if args.download and not path.exists():
            url = urllib.parse.urlparse(entry['url'])
            if url.scheme != 'https' or url.hostname != 'files.pythonhosted.org':
                raise ValueError('Wheel must come from official PyPI hosting')
            with urllib.request.urlopen(entry['url'], timeout=60) as response:
                data = response.read(entry['size'] + 1)
            if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise ValueError('Downloaded wheel does not match lock file: ' + filename)
            path.write_bytes(data)
        data = path.read_bytes()
        if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('Wheel does not match lock file: ' + filename)
        if './wheels/' + filename not in manifest:
            raise ValueError('Wheel is not declared in the manifest: ' + filename)
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError('Invalid wheel ZIP: ' + filename)
            if not any('.dist-info/licenses/' in name for name in archive.namelist()):
                raise ValueError('Wheel license files are missing: ' + filename)
    print(f"Verified {len(lock['files'])} pinned Zstandard wheels.")


if __name__ == '__main__':
    main()
