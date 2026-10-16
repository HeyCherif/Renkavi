from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
import uuid
import zipfile
from pathlib import Path
from backend.projects.layout import folder

from PIL import Image

from backend.core.project import RevisionConflict, utc_now
from backend.projects.artifacts import _atomic_image
from backend.projects.store import ProjectStore, atomic_json

STAGES = ("ocr", "translate", "inpaint", "render")


def digest(data):
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def model_key(model):
    return {key:model.get(key) for key in ('id','adapter','path','sha256')} | {key:model.get('config',{}).get(key) for key in ('model_key','profile','base_url')}


def processing_key(job,project,context_hash=None):
    return digest({'version':3,'context_version':2,'models':{k:model_key(m) for k,m in job.get('models',{}).items()},
                   'project_context':context_hash if context_hash is not None else project_context(project)})


def project_context(project):
    return digest({'languages':[project['source_language'],project['target_language']],
                   'glossary':project.get('translation_glossary',{}),
                   'notes':project.get('translation_context',''),
                   'source_context':[[p['id'],[(r['id'],r['source_text'],r['status']=='ignored') for r in p['regions']]] for p in project['pages']]})


def stale_machine_draft(region,context_hash):
    version=region.get('translation_context_version',0)
    if not isinstance(version,int) or isinstance(version,bool):version=0
    return (region['status']=='translated' and bool(region.get('translation_engine')) and not region.get('translation_manual',False)
            and bool(region.get('target_text','').strip()) and (version<2
            or region.get('translation_context_hash')!=context_hash))


def stale_model_draft(region,model):
    selected=model.get('config',{}).get('model_key')
    return bool(selected and region['status']=='translated' and region.get('translation_engine')
                and not region.get('translation_manual',False) and region.get('target_text','').strip()
                and region['translation_engine']!=selected)


def cleaning_signature(page,job):
    return digest({'version':3,'image':page['sha256'],'model':model_key(job.get('models',{}).get('inpainting',{})),
                   'regions':[{'box':r['box'],'lines':[l['box'] for l in r.get('lines',[])],
                               'ignored':r['status']=='ignored','preserve':r.get('preserve_original',False)} for r in page['regions']],
                   'strokes':page.get('mask_strokes',[])})


