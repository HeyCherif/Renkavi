import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from PIL import Image
from backend.api.server import make_server
from backend.projects.store import ProjectStore, atomic_json
from backend.projects.importer import import_source
from backend.projects.workspaces import Workspaces
from scripts.migrate_layout_categories import migrate

class ProjectInboxLayoutTests(unittest.TestCase):
    def test_imports_without_workspace_are_in_one_inbox(self):
        with tempfile.TemporaryDirectory() as temp:
            from unittest.mock import patch
            from test_batch import Engines
            from backend.ai.pipeline import PipelineManager
            import http.client
            store=ProjectStore(Path(temp))
            with patch('backend.api.server.PipelineManager',return_value=PipelineManager(store,Engines)):
                server=make_server(store,port=0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                image=io.BytesIO(); Image.new('RGB',(80,40),'white').save(image,'PNG')
                ids=[]
                for chapter in ('Chapter1.png','Chapter2.png'):
                    c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=12)
                    c.request('POST','/api/import?filename='+chapter,image.getvalue(),{'X-Renkavi-Client':'1'})
                    res=c.getresponse();body=res.read();self.assertEqual(res.status,201,body);ids.append(json.loads(body)['project']['project_id']);c.close()
                workspaces=Workspaces(store).list()
                self.assertEqual(len(workspaces),1)
                self.assertEqual(workspaces[0]['title'],'À classer')
                self.assertEqual({x['project_id'] for x in workspaces[0]['chapters']},set(ids))
                for pid in ids:
                    root=store.project_path(pid)
                    for part in ('original','traduction','export'):
                        self.assertTrue((root/part).is_dir())
                    self.assertTrue((root/'traduction/dialogues.json').is_file())
            finally:
                server.shutdown();server.server_close()

    def test_legacy_chapter_migrates_into_human_folders_without_data_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=ProjectStore(root)
            original=root/'sample.png';Image.new('RGB',(75,45),'white').save(original)
            chapter=import_source(store,original)
            wid=Workspaces(store).create('My series')['id'];Workspaces(store).attach(wid,chapter['project_id'])
            current=store.project_path(chapter['project_id']);manifest=store.load(chapter['project_id'])
            (current/'original').rename(current/'originals')
            manifest['pages'][0]['original']=manifest['pages'][0]['original'].replace('original/','originals/')
            atomic_json(current/'project.json',manifest)
            report=migrate(root)
            self.assertEqual(report['failures'],[])
            loaded=store.load(chapter['project_id'])
            self.assertEqual(loaded['pages'][0]['original'],'original/0001.png')
            self.assertTrue((current/'traduction/dialogues.json').exists())
            self.assertEqual((current/'original/0001.png').read_bytes(),original.read_bytes())
            self.assertEqual(Workspaces(store).get(wid)['chapter_count'],1)

if __name__=='__main__':unittest.main()
