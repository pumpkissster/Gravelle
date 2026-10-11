"""Export only reviewed application files, never live libraries or QA data."""
from pathlib import Path, PurePosixPath
import argparse
import ast
import hashlib
import json
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def source_files():
    names = json.loads((ROOT / 'packaging/source_manifest.json').read_text(encoding='utf-8'))
    if not isinstance(names, list) or len(names) != len(set(names)):
        raise ValueError('Release manifest must contain unique file paths.')
    excluded = {'.git', '.env', 'work', 'dist', 'data', 'data-windows', 'library', 'outputs', 'VERIFICATION.md'}
    for name in names:
        relative = PurePosixPath(name)
        path = (ROOT / name).resolve(strict=True)
        if relative.is_absolute() or '..' in relative.parts or relative.parts[0] in excluded:
            raise ValueError('Private or invalid release path: ' + name)
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError('Release file escaped the project: ' + name)
    return names


def application_version():
    tree = ast.parse((ROOT / 'server.py').read_text(encoding='utf-8'))
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'VERSION' for target in node.targets))


def archive(path, names, prefix=''):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as output:
        for name in names:
            output.write(ROOT / name, prefix + name)
    with zipfile.ZipFile(path) as output:
        assert output.testzip() is None
        assert len(output.namelist()) == len(names)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist/release')
    parser.add_argument('--version')
    args = parser.parse_args()
    version = application_version()
    if args.version is not None and args.version != version:
        parser.error('Release version must match server.VERSION.')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    names = source_files()
    archive(args.output_dir / f'Gravelle-Source-{version}.zip', names, 'gravelle-reader/')
    archive(args.output_dir / f'Gravelle-Docker-{version}.zip',
            ['compose.yaml', '.env.example', 'docs/docker-release.md'])
    artifacts = sorted(args.output_dir.glob('Gravelle-*.exe')) + sorted(args.output_dir.glob('Gravelle-*.zip'))
    checksums = '\n'.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name for path in artifacts)
    (args.output_dir / 'SHA256SUMS.txt').write_text(checksums + '\n', encoding='utf-8')
    print(json.dumps({'version': version, 'source_files': len(names), 'assets': [path.name for path in artifacts]}))


if __name__ == '__main__':
    main()
