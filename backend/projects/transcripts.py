"""Human-readable (JSON) chapter transcript, distinct from internal editor manifest."""
from pathlib import Path
from backend.projects.store import atomic_json
from backend.projects.layout import folder

def write_chapter_transcript(root: Path, manifest: dict) -> None:
    root=Path(root)
    entries=[]
    for page in manifest.get('pages',[]):
        for region in page.get('regions',[]):
            if region.get('source_text','').strip():
                entries.append({
                    'page':page.get('name'),
                    'source':region['source_text'],
                    'translation':region.get('target_text',''),
                    'model':region.get('translation_engine'),
                    'context_version':region.get('translation_context_version'),
                    'status':region.get('status')
                })
    path=(root/'traduction' if (root/'traduction').exists() else root)
    atomic_json(path/'dialogues.json',{'chapter_id':manifest['project_id'],
        'title':manifest['title'],'entries':entries})
