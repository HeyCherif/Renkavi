import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from backend.projects.importer import import_source
from backend.projects.store import ProjectStore


def make_image(path: Path, size=(48, 72)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProjectStore(self.root / "data")

    def test_imports_single_image_with_immutable_copy(self):
        source = self.root / "source.png"
        make_image(source)
        project = import_source(self.store, source, title="Page unique", source_language="ja", target_language="fr")
        self.assertEqual([p["name"] for p in project["pages"]], ["source.png"])
        self.assertEqual(project["pages"][0]["width"], 48)
        original = self.store.project_path(project["project_id"]) / project["pages"][0]["original"]
        self.assertTrue(original.is_file())
        self.assertEqual(original.read_bytes(), source.read_bytes())
        self.assertEqual(self.store.load(project["project_id"])["revision"], 1)

    def test_imports_folder_in_natural_path_order(self):
        folder = self.root / "chapter"
        for name in ("10.png", "2.png", "1.png"):
            make_image(folder / name)
        project = import_source(self.store, folder, source_language="ko", target_language="fr")
        self.assertEqual([p["name"] for p in project["pages"]], ["1.png", "2.png", "10.png"])

    def test_imports_cbz_and_rejects_traversal_without_partial_project(self):
        source = self.root / "source.png"
        make_image(source)
        archive = self.root / "good.cbz"
        with zipfile.ZipFile(archive, "w") as z:
            z.write(source, "10.png")
            z.write(source, "2.png")
        project = import_source(self.store, archive, source_language="ja", target_language="fr")
        self.assertEqual([p["name"] for p in project["pages"]], ["2.png", "10.png"])
        count = len(self.store.list_projects())
        bad = self.root / "bad.cbz"
        with zipfile.ZipFile(bad, "w") as z:
            z.write(source, "../escape.png")
        with self.assertRaises(ValueError):
            import_source(self.store, bad, source_language="ja", target_language="fr")
        self.assertEqual(len(self.store.list_projects()), count)
        self.assertFalse((self.root / "escape.png").exists())

    def test_corrupt_image_leaves_no_project(self):
        path = self.root / "broken.png"
        path.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            import_source(self.store, path, source_language="ja", target_language="fr")
        self.assertEqual(self.store.list_projects(), [])


if __name__ == "__main__":
    unittest.main()
