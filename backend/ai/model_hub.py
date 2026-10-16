"""Curated model downloads. Listing never fetches weights or changes active models.

File identities below were verified against Hugging Face's model API with
``?blobs=true`` on 2026-10-15. Immutable revisions and LFS SHA-256 hashes prevent
upstream changes from silently replacing the reviewed artifacts.
The OCR entries use RapidAI's versioned ModelScope URLs and matching SHA-256.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from backend.ai import registry
from backend.core.project import utc_now
from backend.projects.store import atomic_json

MAX_DOWNLOAD_BYTES = 20 * 1024**3
CHUNK_BYTES = 1024 * 1024
MODELSCOPE_HOSTS = frozenset({'www.modelscope.cn','cdn-lfs-cn-1.modelscope.cn'})
OCR_FILENAMES = {'PP-OCRv6_det_small.onnx':'det','PP-OCRv6_rec_small.onnx':'rec'}


def _weight(model_id, name, kind, repo, revision, filename, size, sha256, license, **extra):
    return dict(id=model_id, name=name, kind=kind, repository=repo, revision=revision,
                filename=filename, size_bytes=size, sha256=sha256, license=license,
                model_card=f"https://huggingface.co/{repo}",
                source_url=f"https://huggingface.co/{repo}/resolve/{revision}/{filename}",
                downloadable=True, **extra)


def _ocr_weight(part, size, sha256):
    filename=f'PP-OCRv6_{part}_small.onnx'
    return dict(id=f'rapidocr-ppocrv6-{part}-small',name=f'PP-OCRv6 small · {"détection" if part=="det" else "reconnaissance"}',
                kind='ocr',repository='RapidAI/RapidOCR',revision='v3.9.2',filename=filename,
                size_bytes=size,sha256=sha256,downloadable=True,install_path='ocr/'+filename,
                source_url=f'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/{part}/{filename}',
                model_card='https://github.com/RapidAI/RapidOCR',license='Apache-2.0',
                license_url='https://rapidai.github.io/RapidOCRDocs/main/en/model_licenses/',runtime='RapidOCR / ONNX Runtime')


CURATED_MODELS = [
    # Canonical files and upstream SHA256 cross-checked against RapidOCR's
    # default_models.yaml and ModelScope v3.9.2 X-Linked-Etag on 2026-10-16.
    _ocr_weight('det',9929594,'090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f'),
    _ocr_weight('rec',21234383,'6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884'),
    _weight("hy-mt2-1-8b-q4", "HY-MT2 1.8B · Q4_K_M", "translation", "tencent/Hy-MT2-1.8B-GGUF",
            "a0c709d9fac510f2c807aa3af52872340dc37a4a", "Hy-MT2-1.8B-Q4_K_M.gguf", 1133080448,
            "dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699", "Apache-2.0",
            license_url="https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF", runtime="LM Studio", profile="hy_mt"),
    _weight("hy-mt2-1-8b-q8", "HY-MT2 1.8B · Q8_0", "translation", "tencent/Hy-MT2-1.8B-GGUF",
            "a0c709d9fac510f2c807aa3af52872340dc37a4a", "Hy-MT2-1.8B-Q8_0.gguf", 1908528192,
            "5c3fe0b1408a5ceb0143184ef247b11b579c525f4b02b060e6c851bb76fef1a4", "Apache-2.0",
            license_url="https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF", runtime="LM Studio", profile="hy_mt"),
    _weight("translategemma-4b-q4", "TranslateGemma 4B IT · Q4_K_M", "translation", "mradermacher/translategemma-4b-it-GGUF",
            "d37868f759d2f817a43d28dd80cf9f982ffa62fd", "translategemma-4b-it.Q4_K_M.gguf", 2489909760,
            "81200d03e843d2ec1ece6eeafe7d13cb6e5211e1fcd336ade55790b683a08330", "Gemma",
            license_url="https://ai.google.dev/gemma/terms", original_model="https://huggingface.co/google/translategemma-4b-it", runtime="LM Studio", profile="translategemma"),
    _weight("translategemma-4b-q8", "TranslateGemma 4B IT · Q8_0", "translation", "mradermacher/translategemma-4b-it-GGUF",
            "d37868f759d2f817a43d28dd80cf9f982ffa62fd", "translategemma-4b-it.Q8_0.gguf", 4130417920,
            "f5006ddf207becaace1668113a61b31cf8b2e03f0f0baa0d1ca37eb8371d4bc1", "Gemma",
            license_url="https://ai.google.dev/gemma/terms", original_model="https://huggingface.co/google/translategemma-4b-it", runtime="LM Studio", profile="translategemma"),
    _weight("lama-onnx-fp32", "LaMa · ONNX FP32", "inpainting", "Carve/LaMa-ONNX",
            "c3c0c9e468934d62e79c329e35d82dd09ff8c444", "lama_fp32.onnx", 208044816,
            "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6", "Apache-2.0",
            license_url="https://github.com/advimman/lama/blob/main/LICENSE", runtime="ONNX Runtime"),
    _weight("migan-onnx-pipeline", "MI-GAN · ONNX Pipeline v2", "inpainting", "andraniksargsyan/migan",
            "406830d0fa60666da0071c342ad2fbc8f30c5c64", "migan_pipeline_v2.onnx", 28079181,
            "6f1f3530a1a2324b19752018ce756088b07973cda8d7d890034ace5c8a48c40b", "MIT",
            license_url="https://github.com/Picsart-AI-Research/MI-GAN/blob/main/LICENSE-WEIGHTS", runtime="ONNX Runtime"),
    dict(id="anime-manga-lama", name="Anime-Manga LaMa · checkpoint", kind="inpainting", downloadable=False,
         filename="lama_large_512px.ckpt", local_files=["inpainting/lama_large_512px.ckpt"],
         sha256="11d30fbb3000fb2eceae318b75d9ced9229d99ae990a7f8b3ac35c8d31f2c935",
         model_card="https://huggingface.co/dreMaz/AnimeMangaInpainting", license="MIT",
         license_url="https://huggingface.co/dreMaz/AnimeMangaInpainting", runtime="PyTorch CUDA",
         note="Ajout manuel du checkpoint officiel ; environnement CUDA séparé requis."),
    dict(id="rapidocr-ppocrv6-small", name="RapidOCR · PP-OCRv6 small", kind="ocr", downloadable=False,
         local_files=["ocr/PP-OCRv6_det_small.onnx", "ocr/PP-OCRv6_rec_small.onnx"],
         model_card="https://github.com/RapidAI/RapidOCR", license="Apache-2.0",
         license_url="https://rapidai.github.io/RapidOCRDocs/main/en/model_licenses/", runtime="RapidOCR / ONNX Runtime",
         note="Ensemble OCR : installez les deux fichiers de détection et reconnaissance depuis le catalogue."),
]

for _entry in CURATED_MODELS:
    if _entry["kind"] == "translation":
        _entry.update(languages=["en", "fr", "ja", "ko", "zh", "es", "de", "it", "pt", "ar"],
                      languages_note="Exemples de langues documentées par l'auteur ; voir la fiche complète.",
                      description="Traduction locale de texte via le profil spécifique du modèle dans LM Studio.",
                      memory_note="La mémoire nécessaire dépasse la taille du fichier : contexte et cache KV supplémentaires. GPU partiel ou CPU selon LM Studio.")
    elif _entry["kind"] == "inpainting":
        _entry.update(languages=[], description="Restauration des zones masquées ; indépendant de la langue du texte.",
                      memory_note="La mémoire dépend de la taille des zones à restaurer et du moteur. Les zones sont traitées par recadrages.")
    else:
        _entry.update(languages=[], description="Détection et reconnaissance du texte avec les poids OCR installés.",
                      memory_note="Poids ONNX locaux ; traitement des longues pages par bandes.")


class _HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        parsed = urlparse(newurl)
        host = parsed.hostname or ""
        modelscope_redirect=urlparse(request.full_url).hostname in MODELSCOPE_HOSTS and host in MODELSCOPE_HOSTS
        if parsed.scheme != "https" or parsed.username or parsed.password or not (
            host == "huggingface.co" or host.endswith((".huggingface.co", ".hf.co")) or modelscope_redirect):
            raise ValueError("Redirection de téléchargement non autorisée.")
        return super().redirect_request(request, fp, code, message, headers, newurl)


class ModelHub:
    def __init__(self, root=registry.ROOT, *, state_dir=None, models=None, opener=None, installer=None,
                 max_bytes=MAX_DOWNLOAD_BYTES):
        self.root = Path(root).resolve()
        self.download_dir = self.root / "downloads"
        self.state_dir = Path(state_dir).resolve() if state_dir is not None else self.download_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.models = {entry["id"]: copy.deepcopy(entry) for entry in (CURATED_MODELS if models is None else models)}
        self.max_bytes = min(max_bytes, MAX_DOWNLOAD_BYTES)
        for entry in self.models.values():
            self._validate_entry(entry)
        self._open = opener or urllib.request.build_opener(_HTTPSRedirects()).open
        self._installer = installer or self._install_registry
        self._lock = threading.RLock()
        self._jobs = {}
        self._threads = {}
        self._commands = {}
        self._scope = hashlib.sha256(str(self.state_dir).encode()).hexdigest()[:16]
        self._load()

    def _validate_entry(self, entry):
        if not re.fullmatch(r"[a-z0-9-]{1,100}", entry["id"]):
            raise ValueError("Identifiant de modèle invalide.")
        for relative in entry.get("local_files", []):
            if not (self.root / relative).resolve().is_relative_to(self.root):
                raise ValueError("Chemin de modèle invalide.")
        if not entry.get("downloadable"):
            return
        filename = entry["filename"]
        parsed = urlparse(entry["source_url"])
        hf_source=(parsed.netloc=='huggingface.co' and parsed.path.endswith('/'+filename) and '/resolve/' in parsed.path)
        ocr_part=OCR_FILENAMES.get(filename)
        ocr_source=(entry.get('kind')=='ocr' and ocr_part is not None and parsed.netloc=='www.modelscope.cn'
                    and parsed.path==f'/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/{ocr_part}/{filename}')
        if (Path(filename).name != filename or "/" in filename or "\\" in filename
            or Path(filename).suffix.lower() not in {".gguf", ".onnx"}
            or parsed.scheme != "https" or not (hf_source or ocr_source)
            or parsed.query or parsed.fragment):
            raise ValueError("Source de modèle non autorisée.")
        if (isinstance(entry.get("size_bytes"), bool) or not isinstance(entry.get("size_bytes"), int)
            or not 4 <= entry["size_bytes"] <= self.max_bytes
            or not re.fullmatch(r"[a-f0-9]{64}", entry.get("sha256", ""))):
            raise ValueError("Taille ou empreinte du modèle invalide.")

    @staticmethod
    def _job_id(model_id):
        return hashlib.sha256(model_id.encode()).hexdigest()[:24]

    def _paths(self, job):
        entry = self.models[job["model_id"]]
        folder = self.download_dir / self._job_id(entry["id"])
        return folder / ("." + entry["filename"] + "." + self._scope + ".part"), folder / entry["filename"]

    def _state_path(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{24}", job_id):
            raise ValueError("Identifiant de téléchargement invalide.")
        return self.state_dir / (job_id + ".json")

    def _save(self, job):
        job["updated_at"] = utc_now()
        atomic_json(self._state_path(job["id"]), job)

    def _load(self):
        for file in self.state_dir.glob("*.json"):
            try:
                job = json.loads(file.read_text(encoding="utf-8"))
                if job.get("model_id") not in self.models or self._job_id(job["model_id"]) != job["id"] or file != self._state_path(job["id"]):
                    continue
                if not self.models[job["model_id"]].get("downloadable"):
                    continue
                if job["state"] == "downloading":
                    job.update(state="paused", message="Téléchargement interrompu ; reprise disponible.", speed_bytes_s=0)
                    partial, _ = self._paths(job)
                    job["bytes_downloaded"] = partial.stat().st_size if partial.exists() else 0
                elif job["state"] == "installing":
                    job.update(state="downloaded", message="Installation interrompue ; relancez l'installation.", speed_bytes_s=0)
                self._jobs[job["id"]] = job
                self._save(job)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue

    def _registered_models(self):
        try:
            models = json.loads((self.root / "registry.json").read_text(encoding="utf-8")).get("models", [])
            return [model for model in models if isinstance(model, dict)] if isinstance(models, list) else []
        except (OSError, ValueError, AttributeError):
            return []

    def _installed(self, entry, registered):
        if entry.get('kind')=='ocr' and entry.get('downloadable'):
            path=self.root/'ocr'/entry['filename']
            try:
                if not path.resolve().is_relative_to(self.root):return False
                self._verify(path,entry)
                return True
            except (OSError,ValueError):return False
        if entry.get("local_files"):
            return all((self.root / relative).is_file() and (self.root / relative).stat().st_size > 0 for relative in entry["local_files"])
        for model in registered:
            if model.get("kind") != entry["kind"] or not model.get("path"):
                continue
            path = Path(model["path"])
            try:
                if path.is_file() and (model.get("sha256") == entry.get("sha256") or (
                    path.name == entry.get("filename") and path.stat().st_size == entry.get("size_bytes"))):
                    return True
            except OSError:
                pass
        return False

    def _downloaded(self, job):
        _, destination = self._paths(job)
        try:
            stat = destination.stat()
            return (stat.st_size == self.models[job["model_id"]]["size_bytes"]
                    and stat.st_mtime_ns == job.get("verified_mtime_ns")
                    and job.get("verified_sha256") == self.models[job["model_id"]]["sha256"])
        except OSError:
            return False

    def catalog(self):
        registered = self._registered_models()
        with self._lock:
            rows = []
            for entry in self.models.values():
                row = copy.deepcopy(entry)
                job = self._jobs.get(self._job_id(entry["id"]))
                row["installed"] = self._installed(entry, registered)
                row["downloaded"] = bool(job and self._downloaded(job))
                row["download_id"] = job["id"] if job else None
                rows.append(row)
            return {"models": rows, "downloads": self.list_downloads()}

    def list_downloads(self):
        with self._lock:
            rows = copy.deepcopy(list(self._jobs.values()))
            for row in rows:
                total = row.get("total_bytes", 0)
                row["progress"] = min(1., row.get("bytes_downloaded", 0) / total) if total else 0.
                row["bytes_done"] = row.get("bytes_downloaded", 0)
                row["speed_bps"] = row.get("speed_bytes_s", 0)
            return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

    def installing(self):
        with self._lock:
            return any(job["state"] == "installing" for job in self._jobs.values())

    def _snapshot(self, job_id):
        return next(row for row in self.list_downloads() if row["id"] == job_id)

    def start(self, model_id):
        with self._lock:
            entry = self.models.get(model_id)
            if not entry or not entry.get("downloadable"):
                raise ValueError("Ce modèle ne peut pas être téléchargé depuis le catalogue.")
            job_id = self._job_id(model_id)
            job = self._jobs.get(job_id)
            if job and self._threads.get(job_id) and self._threads[job_id].is_alive():
                return self._snapshot(job_id)
            if job and self._downloaded(job):
                if job["state"] != "installed":
                    job.update(state="downloaded", error=None)
                    self._save(job)
                return self._snapshot(job_id)
            self.download_dir.mkdir(parents=True, exist_ok=True)
            partial, destination = self._paths({"model_id": model_id})
            # A different library may already have downloaded this immutable
            # artifact. Verify it in the worker before requiring download space.
            try:
                candidate = destination.stat().st_size == entry["size_bytes"]
            except OSError:
                candidate = False
            if not candidate:
                self._check_disk_space(entry, partial)
            if not job:
                job = dict(id=job_id, model_id=model_id, name=entry["name"], kind=entry["kind"],
                           filename=entry["filename"], created_at=utc_now(), bytes_downloaded=0)
                self._jobs[job_id] = job
            job.update(state="downloading", total_bytes=entry["size_bytes"], speed_bytes_s=0,
                       error=None, message="Téléchargement en cours")
            self._commands.pop(job_id, None)
            self._save(job)
            self._spawn(job_id, self._download)
            return self._snapshot(job_id)

    def _spawn(self, job_id, target):
        thread = threading.Thread(target=target, args=(job_id,), daemon=True, name="model-" + job_id)
        self._threads[job_id] = thread
        thread.start()

    def control(self, job_id, action):
        with self._lock:
            self._state_path(job_id)
            if job_id not in self._jobs:
                raise FileNotFoundError("Téléchargement introuvable.")
            job = self._jobs[job_id]
            running = bool(self._threads.get(job_id) and self._threads[job_id].is_alive())
            if action in {"pause", "cancel"}:
                if job["state"] == "installing":
                    raise ValueError("Attendez la fin de l'installation.")
                if job["state"] == "installed":
                    raise ValueError("Un modèle installé ne peut pas être annulé ici.")
                if running:
                    self._commands[job_id] = action
                    job["message"] = "Pause demandée" if action == "pause" else "Annulation demandée"
                elif action == "cancel":
                    partial, _ = self._paths(job)
                    partial.unlink(missing_ok=True)
                    job.update(state="cancelled", bytes_downloaded=0, speed_bytes_s=0)
                elif job["state"] in {"failed", "paused"}:
                    job.update(state="paused", speed_bytes_s=0)
                self._save(job)
            elif action == "resume":
                if running:
                    return self._snapshot(job_id)
                return self.start(job["model_id"])
            elif action == "install":
                if running or self.installing():
                    raise ValueError("Un téléchargement ou une installation est encore en cours pour ce modèle.")
                if not self._downloaded(job):
                    raise ValueError("Téléchargez et vérifiez ce modèle avant de l'installer.")
                job.update(state="installing", error=None, message="Installation et validation du modèle")
                self._save(job)
                self._spawn(job_id, self._install)
            else:
                raise ValueError("Action de téléchargement invalide.")
            return self._snapshot(job_id)

    def _stop_requested(self, job, partial, *, remove=True):
        with self._lock:
            command = self._commands.pop(job["id"], None)
            if command:
                if command == "cancel" and remove:
                    partial.unlink(missing_ok=True)
                job.update(state="paused" if command == "pause" else "cancelled", speed_bytes_s=0,
                           bytes_downloaded=partial.stat().st_size if command == "pause" and partial.exists() else 0,
                           message="Téléchargement en pause" if command == "pause" else "Téléchargement annulé")
                self._save(job)
                return True
            return False

    def _verify(self, file, entry):
        if file.stat().st_size != entry["size_bytes"]:
            raise ValueError("Téléchargement incomplet : taille du fichier incorrecte.")
        with file.open("rb") as source:
            if file.suffix.lower() == ".gguf" or entry["filename"].lower().endswith(".gguf"):
                if source.read(4) != b"GGUF":
                    raise ValueError("En-tête GGUF invalide.")
                source.seek(0)
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != entry["sha256"]:
            raise ValueError("Empreinte SHA-256 incorrecte. Relancez le téléchargement.")

    def _check_disk_space(self, entry, partial):
        offset = partial.stat().st_size if partial.exists() else 0
        required = entry["size_bytes"] - offset if offset <= entry["size_bytes"] else entry["size_bytes"]
        if shutil.disk_usage(self.download_dir).free < required:
            raise OSError(f"Espace disque insuffisant : {required:,} octets supplémentaires nécessaires.")

    def _download(self, job_id):
        job = self._jobs[job_id]
        entry = self.models[job["model_id"]]
        partial, destination = self._paths(job)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Other libraries can reuse the same verified immutable artifact.
            if destination.is_file():
                try:
                    self._verify(destination, entry)
                except ValueError:
                    pass
                else:
                    with self._lock:
                        if not self._stop_requested(job, partial):
                            self._complete(job, destination)
                    return
            self._check_disk_space(entry, partial)
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > entry["size_bytes"]:
                partial.unlink()
                offset = 0
            if offset < entry["size_bytes"]:
                headers = {"Accept-Encoding": "identity", "User-Agent": "Renkavi-ModelHub/1"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                request = urllib.request.Request(entry["source_url"], headers=headers)
                with self._open(request, timeout=30) as response:
                    status = getattr(response, "status", None)
                    if status is None:
                        status = response.getcode()
                    if status == 200:
                        offset = 0  # Server ignored Range: truncate before writing.
                    elif status == 206:
                        value = response.headers.get("Content-Range", "")
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value)
                        if not match or tuple(map(int, match.groups())) != (offset, entry["size_bytes"]-1, entry["size_bytes"]):
                            raise ValueError("Réponse de reprise HTTP incohérente.")
                    else:
                        raise ValueError(f"Réponse HTTP inattendue : {status}")
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) != entry["size_bytes"] - offset:
                        raise ValueError("Taille annoncée par le serveur incorrecte.")
                    if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
                        raise ValueError("Encodage HTTP du fichier non pris en charge.")
                    started, initial, last_save = time.monotonic(), offset, time.monotonic()
                    stopped = False
                    with partial.open("ab" if offset else "wb") as output:
                        while True:
                            chunk = response.read(CHUNK_BYTES)
                            if not chunk:
                                break
                            if offset + len(chunk) > entry["size_bytes"]:
                                raise ValueError("Le serveur dépasse la taille de fichier attendue.")
                            output.write(chunk)
                            offset += len(chunk)
                            with self._lock:
                                job.update(bytes_downloaded=offset, speed_bytes_s=(offset-initial)/max(.001, time.monotonic()-started))
                                if time.monotonic()-last_save > .5:
                                    self._save(job)
                                    last_save = time.monotonic()
                            output.flush()
                            if self._stop_requested(job, partial, remove=False):
                                stopped = True
                                break
                        output.flush()
                        os.fsync(output.fileno())
                    if stopped:
                        if job["state"] == "cancelled":
                            partial.unlink(missing_ok=True)
                        return
            if self._stop_requested(job, partial):
                return
            with self._lock:
                job["message"] = "Vérification SHA-256 du fichier téléchargé"
                self._save(job)
            try:
                self._verify(partial, entry)
            except ValueError:
                # A complete corrupt response must not be resumed at EOF forever.
                if partial.stat().st_size == entry["size_bytes"]:
                    partial.unlink()
                raise
            with self._lock:
                # Hashing multi-GB weights can take seconds. Honor controls
                # received during verification before publishing the artifact.
                if self._stop_requested(job, partial):
                    return
                os.replace(partial, destination)
                self._complete(job, destination)
        except Exception as exc:
            self._fail(job, exc)

    def _complete(self, job, destination):
        with self._lock:
            job.update(state="downloaded", bytes_downloaded=destination.stat().st_size, speed_bytes_s=0,
                       verified_mtime_ns=destination.stat().st_mtime_ns,
                       verified_sha256=self.models[job["model_id"]]["sha256"], error=None,
                       message="Téléchargé et vérifié. Installation disponible.")
            self._save(job)

    def _fail(self, job, exc):
        with self._lock:
            partial, _ = self._paths(job)
            job.update(state="failed", speed_bytes_s=0, error=str(exc), message="Échec ; reprise disponible",
                       bytes_downloaded=self.models[job["model_id"]]["size_bytes"] if self._downloaded(job) else partial.stat().st_size if partial.exists() else 0)
            self._save(job)

    def _install_registry(self, entry, path):
        if entry['kind']=='ocr':
            destination=self.root/'ocr'/entry['filename']
            if entry['filename'] not in OCR_FILENAMES or not destination.resolve().is_relative_to(self.root):
                raise ValueError('Chemin de modèle OCR invalide.')
            destination.parent.mkdir(parents=True,exist_ok=True)
            temporary=None
            try:
                with tempfile.NamedTemporaryFile(dir=destination.parent,prefix='.'+entry['filename']+'.',suffix='.tmp',delete=False) as target:
                    temporary=Path(target.name)
                    with path.open('rb') as source:shutil.copyfileobj(source,target,CHUNK_BYTES)
                    target.flush()
                    os.fsync(target.fileno())
                self._verify(temporary,entry)
                os.replace(temporary,destination)
            finally:
                if temporary is not None:temporary.unlink(missing_ok=True)
            return
        return registry.add_model(entry["kind"], str(path), root=self.root, activate=False, test=False)

    def _install(self, job_id):
        job = self._jobs[job_id]
        entry = self.models[job["model_id"]]
        _, destination = self._paths(job)
        try:
            self._verify(destination, entry)
            self._installer(copy.deepcopy(entry), destination)
            with self._lock:
                job.update(state="installed", error=None, message="Poids OCR installé." if entry['kind']=='ocr' else "Modèle installé. Sélectionnez-le pour l'utiliser.")
                self._save(job)
        except Exception as exc:
            self._fail(job, exc)
