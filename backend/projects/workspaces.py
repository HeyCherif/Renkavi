from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from backend.core.project import ProjectValidationError, new_manifest, utc_now
from backend.projects.store import ProjectStore, atomic_json


class Workspaces:
    """Series folders. Membership is derived from chapter folders, never a second index."""

    def __init__(self, store: ProjectStore):
        self.store = store
        self.migration_errors: list[dict[str, str]] = []

    def _path(self, workspace_id: str):
        try:
            if str(uuid.UUID(workspace_id)) != workspace_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProjectValidationError("ID de projet invalide.") from exc
        return self.store.projects_dir / workspace_id

    def _read(self, workspace_id):
        path = self._path(workspace_id) / "workspace.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ProjectValidationError("Manifeste de projet illisible.") from exc
        if not isinstance(data, dict) or data.get("workspace_id") != workspace_id or data.get("schema_version") != 1:
            raise ProjectValidationError("Manifeste de projet invalide.")
        self._validated_fields(data)
        return data

    @staticmethod
    def _validated_fields(data):
        # Share chapter title/language validation without coupling their schemas.
        if any(not isinstance(data.get(key, default), str) for key, default in (
            ("title", ""), ("source_language", "en"), ("target_language", "fr"))):
            raise ProjectValidationError("Titre ou langue de projet invalide.")
        candidate = new_manifest(data.get("title", ""), data.get("source_language", "en"), data.get("target_language", "fr"))
        return {key: candidate[key] for key in ("title", "source_language", "target_language")}

    def inbox(self):
        """One shared project for imports without an explicit destination.

        Never create one top-level workspace per uploaded chapter.
        """
        workspace_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'renkavi:inbox:unclassified'))
        with self.store._lock:
            if not (self._path(workspace_id) / 'workspace.json').is_file():
                self._create(workspace_id, self._validated_fields(dict(
                    title='À classer', source_language='auto', target_language='fr')))
            return self.get(workspace_id)

    def create(self, title, source_language="en", target_language="fr"):
        fields = self._validated_fields(dict(title=title, source_language=source_language, target_language=target_language))
        workspace_id = str(uuid.uuid4())
        with self.store._lock:
            self._create(workspace_id, fields)
            return self.get(workspace_id)

    def _create(self, workspace_id, fields):
        now = utc_now()
        data = dict(schema_version=1, workspace_id=workspace_id, created_at=now, updated_at=now, **fields)
        folder = self._path(workspace_id)
        (folder / "chapters").mkdir(parents=True, exist_ok=True)
        atomic_json(folder / "workspace.json", data)

    def get(self, workspace_id):
        with self.store._lock:
            data = self._read(workspace_id)
            data["id"] = workspace_id
            data["folder"] = str(self._path(workspace_id))
            data["chapters"] = [chapter for chapter in self.store.list_projects() if chapter.get("workspace_id") == workspace_id]
            data["chapter_count"] = len(data["chapters"])
            data["page_count"] = sum(chapter.get("page_count", 0) for chapter in data["chapters"])
            return data

    def list(self):
        with self.store._lock:
            workspaces = []
            for path in self.store.projects_dir.glob("*/workspace.json"):
                if path.parent.name.startswith("."):
                    continue
                try:
                    workspaces.append(self.get(path.parent.name))
                except (ProjectValidationError, FileNotFoundError) as exc:
                    workspaces.append({"id": path.parent.name, "workspace_id": path.parent.name, "error": str(exc)})
            return sorted(workspaces, key=lambda data: data.get("updated_at", ""), reverse=True)

    def update(self, workspace_id, data: dict[str, Any]):
        with self.store._lock:
            current = self._read(workspace_id)
            fields = self._validated_fields({**current, **{key: data[key] for key in ("title", "source_language", "target_language") if key in data}})
            current.update(fields, updated_at=utc_now())
            atomic_json(self._path(workspace_id) / "workspace.json", current)
            return self.get(workspace_id)

    def attach(self, workspace_id, chapter_id):
        """Caller must exclude active jobs before moving a chapter."""
        with self.store._lock:
            current = self._read(workspace_id)
            self.store.load(chapter_id)
            source = self.store.project_path(chapter_id)
            destination = self._path(workspace_id) / "chapters" / chapter_id
            if source != destination:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ProjectValidationError("Ce chapitre existe déjà dans le projet.")
                source.rename(destination)
                self.store._paths[chapter_id] = destination
                current["updated_at"] = utc_now()
                atomic_json(self._path(workspace_id) / "workspace.json", current)
            return self.get(workspace_id)

    def migrate_legacy(self):
        """Restartable wrapping: persist a deterministic parent before moving its chapter."""
        with self.store._lock:
            self.migration_errors = []
            for directory in list(self.store.chapter_paths()):
                if directory.parent != self.store.projects_dir:
                    continue
                try:
                    chapter = self.store.load(directory.name)
                except (ProjectValidationError, FileNotFoundError) as exc:
                    self.migration_errors.append({"chapter_id": directory.name, "folder": str(directory), "error": str(exc)})
                    logging.getLogger(__name__).warning("Chapitre conservé sans migration (%s) : %s", directory, exc)
                    continue
                workspace_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "renkavi:legacy:" + directory.name))
                if not (self._path(workspace_id) / "workspace.json").is_file():
                    self._create(workspace_id, self._validated_fields(chapter))
                self.attach(workspace_id, directory.name)
            return self.list()
