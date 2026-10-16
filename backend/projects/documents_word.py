"""Structured DOCX translation with an explicitly approximate page preview."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
import zipfile
from backend.projects.layout import folder
from pathlib import Path, PurePosixPath

from lxml import etree
from PIL import Image, ImageDraw, ImageFont

from backend.core.project import new_manifest

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT_NS = "http://purl.oclc.org/ooxml/wordprocessingml/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
SOURCE_FILE = "original/source/original.docx"
LEGACY_SOURCE_FILE = "source/original.docx"
PREVIEW_SIZE = (1200, 1600)
CHUNK_CHARS = 800
MAX_SOURCE_BYTES = 250 * 1024**2
MAX_UNCOMPRESSED_BYTES = 512 * 1024**2
MAX_MEMBER_BYTES = 128 * 1024**2
MAX_XML_BYTES = 20 * 1024**2
MAX_TEXT_CHARS = 2_000_000
MAX_PAGES = 1000


def _members(archive):
    members = archive.infolist()
    if len(members) > 10000:
        raise ValueError("Le DOCX contient trop de fichiers internes.")
    names, total = set(), 0
    for member in members:
        name = member.filename
        parts = PurePosixPath(name).parts
        if (name.startswith("/") or "\\" in name or not parts or re.match(r"^[A-Za-z]:", name)
            or any(part in {".", ".."} for part in name.split("/"))
            or name.casefold() in names or (member.external_attr >> 16) & 0o170000 == 0o120000):
            raise ValueError("Chemin interne DOCX dangereux ou dupliqué.")
        names.add(name.casefold())
        if member.flag_bits & 1:
            raise ValueError("Les documents Word chiffrés ne sont pas pris en charge.")
        total += member.file_size
        if (member.file_size > MAX_MEMBER_BYTES or total > MAX_UNCOMPRESSED_BYTES
            or member.file_size / max(1, member.compress_size) > 1000):
            raise ValueError("Le DOCX dépasse les limites de décompression.")
        if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError("Compression DOCX non prise en charge.")
    if "word/document.xml" not in {member.filename for member in members} or "[Content_Types].xml" not in {member.filename for member in members}:
        raise ValueError("Ce fichier n'est pas un document DOCX valide.")
    return members


def _xml(data):
    if len(data) > MAX_XML_BYTES:
        raise ValueError("Une partie XML Word est trop volumineuse.")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_blank_text=False)
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError("Une partie XML du document Word est illisible.") from exc
    if root.getroottree().docinfo.doctype or any(isinstance(node, etree._Entity) for node in root.iter()):
        raise ValueError("Les déclarations DTD et entités XML ne sont pas acceptées dans un DOCX.")
    return root


def _parts(archive, members):
    names = {member.filename for member in members}
    selected = ["word/document.xml"] + sorted(name for name in names if re.fullmatch(
        r"word/(?:header\d*|footer\d*|footnotes|endnotes)\.xml", name))
    return [(name, _xml(archive.read(name))) for name in selected]


def _tag(node):
    if not isinstance(node.tag, str):
        return ""
    qname = etree.QName(node)
    return qname.localname if qname.namespace in {WORD_NS, STRICT_NS} else ""


def _paragraph_segments(root):
    """Every text node belongs to its nearest paragraph, including text boxes.

    Tabs, breaks and field values remain native OOXML objects. Splitting there
    keeps translations from swallowing them or moving their neighboring text.
    """
    paragraphs = [node for node in root.iter() if _tag(node) == "p"]
    indexed = {paragraph: index for index, paragraph in enumerate(paragraphs)}
    segments = [[] for _ in paragraphs]
    current = {}
    field_depth = 0
    for node in root.iter():
        tag = _tag(node)
        if tag == "fldChar":
            field_type = node.get(f"{{{etree.QName(node).namespace}}}fldCharType")
            if field_type == "begin":
                field_depth += 1
            elif field_type == "end":
                field_depth = max(0, field_depth - 1)
        if tag not in {"t", "tab", "br", "cr", "fldChar", "instrText"}:
            continue
        ancestors = list(node.iterancestors())
        paragraph = next((ancestor for ancestor in ancestors if ancestor in indexed), None)
        if paragraph is None:
            continue
        index = indexed[paragraph]
        excluded = field_depth or any(_tag(ancestor) in {"fldSimple", "del"} for ancestor in ancestors)
        if tag != "t" or excluded:
            current.pop(index, None)
            continue
        if index not in current:
            current[index] = []
            segments[index].append(current[index])
        current[index].append(node)
    return [(index, segment_index, nodes) for index, groups in enumerate(segments)
            for segment_index, nodes in enumerate(groups) if "".join(node.text or "" for node in nodes).strip()]


def _chunks(text):
    result = []
    while text:
        boundary = min(len(text), CHUNK_CHARS)
        if len(text) > boundary:
            boundary = max((match.end() for match in re.finditer(r"\s+", text[:boundary])
                            if match.end() >= CHUNK_CHARS // 2), default=boundary)
        newlines = list(re.finditer(r"[\r\n]", text[:boundary]))
        if len(newlines) > 30:
            boundary = min(boundary, newlines[29].end())
        result.append(text[:boundary])
        text = text[boundary:]
    return result


def _segments(parts):
    total = 0
    for part, root in parts:
        for paragraph_index, segment_index, nodes in _paragraph_segments(root):
            text = "".join(node.text or "" for node in nodes)
            total += len(text)
            if total > MAX_TEXT_CHARS:
                raise ValueError("Le document Word contient trop de texte.")
            yield (part, paragraph_index, segment_index), nodes, _chunks(text)


def _font():
    for filename in ("C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(filename, 28)
        except OSError:
            pass
    return ImageFont.load_default(size=28)


def _wrap(text, font, width=1060):
    # Character wrapping also handles CJK and long unbroken strings.
    lines, line = [], ""
    for character in text:
        if character in "\r\n":
            lines.append(line)
            line = ""
        elif line and font.getlength(line + character) > width:
            lines.append(line)
            line = character
        else:
            line += character
    lines.append(line)
    return lines


def _previews(staging, manifest, entries):
    font = _font()
    page, draw, image, y = None, None, None, 110

    def finish():
        if page is None:
            return
        file = staging / page["original"]
        image.save(file, format="PNG")
        with file.open("rb") as source:
            page["sha256"] = hashlib.file_digest(source, "sha256").hexdigest()

    for reference, nodes, chunks in entries:
        for chunk_index, text in enumerate(chunks):
            lines = _wrap(text, font)
            height = max(70, len(lines) * 36 + 24)
            if height > 1400:
                raise ValueError("Un bloc Word est trop grand pour l'aperçu simplifié.")
            if page is None or y + height > 1530:
                finish()
                if len(manifest["pages"]) >= MAX_PAGES:
                    raise ValueError("L'aperçu Word dépasse 1000 pages.")
                order = len(manifest["pages"])
                page = dict(id=str(uuid.uuid4()), order=order, name=f"Aperçu Word {order+1}", original=f"original/{order+1:04d}.png",
                            width=1200, height=1600, sha256="", regions=[])
                manifest["pages"].append(page)
                image = Image.new("RGB", PREVIEW_SIZE, "white")
                draw = ImageDraw.Draw(image)
                draw.text((60, 25), f"Word — aperçu simplifié {order+1}", font=font, fill=(90, 90, 90))
                draw.line((60, 80, 1140, 80), fill=(210, 210, 210), width=2)
                y = 110
            region = dict(id=str(uuid.uuid4()), box=[60, y, 1140, y+height], source_text=text, target_text="", status="detected" if text.strip() else "ignored",
                          confidence_detection=None, confidence_ocr=None,
                          document_ref=dict(part=reference[0], paragraph_index=reference[1], segment_index=reference[2], chunk_index=chunk_index,
                                            in_table=any(_tag(a) == "tc" for a in nodes[0].iterancestors())),
                          auto_style=dict(font="arial", casing="normal", uppercase=False, fill=[0, 0, 0], ink_height=28, line_count=len(lines)))
            page["regions"].append(region)
            for line_number, line in enumerate(lines):
                draw.text((68, y+10+line_number*36), line, font=font, fill="black")
            y += height + 20
    finish()


def import_docx(store, source, title=None, source_language="auto", target_language="fr"):
    from backend.projects.importer import staged_project

    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError("Document Word introuvable.")
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Le document Word dépasse 250 Mo.")
    manifest = new_manifest(title or source.stem, source_language, target_language)
    with staged_project(store, manifest) as staging:
        copied = staging / SOURCE_FILE
        copied.parent.mkdir()
        digest, total = hashlib.sha256(), 0
        with source.open("rb") as input_file, copied.open("wb") as output:
            while chunk := input_file.read(1024*1024):
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("Le document Word dépasse 250 Mo.")
                digest.update(chunk)
                output.write(chunk)
        try:
            with zipfile.ZipFile(copied) as archive:
                members = _members(archive)
                parts = _parts(archive, members)
                entries = list(_segments(parts))
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise ValueError("Document DOCX illisible ou chiffré.") from exc
        if not entries:
            raise ValueError("Ce DOCX ne contient aucun texte exploitable. Les documents Word constitués uniquement d'images nécessitent un export PDF ou des images pour l'OCR.")
        manifest["provenance"] = dict(source_format="docx", source_name=source.name, source_file=SOURCE_FILE,
                                      source_sha256=digest.hexdigest(), source_size=total, document_mode="structured",
                                      document_version=1, document_preview="approximate", document_chunk_chars=CHUNK_CHARS,
                                      document_parts=[part for part, _ in parts])
        _previews(staging, manifest, entries)
    return manifest


def clean_preview(image, page):
    cleaned = image.convert("RGB").copy()
    mask = Image.new("L", image.size)
    drawing, mask_drawing = ImageDraw.Draw(cleaned), ImageDraw.Draw(mask)
    count = 0
    for region in page["regions"]:
        if region["status"] == "ignored" or region.get("preserve_original"):
            continue
        x1, y1, x2, y2 = map(int, region["box"])
        box = (x1, y1, x2-1, y2-1)
        drawing.rectangle(box, fill="white")
        mask_drawing.rectangle(box, fill=255)
        count += 1
    return cleaned, mask, {"flat": count, "structured_preview": True}


def _restore_edge_whitespace(source, translated):
    leading = re.match(r"\s*", source).group()
    trailing = re.search(r"\s*$", source).group()
    return leading + translated.strip() + trailing


def _replace_text(nodes, text):
    lengths = [len(node.text or "") for node in nodes]
    total = sum(lengths)
    cumulative, previous = 0, 0
    boundaries = [match.end() for match in re.finditer(r"\s+", text)] + [len(text)]
    for index, (node, length) in enumerate(zip(nodes, lengths)):
        cumulative += length
        if index == len(nodes)-1:
            end = len(text)
        else:
            ideal = round(len(text) * cumulative / max(1, total))
            end = min((boundary for boundary in boundaries if boundary >= previous), key=lambda boundary: abs(boundary-ideal), default=previous)
        node.text = text[previous:end]
        if node.text and (node.text[0].isspace() or node.text[-1].isspace()):
            node.set(XML_SPACE, "preserve")
        previous = end



def _protect_table_grids(tables):
    """Prevent translated text from expanding original DOCX table columns.

    Retain every tblGrid/gridCol, tcW, margin, border, alignment and run style.
    Only stabilize layout for tables with explicit grid widths and substantial
    text growth. Word can still increase row heights for longer translations.
    """
    changed = 0
    for table in tables:
        grid = next((child for child in table if _tag(child) == "tblGrid"), None)
        if grid is None or not any(_tag(col) == "gridCol" and any(attr.endswith("}w") for attr in col.attrib) for col in grid):
            continue
        props = next((child for child in table if _tag(child) == "tblPr"), None)
        namespace = etree.QName(table).namespace
        if props is None:
            props = etree.Element(f"{{{namespace}}}tblPr")
            table.insert(0, props)
        layout = next((child for child in props if _tag(child) == "tblLayout"), None)
        if layout is None:
            layout = etree.SubElement(props, f"{{{namespace}}}tblLayout")
        if layout.get(f"{{{namespace}}}type") != "fixed":
            layout.set(f"{{{namespace}}}type", "fixed")
            changed += 1
    return changed

def export_docx(store, chapter_id, options=None):
    from backend.ai.pipeline import output_file

    with store._lock:
        project = store.load(chapter_id)
        provenance = project.get("provenance", {})
        if (provenance.get("source_format") != "docx" or provenance.get("source_file") not in {SOURCE_FILE, LEGACY_SOURCE_FILE}
            or provenance.get("document_version") != 1 or provenance.get("document_chunk_chars") != CHUNK_CHARS):
            raise ValueError("Le fichier source de ce chapitre n'est pas un DOCX structuré compatible.")
        root = store.project_path(chapter_id).resolve()
        source = (root / provenance["source_file"]).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError("Source Word absente ou chemin invalide.")
        with source.open("rb") as input_file:
            if hashlib.file_digest(input_file, "sha256").hexdigest() != provenance.get("source_sha256"):
                raise ValueError("Le document Word original a été modifié ; export refusé.")
        regions = {}
        for page in project["pages"]:
            output_file(store, chapter_id, page["id"], "rendered")
            for region in page["regions"]:
                reference = region.get("document_ref", {})
                if not isinstance(reference, dict):
                    raise ValueError("Correspondance de texte Word invalide.")
                key = tuple(reference.get(field) for field in ("part", "paragraph_index", "segment_index", "chunk_index"))
                if (not isinstance(key[0], str) or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in key[1:])
                    or key in regions):
                    raise ValueError("Correspondance de texte Word absente ou dupliquée.")
                if region["status"] != "ignored" and not region.get("preserve_original") and not region["target_text"].strip():
                    raise ValueError("Traduction Word incomplète : une zone n'est pas traduite.")
                regions[key] = region
        with zipfile.ZipFile(source) as archive:
            members = _members(archive)
            parts = _parts(archive, members)
            expected = set()
            changed_parts = set()
            expanded_tables = set()
            for reference, nodes, chunks in _segments(parts):
                result, modified = [], False
                for index, text in enumerate(chunks):
                    key = (*reference, index)
                    expected.add(key)
                    region = regions.get(key)
                    if region is None:
                        raise ValueError("Correspondance Word incomplète : une zone source est absente.")
                    if region["status"] == "ignored" or region.get("preserve_original"):
                        result.append(text)
                    else:
                        result.append(_restore_edge_whitespace(text, region["target_text"]))
                        modified = True
                if modified:
                    translated_text="".join(result)
                    source_text="".join(chunks)
                    if len(source_text.strip()) >= 8 and len(translated_text.strip()) > len(source_text.strip()) * 1.18:
                        for ancestor in nodes[0].iterancestors():
                            if _tag(ancestor) == "tbl":
                                expanded_tables.add(ancestor)
                                break
                    _replace_text(nodes, translated_text)
                    changed_parts.add(reference[0])
            _protect_table_grids(expanded_tables)
            if expected != set(regions):
                raise ValueError("Correspondance Word inconnue dans les corrections.")
            replacements = {part: etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
                            for part, tree in parts if part in changed_parts}
            directory = folder(root, "export")
            directory.mkdir(exist_ok=True)
            stem = re.sub(r"[^\w.-]+", "_", Path(provenance.get("source_name", "document.docx")).stem).strip(".")[:80] or "document"
            destination = directory / f"{stem}_{project['target_language']}.docx"
            partial = directory / f".{destination.name}.{uuid.uuid4().hex}.tmp"
            try:
                with partial.open("wb") as output:
                    with zipfile.ZipFile(output, "w") as translated:
                        translated.comment = archive.comment
                        for member in members:
                            if member.filename in replacements:
                                translated.writestr(member, replacements[member.filename])
                            else:
                                with archive.open(member) as input_member, translated.open(member, "w") as output_member:
                                    shutil.copyfileobj(input_member, output_member, length=1024*1024)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(partial, destination)
            finally:
                partial.unlink(missing_ok=True)
        return destination
