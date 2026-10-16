"""Safe in-place migration of chapter folders from legacy names to human categories."""
from __future__ import annotations
from pathlib import Path
import json, shutil, hashlib, time, uuid
from backend.projects.store import atomic_json
from backend.projects.workspaces import Workspaces
from backend.projects.store import ProjectStore

def migrate(root: Path) -> dict:
    root=Path(root).resolve()
    store=ProjectStore(root)
    backup=root/'backups'/'chapter-layout-20261015'
    summary={'chapters':[],'failures':[]}
    # File-level audit precedes changes; never move an active project.
    for chapter in list(store.chapter_paths()):
        pid=chapter.name
        metadata=chapter/'project.json'
        if not metadata.is_file():
            continue
        try:
            data=json.loads(metadata.read_text(encoding='utf8'))
            backup.mkdir(parents=True,exist_ok=True)
            original_meta=backup/(pid+'.json')
            if not original_meta.exists(): shutil.copy2(metadata,original_meta)
            old_files=[chapter/p['original'] for p in data.get('pages',[])]
            for file in old_files:
                if not file.is_file():raise FileNotFoundError('Original missing '+str(file))
            src=data.get('provenance',{}).get('source_file')
            if src and not (chapter/src).is_file():
                raise FileNotFoundError('Document source missing '+str(chapter/src))
            dirs=[
                ('originals','original'),
                ('source','original/source'),
                ('ai','traduction/ai'),
                ('ai-jobs','traduction/jobs'),
                ('artifacts','traduction/artifacts'),
                ('exports','export')
            ]
            for old,new in dirs:
                a=chapter/old;b=chapter/new
                if a.is_dir():
                    if b.exists():raise FileExistsError('Collision '+str(b))
                    b.parent.mkdir(parents=True,exist_ok=True)
                    a.rename(b)
            for key in ('original','traduction','export'):
                (chapter/key).mkdir(exist_ok=True)
            changed=False
            for page in data['pages']:
                original=page['original']
                if original.startswith('originals/'):
                    page['original']='original/'+original[len('originals/'):]
                    changed=True
                if not (chapter/page['original']).is_file():
                    raise FileNotFoundError(str(chapter/page['original']))
            source=data.get('provenance',{}).get('source_file')
            if source and source.startswith('source/'):
                data['provenance']['source_file']='original/'+source
                changed=True
            if changed:atomic_json(metadata,data)
            entries=[]
            for page in data['pages']:
                for region in page['regions']:
                    if region.get('source_text','').strip():
                        entries.append({
                            'page':page.get('name'),'source':region['source_text'],
                            'translation':region.get('target_text',''),
                            'model':region.get('translation_engine'),
                            'context_version':region.get('translation_context_version'),
                            'status':region.get('status')
                        })
            atomic_json(chapter/'traduction'/'dialogues.json',{'chapter_id':pid,'title':data['title'],'entries':entries})
            summary['chapters'].append({'chapter_id':pid,'folder':str(chapter),'pages':len(data['pages']),'dialogs':len(entries),'changed_manifest':changed})
        except Exception as exc:
            summary['failures'].append({'chapter_id':pid,'reason':repr(exc)})
            # The backed-up manifest and originals remain available for recovery.
            print('CHAPTER_ERROR',pid,repr(exc),flush=True)
    for folder in root.glob('projects/*/workspace.json'):
        try:
            workspace=json.loads(folder.read_text(encoding='utf8'))
            chapters=[]
            for meta in (folder.parent/'chapters').glob('*/project.json'):
                p=json.loads(meta.read_text(encoding='utf8'))
                chapters.append({'title':p['title'],'chapter_id':p['project_id'],'path':'chapters/'+p['project_id']})
            chapters.sort(key=lambda x:x['title'].casefold())
            atomic_json(folder.parent/'chapitres.json',{'project_title':workspace['title'],'chapters':chapters})
            if not (folder.parent/'LIRE-MOI.txt').is_file():
                (folder.parent/'LIRE-MOI.txt').write_text(
                    'RENKAVI - Projet : '+workspace['title']+'\n'
                    'Un seul projet contient ses chapitres dans chapters.\n'
                    'Chaque chapitre contient original, traduction et export.\n'
                    'Les noms de dossiers techniques servent à conserver les références des traitements.\n'
                    'Voir chapitres.json pour les noms lisibles.\n',encoding='utf8')
            (folder.parent/'export').mkdir(exist_ok=True)
            old_exports=folder.parent/'exports'
            if old_exports.is_dir():
                target=folder.parent/'export'
                if any(target.iterdir()):
                    raise FileExistsError('Workspace export collision '+str(target))
                target.rmdir()
                old_exports.rename(target)
        except Exception as exc:
            summary['failures'].append({'workspace_file':str(folder),'reason':repr(exc)})
    atomic_json(root/'chapter-layout-migration-report.json',summary)
    print('MIGRATED',len(summary['chapters']),'FAILURES',len(summary['failures']),flush=True)
    return summary
if __name__=='__main__':
 import sys
 res=migrate(Path(sys.argv[1]))
 if res['failures']:sys.exit(1)
