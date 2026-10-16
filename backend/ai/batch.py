"""Persistent serial chapter queue sharing the editor's inference engines."""
from __future__ import annotations
import copy
import json
import threading
import uuid
from backend.ai.pipeline import STAGES,export_project
from backend.core.project import utc_now
from backend.projects.store import atomic_json

TERMINAL={'completed','completed_with_errors','failed','cancelled','paused','interrupted'}


class BatchManager:
    def __init__(self,pipeline,export_options=None):
        self.pipeline=pipeline
        self.export_options=export_options
        self.store=pipeline.store
        self.directory=self.store.data_dir/'batches'
        self.directory.mkdir(exist_ok=True)
        self.lock=pipeline._lock
        self.active=None
        self.batches={}
        for path in self.directory.glob('*.json'):
            try:
                batch=json.loads(path.read_text(encoding='utf-8'))
                if batch['state'] not in TERMINAL:
                    batch.update(state='interrupted',message='Application redémarrée. Reprenez la file pour continuer.',intent=None)
                    for item in batch['items']:
                        if item['state']=='running':item['state']='pending'
                    self._save(batch)
                self.batches[batch['id']]=batch
            except (OSError,ValueError,KeyError):continue

    def _save(self,batch):
        batch['updated_at']=utc_now()
        atomic_json(self.directory/f"{batch['id']}.json",batch)

    def get(self,bid):
        with self.lock:
            if bid not in self.batches:raise FileNotFoundError('File batch introuvable.')
            result=copy.deepcopy(self.batches[bid])
            # Old history can outlive source files. Keep the journal intact,
            # but never advertise a vanished export as downloadable.
            if result['state'] in {'completed','completed_with_errors'}:
                lost=0
                for item in result['items']:
                    if item['state']=='completed' and not (self.store.project_path(item['project_id'])/'project.json').is_file():
                        item.update(state='missing',missing_source=True,export_ready=False,
                                    error='Le dossier de ce fichier est introuvable. Son historique existe encore, pas son export.')
                        lost+=1
                if lost:
                    result['state']='unavailable'
                    result['message']=f'{lost} fichier(s) de cet ancien lot manquant(s) sur le disque. Aucun fichier n’a été supprimé par cette vérification.'
            return result

    def list(self):
        with self.lock:
            return sorted([self.get(bid) for bid in self.batches],key=lambda b:b['created_at'],reverse=True)[:20]

    def start(self,data):
        with self.lock:
            if self.pipeline.activity_guard():raise ValueError('Installation de modèle en cours. Attendez sa fin avant de lancer le batch.')
            if self.active or self.pipeline._active or self.pipeline.batch_owner:
                raise ValueError('Un traitement utilise déjà les moteurs. Attendez ou mettez-le en pause.')
            ids=data.get('project_ids')
            workspace_id=data.get('workspace_id')
            scope=data.get('scope','all' if workspace_id else 'selected')
            if scope not in {'all','selected'}:raise ValueError('Portée batch invalide.')
            if workspace_id:
                from backend.projects.workspaces import Workspaces
                from backend.projects.importer import natural_key
                members=Workspaces(self.store).get(workspace_id)['chapters']
                member_ids=[p['project_id'] for p in sorted(members,key=lambda p:natural_key(p['title']))]
                if scope=='all':ids=member_ids
                elif not isinstance(ids,list) or any(pid not in member_ids for pid in ids):
                    raise ValueError('La sélection contient un fichier extérieur au projet.')
            elif scope=='all':raise ValueError('Choisissez un projet pour traiter tous ses fichiers.')
            if not isinstance(ids,list) or not 1<=len(ids)<=200 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
                raise ValueError('Choisissez entre 1 et 200 fichiers distincts.')
            stages=data.get('stages',list(STAGES))
            if not isinstance(stages,list) or not stages or any(s not in STAGES for s in stages) or len(set(stages))!=len(stages):
                raise ValueError('Étapes batch invalides.')
            for key in ('auto_export','skip_existing','retranslate'):
                if key in data and not isinstance(data[key],bool):raise ValueError('Options batch invalides.')
            glossary=data.get('glossary',{})
            if not isinstance(glossary,dict) or len(glossary)>100 or any(not isinstance(k,str) or not isinstance(v,str) or not k.strip() or not v.strip() or len(k)>150 or len(v)>150 for k,v in glossary.items()):
                raise ValueError('Glossaire invalide : 100 entrées texte maximum.')
            projects=[self.store.load(pid) for pid in ids]
            from backend.projects.formats import source_format,export_extension
            models={}
            if not self.pipeline.engine_factory:
                from backend.ai.registry import selected_model
                models={kind:copy.deepcopy(selected_model(kind)) for kind in ('translation','inpainting')}
            batch={'id':str(uuid.uuid4()),'state':'queued','intent':None,'message':'File prête',
                   'workspace_id':workspace_id,'scope':scope,
                   'created_at':utc_now(),'models':models,'glossary':glossary,
                   'options':{'stages':stages,'skip_existing':data.get('skip_existing',True),
                              'export_options':copy.deepcopy(self.export_options() if self.export_options else {}),
                              'auto_export':data.get('auto_export',True),'retranslate':data.get('retranslate',False)},
                   'items':[{'project_id':p['project_id'],'title':p['title'],'state':'pending','total':len(p['pages']),
                             'source_format':source_format(p),'export_extension':export_extension(p),
                             'pages_done':0,'pages_skipped':0,'warnings':[],'export_ready':False} for p in projects]}
            self.batches[batch['id']]=batch
            self._launch(batch)
            return self.get(batch['id'])

    def _launch(self,batch):
        self.active=batch['id']
        self.pipeline.batch_owner=batch['id']
        batch.update(state='queued',intent=None,message='Démarrage de la file')
        self._save(batch)
        threading.Thread(target=self._run,args=(batch,),daemon=True).start()

    def control(self,bid,action):
        with self.lock:
            batch=self.batches.get(bid)
            if batch is None:raise FileNotFoundError('File batch introuvable.')
            if action=='resume':
                if batch['state'] not in {'paused','interrupted','completed_with_errors','failed'}:raise ValueError('Cette file ne peut pas être reprise.')
                if self.active or self.pipeline._active or self.pipeline.batch_owner:raise ValueError('Les moteurs sont déjà occupés.')
                for item in batch['items']:
                    if item['state'] in {'failed','paused','running'}:item.update(state='pending',error=None,export_ready=False)
                self._launch(batch)
            elif action in {'pause','cancel'}:
                if batch['state'] in {'completed','cancelled'}:raise ValueError('Cette file est déjà terminée.')
                if self.active==bid:
                    batch.update(intent=action,state='pausing' if action=='pause' else 'cancelling',
                                 message='Pause après la page en cours' if action=='pause' else 'Arrêt après la page en cours')
                    if self.pipeline._active:self.pipeline.request_stop(self.pipeline._active)
                    self._save(batch)
                elif action=='cancel':
                    for item in batch['items']:
                        if item['state'] in {'pending','paused'}:item['state']='cancelled'
                    batch.update(state='cancelled',intent=None,message='File annulée. Les résultats enregistrés sont conservés.')
                    self._save(batch)
                else:raise ValueError('La file est déjà en pause.')
            else:raise ValueError('Action batch inconnue.')
            return self.get(bid)

    def _run(self,batch):
        released=False
        try:
            with self.lock:
                if not batch.get('intent'):batch.update(state='running',message='Traitement des chapitres')
                self._save(batch)
            for item in batch['items']:
                with self.lock:
                    if batch.get('intent'):break
                    if item['state']=='completed':continue
                    item.update(state='running',error=None)
                    batch['message']=f"Traitement · {item['title']}"
                    self._save(batch)
                try:
                    project=self.store.load(item['project_id'])
                    if batch['glossary']:
                        project['translation_glossary']={**project.get('translation_glossary',{}),**batch['glossary']}
                        project=self.store.save(project,project['revision'])
                    options=batch['options']
                    job=self.pipeline.start(project['project_id'],{'expected_revision':project['revision'],
                         'stages':options['stages'],'skip_existing':options['skip_existing'],'retranslate':options['retranslate']},
                         batch_id=batch['id'],models_snapshot=batch['models'])
                    with self.lock:
                        item['job_id']=job['id']
                        # Control may have arrived between the chapter setup and its job.
                        if batch.get('intent'):self.pipeline.request_stop(job['id'])
                    previous=None
                    while True:
                        done=self.pipeline.wait(job['id'],.3)
                        job=self.pipeline.get(project['project_id'],job['id'])
                        progress=(job['state'],job['pages_done'],job['message'])
                        with self.lock:
                            item.update(pages_done=job['pages_done'],pages_skipped=job.get('pages_skipped',0),
                                        stage=job['stage'],message=job['message'],warnings=job.get('warnings',[]))
                            if progress!=previous:self._save(batch);previous=progress
                        if done:break
                    with self.lock:
                        item.update(state=job['state'],seconds=job.get('seconds',0),error=job.get('error'))
                    if job['state']=='completed' and options['auto_export'] and 'render' in options['stages']:
                        export_project(self.store,project['project_id'],options.get('export_options'))
                        with self.lock:item['export_ready']=True
                    with self.lock:self._save(batch)
                except Exception as exc:
                    with self.lock:
                        item.update(state='failed',error=str(exc),message=str(exc))
                        self._save(batch)
            with self.lock:
                remaining=any(i['state'] in {'pending','paused','running'} for i in batch['items'])
                if batch.get('intent')=='cancel':
                    for item in batch['items']:
                        if item['state'] in {'pending','paused','running'}:item['state']='cancelled'
                    batch.update(state='cancelled',message='File arrêtée. Les résultats enregistrés sont conservés.')
                elif batch.get('intent')=='pause' and remaining:
                    batch.update(state='paused',message='En pause · reprenez la file pour continuer')
                else:
                    errors=sum(i['state']=='failed' for i in batch['items'])
                    batch.update(state='completed_with_errors' if errors else 'completed',
                                 message=f"File terminée · {errors} chapitre(s) en erreur" if errors else 'Tous les chapitres sont terminés')
                batch['intent']=None
                self._save(batch)
                self._release(batch)
                released=True
        except Exception as exc:
            with self.lock:
                batch.update(state='failed',message=str(exc))
                try:self._save(batch)
                finally:
                    self._release(batch)
                    released=True
        finally:
            if not released:
                with self.lock:self._release(batch)

    def _release(self,batch):
        # Publish a terminal state and release ownership in the same critical section.
        # A resumed worker must never be cleared by the previous worker's finally.
        if self.active==batch['id']:self.active=None
        if self.pipeline.batch_owner==batch['id']:self.pipeline.batch_owner=None
