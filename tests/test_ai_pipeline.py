import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from backend.ai.pipeline import PipelineManager, export_project, output_file
from backend.projects.importer import import_source
from backend.projects.store import ProjectStore


class FakeEngines:
    def detect(self, image, progress, source="auto"):
        return [{"id": "test-region", "box": [10, 10, 190, 90], "source_text": "Hello world", "target_text": "",
                 "status": "detected", "confidence_detection": None, "confidence_ocr": .9}]

    def translate(self, regions, source, target):
        for region in regions:
            if not region["target_text"]:
                region.update(target_text="Bonjour", status="translated")

    def clean(self, image, regions, progress):
        return image.copy(), Image.new("L", image.size, 0), {"flat": 1, "lama": 0}


class PipelineTests(unittest.TestCase):
    def test_context_review_refreshes_render_and_export_and_closes_engines(self):
        from unittest.mock import patch,Mock
        from backend.ai.pipeline import page_directory,read_meta
        engine=FakeEngines();engine.close=Mock()
        primary={'id':'gemma','name':'Gemma','adapter':'lmstudio','config':{'profile':'translategemma','model':'renkavi-test','model_key':'gemma','base_url':'http://127.0.0.1:1234'}}
        paint={'id':'paint','name':'Paint','adapter':'telea'}
        reviewer={'id':'review','name':'Qwen','adapter':'lmstudio','config':{'model':'renkavi-review','model_key':'qwen','base_url':'http://127.0.0.1:1234'}}
        snapshot={'translation':primary,'inpainting':paint,'review':reviewer}
        def review(p,*args,**kwargs):
            p['pages'][0]['regions'][0]['target_text']='Traduction corrigée'
            return {'checked':1,'corrected':1,'rejected':0}
        with patch('backend.ai.registry.selected_model',side_effect=lambda kind:snapshot[kind]),patch('backend.ai.engines.model_status',return_value={k:{'ready':True} for k in ('ocr','translation','inpainting')}),patch('backend.ai.engines.LocalEngines',return_value=engine),patch('backend.ai.context_review.review_project',side_effect=review),patch('backend.ai.local_llm.ensure_loaded'),patch('backend.ai.local_llm.release_loaded'):
            manager=PipelineManager(self.store)
            job=manager.start(self.project['project_id'],{'expected_revision':self.project['revision']},models_snapshot=snapshot)
            self.assertTrue(manager.wait(job['id'],10))
            result=manager.get(self.project['project_id'],job['id'])
        self.assertEqual(result['state'],'completed',result.get('error'))
        self.assertEqual(result['review']['corrected'],1)
        saved=self.store.load(self.project['project_id'])
        self.assertEqual(saved['pages'][0]['regions'][0]['target_text'],'Traduction corrigée')
        meta=read_meta(page_directory(self.store,saved['project_id'],saved['pages'][0]['id']))
        self.assertFalse(meta['review_pending'])
        self.assertTrue(output_file(self.store,saved['project_id'],saved['pages'][0]['id'],'rendered').is_file())
        self.assertTrue(export_project(self.store,saved['project_id']).is_file())
        engine.close.assert_called_once()

    def test_export_refuses_a_render_waiting_for_context_review(self):
        from backend.ai.pipeline import page_directory,read_meta
        from backend.projects.store import atomic_json
        manager=PipelineManager(self.store,FakeEngines)
        self.assertEqual(self.run_job(manager,['ocr','translate','render'])['state'],'completed')
        page=self.store.load(self.project['project_id'])['pages'][0]
        path=page_directory(self.store,self.project['project_id'],page['id'])/'meta.json'
        meta=read_meta(path.parent);meta['review_pending']=True;atomic_json(path,meta)
        with self.assertRaisesRegex(FileNotFoundError,'relecture'):output_file(self.store,self.project['project_id'],page['id'],'rendered')

    def test_failed_job_disposes_engine_sessions_before_releasing_ownership(self):
        from unittest.mock import Mock
        engine=FakeEngines();engine.detect=Mock(side_effect=RuntimeError('bad allocation'));engine.close=Mock()
        manager=PipelineManager(self.store,lambda:engine)
        result=self.run_job(manager,['ocr'])
        self.assertEqual(result['state'],'failed')
        self.assertTrue(manager.wait(result['id'],2))
        engine.close.assert_called_once()
        self.assertIsNone(manager.engines)

    def test_changed_model_regenerates_fresh_machine_draft_and_preserves_human_text(self):
        import copy
        from unittest.mock import patch
        from backend.ai.pipeline import project_context
        project=self.store.load(self.project['project_id'])
        original=FakeEngines().detect(None,None)[0]
        original.update(target_text='Previous model output',status='translated',translation_engine='old-model',translation_context_version=2)
        manual=copy.deepcopy(original);manual.update(id='manual',translation_manual=True,target_text='Human correction')
        reviewed=copy.deepcopy(original);reviewed.update(id='reviewed',status='reviewed',target_text='Reviewed text')
        project['pages'][0]['regions']=[original,manual,reviewed]
        context=project_context(project)
        for region in project['pages'][0]['regions']:region['translation_context_hash']=context
        self.store.save(project,project['revision'])
        translation={'id':'new-model-id','name':'New model','adapter':'lmstudio','config':{'model_key':'new-model'}}
        paint={'id':'paint','adapter':'telea','name':'Paint'}
        calls=[]
        class NewModel(FakeEngines):
            def translate(inner,regions,*args):
                calls.extend(r['id'] for r in regions if not r['target_text'])
                for region in regions:
                    if not region['target_text']:
                        region.update(target_text='New model output',translation_engine='new-model',status='translated')
        ready={part:{'ready':True} for part in ('ocr','translation','inpainting')}
        with patch('backend.ai.registry.selected_model',side_effect=lambda kind:translation if kind=='translation' else paint),patch('backend.ai.engines.model_status',return_value=ready),patch('backend.ai.engines.LocalEngines',return_value=NewModel()):
            job=self.run_job(PipelineManager(self.store),['translate'])
        self.assertEqual(job['state'],'completed',job.get('error'))
        regions=self.store.load(project['project_id'])['pages'][0]['regions']
        self.assertEqual(calls,[original['id']])
        self.assertEqual([r['target_text'] for r in regions],['New model output','Human correction','Reviewed text'])
        self.assertEqual(regions[0]['translation_context_hash'],context)

    def test_docx_translate_render_repeat_uses_complete_cache(self):
        from tests.test_documents_word import CONTENT_TYPES,document,paragraph
        source=self.root/'document.docx'
        with zipfile.ZipFile(source,'w') as archive:
            archive.writestr('[Content_Types].xml',CONTENT_TYPES)
            archive.writestr('word/document.xml',document(paragraph('Hello world')))
        self.project=import_source(self.store,source,source_language='en',target_language='fr')
        stages=['translate','render']
        self.assertEqual(self.run_job(PipelineManager(self.store,FakeEngines),stages)['state'],'completed')
        unavailable=lambda: (_ for _ in ()).throw(AssertionError('inference runtime accessed'))
        job=self.run_job(PipelineManager(self.store,unavailable),stages,skip_existing=True)
        self.assertEqual(job['state'],'completed',job.get('error'))
        self.assertEqual(job['pages_skipped'],len(self.project['pages']))
        project=self.store.load(self.project['project_id'])
        project['translation_context']='An edited context note invalidates the translation.'
        self.store.save(project,project['revision'])
        stale=self.run_job(PipelineManager(self.store,unavailable),stages,skip_existing=True)
        self.assertEqual(stale['state'],'failed')
        self.assertEqual(stale['pages_skipped'],0)

    def test_five_page_repeat_hashes_context_twice_without_any_inference(self):
        import uuid
        from unittest.mock import patch
        from backend.ai.pipeline import project_context
        source=self.root/'chapter.cbz'
        image=(self.root/'sample.png').read_bytes()
        with zipfile.ZipFile(source,'w') as archive:
            for index in range(5):archive.writestr(f'{index:03}.png',image)
        self.project=import_source(self.store,source,source_language='en',target_language='fr')
        class UniqueEngines(FakeEngines):
            def detect(inner,*args):
                rows=super().detect(*args);rows[0]['id']=str(uuid.uuid4());return rows
        manager=PipelineManager(self.store,UniqueEngines)
        self.assertEqual(self.run_job(manager,['ocr','translate','inpaint','render'])['state'],'completed')
        fresh=PipelineManager(self.store,lambda: (_ for _ in ()).throw(AssertionError('inference runtime accessed')))
        with patch('backend.ai.pipeline.project_context',wraps=project_context) as hash_context:
            job=self.run_job(fresh,['ocr','translate','inpaint','render'],skip_existing=True)
        self.assertEqual(job['state'],'completed',job.get('error'))
        self.assertEqual(job['pages_skipped'],5)
        self.assertEqual(hash_context.call_count,2)

    def test_valid_repeat_does_not_initialize_or_check_engines(self):
        from unittest.mock import patch
        models={'id':'test','adapter':'test','name':'Test','config':{}}
        ready={part:{'ready':True} for part in ('ocr','translation','inpainting')}
        with patch('backend.ai.registry.selected_model',return_value=models):
            with patch('backend.ai.engines.LocalEngines',return_value=FakeEngines()),patch('backend.ai.engines.model_status',return_value=ready):
                manager=PipelineManager(self.store)
                self.assertEqual(self.run_job(manager,['ocr','translate','inpaint','render'])['state'],'completed')
            # A fresh application reuses artifacts even when no inference runtime is available.
            with patch('backend.ai.engines.LocalEngines',side_effect=AssertionError('engine initialized')),patch('backend.ai.engines.model_status',side_effect=AssertionError('runtime checked')):
                job=self.run_job(PipelineManager(self.store),['ocr','translate','inpaint','render'],skip_existing=True)
        self.assertEqual(job['state'],'completed',job.get('error'))
        self.assertEqual(job['pages_skipped'],1)

    def test_render_only_cannot_certify_a_stale_machine_translation(self):
        project=self.store.load(self.project['project_id'])
        project['pages'][0]['regions']=FakeEngines().detect(None,None)
        project['pages'][0]['regions'][0].update(target_text='Ancienne traduction',status='translated',translation_engine='hy-mt',translation_context_version=2,translation_context_hash='stale')
        project=self.store.save(project,project['revision'])
        manager=PipelineManager(self.store,FakeEngines)
        with self.assertRaisesRegex(ValueError,'contexte'):
            manager.start(project['project_id'],{'expected_revision':project['revision'],'stages':['render']})
    def test_saved_story_notes_invalidate_context_freshness(self):
        from backend.ai.pipeline import project_context
        project=self.store.load(self.project['project_id']);before=project_context(project)
        project['translation_context']='The narrator is the older brother.'
        self.assertNotEqual(before,project_context(project))
    def test_blank_page_ocr_is_cached_on_repeat_full_run(self):
        class BlankEngines(FakeEngines):
            detections=0
            def detect(inner,*args):inner.detections+=1;return []
        manager=PipelineManager(self.store,BlankEngines)
        stages=['ocr','translate','inpaint','render']
        self.assertEqual(self.run_job(manager,stages)['state'],'completed')
        again=self.run_job(manager,stages,skip_existing=True)
        self.assertEqual(again['state'],'completed',again.get('error'))
        self.assertEqual(again['pages_skipped'],1)
        self.assertEqual(manager.engines.detections,1)
    def test_neighbor_source_edit_invalidates_context_cache_and_output(self):
        import copy,uuid
        from backend.ai.pipeline import processing_key
        project=self.store.load(self.project['project_id'])
        page=copy.deepcopy(project['pages'][0]);page.update(id=str(uuid.uuid4()),order=1,name='next.png',original='originals/0002.png')
        project['pages'].append(page)
        before=processing_key({'models':{}},project)
        page['regions']=FakeEngines().detect(None,None)
        self.assertNotEqual(before,processing_key({'models':{}},project))
    def test_context_ocr_prepass_makes_following_page_available_before_translation(self):
        import uuid
        page=__import__('copy').deepcopy(self.project['pages'][0]);page['id']=str(uuid.uuid4());page['order']=1
        page['name']='next.png';page['original']='originals/0002.png'
        root=self.store.project_path(self.project['project_id'])
        (root/page['original']).parent.mkdir(parents=True,exist_ok=True)
        (root/page['original']).write_bytes((root/self.project['pages'][0]['original']).read_bytes())
        # Fixture extends the source manifest before processing, like a two-page import.
        self.project['pages'].append(page)
        from backend.projects.store import atomic_json
        atomic_json(root/'project.json',self.project)
        observed=[]
        class ContextEngines(FakeEngines):
            context_before=[]
            context_after=[]
            def detect(inner,*args):
                result=super().detect(*args);result[0]['id']=str(uuid.uuid4());return result
            def translate(inner,*args):
                observed.append(len(inner.context_after));return super().translate(*args)
        manager=PipelineManager(self.store,ContextEngines)
        job=self.run_job(manager,['ocr','translate'])
        self.assertEqual(job['state'],'completed',job.get('error'))
        self.assertGreater(observed[0],0)

    def test_context_upgrade_retranslates_old_machine_drafts_and_preserves_manual_edits(self):
        import copy
        from unittest.mock import patch
        project=self.store.load(self.project['project_id'])
        rows=FakeEngines().detect(None,None)
        rows[0].update(target_text='Ancien contresens',status='translated',translation_engine='old',translation_context_version=1)
        manual=copy.deepcopy(rows[0]);manual.update(id='manual',target_text='Correction humaine',translation_manual=True)
        reviewed=copy.deepcopy(rows[0]);reviewed.update(id='reviewed',target_text='Texte relu',status='reviewed')
        project['pages'][0]['regions']=[*rows,manual,reviewed]
        self.store.save(project,project['revision'])
        manager=PipelineManager(self.store)
        translation={'id':'context-test','adapter':'lmstudio','name':'Context fixture','config':{'model_key':'context-fixture'}}
        inpainting={'id':'paint-test','adapter':'telea','name':'Paint fixture'}
        with patch('backend.ai.registry.selected_model',side_effect=lambda kind:translation if kind=='translation' else inpainting),patch('backend.ai.engines.LocalEngines',return_value=FakeEngines()),patch('backend.ai.engines.model_status',return_value={'ocr':{'ready':True},'translation':{'ready':True},'inpainting':{'ready':True}}):
            job=self.run_job(manager,['translate'])
        self.assertEqual(job['state'],'completed',job.get('error'))
        translated=self.store.load(project['project_id'])['pages'][0]['regions']
        self.assertEqual([r['target_text'] for r in translated],['Bonjour','Correction humaine','Texte relu'])
        self.assertEqual(job['metrics'][0]['translation']['upgraded_context'],1)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "sample.png"
        Image.new("RGB", (200, 100), "white").save(source)
        self.store = ProjectStore(self.root / "library")
        self.project = import_source(self.store, source, source_language="en", target_language="fr")

    def run_job(self, manager, stages, **options):
        project = self.store.load(self.project["project_id"])
        job = manager.start(project["project_id"], {"stages": stages, "expected_revision": project["revision"], **options})
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            job = manager.get(project["project_id"], job["id"])
            if job["state"] in {"completed", "failed"}:
                return job
            threading.Event().wait(.02)
        self.fail("Traitement de test bloqué")

    def test_complete_pipeline_export_and_stale_output(self):
        manager = PipelineManager(self.store, FakeEngines)
        original = (self.store.project_path(self.project["project_id"]) / self.project["pages"][0]["original"]).read_bytes()
        job = self.run_job(manager, ["ocr", "translate", "inpaint", "render"])
        self.assertEqual(job["state"], "completed", job.get("error"))
        self.assertEqual(job["pages_done"], 1)
        # A single imported image now exports to its native format rather than CBZ.
        native = export_project(self.store, self.project["project_id"])
        self.assertEqual(native.suffix, ".png")
        with Image.open(native) as image:
            self.assertEqual(image.size, (200, 100))
            image.verify()
        project = self.store.load(self.project["project_id"])
        self.assertEqual(project["pages"][0]["regions"][0]["target_text"], "Bonjour")
        self.assertEqual(original, (self.store.project_path(project["project_id"]) / project["pages"][0]["original"]).read_bytes())
        project["pages"][0]["regions"][0]["target_text"] = "Une correction"
        self.store.save(project, project["revision"])
        with self.assertRaises(FileNotFoundError):
            output_file(self.store, project["project_id"], project["pages"][0]["id"], "rendered")
        with self.assertRaises(FileNotFoundError):
            export_project(self.store, project["project_id"])

    def test_failed_translation_keeps_ocr_checkpoint(self):
        class Failing(FakeEngines):
            def translate(self, *args):
                raise RuntimeError("modèle indisponible")
        job = self.run_job(PipelineManager(self.store, Failing), ["ocr", "translate", "render"])
        self.assertEqual(job["state"], "failed")
        project = self.store.load(self.project["project_id"])
        self.assertEqual(project["pages"][0]["regions"][0]["source_text"], "Hello world")
        with self.assertRaises(FileNotFoundError):
            export_project(self.store, project["project_id"])

    def test_existing_manual_regions_survive_ocr(self):
        manager = PipelineManager(self.store, FakeEngines)
        self.assertEqual(self.run_job(manager, ["ocr"])["state"], "completed")
        project = self.store.load(self.project["project_id"])
        project["pages"][0]["regions"][0]["source_text"] = "Correction manuelle"
        self.store.save(project, project["revision"])
        self.assertEqual(self.run_job(manager, ["ocr"])["state"], "completed")
        self.assertEqual(self.store.load(project["project_id"])["pages"][0]["regions"][0]["source_text"], "Correction manuelle")

    def test_repeated_complete_job_skips_valid_pages(self):
        manager=PipelineManager(self.store,FakeEngines)
        self.assertEqual(self.run_job(manager,['ocr','translate','inpaint','render'])['state'],'completed')
        job=self.run_job(manager,['ocr','translate','inpaint','render'],skip_existing=True)
        self.assertEqual(job.get('pages_skipped'),1)

    def test_text_only_edit_reuses_cleaning_but_box_edit_invalidates_it(self):
        from unittest.mock import patch
        manager=PipelineManager(self.store,FakeEngines)
        self.run_job(manager,['ocr','translate','inpaint','render'])
        project=self.store.load(self.project['project_id'])
        project['pages'][0]['regions'][0]['target_text']='Bonjour corrigé'
        self.store.save(project,project['revision'])
        with patch.object(manager.engines,'clean',wraps=manager.engines.clean) as clean:
            self.assertEqual(self.run_job(manager,['render'])['state'],'completed')
            self.assertEqual(clean.call_count,0)
        project=self.store.load(self.project['project_id'])
        project['pages'][0]['regions'][0]['box'][0]+=2
        self.store.save(project,project['revision'])
        with patch.object(manager.engines,'clean',wraps=manager.engines.clean) as clean:
            self.run_job(manager,['render'])
            self.assertEqual(clean.call_count,1)

    def test_glossary_change_invalidates_render_and_language_change_keeps_corrections(self):
        manager=PipelineManager(self.store,FakeEngines)
        self.run_job(manager,['ocr','translate','inpaint','render'])
        project=self.store.load(self.project['project_id'])
        project['translation_glossary']={'world':'monde'}
        project=self.store.save(project,project['revision'])
        with self.assertRaises(FileNotFoundError):output_file(self.store,project['project_id'],project['pages'][0]['id'],'rendered')
        project['target_language']='es'
        with self.assertRaisesRegex(ValueError,'déjà des traductions'):self.store.save(project,project['revision'])
        self.assertEqual(self.store.load(project['project_id'])['target_language'],'fr')

    def test_line_grouping_keeps_logo_separate(self):
        from backend.ai.engines import group_lines
        regions = group_lines([{"box": [10, 10, 140, 70], "text": "SCANS", "score": .9},
                               {"box": [10, 85, 140, 105], "text": "Hello", "score": .9},
                               {"box": [10, 115, 140, 135], "text": "world", "score": .9}])
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["status"], "ignored")
        self.assertEqual(regions[1]["source_text"], "Hello world")

    def test_retranslate_keeps_manual_and_reviewed_text(self):
        manager=PipelineManager(self.store,FakeEngines)
        self.run_job(manager,['ocr'])
        project=self.store.load(self.project['project_id'])
        import copy
        base=project['pages'][0]['regions'][0]
        regions=[]
        for index,(status,manual) in enumerate([('translated',False),('reviewed',False),('translated',True),('final',False)]):
            r=copy.deepcopy(base);r.update(id=f'region-{index}',target_text='Ancien texte',status=status,translation_engine='old-model',translation_manual=manual)
            regions.append(r)
        project['pages'][0]['regions']=regions
        self.store.save(project,project['revision'])
        job=self.run_job(manager,['translate'],retranslate=True)
        self.assertEqual(job['state'],'completed',job.get('error'))
        texts=[r['target_text'] for r in self.store.load(project['project_id'])['pages'][0]['regions']]
        self.assertEqual(texts,['Bonjour','Ancien texte','Ancien texte','Ancien texte'])


if __name__ == "__main__":
    unittest.main()
