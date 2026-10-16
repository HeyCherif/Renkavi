"""Local PDF previews, native text extraction and searchable raster PDF exports.

PDFium is serialized process-wide. Neither imports nor exports execute embedded
JavaScript or fetch external resources. Original document bytes remain immutable.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import threading
import uuid
from contextlib import ExitStack, closing
from functools import lru_cache
from backend.projects.layout import folder
from pathlib import Path

from PIL import Image

from backend.core.project import RevisionConflict, new_manifest
from backend.projects.exporting import _chapter_recipe, _export_lock, _sha256
from backend.projects.store import atomic_json

PDFIUM_LOCK = threading.RLock()
_FONT_LOCK = threading.RLock()
DPI = 150
MAX_PAGES = 1000
MAX_PIXELS = 100_000_000
MAX_TOTAL_BYTES = 2 * 1024**3
MAX_TEXT_RECTS = 20_000
MAX_TEXT_CHARS = 1_000_000


def _pdfium():
    try:
        import pypdfium2
        return pypdfium2
    except ImportError as exc:
        raise ValueError("Le support PDF manque. Relancez INSTALLER.bat.") from exc


def _union(boxes):
    return [min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes)]


def _native_regions(page, bitmap):
    """Merge overlapping text rectangles before extraction to avoid double selection."""
    converter = bitmap.get_posconv(page)
    crop = page.get_bbox()
    rectangles = []
    with closing(page.get_textpage()) as textpage:
        if textpage.count_chars() > MAX_TEXT_CHARS:
            raise ValueError("Cette page PDF contient trop de texte natif.")
        count = textpage.count_rects()
        if count > MAX_TEXT_RECTS:
            raise ValueError("Cette page PDF contient trop de zones de texte.")
        for index in range(count):
            left, bottom, right, top = textpage.get_rect(index)
            rect = [max(left, crop[0]), max(bottom, crop[1]), min(right, crop[2]), min(top, crop[3])]
            if rect[0] >= rect[2] or rect[1] >= rect[3]:
                continue
            corners = [converter.to_bitmap(x, y) for x in (rect[0], rect[2]) for y in (rect[1], rect[3])]
            box = [max(0, min(point[0] for point in corners)), max(0, min(point[1] for point in corners)),
                   min(bitmap.width, max(point[0] for point in corners)), min(bitmap.height, max(point[1] for point in corners))]
            if box[0] < box[2] and box[1] < box[3]:
                rectangles.append({"pdf": rect, "box": box})
        groups = []
        for rectangle in sorted(rectangles, key=lambda row: (row["box"][1], row["box"][0])):
            box = rectangle["box"]
            selected = None
            for group in reversed(groups[-30:]):
                previous = _union([row["box"] for row in group])
                height = min(box[3] - box[1], group[-1]["box"][3] - group[-1]["box"][1])
                overlap_x = min(box[2], previous[2]) - max(box[0], previous[0])
                overlap_y = min(box[3], previous[3]) - max(box[1], previous[1])
                gap_y = box[1] - previous[3]
                gap_x = max(box[0] - previous[2], previous[0] - box[2], 0)
                same_line = overlap_y >= height * .5 and gap_x <= height * .7
                paragraph = (0 <= gap_y <= height * .85 and overlap_x > .5 * min(box[2]-box[0], previous[2]-previous[0]))
                if (overlap_x > 0 and overlap_y > 0) or same_line or paragraph:
                    selected = group
                    break
            if selected is None:
                groups.append([rectangle])
            else:
                selected.append(rectangle)
        regions = []
        for group in groups:
            bounds = _union([row["pdf"] for row in group])
            text = textpage.get_text_bounded(left=bounds[0]-.01, bottom=bounds[1]-.01,
                                             right=bounds[2]+.01, top=bounds[3]+.01).replace("\r\n", "\n").strip()
            if not text:
                continue
            box = _union([row["box"] for row in group])
            box = [max(0, box[0]-2), max(0, box[1]-2), min(bitmap.width, box[2]+2), min(bitmap.height, box[3]+2)]
            heights = sorted(row["box"][3]-row["box"][1] for row in group)
            regions.append({"id": str(uuid.uuid4()), "box": box, "source_text": text, "target_text": "",
                            "status": "detected", "confidence_detection": None, "confidence_ocr": None,
                            "kind": "pdf_native_text", "auto_style": {
                                "version": 2, "font": "arial", "casing": "normal", "uppercase": False,
                                "fill": [0, 0, 0], "background": [255, 255, 255], "hollow": False,
                                "ink_height": heights[len(heights)//2], "line_count": max(1, len(text.splitlines())),
                                "line_colors": [[0, 0, 0]], "stretch": 1.}})
        return regions


def _render_scale(width, height):
    if not all(math.isfinite(value) and 0 < value <= 100000 for value in (width, height)):
        raise ValueError("Dimensions de page PDF invalides ou excessives.")
    scale = min(DPI / 72, math.sqrt(MAX_PIXELS / (width * height)), 100000 / width, 100000 / height)
    while math.ceil(width * scale) * math.ceil(height * scale) > MAX_PIXELS:
        scale *= .999
    return scale


def _close_pdf(document):
    with PDFIUM_LOCK:
        document.close()


def import_pdf(store, source, title=None, source_language="auto", target_language="fr"):
    from backend.projects.importer import staged_project
    pdfium = _pdfium()
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError("Fichier PDF introuvable.")
    if source.stat().st_size > MAX_TOTAL_BYTES:
        raise ValueError("Le PDF dépasse 2 Go.")
    manifest = new_manifest(title or source.stem, source_language, target_language)
    try:
        # ExitStack closes PDFium's source handle before staging cleanup tries
        # to remove the file (Windows refuses unlinking an open PDF).
        with staged_project(store, manifest) as staging, ExitStack() as cleanup:
            (staging / "original/source").mkdir()
            original = staging / "original/source/original.pdf"
            digest = hashlib.sha256()
            total = 0
            with source.open("rb") as incoming, original.open("wb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_TOTAL_BYTES:
                        raise ValueError("Le PDF dépasse 2 Go.")
                    digest.update(chunk)
                    outgoing.write(chunk)
            manifest["provenance"].update(source_format="pdf", source_name=source.name,
                                           source_file="original/source/original.pdf", source_sha256=digest.hexdigest())
            with PDFIUM_LOCK:
                document = pdfium.PdfDocument(str(original))
                cleanup.callback(_close_pdf, document)
                count = len(document)
            if not 1 <= count <= MAX_PAGES:
                raise ValueError("Le PDF doit contenir entre 1 et 1000 pages.")
            image_bytes = 0
            for index in range(count):
                with PDFIUM_LOCK:
                    with closing(document[index]) as page:
                        width_points, height_points = page.get_size()
                        scale = _render_scale(width_points, height_points)
                        with closing(page.render(scale=scale, rev_byteorder=True)) as bitmap:
                            regions = _native_regions(page, bitmap)
                            # Detach from PDFium-owned memory before releasing its bitmap.
                            image = bitmap.to_pil().convert("RGB")
                relative = f"original/{index+1:04d}.png"
                destination = staging / relative
                try:
                    image.save(destination, format="PNG", compress_level=2, dpi=(scale*72, scale*72))
                    width, height = image.size
                finally:
                    image.close()
                image_bytes += destination.stat().st_size
                if image_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("Les aperçus PDF dépassent 2 Go.")
                manifest["pages"].append({"id": str(uuid.uuid4()), "order": index, "name": f"Page {index+1}",
                                           "original": relative, "width": width, "height": height,
                                           "sha256": _sha256(destination), "regions": regions,
                                           "pdf_size_points": [width_points, height_points],
                                           "pdf_render_dpi": scale*72, "pdf_text_native": bool(regions)})
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == 4 or "password" in str(exc).lower():
            raise ValueError("PDF chiffré : retirez le mot de passe avant l’import.") from None
        raise ValueError("PDF illisible ou endommagé.") from None
    return manifest


def _page_size(page):
    size = page.get("pdf_size_points")
    if (isinstance(size, (list, tuple)) and len(size) == 2
            and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value > 0 for value in size)):
        return tuple(size)
    return page["width"] * 72 / DPI, page["height"] * 72 / DPI


@lru_cache(maxsize=32)
def _embedded_font(path):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont, TTFError
    if not path.is_file():
        return None
    name = "RenkaviPDF-" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    try:
        with _FONT_LOCK:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=0))
            return name, pdfmetrics.getFont(name).face.charToGlyph
    except (TTFError, OSError, ValueError):
        return None


def _text_font(text, warnings):
    # Arial covers Latin, Greek and Cyrillic; available CJK system fonts are
    # embedded when permitted by their TrueType format and local installation.
    candidates = [Path("C:/Windows/Fonts") / name for name in ("arial.ttf", "msyh.ttc", "malgun.ttf", "meiryo.ttc")]
    candidates += [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                   __import__('backend.runtime',fromlist=['resource_root']).resource_root() / "assets/fonts/ComicNeue-Regular.ttf"]
    for path in candidates:
        font = _embedded_font(path)
        if font and all(char.isspace() or ord(char) in font[1] for char in text):
            return font[0], text
    if all(ord(char) < 128 for char in text):
        return "Helvetica", text
    warning = "Couche de recherche : certains caractères ne disposent pas de police locale intégrable et sont remplacés par ?."
    if warning not in warnings:
        warnings.append(warning)
    return "Helvetica", text.encode("cp1252", errors="replace").decode("cp1252")


def _write_pdf(destination, project, sources, quality):
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen.canvas import Canvas
    warnings = []
    with destination.open("wb") as stream:
        writer = Canvas(stream, pageCompression=1)
        writer.setTitle(project["title"])
        writer.setSubject("Renkavi : pages traduites avec couche de recherche ; composition visuelle rasterisée.")
        for page, source in zip(project["pages"], sources):
            width, height = _page_size(page)
            writer.setPageSize((width, height))
            with Image.open(source) as image:
                image.load()
                if image.size != (page["width"], page["height"]):
                    raise ValueError(f"Dimensions de rendu incohérentes : {page['name']}")
                with image.convert("RGB") as rgb:
                    jpeg = io.BytesIO()
                    rgb.save(jpeg, format="JPEG", quality=quality, subsampling=0)
            writer.drawImage(ImageReader(jpeg), 0, 0, width=width, height=height)
            sx, sy = width / page["width"], height / page["height"]
            for region in page["regions"]:
                text = (region["source_text"] if region.get("status") == "ignored" or region.get("preserve_original")
                        else region["target_text"]).strip()
                if not text:
                    continue
                lines = text.splitlines()
                x1, y1, x2, y2 = region["box"]
                size = min(36, max(1, (y2-y1)*sy / max(1, len(lines)) * .8))
                text_object = writer.beginText(x1*sx, height-y1*sy-size)
                text_object.setTextRenderMode(3)  # Search/copy layer, never painted.
                text_object.setLeading(size*1.15)
                for line in lines:
                    font, prepared = _text_font(line, warnings)
                    text_object.setFont(font, size)
                    measured = pdfmetrics.stringWidth(prepared, font, size)
                    text_object.setHorizScale(min(100, 100*(x2-x1)*sx/max(.01, measured)))
                    text_object.textLine(prepared)
                writer.drawText(text_object)
            writer.showPage()
        if warnings:
            writer.setKeywords("; ".join(warnings))
        writer.save()
        stream.flush()
        os.fsync(stream.fileno())
    return warnings


def _valid_pdf(path, page_count):
    pdfium = _pdfium()
    try:
        with PDFIUM_LOCK:
            with pdfium.PdfDocument(str(path)) as document:
                return len(document) == page_count
    except (pdfium.PdfiumError, OSError, ValueError):
        return False


def export_pdf(store, chapter_id, options=None):
    from backend.ai.pipeline import output_file
    options = {} if options is None else options
    if not isinstance(options, dict):
        raise ValueError("Options d’export PDF invalides.")
    quality = options.get("quality", 92)
    if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
        raise ValueError("Qualité PDF entre 1 et 100.")
    with _export_lock(store, chapter_id):
        with tempfile.TemporaryDirectory(prefix=".renkavi-pdf-", dir=store.data_dir) as temporary:
            temporary = Path(temporary)
            with store._lock:
                project = store.load(chapter_id)
                if not project["pages"]:
                    raise ValueError("Le document ne contient aucune page.")
                root = store.project_path(chapter_id)
                state = _chapter_recipe(project)
                sizes = [_page_size(page) for page in project["pages"]]
                sources, live = [], []
                for index, page in enumerate(project["pages"]):
                    file = output_file(store, chapter_id, page["id"], "rendered")
                    if any(region.get("status") != "ignored" and not region.get("preserve_original")
                           and region["source_text"].strip() and not region["target_text"].strip() for region in page["regions"]):
                        raise ValueError(f"Traduction incomplète : {page['name']}.")
                    snapshot = temporary / f"page-{index}.png"
                    try:
                        os.link(file, snapshot)
                    except OSError:
                        shutil.copyfile(file, snapshot)
                    sources.append(snapshot)
                    live.append(file)
                directory = folder(root, "export")
                directory.mkdir(exist_ok=True)
                destination = directory / f"document_{project['target_language']}_q{quality}.pdf"
                metadata = destination.with_suffix(".pdf.json")
            hashes = [_sha256(file) for file in sources]
            recipe = {"version": 1, **state, "sizes": [list(size) for size in sizes], "quality": quality, "sources": hashes}

            def validate_current():
                current = store.load(chapter_id)
                if (current["revision"] != project["revision"] or _chapter_recipe(current) != state
                        or store.project_path(chapter_id) != root or [_page_size(page) for page in current["pages"]] != sizes):
                    raise RevisionConflict("Le document a changé pendant l’export PDF. Relancez l’export.")
                for page in current["pages"]:
                    output_file(store, chapter_id, page["id"], "rendered")
                if [_sha256(file) for file in live] != hashes:
                    raise RevisionConflict("Le rendu a changé pendant l’export PDF. Relancez l’export.")

            cached = False
            try:
                data = json.loads(metadata.read_text(encoding="utf-8"))
                cached = (data["recipe"] == recipe and destination.stat().st_size == data["size"]
                          and _sha256(destination) == data["sha256"] and _valid_pdf(destination, len(sources)))
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if cached:
                with store._lock:
                    validate_current()
                return destination
            partial = temporary / "document.pdf"
            warnings = _write_pdf(partial, project, sources, quality)
            if not _valid_pdf(partial, len(sources)):
                raise ValueError("Le PDF exporté est incomplet ou illisible.")
            digest, size = _sha256(partial), partial.stat().st_size
            with store._lock:
                validate_current()
                os.replace(partial, destination)
                atomic_json(metadata, {"recipe": recipe, "size": size, "sha256": digest, "warnings": warnings})
            return destination
