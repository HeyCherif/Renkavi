import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from lxml import etree
from PIL import Image

from backend.ai.pipeline import page_directory, project_context, signature
from backend.projects.documents_word import WORD_NS, clean_preview, export_docx, import_docx
from backend.projects.store import ProjectStore, atomic_json

NS = {"w": WORD_NS}
CONTENT_TYPES = b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>'


def document(body):
    return (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{WORD_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>{body}</w:body></w:document>').encode()


def paragraph(text):
    return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


class WordDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProjectStore(self.root / "library")

    def fixture(self, body=None, extra=None, filename="sample.docx"):
        source = self.root / filename
        if body is None:
            body = '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Hello </w:t></w:r><w:hyperlink r:id="rIdHyper"><w:r><w:rPr><w:i/></w:rPr><w:t>world</w:t></w:r></w:hyperlink><w:r><w:tab/><w:t>After tab</w:t><w:br/><w:t>Last line</w:t></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p><w:tbl><w:tr><w:tc>' + paragraph('Cell text') + '</w:tc></w:tr></w:tbl>'
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.comment = b"original comment"
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("word/document.xml", document(body))
            archive.writestr("word/styles.xml", b'<styles untouched="yes"/>')
            archive.writestr("word/media/image1.png", b"image bytes unchanged")
            archive.writestr("word/_rels/document.xml.rels", b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdHyper" Type="hyperlink" Target="https://example.org" TargetMode="External"/></Relationships>')
            for name, content in (extra or {}).items():
                archive.writestr(name, content)
        return source

    def import_fixture(self, **kwargs):
        return import_docx(self.store, self.fixture(**kwargs), source_language="en", target_language="fr")

    def regions(self, project):
        return [region for page in project["pages"] for region in page["regions"]]

    def fresh(self, project):
        for page in project["pages"]:
            directory = page_directory(self.store, project["project_id"], page["id"])
            directory.mkdir(parents=True, exist_ok=True)
            source = self.store.project_path(project["project_id"]) / page["original"]
            with Image.open(source) as image:
                image.save(directory / "rendered.png")
            atomic_json(directory / "meta.json", {"signature": signature(page), "project_context": project_context(project), "variants": ["rendered"]})

    def translate(self, project):
        for region in self.regions(project):
            if region["status"] != "ignored":
                region.update(target_text="FR " + region["source_text"].strip(), status="translated")
        project = self.store.save(project, project["revision"])
        self.fresh(project)
        return project

    def test_import_includes_tables_headers_footers_notes_and_preserves_source(self):
        extra = {}
        for part, tag, text in (("header1", "hdr", "Header"), ("footer1", "ftr", "Footer"), ("footnotes", "footnotes", "Note"), ("endnotes", "endnotes", "End note")):
            extra[f"word/{part}.xml"] = f'<w:{tag} xmlns:w="{WORD_NS}">{paragraph(text)}</w:{tag}>'.encode()
        source = self.fixture(extra=extra)
        before = source.read_bytes()
        project = import_docx(self.store, source)
        provenance = project["provenance"]
        self.assertEqual(provenance["document_mode"], "structured")
        self.assertEqual(provenance["document_preview"], "approximate")
        self.assertEqual(provenance["source_sha256"], hashlib.sha256(before).hexdigest())
        copied = self.store.project_path(project["project_id"]) / provenance["source_file"]
        self.assertEqual(copied.read_bytes(), before)
        texts = [region["source_text"] for region in self.regions(project)]
        self.assertEqual(set(texts), {"Hello world", "After tab", "Last line", "Cell text", "Header", "Footer", "Note", "End note"})
        self.assertNotIn("1", texts)
        for page in project["pages"]:
            with Image.open(self.store.project_path(project["project_id"]) / page["original"]) as preview:
                self.assertEqual(preview.size, (1200, 1600))
        self.assertEqual(source.read_bytes(), before)

    def test_native_roundtrip_preserves_styles_hyperlinks_tabs_breaks_fields_images(self):
        project = self.import_fixture()
        copied = self.store.project_path(project["project_id"]) / project["provenance"]["source_file"]
        before = copied.read_bytes()
        project = self.translate(project)
        for region in self.regions(project):
            if region["source_text"] == "Hello world":
                region["target_text"] = "Bonjour tout le monde"
        project = self.store.save(project, project["revision"])
        self.fresh(project)
        exported = export_docx(self.store, project["project_id"])
        self.assertEqual(exported.suffix, ".docx")
        with zipfile.ZipFile(copied) as original, zipfile.ZipFile(exported) as translated:
            self.assertEqual(translated.namelist(), original.namelist())
            self.assertEqual(translated.comment, original.comment)
            for name in original.namelist():
                if name != "word/document.xml":
                    self.assertEqual(translated.read(name), original.read(name))
            root = etree.fromstring(translated.read("word/document.xml"))
            first = root.find("w:body/w:p", NS)
            self.assertEqual(len(first.findall(".//w:tab", NS)), 1)
            self.assertEqual(len(first.findall(".//w:br", NS)), 1)
            self.assertEqual(len(first.findall(".//w:b", NS)), 1)
            self.assertEqual(len(first.findall(".//w:i", NS)), 1)
            self.assertEqual(first.find("w:pPr/w:numPr/w:numId", NS).get(f"{{{WORD_NS}}}val"), "7")
            self.assertEqual(first.find("w:hyperlink", NS).get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"), "rIdHyper")
            self.assertEqual(first.find("w:hyperlink/w:r/w:t", NS).text, "le monde")
            self.assertEqual(first.find("w:r/w:t", NS).text, "Bonjour tout ")
            self.assertIn("1", first.xpath(".//w:t/text()", namespaces=NS))
            self.assertEqual(first.find(".//w:instrText", NS).text, " PAGE ")
            self.assertIn("FR Cell text", root.xpath(".//w:tbl//w:t/text()", namespaces=NS))
        self.assertEqual(copied.read_bytes(), before)

    def test_export_replaces_header_footer_and_notes_text(self):
        extras = {f"word/{part}.xml": f'<w:hdr xmlns:w="{WORD_NS}">{paragraph(text)}</w:hdr>'.encode()
                  for part, text in (("header1", "Header"), ("footer1", "Footer"), ("footnotes", "Note"), ("endnotes", "End"))}
        project = self.translate(self.import_fixture(extra=extras))
        with zipfile.ZipFile(export_docx(self.store, project["project_id"])) as archive:
            for name in extras:
                root = etree.fromstring(archive.read(name))
                self.assertTrue("".join(root.xpath(".//w:t/text()", namespaces=NS)).startswith("FR "))

    def test_long_paragraph_splits_across_pages_without_losing_characters(self):
        text = ("A paragraph with many words and spacing. " * 200) + "Fin."
        project = self.import_fixture(body=paragraph(text))
        regions = self.regions(project)
        self.assertGreater(len(project["pages"]), 1)
        self.assertEqual("".join(region["source_text"] for region in regions), text)
        self.assertEqual([region["document_ref"]["chunk_index"] for region in regions], list(range(len(regions))))
        for region in regions:
            region.update(target_text=region["source_text"], status="translated")
        project = self.store.save(project, project["revision"])
        self.fresh(project)
        with zipfile.ZipFile(export_docx(self.store, project["project_id"])) as archive:
            root = etree.fromstring(archive.read("word/document.xml"))
            self.assertEqual("".join(root.xpath(".//w:t/text()", namespaces=NS)), text)

    def test_nested_text_box_not_double_counted_and_simple_field_preserved(self):
        body = '<w:p><w:r><w:t>Outer </w:t><w:drawing><w:txbxContent>' + paragraph("Text box") + '</w:txbxContent></w:drawing><w:t>tail</w:t></w:r><w:fldSimple w:instr="DATE"><w:r><w:t>2026</w:t></w:r></w:fldSimple></w:p>'
        project = self.import_fixture(body=body)
        self.assertEqual([region["source_text"] for region in self.regions(project)], ["Outer tail", "Text box"])
        project = self.translate(project)
        with zipfile.ZipFile(export_docx(self.store, project["project_id"])) as archive:
            root = etree.fromstring(archive.read("word/document.xml"))
            self.assertEqual(root.find(".//w:fldSimple/w:r/w:t", NS).text, "2026")

    def test_missing_stale_untranslated_and_source_mutation_are_rejected(self):
        project = self.import_fixture()
        with self.assertRaises(FileNotFoundError):
            export_docx(self.store, project["project_id"])
        self.fresh(project)
        with self.assertRaisesRegex(ValueError, "incomplète"):
            export_docx(self.store, project["project_id"])
        project = self.translate(project)
        self.regions(project)[0]["target_text"] = "Correction"
        project = self.store.save(project, project["revision"])
        with self.assertRaisesRegex(FileNotFoundError, "périmé"):
            export_docx(self.store, project["project_id"])
        self.fresh(project)
        source = self.store.project_path(project["project_id"]) / project["provenance"]["source_file"]
        source.write_bytes(source.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "original a été modifié"):
            export_docx(self.store, project["project_id"])

    def test_ignored_and_preserved_chunks_keep_original_text_and_xml(self):
        project = self.import_fixture()
        for index, region in enumerate(self.regions(project)):
            if index % 2:
                region["preserve_original"] = True
            else:
                region["status"] = "ignored"
        project = self.store.save(project, project["revision"])
        self.fresh(project)
        source = self.store.project_path(project["project_id"]) / project["provenance"]["source_file"]
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(export_docx(self.store, project["project_id"])) as exported:
            self.assertEqual(exported.read("word/document.xml"), original.read("word/document.xml"))

    def test_atomic_failure_keeps_previous_export_and_cleans_partial(self):
        project = self.translate(self.import_fixture())
        destination = export_docx(self.store, project["project_id"])
        before = destination.read_bytes()
        with patch("backend.projects.documents_word.zipfile.ZipFile.writestr", side_effect=OSError("disk failed")):
            with self.assertRaisesRegex(OSError, "disk failed"):
                export_docx(self.store, project["project_id"])
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(list(destination.parent.glob("*.tmp")), [])

    def test_bad_zip_traversal_entities_image_only_and_limits_leave_no_chapter(self):
        bad = self.root / "broken.docx"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(ValueError):
            import_docx(self.store, bad)
        for extras, body in (({"../escape.xml": b"bad"}, paragraph("text")),
                             ({"word/header1.xml": b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///C:/secret">]><x>&secret;</x>'}, paragraph("text")),
                             ({}, "<w:p><w:r><w:drawing/></w:r></w:p>"),
                             ({"word/header1.xml": b"<malformed"}, paragraph("text"))):
            with self.subTest(extras=extras), self.assertRaises(ValueError):
                import_docx(self.store, self.fixture(body=body, extra=extras))
        with patch("backend.projects.documents_word.MAX_PAGES", 1):
            with self.assertRaisesRegex(ValueError, "1000 pages"):
                self.import_fixture(body=paragraph("Many words in a long paragraph. " * 200))
        self.assertEqual(list(self.store.chapter_paths()), [])
        self.assertEqual(list(self.store.projects_dir.glob(".*.tmp")), [])

    def test_duplicate_or_missing_document_mapping_is_rejected(self):
        project = self.translate(self.import_fixture())
        regions = self.regions(project)
        regions[1]["document_ref"] = dict(regions[0]["document_ref"])
        project = self.store.save(project, project["revision"])
        self.fresh(project)
        with self.assertRaisesRegex(ValueError, "dupliquée"):
            export_docx(self.store, project["project_id"])

    def test_preview_cleaning_preserves_ignored_boxes_and_page_outside_masks(self):
        project = self.import_fixture()
        page = project["pages"][0]
        page["regions"][0]["status"] = "ignored"
        with Image.open(self.store.project_path(project["project_id"]) / page["original"]) as image:
            original = image.convert("RGB")
        cleaned, mask, counts = clean_preview(original, page)
        first = tuple(page["regions"][0]["box"])
        self.assertEqual(cleaned.crop(first).tobytes(), original.crop(first).tobytes())
        self.assertEqual(cleaned.crop((0, 0, 1200, 90)).tobytes(), original.crop((0, 0, 1200, 90)).tobytes())
        self.assertEqual(mask.getpixel((60, 110)), 0)
        self.assertEqual(counts["flat"], len(page["regions"])-1)


    def test_longer_french_in_table_keeps_original_grid_and_other_table(self):
        width = ('<w:tblPr><w:tblLayout w:type="autofit"/></w:tblPr>'
                 '<w:tblGrid><w:gridCol w:w="1800"/><w:gridCol w:w="4000"/></w:tblGrid>')
        def cell(value, width):
            return (f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
                    f'{paragraph(value)}</w:tc>')
        body = ('<w:tbl>'+width+'<w:tr>'+cell('The secret is safe',1800)
                +cell('A small room',4000)+'</w:tr></w:tbl>'
                +'<w:tbl>'+width+'<w:tr>'+cell('Untouched text',1800)
                +cell('More untouched text',4000)+'</w:tr></w:tbl>')
        project = self.import_fixture(body=body)
        rows = self.regions(project)
        self.assertTrue(all(row['document_ref']['in_table'] for row in rows))
        for row in rows:
            row['target_text'] = ('Le secret est parfaitement bien gardé dans une autre pièce.'
                                  if row['source_text'] == 'The secret is safe' else row['source_text'])
            row['status'] = 'translated'
            if row['source_text'] != 'The secret is safe':
                row['preserve_original'] = True
        project=self.store.save(project,project['revision'])
        self.fresh(project)
        original=self.store.project_path(project['project_id'])/project['provenance']['source_file']
        output=export_docx(self.store,project['project_id'])
        with zipfile.ZipFile(original) as before, zipfile.ZipFile(output) as after:
            a=etree.fromstring(before.read('word/document.xml'))
            b=etree.fromstring(after.read('word/document.xml'))
            orig_tables=a.xpath('.//w:tbl',namespaces=NS)
            new_tables=b.xpath('.//w:tbl',namespaces=NS)
            self.assertEqual(len(new_tables),2)
            for old_table,new_table in zip(orig_tables,new_tables):
                self.assertEqual(
                    etree.tostring(old_table.find('w:tblGrid',NS)),
                    etree.tostring(new_table.find('w:tblGrid',NS)))
                self.assertEqual(
                    [c.get(f'{{{WORD_NS}}}w') for c in old_table.xpath('.//w:tcW',namespaces=NS)],
                    [c.get(f'{{{WORD_NS}}}w') for c in new_table.xpath('.//w:tcW',namespaces=NS)])
            self.assertEqual(new_tables[0].find('w:tblPr/w:tblLayout',NS).get(f'{{{WORD_NS}}}type'),'fixed')
            self.assertEqual(new_tables[1].find('w:tblPr/w:tblLayout',NS).get(f'{{{WORD_NS}}}type'),'autofit')
            self.assertEqual(before.read('word/styles.xml'),after.read('word/styles.xml'))
            self.assertIn('Le secret est parfaitement',after.read('word/document.xml').decode())


if __name__ == "__main__":
    unittest.main()
