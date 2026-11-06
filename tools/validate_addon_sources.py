"""Reject truncated runtime modules and mesh assets before packaging."""
import ast
from pathlib import Path


def validate(root):
    failures=[]
    for path in root.rglob('*'):
        if not path.is_file() or 'tests' in path.parts or '__pycache__' in path.parts:
            continue
        if path.suffix not in ('.py','.mesh'):
            continue
        if not path.stat().st_size:
            failures.append(f'{path}: empty runtime file')
        elif path.suffix=='.py':
            try:
                ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
            except (SyntaxError,UnicodeError) as exc:
                failures.append(f'{path}: {exc}')
    if failures:
        raise SystemExit('Add-on source validation failed:\n'+'\n'.join(failures))


if __name__=='__main__':
    import sys
    validate(Path(sys.argv[1]) if len(sys.argv)>1 else Path('roblox_animations'))
    print('Add-on source validation passed.')
