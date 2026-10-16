from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import tempfile
import zipfile
import uuid
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from backend.core.project import ProjectValidationError, RevisionConflict
from backend.projects.importer import import_source
from backend.projects.migrate_v1 import migrate_v1
from backend.projects.store import ProjectStore
from backend.projects.artifacts import generate_variants, variant_file, variant_status
from backend.ai.pipeline import PipelineManager, output_file, export_project
from backend.ai.batch import BatchManager
from backend.projects.workspaces import Workspaces
from backend.settings import Settings
from backend.runtime import resource_root,default_data_dir as runtime_data_dir
from backend.version import __version__


MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
WEB_ROOT = resource_root() / "frontend"
WEB_FILES = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/i18n.js":"i18n.js"}
WEB_FILES.update({f'/vendor/{name}':f'vendor/{name}' for name in ('tabler.min.css','tabler-sprite.svg','jszip.min.js','docx-preview.min.js')})
WEB_FILES.update({'/brand/renkavi.png':'brand/renkavi.png','/favicon.ico':'brand/renkavi.ico'})


def default_data_dir() -> Path:
    return runtime_data_dir()


def make_server(store: ProjectStore, port: int = 8765) -> ThreadingHTTPServer:
    workspaces=Workspaces(store)
    # Metadata backup precedes the reversible same-volume folder migration.
    for directory in store.chapter_paths():
        if directory.parent==store.projects_dir:
            backup=store.data_dir/'backups'/'project-layout-20261014'/f'{directory.name}.json'
            if not backup.exists():
                backup.parent.mkdir(parents=True,exist_ok=True)
                backup.write_bytes((directory/'project.json').read_bytes())
    workspaces.migrate_legacy()
    settings=Settings(store.data_dir)
    pipeline = PipelineManager(store)
    batches = BatchManager(pipeline,export_options=lambda:settings.get()['export'])
    from backend.ai.model_hub import ModelHub
    hub=ModelHub(state_dir=store.data_dir/'model-downloads')
    pipeline.activity_guard=hub.installing

    def attach_import(manifest,workspace_id=None):
        if workspace_id is None:
            workspace_id=workspaces.inbox()['id']
        workspaces.attach(workspace_id,manifest['project_id'])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            print(f"[Renkavi] {self.address_string()} {format % args}")

        def _json(self, status: int, data: dict):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, code: str, message: str):
            self._json(status, {"error": {"code": code, "message": message}})

        def _body(self, limit: int = 1024 * 1024) -> bytes:
            length = self._content_length(limit)
            return self.rfile.read(length)

        def _content_length(self, limit: int) -> int:
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError as exc:
                raise ValueError("Taille de requête invalide.") from exc
            if length < 0 or length > limit:
                raise ValueError("Taille de requête invalide ou excessive.")
            return length

        def _file(self, file: Path, cache_control: str = "no-store", download_name=None):
            # Open before sending HTTP 200: avoid a broken connection when an export vanishes.
            with file.open("rb") as source:
                size = file.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(size))
                if download_name is not None or file.suffix == ".cbz":
                    self.send_header("Content-Disposition", 'attachment; filename="renkavi_export' + file.suffix + '"; filename*=UTF-8\'\'' + quote(download_name or file.name, safe=''))
                self.send_header("Cache-Control", cache_control)
                self.end_headers()
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)

        def _dispatch(self, method: str):
            allowed_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            host = self.headers.get('Host', '').lower()
            origin = self.headers.get('Origin')
            if host not in allowed_hosts or (origin is not None and origin != f'http://{host}'):
                self._error(403, 'invalid_origin', 'Origine de requête non autorisée.')
                return
            try:
                self._route(method)
            except RevisionConflict as exc:
                self._error(409, "revision_conflict", str(exc))
            except FileNotFoundError as exc:
                self._error(404, "not_found", str(exc))
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                return
            except (ValueError, ProjectValidationError, json.JSONDecodeError, KeyError, TypeError) as exc:
                self._error(400, "invalid_request", str(exc))
            except Exception as exc:
                self._error(500, "internal_error", str(exc))

        def do_GET(self): self._dispatch("GET")
        def do_POST(self): self._dispatch("POST")
        def do_PUT(self): self._dispatch("PUT")

        def _route(self, method: str):
            if method != "GET" and self.headers.get("X-Renkavi-Client") != "1":
                self._error(403, "client_header_required", "En-tête client Renkavi requis.")
                return
            parsed = urlparse(self.path)
            if method == "GET" and parsed.path in WEB_FILES:
                self._file(WEB_ROOT / WEB_FILES[parsed.path], "no-store")
                return
            parts = [unquote(p) for p in parsed.path.strip("/").split("/")]
            if parts == ["api", "health"] and method == "GET":
                self._json(200, {"ok": True, "version": 3, "application_version":__version__, "ui_version":"studio-2026.10.16-desktop", "data_dir":str(store.data_dir),"features":{"docx_pdf_import":True,"native_download":True,"project_folders":True,"translation_context_version":2,"desktop":True}})
                return
            if parts==['api','settings']:
                if method=='GET':self._json(200,{'settings':settings.get(),'data_dir':str(store.data_dir)});return
                if method=='PUT':self._json(200,{'settings':settings.save(json.loads(self._body())['settings'])});return
            if parts==['api','system'] and method=='GET':
                from backend.system_status import system_status
                from backend.ai.registry import ROOT
                self._json(200,system_status(store.data_dir,ROOT));return
            if parts==['api','model-hub'] and method=='GET':
                self._json(200,hub.catalog());return
            if parts==['api','model-hub','downloads'] and method=='GET':
                self._json(200,{'downloads':hub.list_downloads()});return
            if parts==['api','model-hub','download'] and method=='POST':
                self._json(202,{'download':hub.start(json.loads(self._body())['model_id'])});return
            if len(parts)==5 and parts[:3]==['api','model-hub','downloads'] and method=='POST':
                self._body()
                with pipeline._lock:
                    if parts[4]=='install' and (pipeline._active or pipeline.batch_owner or hub.installing()):
                        raise ValueError('Mettez les traitements en pause et attendez la fin des installations avant d’installer un modèle.')
                    self._json(202,{'download':hub.control(parts[3],parts[4])});return
            if parts==['api','workspaces']:
                if method=='GET':self._json(200,{'workspaces':workspaces.list()});return
                if method=='POST':
                    data=json.loads(self._body())
                    self._json(201,{'workspace':workspaces.create(data.get('title',''),data.get('source_language','en'),data.get('target_language','fr'))});return
            if len(parts)==3 and parts[:2]==['api','workspaces']:
                if method=='GET':self._json(200,{'workspace':workspaces.get(parts[2])});return
                if method=='PUT':self._json(200,{'workspace':workspaces.update(parts[2],json.loads(self._body()))});return
            if len(parts)==4 and parts[:2]==['api','workspaces'] and parts[3]=='export' and method=='GET':
                from backend.projects.file_exports import export_workspace
                workspace=workspaces.get(parts[2])
                title=re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', workspace['title']).strip(' .')[:120] or 'projet'
                exported=export_workspace(store,workspaces,parts[2],settings.get()['export'])
                if parse_qs(parsed.query).get('prepare')==['1']:
                    self._json(200,{'download_url':parsed.path,'filename':title+'.zip','size':exported.stat().st_size})
                else:self._file(exported,download_name=title+'.zip')
                return
            if len(parts)==4 and parts[:2]==['api','workspaces'] and parts[3]=='cover' and method=='GET':
                workspace=workspaces.get(parts[2])
                chapter=next((p for p in workspace['chapters'] if p.get('page_count')),None)
                if not chapter:raise FileNotFoundError('Ce projet ne contient pas encore de page.')
                manifest=store.load(chapter['project_id'])
                page=manifest['pages'][min(2,len(manifest['pages'])-1)]
                cover=Path(workspace['folder'])/'thumbnails'/f"{page['sha256'][:24]}.jpg"
                from PIL import Image,ImageOps
                source=store.project_path(chapter['project_id'])/page['original']
                if not cover.is_file():
                    with Image.open(source) as image:
                        # Keep a small real crop of the chapter instead of loading the full strip in cards.
                        crop=image.crop((0,0,image.width,min(image.height,image.width*1.2))).convert('RGB')
                        crop=ImageOps.fit(crop,(480,320),method=Image.Resampling.LANCZOS)
                        import io
                        data=io.BytesIO();crop.save(data,format='JPEG',quality=85)
                    cover.parent.mkdir(exist_ok=True)
                    temporary=cover.with_name(f'.{uuid.uuid4().hex}.tmp')
                    try:temporary.write_bytes(data.getvalue());temporary.replace(cover)
                    finally:temporary.unlink(missing_ok=True)
                self._file(cover,'private, max-age=60');return
            if len(parts)==4 and parts[:2]==['api','workspaces'] and method=='POST':
                data=json.loads(self._body())
                if parts[3]=='chapters':
                    with pipeline._lock:
                        if pipeline._active or pipeline.batch_owner:raise ValueError('Mettez les traitements en pause avant de déplacer un chapitre.')
                        self._json(200,{'workspace':workspaces.attach(parts[2],data['chapter_id'])});return
                if parts[3]=='open-folder':
                    folder=workspaces.get(parts[2])['folder']
                    if os.name=='nt':os.startfile(folder)
                    self._json(200,{'folder':folder});return
            if parts in (['api','connectors'],['api','connectors','test']) and method=='POST':
                from backend.ai.connectors import add_connector,test_connection
                data=json.loads(self._body())
                with pipeline._lock:
                    if pipeline._active or pipeline.batch_owner or hub.installing():raise ValueError('Mettez la file en pause et attendez la fin des installations avant de configurer un connecteur.')
                    self._json(200,test_connection(data) if parts[-1]=='test' else add_connector(data));return
            if parts == ["api", "ai", "status"] and method == "GET":
                try:
                    from backend.ai.engines import model_status
                    status = model_status()
                except ImportError:
                    status = {"ready": False, "message": "Lancez INSTALLER.bat, puis LANCER.bat pour activer les moteurs IA."}
                self._json(200, status)
                return
            if parts == ["api", "models"] and method == "GET":
                from backend.ai.registry import catalog
                self._json(200, catalog())
                return
            if parts == ["api", "fonts"] and method == "GET":
                from backend.ai.typography import available_fonts
                self._json(200, {"fonts": list(available_fonts())})
                return
            if len(parts) == 3 and parts[:2] == ["api", "models"] and method == "POST":
                from backend.ai.registry import add_model, catalog, pick_model, select_model
                data = json.loads(self._body())
                with pipeline._lock:
                    if pipeline._active or pipeline.batch_owner or hub.installing():
                        raise ValueError("Attendez la fin du traitement avant de changer de modèle.")
                    if parts[2] == "select":
                        self._json(200, select_model(data["kind"], data["id"]))
                        return
                    if parts[2] in {"add", "pick"}:
                        path = pick_model(data["kind"]) if parts[2] == "pick" else data["path"]
                        self._json(200, add_model(data["kind"], path) if path else {"cancelled": True, **catalog()})
                        return
            if parts == ["api", "projects"] and method == "GET":
                self._json(200, {"projects": store.list_projects()})
                return
            if parts == ['api','batches']:
                if method=='GET':
                    with pipeline._lock:
                        job=pipeline._jobs.get(pipeline._active) if not pipeline.batch_owner else None
                        active_job={key:job.get(key) for key in ('id','project_id','state','stage','message','pages_done','total')} if job else None
                        if active_job:
                            active_job['title']=store.load(job['project_id'])['title']
                        self._json(200,{'batches':batches.list(),'active_id':batches.active,'active_job':active_job})
                    return
                if method=='POST':self._json(202,{'batch':batches.start(json.loads(self._body()))});return
            if len(parts)==3 and parts[:2]==['api','batches'] and method=='GET':
                self._json(200,{'batch':batches.get(parts[2])});return
            if len(parts)==4 and parts[:2]==['api','batches'] and method=='POST':
                self._body()
                self._json(200,{'batch':batches.control(parts[2],parts[3])});return
            if parts==['api','import-batch'] and method=='POST':
                from backend.projects.importer import natural_key
                from backend.projects.formats import SUPPORTED_EXTENSIONS
                data=json.loads(self._body());paths=data.get('paths',[])
                if not isinstance(paths,list) or not paths or len(paths)>200 or any(not isinstance(p,str) for p in paths):
                    raise ValueError('Indiquez de 1 à 200 chemins locaux.')
                sources=[]
                for value in paths:
                    source=Path(value.strip().strip('"')).expanduser()
                    if source.is_dir():
                        sources.extend(sorted((p for p in source.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS),key=lambda p:natural_key(p.name)))
                    else:sources.append(source)
                if not sources or len(sources)>200:raise ValueError('Aucun fichier compatible trouvé, ou plus de 200 fichiers.')
                projects=[];errors=[]
                workspace_id=data.get('workspace_id')
                if workspace_id:workspaces.get(workspace_id)
                for source in dict.fromkeys(sources):
                    try:
                        manifest=import_source(store,source,source_language=data.get('source_language','en'),target_language=data.get('target_language','fr'))
                        attach_import(manifest,workspace_id)
                        projects.append({'project_id':manifest['project_id'],'title':manifest['title'],'page_count':len(manifest['pages'])})
                    except (OSError,ValueError,zipfile.BadZipFile) as exc:errors.append({'path':str(source),'message':str(exc)})
                self._json(200,{'projects':projects,'errors':errors});return
            if parts == ["api", "import"] and method == "POST":
                query = parse_qs(parsed.query)
                workspace_id=query.get('workspace_id',[None])[0]
                if workspace_id:workspaces.get(workspace_id)
                name = query.get("filename", [""])[0]
                if not name or Path(name).name != name or "\\" in name:
                    raise ValueError("Nom de fichier invalide.")
                suffix = Path(name).suffix.lower()
                from backend.projects.formats import SUPPORTED_EXTENSIONS
                if suffix not in SUPPORTED_EXTENSIONS:
                    raise ValueError("Format non pris en charge.")
                length = self._content_length(MAX_UPLOAD_BYTES)
                with tempfile.TemporaryDirectory(prefix="renkavi-upload-") as tmp:
                    source = Path(tmp) / name
                    with source.open("wb") as output:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("Téléversement incomplet.")
                            output.write(chunk)
                            remaining -= len(chunk)
                    manifest = import_source(store, source, title=query.get("title", [Path(name).stem])[0],
                                             source_language=query.get("source_language", ["auto"])[0],
                                             target_language=query.get("target_language", ["fr"])[0])
                    attach_import(manifest,workspace_id)
                self._json(201, {"project": manifest})
                return
            if parts == ["api", "import-path"] and method == "POST":
                data = json.loads(self._body())
                workspace_id=data.get('workspace_id')
                if workspace_id:workspaces.get(workspace_id)
                manifest = import_source(store, data["path"], title=data.get("title"),
                                         source_language=data.get("source_language", "auto"),
                                         target_language=data.get("target_language", "fr"))
                attach_import(manifest,workspace_id)
                self._json(201, {"project": manifest})
                return
            if parts == ["api", "migrate-v1"] and method == "POST":
                data = json.loads(self._body())
                workspace_id=data.get('workspace_id')
                if workspace_id:workspaces.get(workspace_id)
                manifest = migrate_v1(store, data["path"])
                attach_import(manifest,workspace_id)
                self._json(201, {"project": manifest})
                return
            if len(parts) >= 3 and parts[:2] == ["api", "projects"]:
                project_id = parts[2]
                if len(parts) == 4 and parts[3] == 'source-docx' and method == 'GET':
                    from backend.projects.documents_word import SOURCE_FILE, LEGACY_SOURCE_FILE
                    project = store.load(project_id)
                    if project.get('provenance', {}).get('source_format') != 'docx':
                        raise ValueError('L’aperçu Word nécessite un fichier DOCX original.')
                    directory = store.project_path(project_id).resolve()
                    source = (directory / project["provenance"].get("source_file", SOURCE_FILE)).resolve()
                    if not source.is_relative_to(directory) or not source.is_file():
                        raise FileNotFoundError('Document Word original introuvable.')
                    self._file(source, cache_control='private, no-store')
                    return
                if len(parts) == 4 and parts[3] == "process" and method == "POST":
                    self._json(202, {"job": pipeline.start(project_id, json.loads(self._body()))})
                    return
                if len(parts) == 5 and parts[3] == "tasks" and method == "GET":
                    self._json(200, {"job": pipeline.get(project_id, parts[4])})
                    return
                if len(parts) == 4 and parts[3] == "tasks" and method == "GET":
                    self._json(200, {"job": pipeline.latest(project_id)})
                    return
                if len(parts) == 4 and parts[3] == "export" and method == "GET":
                    project = store.load(project_id)
                    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', project['title']).strip(' .')[:120] or 'chapitre'
                    query=parse_qs(parsed.query)
                    options=settings.get()['export']
                    if 'format' in query:options['format']=query['format'][0]
                    if 'output' in query:options['output']=query['output'][0]
                    if 'quality' in query:
                        try:options['quality']=int(query['quality'][0])
                        except ValueError:raise ValueError('Qualité export entre 1 et 100.') from None
                    exported=export_project(store, project_id,options)
                    download_name=f"{title}_{project['target_language']}{exported.suffix}"
                    if query.get('prepare')==['1']:
                        from urllib.parse import urlencode
                        arguments={key:values[0] for key,values in query.items() if key!='prepare'}
                        url=parsed.path+('?' + urlencode(arguments) if arguments else '')
                        self._json(200,{'download_url':url,'filename':download_name,'size':exported.stat().st_size})
                    else:self._file(exported, download_name=download_name)
                    return
                if len(parts) == 7 and parts[3] == "pages" and parts[5] == "ai-output" and method == "GET":
                    self._file(output_file(store, project_id, parts[4], parts[6]))
                    return
                if len(parts) == 6 and parts[3] == "pages" and parts[5] == "variants":
                    page_id = parts[4]
                    if method == "GET":
                        self._json(200, variant_status(store, project_id, page_id))
                        return
                    if method == "POST":
                        data = json.loads(self._body())
                        self._json(200, generate_variants(store, project_id, page_id, data["expected_revision"]))
                        return
                if len(parts) == 7 and parts[3] == "pages" and parts[5] == "variants" and method == "GET":
                    self._file(variant_file(store, project_id, parts[4], parts[6]))
                    return
                if len(parts) == 3 and method == "GET":
                    self._json(200, {"project": store.load(project_id)})
                    return
                if len(parts) == 3 and method == "PUT":
                    data = json.loads(self._body(20 * 1024 * 1024))
                    if data["project"].get("project_id") != project_id:
                        raise ValueError("ID de projet discordant.")
                    with pipeline._lock:
                        if pipeline.batch_owner and any(item['project_id']==project_id for item in batches.batches[pipeline.batch_owner]['items']):
                            raise ValueError('Mettez la file batch en pause avant de modifier ce chapitre.')
                        saved = store.save(data["project"], data["expected_revision"])
                    self._json(200, {"project": saved})
                    return
                if len(parts) == 5 and parts[3] == "pages" and method == "GET":
                    project = store.load(project_id)
                    page = next((p for p in project["pages"] if p["id"] == parts[4]), None)
                    if page is None:
                        raise FileNotFoundError("Page introuvable.")
                    file = store.project_path(project_id) / page["original"]
                    if not file.is_file():
                        raise FileNotFoundError("Image originale introuvable.")
                    self._file(file, "private, max-age=3600")
                    return
            self._error(404, "not_found", "Route introuvable.")

    server=ThreadingHTTPServer(("127.0.0.1", port), Handler)
    def native_export(kind,identifier,options=None):
        selected={**settings.get()['export'],**(options or {})}
        if kind=='project':
            project=store.load(identifier)
            title=project['title']
            file=export_project(store,identifier,selected)
            name=f"{title}_{project['target_language']}{file.suffix}"
        elif kind=='workspace':
            from backend.projects.file_exports import export_workspace
            workspace=workspaces.get(identifier)
            file=export_workspace(store,workspaces,identifier,selected)
            name=workspace['title']+'.zip'
        else:raise ValueError('Type export invalide.')
        name=re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:180] or 'renkavi-export'+file.suffix
        return file,name
    def shutdown_runtime(timeout=8):
        deadline=time.monotonic()+timeout
        if batches.active:
            try:batches.control(batches.active,'pause')
            except (ValueError,FileNotFoundError):pass
        for download in hub.list_downloads():
            if download.get('state')=='downloading':
                try:hub.control(download['id'],'pause')
                except (ValueError,FileNotFoundError):pass
        if not pipeline.close(timeout=max(0,deadline-time.monotonic())):return False
        while batches.active or hub.installing():
            if time.monotonic()>=deadline:return False
            time.sleep(.05)
        return True
    server.export_file=native_export
    server.shutdown_runtime=shutdown_runtime
    server.pipeline=pipeline
    return server


def main():
    parser = argparse.ArgumentParser(description="API locale Renkavi V2")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(ProjectStore(args.data_dir), args.port)
    print(f"Renkavi API sur http://127.0.0.1:{server.server_port}, bibliothèque {args.data_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
