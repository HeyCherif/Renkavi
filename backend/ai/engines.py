from __future__ import annotations

import importlib.util
import re
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from backend.runtime import resource_root, models_root

ROOT = resource_root()
MODELS = models_root()


def model_status(models=None) -> dict:
    from backend.ai.registry import selected_model, is_available
    translator = models["translation"] if models else selected_model("translation")
    painter = models["inpainting"] if models else selected_model("inpainting")
    ocr_runtime = all(importlib.util.find_spec(m) is not None for m in ("rapidocr", "onnxruntime", "cv2"))
    translation_runtime = (importlib.util.find_spec("ctranslate2") is not None and importlib.util.find_spec("sentencepiece") is not None) if translator["adapter"] == "argos" else True
    from backend.ai.anime_lama import worker_available
    paint_runtime = worker_available() if painter["adapter"] == "anime_lama_torch" else all(importlib.util.find_spec(m) is not None for m in ("cv2", "onnxruntime"))
    ocr_ready = ocr_runtime and (MODELS / "ocr" / "PP-OCRv6_det_small.onnx").is_file() and (MODELS / "ocr" / "PP-OCRv6_rec_small.onnx").is_file()
    translate_ready = translation_runtime and is_available(translator)
    paint_ready = paint_runtime and is_available(painter)
    return {"ready": ocr_ready and translate_ready and paint_ready, "runtime": ocr_runtime,
            "ocr": {"ready": ocr_ready, "name": "RapidOCR PP-OCRv6", "languages": ["en", "zh"]},
            "translation": {"ready": translate_ready, "name": translator["name"], "pairs": ["en-fr"] if translator["adapter"] == "argos" else ["multilingue"]},
            "inpainting": {"ready": paint_ready, "name": painter["name"]},
            "device": f"Nettoyage : {'PyTorch CUDA' if painter['adapter'] == 'anime_lama_torch' else 'CPU'} · Traduction : {'LM Studio' if translator['adapter'] == 'lmstudio' else 'Connecteur API' if translator['adapter']=='openai_compatible' else 'CPU'}",
            "messages": (["Environnement CUDA DubRoom introuvable pour Anime-Manga LaMa."] if painter["adapter"] == "anime_lama_torch" and not paint_ready else [])}


def box_iou(a: list, b: list) -> float:
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union else 0


def group_lines(lines: list[dict]) -> list[dict]:
    # Join nearby lines of a speech block; retain each line for glyph masks.
    groups = []
    for line in sorted(lines, key=lambda item: (item["box"][1], item["box"][0])):
        b = line["box"]
        matched = None
        for group in reversed(groups[-10:]):
            previous = group[-1]["box"]
            height = min(b[3] - b[1], previous[3] - previous[1])
            size_ratio = max(b[3]-b[1], previous[3]-previous[1]) / max(1, height)
            overlap = max(0, min(b[2], previous[2]) - max(b[0], previous[0]))
            gap = b[1] - previous[3]
            fragment = not re.search(r'[.!?…:]\s*$', group[-1]['text']) and len(line['text']) <= 12
            max_gap = height * (1.6 if fragment else .85)
            if size_ratio < 1.7 and -height * .35 <= gap <= max_gap and overlap / max(1, min(b[2]-b[0], previous[2]-previous[0])) > .35:
                matched = group
                break
        if matched is None:
            groups.append([line])
        else:
            matched.append(line)
    regions = [{"id": str(uuid.uuid4()), "box": [min(x["box"][0] for x in group), min(x["box"][1] for x in group),
             max(x["box"][2] for x in group), max(x["box"][3] for x in group)], "lines": group,
             "source_text": " ".join(x["text"].strip() for x in group), "target_text": "", "status": "detected",
             "confidence_detection": None, "confidence_ocr": round(sum(x["score"] for x in group)/len(group), 4)} for group in groups]
    for region in regions:
        if re.search(r"https?://|www\.|\b[\w-]+\.(com|org|net|gg|io)\b|discord|this chapter|translated by|\b(?:vortex|nyx)\s+scans?\b|^(vortex|nyx|scans?)$", region["source_text"], re.I):
            region.update(status="ignored", kind="credit_or_link")
        elif len(re.sub(r'[^a-zA-Z]', '', region['source_text'])) < 2 or re.search(r'([A-Za-z])\1{3}',region['source_text']) or (region['confidence_ocr'] < .82 and len(region['source_text']) < 6):
            region.update(status='ignored', kind='uncertain_ocr')
    return regions


