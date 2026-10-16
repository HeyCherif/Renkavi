import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.ai import registry


class CatalogLockTests(unittest.TestCase):
    def test_catalog_creation_obeys_catalog_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocked = MagicMock()
            blocked.__enter__.side_effect = ValueError("catalog occupied")
            with patch.object(registry, "LOCK", blocked):
                with self.assertRaisesRegex(ValueError, "catalog occupied"):
                    registry.read_catalog(root)
            self.assertFalse((root / "registry.json").exists())

    def test_nested_lock_and_exception_release_for_another_process(self):
        from backend.catalog_lock import CatalogLock
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.lock"
            lock = CatalogLock(path, timeout=.2)
            with self.assertRaisesRegex(RuntimeError, "intentional"):
                with lock:
                    with lock:
                        raise RuntimeError("intentional")
            result = self.competing_process(path)
            self.assertEqual(result.stdout.strip(), "acquired", result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            with lock:
                with lock:
                    pass

    def test_other_process_times_out_without_entering_protected_section(self):
        from backend.catalog_lock import CatalogLock
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.lock"
            with CatalogLock(path, timeout=.2):
                result = self.competing_process(path)
            self.assertEqual(result.stdout.strip(), "occupied", result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.competing_process(path)
            self.assertEqual(result.stdout.strip(), "acquired", result.stderr)

    @staticmethod
    def competing_process(path):
        script = """import sys
from backend.catalog_lock import CatalogLock
try:
    with CatalogLock(sys.argv[1], timeout=.2):
        print('acquired')
except ValueError:
    print('occupied')
"""
        return subprocess.run([sys.executable, "-c", script, str(path)],
                              capture_output=True, text=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
