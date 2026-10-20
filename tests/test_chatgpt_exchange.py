import copy
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from backend.projects.store import ProjectStore
from backend.projects.importer import import_source
from backend.projects import chatgpt_exchange as exchange
from backend.core.project import RevisionConflict


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);image=root/'page.png';Image.new('RGB',(300,200),'white').save(image)
        self.store=ProjectStore(root/'library');p=import_source(self.store,image,source_language='en',target_language='fr')
        p['pages'][0]['regions']=[{'id':str(i),'source_text':s,'target_text':t,'status':'translated' if t else 'detected',
          'box':[10,10+i*30,250,35+i*30],'confidence_detection':None,'confidence_ocr':.99,'translation_manual':manual}
          for i,(s,t,manual) in enumerate([('Hello.','',False),('Run!','Courir !',False),('My name is Ada.','Je suis Ada.',True)])]
        self.project=self.store.save(p,p['revision']);self.pid=p['project_id']

    def response(self):
        info=exchange.export_request(self.store,self.pid,self.project['revision'])
        payload=json.loads(Path(info['response_template_path']).read_text(encoding='utf-8'))
        for row,text in zip(payload['translations'],['Bonjour.','Cours !']):row['translation']=text
        return info,payload

    def test_round_trip_preview_backup_and_manual_protection(self):
        info,response=self.response()
        preview=exchange.import_response(self.store,self.pid,response,self.project['revision'],dry_run=True)
        self.assertEqual(preview['count'],2)
        self.assertEqual(self.store.load(self.pid),self.project)
        result=exchange.import_response(self.store,self.pid,response,self.project['revision'])
        rows=result['project']['pages'][0]['regions']
        self.assertEqual([r['target_text'] for r in rows],['Bonjour.','Cours !','Je suis Ada.'])
        self.assertEqual(rows[0]['translation_engine'],'chatgpt-json')
        self.assertTrue(Path(result['backup_path']).is_file())

    def test_missing_duplicate_foreign_and_blank_rows_are_atomic(self):
        _,response=self.response()
        variants=[]
        bad=copy.deepcopy(response);bad['translations'].pop();variants.append(bad)
        bad=copy.deepcopy(response);bad['translations'][1]=copy.deepcopy(bad['translations'][0]);variants.append(bad)
        bad=copy.deepcopy(response);bad['translations'][0]['region_id']='foreign';variants.append(bad)
        bad=copy.deepcopy(response);bad['translations'][0]['translation']=' ';variants.append(bad)
        bad=copy.deepcopy(response);bad['project_id']='foreign';variants.append(bad)
        for payload in variants:
            with self.assertRaises(ValueError):exchange.import_response(self.store,self.pid,payload,self.project['revision'])
            self.assertEqual(self.store.load(self.pid),self.project)

    def test_stale_exchange_is_rejected_even_with_current_revision(self):
        _,response=self.response();p=self.store.load(self.pid)
        p['pages'][0]['regions'][0]['source_text']='Changed OCR.';p=self.store.save(p,p['revision'])
        with self.assertRaises(RevisionConflict):exchange.import_response(self.store,self.pid,response,p['revision'])

    def test_exchange_identifier_cannot_escape_its_directory(self):
        _,response=self.response();response['exchange_id']='../../project'
        with self.assertRaises(ValueError):exchange.import_response(self.store,self.pid,response,self.project['revision'])

    def test_request_contains_full_context_and_read_only_manual_text(self):
        info,_=self.response();request=json.loads(Path(info['request_path']).read_text(encoding='utf-8'))
        self.assertEqual(len(request['pages'][0]['regions']),3)
        self.assertFalse(request['pages'][0]['regions'][2]['translate'])
        self.assertEqual(len(request['response_template']['translations']),2)

    def test_http_export_preview_import_and_local_translation_guard(self):
        import http.client,threading
        from unittest.mock import patch
        from backend.api.server import make_server
        from backend.settings import Settings
        server=make_server(self.store,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        Settings(self.store.data_dir).save({'translation_mode':'chatgpt'})
        def request(method,path,data=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port)
            c.request(method,path,body=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type':'application/json','X-Renkavi-Client':'1'})
            r=c.getresponse();payload=r.read();status=r.status;c.close();return status,json.loads(payload)
        base=f'/api/projects/{self.pid}'
        with patch('backend.ai.local_llm.ensure_loaded',side_effect=AssertionError('No local LLM')):
            status,data=request('POST',base+'/chatgpt/export',{'expected_revision':self.project['revision']})
            self.assertEqual(status,201,data)
            response=json.loads(Path(data['exchange']['response_template_path']).read_text(encoding='utf-8'))
            for row,text in zip(response['translations'],['Bonjour.','Cours !']):row['translation']=text
            status,preview=request('POST',base+'/chatgpt/import',{'expected_revision':self.project['revision'],'response':response,'dry_run':True})
            self.assertEqual(status,200,preview);self.assertEqual(preview['count'],2)
            status,result=request('POST',base+'/chatgpt/import',{'expected_revision':self.project['revision'],'response':preview['response']})
            self.assertEqual(status,200,result)
            status,blocked=request('POST',base+'/process',{'expected_revision':result['project']['revision'],'stages':['translate']})
            self.assertEqual(status,400,blocked);self.assertIn('Mode ChatGPT',blocked['error']['message'])


if __name__=='__main__':unittest.main()
