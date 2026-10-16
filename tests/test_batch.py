import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from PIL import Image
from backend.ai.pipeline import PipelineManager
from backend.projects.importer import import_source
from backend.projects.store import ProjectStore,atomic_json
from test_ai_pipeline import FakeEngines


class Engines(FakeEngines):
    def detect(self,*args):
        rows=super().detect(*args)
        rows[0]['id']=str(uuid.uuid4())
        return rows


class BatchTests(unittest.TestCase):
    def setUp(self):
        from backend.ai.batch import BatchManager
        self.BatchManager=BatchManager
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=ProjectStore(self.root/'library')
        self.projects=[]
        for index in range(2):
            path=self.root/f'page{index}.png';Image.new('RGB',(200,100),'white').save(path)
            self.projects.append(import_source(self.store,path,source_language='en',target_language='fr'))

    def wait(self,manager,bid,states=('completed','completed_with_errors','failed','paused','cancelled')):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            result=manager.get(bid)
            if result['state'] in states:return result
            threading.Event().wait(.02)
        self.fail('Batch did not finish')

    def test_serial_batch_exports_and_repeat_skips_pages(self):
        pipeline=PipelineManager(self.store,Engines);manager=self.BatchManager(pipeline)
        ids=[p['project_id'] for p in self.projects]
        batch=self.wait(manager,manager.start({'project_ids':ids})['id'])
        self.assertEqual(batch['state'],'completed',batch.get('message'))
        self.assertTrue(all(i['export_ready'] for i in batch['items']))
        again=self.wait(manager,manager.start({'project_ids':ids})['id'])
        self.assertTrue(all(i['pages_skipped']==1 for i in again['items']))

    def test_failure_does_not_block_next_chapter(self):
        class Failing(Engines):
            failed=False
            def detect(self,*args):
                if not Failing.failed:Failing.failed=True;raise RuntimeError('Broken OCR sample')
                return super().detect(*args)
        manager=self.BatchManager(PipelineManager(self.store,Failing))
        batch=self.wait(manager,manager.start({'project_ids':[p['project_id'] for p in self.projects]})['id'])
        self.assertEqual(batch['state'],'completed_with_errors')
        self.assertEqual([i['state'] for i in batch['items']],['failed','completed'])

    def test_memory_failure_pauses_queue_and_releases_engines(self):
        closed=threading.Event()
        class Exhausted(Engines):
            def detect(self,*args): raise RuntimeError('ONNXRuntime: bad allocation')
            def close(self): closed.set()
        pipeline=PipelineManager(self.store,Exhausted)
        manager=self.BatchManager(pipeline)
        batch=self.wait(manager,manager.start({'project_ids':[p['project_id'] for p in self.projects]})['id'])
        self.assertEqual(batch['state'],'paused')
        self.assertEqual([i['state'] for i in batch['items']],['failed','pending'])
        self.assertEqual(batch['failure_kind'],'memory')
        self.assertTrue(closed.is_set())
        self.assertIsNone(pipeline.engines)
        self.assertIsNone(pipeline.batch_owner)

    def test_resume_refreshes_selected_model_snapshot(self):
        from unittest.mock import patch
        pipeline=PipelineManager(self.store)
        manager=self.BatchManager(pipeline)
        bid=str(uuid.uuid4())
        batch={'id':bid,'state':'paused','models':{'translation':{'id':'old'}},'items':[{'state':'failed'}]}
        manager.batches[bid]=batch
        with patch('backend.ai.registry.selected_model',side_effect=lambda kind:{'id':'new-'+kind}),patch.object(manager,'_launch'):
            manager.control(bid,'resume')
        self.assertEqual(batch['models']['translation']['id'],'new-translation')

    def test_active_batch_remains_visible_after_history_limit(self):
        manager=self.BatchManager(PipelineManager(self.store,Engines))
        for index in range(25):
            bid=str(index);manager.batches[bid]={'id':bid,'state':'completed','created_at':f'{index:03}','items':[]}
        manager.active='0';manager.batches['0']['state']='running'
        self.assertIn('0',[b['id'] for b in manager.list()])

    def test_pause_resume_and_gpu_ownership(self):
        entered=threading.Event();release=threading.Event()
        class Blocking(Engines):
            def clean(self,*args):
                entered.set();release.wait(5)
                return super().clean(*args)
        pipeline=PipelineManager(self.store,Blocking);manager=self.BatchManager(pipeline)
        batch=manager.start({'project_ids':[p['project_id'] for p in self.projects]})
        self.assertTrue(entered.wait(3))
        with self.assertRaises(ValueError):pipeline.start(self.projects[1]['project_id'],{'expected_revision':self.projects[1]['revision']})
        manager.control(batch['id'],'pause');release.set()
        paused=self.wait(manager,batch['id'],('paused',))
        self.assertIsNone(pipeline.batch_owner)
        self.assertNotEqual(paused['items'][1]['state'],'completed')
        manager.control(batch['id'],'resume')
        self.assertEqual(self.wait(manager,batch['id'])['state'],'completed')

    def test_restart_marks_running_batch_interrupted(self):
        directory=self.store.data_dir/'batches';bid=str(uuid.uuid4())
        atomic_json(directory/f'{bid}.json',{'id':bid,'state':'running','items':[{'state':'running'}],'created_at':'2026-10-14'})
        manager=self.BatchManager(PipelineManager(self.store,Engines))
        result=manager.get(bid)
        self.assertEqual(result['state'],'interrupted')
        self.assertEqual(result['items'][0]['state'],'pending')

    def test_cancel_finishes_current_page_and_preserves_export(self):
        entered=threading.Event();release=threading.Event()
        class Blocking(Engines):
            def clean(self,*args):
                entered.set();release.wait(5)
                return super().clean(*args)
        manager=self.BatchManager(PipelineManager(self.store,Blocking))
        batch=manager.start({'project_ids':[p['project_id'] for p in self.projects]})
        self.assertTrue(entered.wait(3))
        manager.control(batch['id'],'cancel');release.set()
        result=self.wait(manager,batch['id'],('cancelled',))
        self.assertEqual(result['items'][0]['state'],'completed')
        self.assertTrue(result['items'][0]['export_ready'])
        self.assertEqual(result['items'][1]['state'],'cancelled')
        self.assertEqual(self.store.load(self.projects[1]['project_id'])['pages'][0]['regions'],[])

    def test_duplicate_projects_are_rejected(self):
        manager=self.BatchManager(PipelineManager(self.store,Engines));pid=self.projects[0]['project_id']
        with self.assertRaises(ValueError):manager.start({'project_ids':[pid,pid]})


if __name__=='__main__':unittest.main()


class MissingSourceHistoryTests(unittest.TestCase):
    def test_vanished_project_does_not_offer_broken_download(self):
        from backend.ai.batch import BatchManager
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            store=ProjectStore(Path(temporary)/'library')
            image=Path(temporary)/'test.png'
            Image.new('RGB',(200,100),'white').save(image)
            project=import_source(store,image,source_language='en',target_language='fr')
            manager=BatchManager(PipelineManager(store,Engines))
            batch=manager.start({'project_ids':[project['project_id']]})
            deadline=time.monotonic()+12
            while manager.get(batch['id'])['state'] not in {'completed','failed'} and time.monotonic()<deadline:
                time.sleep(.03)
            self.assertEqual(manager.get(batch['id'])['state'],'completed')
            # Simulate a missing source without mutating any user library.
            shutil.rmtree(store.project_path(project['project_id']))
            view=manager.get(batch['id'])
            self.assertEqual(view['state'],'unavailable')
            self.assertEqual(view['items'][0]['state'],'missing')
            self.assertFalse(view['items'][0]['export_ready'])
            self.assertTrue(view['items'][0]['missing_source'])
            self.assertEqual(manager.batches[batch['id']]['state'],'completed')
