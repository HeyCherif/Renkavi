"""Read-only dialogue context for page and document translation.

Only target regions are numbered; surrounding text is reference material.
Prevents boundary losses when windows of six/eight bubbles are translated.
"""
from __future__ import annotations
import re
from collections import Counter

CONTEXT_VERSION = 2


def _trusted_translation(region, completed):
    value=(completed or {}).get(region.get('id'))
    if value:return value
    if (region.get('translation_manual') or region.get('status')=='reviewed'
            or not region.get('translation_engine')):
        return region.get('target_text','')
    return ''


def _chapter_references(regions,batch,excluded,source,budget):
    """Select verbatim source anchors; no generated synopsis or inferred identity."""
    from backend.ai.local_llm import normalize_source
    stop=set('the a an and or to of in is it was were i you he she they we my your his her our this that for with as me be have had do not so'.split())
    def words(text):return set(re.findall(r'[^\W\d_]{3,}',text.casefold()))-stop
    query=set().union(*(words(r['source_text']) for r in batch))
    frequency=Counter(word for r in regions for word in words(r['source_text']))
    candidates=[]
    for index,region in enumerate(regions):
        if region.get('id') in excluded:continue
        text=normalize_source(region['source_text'],source)
        shared=query & words(text)
        identity=bool(re.search(r'\b(?:my name is|i am called)\b',text,re.I))
        role=bool(re.search(r'\bI\b[^.!?]{0,55}\b(?:son|daughter)\b',text,re.I))
        relation=bool(re.search(r'\bmy (?:little |older |younger )?(?:sister|brother)\b',text,re.I))
        category='identity' if identity else 'role' if role else 'relation' if relation else None
        score=sum(1/frequency[w] for w in shared)+(3 if identity or role else 1 if relation else 0)
        if score and len(text)>25:candidates.append((-score,index,text,category))
    lines=[];used=0
    seen=set()
    for _,_,text,category in sorted(candidates):
        if category and category in seen:continue
        line='REPÈRE SOURCE: '+text[:210]
        if used+len(line)+1>budget:continue
        lines.append(line);used+=len(line)+1
        if category:seen.add(category)
        if len(lines)==3:break
    return lines

def context_for_window(all_regions, batch, *, source="en", completed=None, before=6, after=5, max_chars=2200, chapter_regions=(),notes=''):
    if not batch:
        return ""
    from backend.ai.local_llm import normalize_source
    all_regions = [r for r in all_regions if r.get("source_text", "").strip() and r.get("status") != "ignored"]
    position = {r.get("id"): i for i, r in enumerate(all_regions)}
    if batch[0].get("id") not in position or batch[-1].get("id") not in position:
        return ""
    first, last = position[batch[0]["id"]], position[batch[-1]["id"]]
    # Keep DOCX header/footer and document body in separate context tracks.
    part = batch[0].get("document_ref", {}).get("part")
    def compatible(r):
        ref = r.get("document_ref", {}).get("part")
        return part is None or ref is None or ref == part
    left = [r for r in all_regions[max(0, first - 10):first] if compatible(r)][-before:]
    right = [r for r in all_regions[last + 1:last + 11] if compatible(r)][:after]
    target_ids={r.get('id') for r in batch}
    middle=[r for r in all_regions[first:last+1] if r.get('id') not in target_ids and compatible(r)]
    anchors=_chapter_references([r for r in chapter_regions if compatible(r) and r.get('status')!='ignored' and r.get('source_text','').strip()],
                               batch,{r.get('id') for r in [*left,*batch,*middle,*right]},source,min(700,max_chars//3))
    lines=[];remaining=max_chars
    if notes.strip():
        note='FICHE DE CONTEXTE: '+notes.strip()[:1200]
        if len(note)+1<=remaining:lines.append(note);remaining-=len(note)+1
    for r in middle:
        translated=_trusted_translation(r,completed)
        line='ENTRE: '+normalize_source(r['source_text'],source)[:210]+(' | traduction relue : '+translated[:120] if translated else '')
        if len(line)+1<=remaining:lines.append(line);remaining-=len(line)+1
    for line in anchors:
        if len(line)+1<=remaining:lines.append(line);remaining-=len(line)+1
    # Alternate nearest before/after instead of clipping away the following answer.
    for index in range(max(len(left),len(right))):
        neighbors=[]
        if index<len(left):neighbors.append(('AVANT',left[-index-1]))
        if index<len(right):neighbors.append(('APRÈS',right[index]))
        for label,r in neighbors:
            text=normalize_source(r['source_text'],source)[:210]
            translated=_trusted_translation(r,completed) if label=='AVANT' else ''
            hint=(' | traduction précédente : '+translated[:120]) if translated else ''
            line=label+': '+text+hint
            if len(line)+1<=remaining:lines.append(line);remaining-=len(line)+1
    return '\n'.join(lines)[:max_chars]

def dialogue_instruction(table=False):
    base = ("Use the surrounding context to resolve speaker changes, references, pronouns, "
            "negation, idioms, question-and-answer pairs and story continuity. "
            "Never invent missing facts or omit dialogue. Preserve the numbered output alignment. "
            "Keep each line's speaker and addressee: context must not change I into you, or your sister into my sister. "
            "Preserve imperatives and threats; interpret idioms using the scene rather than literal financial meanings. "
            "Keep established names and titles consistent; do not invent gender or speaker identity when uncertain. ")
    if table:
        base += ("These lines may be Word table cells. Translate concisely without deleting "
                 "information; avoid unnecessary expansions because the original column widths must remain unchanged. ")
    return base
