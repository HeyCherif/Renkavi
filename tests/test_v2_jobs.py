import tempfile
import unittest
from pathlib import Path

from backend.jobs.store import JobStore
from backend.projects.store import ProjectStore


class JobStoreTests(unittest.TestCase):
    def test_checkpoint_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            project = ProjectStore(Path(root)).create("Chapitre", "ja", "fr")
            jobs = JobStore(Path(root) / "projects" / project["project_id"])
            job = jobs.enqueue("ocr", page_count=4)
            jobs.update(job["id"], state="running", page_index=2)
            after_restart = JobStore(jobs.project_dir)
            after_restart.recover_interrupted()
            recovered = after_restart.list()[0]
            self.assertEqual(recovered["state"], "interrupted")
            self.assertEqual(recovered["page_index"], 2)
            self.assertEqual(recovered["page_count"], 4)


if __name__ == "__main__":
    unittest.main()
