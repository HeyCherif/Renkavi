import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from reportlab.pdfgen import canvas
from backend.ai.pipeline import PipelineManager
from backend.api.server import make_server
from backend.projects.formats import detect_source
from backend.projects.store import ProjectStore
from test_batch import Engines
from test_documents_word import CONTENT_TYPES, document, paragraph


class DocumentBatchApiTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.store=ProjectStore(self.root/'library')
        self.pipeline=PipelineManager(self.store,Engines)
        with patch('backend.api.server.PipelineManager',return_value=self.pipeline):
            self.server=make_server(self.store,port=0)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.workspace=self.json('POST','/api/workspaces',{'title':'Documents mixtes'})['workspace']

    def request(self,method,url,data=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=20)
        payload=json.dumps(data).encode() if isinstance(data,dict) else data
        connection.request(method,url,body=payload,headers={'X-Renkavi-Client':'1'})
        response=connection.getresponse();result=(response.status,dict(response.getheaders()),response.read())
        connection.close();return result

    def json(self,method,url,data=None):
        status,_,body=self.request(method,url,data)
        self.assertIn(status,(200,201,202),body)
        return json.loads(body)

    def files(self):
        image=io.BytesIO();Image.new('RGB',(400,200),'white').save(image,format='PNG')
        pdf=io.BytesIO();writer=canvas.Canvas(pdf,pagesize=(240,300));writer.drawString(20,250,'Hello document');writer.save()
        word=io.BytesIO()
        with zipfile.ZipFile(word,'w') as archive:
            archive.writestr('[Content_Types].xml',CONTENT_TYPES)
            archive.writestr('word/document.xml',document(paragraph('Hello editable Word document')))
        result=[]
        for name,content in [('Photo.png',image.getvalue()),('Rapport.pdf',pdf.getvalue()),('Texte.docx',word.getvalue())]:
            result.append(self.json('POST',f"/api/import?filename={name}&workspace_id={self.workspace['id']}&source_language=en&target_language=fr",content)['project'])
        return result

    def batch(self,data):
        batch=self.json('POST','/api/batches',data)['batch']
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            batch=self.json('GET',f"/api/batches/{batch['id']}")['batch']
            if batch['state'] in {'completed','failed','completed_with_errors'}:return batch
            threading.Event().wait(.03)
        self.fail('Batch timeout')

    def test_mixed_project_processes_every_file_and_native_exports(self):
        files=self.files()
        batch=self.batch({'workspace_id':self.workspace['id'],'scope':'all','project_ids':[files[0]['project_id']]})
        self.assertEqual(batch['state'],'completed',batch)
        self.assertEqual(len(batch['items']),3)
        self.assertEqual({i['source_format'] for i in batch['items']},{'png','pdf','docx'})
        for project,extension in zip(files,('.png','.pdf','.docx')):
            status,headers,body=self.request('GET',f"/api/projects/{project['project_id']}/export")
            self.assertEqual(status,200,body[:300]);self.assertIn(extension,headers['Content-Disposition'])
            if extension=='.png':
                with Image.open(io.BytesIO(body)) as image:self.assertEqual(image.size,(400,200))
            elif extension=='.pdf':self.assertTrue(body.startswith(b'%PDF-'))
            else:
                with zipfile.ZipFile(io.BytesIO(body)) as archive:self.assertIn(b'Bonjour',archive.read('word/document.xml'))
        status,headers,body=self.request('GET',f"/api/workspaces/{self.workspace['id']}/export")
        self.assertEqual(status,200,body[:200]);self.assertIn('.zip',headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual({Path(n).suffix for n in archive.namelist()},{'.png','.pdf','.docx'})

    def test_selected_scope_and_foreign_membership(self):
        files=self.files()
        batch=self.batch({'workspace_id':self.workspace['id'],'scope':'selected','project_ids':[files[-1]['project_id']]})
        self.assertEqual(batch['state'],'completed',batch)
        self.assertEqual(len(batch['items']),1)
        status,_,_=self.request('POST','/api/batches',{'workspace_id':self.workspace['id'],'scope':'selected','project_ids':['foreign']})
        self.assertEqual(status,400)
        status,_,_=self.request('GET',f"/api/workspaces/{self.workspace['id']}/export")
        self.assertIn(status,(400,404))

    def test_content_detection_overrides_extension_and_old_doc_error(self):
        image=self.root/'looks-like.pdf';Image.new('RGB',(20,20)).save(image,format='JPEG')
        self.assertEqual(detect_source(image),'jpeg')
        project=self.json('POST','/api/import?filename=mislabeled.pdf',image.read_bytes())['project']
        self.assertEqual(project['provenance']['source_format'],'jpeg')
        self.assertTrue(project['pages'][0]['original'].endswith('.jpg'))
        status,_,body=self.request('POST','/api/import?filename=ancien.doc',b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1test')
        self.assertEqual(status,400);self.assertIn('docx',body.decode())

    def test_original_docx_visual_preview_source_and_vendored_renderer(self):
        files=self.files()
        word=files[-1]
        saved=self.store.project_path(word['project_id'])/word['provenance']['source_file']
        status, headers, body=self.request('GET',f"/api/projects/{word['project_id']}/source-docx")
        self.assertEqual(status,200)
        self.assertEqual(body,saved.read_bytes())
        self.assertNotIn('attachment',headers.get('Content-Disposition',''))
        status,_,_=self.request('GET',f"/api/projects/{files[0]['project_id']}/source-docx")
        self.assertEqual(status,400)
        for resource in ('jszip.min.js','docx-preview.min.js'):
            status,_,body=self.request('GET',f'/vendor/{resource}')
            self.assertEqual(status,200)
            self.assertGreater(len(body),20000)

    def test_docx_never_uses_ocr_or_neural_cleaning(self):
        project=self.files()[-1]
        class WordEngines(Engines):
            def detect(self,*args):raise AssertionError('Word must not use OCR')
            def clean(self,*args,**kwargs):raise AssertionError('Word must not use inpainting')
        self.pipeline.engine_factory=WordEngines
        batch=self.batch({'project_ids':[project['project_id']]})
        self.assertEqual(batch['state'],'completed',batch)

    def test_mixed_failure_continues_with_pdf_and_word(self):
        files = self.files()

        class FailingFirstOCR(Engines):
            attempted = False

            def detect(self, *args):
                if not self.attempted:
                    self.attempted = True
                    raise RuntimeError('OCR sample failure')
                return super().detect(*args)

        self.pipeline.engine_factory = FailingFirstOCR
        batch = self.batch({'workspace_id': self.workspace['id'], 'scope': 'all'})
        self.assertEqual(batch['state'], 'completed_with_errors', batch)
        self.assertEqual([item['state'] for item in batch['items']], ['failed', 'completed', 'completed'])
        self.assertFalse(batch['items'][0]['export_ready'])
        for item in batch['items'][1:]:
            self.assertTrue(item['export_ready'], item)
        for project, suffix in zip(files[1:], ('.pdf', '.docx')):
            status, headers, output = self.request('GET', f"/api/projects/{project['project_id']}/export")
            self.assertEqual(status, 200, output[:200])
            self.assertIn(suffix, headers['Content-Disposition'])
        status, _, _ = self.request('GET', f"/api/workspaces/{self.workspace['id']}/export")
        self.assertNotEqual(status, 200, 'Incomplete workspace must not produce a misleading ZIP')

    def test_native_preparation_and_real_project_folder(self):
        files = self.files()
        workspace_dir = Path(self.workspace['folder'])
        self.assertTrue(workspace_dir.is_dir())
        for project in files:
            self.assertTrue((workspace_dir / 'chapters' / project['project_id'] / 'project.json').is_file())
        batch = self.batch({'workspace_id': self.workspace['id'], 'scope': 'all'})
        self.assertEqual(batch['state'], 'completed', batch)
        for project, ext in zip(files, ('.png', '.pdf', '.docx')):
            route = f"/api/projects/{project['project_id']}/export?prepare=1"
            result = self.json('GET', route)
            self.assertGreater(result['size'], 0)
            self.assertTrue(result['filename'].endswith(ext))
            self.assertIn('/export', result['download_url'])
            status, headers, body = self.request('GET', result['download_url'])
            self.assertEqual(status, 200)
            self.assertEqual(len(body), result['size'])
        prepared = self.json('GET', f"/api/workspaces/{self.workspace['id']}/export?prepare=1")
        self.assertTrue(prepared['filename'].endswith('.zip'))
        status, _, body = self.request('GET', prepared['download_url'])
        self.assertEqual(status, 200)
        self.assertEqual(len(body), prepared['size'])
