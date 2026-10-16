from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import tempfile
import threading
import weakref
import zipfile
from backend.projects.layout import folder
from pathlib import Path

from PIL import Image

from backend.core.project import RevisionConflict
from backend.projects.store import ProjectStore, atomic_json


_LOCK_GUARD = threading.Lock()
_EXPORT_LOCKS = weakref.WeakValueDictionary()


def _export_lock(store, chapter_id):
    # Share locks across ProjectStore instances opening the same library.
    key = (str(store.data_dir), chapter_id)
    with _LOCK_GUARD:
        lock = _EXPORT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _EXPORT_LOCKS[key] = lock
        return lock


def _chapter_recipe(project):
    from backend.ai.pipeline import project_context, signature
    return {"context": project_context(project), "pages": [
        {"id": page["id"], "signature": signature(page), "sha256": page["sha256"]}
        for page in project["pages"]]}


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _cached_archive(destination, metadata, recipe):
    try:
        cached = json.loads(metadata.read_text(encoding="utf-8"))
        if cached["recipe"] != recipe or destination.stat().st_size != cached["size"]:
            return False
        with zipfile.ZipFile(destination) as archive:
            return archive.namelist() == cached["entries"] and archive.testzip() is None
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, EOFError):
        return False


def export_project_compact(store: ProjectStore, chapter_id: str, options=None) -> Path:
    """Create a complete CBZ, preserving dimensions and untouched source pages.

    Options: format (webp/jpeg/png), quality (1..100), reuse_originals (bool).
    Freshness is checked through the pipeline for every page before source reuse.
    """
    from backend.ai.pipeline import output_file

    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("Options d'export invalides.")
    image_format = options.get("format", "webp")
    if image_format not in {"webp", "jpeg", "png"}:
        raise ValueError("Format d'export invalide : webp, jpeg ou png attendu.")
    quality = options.get("quality", 92)
    if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
        raise ValueError("La qualité d'export doit être un entier de 1 à 100.")
    reuse = options.get("reuse_originals", True)
    if not isinstance(reuse, bool):
        raise ValueError("L'option reuse_originals doit être un booléen.")
    with _export_lock(store, chapter_id):
        with tempfile.TemporaryDirectory(prefix=".renkavi-export-", dir=store.data_dir) as temporary:
            temporary = Path(temporary)
            with store._lock:
                project = store.load(chapter_id)
                if not project["pages"]:
                    raise ValueError("Le chapitre ne contient aucune page.")
                root = store.project_path(chapter_id)
                chapter_recipe = _chapter_recipe(project)
                sources = []
                live_sources = []
                for index, page in enumerate(project["pages"]):
                    # Always validate current processing, including pages reusing originals.
                    rendered = output_file(store, chapter_id, page["id"], "rendered")
                    if any(region.get("status") != "ignored" and not region.get("preserve_original")
                           and region.get("source_text", "").strip() and not region.get("target_text", "").strip()
                           for region in page["regions"]):
                        raise ValueError(f"Traduction incomplète : des régions de la page {page['name']} ne sont pas traduites.")
                    modified = bool(page.get("mask_strokes")) or any(
                        region.get("status") != "ignored" and not region.get("preserve_original")
                        and bool(region.get("target_text", "").strip()) for region in page["regions"])
                    file = rendered if modified else root / page["original"]
                    live_sources.append(file)
                    if modified:
                        # Pipeline images are replaced atomically. A hard link keeps the old
                        # inode stable even if another render replaces the visible image.
                        snapshot = temporary / f"source-{index}.png"
                        try:
                            os.link(file, snapshot)
                        except OSError:
                            shutil.copyfile(file, snapshot)
                        file = snapshot
                    sources.append((file, not modified and reuse))
                directory = folder(root, "export")
                directory.mkdir(exist_ok=True)
                destination = directory / f"chapitre_{project['target_language']}_{image_format}_q{quality}.cbz"
                metadata = destination.with_suffix(".cbz.json")

            # Validation, hashing and codecs can be expensive on long webtoon pages.
            # Only immutable originals and private snapshots are read without the lock.
            hashes = []
            for (file, original), page in zip(sources, project["pages"]):
                with Image.open(file) as image:
                    if image.size != (page["width"], page["height"]):
                        raise ValueError(f"Dimensions de page incohérentes : {page['name']}")
                    image.verify()
                hashes.append(_sha256(file))
            recipe = {"version": 1, **chapter_recipe,
                      "options": {"format": image_format, "quality": quality, "reuse_originals": reuse},
                      "source_sha256": hashes}

            def validate_current():
                current = store.load(chapter_id)
                if (current["revision"] != project["revision"] or _chapter_recipe(current) != chapter_recipe
                        or store.project_path(chapter_id) != root):
                    raise RevisionConflict("Le chapitre a changé pendant l’export. Relancez l’export après vos corrections.")
                for page in current["pages"]:
                    output_file(store, chapter_id, page["id"], "rendered")
                if [_sha256(file) for file in live_sources] != hashes:
                    raise RevisionConflict("Le rendu a changé pendant l’export. Relancez l’export.")

            if _cached_archive(destination, metadata, recipe):
                with store._lock:
                    validate_current()
                return destination

            partial = temporary / "archive.cbz"
            _encode_archive(partial, sources, image_format, quality)
            with zipfile.ZipFile(partial) as archive:
                entries = archive.namelist()
            with store._lock:
                validate_current()
                size = partial.stat().st_size
                os.replace(partial, destination)
                atomic_json(metadata, {"recipe": recipe, "size": size, "entries": entries})
            return destination


def _encode_archive(partial, sources, image_format, quality):
    with partial.open("wb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for index, (file, original) in enumerate(sources, 1):
                if original:
                    archive.write(file, f"{index:04d}{file.suffix.lower()}")
                    continue
                with Image.open(file) as image:
                    image.load()
                    # Webtoon strips can exceed WebP/JPEG codec dimension limits.
                    # Fall back per page rather than resizing or truncating the strip.
                    codec = image_format
                    if codec == "webp" and max(image.size) > 16383:
                        codec = "jpeg"
                    if codec == "jpeg" and max(image.size) > 65500:
                        codec = "png"
                    extension = "jpg" if codec == "jpeg" else codec
                    if codec == "jpeg":
                        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                            rgba = image.convert("RGBA")
                            converted = Image.new("RGB", image.size, "white")
                            converted.paste(rgba, mask=rgba.getchannel("A"))
                            image = converted
                        else:
                            image = image.convert("RGB")
                    elif image.mode not in {"RGB", "RGBA", "L", "LA"}:
                        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                    buffer = io.BytesIO()
                    settings = {"quality": quality, "method": 5} if codec == "webp" else (
                        {"quality": quality, "subsampling": 0} if codec == "jpeg" else {"optimize": True})
                    image.save(buffer, format=codec.upper(), **settings)
                    archive.writestr(f"{index:04d}.{extension}", buffer.getvalue())
        stream.flush()
        os.fsync(stream.fileno())
