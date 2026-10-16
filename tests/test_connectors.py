import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from backend.ai import connectors,local_llm,registry


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]
        received=self.requests
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,data,status=200):
                raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                received.append((self.path,self.headers.get('Authorization')))
                self.reply({'data':[{'id':'test-translator'}]})
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path,data))
                rows=json.loads(data['messages'][1]['content'])
                self.reply({'choices':[{'message':{'content':json.dumps([{'id':r['id'],'text':'Hola mundo'} for r in rows])}}]})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.data={'name':'Test Ollama','provider':'ollama','base_url':f'http://127.0.0.1:{self.server.server_port}/v1','model':'test-translator'}

    def test_model_discovery_and_chat_translation_use_standard_paths(self):
        self.assertEqual(connectors.test_connection(self.data)['models'],['test-translator'])
        config=connectors.validate(self.data);config['model_key']=config['model']
        regions=[{'id':'balloon','source_text':'Hello world','target_text':''}]
        with patch.object(local_llm,'ensure_loaded',side_effect=AssertionError('Must not start LM Studio')):
            local_llm.translate(regions,'en','es',config)
        self.assertEqual(regions[0]['target_text'],'Hola mundo')
        self.assertEqual(self.requests[0][0],'/v1/models')
        self.assertEqual(self.requests[1][0],'/v1/chat/completions')
        self.assertIn('to es',self.requests[1][1]['messages'][0]['content'])

    def test_connector_add_preserves_active_model_and_never_persists_raw_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            before=registry.read_catalog(root)['selected']
            with patch.object(connectors,'secret_directory',return_value=root/'secrets'):
                result=connectors.add_connector({**self.data,'api_key':'private-test-token'},root)
                model=result['models'][-1]
                raw=(root/'registry.json').read_text()
                self.assertNotIn('private-test-token',raw)
                self.assertEqual(connectors.auth_headers(model['config'])['Authorization'],'Bearer private-test-token')
                self.assertEqual(result['selected'],before)
                self.assertNotIn('api_key',model['config'])

    def test_auth_env_and_unknown_model_are_explicit(self):
        with patch.dict('os.environ',{'RENKAVI_TEST_TOKEN':'test-token'}):
            connectors.test_connection({**self.data,'api_key_env':'RENKAVI_TEST_TOKEN'})
        self.assertEqual(self.requests[0][1],'Bearer test-token')
        with self.assertRaisesRegex(ValueError,'pas annoncé'):
            connectors.test_connection({**self.data,'model':'missing-model'})

    def test_unsafe_endpoints_and_invalid_alignment_are_rejected(self):
        for value in ['http://remote.example/v1','https://user:secret@example.org/v1','https://example.org/v1?key=secret']:
            with self.assertRaises(ValueError):connectors.endpoint(value)
        with patch.object(connectors,'request',return_value={'choices':[{'message':{'content':'[{"id":"wrong","text":"x"}]'}}]}):
            with self.assertRaises(ValueError):connectors.translate_compatible([{'id':'a','source_text':'x'}],'en','fr',{'model':'m','model_key':'m'})


if __name__=='__main__':unittest.main()
