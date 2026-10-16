"""Real local HTTP fixtures; no external requests or real model downloads."""
import copy
import hashlib
import http.server
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from backend.ai.model_hub import CURATED_MODELS, ModelHub, _weight, _HTTPSRedirects
from backend.projects.store import atomic_json


class ModelHubTests(unittest.TestCase):
    def test_ocr_catalog_has_independent_pinned_downloads(self):
        entries=[row for row in CURATED_MODELS if row['kind']=='ocr' and row.get('downloadable')]
        self.assertEqual({row['filename'] for row in entries},{'PP-OCRv6_det_small.onnx','PP-OCRv6_rec_small.onnx'})
        self.assertTrue(all('/resolve/v3.9.2/onnx/PP-OCRv6/' in row['source_url'] for row in entries))

    def test_ocr_download_installs_canonical_verified_file_without_registry_change(self):
        entries=[]
        for part in ('det','rec'):
            entry=copy.deepcopy(self.fixture)
            entry.update(id=f'ocr-{part}-fixture',kind='ocr',filename=f'PP-OCRv6_{part}_small.onnx',revision='v3.9.2',
                         source_url=f'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/{part}/PP-OCRv6_{part}_small.onnx')
            entries.append(entry)
        hub=ModelHub(self.root,state_dir=self.state,models=entries,opener=self.opener)
        atomic_json(self.root/'registry.json',{'selected':{'translation':'keep','inpainting':'keep-paint'},'models':[]})
        registry_before=(self.root/'registry.json').read_bytes()
        for index,entry in enumerate(entries):
            job=hub.start(entry['id']);self.wait(hub,job['id'],'downloaded')
            hub.control(job['id'],'install');self.wait(hub,job['id'],'installed')
            self.assertEqual((self.root/'ocr'/entry['filename']).read_bytes(),self.payload)
            self.assertEqual([row['installed'] for row in hub.catalog()['models']],[True,index==1])
        self.assertEqual((self.root/'registry.json').read_bytes(),registry_before)
        self.assertTrue(hub.catalog()['models'][0]['installed'])
        (self.root/'ocr'/entries[0]['filename']).write_bytes(b'bad!'+self.payload[4:])
        self.assertEqual([row['installed'] for row in hub.catalog()['models']],[False,True])
        # Reverification of the staging copy preserves a previous canonical
        # weight if its download changes while the installer copies it.
        canonical=self.root/'ocr'/entries[1]['filename']
        bad_source=self.root/'bad.onnx';bad_source.write_bytes(b'bad!'+self.payload[4:])
        with self.assertRaisesRegex(ValueError,'SHA-256'):
            hub._install_registry(entries[1],bad_source)
        self.assertEqual(canonical.read_bytes(),self.payload)
        self.assertFalse(list(canonical.parent.glob('*.tmp')))

    def test_modelscope_sources_reject_other_repositories_tags_and_filenames(self):
        entry=next(copy.deepcopy(row) for row in CURATED_MODELS if row.get('downloadable') and row['kind']=='ocr')
        for changed in (entry['source_url'].replace('/RapidAI/','/someone/'),entry['source_url'].replace('/v3.9.2/','/main/'),entry['source_url']+'?query=x',entry['source_url'].replace('www.modelscope.cn','cdn-lfs-cn-1.modelscope.cn')):
            entry['source_url'],original=changed,entry['source_url']
            with self.assertRaisesRegex(ValueError,'Source'):
                ModelHub(self.root,state_dir=self.state,models=[entry],opener=self.opener)
            entry['source_url']=original

    def test_modelscope_redirect_hosts_are_restricted_and_hf_rules_preserved(self):
        redirects=_HTTPSRedirects()
        request=urllib.request.Request('https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/a.onnx')
        good='https://cdn-lfs-cn-1.modelscope.cn/weight'
        self.assertIsNotNone(redirects.redirect_request(request,None,302,'Found',{},good))
        for target in ('http://cdn-lfs-cn-1.modelscope.cn/weight','https://evil.modelscope.cn/weight','https://cdn-lfs-cn-1.modelscope.cn.evil.com/weight','https://user@cdn-lfs-cn-1.modelscope.cn/weight'):
            with self.assertRaises(ValueError):redirects.redirect_request(request,None,302,'Found',{},target)
        hf=urllib.request.Request('https://huggingface.co/a/resolve/main/weight')
        self.assertIsNotNone(redirects.redirect_request(hf,None,302,'Found',{},'https://cdn-lfs.hf.co/weight'))
        with self.assertRaises(ValueError):redirects.redirect_request(hf,None,302,'Found',{},good)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "models"
        self.state = Path(self.temp.name) / "library" / "model-downloads"
        self.payload = b"GGUF" + bytes(range(256)) * 768
        self.mode = "range"
        self.delay = 0
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requested_range = self.headers.get("Range")
                owner.requests.append(requested_range)
                offset = int(requested_range.split("=")[1].split("-")[0]) if requested_range else 0
                if owner.mode == "ignore":
                    offset = 0
                content = owner.payload[offset:]
                if owner.mode == "corrupt":
                    content = b"GGUF" + b"x" * (len(content)-4)
                self.send_response(206 if requested_range and owner.mode != "ignore" else 200)
                if requested_range and owner.mode != "ignore":
                    self.send_header("Content-Range", f"bytes {offset if owner.mode != 'bad-range' else 0}-{len(owner.payload)-1}/{len(owner.payload)}")
                self.send_header("Content-Length", len(content) + (1 if owner.mode == "bad-size" else 0))
                self.end_headers()
                try:
                    if owner.mode == "short":
                        content = content[:len(content)//2]
                    for start in range(0, len(content), 1024):
                        self.wfile.write(content[start:start+1024])
                        self.wfile.flush()
                        if owner.delay:
                            time.sleep(owner.delay)
                except (OSError, ConnectionError):
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.chunk_patch = patch("backend.ai.model_hub.CHUNK_BYTES", 1024)
        self.chunk_patch.start()
        self.addCleanup(self.chunk_patch.stop)
        self.fixture = _weight("fixture-gguf", "Fixture GGUF", "translation", "fixture/weights", "a"*40,
                               "fixture.gguf", len(self.payload), hashlib.sha256(self.payload).hexdigest(), "Test fixture")

    def opener(self, request, timeout=30):
        # Production validation still sees the curated HTTPS HF URL; only this
        # injected transport maps it to the test HTTP server.
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/weight", headers=dict(request.header_items()))
        return urllib.request.urlopen(request, timeout=timeout)

    def hub(self, **kwargs):
        return ModelHub(self.root, state_dir=self.state, models=[self.fixture], opener=self.opener, **kwargs)

    def wait(self, hub, job_id, state, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = next(job for job in hub.list_downloads() if job["id"] == job_id)
            if job["state"] == state and not hub._threads[job_id].is_alive():
                return job
            time.sleep(.01)
        self.fail(f"Expected {state}, got {hub.list_downloads()}")

    def test_listing_is_offline_and_does_not_download_or_install(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("unexpected network")):
            hub = ModelHub(self.root, state_dir=self.state)
            catalog = hub.catalog()
        self.assertGreaterEqual(len(catalog["models"]), 8)
        self.assertFalse(catalog["downloads"])
        self.assertFalse(any(row["downloaded"] or row["installed"] for row in catalog["models"]))
        self.assertFalse((self.root / "downloads").exists())
        for model in CURATED_MODELS:
            if model.get("downloadable"):
                self.assertEqual(len(model["sha256"]), 64)
                self.assertIn(model["revision"], model["source_url"])
                self.assertTrue(model["license_url"].startswith("https://"))

    def test_download_verified_then_explicit_install_preserves_selection(self):
        installed = []

        def installer(entry, path):
            installed.append((entry, path))
            atomic_json(self.root / "registry.json", {"schema_version": 1, "models": [
                {"id": "downloaded-model", "kind": "translation", "path": str(path), "sha256": entry["sha256"]}],
                "selected": {"translation": "existing-choice"}})

        hub = self.hub(installer=installer)
        job = hub.start(self.fixture["id"])
        result = self.wait(hub, job["id"], "downloaded")
        self.assertEqual(result["progress"], 1)
        self.assertEqual(result["bytes_downloaded"], len(self.payload))
        self.assertFalse(installed)
        partial, destination = hub._paths(result)
        self.assertEqual(destination.read_bytes(), self.payload)
        self.assertFalse(partial.exists())
        self.assertTrue(hub.catalog()["models"][0]["downloaded"])
        hub.control(job["id"], "install")
        self.wait(hub, job["id"], "installed")
        self.assertEqual(len(installed), 1)
        self.assertTrue(hub.catalog()["models"][0]["installed"])
        self.assertEqual(json.loads((self.root / "registry.json").read_text())["selected"]["translation"], "existing-choice")

    def paused(self):
        self.delay = .002
        hub = self.hub()
        job = hub.start(self.fixture["id"])
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if hub.list_downloads()[0]["bytes_downloaded"] >= 4096:
                break
            time.sleep(.002)
        hub.control(job["id"], "pause")
        result = self.wait(hub, job["id"], "paused")
        self.assertGreater(result["bytes_downloaded"], 0)
        self.assertLess(result["bytes_downloaded"], len(self.payload))
        return hub, result

    def test_pause_resume_uses_correct_range_and_exact_result(self):
        hub, job = self.paused()
        offset = job["bytes_downloaded"]
        self.delay = 0
        hub.control(job["id"], "resume")
        result = self.wait(hub, job["id"], "downloaded")
        self.assertEqual(self.requests[-1], f"bytes={offset}-")
        self.assertEqual(hub._paths(result)[1].read_bytes(), self.payload)

    def test_range_ignored_http_200_truncates_instead_of_appending(self):
        hub, job = self.paused()
        self.mode, self.delay = "ignore", 0
        hub.control(job["id"], "resume")
        result = self.wait(hub, job["id"], "downloaded")
        self.assertEqual(hub._paths(result)[1].read_bytes(), self.payload)

    def test_wrong_content_range_fails_without_final_artifact(self):
        hub, job = self.paused()
        self.mode, self.delay = "bad-range", 0
        hub.control(job["id"], "resume")
        result = self.wait(hub, job["id"], "failed")
        self.assertIn("reprise HTTP", result["error"])
        self.assertFalse(hub._paths(result)[1].exists())

    def test_cancel_closes_and_removes_partial_on_windows(self):
        hub, job = self.paused()
        hub.control(job["id"], "resume")
        hub.control(job["id"], "cancel")
        result = self.wait(hub, job["id"], "cancelled")
        self.assertEqual(result["bytes_downloaded"], 0)
        self.assertFalse(hub._paths(result)[0].exists())
        self.assertFalse(hub._paths(result)[1].exists())

    def test_corrupt_sha_and_bad_size_never_expose_final_file(self):
        for mode, error in (("corrupt", "SHA-256"), ("bad-size", "Taille annoncée")):
            with self.subTest(mode=mode):
                self.mode = mode
                hub = self.hub()
                job = hub.start(self.fixture["id"])
                result = self.wait(hub, job["id"], "failed")
                self.assertIn(error, result["error"])
                self.assertFalse(hub._paths(result)[1].exists())
                self.assertFalse(hub.catalog()["models"][0]["downloaded"])

    def test_short_body_remains_resumable(self):
        self.mode = "short"
        hub = self.hub()
        job = hub.start(self.fixture["id"])
        result = self.wait(hub, job["id"], "failed")
        partial = hub._paths(result)[0]
        self.assertTrue(partial.exists())
        offset = partial.stat().st_size
        self.mode = "range"
        hub.control(job["id"], "resume")
        self.wait(hub, job["id"], "downloaded")
        self.assertEqual(self.requests[-1], f"bytes={offset}-")

    def test_restart_marks_own_job_paused_without_touching_other_state_directory(self):
        hub, job = self.paused()
        path = hub._state_path(job["id"])
        persisted = json.loads(path.read_text())
        persisted["state"] = "downloading"
        atomic_json(path, persisted)
        other = ModelHub(self.root, state_dir=self.state.parent / "other-downloads", models=[self.fixture], opener=self.opener)
        self.assertEqual(other.list_downloads(), [])
        self.assertEqual(json.loads(path.read_text())["state"], "downloading")
        restarted = self.hub()
        self.assertEqual(restarted.list_downloads()[0]["state"], "paused")
        self.delay = 0
        restarted.control(job["id"], "resume")
        self.wait(restarted, job["id"], "downloaded")

    def test_install_uses_non_activating_registry_contract_and_verifies_again(self):
        hub = self.hub()
        job = hub.start(self.fixture["id"])
        result = self.wait(hub, job["id"], "downloaded")
        with patch("backend.ai.model_hub.registry.add_model", return_value={}) as add:
            hub.control(job["id"], "install")
            self.wait(hub, job["id"], "installed")
            add.assert_called_once_with("translation", str(hub._paths(result)[1]), root=self.root.resolve(), activate=False, test=False)
        destination = hub._paths(result)[1]
        destination.write_bytes(b"GGUF" + b"z" * (len(self.payload)-4))
        self.assertFalse(hub.catalog()["models"][0]["downloaded"])
        with self.assertRaises(ValueError):
            hub.control(job["id"], "install")

    def test_rejects_untrusted_sources_paths_sizes_and_arbitrary_api_ids(self):
        for changes in ({"source_url": "http://huggingface.co/a/resolve/main/fixture.gguf"},
                        {"source_url": "https://example.com/a/resolve/main/fixture.gguf"},
                        {"filename": "../escape.gguf"}, {"size_bytes": 21 * 1024**3}, {"sha256": "nope"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ModelHub(self.root, state_dir=self.state, models=[{**self.fixture, **changes}])
        hub = self.hub()
        with self.assertRaises(ValueError):
            hub.start("https://example.com/weights.gguf")
        with self.assertRaises(ValueError):
            hub.control("../escape", "cancel")

    def test_invalid_gguf_magic_rejected_even_if_sha_matches(self):
        self.payload = b"HTML" + self.payload[4:]
        self.fixture["sha256"] = hashlib.sha256(self.payload).hexdigest()
        hub = self.hub()
        job = hub.start(self.fixture["id"])
        result = self.wait(hub, job["id"], "failed")
        self.assertIn("GGUF", result["error"])
        self.assertFalse(hub._paths(result)[1].exists())

    def test_checksum_failure_resume_restarts_cleanly_and_disk_space_is_checked(self):
        from collections import namedtuple
        Usage = namedtuple("usage", "total used free")
        hub = self.hub()
        with patch("backend.ai.model_hub.shutil.disk_usage", return_value=Usage(10, 10, 0)):
            with self.assertRaisesRegex(OSError, "Espace disque"):
                hub.start(self.fixture["id"])
        self.assertEqual(hub.list_downloads(), [])
        self.mode = "corrupt"
        job = hub.start(self.fixture["id"])
        result = self.wait(hub, job["id"], "failed")
        self.assertFalse(hub._paths(result)[0].exists())
        self.mode = "range"
        hub.control(job["id"], "resume")
        result = self.wait(hub, job["id"], "downloaded")
        self.assertIsNone(self.requests[-1])
        self.assertEqual(result["bytes_done"], result["bytes_downloaded"])
        self.assertEqual(result["speed_bps"], result["speed_bytes_s"])

    def test_other_library_reuses_cached_artifact_offline_without_download_space(self):
        from collections import namedtuple
        Usage = namedtuple("usage", "total used free")
        first = self.hub()
        job = first.start(self.fixture["id"])
        self.wait(first, job["id"], "downloaded")
        second = ModelHub(self.root, state_dir=self.state.parent / "second-downloads", models=[self.fixture],
                          opener=lambda *args, **kwargs: self.fail("shared artifact fetched again"))
        with patch("backend.ai.model_hub.shutil.disk_usage", return_value=Usage(10, 10, 0)):
            reused = second.start(self.fixture["id"])
            result = self.wait(second, reused["id"], "downloaded")
        self.assertTrue(second.catalog()["models"][0]["downloaded"])
        self.assertEqual(first.list_downloads()[0]["state"], "downloaded")
        self.assertEqual(second._paths(result)[1].read_bytes(), self.payload)

    def test_repeated_pause_during_verification_does_not_publish_or_redownload(self):
        hub = self.hub()
        verify = hub._verify
        entered, release = threading.Event(), threading.Event()
        def slow_verify(file, entry):
            verify(file, entry)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test verification timed out")
        with patch.object(hub, "_verify", slow_verify):
            job = hub.start(self.fixture["id"])
            for _ in range(2):
                try:
                    self.assertTrue(entered.wait(5))
                    hub.control(job["id"], "pause")
                finally:
                    release.set()
                paused = self.wait(hub, job["id"], "paused")
                self.assertEqual(paused["bytes_downloaded"], len(self.payload))
                self.assertFalse(hub._paths(paused)[1].exists())
                entered.clear()
                release.clear()
                if _ == 0:
                    hub.control(job["id"], "resume")
        hub.control(job["id"], "resume")
        self.wait(hub, job["id"], "downloaded")
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
