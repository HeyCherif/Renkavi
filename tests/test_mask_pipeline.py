import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from backend.ai.engines import LocalEngines
from backend.ai.pipeline import PipelineManager, output_file
from backend.projects.importer import import_source
from backend.projects.store import ProjectStore


class MaskPipelineTests(unittest.TestCase):
    def test_worker_startup_pagefile_error_releases_owned_llm_and_retries_once(self):
        from unittest.mock import MagicMock
        engine=LocalEngines({'translation':{'adapter':'lmstudio','config':{'model':'renkavi-test','lms_cli':'cli'}},'inpainting':{'adapter':'anime_lama_torch','id':'anime','path':'model.ckpt'}})
        recovered=MagicMock();recovered.inpaint.return_value=self.pixels
        with patch('backend.ai.anime_lama.AnimeLama',side_effect=[RuntimeError('OSError: [WinError 1455] Le fichier de pagination est insuffisant'),recovered]) as worker,patch('backend.ai.local_llm.release_loaded',return_value=True) as release:
            result=engine._lama_crop(self.pixels,np.ones(self.pixels.shape[:2],np.uint8)*255)
        self.assertTrue(np.array_equal(result,self.pixels))
        self.assertEqual(worker.call_count,2)
        release.assert_called_once()

    def test_inpainting_retries_once_after_releasing_local_translator_on_memory_error(self):
        from unittest.mock import MagicMock
        engine=LocalEngines({'translation':{'adapter':'lmstudio','config':{'model':'renkavi-test','lms_cli':'cli'}},'inpainting':{'adapter':'anime_lama_torch','id':'anime','path':'model.ckpt'}})
        failing=MagicMock();failing.inpaint.side_effect=RuntimeError('DefaultCPUAllocator: not enough memory')
        recovered=MagicMock();recovered.inpaint.return_value=self.pixels
        with patch('backend.ai.anime_lama.AnimeLama',side_effect=[failing,recovered]),patch('backend.ai.local_llm.release_loaded',return_value=True) as release:
            output=engine._lama_crop(self.pixels,np.ones(self.pixels.shape[:2],np.uint8)*255)
        self.assertTrue(np.array_equal(output,self.pixels));release.assert_called_once();failing.close.assert_called_once()

    def setUp(self):
        rows, columns = np.indices((600, 900))
        self.pixels = np.stack([30 + columns % 40, 40 + rows % 30, 60 + columns % 20], axis=2).astype(np.uint8)
        self.image = Image.fromarray(self.pixels)
        self.engine = LocalEngines()
        self.calls = []

    def neural(self, rgb, mask):
        self.engine.inpaint_method = "anime_lama_torch"
        self.calls.append((rgb.shape, mask.copy()))
        # Deliberately alter context too: clean must only accept masked pixels.
        return np.full_like(rgb, (201, 202, 203))

    @staticmethod
    def stroke(mode, point, radius=8):
        return {"mode": mode, "radius": radius, "points": [list(point)]}

    def test_paint_uses_selected_neural_model_on_local_context(self):
        with patch.object(self.engine, "_lama_crop", side_effect=self.neural):
            cleaned, mask, counts = self.engine.clean(self.image, [], strokes=[self.stroke("paint", (450, 300))])
        self.assertEqual(tuple(np.asarray(cleaned)[300, 450]), (201, 202, 203))
        self.assertEqual(np.asarray(mask)[300, 450], 255)
        self.assertEqual(counts["anime_lama"], 1)
        self.assertTrue(self.calls)
        self.assertLess(max(self.calls[0][0][:2]), 250)
        self.assertTrue(np.array_equal(np.asarray(cleaned)[np.asarray(mask) == 0], self.pixels[np.asarray(mask) == 0]))
        self.assertTrue(np.array_equal(np.asarray(self.image), self.pixels))

    def test_erase_restores_original_inside_automatic_region(self):
        region = {"box": [100, 100, 180, 150], "status": "translated"}
        with patch.object(self.engine, "_lama_crop", side_effect=self.neural):
            cleaned, mask, _ = self.engine.clean(self.image, [region], strokes=[self.stroke("erase", (120, 120))])
        output, mask_array = np.asarray(cleaned), np.asarray(mask)
        self.assertEqual(mask_array[120, 120], 0)
        np.testing.assert_array_equal(output[120, 120], self.pixels[120, 120])
        self.assertEqual(mask_array[120, 150], 255)
        self.assertFalse(np.array_equal(output[120, 150], self.pixels[120, 150]))
        np.testing.assert_array_equal(output[mask_array == 0], self.pixels[mask_array == 0])

    def test_ordered_paint_erase_paint_preserves_hole_and_bounds_large_stroke(self):
        strokes = [self.stroke("paint", (450, 300), 12), self.stroke("erase", (450, 300), 8), self.stroke("paint", (450, 300), 2)]
        with patch.object(self.engine, "_lama_crop", side_effect=self.neural):
            cleaned, mask, _ = self.engine.clean(self.image, [], strokes=strokes)
        self.assertEqual(np.asarray(mask)[300, 450], 255)
        self.assertEqual(np.asarray(mask)[300, 455], 0)
        self.assertEqual(np.asarray(mask)[300, 460], 255)
        np.testing.assert_array_equal(np.asarray(cleaned)[300, 455], self.pixels[300, 455])
        self.calls.clear()
        with patch.object(self.engine, "_lama_crop", side_effect=self.neural):
            self.engine.clean(self.image, [], strokes=[{"mode": "paint", "radius": 4, "points": [[10, 300], [890, 300]]}])
        self.assertGreater(len(self.calls), 1)
        self.assertTrue(all(max(shape[:2]) <= 1024 for shape, _ in self.calls))

    def test_pipeline_forwards_persisted_strokes_and_keeps_three_argument_engines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "page.png"
            self.image.save(source)
            store = ProjectStore(root / "library")
            chapter = import_source(store, source)
            chapter["pages"][0]["mask_strokes"] = [self.stroke("paint", (450, 300))]
            chapter = store.save(chapter, chapter["revision"])
            manager = PipelineManager(store, lambda: self.engine)
            with patch.object(self.engine, "_lama_crop", side_effect=self.neural):
                job = manager.start(chapter["project_id"], {"stages": ["inpaint"], "expected_revision": chapter["revision"]})
                self.assertTrue(manager.wait(job["id"], timeout=5))
                result = manager.get(chapter["project_id"], job["id"])
            self.assertEqual(result["state"], "completed", result.get("error"))
            with Image.open(output_file(store, chapter["project_id"], chapter["pages"][0]["id"], "cleaned")) as cleaned:
                self.assertEqual(cleaned.getpixel((450, 300)), (201, 202, 203))
            self.assertEqual(store.load(chapter["project_id"])["pages"][0]["mask_strokes"], chapter["pages"][0]["mask_strokes"])

            class ThreeArguments:
                def clean(self, image, regions, progress):
                    return image.copy(), Image.new("L", image.size), {"flat": 0}

            second = import_source(store, source)
            compatible = PipelineManager(store, ThreeArguments)
            job = compatible.start(second["project_id"], {"stages": ["inpaint"], "expected_revision": second["revision"]})
            self.assertTrue(compatible.wait(job["id"], timeout=5))
            self.assertEqual(compatible.get(second["project_id"], job["id"])["state"], "completed")


if __name__ == "__main__":
    unittest.main()
