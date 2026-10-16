from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any


SCHEMA_VERSION = 2
STATUSES = {"detected", "ocr_checked", "translated", "reviewed", "final", "ignored"}


class ProjectValidationError(ValueError):
    pass


class RevisionConflict(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_manifest(title: str, source_language: str, target_language: str) -> dict[str, Any]:
    now = utc_now()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "project_id": str(uuid.uuid4()),
        "revision": 1,
        "title": title.strip(),
        "source_language": source_language.strip().lower(),
        "target_language": target_language.strip().lower(),
        "created_at": now,
        "updated_at": now,
        "pages": [],
        "provenance": {},
    }
    validate_manifest(manifest)
    return manifest


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ProjectValidationError(message)


def _safe_original(value: Any) -> bool:
    if not isinstance(value, str) or chr(92) in value or value.startswith("/"):
        return False
    path = PurePosixPath(value)
    return len(path.parts) == 2 and path.parts[0] in {"originals", "original"} and path.parts[1] not in {".", ".."}


def _confidence(value: Any) -> bool:
    return value is None or (isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1)


def validate_manifest(manifest: Any) -> None:
    _check(isinstance(manifest, dict), "Le manifeste doit être un objet JSON.")
    _check(manifest.get("schema_version") == SCHEMA_VERSION, "Version de projet non prise en charge.")
    try:
        uuid.UUID(manifest.get("project_id", ""))
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError("ID de projet invalide.") from exc
    _check(isinstance(manifest.get("revision"), int) and manifest["revision"] >= 1, "Révision de projet invalide.")
    _check(isinstance(manifest.get("title"), str) and 0 < len(manifest["title"].strip()) <= 160, "Titre de projet invalide.")
    for key in ("source_language", "target_language"):
        _check(isinstance(manifest.get(key), str) and bool(re.fullmatch(r"[a-z]{2,8}|auto", manifest[key])), f"{key} invalide.")
    for key in ("created_at", "updated_at"):
        _check(isinstance(manifest.get(key), str) and bool(manifest[key]), f"{key} absent.")
    _check(isinstance(manifest.get("provenance"), dict), "Provenance invalide.")
    _check(isinstance(manifest.get('translation_context',''),str) and len(manifest.get('translation_context',''))<=1200,'La fiche de contexte doit contenir au maximum 1 200 caractères.')
    pages = manifest.get("pages")
    _check(isinstance(pages, list) and len(pages) <= 1000, "Liste de pages invalide.")
    page_ids: set[str] = set()
    region_ids: set[str] = set()
    originals: set[str] = set()
    for order, page in enumerate(pages):
        _check(isinstance(page, dict), f"Page {order + 1} invalide.")
        page_id = page.get("id")
        _check(isinstance(page_id, str) and bool(page_id) and page_id not in page_ids, "ID de page absent ou dupliqué.")
        page_ids.add(page_id)
        _check(page.get("order") == order, "Ordre des pages incohérent.")
        _check(isinstance(page.get("name"), str) and bool(page["name"]), "Nom de page invalide.")
        original = page.get("original")
        _check(_safe_original(original) and original not in originals, "Chemin d'original invalide ou dupliqué.")
        originals.add(original)
        width, height = page.get("width"), page.get("height")
        _check(isinstance(width, int) and isinstance(height, int) and 0 < width <= 100000 and 0 < height <= 100000, "Dimensions de page invalides.")
        _check(isinstance(page.get("sha256"), str) and bool(re.fullmatch(r"[0-9a-f]{64}", page["sha256"])), "Empreinte d'image invalide.")
        regions = page.get("regions")
        _check(isinstance(regions, list), "Régions de page invalides.")
        strokes = page.get("mask_strokes", [])
        _check(isinstance(strokes, list) and len(strokes) <= 10000, "Traits de masque invalides.")
        for stroke in strokes:
            _check(isinstance(stroke, dict) and stroke.get("mode") in {"paint", "erase"}, "Trait de masque invalide.")
            radius = stroke.get("radius")
            _check(isinstance(radius, (int, float)) and not isinstance(radius, bool) and math.isfinite(radius) and 1 <= radius <= 100, "Rayon de masque invalide.")
            points = stroke.get("points")
            _check(isinstance(points, list) and 1 <= len(points) <= 2000, "Points de masque invalides.")
            for point in points:
                _check(isinstance(point, list) and len(point) == 2 and all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in point), "Coordonnée de masque invalide.")
                _check(0 <= point[0] <= width and 0 <= point[1] <= height, "Trait de masque hors de la page.")
        for region in regions:
            _check(isinstance(region, dict), "Région invalide.")
            region_id = region.get("id")
            _check(isinstance(region_id, str) and bool(region_id) and region_id not in region_ids, "ID de région absent ou dupliqué.")
            region_ids.add(region_id)
            box = region.get("box")
            _check(isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in box), "Boîte de région invalide.")
            x1, y1, x2, y2 = box
            _check(0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height, "Boîte hors de la page.")
            for field in ("source_text", "target_text"):
                _check(isinstance(region.get(field), str), f"{field} invalide.")
            _check(region.get("status") in STATUSES, "Statut de région invalide.")
            for field in ("confidence_detection", "confidence_ocr"):
                _check(_confidence(region.get(field)), f"{field} invalide.")
