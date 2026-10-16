import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.ai.anime_lama import AnimeLama


class WorkerTests(unittest.TestCase):
    def test_startup_memory_error_preserves_protocol_diagnostic(self):
        class Process:
            stdout=io.BytesIO(b'{"ready":false,"error":"OSError: [WinError 1455] pagination insuffisante"}\n')
            stdin=io.BytesIO()
            def poll(self):return 1
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'models').mkdir()
            with patch('backend.ai.anime_lama.models_root',return_value=root/'models'),patch('backend.ai.anime_lama.worker_available',return_value=True),patch('backend.ai.anime_lama.subprocess.Popen',return_value=Process()):
                with self.assertRaisesRegex(RuntimeError,'WinError 1455'):
                    AnimeLama(Path('checkpoint'))

    def test_import_failure_is_reported_over_json_protocol(self):
        source=Path(__file__).resolve().parents[1]/'backend'/'ai'/'anime_lama_worker.py'
        code="""import builtins, runpy, sys
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name=='torch':raise OSError('[WinError 1455] paging file is too small')
    return original(name,*args,**kwargs)
builtins.__import__=guarded
runpy.run_path(sys.argv[1],run_name='__main__')
"""
        result=subprocess.run([sys.executable,'-c',code,str(source)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,1)
        response=json.loads(result.stdout)
        self.assertFalse(response['ready'])
        self.assertIn('WinError 1455',response['error'])

    def test_unresponsive_stdout_has_a_deadline(self):
        worker=AnimeLama.__new__(AnimeLama)
        event=threading.Event()
        class Output:
            def readline(self):event.wait(2);return b''
        worker.process=type('Process',(),{'stdout':Output(),'kill':lambda self:event.set(),'wait':lambda self,timeout:None})()
        try:
            with self.assertRaisesRegex(RuntimeError,'délai'):
                worker._read_response(.02)
        finally:event.set()
