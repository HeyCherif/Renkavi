"""Reentrant catalogue lock shared by threads and independent app processes."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path


class CatalogLock:
    def __init__(self, path, timeout=2.):
        self.path = Path(path)
        self.timeout = timeout
        self._thread_lock = threading.RLock()
        self._local = threading.local()

    @staticmethod
    def _occupied():
        return ValueError("Le catalogue de modèles est occupé par une autre instance. Réessayez après la fin de son opération.")

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        if not self._thread_lock.acquire(timeout=self.timeout):
            raise self._occupied()
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            return self
        file = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            file = self.path.open("a+b")
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise self._occupied() from None
                    time.sleep(min(.025, remaining))
            self._local.file = file
            self._local.depth = 1
            return self
        except BaseException:
            if file is not None:
                file.close()
            self._thread_lock.release()
            raise

    def __exit__(self, *args):
        self._local.depth -= 1
        try:
            if not self._local.depth:
                file = self._local.file
                try:
                    file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(file, fcntl.LOCK_UN)
                finally:
                    file.close()
                    self._local.file = None
        finally:
            self._thread_lock.release()
