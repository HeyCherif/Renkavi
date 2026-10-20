"""Small, validated application preferences stored with the user's library."""
from __future__ import annotations
import copy
import json
import threading
from backend.projects.store import atomic_json

DEFAULTS={'ui_language':'fr','translation_mode':'local','export':{'format':'webp','quality':92}}


class Settings:
    def __init__(self,data_dir):
        self.path=data_dir/'settings.json'
        self.lock=threading.RLock()

    def get(self):
        with self.lock:
            data=copy.deepcopy(DEFAULTS)
            if self.path.is_file():
                stored=json.loads(self.path.read_text(encoding='utf-8'))
                data.update(stored)
                data['export']={**DEFAULTS['export'],**stored.get('export',{})}
            else:stored={}
            # An installer may stage the initial mode while an older application
            # is still open. Explicit preferences saved later take precedence.
            staged=self.path.with_name('translation-default.json')
            if 'translation_mode' not in stored and staged.is_file():
                data['translation_mode']=json.loads(staged.read_text(encoding='utf-8'))['translation_mode']
            return self.validate(data)

    @staticmethod
    def validate(data):
        if not isinstance(data,dict) or set(data)-set(DEFAULTS):raise ValueError('Paramètres inconnus.')
        if data.get('ui_language') not in {'fr','en'}:raise ValueError('Langue de l’interface : français ou anglais.')
        if data.get('translation_mode','local') not in {'local','chatgpt'}:raise ValueError('Mode de traduction invalide.')
        export=data.get('export')
        if not isinstance(export,dict) or set(export)-{'format','quality'} or export.get('format') not in {'webp','jpeg','png'}:
            raise ValueError('Format export invalide : WebP, JPEG ou PNG.')
        quality=export.get('quality')
        if isinstance(quality,bool) or not isinstance(quality,int) or not 1<=quality<=100:raise ValueError('Qualité export entre 1 et 100.')
        return copy.deepcopy(data)

    def save(self,changes):
        with self.lock:
            if not isinstance(changes,dict):raise ValueError('Paramètres invalides.')
            data=self.get()
            data.update(changes)
            if 'export' in changes and isinstance(changes['export'],dict):data['export']={**self.get()['export'],**changes['export']}
            data=self.validate(data)
            atomic_json(self.path,data)
            return data