def read_meta(directory):
    try:return json.loads((directory/'meta.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}


def signature(page: dict) -> str:
    data = {"version": 1, "sha256": page["sha256"], "regions": page["regions"], "mask_strokes": page.get("mask_strokes", [])}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def page_directory(store: ProjectStore, project_id: str, page_id: str) -> Path:
    return folder(store.project_path(project_id), "translation") / hashlib.sha256(page_id.encode()).hexdigest()


def output_file(store: ProjectStore, project_id: str, page_id: str, variant: str) -> Path:
    if variant not in {"cleaned", "rendered", "mask"}:
        raise FileNotFoundError("Variante IA inconnue.")
    project = store.load(project_id)
    page = next((p for p in project["pages"] if p["id"] == page_id), None)
    if page is None:
        raise FileNotFoundError("Page introuvable.")
    directory = page_directory(store, project_id, page_id)
    context_hash=project_context(project)
    if variant=='rendered' and any(stale_machine_draft(r,context_hash) for r in page['regions']):
        raise FileNotFoundError('Le contexte de la traduction a changé. Relancez la traduction puis le rendu.')
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FileNotFoundError("Lancez le nettoyage ou le rendu de cette page.") from exc
    if meta.get("signature") != signature(page) or variant not in meta.get("variants", []) or meta.get('project_context',context_hash)!=context_hash or (page.get('mask_strokes') and meta.get('mask_processing_version',0)<3):
        raise FileNotFoundError("Résultat périmé : relancez le traitement après vos corrections.")
    output = directory / f"{variant}.png"
    if not output.is_file():
        raise FileNotFoundError("Résultat manquant.")
    return output


def export_project(store: ProjectStore, project_id: str, options=None) -> Path:
    from backend.projects.file_exports import export_native
    return export_native(store,project_id,options)


class PipelineManager:
    def __init__(self, store: ProjectStore, engine_factory=None):
        self.store = store
        self.engine_factory = engine_factory
        self.engines = None
        self._lock = threading.RLock()
        self._active = None
        self.batch_owner = None
        self.activity_guard=lambda:False
        self._events = {}
        self._jobs = {}
        for file in (job for directory in store.chapter_paths() for job in folder(directory, 'jobs').glob('*.json')):
            try:
                job = json.loads(file.read_text(encoding="utf-8"))
                if job["state"] in {"queued", "running"}:
                    job.update(state="interrupted", message="Traitement interrompu au redémarrage. Les pages enregistrées sont conservées.", updated_at=utc_now())
                    atomic_json(file, job)
            except (ValueError, KeyError, OSError):
                continue

    def _path(self, project_id: str, job_id: str) -> Path:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError("ID de traitement invalide.")
        return folder(self.store.project_path(project_id), "jobs") / f"{job_id}.json"

    def get(self, project_id: str, job_id: str) -> dict:
        with self._lock:
            if job_id in self._jobs and self._jobs[job_id]["project_id"] == project_id:
                return copy.deepcopy(self._jobs[job_id])
            return json.loads(self._path(project_id, job_id).read_text(encoding="utf-8"))

    def latest(self, project_id: str) -> dict | None:
        with self._lock:
            files = sorted(folder(self.store.project_path(project_id), "jobs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            return json.loads(files[0].read_text(encoding="utf-8")) if files else None

    def _update(self, job: dict, **changes):
        with self._lock:
            job.update(changes, updated_at=utc_now())
            atomic_json(self._path(job["project_id"], job["id"]), job)

    def request_stop(self,job_id):
        with self._lock:
            if self._active==job_id:
                self._update(self._jobs[job_id],stop_requested=True,message='Pause après la page en cours')

    def wait(self,job_id,timeout=.2):
        return self._events[job_id].wait(timeout)

    def close(self, timeout=5):
        """Pause at a persisted page boundary and release idle model resources."""
        with self._lock:
            active=self._active
            if active:self.request_stop(active)
        if active and not self.wait(active,timeout):return False
        with self._lock:
            if self.engines is not None and hasattr(self.engines,'close'):
                self.engines.close()
            self.engines=None
        return True

    def _reuse_completed(self, job, project):
        if not job['skip_existing'] or job['retranslate']:return False
        from backend.projects.formats import source_format
        if job['stages']!=list(STAGES) and not (job['stages']==['translate','render'] and source_format(project)=='docx'):return False
        context_hash=project_context(project)
        key=processing_key(job,project,context_hash)
        selected={page['id']:page for page in project['pages']}
        cached=[]
        for page_id in job['page_ids']:
            page=selected[page_id]
            directory=page_directory(self.store,project['project_id'],page_id)
            meta=read_meta(directory)
            if (meta.get('processing_key')!=key or meta.get('signature')!=signature(page)
                    or meta.get('project_context')!=context_hash or 'rendered' not in meta.get('variants',[])
                    or (page.get('mask_strokes') and meta.get('mask_processing_version',0)<3)
                    or any(stale_machine_draft(r,context_hash) or stale_model_draft(r,job.get('models',{}).get('translation',{})) for r in page['regions'])
                    or not (directory/'rendered.png').is_file()):return False
            cached.append((page,meta))
        # One read-only validation pass and one job write; no runtime checks or
        # model construction are needed when every requested result is valid.
        self._update(job,pages_done=len(cached),pages_skipped=len(cached),
                     regions=sum(len(page['regions']) for page,_ in cached),
                     warnings=list(dict.fromkeys(w for _,meta in cached for w in meta.get('warnings',[]))),
                     metrics=[{'page_id':page['id'],'name':page['name'],'timings':{},'cached':True} for page,_ in cached])
        return True

    def start(self, project_id: str, data: dict, *, batch_id=None, models_snapshot=None) -> dict:
        with self._lock:
            if self.activity_guard():raise ValueError('Une installation de modèle est en cours. Attendez sa fin avant de lancer un traitement.')
            if self._active or (self.batch_owner and self.batch_owner!=batch_id):
                raise ValueError("Un traitement est déjà en cours. Attendez sa fin.")
            project = self.store.load(project_id)
            if data.get("expected_revision") != project["revision"]:
                raise RevisionConflict("Enregistrez ou rechargez le projet avant le traitement.")
            stages = data.get("stages", list(STAGES))
            if not isinstance(stages, list) or not stages or len(set(stages)) != len(stages) or any(s not in STAGES for s in stages):
                raise ValueError("Étapes de traitement invalides.")
            selected = data.get("page_ids", [p["id"] for p in project["pages"]])
            if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected) or any(p not in [x["id"] for x in project["pages"]] for p in selected):
                raise ValueError("Sélection de pages invalide.")
            context_hash=project_context(project)
            if 'render' in stages and 'translate' not in stages and any(stale_machine_draft(r,context_hash) for p in project['pages'] if p['id'] in selected for r in p['regions']):
                raise ValueError('Le contexte de la traduction a changé. Relancez la traduction avant le rendu.')
            if not isinstance(data.get('retranslate',False),bool):
                raise ValueError('Option de retraduction invalide.')
            if not isinstance(data.get('skip_existing',False),bool):
                raise ValueError('Option de reprise invalide.')
            glossary=project.get('translation_glossary',{})
            if not isinstance(glossary,dict) or len(glossary)>100 or any(not isinstance(k,str) or not isinstance(v,str) or len(k)>150 or len(v)>150 for k,v in glossary.items()):
                raise ValueError('Glossaire invalide : 100 entrées texte maximum.')
            job = {"id": str(uuid.uuid4()), "project_id": project_id, "state": "queued", "stage": "queued", "message": "En attente",
                   "stages": [s for s in STAGES if s in stages], "page_ids": selected, "pages_done": 0, "total": len(selected),
                   "regions": 0, "warnings": [], "metrics": [], "created_at": utc_now(), "updated_at": utc_now()}
            job['retranslate']=data.get('retranslate',False)
            job.update(skip_existing=data.get('skip_existing',False),pages_skipped=0)
            if not self.engine_factory:
                from backend.ai.registry import selected_model
                job["models"] = {kind: copy.deepcopy(selected_model(kind)) for kind in ("translation", "inpainting")}
                if models_snapshot is not None:job['models']=copy.deepcopy(models_snapshot)
                if job['models']['translation']['adapter'] in {'lmstudio','openai_compatible'}:
                    job['models']['translation']['config']['glossary']=copy.deepcopy(glossary)
                    job['models']['translation']['config']['document_context']=project.get('translation_context','')
            self._jobs[job["id"]] = job
            self._active = job["id"]
            self._events[job['id']]=threading.Event()
            self._update(job)
            threading.Thread(target=self._run, args=(job,), daemon=True).start()
            return copy.deepcopy(job)

    def _run(self, job: dict):
        started = time.perf_counter()
        try:
            self._update(job, state="running", message="Chargement des moteurs locaux")
            if self._reuse_completed(job,self.store.load(job['project_id'])):
                self._update(job,state='completed',message='Résultats déjà valides',seconds=round(time.perf_counter()-started,2))
                return
            from backend.projects.formats import source_format
            structured = source_format(self.store.load(job['project_id'])) == 'docx'
            if not self.engine_factory:
                from backend.ai.engines import model_status
                status = model_status(job['models'])
                needed = {"ocr" if not structured and "ocr" in job["stages"] else None,
                          "translation" if "translate" in job["stages"] else None,
                          "inpainting" if not structured and any(s in job["stages"] for s in ("inpaint", "render")) else None}
                missing = [part for part in needed if part and not status[part]["ready"]]
                if missing:
                    raise RuntimeError("Moteurs indisponibles : " + ", ".join(sorted(missing)))
            if self.engines is None:
                if self.engine_factory:
                    self.engines = self.engine_factory()
                else:
                    from backend.ai.engines import LocalEngines
                    self.engines = LocalEngines(job["models"])
            if not self.engine_factory:
                self.engines.models = job["models"]
            # Collect the document's source before translating its first page.
            # Otherwise the next page is still empty during a first full run.
            ocr_prepared=set()
            preparation_started=time.perf_counter()
            if not structured and 'ocr' in job['stages'] and 'translate' in job['stages']:
                context_project=self.store.load(job['project_id'])
                for source_page in context_project['pages']:
                    if source_page['regions']:continue
                    if source_page.get('ocr_completed')=={'sha256':source_page['sha256'],'language':context_project['source_language']}:
                        ocr_prepared.add(source_page['id'])
                        continue
                    if job.get('stop_requested'):
                        self._update(job,state='paused',message='Préparation du contexte en pause',seconds=round(time.perf_counter()-started,2))
                        return
                    self._update(job,stage='ocr',message=f"Lecture du contexte · page {source_page['order']+1}/{len(context_project['pages'])}")
                    with Image.open(self.store.project_path(job['project_id'])/source_page['original']) as original:
                        regions=self.engines.detect(original.convert('RGB'),lambda message:self._update(job,message=message),context_project['source_language'])
                    with self.store._lock:
                        current=self.store.load(job['project_id'])
                        current_page=next(p for p in current['pages'] if p['id']==source_page['id'])
                        if signature(current_page)!=signature(source_page):
                            raise RevisionConflict('Page modifiée pendant la préparation du contexte. Relancez le traitement.')
                        current_page['regions']=regions
                        current_page['ocr_completed']={'sha256':source_page['sha256'],'language':context_project['source_language']}
                        self.store.save(current,current['revision'])
                        ocr_prepared.add(source_page['id'])
            self._update(job,preparation={'ocr_pages':len(ocr_prepared),'seconds':round(time.perf_counter()-preparation_started,3)})
            for index, page_id in enumerate(job["page_ids"]):
                if job.get('stop_requested'):
                    self._update(job,state='paused',message='Traitement en pause après la dernière page enregistrée',seconds=round(time.perf_counter()-started,2))
                    return
                project = self.store.load(job["project_id"])
                page = next(p for p in project["pages"] if p["id"] == page_id)
                directory=page_directory(self.store,project['project_id'],page_id)
                meta=read_meta(directory)
                context_hash=project_context(project)
                if job['skip_existing'] and not job['retranslate'] and job['stages']==list(STAGES) and not any(stale_machine_draft(r,context_hash) or stale_model_draft(r,job.get('models',{}).get('translation',{})) for r in page['regions']) and meta.get('processing_key')==processing_key(job,project):
                    try:output_file(self.store,project['project_id'],page_id,'rendered')
                    except FileNotFoundError:pass
                    else:
                        self._update(job,pages_done=index+1,pages_skipped=job['pages_skipped']+1,regions=job['regions']+len(page['regions']),
                                     warnings=list(dict.fromkeys(job['warnings']+meta.get('warnings',[]))),
                                     message=f"Page {index+1}/{job['total']} · résultat déjà valide",
                                     metrics=job['metrics']+[{'page_id':page_id,'name':page['name'],'timings':{},'cached':True}])
                        continue
                with Image.open(self.store.project_path(project["project_id"]) / page["original"]) as source:
                    image = source.convert("RGB")
                metric = {"page_id": page_id, "name": page["name"], "timings": {}}
                cleaned = None
                written_cleaned=False
                warnings = list(job["warnings"])
                def progress(message):
                    self._update(job, message=f"Page {index + 1}/{job['total']} · {message}")
                for stage in job["stages"]:
                    self._update(job, stage=stage)
                    start = time.perf_counter()
                    if stage == "ocr":
                        if not structured and not page["regions"] and page_id not in ocr_prepared:
                            page["regions"] = self.engines.detect(image, progress, project["source_language"])
                            project = self.store.save(project, project["revision"])
                            page = next(p for p in project["pages"] if p["id"] == page_id)
                        else:
                            progress("Zones existantes conservées")
                        if not page['regions']:
                            warnings.append(f"{page['name']} : aucun texte détecté ; vérifier visuellement.")
                    elif stage == "translate":
                        name = job.get("models", {}).get("translation", {}).get("name", "locale")
                        progress(f"Traduction · {name}")
                        metric["translation"] = {
                            "existing_kept": sum(bool(r["target_text"].strip()) for r in page["regions"]),
                            "model_id": job.get("models", {}).get("translation", {}).get("id")}
                        upgraded=0
                        upgraded_model=0
                        contextual_model=job.get('models',{}).get('translation',{}).get('adapter') in {'lmstudio','openai_compatible'}
                        if job['retranslate'] or contextual_model:
                            for region in page['regions']:
                                outdated=stale_machine_draft(region,context_hash) or not region['target_text'].strip()
                                model_changed=stale_model_draft(region,job.get('models',{}).get('translation',{}))
                                if (job['retranslate'] or outdated or model_changed) and region['status']=='translated' and region.get('translation_engine') and not region.get('translation_manual',False):
                                    region['target_text']=''
                                    region.pop('preserve_original',None)
                                    region.pop('translation_warnings',None)
                                    upgraded+=int(outdated)
                                    upgraded_model+=int(model_changed)
                        metric['translation']['upgraded_context']=upgraded
                        metric['translation']['upgraded_model']=upgraded_model
                        metric['translation']['existing_kept']=sum(bool(r['target_text'].strip()) for r in page['regions'])
                        pending_ids={r['id'] for r in page['regions'] if r['status']!='ignored' and r['source_text'].strip() and not r['target_text'].strip()}
                        if hasattr(self.engines, 'context_before'):
                            page_index = next(i for i, current in enumerate(project['pages']) if current['id'] == page_id)
                            previous = project['pages'][max(0, page_index-1):page_index]
                            following = project['pages'][page_index+1:page_index+2]
                            self.engines.context_before = [r for p in previous for r in p['regions'] if r['source_text'].strip()][-5:]
                            self.engines.context_after = [r for p in following for r in p['regions'] if r['source_text'].strip()][:4]
                            self.engines.chapter_context = [r for p in project['pages'] for r in p['regions'] if r['source_text'].strip() and r['status']!='ignored']
                        self.engines.translate(page["regions"], project["source_language"], project["target_language"])
                        if contextual_model:
                            for region in page['regions']:
                                if region['id'] in pending_ids and region['target_text'].strip():
                                    region['translation_context_hash']=context_hash
                                    region['translation_context_version']=2
                        metric["translation"]["translated_now"] = sum(r['id'] in pending_ids and bool(r['target_text'].strip()) for r in page['regions'])
                        metric["translation"]["contextual_mode"] = "document-source-and-neighbors-v2"
                        for region in page['regions']:
                            warnings.extend(f"{page['name']} : {w}" for w in region.get('translation_warnings',[]))
                        project = self.store.save(project, project["revision"])
                        page = next(p for p in project["pages"] if p["id"] == page_id)
                    elif stage in {"inpaint", "render"}:
                        if cleaned is None:
                            if not structured and not self.engine_factory:
                                from backend.ai.typography import analyze_style, VERSION
                                for region in page['regions']:
                                    if region['status'] != 'ignored' and region.get('auto_style',{}).get('version') != VERSION:
                                        region['auto_style'] = analyze_style(image,region)
                                project = self.store.save(project,project['revision'])
                                page = next(p for p in project['pages'] if p['id'] == page_id)
                            cached=read_meta(directory)
                            if cached.get('cleaning_signature')==cleaning_signature(page,job) and all((directory/f'{v}.png').is_file() for v in ('cleaned','mask')):
                                try:
                                    with Image.open(directory/'cleaned.png') as im:cleaned=im.convert('RGB')
                                    with Image.open(directory/'mask.png') as im:mask=im.convert('L')
                                    metric['cleaning_cached']=True
                                    counts=cached.get('cleaning',{})
                                    written_cleaned=True
                                except OSError:cleaned=None
                            if cleaned is None:
                                if structured:
                                    from backend.projects.documents_word import clean_preview
                                    cleaned, mask, counts = clean_preview(image, page)
                                elif page.get("mask_strokes"):
                                    cleaned, mask, counts = self.engines.clean(image, page["regions"], progress, strokes=page["mask_strokes"])
                                else:
                                    cleaned, mask, counts = self.engines.clean(image, page["regions"], progress)
                            metric["cleaning"] = counts
                        directory = page_directory(self.store, project["project_id"], page_id)
                        images = {} if written_cleaned else {"cleaned": cleaned, "mask": mask}
                        variants = ["cleaned", "mask"]
                        if stage == "render":
                            progress("Composition du texte traduit")
                            from backend.ai.engines import render_translation
                            rendered, render_warnings = render_translation(cleaned, page["regions"])
                            warnings.extend(f"{page['name']} : {warning}" for warning in render_warnings)
                            images["rendered"] = rendered
                            variants.append("rendered")
                            untranslated = sum(bool(r["source_text"].strip()) and not r["target_text"].strip() and r["status"] != "ignored" for r in page["regions"])
                            if untranslated:
                                warnings.append(f"{page['name']} : {untranslated} zone(s) sans traduction.")
                        # Refuse to attach results to a page edited while inference ran.
                        with self.store._lock:
                            current = self.store.load(project["project_id"])
                            current_page = next(p for p in current["pages"] if p["id"] == page_id)
                            if signature(current_page) != signature(page):
                                raise RevisionConflict("Page modifiée pendant le traitement. Relancez le rendu.")
                            (directory / "meta.json").unlink(missing_ok=True)
                            for name, output in images.items():
                                _atomic_image(directory / f"{name}.png", output,compress_level=2)
                            written_cleaned=True
                            atomic_json(directory / "meta.json", {"signature": signature(page), "variants": variants, "models": job.get("models", {}),
                                'project_context':project_context(project),
                                'mask_processing_version':3,
                                'processing_key':processing_key(job,project),'cleaning_signature':cleaning_signature(page,job),'cleaning':counts,
                                'warnings':[w for w in warnings if w.startswith(page['name']+' :')],"created_at": utc_now()})
                    metric["timings"][stage] = round(time.perf_counter()-start, 3)
                self._update(job, pages_done=index+1, regions=job["regions"]+len(page["regions"]), warnings=list(dict.fromkeys(warnings)),
                             metrics=job["metrics"]+[metric])
            self._update(job, state="completed", message="Traitement terminé", seconds=round(time.perf_counter()-started, 2))
        except Exception as exc:
            self._update(job, state="failed", message=str(exc), error=f"{type(exc).__name__}: {exc}", seconds=round(time.perf_counter()-started, 2))
        finally:
            if job['state']=='failed':
                engine,self.engines=self.engines,None
                if engine is not None and hasattr(engine,'close'):
                    try:engine.close()
                    except Exception:logging.getLogger(__name__).exception('Libération du moteur après échec impossible')
            with self._lock:
                self._active = None
                self._events[job['id']].set()