class LocalEngines:
    def __init__(self, models=None):
        self.models = models
        self.ocr = None
        self.translator = None
        self.tokenizer = None
        self.lama = None
        self.inpainting_id = None
        self.anime_engine = None
        self.context_before = []
        self.context_after = []
        self.chapter_context = []

    def close(self):
        if self.anime_engine is not None:
            self.anime_engine.close()
            self.anime_engine = None
        self.ocr = self.translator = self.tokenizer = self.lama = None

    def detect(self, image: Image.Image, progress=lambda message: None, source: str = "auto") -> list[dict]:
        from rapidocr import RapidOCR
        if self.ocr is None:
            self.ocr = RapidOCR(params={"Global.log_level": "error", "Global.model_root_dir": str(MODELS / "ocr"),
                "EngineConfig.onnxruntime.intra_op_num_threads": 4, "Global.use_cls": False})
        lines = []
        height = 1600
        step = height - 160
        for top in range(0, image.height, step):
            if top and image.height - top < 160:
                break
            progress(f"OCR : bande {top // step + 1}")
            crop = image.crop((0, top, image.width, min(image.height, top + height)))
            result = self.ocr(crop, use_cls=False)
            texts = result.txts or ()
            for polygon, text, score in zip(result.boxes if result.boxes is not None else [], texts, result.scores or ()):
                if not text.strip() or score < .6:
                    continue
                points = [[float(p[0]), float(p[1]) + top] for p in polygon]
                box = [max(0, int(min(p[0] for p in points))), max(0, int(min(p[1] for p in points))),
                       min(image.width, int(max(p[0] for p in points)) + 1), min(image.height, int(max(p[1] for p in points)) + 1)]
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                duplicate = next((old for old in lines if box_iou(old["box"], box) > .4), None)
                line = {"box": box, "text": str(text), "score": float(score)}
                if duplicate:
                    if score > duplicate["score"]:
                        duplicate.update(line)
                else:
                    lines.append(line)
        regions = group_lines(lines)
        for region in regions:
            if source == "en" and re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", region["source_text"]):
                region.update(status="ignored", kind="script_mismatch")
        return regions

    def translate(self, regions: list[dict], source: str, target: str) -> int:
        pending = [region for region in regions if region["source_text"].strip() and not region["target_text"].strip() and region["status"] != "ignored"]
        if not pending:
            return 0
        if source == target:
            for region in pending:
                region["target_text"] = region["source_text"]
            return len(pending)
        from backend.ai.local_llm import configuration, translate
        llm = configuration(self.models["translation"] if self.models else None)
        if llm:
            translate(pending, source, target, llm, reference_regions=regions,
                      previous_context=self.context_before, next_context=self.context_after,chapter_context=self.chapter_context)
            for region in pending:
                region['preserve_original'] = region['target_text'].casefold().strip() == region['source_text'].casefold().strip()
            return len(pending)
        import ctranslate2
        import sentencepiece as spm
        if source == "auto":
            joined = "".join(region["source_text"] for region in pending)
            source = "en" if sum(c.isascii() for c in joined) / max(1, len(joined)) > .85 else "unknown"
        if (source, target) != ("en", "fr"):
            raise ValueError(f"Traduction locale {source} → {target} non installée. Le modèle disponible est anglais → français.")
        model = MODELS / "translation" / "en-fr"
        if self.translator is None:
            self.tokenizer = spm.SentencePieceProcessor(model_file=str(model / "sentencepiece.model"))
            self.translator = ctranslate2.Translator(str(model / "model"), device="cpu", compute_type="int8", intra_threads=4)
        texts = [re.sub(r"\s+", " ", region["source_text"]).strip() for region in pending]
        texts = [text.capitalize() if text.isupper() else text for text in texts]
        tokens = [self.tokenizer.encode(text, out_type=str) for text in texts]
        results = self.translator.translate_batch(tokens, beam_size=4, max_batch_size=16, max_decoding_length=256)
        for region, result in zip(pending, results):
            translated = self.tokenizer.decode(result.hypotheses[0]).replace("▁", " ")
            translated = re.sub(r"\s+([,.!?;:])", r"\1", translated)
            translated = re.sub(r"\s+", " ", translated).strip()
            if not translated:
                raise RuntimeError("Le moteur de traduction a renvoyé une ligne vide.")
            region["target_text"] = translated
            region["status"] = "translated"
        return len(pending)

    def _lama_crop(self, rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        import cv2
        import onnxruntime as ort
        from backend.ai.registry import selected_model
        model = self.models["inpainting"] if self.models else selected_model("inpainting")
        self.inpaint_method = model["adapter"]
        if model["adapter"] == "telea":
            return cv2.inpaint(rgb, mask, 3, cv2.INPAINT_TELEA)
        if model["adapter"] == "anime_lama_torch":
            from backend.ai.anime_lama import AnimeLama
            def run():
                if self.anime_engine is None or self.inpainting_id != model['id']:
                    if self.anime_engine is not None:self.anime_engine.close()
                    self.anime_engine=AnimeLama(Path(model['path']))
                    self.inpainting_id=model['id']
                return self.anime_engine.inpaint(rgb,mask)
            try:return run()
            except RuntimeError as exc:
                translator=(self.models or {}).get('translation',{})
                if translator.get('adapter')!='lmstudio' or not any(marker in str(exc).lower() for marker in ('out of memory','not enough memory','bad_alloc','bad allocation','winerror 1455','paging file','fichier de pagination')):
                    raise
                from backend.ai.local_llm import release_loaded
                if not release_loaded(translator.get('config',{})):raise
                if self.anime_engine is not None:self.anime_engine.close()
                self.anime_engine=None
                return run()
        if self.lama is None or self.inpainting_id != model["id"]:
            options = ort.SessionOptions()
            options.intra_op_num_threads = 4
            self.lama = ort.InferenceSession(model["path"], sess_options=options, providers=["CPUExecutionProvider"])
            self.inpainting_id = model["id"]
        h, w = mask.shape
        if model["adapter"] == "migan_pipeline":
            output = self.lama.run(None, {model["image_input"]: rgb.transpose(2, 0, 1)[None], model["mask_input"]: (255-mask)[None, None]})[0][0].transpose(1, 2, 0)
            return np.where(mask[..., None] > 0, output.astype(np.uint8), rgb)
        size = model.get("size", 512)
        scale = min(1., size / max(w, h))
        rw, rh = max(1, round(w*scale)), max(1, round(h*scale))
        resized = cv2.resize(rgb, (rw, rh), interpolation=cv2.INTER_AREA)
        reduced_mask = cv2.resize(mask, (rw, rh), interpolation=cv2.INTER_NEAREST)
        padded = cv2.copyMakeBorder(resized, 0, size-rh, 0, size-rw, cv2.BORDER_REFLECT)
        padded_mask = cv2.copyMakeBorder(reduced_mask, 0, size-rh, 0, size-rw, cv2.BORDER_CONSTANT, value=0)
        inputs = {model["image_input"]: padded.transpose(2, 0, 1)[None].astype(np.float32)/255,
                  model["mask_input"]: (padded_mask[None, None] > 0).astype(np.float32)}
        output = self.lama.run(None, inputs)[0][0].transpose(1, 2, 0)
        if not np.isfinite(output).all():
            raise RuntimeError("LaMa a produit des pixels invalides.")
        if output.max() <= 1.5:
            output *= 255
        restored = cv2.resize(np.clip(output[:rh, :rw], 0, 255).astype(np.uint8), (w, h), interpolation=cv2.INTER_CUBIC)
        return np.where(mask[..., None] > 0, restored, rgb)

    def clean(self, image: Image.Image, regions: list[dict], progress=lambda message: None, strokes=None) -> tuple[Image.Image, Image.Image, dict]:
        import cv2
        original = np.asarray(image.convert("RGB"))
        result = original.copy()
        full_mask = np.zeros(original.shape[:2], dtype=np.uint8)
        counts = {"flat": 0, "lama": 0, "anime_lama": 0, "migan": 0, "telea": 0, "no_glyphs": 0}
        for index, region in enumerate(regions):
            if region["status"] == "ignored" or region.get('preserve_original'):
                continue
            progress(f"Nettoyage : zone {index + 1}/{len(regions)}")
            x1, y1, x2, y2 = [int(v) for v in region["box"]]
            padding = max(96, min(192, int(max(x2-x1,y2-y1)*.35)))
            left, top, right, bottom = max(0, x1-padding), max(0, y1-padding), min(image.width, x2+padding), min(image.height, y2+padding)
            crop = original[top:bottom, left:right]
            mask = np.zeros(crop.shape[:2], np.uint8)
            for line in region.get("lines") or [{"box": region["box"]}]:
                a, b, c, d = line["box"]
                radius = max(3, min(12, round((d-b)*.22)))
                a, b, c, d = max(0, int(a)-left-radius-2), max(0, int(b)-top-radius-2), min(right-left, int(c)-left+radius+2), min(bottom-top, int(d)-top+radius+2)
                # Cover ink, hollow letters, outlines and colored glow together.
                # Tiny glyph-only holes left the original lettering visible.
                if c > a and d > b:
                    mask[b:d, a:c] = 255
            if not mask.any():
                counts["no_glyphs"] += 1
                continue
            ring = cv2.subtract(cv2.dilate(mask, np.ones((17, 17), np.uint8)), mask)
            samples = crop[ring > 0]
            # Only truly uniform paper is filled. Dark artwork always goes
            # through the selected model, even when its contrast is low.
            if len(samples) and np.median(samples) > 235 and np.max(np.std(samples.astype(np.float32), axis=0)) < 3:
                restored = np.where(mask[..., None] > 0, np.median(samples, axis=0).astype(np.uint8), crop)
                counts["flat"] += 1
            else:
                restored = self._lama_crop(crop, mask)
                alpha = np.minimum(1., cv2.distanceTransform(mask, cv2.DIST_L2, 3)/3.)[...,None]
                restored = np.clip(restored*alpha+crop*(1-alpha),0,255).astype(np.uint8)
                key = {"lama_onnx": "lama", "anime_lama_torch": "anime_lama", "migan_pipeline": "migan", "telea": "telea"}[self.inpaint_method]
                counts[key] += 1
            view = result[top:bottom, left:right]
            view[mask > 0] = restored[mask > 0]
            full_mask[top:bottom, left:right] = np.maximum(full_mask[top:bottom, left:right], mask)
        if strokes:
            from backend.projects.artifacts import _draw_stroke

            manual_mask = np.zeros_like(full_mask)
            # Brush edits apply after automatic masks, in their persisted order.
            for stroke in strokes:
                layer, (left, top, right, bottom) = _draw_stroke(image.size, stroke)
                covered = np.asarray(layer) > 0
                value = 0 if stroke["mode"] == "erase" else 255
                full_mask[top:bottom, left:right][covered] = value
                manual_mask[top:bottom, left:right][covered] = value
            # Erasing an automatically cleaned area restores exact original pixels.
            result[full_mask == 0] = original[full_mask == 0]
            # Contours avoid a full-page int32 component label image on tall strips.
            contours, _ = cv2.findContours(manual_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                x, y, width, height = cv2.boundingRect(contour)
                # Bound model context even for a long connected brush stroke.
                for core_top in range(y, y + height, 832):
                    for core_left in range(x, x + width, 832):
                        core_right = min(x + width, core_left + 832)
                        core_bottom = min(y + height, core_top + 832)
                        selected = manual_mask[core_top:core_bottom, core_left:core_right] > 0
                        if not selected.any():
                            continue
                        left, top = max(0, core_left - 96), max(0, core_top - 96)
                        right, bottom = min(image.width, core_right + 96), min(image.height, core_bottom + 96)
                        progress("Nettoyage : corrections au pinceau")
                        restored = self._lama_crop(original[top:bottom, left:right], full_mask[top:bottom, left:right])
                        restored_core = restored[core_top-top:core_bottom-top, core_left-left:core_right-left]
                        result[core_top:core_bottom, core_left:core_right][selected] = restored_core[selected]
                        key = {"lama_onnx": "lama", "anime_lama_torch": "anime_lama", "migan_pipeline": "migan", "telea": "telea"}[self.inpaint_method]
                        counts[key] += 1
        return Image.fromarray(result), Image.fromarray(full_mask), counts


def render_translation(image: Image.Image, regions: list[dict]) -> tuple[Image.Image, list[str]]:
    from backend.ai.typography import render
    return render(image, regions)

