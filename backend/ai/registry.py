"""Persistent local model catalog. GGUF through LM Studio, validated ONNX inpainting."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from backend.core.project import utc_now
from backend.catalog_lock import CatalogLock
from backend.projects.store import atomic_json

from backend.runtime import models_root
ROOT = models_root()
LOCK = CatalogLock(ROOT / ".registry.lock")


def read_catalog(root: Path = ROOT) -> dict:
    # Initial creation is a mutation too: it must never replace another
    # process's freshly initialized or updated catalogue.
    with LOCK:
        return _read_catalog(root)


def _read_catalog(root: Path) -> dict:
    path = root / "registry.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("Version du catalogue de modèles inconnue.")
        return data
    models = [
        {"id": "argos-en-fr", "kind": "translation", "name": "Argos anglais → français (CPU)", "adapter": "argos", "path": str(root / "translation/en-fr/model/model.bin")},
        {"id": "lama", "kind": "inpainting", "name": "LaMa ONNX", "adapter": "lama_onnx", "path": str(root / "inpainting/lama_fp32.onnx"), "size": 512, "image_input": "image", "mask_input": "mask"},
        {"id": "telea", "kind": "inpainting", "name": "OpenCV Telea (rapide, sans modèle)", "adapter": "telea"},
    ]
    selected = {"translation": "argos-en-fr", "inpainting": "lama"}
    if (root / "llm.json").is_file():
        config = json.loads((root / "llm.json").read_text(encoding="utf-8"))
        models.insert(0, {"id": "local-llm", "kind": "translation", "name": config.get("model_key", "LLM local"), "adapter": "lmstudio", "config": config, "tested": True})
        selected["translation"] = "local-llm"
    anime = root / "inpainting" / "lama_large_512px.ckpt"
    if anime.is_file():
        models.insert(0, {"id": "anime-manga-lama", "kind": "inpainting",
            "name": "Anime-Manga LaMa · PyTorch CUDA", "adapter": "anime_lama_torch",
            "path": str(anime), "tested": False})
        from backend.ai.anime_lama import worker_available
        if worker_available():
            selected["inpainting"] = "anime-manga-lama"
    hy = root / "translation" / "Hy-MT2-1.8B-Q8_0.gguf"
    cli = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    if hy.is_file():
        config = {"base_url": "http://127.0.0.1:1234", "model": "renkavi-hy-mt2",
                  "model_key": "hy-mt2-1-8b", "lms_cli": str(cli), "profile": "hy_mt"}
        models.insert(0, {"id": "hy-mt2-1.8b", "kind": "translation",
                          "name": "HY-MT2 1.8B Q8 · LM Studio", "adapter": "lmstudio",
                          "path": str(hy), "config": config, "tested": False})
        if cli.is_file():
            selected["translation"] = "hy-mt2-1.8b"
    data = {"schema_version": 1, "models": models, "selected": selected}
    atomic_json(path, data)
    return data


def save_endpoint(config: dict, base_url: str) -> None:
    """Remember a usable endpoint for this exact model, preserving other choices."""
    with LOCK:
        data = read_catalog()
        for model in data["models"]:
            current = model.get("config", {})
            if current.get("model") == config["model"] and current.get("model_key") == config["model_key"]:
                current["base_url"] = base_url
        atomic_json(ROOT / "registry.json", data)


def is_available(model: dict) -> bool:
    if model['adapter']=='openai_compatible':
        from backend.ai.connectors import endpoint
        try:return bool(model['config']['model'] and endpoint(model['config']['base_url']))
        except (ValueError,KeyError):return False
    if model["adapter"] == "telea":
        return True
    if model["adapter"] == "anime_lama_torch":
        from backend.ai.anime_lama import worker_available
        return Path(model["path"]).is_file() and worker_available()
    if model["adapter"] == "lmstudio":
        return Path(model["config"]["lms_cli"]).is_file() and (not model.get('path') or Path(model['path']).is_file())
    return Path(model.get("path", "")).is_file()


def catalog(root: Path = ROOT) -> dict:
    with LOCK:
        data = read_catalog(root)
        for model in data["models"]:
            model["available"] = is_available(model)
        return data


def selected_model(kind: str, root: Path = ROOT) -> dict:
    with LOCK:
        data = read_catalog(root)
        model = next((m for m in data["models"] if m["id"] == data["selected"].get(kind) and m["kind"] == kind), None)
        if model is None:
            raise ValueError("Modèle sélectionné introuvable.")
        return model


def select_model(kind: str, model_id: str, root: Path = ROOT) -> dict:
    with LOCK:
        data = read_catalog(root)
        model = next((m for m in data["models"] if m["id"] == model_id and m["kind"] == kind), None)
        if model is None or not is_available(model):
            raise ValueError("Ce modèle est introuvable sur le disque.")
        data["selected"][kind] = model_id
        atomic_json(root / "registry.json", data)
        return catalog(root)


def validate_onnx(path: Path) -> dict:
    import numpy as np
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
    inputs = session.get_inputs()
    image = next((i for i in inputs if len(i.shape) == 4 and i.shape[1] == 3), None)
    mask = next((i for i in inputs if len(i.shape) == 4 and i.shape[1] == 1), None)
    if len(inputs) != 2 or image is None or mask is None:
        raise ValueError("ONNX incompatible : il faut deux entrées NCHW, image RGB et masque (LaMa ou pipeline MI-GAN).")
    if image.type == mask.type == "tensor(float)":
        size = image.shape[2] if isinstance(image.shape[2], int) else 512
        if not isinstance(size, int) or not 64 <= size <= 2048 or image.shape[3] not in {size, None, "width"}:
            raise ValueError("Dimensions ONNX non prises en charge.")
        adapter = "lama_onnx"
        feed = {image.name: np.full((1, 3, size, size), .5, np.float32), mask.name: np.zeros((1, 1, size, size), np.float32)}
        feed[mask.name][:, :, size//3:2*size//3, size//3:2*size//3] = 1
    elif image.type == mask.type == "tensor(uint8)":
        size = 512
        adapter = "migan_pipeline"
        feed = {image.name: np.full((1, 3, size, size), 128, np.uint8), mask.name: np.full((1, 1, size, size), 255, np.uint8)}
        feed[mask.name][:, :, 180:330, 180:330] = 0
    else:
        raise ValueError("Types ONNX non pris en charge : float32 LaMa ou uint8 MI-GAN attendus.")
    result = session.run(None, feed)[0]
    if result.shape != (1, 3, size, size) or not np.isfinite(result).all():
        raise ValueError("Le modèle ne produit pas une image RGB valide.")
    return {"adapter": adapter, "size": size, "image_input": image.name, "mask_input": mask.name, "tested": True}


def add_model(kind: str, file: str, root: Path = ROOT, *, activate=True, test=True) -> dict:
    if kind not in {"translation", "inpainting"}:
        raise ValueError("Type de modèle invalide.")
    path = Path(file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("Fichier de modèle introuvable.")
    with LOCK:
        data = read_catalog(root)
        previous = next((m for m in data["models"] if m.get("path") == str(path) and m["kind"] == kind), None)
        if previous:
            return select_model(kind, previous["id"], root) if activate else catalog(root)
        model = {"id": uuid.uuid4().hex, "kind": kind, "name": path.stem, "path": str(path), "added_at": utc_now()}
        if kind == "inpainting":
            if path.suffix.lower() == ".ckpt":
                with path.open('rb') as checkpoint:checkpoint_hash=hashlib.file_digest(checkpoint,'sha256').hexdigest()
                if checkpoint_hash != "11d30fbb3000fb2eceae318b75d9ced9229d99ae990a7f8b3ac35c8d31f2c935":
                    raise ValueError("Checkpoint inconnu : seuls les poids AnimeMangaInpainting officiels sont acceptés.")
                model.update(adapter="anime_lama_torch", tested=False)
            elif path.suffix.lower() == ".onnx":
                model.update(validate_onnx(path))
            else:
                raise ValueError("Choisissez un ONNX ou le checkpoint officiel Anime-Manga LaMa (.ckpt).")
        else:
            if path.suffix.lower() != ".gguf":
                raise ValueError("Choisissez un fichier GGUF pour la traduction (HY-MT2, Qwen…).")
            with path.open("rb") as source:
                if source.read(4) != b"GGUF":
                    raise ValueError("Le fichier ne contient pas un modèle GGUF valide.")
            cli = Path.home() / ".lmstudio/bin/lms.exe"
            if not cli.is_file():
                raise FileNotFoundError("Installez LM Studio pour charger les traducteurs GGUF.")
            slug = re.sub(r"[^a-z0-9-]", "-", path.stem.lower())[:60].strip("-") or model["id"]
            repository = "renkavi/"+slug
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            log_path = root / "model-import.log"
            root.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run([str(cli), "import", str(path), "--symbolic-link", "--user-repo", repository, "-y"], stdout=log, stderr=log, timeout=180, creationflags=flags)
                if result.returncode:
                    result = subprocess.run([str(cli), "import", str(path), "--hard-link", "--user-repo", repository, "-y"], stdout=log, stderr=log, timeout=180, creationflags=flags)
                if result.returncode:
                    raise RuntimeError("LM Studio ne peut pas lier ce fichier. Copie manuelle nécessaire (non effectuée).")
            listed = subprocess.run([str(cli), "ls", "--json"], capture_output=True, text=True, encoding="utf-8", timeout=30, creationflags=flags)
            content = listed.stdout
            entries = json.loads(content[content.find("["):])
            installed = next((m for m in entries if repository in m["modelKey"] or repository in m.get("path", "")), None)
            if not installed:
                raise RuntimeError("Modèle importé, mais son identifiant LM Studio n'a pas été trouvé.")
            previous_config=next((m.get('config',{}) for m in data['models'] if m['id']==data['selected'].get('translation') and m['adapter']=='lmstudio'), {})
            if not previous_config:previous_config=next((m['config'] for m in data['models'] if m['adapter']=='lmstudio'),{})
            config = {"base_url": previous_config.get('base_url','http://127.0.0.1:1234'), "model": "renkavi-"+model["id"][:12],
                      "model_key": installed["modelKey"], "lms_cli": str(cli),
                      "profile": "translategemma" if 'translategemma' in path.stem.lower() else "hy_mt" if re.search(r"hy[-_]?mt", path.stem, re.I) else "chat"}
            model.update(adapter='lmstudio',config=config,tested=False)
            if test:
                from backend.ai.local_llm import translate
                sample = [{"id": "sample", "source_text": "Hello world.", "target_text": "", "status": "detected"}]
                translate(sample, "en", "fr", config)
                model.update(tested=True,sample=sample[0]['target_text'])
        with path.open("rb") as source:
            model["sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
        data["models"].append(model)
        if activate:data["selected"][kind] = model["id"]
        atomic_json(root / "registry.json", data)
        return catalog(root)


def pick_model(kind: str) -> str:
    if kind not in {"translation", "inpainting"}:
        raise ValueError("Type de modèle invalide.")
    from tkinter import Tk, filedialog
    root = Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        return filedialog.askopenfilename(title="Ajouter un modèle de traduction" if kind == "translation" else "Ajouter un modèle d’inpainting",
                    filetypes=[("Modèle GGUF", "*.gguf")] if kind == "translation" else [("Inpainting ONNX ou Anime-Manga LaMa", "*.onnx *.ckpt")], parent=root)
    finally:
        root.destroy()
