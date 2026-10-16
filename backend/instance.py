"""Prevent two launcher processes from operating on the same local library."""
from __future__ import annotations
import json
import os
from pathlib import Path
from backend.projects.store import atomic_json


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self,directory):
        self.directory=Path(directory).expanduser().resolve()
        self.file=None

    def __enter__(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        self.file=(self.directory/'.instance.lock').open('a+b')
        self.file.seek(0,os.SEEK_END)
        if self.file.tell()==0:self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();self.file=None
            raise AlreadyRunning('Renkavi utilise déjà cette bibliothèque.') from None
        return self

    def set_url(self,url):
        atomic_json(self.directory/'.instance.json',{'url':url,'pid':os.getpid()})

    def __exit__(self,*args):
        (self.directory/'.instance.json').unlink(missing_ok=True)
        if self.file:
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_UN)
            self.file.close();self.file=None


def existing_url(directory):
    from urllib.parse import urlsplit
    try:
        data=json.loads((Path(directory).expanduser().resolve()/'.instance.json').read_text(encoding='utf-8'))
        url=data['url'];parsed=urlsplit(url)
        if parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port and not parsed.username:return url
    except (OSError,ValueError,KeyError,TypeError):pass
    return None
