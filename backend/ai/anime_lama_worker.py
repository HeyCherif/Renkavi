"""Persistent PyTorch CUDA worker for Anime-Manga LaMa. JSONL over stdin/stdout."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import traceback

OFFICIAL_SHA256 = "11d30fbb3000fb2eceae318b75d9ced9229d99ae990a7f8b3ac35c8d31f2c935"


def send(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def decode(encoded, mode):
    with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as im:
        return np.asarray(im.convert(mode)).copy()


def encode(array):
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="PNG", compress_level=1)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def process(model, device, image, mask):
    if image.ndim != 3 or image.shape[2] != 3 or mask.shape != image.shape[:2]:
        raise ValueError("Dimensions image/masque incompatibles")
    if not np.any(mask):
        return image.copy()
    h, w = mask.shape
    # The application supplies local crops. Limit unusually large regions.
    maximum = 1024 if device == "cuda" else 768
    ratio = min(1.0, maximum / max(w, h))
    nw, nh = max(8, round(w * ratio)), max(8, round(h * ratio))
    if ratio < 1.0:
        image_small = np.asarray(Image.fromarray(image).resize((nw, nh), Image.Resampling.LANCZOS))
        mask_small = np.asarray(Image.fromarray(mask).resize((nw, nh), Image.Resampling.NEAREST))
    else:
        image_small, mask_small = image, mask
    padding = (0, (8 - nw % 8) % 8, 0, (8 - nh % 8) % 8)
    pixels = torch.from_numpy(np.ascontiguousarray(image_small.transpose(2, 0, 1)))[None].float().to(device).div_(255.0)
    alpha = torch.from_numpy(np.ascontiguousarray((mask_small > 0).astype(np.float32)))[None,None].to(device)
    if padding[1] or padding[3]:
        pixels = F.pad(pixels, padding, mode="reflect" if nw > padding[1] and nh > padding[3] else "replicate")
        alpha = F.pad(alpha, padding, mode="constant", value=0)
    with torch.inference_mode():
        restored = model(torch.cat((pixels * (1 - alpha), alpha), dim=1))
        generated = restored[0,:,:nh,:nw].mul(255).clamp(0,255).permute(1,2,0).byte().cpu().numpy()
    if ratio < 1:
        generated = np.asarray(Image.fromarray(generated).resize((w,h), Image.Resampling.BICUBIC))
    return np.where(mask[...,None]>0, generated, image).astype(np.uint8)


def main():
    # Imports belong inside the guarded startup: DLL/commit-memory errors must
    # reach the parent through JSON rather than disappearing into stderr.
    global np, torch, F, Image
    import numpy as np
    import torch
    import torch.nn.functional as F
    from PIL import Image
    from backend.ai.vendor.lama_ffc import load_generator
    checkpoint = sys.argv[1]
    with open(checkpoint, "rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != OFFICIAL_SHA256:
            raise ValueError("Checkpoint officiel attendu : signature SHA-256 incorrecte")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA est indisponible dans l'environnement PyTorch")
    device="cuda"
    model=load_generator(checkpoint, map_location="cpu").to(device).eval()
    send({"ready": True,"device":torch.cuda.get_device_name(0),"torch":torch.__version__})
    for line in sys.stdin:
        try:
            request=json.loads(line)
            image=decode(request["image"],"RGB")
            mask=decode(request["mask"],"L")
            result=process(model,device,image,mask)
            send({"ok":True,"image":encode(result)})
        except Exception as exc:
            send({"ok":False,"error":f"{type(exc).__name__}: {exc}"})
            traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        send({"ready":False, "error":f"{type(exc).__name__}: {exc}"})
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
