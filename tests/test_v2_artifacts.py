import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from backend.core.project import ProjectValidationError, RevisionConflict
from backend.projects.artifacts import generate_variants, variant_file, variant_status
from backend.projects.importer import import_source
from backend.projects.store import ProjectStore


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProjectStore(Path(self.temp.name) / "data")
        source = Path(self.temp.name) / "page.png"
        image = Image.new("RGB", (50, 40), "white")
        ImageDraw.Draw(image).rectangle((20, 18, 30, 22), fill="black")
        image.save(source)
        self.project = import_source(self.store, source, source_language="ja", target_language="fr")
        self.page = self.project["pages"][0]
        self.page["mask_strokes"] = [{"mode": "paint", "radius": 4, "points": [[20, 20], [30, 20]]}]

    def test_mask_and_cleaned_are_real_files_without_touching_original(self):
        original = self.store.project_path(self.project["project_id"]) / self.page["original"]
        before = original.read_bytes()
        self.project = self.store.save(self.project, 1)
        self.assertFalse(variant_status(self.store, self.project["project_id"], self.page["id"])["available"])
        result = generate_variants(self.store, self.project["project_id"], self.page["id"], 2)
        self.assertEqual(result["method"], "flat-border-v1")
        with Image.open(variant_file(self.store, self.project["project_id"], self.page["id"], "mask")) as mask:
            self.assertEqual(mask.getpixel((25, 20)), 255)
            self.assertEqual(mask.getpixel((0, 0)), 0)
        with Image.open(variant_file(self.store, self.project["project_id"], self.page["id"], "cleaned")) as cleaned:
            self.assertEqual(cleaned.size, (50, 40))
            self.assertEqual(cleaned.getpixel((25, 20)), (255, 255, 255))
        self.assertEqual(original.read_bytes(), before)

    def test_erase_and_stale_variant(self):
        self.page["mask_strokes"].append({"mode": "erase", "radius": 2, "points": [[25, 20]]})
        saved = self.store.save(self.project, 1)
        generate_variants(self.store, saved["project_id"], self.page["id"], 2)
        with Image.open(variant_file(self.store, saved["project_id"], self.page["id"], "mask")) as mask:
            self.assertEqual(mask.getpixel((25, 20)), 0)
            self.assertEqual(mask.getpixel((20, 20)), 255)
        saved["pages"][0]["mask_strokes"].append({"mode": "paint", "radius": 2, "points": [[5, 5]]})
        self.store.save(saved, 2)
        self.assertFalse(variant_status(self.store, saved["project_id"], self.page["id"])["available"])
        with self.assertRaises(FileNotFoundError):
            variant_file(self.store, saved["project_id"], self.page["id"], "cleaned")

    def test_revision_and_validation(self):
        saved = self.store.save(self.project, 1)
        with self.assertRaises(RevisionConflict):
            generate_variants(self.store, saved["project_id"], self.page["id"], 1)
        saved["pages"][0]["mask_strokes"][0]["points"] = [[100, 20]]
        with self.assertRaises(ProjectValidationError):
            self.store.save(saved, 2)


if __name__ == "__main__":
    unittest.main()
