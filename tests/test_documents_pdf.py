import hashlib
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pypdfium2 as pdfium
from PIL import Image
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from backend.ai.pipeline import page_directory, project_context, signature
from backend.core.project import RevisionConflict
from backend.projects.store import ProjectStore, atomic_json


class PDFDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProjectStore(self.root / "library")

    def fixture(self, name="source.pdf", *, scanned=False, encrypted=False):
        path = self.root / name
        options = {"encrypt": StandardEncryption("secret-password")} if encrypted else {}
        writer = canvas.Canvas(str(path), pagesize=(200, 300), **options)
        if scanned:
            image = Image.new("RGB", (80, 80), "#d9d9d9")
            writer.drawImage(ImageReader(image), 20, 150, width=80, height=80)
        else:
            writer.setFont("Helvetica", 12)
            writer.drawString(20, 260, "Hello native PDF.")
            writer.drawString(20, 240, "https://example.org")
        writer.showPage()
        writer.setPageSize((400, 200))
        writer.setFont("Helvetica", 12)
        writer.drawString(20, 160, "Second page.")
        writer.showPage()
        writer.save()
        return path

    def imported(self, **kwargs):
        from backend.projects.documents_pdf import import_pdf
        return import_pdf(self.store, self.fixture(**kwargs), source_language="en", target_language="fr")

    def fresh(self, project):
        for page in project["pages"]:
            directory = page_directory(self.store, project["project_id"], page["id"])
            directory.mkdir(parents=True, exist_ok=True)
            with Image.open(self.store.project_path(project["project_id"]) / page["original"]) as image:
                image.save(directory / "rendered.png")
            atomic_json(directory / "meta.json", {"signature": signature(page), "variants": ["rendered"],
                                                 "project_context": project_context(project)})

    def translated(self):
        project = self.imported()
        for page in project["pages"]:
            for region in page["regions"]:
                if "https://" in region["source_text"]:
                    region["status"] = "ignored"
                else:
                    region.update(target_text="Bonjour document.", status="translated")
        project = self.store.save(project, project["revision"])
        self.fresh(project)
        return project

    def test_native_text_and_source_are_preserved_without_watermark_filter(self):
        source = self.fixture()
        before = source.read_bytes()
        from backend.projects.documents_pdf import import_pdf
        project = import_pdf(self.store, source)
        self.assertEqual(project["provenance"]["source_format"], "pdf")
        self.assertEqual(project["provenance"]["source_sha256"], hashlib.sha256(before).hexdigest())
        stored_source = self.store.project_path(project["project_id"]) / project["provenance"]["source_file"]
        self.assertEqual(stored_source.read_bytes(), before)
        self.assertEqual(source.read_bytes(), before)
        first = project["pages"][0]
        text = " ".join(region["source_text"] for region in first["regions"])
        self.assertEqual(text.count("Hello native PDF."), 1)
        self.assertIn("https://example.org", text)
        self.assertTrue(first["pdf_text_native"])
        self.assertTrue(all(region["status"] != "ignored" for region in first["regions"]))
        self.assertTrue(all(region["auto_style"]["font"] == "arial" for region in first["regions"]))
        self.assertEqual(first["pdf_size_points"], [200., 300.])

    def test_scanned_page_remains_available_for_ocr(self):
        project = self.imported(scanned=True)
        self.assertFalse(project["pages"][0]["pdf_text_native"])
        self.assertEqual(project["pages"][0]["regions"], [])

    def test_page_limit_and_render_failure_leave_no_locked_staging_files(self):
        from backend.projects import documents_pdf
        source = self.fixture()
        with patch.object(documents_pdf, "MAX_PAGES", 1):
            with self.assertRaisesRegex(ValueError, "pages"):
                documents_pdf.import_pdf(self.store, source)
        with patch.object(documents_pdf, "_native_regions", side_effect=ValueError("bad text extraction")):
            with self.assertRaisesRegex(ValueError, "bad text extraction"):
                documents_pdf.import_pdf(self.store, source)
        self.assertEqual(list(self.store.projects_dir.iterdir()), [])

    def test_mixed_font_inline_text_is_extracted_once(self):
        from backend.projects.documents_pdf import import_pdf
        source = self.root / "inline.pdf"
        writer = canvas.Canvas(str(source), pagesize=(300, 100))
        writer.setFont("Helvetica", 12)
        writer.drawString(20, 60, "Hello ")
        writer.setFont("Helvetica-Bold", 16)
        writer.drawString(50, 60, "world")
        writer.showPage()
        writer.save()
        project = import_pdf(self.store, source)
        text = " ".join(region["source_text"] for region in project["pages"][0]["regions"])
        self.assertEqual(text.count("Hello"), 1)
        self.assertEqual(text.count("world"), 1)

    def test_large_page_is_downsampled_within_pixel_budget(self):
        from backend.projects import documents_pdf
        source = self.fixture()
        with patch.object(documents_pdf, "MAX_PIXELS", 10000):
            project = documents_pdf.import_pdf(self.store, source)
        self.assertTrue(all(page["width"]*page["height"] <= 10000 for page in project["pages"]))
        self.assertEqual(project["pages"][0]["pdf_size_points"], [200., 300.])

    def test_rotation_crop_and_blank_pages_have_valid_image_coordinates(self):
        source = self.fixture()
        changed = self.root / "rotated.pdf"
        with pdfium.PdfDocument(str(source)) as document:
            with closing(document[0]) as page:
                page.set_rotation(90)
                page.set_cropbox(10, 20, 190, 290)
            document.new_page(50, 60).close()
            document.save(str(changed))
        from backend.projects.documents_pdf import import_pdf
        project = import_pdf(self.store, changed)
        self.assertEqual(len(project["pages"]), 3)
        page = project["pages"][0]
        self.assertEqual(page["pdf_size_points"], [270., 180.])
        self.assertEqual(project["pages"][-1]["regions"], [])
        self.assertIn("Hello native PDF.", " ".join(r["source_text"] for r in page["regions"]))
        for region in page["regions"]:
            x1, y1, x2, y2 = region["box"]
            self.assertTrue(0 <= x1 < x2 <= page["width"])
            self.assertTrue(0 <= y1 < y2 <= page["height"])

    def test_encrypted_and_malformed_files_fail_without_partial_projects(self):
        from backend.projects.documents_pdf import import_pdf
        encrypted = self.fixture(encrypted=True)
        with self.assertRaisesRegex(ValueError, "chiffr|mot de passe"):
            import_pdf(self.store, encrypted)
        broken = self.root / "broken.pdf"
        broken.write_bytes(b"%PDF-1.7\nmalformed")
        with self.assertRaises(ValueError):
            import_pdf(self.store, broken)
        self.assertEqual(list(self.store.chapter_paths()), [])
        self.assertEqual(list(self.store.projects_dir.glob(".*.tmp")), [])

    def test_export_preserves_page_sizes_searchable_translation_and_ignored_source(self):
        from backend.projects.documents_pdf import export_pdf
        project = self.translated()
        source = self.store.project_path(project["project_id"]) / project["provenance"]["source_file"]
        before = source.read_bytes()
        result = export_pdf(self.store, project["project_id"])
        with pdfium.PdfDocument(str(result)) as document:
            self.assertEqual(len(document), 2)
            for index, size in enumerate(((200., 300.), (400., 200.))):
                with closing(document[index]) as page:
                    self.assertEqual(page.get_size(), size)
                    with closing(page.get_textpage()) as textpage:
                        text = textpage.get_text_range()
                    self.assertIn("Bonjour document.", text)
            with closing(document[0]) as page:
                with closing(page.get_textpage()) as textpage:
                    self.assertIn("https://example.org", textpage.get_text_range())
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(result.suffix, ".pdf")

    def test_export_refuses_incomplete_or_stale_results_and_keeps_previous_pdf(self):
        from backend.projects.documents_pdf import export_pdf
        project = self.translated()
        result = export_pdf(self.store, project["project_id"])
        before = result.read_bytes()
        project["pages"][0]["regions"][0]["target_text"] = ""
        project = self.store.save(project, project["revision"])
        with self.assertRaises((FileNotFoundError, ValueError)):
            export_pdf(self.store, project["project_id"])
        self.fresh(project)
        with self.assertRaisesRegex(ValueError, "incomplète"):
            export_pdf(self.store, project["project_id"])
        self.assertEqual(result.read_bytes(), before)

    def test_repeat_export_reuses_cache_and_changed_quality_reencodes(self):
        from backend.projects.documents_pdf import export_pdf
        project = self.translated()
        result = export_pdf(self.store, project["project_id"])
        with patch("backend.projects.documents_pdf._write_pdf", side_effect=AssertionError("cached PDF regenerated")):
            self.assertEqual(export_pdf(self.store, project["project_id"]), result)
            with self.assertRaisesRegex(AssertionError, "regenerated"):
                export_pdf(self.store, project["project_id"], {"quality": 73})

    def test_edit_during_pdf_encoding_is_saved_and_prevents_stale_publication(self):
        from backend.projects import documents_pdf
        project = self.translated()
        chapter_id = project["project_id"]
        result = documents_pdf.export_pdf(self.store, chapter_id)
        before = result.read_bytes()
        entered, release, saved = threading.Event(), threading.Event(), threading.Event()
        write = documents_pdf._write_pdf
        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("encoder timed out")
            return write(*args, **kwargs)
        def edit():
            current = self.store.load(chapter_id)
            current["title"] = "Saved while exporting"
            self.store.save(current, current["revision"])
            saved.set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            with patch.object(documents_pdf, "_write_pdf", slow):
                exporting = executor.submit(documents_pdf.export_pdf, self.store, chapter_id, {"quality": 73})
                try:
                    self.assertTrue(entered.wait(5))
                    saving = executor.submit(edit)
                    saved_while_encoding = saved.wait(2)
                finally:
                    release.set()
                saving.result(timeout=5)
                self.assertTrue(saved_while_encoding)
                with self.assertRaises(RevisionConflict):
                    exporting.result(timeout=5)
        self.assertEqual(result.read_bytes(), before)
        self.assertEqual(list(self.store.data_dir.glob(".renkavi-pdf-*")), [])

    def test_text_layer_supports_cyrillic_and_cjk_when_local_fonts_available(self):
        from backend.projects.documents_pdf import export_pdf
        project = self.translated()
        project["pages"][0]["regions"][0].update(target_text="Привет мир. 你好世界。", status="translated")
        project = self.store.save(project, project["revision"])
        self.fresh(project)
        result = export_pdf(self.store, project["project_id"])
        with pdfium.PdfDocument(str(result)) as document:
            with closing(document[0]) as page:
                with closing(page.get_textpage()) as textpage:
                    text = textpage.get_text_range()
        self.assertIn("Привет мир.", text)
        self.assertIn("你好世界。", text)

    def test_generic_image_pages_export_to_pdf_at_default_dpi(self):
        from backend.projects.documents_pdf import export_pdf
        from backend.projects.importer import import_source
        source = self.root / "image.png"
        Image.new("RGB", (300, 450), "white").save(source)
        project = import_source(self.store, source)
        self.fresh(project)
        result = export_pdf(self.store, project["project_id"])
        with pdfium.PdfDocument(str(result)) as document:
            with closing(document[0]) as page:
                self.assertEqual(page.get_size(), (144., 216.))


if __name__ == "__main__":
    unittest.main()
