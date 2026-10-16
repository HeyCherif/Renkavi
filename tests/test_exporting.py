import io
import random
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.ai.pipeline import page_directory, signature
from backend.core.project import RevisionConflict
from backend.projects.exporting import export_project_compact
from backend.projects.importer import import_source
from backend.projects.store import ProjectStore, atomic_json


class CompactExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProjectStore(self.root / "data")
        source = self.root / "source"
        source.mkdir()
        randomizer = random.Random(10)
        # A textured synthetic page approximates the PNG inflation of rendered JPEG scans.
        image = Image.frombytes("RGB", (384, 600), randomizer.randbytes(384 * 600 * 3))
        image.save(source / "10.jpg", quality=91)
        Image.new("RGB", (200, 300), "white").save(source / "2.jpg", quality=91)
        self.project = import_source(self.store, source)
        self.cid = self.project["project_id"]
        self.fresh_outputs(self.project)

    def fresh_outputs(self, project):
        for page in project["pages"]:
            directory = page_directory(self.store, project["project_id"], page["id"])
            directory.mkdir(parents=True, exist_ok=True)
            with Image.open(self.store.project_path(project["project_id"]) / page["original"]) as image:
                image.save(directory / "rendered.png")
            atomic_json(directory / "meta.json", {"signature": signature(page), "variants": ["rendered"]})

    def rendered(self):
        page = self.project["pages"][1]
        page["regions"] = [{"id": "text-1", "box": [0, 0, 100, 40], "source_text": "Hello", "target_text": "Bonjour", "status": "translated", "confidence_detection": None, "confidence_ocr": None}]
        self.project = self.store.save(self.project, self.project["revision"])
        page = self.project["pages"][1]
        self.fresh_outputs(self.project)
        return page_directory(self.store, self.cid, page["id"]) / "rendered.png"

    def test_unchanged_pages_preserve_bytes_dimensions_and_natural_order(self):
        expected = [(self.store.project_path(self.cid) / p["original"]).read_bytes() for p in self.project["pages"]]
        destination = export_project_compact(self.store, self.cid)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(archive.namelist(), ["0001.jpg", "0002.jpg"])
            for entry, source, page in zip(archive.infolist(), expected, self.project["pages"]):
                self.assertEqual(entry.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(archive.read(entry), source)
                with Image.open(io.BytesIO(archive.read(entry))) as image:
                    self.assertEqual(image.size, (page["width"], page["height"]))

    def test_rendered_webp_is_compact_and_sources_remain_immutable(self):
        rendered = self.rendered()
        originals = [(self.store.project_path(self.cid) / p["original"]).read_bytes() for p in self.project["pages"]]
        before = rendered.read_bytes()
        result = export_project_compact(self.store, self.cid)
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(archive.namelist(), ["0001.jpg", "0002.webp"])
            webp = archive.read("0002.webp")
            self.assertLess(len(webp), len(before) * .6)
            with Image.open(io.BytesIO(webp)) as image:
                self.assertEqual(image.size, (384, 600))
                self.assertEqual(image.format, "WEBP")
        self.assertEqual(rendered.read_bytes(), before)
        self.assertEqual([(self.store.project_path(self.cid) / p["original"]).read_bytes() for p in self.project["pages"]], originals)

    def test_stale_render_rejected_without_replacing_previous_export(self):
        self.rendered()
        destination = export_project_compact(self.store, self.cid)
        before = destination.read_bytes()
        self.project["pages"][1]["regions"][0]["target_text"] = "Salut"
        self.store.save(self.project, self.project["revision"])
        with self.assertRaisesRegex(FileNotFoundError, "périmé"):
            export_project_compact(self.store, self.cid)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(list(destination.parent.glob("*.tmp")), [])

    def test_explicit_codecs_and_options(self):
        for codec, extension in (("jpeg", "jpg"), ("png", "png"), ("webp", "webp")):
            result = export_project_compact(self.store, self.cid, {"format": codec, "quality": 90, "reuse_originals": False})
            with zipfile.ZipFile(result) as archive:
                self.assertEqual(archive.namelist(), [f"0001.{extension}", f"0002.{extension}"])
        for options in ({"format": "gif"}, {"quality": True}, {"quality": 101}, {"quality": 1.5}, {"reuse_originals": "yes"}, []):
            with self.assertRaises(ValueError):
                export_project_compact(self.store, self.cid, options)

    def test_preserved_translation_uses_original_but_manual_mask_requires_render(self):
        self.rendered()
        page = self.project["pages"][1]
        page["regions"][0]["preserve_original"] = True
        self.project = self.store.save(self.project, self.project["revision"])
        self.fresh_outputs(self.project)
        with zipfile.ZipFile(export_project_compact(self.store, self.cid)) as archive:
            self.assertEqual(archive.namelist(), ["0001.jpg", "0002.jpg"])
        self.project["pages"][1]["mask_strokes"] = [{"mode": "paint", "radius": 3, "points": [[5, 5]]}]
        self.store.save(self.project, self.project["revision"])
        with self.assertRaises(FileNotFoundError):
            export_project_compact(self.store, self.cid)

    def test_tall_webtoon_uses_supported_codec_without_resizing(self):
        for height, extension in ((17000, "jpg"), (66000, "png")):
            source = self.root / f"tall-{height}.png"
            Image.new("RGB", (20, height), "white").save(source)
            chapter = import_source(self.store, source)
            self.fresh_outputs(chapter)
            destination = export_project_compact(self.store, chapter["project_id"], {"reuse_originals": False})
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.namelist(), [f"0001.{extension}"])
                with Image.open(io.BytesIO(archive.read(archive.namelist()[0]))) as image:
                    self.assertEqual(image.size, (20, height))

    def test_encoding_failure_cleans_partial_and_keeps_completed_archive(self):
        self.rendered()
        destination = export_project_compact(self.store, self.cid)
        before = destination.read_bytes()
        # A changed recipe must attempt a new encode rather than reuse the cache.
        self.project["pages"][1]["regions"][0]["target_text"] = "Salut"
        self.project = self.store.save(self.project, self.project["revision"])
        self.fresh_outputs(self.project)
        with patch.object(Image.Image, "save", side_effect=OSError("encoder failure")):
            with self.assertRaisesRegex(OSError, "encoder failure"):
                export_project_compact(self.store, self.cid)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(list(destination.parent.glob("*.tmp")), [])

    def test_repeat_download_reuses_archive_without_encoding(self):
        self.rendered()
        destination = export_project_compact(self.store, self.cid)
        before = destination.read_bytes()
        with patch.object(Image.Image, "save", side_effect=AssertionError("cached export encoded again")):
            self.assertEqual(export_project_compact(self.store, self.cid), destination)
        self.assertEqual(destination.read_bytes(), before)

    def test_quality_and_text_changes_rebuild_cached_archive(self):
        self.rendered()
        export_project_compact(self.store, self.cid)
        save_image = Image.Image.save
        encoded = []
        def record(image, *args, **kwargs):
            encoded.append(kwargs.get("quality"))
            return save_image(image, *args, **kwargs)
        with patch.object(Image.Image, "save", record):
            export_project_compact(self.store, self.cid, {"quality": 81})
        self.assertEqual(encoded, [81])
        self.project["pages"][1]["regions"][0]["target_text"] = "Salut"
        self.project = self.store.save(self.project, self.project["revision"])
        self.fresh_outputs(self.project)
        encoded.clear()
        with patch.object(Image.Image, "save", record):
            export_project_compact(self.store, self.cid)
        self.assertEqual(encoded, [92])

    def test_corrupt_cached_archive_is_rebuilt(self):
        self.rendered()
        destination = export_project_compact(self.store, self.cid)
        # Keep the same length to exercise ZIP validation in addition to size checks.
        destination.write_bytes(b"x" * destination.stat().st_size)
        export_project_compact(self.store, self.cid)
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(), ["0001.jpg", "0002.webp"])

    def test_new_render_bytes_invalidate_cache_even_when_page_is_unchanged(self):
        rendered = self.rendered()
        destination = export_project_compact(self.store, self.cid)
        before = destination.read_bytes()
        Image.new("RGB", (384, 600), "white").save(rendered)
        export_project_compact(self.store, self.cid)
        self.assertNotEqual(destination.read_bytes(), before)
        with zipfile.ZipFile(destination) as archive:
            with Image.open(io.BytesIO(archive.read("0002.webp"))) as image:
                self.assertEqual(image.getpixel((100, 100)), (255, 255, 255))

    def test_snapshot_copy_fallback_preserves_render_and_cleans_temporary_files(self):
        rendered = self.rendered()
        before = rendered.read_bytes()
        with patch("backend.projects.exporting.os.link", side_effect=OSError("hard links unavailable")):
            destination = export_project_compact(self.store, self.cid)
        self.assertEqual(rendered.read_bytes(), before)
        self.assertEqual(list(self.store.data_dir.glob(".renkavi-export-*")), [])
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())

    def test_slow_encode_allows_save_and_rejects_changed_chapter(self):
        self.rendered()
        destination = export_project_compact(self.store, self.cid)
        before = destination.read_bytes()
        self.project["pages"][1]["regions"][0]["target_text"] = "Salut"
        self.project = self.store.save(self.project, self.project["revision"])
        self.fresh_outputs(self.project)
        entered, release, saved = threading.Event(), threading.Event(), threading.Event()
        save_image = Image.Image.save
        def slow(image, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test encoder timed out")
            return save_image(image, *args, **kwargs)
        def edit():
            current = self.store.load(self.cid)
            current["title"] = "Saved during export"
            self.store.save(current, current["revision"])
            saved.set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            with patch.object(Image.Image, "save", slow):
                exporting = executor.submit(export_project_compact, self.store, self.cid)
                try:
                    self.assertTrue(entered.wait(5))
                    saving = executor.submit(edit)
                    saved_during_encoding = saved.wait(2)
                finally:
                    release.set()
                saving.result(timeout=5)
                self.assertTrue(saved_during_encoding, "image encoding held the library lock")
                with self.assertRaises(RevisionConflict):
                    exporting.result(timeout=5)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(list(destination.parent.glob("*.tmp")), [])
        self.assertEqual(list(self.store.data_dir.glob(".renkavi-export-*")), [])

    def test_simultaneous_downloads_encode_once(self):
        self.rendered()
        save_image = Image.Image.save
        encoded = []
        def record(image, *args, **kwargs):
            encoded.append(1)
            return save_image(image, *args, **kwargs)
        with ThreadPoolExecutor(max_workers=2) as executor:
            with patch.object(Image.Image, "save", record):
                exports = [executor.submit(export_project_compact, self.store, self.cid) for _ in range(2)]
                paths = [future.result(timeout=10) for future in exports]
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(len(encoded), 1)

    def test_unchanged_page_requires_complete_fresh_processing(self):
        page = self.project["pages"][0]
        directory = page_directory(self.store, self.cid, page["id"])
        (directory / "meta.json").unlink()
        with self.assertRaises(FileNotFoundError):
            export_project_compact(self.store, self.cid)
        self.fresh_outputs(self.project)
        self.project["pages"][0]["regions"] = [{"id": "ignored", "box": [0, 0, 10, 10], "source_text": "Hello", "target_text": "", "status": "ignored", "confidence_detection": None, "confidence_ocr": None}]
        self.project = self.store.save(self.project, self.project["revision"])
        with self.assertRaisesRegex(FileNotFoundError, "périmé"):
            export_project_compact(self.store, self.cid)
        self.fresh_outputs(self.project)
        with zipfile.ZipFile(export_project_compact(self.store, self.cid)) as archive:
            self.assertEqual(archive.namelist(), ["0001.jpg", "0002.jpg"])

    def test_active_untranslated_region_rejected_even_with_fresh_render(self):
        self.rendered()
        self.project["pages"][1]["regions"][0]["target_text"] = ""
        self.project = self.store.save(self.project, self.project["revision"])
        self.fresh_outputs(self.project)
        with self.assertRaisesRegex(ValueError, "Traduction incomplète"):
            export_project_compact(self.store, self.cid)


if __name__ == "__main__":
    unittest.main()
