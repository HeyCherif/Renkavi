from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from pathlib import Path
from backend.projects.layout import folder

from PIL import Image, ImageDraw

from backend.core.project import RevisionConflict
from backend.projects.store import ProjectStore, atomic_json


MAX_ARTIFACT_PIXELS = 40_000_000
METHOD = "flat-border-v1"


def _page(store: ProjectStore, project_id: str, page_id: str):
    project = store.load(project_id)
    page = next((item for item in project["pages"] if item["id"] == page_id), None)
    if page is None:
        raise FileNotFoundError("Page introuvable.")
    return project, page


def _paths(store: ProjectStore, project_id: str, page_id: str):
    safe_id = hashlib.sha256(page_id.encode("utf-8")).hexdigest()
    directory = folder(store.project_path(project_id), "artifacts") / safe_id
    return directory / "mask.png", directory / "cleaned.png", directory / "meta.json"


def _signature(page: dict) -> str:
    data = {"original": page["sha256"], "strokes": page.get("mask_strokes", []), "method": METHOD}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def variant_status(store: ProjectStore, project_id: str, page_id: str) -> dict:
    _, page = _page(store, project_id, page_id)
    mask_path, clean_path, meta_path = _paths(store, project_id, page_id)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, UnicodeError):
        return {"available": False, "method": METHOD}
    valid = meta.get("signature") == _signature(page) and mask_path.is_file() and clean_path.is_file()
    return {"available": valid, "method": METHOD}


def variant_file(store: ProjectStore, project_id: str, page_id: str, variant: str) -> Path:
    if variant not in {"mask", "cleaned"}:
        raise FileNotFoundError("Variante inconnue.")
    if not variant_status(store, project_id, page_id)["available"]:
        raise FileNotFoundError("Variante absente ou périmée.")
    mask_path, clean_path, _ = _paths(store, project_id, page_id)
    return mask_path if variant == "mask" else clean_path


def _draw_stroke(size: tuple[int, int], stroke: dict) -> tuple[Image.Image, tuple[int, int, int, int]]:
    radius = stroke["radius"]
    xs, ys = zip(*stroke["points"])
    left = max(0, math.floor(min(xs) - radius - 1))
    top = max(0, math.floor(min(ys) - radius - 1))
    right = min(size[0], math.ceil(max(xs) + radius + 1))
    bottom = min(size[1], math.ceil(max(ys) + radius + 1))
    box = (left, top, right, bottom)
    layer = Image.new("L", (right - left, bottom - top), 0)
    draw = ImageDraw.Draw(layer)
    points = [(x - left, y - top) for x, y in stroke["points"]]
    if len(points) > 1:
        draw.line(points, fill=255, width=max(1, round(radius * 2)), joint="curve")
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    return layer, box


def _border_color(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int]:
    left, top, right, bottom = box
    width, height = image.size
    pad = 5
    samples = []
    step_x = max(1, (right - left) // 150)
    step_y = max(1, (bottom - top) // 150)
    for x in range(max(0, left - pad), min(width, right + pad), step_x):
        samples.append(image.getpixel((x, max(0, top - pad))))
        samples.append(image.getpixel((x, min(height - 1, bottom + pad - 1))))
    for y in range(max(0, top - pad), min(height, bottom + pad), step_y):
        samples.append(image.getpixel((max(0, left - pad), y)))
        samples.append(image.getpixel((min(width - 1, right + pad - 1), y)))
    if not samples:
        return (255, 255, 255)
    return tuple(sorted(pixel[channel] for pixel in samples)[len(samples) // 2] for channel in range(3))


def _atomic_image(path: Path, image: Image.Image, compress_level: int = 6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as output:
            image.save(output, format="PNG", compress_level=compress_level)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def generate_variants(store: ProjectStore, project_id: str, page_id: str, expected_revision: int) -> dict:
    with store._lock:
        project, page = _page(store, project_id, page_id)
        if project["revision"] != expected_revision:
            raise RevisionConflict("Le projet a changé. Enregistrez et rechargez avant de générer.")
        strokes = page.get("mask_strokes", [])
        if not strokes:
            raise ValueError("Dessinez un masque avant de générer une variante.")
        if page["width"] * page["height"] > MAX_ARTIFACT_PIXELS:
            raise ValueError("Cette page est trop grande pour la génération locale actuelle (40 MP).")
        original_path = store.project_path(project_id) / page["original"]
        with Image.open(original_path) as source:
            original = source.convert("RGB")
        mask = Image.new("L", original.size, 0)
        cleaned = original.copy()
        for stroke in strokes:
            layer, box = _draw_stroke(original.size, stroke)
            if stroke["mode"] == "erase":
                mask.paste(0, box, layer)
                cleaned.paste(original.crop(box), box, layer)
            else:
                color = _border_color(original, box)
                cleaned.paste(color, box, layer)
                mask.paste(255, box, layer)
        mask_path, clean_path, meta_path = _paths(store, project_id, page_id)
        _atomic_image(mask_path, mask)
        _atomic_image(clean_path, cleaned)
        atomic_json(meta_path, {"signature": _signature(page), "method": METHOD, "revision": expected_revision})
        return {"available": True, "method": METHOD}
