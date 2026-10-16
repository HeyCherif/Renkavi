from __future__ import annotations

import hashlib
import re
import shutil
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from PIL import Image, UnidentifiedImageError

from backend.core.project import new_manifest, validate_manifest
from backend.projects.store import ProjectStore, atomic_json
from backend.projects.formats import detect_source,IMAGE_FORMATS


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ARCHIVE_EXTENSIONS = {".cbz", ".zip"}
MAX_PAGES = 1000
MAX_IMAGE_BYTES = 250 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_PIXELS = 100_000_000


def natural_key(name: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


@contextmanager
def staged_project(store: ProjectStore, manifest: dict) -> Iterator[Path]:
    target = store.project_path(manifest["project_id"])
    staging = store.projects_dir / f".{manifest['project_id']}.tmp"
    staging.mkdir()
    try:
        (staging / "original").mkdir()
        (staging / "traduction").mkdir()
        (staging / "export").mkdir()
        yield staging
        validate_manifest(manifest)
        atomic_json(staging / "project.json", manifest)
        from backend.projects.transcripts import write_chapter_transcript
        write_chapter_transcript(staging, manifest)
        staging.rename(target)
    finally:
        if staging.exists():
            if staging.parent.resolve() != store.projects_dir.resolve():
                raise RuntimeError("Dossier temporaire inattendu.")
            shutil.rmtree(staging)


def copy_image(source: BinaryIO, destination: Path, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[int, int, str, int]:
    digest = hashlib.sha256()
    total = 0
    with destination.open("wb") as output:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Une image dépasse 250 Mo.")
            digest.update(chunk)
            output.write(chunk)
    try:
        with Image.open(destination) as image:
            image.verify()
        with Image.open(destination) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width > 100000 or height > 100000 or width * height > MAX_PIXELS:
                raise ValueError("Dimensions d'image invalides.")
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError(f"Image illisible : {destination.name}") from exc
    return width, height, digest.hexdigest(), total


def _archive_members(archive: zipfile.ZipFile) -> list[tuple[str, zipfile.ZipInfo]]:
    selected = []
    seen = set()
    for member in archive.infolist():
        name = member.filename.replace("\\", "/")
        raw_parts = name.split("/")
        parts = PurePosixPath(name).parts
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or any(part in {"..", "."} for part in raw_parts):
            raise ValueError(f"Chemin dangereux dans l'archive : {member.filename}")
        if member.is_dir() or not parts or parts[0] == "__MACOSX":
            continue
        # Unix symlink in ZIP external attributes.
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("Lien symbolique interdit dans l'archive.")
        if PurePosixPath(name).suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        folded = PurePosixPath(name).as_posix().casefold()
        if folded in seen:
            raise ValueError("Nom d'image dupliqué dans l'archive.")
        seen.add(folded)
        if member.file_size > MAX_IMAGE_BYTES:
            raise ValueError("Une image dépasse 250 Mo.")
        selected.append((name, member))
    return sorted(selected, key=lambda item: natural_key(item[0]))


def import_source(
    store: ProjectStore,
    source: Path | str,
    title: str | None = None,
    source_language: str = "auto",
    target_language: str = "fr",
) -> dict:
    source = Path(source).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    kind=detect_source(source)
    if kind in {'pdf','docx'}:
        if kind=='pdf':from backend.projects.documents_pdf import import_pdf as import_document
        else:from backend.projects.documents_word import import_docx as import_document
        return import_document(store,source,title=title,source_language=source_language,target_language=target_language)
    if source.is_dir():
        files = sorted((p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), key=lambda p: natural_key(p.relative_to(source).as_posix()))
        if any(p.is_symlink() for p in files):
            raise ValueError("Les liens symboliques ne sont pas acceptés dans un dossier d'images.")
        entries = [(p.relative_to(source).as_posix(), p) for p in files]
        archive = None
    elif kind in IMAGE_FORMATS:
        entries = [(source.name, source)]
        archive = None
    elif kind in {'cbz','zip'}:
        archive = zipfile.ZipFile(source)
        try:
            entries = _archive_members(archive)
        except Exception:
            archive.close()
            raise
    else:
        raise ValueError("Choisissez une image, un dossier, un CBZ ou un ZIP.")
    if not entries or len(entries) > MAX_PAGES:
        if archive:
            archive.close()
        raise ValueError("Le projet doit contenir entre 1 et 1000 images.")
    manifest = new_manifest(title or source.stem or source.name, source_language, target_language)
    manifest['provenance'].update(source_format=kind,source_name=source.name)
    total = 0
    try:
        with staged_project(store, manifest) as staging:
            for index, (name, entry) in enumerate(entries):
                suffix = '.'+IMAGE_FORMATS[kind] if kind in IMAGE_FORMATS else PurePosixPath(name).suffix.lower()
                relative = f"original/{index + 1:04d}{suffix}"
                destination = staging / relative
                if archive:
                    stream = archive.open(entry)
                else:
                    stream = entry.open("rb")
                with stream:
                    width, height, digest, size = copy_image(stream, destination)
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("Le projet dépasse 2 Go d'images.")
                manifest["pages"].append({
                    "id": str(uuid.uuid4()), "order": index, "name": name,
                    "original": relative, "width": width, "height": height,
                    "sha256": digest, "regions": [],
                })
    finally:
        if archive:
            archive.close()
    return manifest
