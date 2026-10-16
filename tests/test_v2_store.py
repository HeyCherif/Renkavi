import tempfile
import unittest
from pathlib import Path

from backend.core.project import ProjectValidationError, RevisionConflict
from backend.projects.store import ProjectStore
from backend.projects.importer import import_source
from PIL import Image


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProjectStore(Path(self.temp.name))

    def test_create_save_reload_and_list(self):
        project = self.store.create("Chapitre 01", "ja", "fr")
        self.assertEqual(project["schema_version"], 2)
        self.assertEqual(project["revision"], 1)
        project["title"] = "Chapitre 01 revu"
        saved = self.store.save(project, expected_revision=1)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(self.store.load(project["project_id"])["pages"], [])
        self.assertEqual(self.store.list_projects()[0]["title"], "Chapitre 01 revu")

    def test_original_page_metadata_is_immutable(self):
        source = Path(self.temp.name) / "page.png"
        Image.new("RGB", (100, 200), "white").save(source)
        project = import_source(self.store, source, source_language="ja", target_language="fr")
        project["pages"][0]["width"] = 90
        with self.assertRaises(ProjectValidationError):
            self.store.save(project, expected_revision=1)

    def test_stale_revision_does_not_overwrite(self):
        project = self.store.create("Chapitre", "ko", "fr")
        first = self.store.load(project["project_id"])
        second = self.store.load(project["project_id"])
        first["title"] = "Correction"
        self.store.save(first, expected_revision=1)
        second["title"] = "Écrasement"
        with self.assertRaises(RevisionConflict):
            self.store.save(second, expected_revision=1)
        self.assertEqual(self.store.load(project["project_id"])["title"], "Correction")

    def test_rejects_traversal_and_duplicate_region_ids(self):
        project = self.store.create("Chapitre", "ja", "fr")
        page = {
            "id": "page-1", "order": 0, "name": "001.png",
            "original": "../outside.png", "width": 100, "height": 200,
            "sha256": "a" * 64, "regions": [],
        }
        project["pages"] = [page]
        with self.assertRaises(ProjectValidationError):
            self.store.save(project, expected_revision=1)
        page["original"] = "originals/0001.png"
        region = {"id": "r1", "box": [0, 0, 20, 20], "source_text": "a",
                  "target_text": "", "status": "detected", "confidence_detection": None,
                  "confidence_ocr": None}
        page["regions"] = [region, dict(region)]
        with self.assertRaises(ProjectValidationError):
            self.store.save(project, expected_revision=1)

    def test_rejects_unknown_version_on_load(self):
        project = self.store.create("Chapitre", "ja", "fr")
        path = self.store.project_path(project["project_id"]) / "project.json"
        text = path.read_text(encoding="utf-8").replace('"schema_version": 2', '"schema_version": 99')
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(ProjectValidationError):
            self.store.load(project["project_id"])


if __name__ == "__main__":
    unittest.main()
