"""Recognize supported file contents and describe their native export format."""
from __future__ import annotations
import zipfile
from pathlib import Path
from PIL import Image,UnidentifiedImageError

IMAGE_FORMATS={'png':'png','jpeg':'jpg','webp':'webp','bmp':'bmp'}
SUPPORTED_EXTENSIONS={'.png','.jpg','.jpeg','.webp','.bmp','.cbz','.zip','.pdf','.docx','.doc'}


def detect_source(source):
    source=Path(source)
    if source.is_dir():return 'images'
    with source.open('rb') as stream:header=stream.read(1024)
    if header.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        raise ValueError('Ce document Word ancien (.doc) ou chiffré doit être enregistré en .docx non chiffré avant import.')
    if b'%PDF-' in header[:1024]:return 'pdf'
    if header.startswith((b'PK\x03\x04',b'PK\x05\x06',b'PK\x07\x08')):
        with zipfile.ZipFile(source) as archive:
            names=set(archive.namelist())
            if 'word/document.xml' in names and '[Content_Types].xml' in names:return 'docx'
        if source.suffix.lower()=='.docx':raise ValueError('Document DOCX invalide : le contenu Word est absent.')
        return 'cbz' if source.suffix.lower()=='.cbz' else 'zip'
    try:
        with Image.open(source) as image:
            kind=(image.format or '').lower()
            if kind not in IMAGE_FORMATS:raise ValueError('Format image non pris en charge : utilisez PNG, JPEG, WebP ou BMP.')
            return kind
    except (UnidentifiedImageError,OSError) as exc:
        raise ValueError('Fichier non reconnu. Formats acceptés : PDF, DOCX, PNG, JPEG, WebP, BMP, CBZ et ZIP d’images.') from exc


def source_format(project):
    kind=project.get('provenance',{}).get('source_format')
    if kind in {*IMAGE_FORMATS,'pdf','docx','cbz','zip','images'}:return kind
    # Older manifests always exported CBZ; a one-page archive is ambiguous.
    return 'cbz'


def export_extension(project):
    kind=source_format(project)
    return IMAGE_FORMATS.get(kind,'cbz' if kind=='images' else kind)
