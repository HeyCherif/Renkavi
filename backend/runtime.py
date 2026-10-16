"""Locate immutable application resources and writable user state."""
from __future__ import annotations
import os
import sys
from pathlib import Path


def is_frozen():
    return bool(getattr(sys,'frozen',False))


def resource_root():
    return Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1])).resolve()


def user_root():
    override=os.environ.get('RENKAVI_HOME')
    if override:return Path(override).expanduser().resolve()
    if not is_frozen():return resource_root()
    base=Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local')) if os.name=='nt' else Path(os.environ.get('XDG_DATA_HOME',Path.home()/'.local/share'))
    return (base/'Renkavi').resolve()


def models_root():
    override=os.environ.get('RENKAVI_MODELS_DIR')
    return Path(override).expanduser().resolve() if override else user_root()/'models'


def default_data_dir():
    override=os.environ.get('RENKAVI_DATA_DIR')
    if override:return Path(override).expanduser().resolve()
    root=user_root()
    # Reuse the historical AppData library instead of showing an empty one.
    return root if is_frozen() and (root/'projects').is_dir() else root/'data'


def custom_fonts_root():
    return user_root()/'fonts' if is_frozen() else resource_root()/'assets/fonts/custom'
