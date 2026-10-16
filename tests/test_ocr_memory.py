import sys
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from backend.ai.engines import LocalEngines


class OCRMemoryTests(unittest.TestCase):
    def detect_with(self, image, ocr):
        engine = LocalEngines()
        with patch.dict(sys.modules, {"rapidocr": SimpleNamespace(RapidOCR=ocr)}):
            return engine.detect(image)

    def test_wide_bands_are_bounded_and_map_to_original_coordinates(self):
        sizes = []
        configs = []

        def factory(params):
            configs.append(params)

            def recognize(image, **kwargs):
                sizes.append(image.size)
                return SimpleNamespace(boxes=[[[10, 20], [50, 20], [50, 40], [10, 40]]],
                                       txts=["Hello world"], scores=[.91])
            return recognize

        with Image.new("L", (3200, 3200)) as image:
            regions = self.detect_with(image, factory)
        self.assertTrue(all(max(size) <= 1600 for size in sizes), sizes)
        self.assertEqual(sizes, [(1600, 800), (1600, 800), (1600, 160)])
        self.assertEqual([r["box"] for r in regions],
                         [[20, 40, 101, 81], [20, 1480, 101, 1521], [20, 2920, 101, 2961]])
        self.assertTrue(all(r["confidence_ocr"] == .91 for r in regions))
        self.assertEqual(configs[0]["Det.limit_type"], "max")
        self.assertEqual(configs[0]["Det.limit_side_len"], 1600)

    def test_previous_band_pixels_and_result_are_released_before_next_call(self):
        previous_images = []
        previous_results = []

        class Result:
            boxes = []
            txts = []
            scores = []

        def recognize(image, **kwargs):
            if previous_images:
                with self.assertRaises(ValueError):
                    previous_images[-1].getpixel((0, 0))
                self.assertIsNone(previous_results[-1]())
            previous_images.append(image)
            result = Result()
            previous_results.append(weakref.ref(result))
            return result

        with Image.new("L", (800, 3200)) as image:
            self.detect_with(image, lambda **kwargs: recognize)
        with self.assertRaises(ValueError):
            previous_images[-1].getpixel((0, 0))
        self.assertIsNone(previous_results[-1]())

    def test_overlap_deduplication_keeps_best_score_and_grouping(self):
        calls = []

        def recognize(image, **kwargs):
            index = len(calls)
            calls.append(image.size)
            y, score = (725, .8) if index == 0 else (5, .95)
            return SimpleNamespace(boxes=[[[10, y], [50, y], [50, y + 20], [10, y + 20]]],
                                   txts=["Hello world"], scores=[score])

        with Image.new("L", (3200, 1600)) as image:
            regions = self.detect_with(image, lambda **kwargs: recognize)
        self.assertEqual(len(regions), 1)
        self.assertEqual(len(regions[0]["lines"]), 1)
        self.assertEqual(regions[0]["box"], [20, 1450, 101, 1491])
        self.assertEqual(regions[0]["confidence_ocr"], .95)

    def test_inference_error_propagates_and_closes_band(self):
        seen = []

        def recognize(image, **kwargs):
            seen.append(image)
            raise RuntimeError("bad allocation")

        with Image.new("L", (800, 1800)) as image:
            with self.assertRaisesRegex(RuntimeError, "bad allocation"):
                self.detect_with(image, lambda **kwargs: recognize)
        self.assertEqual(len(seen), 1)
        with self.assertRaises(ValueError):
            seen[0].getpixel((0, 0))


if __name__ == "__main__":
    unittest.main()
