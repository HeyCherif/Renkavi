from __future__ import annotations

import json
import uuid
from pathlib import Path, PurePosixPath

from backend.core.project import new_manifest
from backend.projects.importer import MAX_TOTAL_BYTES, copy_image, staged_project
from backend.projects.store import ProjectStore


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Fichier V1 illisible : {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Format V1 invalide : {path.name}")
    return value


def migrate_v1(store: ProjectStore, legacy_dir: Path | str) -> dict:
    legacy = Path(legacy_dir).expanduser().resolve()
    meta = _read_json(legacy / "project.json")
    translation = _read_json(legacy / "translation.json")
    if translation.get("schema") != "manhwa-localizer-1" or not isinstance(meta.get("project_id"), str) or translation.get("project_id") != meta["project_id"]:
        raise ValueError("Le projet V1 et sa traduction ne correspondent pas.")
    paths = meta.get("pages")
    items = translation.get("items")
    if not isinstance(paths, list) or not paths or len(paths) > 1000 or meta.get("page_count") != len(paths) or not isinstance(items, list):
        raise ValueError("Liste de pages ou de régions V1 invalide.")
    seen_paths = set()
    resolved = []
    for name in paths:
        if not isinstance(name, str):
            raise ValueError("Chemin de page V1 invalide.")
        normalized = name.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if len(parts) != 2 or parts[0] != "raw" or normalized in seen_paths:
            raise ValueError("Chemin de page V1 invalide ou dupliqué.")
        seen_paths.add(normalized)
        path = (legacy / normalized).resolve()
        if not path.is_file() or path.parent != (legacy / "raw").resolve():
            raise ValueError(f"Page V1 introuvable : {name}")
        resolved.append(path)
    region_ids = set()
    per_page: list[list[dict]] = [[] for _ in resolved]
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or item["id"] in region_ids:
            raise ValueError("ID de région V1 invalide ou dupliqué.")
        region_ids.add(item["id"])
        index = item.get("page_index")
        if not isinstance(index, int) or not 0 <= index < len(resolved):
            raise ValueError("Région V1 liée à une page inconnue.")
        source_text = item.get("source_text", "")
        target_text = item.get("target_text", "")
        if not isinstance(source_text, str) or not isinstance(target_text, str):
            raise ValueError("Texte de région V1 invalide.")
        confidence = item.get("confidence")
        region = {
            "id": item["id"], "box": item.get("box"),
            "source_text": source_text, "target_text": target_text,
            "status": "translated" if target_text.strip() else "detected",
            "confidence_detection": confidence, "confidence_ocr": None,
        }
        for optional in ("polygon", "style", "kind", "font_match", "lock_hash"):
            if optional in item:
                region[optional] = item[optional]
        per_page[index].append(region)
    manifest = new_manifest(legacy.name, meta.get("source_language", "auto"), meta.get("target_language", "fr"))
    manifest["provenance"] = {"legacy_project_id": meta["project_id"], "migration": "copy-from-v1"}
    total = 0
    with staged_project(store, manifest) as staging:
        for index, path in enumerate(resolved):
            relative = f"original/{index + 1:04d}{path.suffix.lower()}"
            with path.open("rb") as stream:
                width, height, digest, size = copy_image(stream, staging / relative)
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Le projet V1 dépasse 2 Go d'images.")
            manifest["pages"].append({
                "id": str(uuid.uuid4()), "order": index, "name": path.name,
                "original": relative, "width": width, "height": height,
                "sha256": digest, "regions": per_page[index],
            })
    return manifest
