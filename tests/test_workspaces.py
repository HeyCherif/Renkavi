import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.core.project import ProjectValidationError
from backend.projects.importer import import_source
from backend.projects.store import ProjectStore
from backend.projects.workspaces import Workspaces


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProjectStore(self.root / "data")
        self.workspaces = Workspaces(self.store)

    def chapter(self, title="Chapitre 1"):
        source = self.root / "source.png"
        Image.new("RGB", (40, 60), "white").save(source)
        return import_source(self.store, source, title=title)

    def test_two_chapters_share_folder_and_reload_after_move(self):
        workspace = self.workspaces.create("Une série", "ja", "fr")
        self.assertEqual(workspace["id"], workspace["workspace_id"])
        first, second = self.chapter(), self.chapter("Chapitre 2")
        original = self.store.project_path(first["project_id"]) / first["pages"][0]["original"]
        before = original.read_bytes()
        for chapter in (first, second):
            self.workspaces.attach(workspace["workspace_id"], chapter["project_id"])
        result = self.workspaces.get(workspace["workspace_id"])
        self.assertEqual(result["chapter_count"], 2)
        self.assertEqual(result["page_count"], 2)
        reopened = ProjectStore(self.store.data_dir)
        path = reopened.project_path(first["project_id"])
        self.assertEqual(path, Path(result["folder"]) / "chapters" / first["project_id"])
        self.assertEqual((path / first["pages"][0]["original"]).read_bytes(), before)
        first["title"] = "Correction conservée"
        reopened.save(first, 1)
        self.assertEqual(reopened.load(first["project_id"])["title"], "Correction conservée")
        self.assertEqual({p["workspace_id"] for p in reopened.list_projects()}, {workspace["workspace_id"]})

    def test_legacy_migration_is_restartable_and_preserves_manifest(self):
        chapter = self.chapter()
        chapter["provenance"]["manual_correction"] = "Texte corrigé"
        chapter = self.store.save(chapter, 1)
        old = self.store.project_path(chapter["project_id"])
        manifest_bytes = (old / "project.json").read_bytes()
        # Simulate power loss after parent manifest is committed, before folder rename.
        with patch.object(self.workspaces, "attach", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.workspaces.migrate_legacy()
        self.assertTrue(old.exists())
        self.assertEqual(len(self.workspaces.list()), 1)
        result = self.workspaces.migrate_legacy()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["chapter_count"], 1)
        self.assertEqual((self.store.project_path(chapter["project_id"]) / "project.json").read_bytes(), manifest_bytes)
        self.assertEqual(self.workspaces.migrate_legacy(), result)
        self.assertFalse(old.exists())

    def test_move_between_workspaces_invalidates_cached_path(self):
        first, second = self.workspaces.create("A"), self.workspaces.create("B")
        chapter = self.chapter()
        cid = chapter["project_id"]
        self.workspaces.attach(first["workspace_id"], cid)
        old = self.store.project_path(cid)
        self.workspaces.attach(second["workspace_id"], cid)
        self.assertFalse(old.exists())
        self.assertEqual(self.store.load(cid), chapter)
        self.assertEqual(self.workspaces.get(first["workspace_id"])["chapter_count"], 0)
        self.assertEqual(self.workspaces.get(second["workspace_id"])["chapter_count"], 1)

    def test_workspace_update_validates_and_ignores_supplied_folder(self):
        workspace = self.workspaces.create("Série")
        updated = self.workspaces.update(workspace["workspace_id"], {"title": "Série revue", "folder": "outside", "target_language": "de"})
        self.assertEqual(updated["target_language"], "de")
        self.assertEqual(updated["folder"], workspace["folder"])
        for bad in ("../bad", str(uuid.uuid4()) + "/../x"):
            with self.assertRaises(ProjectValidationError):
                self.workspaces.get(bad)
        for title in (None, "", 12):
            with self.assertRaises(ProjectValidationError):
                self.workspaces.update(workspace["workspace_id"], {"title": title})

    def test_corrupt_legacy_retained_and_reported_without_blocking_valid_chapter(self):
        bad = self.chapter("Corrompu")
        bad_path = self.store.project_path(bad["project_id"]) / "project.json"
        bad_path.write_text("{invalid", encoding="utf-8")
        good = self.chapter("Valide")
        with self.assertLogs("backend.projects.workspaces", level="WARNING"):
            result = self.workspaces.migrate_legacy()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["chapters"][0]["project_id"], good["project_id"])
        self.assertEqual(result[0]["id"], result[0]["workspace_id"])
        self.assertEqual(self.workspaces.migration_errors[0]["chapter_id"], bad["project_id"])
        self.assertEqual(bad_path.read_text(encoding="utf-8"), "{invalid")

    def test_invalid_field_types_are_reported_as_corrupt_legacy(self):
        bad = self.chapter("Corrompu")
        bad_path = self.store.project_path(bad["project_id"])
        (bad_path / "project.json").write_text('{"schema_version":2,"project_id":1}', encoding="utf-8")
        with self.assertLogs("backend.projects.workspaces", level="WARNING"):
            self.assertEqual(self.workspaces.migrate_legacy(), [])
        self.assertEqual(self.workspaces.migration_errors[0]["chapter_id"], bad["project_id"])
        self.assertTrue(bad_path.exists())


if __name__ == "__main__":
    unittest.main()
