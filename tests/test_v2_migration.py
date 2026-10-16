import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from backend.projects.migrate_v1 import migrate_v1
from backend.projects.store import ProjectStore


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = self.root / "legacy"
        (self.legacy / "raw").mkdir(parents=True)
        Image.new("RGB", (100, 120), "white").save(self.legacy / "raw" / "001.png")
        meta = {
            "project_id": "legacy-1", "source_language": "ja", "target_language": "fr",
            "page_count": 1, "pages": ["raw/001.png"], "source_cbz": "old.cbz",
        }
        translation = {
            "schema": "manhwa-localizer-1", "project_id": "legacy-1",
            "source_language": "ja", "target_language": "fr",
            "items": [
                {"id": "p0001_r0001", "page_index": 0, "box": [5, 6, 50, 60],
                 "polygon": [[5, 6], [50, 6], [50, 60], [5, 60]],
                 "source_text": "原文", "target_text": "Traduction",
                 "confidence": 0.91, "style": {"fill": "black"}, "kind": "text"}
            ],
        }
        (self.legacy / "project.json").write_text(json.dumps(meta), encoding="utf-8")
        (self.legacy / "translation.json").write_text(json.dumps(translation), encoding="utf-8")
        self.store = ProjectStore(self.root / "data")

    def test_migrates_copy_and_preserves_translation(self):
        before = {p.name: p.read_bytes() for p in self.legacy.glob("*.json")}
        project = migrate_v1(self.store, self.legacy)
        region = project["pages"][0]["regions"][0]
        self.assertEqual(region["source_text"], "原文")
        self.assertEqual(region["target_text"], "Traduction")
        self.assertEqual(region["box"], [5, 6, 50, 60])
        self.assertEqual(project["provenance"]["legacy_project_id"], "legacy-1")
        self.assertEqual({p.name: p.read_bytes() for p in self.legacy.glob("*.json")}, before)
        self.assertEqual(self.store.load(project["project_id"])["pages"][0]["regions"][0], region)

    def test_rejects_duplicate_ids_or_foreign_page(self):
        path = self.legacy / "translation.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["items"].append(dict(data["items"][0]))
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            migrate_v1(self.store, self.legacy)
        data["items"] = [dict(data["items"][0], page_index=5)]
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            migrate_v1(self.store, self.legacy)
        self.assertEqual(self.store.list_projects(), [])


if __name__ == "__main__":
    unittest.main()
