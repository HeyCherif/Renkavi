"""Build the Windows desktop ZIP and a deliberately scoped source archive.

Run from a clean virtualenv: python scripts/build_release.py --version 0.3.0-beta.1
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    'run.py', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'requirements.txt', 'requirements-desktop.txt', 'requirements-build.txt',
    'Renkavi.spec', '.gitignore', 'LANCER.bat', 'INSTALLER.bat', 'scripts/build_release.py',
    'scripts/migrate_layout_categories.py',
    'docs/release.md', '.github/workflows/release.yml',
)
SOURCE_TREES = {
    'backend': {'.py'},
    'frontend': {'.html', '.css', '.js', '.svg', '.png', '.ico', '.woff2', '.json'},
    'assets/fonts': {'.ttf', '.otf', '.txt'},
    'assets/brand': {'.svg', '.png', '.ico'},
    'tests': {'.py'},
}


def source_files(root: Path = ROOT):
    """Never walk repository history, libraries, user data, caches or weights."""
    for name in SOURCE_FILES:
        path = root / name
        if path.is_file():
            yield path
    for directory, extensions in SOURCE_TREES.items():
        for path in sorted((root / directory).rglob('*')):
            if (path.is_file() and (path.suffix.lower() in extensions or path.name.endswith('LICENSE') or path.name.startswith('LICENSE'))
                    and not {'__pycache__', 'custom'} & set(path.relative_to(root).parts)
                    and not path.is_symlink()):
                yield path


def write_source_archive(destination: Path, version: str, root: Path = ROOT):
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in source_files(root):
            archive.write(path, f'Renkavi-{version}/{path.relative_to(root).as_posix()}')


def collect_licenses(destination: Path):
    """Preserve installed distributions' actual license/notice files and metadata.

    A dedicated build environment prevents unrelated packages entering notices.
    This does not assert that a package's metadata grants redistribution rights.
    """
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = distribution.metadata['Name']
        target = destination / re.sub(r'[^A-Za-z0-9._-]', '_', name)
        target.mkdir(exist_ok=True)
        copied = []
        for entry in distribution.files or ():
            parts = entry.parts
            filename = entry.name.lower()
            if (filename.startswith(('license', 'copying', 'notice', 'authors'))
                    or any(part.lower() in {'licenses', 'license_files'} for part in parts)):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    # Preserve original relative paths, including namespaced notices.
                    relative = Path(*[p for p in parts if p not in {'..', '.'}])
                    output = target / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, output)
                    copied.append(relative.as_posix())
        # Preserve the installed text. Re-serializing email-style metadata can
        # fail on valid multiline Description fields under Python 3.14.
        metadata_text = distribution.read_text('METADATA') or distribution.read_text('PKG-INFO')
        if metadata_text is None:
            metadata_text = json.dumps({'name': name, 'version': distribution.version}, indent=2)
        (target / 'METADATA.txt').write_text(metadata_text, encoding='utf-8')
        inventory.append({'name': name, 'version': distribution.version, 'license_files': copied})
    (destination / 'inventory.json').write_text(json.dumps(inventory, indent=2), encoding='utf-8')


def checksum_files(paths: list[Path], destination: Path):
    lines = []
    for path in paths:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        lines.append(f'{digest.hexdigest()}  {path.name}\n')
    destination.write_text(''.join(lines), encoding='ascii')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='0.3.0-beta.1')
    parser.add_argument('--output', type=Path, default=ROOT / 'release')
    parser.add_argument('--source-only', action='store_true', help='Only produce the scoped source ZIP (any OS)')
    args = parser.parse_args()
    version = args.version.removeprefix('v')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version):
        parser.error('version must be a semantic version, e.g. 0.3.0-beta.1')
    if not args.source_only and (sys.platform != 'win32' or platform.machine().lower() not in {'amd64', 'x86_64'}):
        parser.error('the desktop release must be built with Windows x64 Python')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    if not args.source_only:
        if not (ROOT / 'assets/brand/renkavi.ico').is_file():
            parser.error('missing application icon: assets/brand/renkavi.ico')
        with tempfile.TemporaryDirectory(prefix='renkavi-build-') as temporary:
            work = Path(temporary)
            collect_licenses(work / 'licenses')
            env = dict(os.environ, RENKAVI_LICENSE_DIR=str(work / 'licenses'))
            subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                            '--workpath', str(work / 'work'), '--distpath', str(work / 'dist'),
                            str(ROOT / 'Renkavi.spec')], cwd=ROOT, env=env, check=True)
            executable = work / 'dist/Renkavi/Renkavi.exe'
            # Exercise the actual frozen program before calling an archive a release.
            subprocess.run([str(executable), '--no-browser', '--port', '0',
                            '--data-dir', str(work / 'smoke-library'), '--smoke-test', '2'],
                           cwd=work, timeout=90, check=True)
            portable = output / f'Renkavi-{version}-windows-x64.zip'
            with zipfile.ZipFile(portable, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for path in sorted((work / 'dist/Renkavi').rglob('*')):
                    if path.is_file():
                        archive.write(path, path.relative_to(work / 'dist').as_posix())
            artifacts.append(portable)
    source = output / f'Renkavi-{version}-source.zip'
    write_source_archive(source, version)
    artifacts.append(source)
    checksum_files(artifacts, output / 'SHA256SUMS')
    for artifact in artifacts:
        print(artifact)


if __name__ == '__main__':
    main()
