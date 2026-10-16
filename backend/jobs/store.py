from __future__ import annotations

import json
import uuid
from pathlib import Path

from backend.core.project import utc_now
from backend.projects.store import atomic_json


STATES = {"queued", "running", "completed", "failed", "interrupted", "cancelled"}


class JobStore:
    def __init__(self, project_dir: Path | str):
        self.project_dir = Path(project_dir)
        if not (self.project_dir / "project.json").is_file():
            raise FileNotFoundError("Projet requis pour les tâches.")
        self.path = self.project_dir / "jobs.json"
        self.recover_interrupted()

    def list(self) -> list[dict]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Fichier de tâches invalide.")
        return data

    def enqueue(self, stage: str, page_count: int) -> dict:
        if not isinstance(stage, str) or not stage or not isinstance(page_count, int) or page_count < 0:
            raise ValueError("Tâche invalide.")
        now = utc_now()
        job = {"id": str(uuid.uuid4()), "stage": stage, "state": "queued", "page_index": 0,
               "page_count": page_count, "error": None, "created_at": now, "updated_at": now}
        jobs = self.list()
        jobs.append(job)
        atomic_json(self.path, jobs)
        return job

    def update(self, job_id: str, *, state: str, page_index: int | None = None, error: str | None = None) -> dict:
        if state not in STATES:
            raise ValueError("État de tâche inconnu.")
        jobs = self.list()
        for job in jobs:
            if job["id"] == job_id:
                index = job["page_index"] if page_index is None else page_index
                if not isinstance(index, int) or not 0 <= index <= job["page_count"]:
                    raise ValueError("Checkpoint de tâche invalide.")
                job.update(state=state, page_index=index, error=error, updated_at=utc_now())
                atomic_json(self.path, jobs)
                return job
        raise KeyError(job_id)

    def recover_interrupted(self) -> list[dict]:
        jobs = self.list()
        changed = False
        for job in jobs:
            if job.get("state") == "running":
                job["state"] = "interrupted"
                job["updated_at"] = utc_now()
                changed = True
        if changed:
            atomic_json(self.path, jobs)
        return jobs
