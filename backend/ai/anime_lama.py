"""Adapter for an already-installed CUDA PyTorch Python 3.12 environment.

The Renkavi API runs Python 3.14, but the user's DubRoom has PyTorch CUDA
in its Python 3.12 virtualenv. Keep dependencies isolated and avoid downloads.
"""
from __future__ import annotations

import atexit
import base64
import io
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from backend.runtime import resource_root, models_root

ROOT = resource_root()
def worker_python():
    configured=os.environ.get('RENKAVI_CUDA_PYTHON')
    if configured:return Path(configured)
    # A portable CUDA runtime can be provisioned beside the user's model cache.
    portable=models_root()/'cuda-runtime'/'Scripts'/'python.exe'
    if portable.is_file():return portable
    return Path(r"D:\manhwa studio\anime_manga_manhua_dubbing_app\data\environments\tts-omnivoice-hq\venv\Scripts\python.exe")

WORKER_PYTHON = worker_python()
STARTUP_LOCK = threading.RLock()


def worker_available() -> bool:
    return WORKER_PYTHON.is_file() and (ROOT / "backend" / "ai" / "vendor" / "lama_ffc.py").is_file()


def pack(array, mode):
    buf = io.BytesIO()
    Image.fromarray(array,mode=mode).save(buf,format="PNG",compress_level=1)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class AnimeLama:
    def __init__(self, checkpoint: Path):
        if not worker_available():
            raise RuntimeError("Environnement CUDA DubRoom introuvable : vérifie le chemin du Python worker")
        self.lock = threading.RLock()
        log_dir=models_root()
        log_dir.mkdir(parents=True,exist_ok=True)
        self.stderr = (log_dir / "anime-lama-worker.log").open("a", encoding="utf-8")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                [str(WORKER_PYTHON), "-u", "-m", "backend.ai.anime_lama_worker", str(checkpoint)],
                cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.stderr, creationflags=creationflags)
        except Exception:
            self.close()
            raise
        atexit.register(self.close)
        try:
            hello = self._read_response(180)
        except Exception:
            self.close()
            raise
        if not hello.get("ready"):
            self.close()
            raise RuntimeError(hello.get("error","Le worker CUDA n'est pas prêt"))
        self.device = hello.get("device", "CUDA")

    def _read_response(self, timeout, request=None):
        """Bound pipe reads on Windows, where select cannot wait on subprocess pipes."""
        responses = queue.Queue(maxsize=1)
        output = self.process.stdout
        input_pipe = self.process.stdin if request is not None else None
        def read():
            try:
                if request is not None:
                    input_pipe.write(request)
                    input_pipe.flush()
                responses.put(output.readline())
            except Exception as exc:responses.put(exc)
        threading.Thread(target=read, daemon=True).start()
        try:line=responses.get(timeout=timeout)
        except queue.Empty:
            # Kill before closing buffered stdin: a blocked writer holds its
            # stream lock and close() would otherwise wait indefinitely too.
            self.process.kill()
            self.process.wait(timeout=3)
            raise RuntimeError(f"Anime-Manga LaMa : délai de réponse dépassé ({timeout:g} s). Voir models/anime-lama-worker.log.") from None
        if isinstance(line,Exception):raise RuntimeError(f"Lecture du worker LaMa échouée : {line}") from line
        if not line:
            raise RuntimeError("Anime-Manga LaMa : worker arrêté sans réponse (voir models/anime-lama-worker.log)")
        try:
            result=json.loads(line)
            if not isinstance(result,dict):raise ValueError('Object expected')
            return result
        except (ValueError,UnicodeError) as exc:
            raise RuntimeError("Réponse du worker LaMa invalide (voir models/anime-lama-worker.log)") from exc

    def close(self):
        atexit.unregister(self.close)
        if getattr(self,"process",None) is not None:
            try:
                if self.process.poll() is None:
                    self.process.stdin.close()
                    self.process.wait(timeout=3)
            except Exception:
                self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            for stream in ('stdin','stdout'):
                pipe=getattr(self.process,stream,None)
                if pipe is not None:
                    try:pipe.close()
                    except OSError:pass
            self.process = None
        if getattr(self,"stderr",None) and not self.stderr.closed:
            self.stderr.close()

    def inpaint(self, rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if rgb.ndim != 3 or rgb.shape[2] != 3 or mask.shape != rgb.shape[:2]:
            raise ValueError("Image RGB ou masque LaMa invalide.")
        if not mask.any():
            return rgb.copy()
        with self.lock:
            if not self.process or self.process.poll() is not None:
                raise RuntimeError("Le moteur CUDA Anime-Manga LaMa s'est arrêté.")
            request = {"image":pack(np.ascontiguousarray(rgb),"RGB"),
                       "mask":pack(np.ascontiguousarray(mask),"L")}
            data = (json.dumps(request,separators=(",",":"))+"\n").encode("ascii")
            try:output = self._read_response(180, data)
            except Exception:
                self.close()
                raise
            if not output.get("ok"):
                raise RuntimeError(output.get("error","Inpainting CUDA échoué"))
            with Image.open(io.BytesIO(base64.b64decode(output["image"],validate=True))) as image:
                restored=np.asarray(image.convert("RGB"))
            if restored.shape != rgb.shape:
                raise RuntimeError("Le worker LaMa a retourné une image de mauvaises dimensions.")
            return np.where(mask[...,None]>0,restored,rgb).astype(np.uint8)
