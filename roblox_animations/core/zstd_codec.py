"""Bounded Zstandard decoding with an offline, bundled Blender dependency."""

import importlib
import platform
import sys
import sysconfig
import tempfile
import threading
import zipfile
from pathlib import Path


_codec = None
_codec_directory = None
_codec_lock = threading.Lock()


def _wheel_matches(filename, python_tag, system, machine):
    parts = filename.removesuffix('.whl').split('-')
    if len(parts) != 5 or parts[2:4] != [python_tag, python_tag]:
        return False
    tags = parts[4].split('.')
    if system == 'win32':
        return 'win_' + machine in tags
    if system == 'darwin':
        return any(tag.startswith('macosx_') and tag.endswith('_' + machine) for tag in tags)
    if system == 'linux':
        return any(tag.startswith('manylinux') and tag.endswith('_' + machine) for tag in tags)
    return False


def _load_codec():
    """Extensions install their wheels; legacy/source installs unpack one offline."""
    global _codec, _codec_directory
    with _codec_lock:
        if _codec is not None:
            return _codec
        try:
            _codec = importlib.import_module('compression.zstd')
            return _codec
        except ModuleNotFoundError as exc:
            if exc.name not in {'compression', 'compression.zstd', '_zstd'}:
                raise
        try:
            _codec = importlib.import_module('backports.zstd')
            return _codec
        except ModuleNotFoundError as exc:
            if exc.name not in {'backports', 'backports.zstd'}:
                raise

        python_tag = f'cp{sys.version_info.major}{sys.version_info.minor}'
        machine = platform.machine().lower()
        if sys.platform == 'win32':
            machine = sysconfig.get_platform().removeprefix('win-')
        wheel_dir = Path(__file__).resolve().parents[1] / 'wheels'
        candidates = sorted(path for path in wheel_dir.glob('backports_zstd-*.whl')
                            if _wheel_matches(path.name, python_tag, sys.platform, machine))
        if not candidates:
            raise ImportError(f'No bundled Zstandard decoder for {python_tag} {sys.platform}/{machine}')
        directory = tempfile.TemporaryDirectory(prefix='rbx_zstd_', ignore_cleanup_errors=True)
        try:
            # Only the bundled, pinned wheel is extracted, never imported model data.
            with zipfile.ZipFile(candidates[0]) as archive:
                archive.extractall(directory.name)
            sys.path.insert(0, directory.name)
            parent_path = None
            backports_path = str(Path(directory.name) / 'backports')
            try:
                parent = sys.modules.get('backports')
                if parent is not None and backports_path not in parent.__path__:
                    parent_path = parent.__path__
                    parent.__path__ = list(parent_path) + [backports_path]
                importlib.invalidate_caches()
                _codec = importlib.import_module('backports.zstd')
            finally:
                sys.path.remove(directory.name)
                if parent_path is not None:
                    parent.__path__ = parent_path
            # Keep native modules available for this process, including on Windows.
            _codec_directory = directory
            return _codec
        except Exception:
            directory.cleanup()
            raise


def decompress_frame(data, expected_length, max_length):
    """Decode exactly one frame, limiting both its history window and output."""
    if not 0 <= expected_length <= max_length:
        raise ValueError('zstd chunk exceeds import safety limit')
    codec = _load_codec()
    try:
        return _decode_frame(codec, data, expected_length, max_length)
    except codec.ZstdError as exc:
        raise ValueError(f'malformed zstd frame: {exc}') from exc


def _decode_frame(codec, data, expected_length, max_length):
    frame_size = codec.get_frame_info(data).decompressed_size
    if frame_size is not None and frame_size != expected_length:
        raise ValueError('zstd frame size does not match chunk header')
    decoder = codec.ZstdDecompressor(options={
        codec.DecompressionParameter.window_log_max: max(10, (max_length - 1).bit_length()),
    })
    output = decoder.decompress(data, max_length=expected_length + 1)
    if len(output) > expected_length:
        raise ValueError('zstd output exceeds declared chunk length')
    if not decoder.eof:
        raise ValueError('truncated zstd frame or output exceeds declared chunk length')
    if decoder.unused_data:
        raise ValueError('extra data after zstd frame')
    if len(output) != expected_length:
        raise ValueError('zstd output length does not match chunk header')
    return output
