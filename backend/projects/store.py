from __future__ import annotations

import copy
import json
import os
import uuid
import threading
from pathlib import Path
from typing import Any

from backend.core.project import ProjectValidationError, RevisionConflict, new_manifest, utc_now, validate_manifest


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ProjectStore:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.projects_dir = self.data_dir / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._paths: dict[str, Path] = {}

    def project_path(self, project_id: str) -> Path:
        try:
            parsed = uuid.UUID(project_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProjectValidationError("ID de projet invalide.") from exc
        if str(parsed) != project_id:
            raise ProjectValidationError("ID de projet invalide.")
        with self._lock:
            cached = self._paths.get(project_id)
            if cached is not None and (cached / "project.json").is_file():
                return cached
            direct = self.projects_dir / project_id
            if (direct / "project.json").is_file():
                self._paths[project_id] = direct
                return direct
            matches = list(self.projects_dir.glob(f"*/chapters/{project_id}/project.json"))
            if len(matches) > 1:
                raise ProjectValidationError("ID de chapitre dupliqué dans plusieurs projets.")
            if matches:
                self._paths[project_id] = matches[0].parent
                return matches[0].parent
            self._paths.pop(project_id, None)
            return direct

    def chapter_paths(self):
        """Snapshot of legacy and nested chapter folders, excluding staging folders."""
        with self._lock:
            paths = list(self.projects_dir.glob("*/project.json"))
            paths += list(self.projects_dir.glob("*/chapters/*/project.json"))
            return iter(sorted(path.parent for path in paths if not any(
                part.startswith(".") for part in path.relative_to(self.projects_dir).parts)))

    def create(self, title: str, source_language: str, target_language: str) -> dict[str, Any]:
        manifest = new_manifest(title, source_language, target_language)
        target = self.project_path(manifest["project_id"])
        staging = self.projects_dir / f".{manifest['project_id']}.tmp"
        staging.mkdir()
        try:
            atomic_json(staging / "project.json", manifest)
            staging.rename(target)
        except Exception:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return copy.deepcopy(manifest)

    def load(self, project_id: str) -> dict[str, Any]:
        path = self.project_path(project_id) / "project.json"
        if not path.is_file():
            raise FileNotFoundError(f"Projet introuvable : {project_id}")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ProjectValidationError("Manifeste JSON illisible.") from exc
        try:
            validate_manifest(manifest)
        except (AttributeError, TypeError, KeyError) as exc:
            raise ProjectValidationError("Structure de manifeste invalide.") from exc
        _ = self.project_path(manifest["project_id"])
        if manifest["project_id"] != project_id:
            raise ProjectValidationError("L'ID du manifeste ne correspond pas au dossier.")
        return manifest

    def save(self, manifest: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        with self._lock:
            validate_manifest(manifest)
            current = self.load(manifest["project_id"])
            if current["revision"] != expected_revision or manifest["revision"] != expected_revision:
                raise RevisionConflict("Le projet a été modifié ailleurs. Rechargez avant de sauvegarder.")
            if any(current[key]!=manifest[key] for key in ('source_language','target_language')) and any(r.get('target_text','').strip() for p in manifest['pages'] for r in p['regions']):
                raise ProjectValidationError('Ce chapitre contient déjà des traductions. Créez une nouvelle copie pour changer de langue sans perdre les corrections.')
            if len(current["pages"]) != len(manifest["pages"]):
                raise ProjectValidationError("Les pages originales ne peuvent pas être modifiées depuis l'éditeur.")
            immutable = ("id", "order", "name", "original", "width", "height", "sha256")
            for previous, incoming in zip(current["pages"], manifest["pages"]):
                if any(previous[key] != incoming[key] for key in immutable):
                    raise ProjectValidationError("Les pages originales ne peuvent pas être modifiées depuis l'éditeur.")
            updated = copy.deepcopy(manifest)
            updated["revision"] = current["revision"] + 1
            updated["created_at"] = current["created_at"]
            updated["updated_at"] = utc_now()
            validate_manifest(updated)
            destination=self.project_path(updated["project_id"])
            atomic_json(destination / "project.json", updated)
            from backend.projects.transcripts import write_chapter_transcript
            write_chapter_transcript(destination, updated)
            return updated

    def list_projects(self) -> list[dict[str, Any]]:
        from backend.projects.formats import source_format,export_extension
        summaries = []
        for directory in self.chapter_paths():
            try:
                manifest = self.load(directory.name)
                summaries.append({
                    "project_id": manifest["project_id"],
                    "workspace_id": directory.parent.parent.name if directory.parent.name == "chapters" else None,
                    "folder": str(directory),
                    "title": manifest["title"],
                    "source_format":source_format(manifest),
                    "export_extension":export_extension(manifest),
                    "source_language": manifest["source_language"],
                    "target_language": manifest["target_language"],
                    "page_count": len(manifest["pages"]),
                    "updated_at": manifest["updated_at"],
                    "revision": manifest["revision"],
                    "translated_regions": sum(bool(r['target_text']) for p in manifest['pages'] for r in p['regions'] if r['status']!='ignored'),
                    "region_count": sum(r['status']!='ignored' for p in manifest['pages'] for r in p['regions']),
                    "translation_engines": sorted({r.get('translation_engine') for p in manifest['pages'] for r in p['regions'] if r.get('target_text','').strip() and r.get('translation_engine')}),
                    "contextual_regions": sum(bool(r.get('translation_context_version')) for p in manifest['pages'] for r in p['regions'] if r.get('target_text','').strip()),
                })
            except (ProjectValidationError, FileNotFoundError) as exc:
                summaries.append({"project_id": directory.name, "error": str(exc),
                                  "workspace_id": directory.parent.parent.name if directory.parent.name == "chapters" else None,
                                  "folder": str(directory)})
        return sorted(summaries, key=lambda item: item.get("updated_at", ""), reverse=True)
