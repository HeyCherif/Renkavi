import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

spec = importlib.util.spec_from_file_location('build_release', Path(__file__).parents[1] / 'scripts/build_release.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ReleasePackagingTests(unittest.TestCase):
    def test_license_metadata_preserves_raw_multiline_description(self):
        raw = 'Name: altgraph\nVersion: 0.17.5\nDescription: first line\n    continuation\n'
        distribution = Mock()
        distribution.metadata = {'Name': 'altgraph'}
        distribution.version = '0.17.5'
        distribution.files = []
        distribution.read_text.return_value = raw
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build.importlib.metadata, 'distributions', return_value=[distribution]):
                build.collect_licenses(Path(tmp))
            self.assertEqual((Path(tmp) / 'altgraph/METADATA.txt').read_text(), raw)
        distribution.read_text.assert_called_once_with('METADATA')

    def test_source_archive_excludes_private_data_and_unapproved_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('run.py', 'backend/api/server.py', 'frontend/app.js',
                         'assets/fonts/font.ttf', 'assets/fonts/font-OFL.txt',
                         'models/secret.onnx', 'data/project.json', 'archive/history.py',
                         'scripts/private.py', 'backend/__pycache__/private.py',
                         'assets/fonts/custom/personal.ttf', 'frontend/private.txt'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('test', encoding='utf-8')
            archive = root / 'source.zip'
            build.write_source_archive(archive, '0.3.0-beta.1', root)
            with zipfile.ZipFile(archive) as contents:
                self.assertEqual(set(contents.namelist()), {
                    f'Renkavi-0.3.0-beta.1/{name}' for name in (
                        'run.py', 'backend/api/server.py', 'frontend/app.js',
                        'assets/fonts/font.ttf', 'assets/fonts/font-OFL.txt')})

    def test_checksums_cover_actual_archive_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'release.zip'
            archive.write_bytes(b'portable archive')
            destination = Path(tmp) / 'SHA256SUMS'
            build.checksum_files([archive], destination)
            self.assertEqual(destination.read_text(),
                             f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  release.zip\n')


if __name__ == '__main__':
    unittest.main()
