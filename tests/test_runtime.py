import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend import runtime


class RuntimeTests(unittest.TestCase):
    def test_source_keeps_existing_paths(self):
        with patch.dict(os.environ,{},clear=True),patch.object(sys,'frozen',False,create=True):
            self.assertEqual(runtime.default_data_dir(),runtime.resource_root()/'data')
            self.assertEqual(runtime.models_root(),runtime.resource_root()/'models')

    def test_frozen_resources_are_separate_from_user_state(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)/'user';resources=Path(folder)/'bundle'
            with patch.dict(os.environ,{'RENKAVI_HOME':str(home)},clear=True),patch.object(sys,'frozen',True,create=True),patch.object(sys,'_MEIPASS',str(resources),create=True):
                self.assertEqual(runtime.resource_root(),resources)
                self.assertEqual(runtime.models_root(),home/'models')
                self.assertEqual(runtime.default_data_dir(),home/'data')
                (home/'projects').mkdir(parents=True)
                self.assertEqual(runtime.default_data_dir(),home)

    def test_model_override(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'RENKAVI_MODELS_DIR':folder}):
            self.assertEqual(runtime.models_root(),Path(folder).resolve())
