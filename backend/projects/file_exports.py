"""Native file exports and complete, atomic project ZIP bundles."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
from backend.projects.layout import folder as layout_folder
from pathlib import Path

from PIL import Image

from backend.core.project import RevisionConflict
from backend.projects.exporting import _chapter_recipe, _export_lock, _sha256, export_project_compact
from backend.projects.formats import IMAGE_FORMATS, source_format
from backend.projects.store import atomic_json


def _options(options):
    options = {} if options is None else options
    if not isinstance(options, dict) or options.get("output", "auto") not in {"auto", "pdf", "cbz", "docx", "image"}:
        raise ValueError("Sortie invalide : auto, pdf, cbz, docx ou image attendue.")
    quality = options.get("quality", 92)
    if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
        raise ValueError("La qualité doit être un entier entre 1 et 100.")
    if not isinstance(options.get("reuse_originals", True), bool):
        raise ValueError("reuse_originals doit être un booléen.")
    return dict(options)


def _safe_name(value, fallback="document"):
    value = unicodedata.normalize("NFKC", str(value))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:100].strip(" .")
    if not value:
        return fallback
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value, re.I):
        value = "_" + value
    return value


def _snapshot(file, destination):
    try:
        os.link(file, destination)
    except OSError:
        shutil.copyfile(file, destination)
    return destination


def _complete_page(page):
    if any(region.get("status") != "ignored" and not region.get("preserve_original")
           and region.get("source_text", "").strip() and not region.get("target_text", "").strip()
           for region in page["regions"]):
        raise ValueError(f"Traduction incomplète : {page['name']}.")


def _fresh_files(store, project):
    from backend.ai.pipeline import output_file
    files = []
    if not project["pages"]:
        raise ValueError("Le fichier ne contient aucune page.")
    for page in project["pages"]:
        files.append(output_file(store, project["project_id"], page["id"], "rendered"))
        _complete_page(page)
    return files


def _validate_project(store, project, root, recipe):
    current = store.load(project["project_id"])
    if (current["revision"] != project["revision"] or _chapter_recipe(current) != recipe
        or store.project_path(project["project_id"]) != root):
        raise RevisionConflict("Le fichier a changé pendant l'export. Relancez l'export après vos corrections.")
    _fresh_files(store, current)


def _cached(destination, metadata, recipe, *, archive=False):
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
        if data["recipe"] != recipe or destination.stat().st_size != data["size"] or _sha256(destination) != data["sha256"]:
            return False
        if archive:
            with zipfile.ZipFile(destination) as file:
                return file.namelist() == data["entries"] and file.testzip() is None
        with Image.open(destination) as image:
            image.verify()
        return True
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, EOFError):
        return False


def _encode_image(destination, source, codec, quality):
    with Image.open(source) as image:
        image.load()
        if codec == "webp" and max(image.size) > 16383:
            raise ValueError("Cette image dépasse les dimensions WebP. Choisissez une sortie PNG.")
        if codec == "jpeg" and max(image.size) > 65500:
            raise ValueError("Cette image dépasse les dimensions JPEG. Choisissez une sortie PNG.")
        if codec in {"jpeg", "bmp"}:
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", image.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
        elif image.mode not in {"RGB", "RGBA", "L", "LA"}:
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        settings = {"quality": quality, "subsampling": 0} if codec == "jpeg" else {"quality": quality, "method": 5} if codec == "webp" else {}
        with destination.open("wb") as output:
            image.save(output, format=codec.upper(), **settings)
            output.flush()
            os.fsync(output.fileno())


def _export_image(store, chapter_id, options, codec):
    if codec not in IMAGE_FORMATS:
        raise ValueError("Format image invalide : png, jpeg, webp ou bmp attendu.")
    with _export_lock(store, chapter_id):
        with tempfile.TemporaryDirectory(prefix=".renkavi-image-", dir=store.data_dir) as temporary:
            temporary = Path(temporary)
            with store._lock:
                project = store.load(chapter_id)
                if len(project["pages"]) != 1:
                    raise ValueError("La sortie image nécessite un fichier d'une seule page.")
                rendered = _fresh_files(store, project)[0]
                root, state = store.project_path(chapter_id), _chapter_recipe(project)
                page = project["pages"][0]
                original = root / page["original"]
                original_snapshot = _snapshot(original, temporary / "original")
                rendered_snapshot = _snapshot(rendered, temporary / "rendered.png")
                modified = bool(page.get("mask_strokes")) or any(
                    region.get("status") != "ignored" and not region.get("preserve_original") and region.get("target_text", "").strip()
                    for region in page["regions"])
                reuse = not modified and options.get("reuse_originals", True) and codec == source_format(project)
                chosen = original_snapshot if not modified else rendered_snapshot
                directory = layout_folder(root, "export")
                directory.mkdir(exist_ok=True)
                destination = directory / f"{_safe_name(project['title'])}_{project['target_language']}.{IMAGE_FORMATS[codec]}"
                metadata = destination.with_suffix(destination.suffix + ".json")
            original_hash, rendered_hash = _sha256(original_snapshot), _sha256(rendered_snapshot)
            if original_hash != page["sha256"]:
                raise ValueError("L'image originale a été modifiée ; export refusé.")
            with Image.open(chosen) as image:
                if image.size != (page["width"], page["height"]):
                    raise ValueError("Dimensions du rendu incohérentes.")
                image.verify()
            recipe = {"version": 1, **state, "original": original_hash, "rendered": rendered_hash,
                      "codec": codec, "quality": options.get("quality", 92), "reuse": reuse}

            def validate_current():
                _validate_project(store, project, root, state)
                if _sha256(original) != original_hash or _sha256(rendered) != rendered_hash:
                    raise RevisionConflict("Le rendu ou l'original a changé pendant l'export image.")

            if _cached(destination, metadata, recipe):
                with store._lock:
                    validate_current()
                return destination
            partial = temporary / ("image." + IMAGE_FORMATS[codec])
            if reuse:
                shutil.copyfile(chosen, partial)
                with partial.open("rb") as stream:
                    os.fsync(stream.fileno())
            else:
                _encode_image(partial, chosen, codec, options.get("quality", 92))
            digest, size = _sha256(partial), partial.stat().st_size
            with store._lock:
                validate_current()
                os.replace(partial, destination)
                atomic_json(metadata, {"recipe": recipe, "size": size, "sha256": digest})
            return destination


def _zip_native(store, chapter_id, options):
    compact = export_project_compact(store, chapter_id, options)
    with _export_lock(store, chapter_id):
        with tempfile.TemporaryDirectory(prefix=".renkavi-zip-", dir=store.data_dir) as temporary:
            temporary = Path(temporary)
            with store._lock:
                project = store.load(chapter_id)
                _fresh_files(store, project)
                root, state = store.project_path(chapter_id), _chapter_recipe(project)
                snapshot = _snapshot(compact, temporary / "source.cbz")
                destination = layout_folder(root, "export") / f"{_safe_name(project['title'])}_{project['target_language']}.zip"
                metadata = destination.with_suffix(".zip.json")
            digest = _sha256(snapshot)
            recipe = {"version": 1, **state, "compact": digest, "options": options}

            def validate_current():
                _validate_project(store, project, root, state)
                if _sha256(compact) != digest:
                    raise RevisionConflict("L'archive de pages a changé pendant l'export ZIP.")

            if _cached(destination, metadata, recipe, archive=True):
                with store._lock:
                    validate_current()
                return destination
            partial = temporary / "archive.zip"
            shutil.copyfile(snapshot, partial)
            with zipfile.ZipFile(partial) as archive:
                entries = archive.namelist()
                if archive.testzip() is not None:
                    raise ValueError("L'archive ZIP des images est endommagée.")
            with store._lock:
                validate_current()
                os.replace(partial, destination)
                atomic_json(metadata, {"recipe": recipe, "size": destination.stat().st_size, "sha256": digest, "entries": entries})
            return destination


def export_native(store, chapter_id, options=None):
    options = _options(options)
    with store._lock:
        project = store.load(chapter_id)
        kind = source_format(project)
    output = options.get("output", "auto")
    if output == "docx" or (output == "auto" and kind == "docx"):
        if kind != "docx":
            raise ValueError("La sortie DOCX nécessite un document Word structuré importé.")
        from backend.projects.documents_word import export_docx
        return export_docx(store, chapter_id, options)
    if kind == "docx":
        raise ValueError("Ce document Word doit être exporté en DOCX ; son aperçu simplifié ne constitue pas une conversion fidèle.")
    if output == "pdf" or (output == "auto" and kind == "pdf"):
        from backend.projects.documents_pdf import export_pdf
        return export_pdf(store, chapter_id, options)
    if output == "cbz" or (output == "auto" and kind in {"cbz", "images"}):
        return export_project_compact(store, chapter_id, options)
    if output == "auto" and kind == "zip":
        return _zip_native(store, chapter_id, options)
    if kind in IMAGE_FORMATS and output in {"auto", "image"}:
        codec = options.get("format", kind) if output == "image" else kind
        return _export_image(store, chapter_id, options, codec)
    raise ValueError("Cette conversion n'est pas prise en charge. Choisissez la sortie automatique, PDF ou CBZ.")


def _workspace_snapshot(store, workspaces, workspace_id):
    workspace = workspaces.get(workspace_id)
    if not workspace["chapters"]:
        raise ValueError("Ce projet ne contient aucun fichier à exporter.")
    rows = []
    for chapter in sorted(workspace["chapters"], key=lambda row: row["project_id"]):
        if chapter.get("error"):
            raise ValueError(f"Un fichier du projet est illisible : {chapter['error']}")
        project = store.load(chapter["project_id"])
        rendered = _fresh_files(store, project)
        root = store.project_path(project["project_id"])
        originals = [root / page["original"] for page in project["pages"]]
        original_hashes = [_sha256(file) for file in originals]
        if original_hashes != [page["sha256"] for page in project["pages"]]:
            raise ValueError("Une image originale du projet a été modifiée.")
        source = project.get("provenance", {}).get("source_file")
        source_hash = None
        if source:
            source_path = (root / source).resolve()
            if not source_path.is_relative_to(root.resolve()):
                raise ValueError("Chemin de document source invalide.")
            source_hash = _sha256(source_path)
            if source_hash != project["provenance"].get("source_sha256"):
                raise ValueError("Un document original du projet a été modifié.")
        rows.append({"id": project["project_id"], "revision": project["revision"], "root": str(root),
                     "title": project["title"], "language": project["target_language"], "kind": source_format(project),
                     "recipe": _chapter_recipe(project), "rendered": [_sha256(file) for file in rendered],
                     "originals": original_hashes, "source": source_hash})
    return workspace, {"workspace_id": workspace_id, "title": workspace["title"], "folder": workspace["folder"], "chapters": rows}


def export_workspace(store, workspaces, workspace_id, options=None):
    options = _options(options)
    with _export_lock(store, "workspace:" + workspace_id):
        with tempfile.TemporaryDirectory(prefix=".renkavi-workspace-", dir=store.data_dir) as temporary:
            temporary = Path(temporary)
            with store._lock:
                workspace, state = _workspace_snapshot(store, workspaces, workspace_id)
            exports, used = [], set()
            for index, chapter in enumerate(state["chapters"]):
                native = export_native(store, chapter["id"], options)
                snapshot = _snapshot(native, temporary / f"file-{index}{native.suffix}")
                stem = f"{_safe_name(chapter['title'])}_{chapter['language']}"
                name, counter = stem + native.suffix, 2
                while name.casefold() in used:
                    name = f"{stem}_{counter}{native.suffix}"
                    counter += 1
                used.add(name.casefold())
                exports.append((snapshot, name, _sha256(snapshot)))
            recipe = {"version": 1, **state, "options": options, "files": [{"name": name, "sha256": digest} for _, name, digest in exports]}
            folder = layout_folder(Path(workspace["folder"]), "export")
            folder.mkdir(exist_ok=True)
            destination = folder / f"{_safe_name(workspace['title'], 'projet')}.zip"
            metadata = destination.with_suffix(".zip.json")

            def validate_current():
                _, current = _workspace_snapshot(store, workspaces, workspace_id)
                if current != state:
                    raise RevisionConflict("Le contenu du projet a changé pendant l'export. Relancez l'export complet.")

            if _cached(destination, metadata, recipe, archive=True):
                with store._lock:
                    validate_current()
                return destination
            partial = temporary / "project.zip"
            with partial.open("wb") as stream:
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                    for file, name, _ in exports:
                        archive.write(file, name)
                stream.flush()
                os.fsync(stream.fileno())
            digest, size = _sha256(partial), partial.stat().st_size
            with store._lock:
                validate_current()
                os.replace(partial, destination)
                atomic_json(metadata, {"recipe": recipe, "size": size, "sha256": digest, "entries": [name for _, name, _ in exports]})
            return destination
