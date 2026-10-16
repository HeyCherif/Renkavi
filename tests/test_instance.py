import tempfile
import unittest
from pathlib import Path
from backend.instance import InstanceLock,AlreadyRunning,existing_url


class InstanceTests(unittest.TestCase):
    def test_second_launcher_uses_existing_instance_and_lock_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            with InstanceLock(tmp) as lock:
                lock.set_url('http://127.0.0.1:8765/')
                self.assertEqual(existing_url(tmp),'http://127.0.0.1:8765/')
                with self.assertRaises(AlreadyRunning):
                    with InstanceLock(tmp):pass
            self.assertIsNone(existing_url(tmp))
            with InstanceLock(tmp):pass

    def test_untrusted_saved_url_is_not_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'.instance.json').write_text('{"url":"https://external.example/"}')
            self.assertIsNone(existing_url(tmp))
