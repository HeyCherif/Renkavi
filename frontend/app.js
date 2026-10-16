const $ = (id) => document.getElementById(id);
const svgNS = 'http://www.w3.org/2000/svg';
const state = { project: null, pageIndex: 0, regionId: null, dirty: false, zoom: 1, draft: null, variant: 'original', processing: false, modelsReady: false, engineStatus: null };
Object.assign(state,{projects:[],selected:new Set(),batchId:null,batchActive:false,batch:null,batchFetching:false});

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get('content-type') || '';
  const data = type.includes('json') ? await response.json() : null;
  if (!response.ok) throw new Error(data?.error?.message || `Erreur HTTP ${response.status}`);
  return data;
}

function flash(message) {
  const notice = $('notice');
  notice.textContent = message;
  notice.hidden = false;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => { notice.hidden = true; }, 4500);
}

function markDirty() {
  state.dirty = true;
  $('save-state').textContent = 'Modifications non enregistrées';
  if (state.variant !== 'original') {
    state.variant = 'original';
    $('view-variant').value = 'original';
    document.querySelector('#page-canvas image')?.setAttribute('href', projectUrl(`/pages/${encodeURIComponent(currentPage().id)}`));
  }
}

function currentPage() { return state.project?.pages[state.pageIndex] || null; }
function currentRegion() { return currentPage()?.regions.find((item) => item.id === state.regionId) || null; }
function projectUrl(path = '') { return `/api/projects/${encodeURIComponent(state.project.project_id)}${path}`; }

async function loadProjects() {
  try {
    const data = await request('/api/projects');
    state.projects = data.projects;
    await loadWorkspaces();
    $('connection').textContent = 'Local · connecté';
    if (!$('home-screen').hidden) $('workspace-location').textContent=tr('Accueil');
    renderProjectList();
  } catch (error) {
    $('connection').textContent = 'Serveur inaccessible';
    flash(error.message);
  }
}

function updateProjectMeta() {
  $('project-meta').textContent = `${sourceFormat(state.project).toUpperCase()} · ${state.project.pages.length} page${state.project.pages.length > 1 ? 's' : ''} · ${state.project.source_language.toUpperCase()} → ${state.project.target_language.toUpperCase()}`;
  const translated=state.project.pages.flatMap(page=>page.regions).filter(region=>region.status!=='ignored'&&region.target_text?.trim());
  const engines=[...new Set(translated.map(region=>region.translation_engine).filter(Boolean))];
  const contextual=translated.filter(region=>region.translation_context_version >= 2).length;
  $('project-meta').textContent+=engines.length?` · Modèle réellement appliqué : ${engines.join(', ')} · Contexte transmis : ${contextual}/${translated.length}`:' · Aucune traduction appliquée';
}

async function openProject(id) {
  try {
    if (state.dirty && !(await saveProject())) return;
    lockEditor(false);
    const data = await request(`/api/projects/${encodeURIComponent(id)}`);
    state.project = data.project;
    state.pageIndex = 0;
    state.regionId = null;
    state.dirty = false;
    state.variant = 'original';
    $('retranslate').checked = false;
    $('process-scope').value='chapter';
    configureDocumentEditor();
    $('chapter-context').value = state.project.translation_context || '';
    $('chapter-glossary').value = Object.entries(state.project.translation_glossary || {}).map(([key,value]) => `${key} = ${value}`).join('\n');
    $('view-variant').value = 'original';
    hideScreens();
    $('editor').hidden = false;
    document.body.classList.add('editing');
    state.fitMode = 'width';
    $('project-title').textContent = state.project.title;
    $('workspace-location').textContent = [state.workspace?.title, state.project.title].filter(Boolean).join(' / ');
    updateProjectMeta();
    $('save-state').textContent = 'Enregistré';
    renderPages();
    renderPage();
    $('job-panel').hidden = true;
    const { job } = await request(projectUrl('/tasks'));
    if (job && ['queued', 'running'].includes(job.state)) {
      const reference = { project_id: id, id: job.id };
      localStorage.setItem('renkavi-active-job', JSON.stringify(reference));
      lockEditor(true);
      pollJob(reference);
    } else if (job) {
      pollJob({ project_id: id, id: job.id });
    }
  } catch (error) { flash(error.message); }
}

function renderPages() {
  const list = $('page-list');
  list.replaceChildren();
  state.project.pages.forEach((page, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = index === state.pageIndex ? 'active' : '';
    button.textContent = `${index + 1}. ${page.name}`;
    button.addEventListener('click', () => {
      state.pageIndex = index;
      state.regionId = null;
      renderPages();
      renderPage();
    });
    list.append(button);
  });
}

function svgPoint(event) {
  const canvas = $('page-canvas');
  const point = canvas.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const imagePoint = point.matrixTransform(canvas.getScreenCTM().inverse());
  const page = currentPage();
  return [Math.max(0, Math.min(page.width, imagePoint.x)), Math.max(0, Math.min(page.height, imagePoint.y))];
}

function makeRect(box, className) {
  const rect = document.createElementNS(svgNS, 'rect');
  rect.setAttribute('x', box[0]);
  rect.setAttribute('y', box[1]);
  rect.setAttribute('width', box[2] - box[0]);
  rect.setAttribute('height', box[3] - box[1]);
  rect.setAttribute('class', className);
  return rect;
}

function renderPage() {
  const page = currentPage();
  const canvas = $('page-canvas');
  canvas.replaceChildren();
  if (!page) {
    $('page-title').textContent = 'Aucune page';
    renderRegions();
    return;
  }
  $('page-title').textContent = page.name;
  if (state.fitMode) {
    const viewport = $('canvas-scroll');
    const width = Math.max(200, viewport.clientWidth - 48);
    state.zoom = Math.min(width/page.width, state.fitMode === 'page' ? Math.max(180,viewport.clientHeight-48)/page.height : Infinity, 1.5);
    $('zoom').value = Math.round(state.zoom*100);
    $('zoom-value').textContent = `${Math.round(state.zoom*100)} %`;
  }
  canvas.setAttribute('viewBox', `0 0 ${page.width} ${page.height}`);
  canvas.setAttribute('width', Math.round(page.width * state.zoom));
  canvas.setAttribute('height', Math.round(page.height * state.zoom));
  const image = document.createElementNS(svgNS, 'image');
  const base = `/pages/${encodeURIComponent(page.id)}`;
  image.setAttribute('href', projectUrl(state.variant === 'original' ? base : `${base}/ai-output/${state.variant}?revision=${state.project.revision}`));
  image.addEventListener('error', () => {
    if (state.variant !== 'original') {
      state.variant = 'original';
      $('view-variant').value = 'original';
      flash('Cette version est absente ou périmée. Lancez le traitement de la page.');
      renderPage();
    }
  });
  image.setAttribute('width', page.width);
  image.setAttribute('height', page.height);
  canvas.append(image);
  renderRegions();
}

function renderRegions() {
  const page = currentPage();
  const canvas = $('page-canvas');
  for (const rect of [...canvas.querySelectorAll('rect')]) rect.remove();
  const list = $('region-list');
  list.replaceChildren();
  if (!page) { renderRegionForm(); return; }
  page.regions.forEach((region, index) => {
    const rect = makeRect(region.box, `zone${region.id === state.regionId ? ' selected' : ''}`);
    rect.addEventListener('pointerdown', (event) => {
      event.stopPropagation();
      state.regionId = region.id;
      renderRegions();
    });
    canvas.append(rect);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = region.id === state.regionId ? 'active' : '';
    button.textContent = `${index + 1}. ${region.target_text || region.source_text || 'Zone sans texte'}`;
    button.addEventListener('click', () => { state.regionId = region.id; renderRegions(); requestAnimationFrame(centerSelectedRegion); });
    list.append(button);
  });
  if (state.draft) canvas.append(makeRect(state.draft, 'draft'));
  renderRegionForm();
}

function centerSelectedRegion() {
  const rect=$('page-canvas').querySelector('.zone.selected');if(!rect)return;
  const viewport=$('canvas-scroll'),regionBounds=rect.getBoundingClientRect(),viewportBounds=viewport.getBoundingClientRect();
  viewport.scrollTo({top:viewport.scrollTop+regionBounds.top-viewportBounds.top-(viewportBounds.height-regionBounds.height)/2,left:viewport.scrollLeft+regionBounds.left-viewportBounds.left-(viewportBounds.width-regionBounds.width)/2,behavior:'auto'});
}

function renderRegionForm() {
  const region = currentRegion();
  $('region-form').hidden = !region;
  $('empty-region').hidden = Boolean(region);
  if (!region) return;
  $('region-heading').textContent = `Zone ${currentPage().regions.indexOf(region) + 1}`;
  $('box-x').value = Math.round(region.box[0]);
  $('box-y').value = Math.round(region.box[1]);
  $('box-w').value = Math.round(region.box[2] - region.box[0]);
  $('box-h').value = Math.round(region.box[3] - region.box[1]);
  $('source-text').value = region.source_text;
  $('target-text').value = region.target_text;
  $('region-status').value = region.status;
  $('region-font').value = region.style?.font || 'auto';
  $('region-casing').value = region.style?.casing || 'source';
  $('region-color').value = region.style?.fill ? '#' + region.style.fill.map(v => v.toString(16).padStart(2,'0')).join('') : '';
  $('detected-font').textContent = region.auto_style ? `Police estimée : ${region.auto_style.font}${region.auto_style.hollow ? ' · contour' : ''}${region.auto_style.glow ? ' · halo coloré' : ''}` : 'Le style sera analysé au prochain rendu.';
}

function updateBox() {
  const region = currentRegion();
  const page = currentPage();
  if (!region) return;
  const [x, y, w, h] = ['box-x', 'box-y', 'box-w', 'box-h'].map((id) => Number($(id).value));
  if (![x, y, w, h].every(Number.isFinite) || x < 0 || y < 0 || w < 1 || h < 1 || x + w > page.width || y + h > page.height) {
    flash('La zone doit rester à l’intérieur de la page.');
    renderRegionForm();
    return;
  }
  region.box = [x, y, x + w, y + h];
  markDirty();
  renderRegions();
}

async function saveProject() {
  if (!state.project || !state.dirty) return true;
  if (state.batchActive) { flash('Mettez la file batch en pause avant de corriger le fichier.'); return false; }
  try {
    $('save').disabled = true;
    const data = await request(projectUrl(), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-Renkavi-Client': '1' },
      body: JSON.stringify({ project: state.project, expected_revision: state.project.revision }),
    });
    state.project = data.project;
    state.dirty = false;
    $('save-state').textContent = 'Enregistré';
    flash('Projet enregistré.');
    return true;
  } catch (error) {
    $('save-state').textContent = 'Échec de sauvegarde';
    flash(error.message);
    return false;
  } finally { $('save').disabled = false; }
}

async function importFiles(files) {
  if (!files.length) return;
  if (!state.workspace) { flash('Choisissez un projet avant d’importer.'); return; }
  const imported = [], errors = [];
  const languages = {workspace_id:state.workspace.id,source_language:$('source-language').value,target_language:$('target-language').value};
  $('upload').disabled = true;
  $('import-feedback').hidden = false;
  try {
    for (const [index,file] of files.entries()) {
      $('import-feedback').textContent = `Import ${index+1}/${files.length} · ${file.name}`;
      try {
        const query = new URLSearchParams({filename:file.name,...languages});
        const data = await request(`/api/import?${query}`, {method:'POST',headers:{'X-Renkavi-Client':'1'},body:file});
        imported.push(data.project);state.selected.add(data.project.project_id);
      } catch(error) { errors.push(`${file.name} : ${error.message}`); }
    }
    await loadProjects();
    $('import-feedback').textContent = `${imported.length} fichier(s) importé(s)${errors.length ? ` · ${errors.length} échec(s)\n${errors.join('\n')}` : ' et sélectionné(s) pour le batch.'}`;
    if (files.length===1 && imported.length===1) await openProject(imported[0].project_id);
  } catch (error) { flash(error.message); }
  finally { $('upload').value = ''; $('upload').disabled = false; }
}

$('refresh-projects').addEventListener('click', loadProjects);
$('upload').addEventListener('change', (event) => importFiles(Array.from(event.target.files)));
$('path-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    flash('Import en cours…');
    const route = $('path-kind').value === 'v1' ? '/api/migrate-v1' : '/api/import-path';
    const data = await request(route, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Renkavi-Client': '1' },
      body: JSON.stringify({ workspace_id:state.workspace?.id, path: $('import-path').value.trim(), source_language: $('source-language').value, target_language: $('target-language').value }),
    });
    $('import-path').value = '';
    await loadProjects();
    if ($('path-kind').value === 'v1' && state.workspace) await postJson(`/api/workspaces/${encodeURIComponent(state.workspace.id)}/chapters`,{chapter_id:data.project.project_id});
    await openProject(data.project.project_id);
  } catch (error) { flash(error.message); }
});
$('back').addEventListener('click', showLibrary);
async function showLibrary() {
  if (!(await saveProject())) return;
  $('batch-scope').value='all';
  lockEditor(false);
  state.project = null;
  hideScreens();
  if (state.workspace) { $('library').hidden = false; $('workspace-location').textContent = state.workspace.title; }
  else { $('projects-screen').hidden=false; $('workspace-location').textContent=tr('Projets'); }
  loadProjects();
  return true;
}
$('save').addEventListener('click', saveProject);
$('zoom').addEventListener('input', () => {
  state.fitMode = null;
  state.zoom = Number($('zoom').value) / 100;
  $('zoom-value').textContent = `${$('zoom').value} %`;
  renderPage();
});
$('add-region').addEventListener('click', () => { $('page-canvas').scrollIntoView({ block: 'nearest' }); flash('Tracez un rectangle sur la page.'); });
for (const id of ['box-x', 'box-y', 'box-w', 'box-h']) $(id).addEventListener('change', updateBox);
for (const [id, key] of [['source-text', 'source_text'], ['target-text', 'target_text'], ['region-status', 'status']]) {
  $(id).addEventListener('input', () => {
    const region = currentRegion();
    if (!region) return;
    region[key] = $(id).value;
    if (key === 'target_text') { region.translation_manual = true; delete region.preserve_original; }
    markDirty();
    renderRegionListOnly();
  });
}
function renderRegionListOnly() {
  const regions = currentPage()?.regions || [];
  [...$('region-list').children].forEach((button, index) => {
    const region = regions[index];
    if (region) button.textContent = `${index + 1}. ${region.target_text || region.source_text || 'Zone sans texte'}`;
  });
}

$('chapter-context').addEventListener('input', () => { if (!state.project) return; state.project.translation_context = $('chapter-context').value; markDirty(); });
$('chapter-glossary').addEventListener('input', () => {
  if (!state.project) return;
  const glossary = {};
  for (const line of $('chapter-glossary').value.split('\n')) {
    const index = line.indexOf('=');
    if (index > 0 && line.slice(index+1).trim()) glossary[line.slice(0,index).trim()] = line.slice(index+1).trim();
  }
  state.project.translation_glossary = glossary;
  markDirty();
});
for (const id of ['region-font','region-casing','region-color']) $(id).addEventListener('change', () => {
  const region = currentRegion();
  if (!region) return;
  region.style ||= {};
  if (id === 'region-font') {
    if ($(id).value === 'auto') delete region.style.font;
    else region.style.font = $(id).value;
  } else if (id === 'region-casing') region.style.casing = $(id).value;
  else {
    const value = $(id).value.trim();
    if (!value) delete region.style.fill;
    else if (/^#[0-9a-f]{6}$/i.test(value)) region.style.fill = [1,3,5].map(start => parseInt(value.slice(start,start+2),16));
    else { flash('Couleur attendue : #RRGGBB'); return; }
  }
  markDirty();
});
request('/api/fonts').then(({ fonts }) => {
  for (const name of fonts) {
    const option = document.createElement('option'); option.value = name; option.textContent = name;
    $('region-font').append(option);
  }
}).catch(error => flash(`Polices : ${error.message}`));
$('delete-region').addEventListener('click', () => {
  const page = currentPage();
  const index = page.regions.findIndex((region) => region.id === state.regionId);
  if (index < 0) return;
  page.regions.splice(index, 1);
  state.regionId = null;
  markDirty();
  renderRegions();
});

$('page-canvas').addEventListener('pointerdown', (event) => {
  if (!currentPage() || isWordDocument(state.project) || state.processing || state.batchActive || state.variant !== 'original' || event.button !== 0 || event.target.closest('.zone')) return;
  const start = svgPoint(event);
  state.drawing = start;
  state.draft = [start[0], start[1], start[0], start[1]];
  $('page-canvas').setPointerCapture(event.pointerId);
  renderRegions();
});
$('page-canvas').addEventListener('pointermove', (event) => {
  if (!state.drawing) return;
  const end = svgPoint(event);
  state.draft = [Math.min(state.drawing[0], end[0]), Math.min(state.drawing[1], end[1]), Math.max(state.drawing[0], end[0]), Math.max(state.drawing[1], end[1])];
  renderRegions();
});
$('page-canvas').addEventListener('pointerup', (event) => {
  if (!state.drawing) return;
  const end = svgPoint(event);
  const box = [Math.min(state.drawing[0], end[0]), Math.min(state.drawing[1], end[1]), Math.max(state.drawing[0], end[0]), Math.max(state.drawing[1], end[1])].map(Math.round);
  state.drawing = null;
  state.draft = null;
  if (box[2] - box[0] >= 8 && box[3] - box[1] >= 8) {
    const region = { id: crypto.randomUUID(), box, source_text: '', target_text: '', status: 'detected', confidence_detection: null, confidence_ocr: null };
    currentPage().regions.push(region);
    state.regionId = region.id;
    markDirty();
  }
  renderRegions();
});
$('page-canvas').addEventListener('pointercancel', () => { state.drawing = null; state.draft = null; renderRegions(); });
window.addEventListener('beforeunload', (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
document.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveProject(); } });

function lockEditor(locked) {
  state.processing = locked;
  const busy=locked||Boolean(state.singleJob);
  for (const element of $('editor').querySelectorAll('button, input, textarea, select')) element.disabled = busy || state.batchActive;
  $('batch-editor-note').hidden = !state.batchActive;
  if (state.batchActive && !busy) for(const element of $('editor').querySelectorAll('#page-list button, #view-variant, #zoom')) element.disabled=false;
  if (!busy && !state.batchActive) {
    const m = state.engineStatus;
    $('process-all').disabled = isWordDocument(state.project)?!m?.translation?.ready:!m?.ready;
    $('process-ocr').disabled = !m?.ocr?.ready;
    $('process-translate').disabled = !m?.translation?.ready;
    $('process-render').disabled = !isWordDocument(state.project)&&!m?.inpainting?.ready;
  }
  if(isWordDocument(state.project))for(const control of $('editor').querySelectorAll('#source-text,#add-region,#delete-region,.coordinates input,#region-form details input,#region-form details select'))control.disabled=true;
  $('back').disabled=false;
}

async function loadModels() {
  try {
    const models = await request('/api/ai/status');
    state.modelsReady = models.ready;
    state.engineStatus = models;
    $('batch-models').textContent = `${models.translation?.name||'Traduction'} · ${models.inpainting?.name||'Nettoyage'}`;
    $('model-summary').textContent = models.ready
      ? `Tous les moteurs prêts${models.device?' · '+models.device:''}`
      : (models.messages?.join(' ') || 'Certains moteurs ne sont pas disponibles. Consulte « Modèles IA ».');
    for (const kind of ['ocr', 'translation', 'inpainting']) {
      const engine = models[kind];
      $('engine-' + kind + '-name').textContent = engine?.name || 'Absent';
      const indicator = $('engine-' + kind + '-status');
      indicator.textContent = engine?.ready ? 'Prêt' : 'À configurer';
      $('engine-' + kind).classList.toggle('ready', !!engine?.ready);
      $('engine-' + kind).classList.toggle('unavailable', !engine?.ready);
    }
    lockEditor(state.processing);
    selectionChanged();
    renderDashboard();renderInstalledModels();
  } catch (error) { $('model-summary').textContent = error.message; }
}

async function processStages(stages) {
  if (state.batchActive) { flash('La file batch utilise les moteurs. Mettez-la en pause pour un traitement individuel.'); return; }
  if (state.processing || state.singleJob || !state.project) return;
  if(isWordDocument(state.project))stages=stages.filter(stage=>!['ocr','inpaint'].includes(stage));
  if(!stages.length||!processingReady(stages,[state.project]))return;
  if(stages.includes('translate')&&!$('retranslate').checked){
    const scope=$('process-scope').value==='chapter'?state.project.pages:[currentPage()];
    const existing=scope.flatMap(page=>page.regions).filter(region=>region.status!=='ignored'&&region.source_text?.trim());
    if(existing.some(region=>region.target_text?.trim())){
      flash('Les anciens brouillons automatiques seront actualisés avec le contexte. Les corrections manuelles et zones relues sont conservées.');
    }
  }
  if (!(await saveProject())) return;
  try {
    const pageIds = $('process-scope').value === 'chapter' ? state.project.pages.map(page => page.id) : [currentPage().id];
    const data = await request(projectUrl('/process'), { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Renkavi-Client': '1' },
      body: JSON.stringify({ stages, page_ids: pageIds, expected_revision: state.project.revision, retranslate: $('retranslate').checked, skip_existing: stages.includes('translate') && stages.includes('render') && !$('retranslate').checked }) });
    const reference = { project_id: state.project.project_id, id: data.job.id };
    localStorage.setItem('renkavi-active-job', JSON.stringify(reference));
    lockEditor(true);
    pollJob(reference);
  } catch (error) { flash(error.message); }
}

async function pollJob(reference) {
  try {
    const { job } = await request(`/api/projects/${reference.project_id}/tasks/${reference.id}`);
    if (state.project?.project_id !== reference.project_id) {
      if (['queued','running'].includes(job.state)) {
        lockEditor(true);
        setTimeout(() => pollJob(reference), 1200);
      } else {
        localStorage.removeItem('renkavi-active-job');
        lockEditor(false);
        refreshBatches();
      }
      return;
    }
    $('job-panel').hidden = false;
    document.querySelector('#job-panel .job-title strong').textContent =
      job.state === 'completed' ? 'Traitement terminé' :
      ['failed', 'interrupted'].includes(job.state) ? 'Traitement interrompu' : 'Traitement en cours';
    $('job-progress').value = job.total ? 100 * job.pages_done / job.total : 0;
    const engines = job.models ? ` · ${job.models.translation.name} · ${job.models.inpainting.name}` : '';
    $('job-message').textContent = `${job.message} · ${job.pages_done}/${job.total} pages · ${job.regions} zones${engines}`;
    document.querySelectorAll('#stage-tracker [data-stage]').forEach(node => {
      const index = ['ocr', 'translate', 'inpaint', 'render'].indexOf(node.dataset.stage);
      const current = ['ocr', 'translate', 'inpaint', 'render'].indexOf(job.stage);
      node.classList.toggle('current', node.dataset.stage === job.stage);
      node.classList.toggle('done', current > index || (job.state === 'completed' && job.stages.includes(node.dataset.stage)));
      node.classList.toggle('skipped', !job.stages.includes(node.dataset.stage));
    });
    $('job-warnings').hidden = !job.warnings?.length;
    $('warning-summary').textContent = `${job.warnings?.length || 0} point(s) à vérifier`;
    $('warning-text').textContent = (job.warnings || []).join('\n');
    if (['queued', 'running'].includes(job.state)) {
      lockEditor(true);
      setTimeout(() => pollJob(reference), 1200);
      return;
    }
    localStorage.removeItem('renkavi-active-job');
    const { project } = await request(`/api/projects/${reference.project_id}`);
    if(state.project?.project_id!==reference.project_id||state.dirty){
      lockEditor(false);refreshBatches();return;
    }
    state.project = project;
    updateProjectMeta();
    state.regionId = null;
    state.dirty = false;
    state.variant = job.state === 'completed' && job.stages.includes('render') ? 'rendered' : 'original';
    $('view-variant').value = state.variant;
    $('save-state').textContent = 'Enregistré';
    renderPages();
    renderPage();
    lockEditor(false);
    flash(job.state === 'completed' ? (job.stages.includes('translate') && !job.stages.includes('render') ? tr('Traduction du texte terminée. Lancez « Nettoyer et rendre » pour actualiser les images et l’export.') : `Terminé : ${job.regions} zones, ${job.seconds} s.`) : `Traitement arrêté : ${job.message}`);
  } catch (error) {
    $('job-message').textContent = `Suivi indisponible : ${error.message}. Nouvelle tentative…`;
    setTimeout(() => pollJob(reference), 3000);
  }
}

$('process-all').addEventListener('click', () => processStages(['ocr', 'translate', 'inpaint', 'render']));
$('process-ocr').addEventListener('click', () => processStages(['ocr']));
$('process-translate').addEventListener('click', () => processStages(['translate']));
$('process-render').addEventListener('click', () => processStages(isWordDocument(state.project)?['render']:['inpaint', 'render']));
$('view-variant').addEventListener('change', () => { state.variant = $('view-variant').value; renderPage(); });
$('export-cbz').addEventListener('click', async () => {
  if (!(await saveProject())) return;
  await downloadChapter(state.project.project_id,state.project.title,$('export-cbz'),state.project);
});

async function initialize() {
  await Promise.all([loadProjects(), loadModels()]);
  const stored = localStorage.getItem('renkavi-active-job');
  if (stored) {
    try {
      const reference = JSON.parse(stored);
      await openProject(reference.project_id);
    } catch { localStorage.removeItem('renkavi-active-job'); }
  }
}

async function refreshModelCatalog() {
  const data = await request('/api/models');
  state.modelCatalog=data;
  for (const kind of ['translation', 'inpainting']) {
    const select = $(`${kind}-model`);
    select.replaceChildren();
    for (const model of data.models.filter(model => model.kind === kind)) {
      const option = document.createElement('option');
      option.value = model.id;
      option.textContent = model.name + ' — ' + tr(!model.available ? 'indisponible (fichier ou dépendance)' : model.tested ? 'vérifié' : 'disponible');
      option.disabled = !model.available;
      select.append(option);
    }
    select.value = data.selected[kind];
  }
}

function lockModels(locked) {
  locked=locked||Boolean(state.singleJob);
  for (const control of $('models-dialog').querySelectorAll('.installed-controls select, .installed-controls input, .installed-controls button, #connector-form input, #connector-form select, #connector-form button')) if (control.id !== 'close-models') control.disabled = locked;
}

$('open-models').addEventListener('click', async () => {
  await navigate('models');
  $('models-message').textContent = state.processing || state.batchActive ? 'Un traitement est en cours. Les modèles pourront être changés à sa fin ou après la pause du lot.' : '';
  try { await refreshModelCatalog(); lockModels(state.processing || state.batchActive); } catch (error) { $('models-message').textContent = error.message; }
});
$('close-models').addEventListener('click', () => navigate('library'));
for (const kind of ['translation', 'inpainting']) {
  $(`${kind}-model`).addEventListener('change', async () => {
    lockModels(true);
    try {
      await request('/api/models/select', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Renkavi-Client': '1' }, body: JSON.stringify({ kind, id: $(`${kind}-model`).value }) });
      $('models-message').textContent = 'Modèle sélectionné pour les prochains traitements.';
      await loadModels();
    } catch (error) { $('models-message').textContent = error.message; }
    finally { await refreshModelCatalog(); lockModels(state.processing || state.batchActive); }
  });
  $(`add-${kind}-model`).addEventListener('click', () => addModel(kind));
}

async function addModel(kind, path) {
  lockModels(true);
  $('models-message').textContent = path ? 'Vérification et essai du modèle… Cela peut prendre quelques minutes.' : 'Choisis le fichier dans la fenêtre Windows ; le modèle sera vérifié puis testé.';
  try {
    if (!path && window.pywebview?.api?.pick_model) {
      const picked = await window.pywebview.api.pick_model(kind);
      if (picked.cancelled) { $('models-message').textContent = 'Ajout annulé.'; return; }
      path = picked.path;
    }
    const data = await request(path ? '/api/models/add' : '/api/models/pick', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Renkavi-Client': '1' }, body: JSON.stringify({ kind, path }) });
    $('models-message').textContent = data.cancelled ? 'Ajout annulé.' : 'Modèle ajouté, testé et activé.';
    await refreshModelCatalog();
    await loadModels();
  } catch (error) { $('models-message').textContent = error.message; }
  finally { lockModels(state.processing || state.batchActive); }
}

$('model-path-form').addEventListener('submit', event => { event.preventDefault(); addModel($('model-kind').value, $('model-path').value.trim()); });
for (const kind of ['ocr', 'translation', 'inpainting']) {
  $('engine-' + kind).addEventListener('click', () => $('open-models').click());
}
function visibleProjects() {
  const query=$('project-search').value.trim().toLocaleLowerCase();
  const projects=state.workspace ? state.projects.filter(p=>state.workspace.chapters?.some(c=>c.project_id===p.project_id)) : [];
  return projects.filter(p=>!query || (p.title||p.project_id).toLocaleLowerCase().includes(query)).sort((a,b)=>new Intl.Collator('fr',{numeric:true,sensitivity:'base'}).compare(a.title||'',b.title||'') || (a.updated_at||'').localeCompare(b.updated_at||''));
}

function workspaceFiles(){
  return (state.workspace?.chapters||[]).map(file=>({...state.projects.find(p=>p.project_id===file.project_id),...file}));
}
function selectedBatchFiles(){const all=workspaceFiles();return $('batch-scope').value==='all'?all:all.filter(file=>state.selected.has(file.project_id));}
function selectionChanged() {
  const valid=visibleProjects().filter(p=>!p.error), chosen=valid.filter(p=>state.selected.has(p.project_id)).length;
  $('select-all-projects').checked=valid.length>0 && chosen===valid.length;
  $('select-all-projects').indeterminate=chosen>0 && chosen<valid.length;
  const files=workspaceFiles();
  $('stat-projects').textContent=files.length;
  $('stat-pages').textContent=files.reduce((n,p)=>n+(p.page_count||0),0).toLocaleString(window.uiLanguage==='en'?'en-US':'fr-FR');
  $('stat-selection').textContent=state.selected.size;$('projects-count').textContent=visibleProjects().length;
  $('selection-label').textContent=state.selected.size?`${state.selected.size} ${tr(state.selected.size===1?'fichier sélectionné':'fichiers sélectionnés')}`:tr('Aucun fichier sélectionné');
  $('clear-selection').disabled=!state.selected.size;
  if($('move-selected-chapters'))$('move-selected-chapters').disabled=!state.selected.size||state.processing||state.batchActive;
  const scope=$('batch-scope').value,batchFiles=selectedBatchFiles(),stages=batchStageModes()[$('batch-stages').value];
  $('batch-start').disabled=!batchFiles.length||state.batchActive||state.processing||Boolean(state.singleJob)||!processingReady(stages,batchFiles);
  $('batch-start').textContent=scope==='all'?(files.length===1?tr('Traiter le fichier du projet'):`${tr('Traiter les')} ${files.length} ${tr('fichiers du projet')}`):(batchFiles.length===1?tr('Traiter le fichier sélectionné'):`${tr('Traiter')} ${batchFiles.length} ${tr('fichiers sélectionnés')}`);
  $('batch-scope-note').textContent=scope==='all'?tr('Tous les fichiers du projet seront traités, même ceux masqués par la recherche.'):tr('Seuls les fichiers cochés seront traités.');
  const preview=$('batch-file-preview');preview.replaceChildren();
  const heading=document.createElement('strong');heading.textContent=`File prévue · ${batchFiles.length} fichier(s)`;preview.append(heading);
  for(const [index,file] of batchFiles.slice(0,6).entries()){
    const line=document.createElement('div');line.className='batch-file-preview-row';
    const counter=document.createElement('span');counter.textContent=String(index+1).padStart(2,'0');
    const name=document.createElement('span');name.textContent=file.title||file.source_name||file.project_id;name.title=file.title||'';
    const type=document.createElement('small');type.textContent=fileExtension(file).toUpperCase();
    line.append(counter,name,type);preview.append(line);
  }
  if(batchFiles.length>6){const remaining=document.createElement('small');remaining.textContent=`Et ${batchFiles.length-6} autres fichiers…`;preview.append(remaining);}
  if(!batchFiles.length){const empty=document.createElement('small');empty.textContent=scope==='all'?'Ajoutez des fichiers à ce projet.':'Sélectionnez des fichiers dans la liste.';preview.append(empty);}
  for(const card of $('project-list').children)card.classList.toggle('selected',state.selected.has(card.dataset.projectId));
}
function batchStageModes(){return {all:['ocr','translate','inpaint','render'],ocr:['ocr'],translate:['translate'],render:['inpaint','render']};}
function sourceFormat(file){return String(file?.source_format||file?.provenance?.source_format||file?.export_extension||(/\.([a-z0-9]+)$/i.exec(file?.source_name||file?.provenance?.source_name||'')||[])[1]||'cbz').replace(/^\./,'').toLowerCase();}
function isWordDocument(file){return sourceFormat(file)==='docx';}
function fileExtension(file){const format=String(file?.export_extension||sourceFormat(file)).replace(/^\./,'').toLowerCase();return ({jpeg:'jpg',images:'cbz'})[format]||format;}
function processingReady(stages,files){
  const m=state.engineStatus;
  if(!m)return false;
  if(files.length&&files.every(isWordDocument))return !stages.includes('translate')||!!m.translation?.ready;
  return (!stages.includes('ocr')||!!m.ocr?.ready)&&(!stages.includes('translate')||!!m.translation?.ready)&&(!stages.some(stage=>['inpaint','render'].includes(stage))||!!m.inpainting?.ready);
}
function formatBadge(file){const badge=document.createElement('span');badge.className='file-format-badge';badge.dataset.format=sourceFormat(file);badge.textContent=sourceFormat(file).toUpperCase();return badge;}
function configureDocumentEditor(){
  const word=isWordDocument(state.project);$('editor').classList.toggle('docx-document',word);$('document-preview-note').hidden=!word;$('process-ocr').hidden=word;
  setButtonLabel($('process-render'),word?'Actualiser l’aperçu':'Nettoyer et rendre',word?'refresh':'brush');
  setButtonLabel($('export-cbz'),`${tr('Exporter')} ${fileExtension(state.project).toUpperCase()}`,'download');
  for(const option of $('view-variant').options)option.disabled=word&&['cleaned','mask'].includes(option.value);
  lockEditor(state.processing);
}
// DOCX visual preview stays isolated from the OCR-like editable text view.
// The renderer is vendored locally (Apache 2.0) and reads the original, not a modified copy.
$('word-original-preview').addEventListener('click', async () => {
  if (!state.project || !isWordDocument(state.project)) return;
  const modal=$('word-preview-dialog'), status=$('word-preview-status');
  const pages=$('word-preview-pages');
  modal.showModal();
  if (state.wordPreviewId === state.project.project_id && pages.childElementCount) return;
  pages.replaceChildren();
  status.textContent='Chargement de la mise en page Word…';
  try {
    if (!window.docx?.renderAsync || !window.JSZip)
      throw Error('Le visualiseur Word local n’est pas disponible.');
    const response=await fetch(projectUrl('/source-docx'),{cache:'no-store'});
    if (!response.ok) throw Error(`Erreur HTTP ${response.status} pendant la lecture du document Word.`);
    const buffer=await response.arrayBuffer();
    await window.docx.renderAsync(buffer,pages,pages,{
      breakPages:true, renderHeaders:true, renderFooters:true,
      renderFootnotes:true, useBase64URL:true
    });
    state.wordPreviewId=state.project.project_id;
    status.textContent='Document original affiché. La pagination exacte peut différer de Microsoft Word.';
  } catch(error) {
    state.wordPreviewId=null;
    pages.replaceChildren();
    status.textContent=`Aperçu Word impossible : ${error.message}`;
  }
});
$('word-preview-close').addEventListener('click',()=> $('word-preview-dialog').close());

function setButtonLabel(button,label,iconName){button.replaceChildren(icon(iconName),document.createTextNode(tr(label)));}

function renderProjectList() {
  const present=new Set(state.projects.map(p=>p.project_id));
  state.selected=new Set([...state.selected].filter(id=>present.has(id)));
  const list=$('project-list');list.replaceChildren();
  for (const project of visibleProjects()) {
    const card=document.createElement('article');card.className='project-card';card.dataset.projectId=project.project_id;
    const check=document.createElement('input');check.type='checkbox';check.checked=state.selected.has(project.project_id);check.disabled=Boolean(project.error);
    check.setAttribute('aria-label',`Sélectionner ${project.title||project.project_id}`);
    check.addEventListener('change',()=>{if(check.checked)state.selected.add(project.project_id);else state.selected.delete(project.project_id);selectionChanged();});
    const icon=document.createElement('span');icon.className='chapter-icon';icon.append(window.workspaceIcon('file-description'));icon.setAttribute('aria-hidden','true');
    const open=document.createElement('button');open.type='button';open.className='project-open';open.disabled=Boolean(project.error);
    const title=document.createElement('strong');title.textContent=project.title||project.project_id;
    const meta=document.createElement('small');meta.textContent=project.error||`${project.page_count} ${project.page_count===1?'page':'pages'} · ${project.source_language.toUpperCase()} → ${project.target_language.toUpperCase()}${project.region_count ? ` · ${project.translated_regions}/${project.region_count} ${tr('zones traduites')}` : ''}`;
    if(!project.error && project.translated_regions){
      const engines=(project.translation_engines||[]).join(', ')||'modèle non identifié';
      meta.textContent+=` · Modèle : ${engines} · Contexte transmis : ${project.contextual_regions||0}/${project.translated_regions}`;
    }
    open.append(title,meta);open.addEventListener('click',()=>openProject(project.project_id));
    const status=document.createElement('span');status.className='status-pill';status.textContent=project.error?'Erreur':project.translated_regions?'À relire':'À préparer';
    const move=document.createElement('button');move.type='button';move.className='chapter-move';move.title=tr('Déplacer le fichier');move.setAttribute('aria-label',tr('Déplacer le fichier'));move.disabled=state.processing||state.batchActive;move.append(window.workspaceIcon('transfer'));move.addEventListener('click',()=>showMoveChapters([project.project_id],project.title));
    card.append(check,icon,open,formatBadge(project),status,move);list.append(card);
  }
  if (!list.children.length) {const empty=document.createElement('div');empty.className='library-empty';empty.textContent=state.projects.length?'Aucun fichier ne correspond à cette recherche.':'Importez un PDF, DOCX, une image ou une archive pour commencer.';list.append(empty);}
  selectionChanged();
}

function glossaryFrom(value) {
  const result={};
  for(const line of value.split('\n')) {
    if(!line.trim())continue;
    const index=line.indexOf('=');
    if(index<1 || !line.slice(index+1).trim()) throw Error('Glossaire : utilisez une ligne « terme = traduction » par entrée.');
    result[line.slice(0,index).trim()]=line.slice(index+1).trim();
  }
  return result;
}
function postJson(path,data={}) {return request(path,{method:'POST',headers:{'Content-Type':'application/json','X-Renkavi-Client':'1'},body:JSON.stringify(data)});}
const batchLabels={pending:'En attente',queued:'En attente',running:'En cours',pausing:'Mise en pause',paused:'En pause',cancelling:'Arrêt en cours',cancelled:'Arrêté',interrupted:'À reprendre',completed:'Terminé',completed_with_errors:'Erreurs à vérifier',failed:'Échec',unavailable:'Sources introuvables',missing:'Fichier absent'};
function duration(seconds) {return seconds<60?`${Math.round(seconds)} s`:`${Math.floor(seconds/60)} min ${Math.round(seconds%60)} s`;}

function renderBatch(batch) {
  state.batch=batch;
  $('batch-empty').hidden=Boolean(batch);$('batch-progress-panel').hidden=!batch;
  if(!batch)return;
  $('batch-used-models').textContent=Object.values(batch.models||{}).map(model=>model.name||model.id).filter(Boolean).join(' · ');
  const total=batch.items.reduce((n,i)=>n+i.total,0), done=batch.items.reduce((n,i)=>n+i.pages_done,0);
  $('batch-state').textContent=tr(batchLabels[batch.state]||batch.state);$('batch-state').dataset.state=batch.state;
  $('batch-totals').textContent=`${done}/${total} pages`;
  $('batch-progress').value=total?100*done/total:0;
  $('batch-message').textContent=batch.message;
  $('batch-pause').hidden=!['queued','running'].includes(batch.state);
  $('batch-resume').hidden=!['paused','interrupted','failed','completed_with_errors'].includes(batch.state);
  $('batch-resume').textContent=batch.state==='completed_with_errors'?'Relancer les erreurs':'Reprendre';
  $('batch-cancel').hidden=['completed','cancelled','cancelling','unavailable'].includes(batch.state);
  const expanded=new Set([...$('batch-items').querySelectorAll('details[open]')].map(el=>el.dataset.projectId));
  const list=$('batch-items');list.replaceChildren();
  batch.items.forEach((item,index)=>{
    const row=document.createElement('div');row.className='queue-item';row.dataset.state=item.state;
    const head=document.createElement('div');head.className='queue-item-heading';
    const order=document.createElement('span');order.className='queue-order';order.textContent=String(index+1).padStart(2,'0');
    const title=document.createElement('button');title.type='button';title.className='queue-project';title.textContent=item.title;title.disabled=Boolean(item.missing_source);if(!item.missing_source)title.addEventListener('click',()=>openProject(item.project_id));
    const badge=document.createElement('span');badge.className='status-pill';badge.dataset.state=item.state;badge.textContent=tr(batchLabels[item.state]||item.state);
    head.append(order,title,formatBadge(item),badge);row.append(head);
    const info=document.createElement('p');info.className='hint';info.textContent=`${item.pages_done}/${item.total} pages${item.seconds!==undefined?` · ${duration(item.seconds)}`:''}${item.pages_skipped?` · ${item.pages_skipped} réutilisée(s)`:''}`;row.append(info);
    if(item.state==='running') {const detail=document.createElement('small');detail.className='queue-live';detail.textContent=item.message||'Chargement des moteurs…';row.append(detail);}
    if(item.error || item.warnings?.length) {const detail=document.createElement('details');detail.dataset.projectId=item.project_id;detail.open=expanded.has(item.project_id);const summary=document.createElement('summary');summary.textContent=item.error?'Voir l’erreur':`${item.warnings.length} point(s) à relire`;const pre=document.createElement('pre');pre.textContent=item.error||item.warnings.join('\n');detail.append(summary,pre);row.append(detail);}
    if(item.export_ready){const link=document.createElement('button');link.type='button';link.className='export-link';link.append(window.workspaceIcon('download'),document.createTextNode(`${tr('Télécharger')} ${fileExtension(item).toUpperCase()}`));link.addEventListener('click',()=>downloadChapter(item.project_id,item.title,link,item));row.append(link);}
    list.append(row);
  });
}

async function refreshBatches() {
  if(state.batchFetching)return;
  state.batchFetching=true;clearTimeout(state.batchTimer);
  try {
    const data=await request('/api/batches');state.batches=data.batches;renderDashboard();const wasActive=state.batchActive;
    state.batchActive=Boolean(data.active_id);
    const wanted=data.batches.find(b=>b.id===state.batchId)||data.batches.find(b=>b.id===data.active_id)||data.batches[0];
    if(wanted)state.batchId=wanted.id;
    const history=$('batch-history');const signature=JSON.stringify(data.batches.map(b=>[b.id,b.state]));
    if(history.dataset.signature!==signature) {history.replaceChildren();for(const [i,b] of data.batches.entries()){const option=document.createElement('option');option.value=b.id;option.textContent=batchOptionLabel(b,i,data.batches.length);history.append(option);}if(!data.batches.length)history.append(new Option('Aucun lot',''));history.dataset.signature=signature;}
    history.value=state.batchId||'';renderBatch(wanted);
    state.singleJob=data.active_job;
    $('queue-single').hidden=!data.active_job;
    if(data.active_job)$('queue-single-message').textContent=`${data.active_job.title} · ${data.active_job.pages_done}/${data.active_job.total} pages · ${data.active_job.message}`;
    const active=data.batches.find(b=>b.id===data.active_id);
    $('queue-badge').hidden=!active;$('queue-badge').textContent=active?active.items.filter(i=>['pending','running'].includes(i.state)).length:0;
    if(state.engineStatus)$('batch-models').textContent=`${state.engineStatus.translation?.name||'Traduction'} · ${state.engineStatus.inpainting?.name||'Nettoyage'}`;
    selectionChanged();lockModels(state.processing||state.batchActive);
    lockEditor(state.processing);
    if(wasActive&&!state.batchActive) {
      await loadProjects();
      if(state.project&&!state.dirty&&!$('editor').hidden)await openProject(state.project.project_id);
    }
  } catch(error) {$('batch-message').textContent=`Suivi indisponible : ${error.message}`;}
  finally {state.batchFetching=false;state.batchTimer=setTimeout(refreshBatches,state.batchActive||state.singleJob?1400:7000);}
}

$('batch-form').addEventListener('submit',async event=>{
  event.preventDefault();$('batch-start').disabled=true;
  try {
    const modes=batchStageModes();
    if(!state.workspace)throw Error(tr('Choisissez un projet.'));
    if($('batch-scope').value==='selected'&&!state.selected.size)throw Error(tr('Sélectionnez au moins un fichier.'));
    const data=await postJson('/api/batches',{workspace_id:state.workspace.id,scope:$('batch-scope').value,project_ids:[...state.selected],stages:modes[$('batch-stages').value],
      skip_existing:$('batch-skip').checked,auto_export:$('batch-export').checked,retranslate:$('batch-retranslate').checked,glossary:glossaryFrom($('batch-glossary').value)});
    state.batchId=data.batch.id;state.batchActive=true;renderBatch(data.batch);flash('File lancée. Les fichiers seront traités successivement.');await navigate('batch');refreshBatches();
  }catch(error){flash(error.message);selectionChanged();}
});
for(const action of ['pause','resume','cancel']) $(`batch-${action}`).addEventListener('click',async()=>{
  const button=$(`batch-${action}`);button.disabled=true;
  try {const data=await postJson(`/api/batches/${state.batchId}/${action}`);renderBatch(data.batch);await refreshBatches();}
  catch(error){flash(error.message);}finally{button.disabled=false;}
});
$('batch-history').addEventListener('change',()=>{state.batchId=$('batch-history').value;refreshBatches();});
$('open-queue').addEventListener('click',()=>navigate('batch'));
$('queue-projects').addEventListener('click',()=>navigate('library'));
$('queue-single-open').addEventListener('click',()=>{if(state.singleJob)openProject(state.singleJob.project_id);});
$('project-search').addEventListener('input',renderProjectList);
$('select-all-projects').addEventListener('change',()=>{for(const p of visibleProjects().filter(p=>!p.error)){if($('select-all-projects').checked)state.selected.add(p.project_id);else state.selected.delete(p.project_id);}renderProjectList();});
$('clear-selection').addEventListener('click',()=>{state.selected.clear();renderProjectList();});
for(const name of ['home','library','models','settings','batch']) $(`nav-${name}`).addEventListener('click',()=>navigate(name));
$('bulk-path-form').addEventListener('submit',async event=>{
  event.preventDefault();$('bulk-import-submit').disabled=true;$('import-feedback').hidden=false;$('import-feedback').textContent='Import des fichiers…';
  try {const data=await postJson('/api/import-batch',{workspace_id:state.workspace?.id,paths:$('bulk-paths').value.split('\n').map(p=>p.trim()).filter(Boolean),source_language:$('source-language').value,target_language:$('target-language').value});
    data.projects.forEach(p=>state.selected.add(p.project_id));await loadProjects();
    $('import-feedback').textContent=`${data.projects.length} fichier(s) importé(s)${data.errors.length?` · ${data.errors.length} échec(s)\n${data.errors.map(e=>e.message).join('\n')}`:''}`;
  }catch(error){$('import-feedback').textContent=error.message;}finally{$('bulk-import-submit').disabled=false;}
});
const dropzone=$('import-dropzone');
dropzone.addEventListener('dragover',event=>{event.preventDefault();dropzone.classList.add('dragging');});
dropzone.addEventListener('dragleave',event=>{if(!dropzone.contains(event.relatedTarget))dropzone.classList.remove('dragging');});
dropzone.addEventListener('drop',event=>{event.preventDefault();dropzone.classList.remove('dragging');if(!$('upload').disabled)importFiles(Array.from(event.dataTransfer.files));});
$('batch-scope').addEventListener('change',selectionChanged);
const batchSettingIds=['batch-stages','batch-skip','batch-export','batch-retranslate','batch-glossary'];
try {const saved=JSON.parse(localStorage.getItem('renkavi-batch-settings')||'{}');for(const id of batchSettingIds)if(id in saved){if($(id).type==='checkbox')$(id).checked=saved[id];else $(id).value=saved[id];}}catch{}
for(const id of batchSettingIds) $(id).addEventListener('input',()=>{const saved={};for(const key of batchSettingIds)saved[key]=$(key).type==='checkbox'?$(key).checked:$(key).value;localStorage.setItem('renkavi-batch-settings',JSON.stringify(saved));selectionChanged();});



// Project workspace navigation. Historical project endpoints represent individual chapters.
function hideScreens() {
  for (const id of ['home-screen','projects-screen','settings-screen','models-dialog','library','editor','queue-screen']) $(id).hidden=true;
  document.body.classList.remove('editing');
}
async function navigate(name) {
  if (state.dirty && !(await saveProject())) return;
  hideScreens();
  document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.id===`nav-${name}`));
  const screens={home:'home-screen',library:'projects-screen',settings:'settings-screen',models:'models-dialog',batch:'queue-screen'};
  $(screens[name]||'home-screen').hidden=false;
  $('workspace-location').textContent=tr({home:'Accueil',library:'Projets',settings:'Paramètres',models:'Modèles',batch:'File de traitement'}[name]);
  if(name==='library'||name==='home')await loadWorkspaces();
  if(name==='models') { try {await Promise.all([refreshModelCatalog(),refreshModelHub()]);lockModels(state.processing||state.batchActive);} catch(error){flash(error.message);} }
  if(name==='batch'){state.batchId=state.batches?.find(b=>['queued','running','pausing','cancelling'].includes(b.state))?.id||state.batchId;await refreshBatches();}
}
function workspaceCard(workspace) {
  const card=document.createElement('button');card.type='button';card.className='workspace-card';
  const cover=document.createElement('span');cover.className='workspace-cover';cover.append(icon('photo'));
  if(workspace.page_count){const image=document.createElement('img');image.src=`/api/workspaces/${encodeURIComponent(workspace.id)}/cover`;image.alt='';image.loading='lazy';image.addEventListener('error',()=>image.remove());cover.append(image);}
  const body=document.createElement('div');body.className='workspace-card-body';
  const title=document.createElement('strong');title.textContent=workspace.title;
  const meta=document.createElement('small');meta.textContent=`${workspace.chapter_count||0} ${tr((workspace.chapter_count||0)===1?'fichier':'fichiers')} · ${workspace.page_count||0} ${(workspace.page_count||0)===1?'page':'pages'}`;
  body.append(title,meta);
  const footer=document.createElement('div');footer.className='workspace-card-footer';
  const lang=document.createElement('span');lang.className='workspace-language';lang.append((workspace.source_language||'auto').toUpperCase(),icon('arrow-right'),(workspace.target_language||'fr').toUpperCase());
  footer.append(lang,icon('chevron-right'));card.append(cover,body,footer);card.addEventListener('click',()=>openWorkspace(workspace.id));return card;
}
function renderWorkspaceCards() {
  const query=$('workspace-search').value.trim().toLocaleLowerCase();
  const matches=(state.workspaces||[]).filter(w=>!query||(w.title||'').toLocaleLowerCase().includes(query));
  $('workspace-total').textContent=`${matches.length} ${tr(matches.length===1?'projet':'projets')}`;
  for(const [id,items] of [['workspace-list',matches],['recent-workspaces',(state.workspaces||[]).slice(0,3)]]) {
    $(id).replaceChildren(...items.map(workspaceCard));
    if(!items.length){const empty=document.createElement('p');empty.className='library-empty';empty.textContent=tr(query?'Aucun projet ne correspond à cette recherche.':'Créez votre premier projet pour ajouter des fichiers.');$(id).append(empty);}
  }
  renderDashboard();
}
async function loadWorkspaces() {
  try {
    const data=await request('/api/workspaces');state.workspaces=data.workspaces||[];
    if(state.workspace)state.workspace=state.workspaces.find(w=>w.id===state.workspace.id)||state.workspace;
    renderWorkspaceCards();
  }catch(error){flash(error.message);}
}
async function openWorkspace(id) {
  if(state.dirty&&!(await saveProject()))return;
  try {
    const {workspace}=await request(`/api/workspaces/${encodeURIComponent(id)}`);state.workspace=workspace;state.selected.clear();state.project=null;$('batch-scope').value='all';
    hideScreens();$('library').hidden=false;$('workspace-heading').textContent=workspace.title;$('workspace-location').textContent=workspace.title;
    $('workspace-folder').textContent=workspace.folder||'';
    $('source-language').value=workspace.source_language||'en';$('target-language').value=workspace.target_language||'fr';
    document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.id==='nav-library'));
    renderProjectList();
  }catch(error){flash(error.message);}
}
async function downloadChapter(id,title,button,file){
  const details=file||state.projects.find(p=>p.project_id===id)||state.project;
  const query=new URLSearchParams(state.settings?.export||{format:'webp',quality:92});
  return downloadNative(`/api/projects/${encodeURIComponent(id)}/export?${query}`,`${title||id}.${fileExtension(details)}`,button);
}
function responseFilename(response,fallback){
  const disposition=response.headers.get('content-disposition')||'';
  const encoded=/filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition);
  if(encoded){try{return decodeURIComponent(encoded[1].trim()).split(/[\\/]/).pop();}catch{}}
  const plain=/filename\s*=\s*(?:"([^"\r\n]+)"|([^;\r\n]+))/i.exec(disposition);
  return (plain?.[1]||plain?.[2]||fallback).trim().split(/[\\/]/).pop();
}
async function downloadNative(path,fallback,button){
  if(button)button.disabled=true;
  try{
    if(window.pywebview?.api?.save_export){
      const url=new URL(path,location.origin),parts=url.pathname.split('/').filter(Boolean);
      const kind=parts[1]==='workspaces'?'workspace':'project';
      const options=Object.fromEntries(url.searchParams);
      if(options.quality!==undefined)options.quality=Number(options.quality);
      const result=await window.pywebview.api.save_export(kind,decodeURIComponent(parts[2]),options);
      if(result.saved)flash(`${tr('Export enregistré :')} ${result.filename}`);
      else flash(tr('Export annulé.'));
      return;
    }
    flash(tr('Préparation de l’export…'));
    const separator=path.includes('?')?'&':'?';
    const prepared=await request(`${path}${separator}prepare=1`);
    if(!prepared.download_url || !prepared.size)throw Error('Export vide ou lien indisponible.');
    // Download through the browser's native streaming stack, never a huge JS Blob.
    const link=document.createElement('a');link.href=prepared.download_url;
    link.download=prepared.filename||fallback;document.body.append(link);link.click();link.remove();
    flash(`Téléchargement lancé : ${prepared.filename||fallback} (${(prepared.size/1048576).toFixed(1)} Mo).`);
  }catch(error){flash(`Export impossible : ${error.message}`);}finally{if(button)button.disabled=false;}
}
$('export-workspace').addEventListener('click',()=>{if(state.workspace)downloadNative(`/api/workspaces/${encodeURIComponent(state.workspace.id)}/export`,`${state.workspace.title}.zip`,$('export-workspace'));});

const languageNames={auto:['Détection automatique','Automatic detection'],en:['Anglais','English'],fr:['Français','French'],zh:['Chinois','Chinese'],ja:['Japonais','Japanese'],ko:['Coréen','Korean'],es:['Espagnol','Spanish'],de:['Allemand','German'],it:['Italien','Italian'],pt:['Portugais','Portuguese'],ru:['Russe','Russian']};
function fillLanguages() {
  for(const id of ['source-language','target-language','workspace-source','workspace-target']) {
    const select=$(id),value=select.value|| (id.includes('source')?'en':'fr');select.replaceChildren();
    for(const [code,names] of Object.entries(languageNames)) {if(code==='auto'&&!id.includes('source'))continue;const option=new Option(names[window.uiLanguage==='en'?1:0],code);select.append(option);}
    select.value=value;
  }
}
async function initializeWorkspace() {
  fillLanguages();
  try {const data=await request('/api/settings');state.settings=data.settings;applySettings();}catch(error){flash(error.message);}
}
function applySettings() {
  state.settings ||= {ui_language:'fr',export:{format:'webp',quality:92}};
  $('ui-language').value=state.settings.ui_language||'fr';$('export-format').value=state.settings.export?.format||'webp';$('export-quality').value=state.settings.export?.quality||92;
  setUILanguage($('ui-language').value);fillLanguages();refreshLocalizedViews();
}
$('ui-language').addEventListener('change',()=>{setUILanguage($('ui-language').value);fillLanguages();refreshLocalizedViews();});
$('settings-form').addEventListener('submit',async event=>{
  event.preventDefault();
  try {const settings={ui_language:$('ui-language').value,export:{format:$('export-format').value,quality:Number($('export-quality').value)}};
    const data=await request('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json','X-Renkavi-Client':'1'},body:JSON.stringify({settings})});state.settings=data.settings||settings;applySettings();$('settings-message').textContent=tr('Paramètres enregistrés.');
  }catch(error){$('settings-message').textContent=error.message;}
});
function showWorkspaceForm() {navigate('library').then(()=>{$('workspace-form').hidden=false;$('workspace-name').focus();});}
$('new-workspace').addEventListener('click',showWorkspaceForm);$('home-new').addEventListener('click',showWorkspaceForm);$('home-projects').addEventListener('click',()=>navigate('library'));$('all-workspaces').addEventListener('click',()=>navigate('library'));
$('cancel-workspace').addEventListener('click',()=>{$('workspace-form').hidden=true;});
$('workspace-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try {const {workspace}=await postJson('/api/workspaces',{title:$('workspace-name').value.trim(),source_language:$('workspace-source').value,target_language:$('workspace-target').value});$('workspace-form').reset();$('workspace-form').hidden=true;await loadWorkspaces();await openWorkspace(workspace.id);}
  catch(error){flash(error.message);}finally{button.disabled=false;}
});
for(const mode of ['width','page']) $(`fit-${mode}`).addEventListener('click',()=>{state.fitMode=mode;renderPage();});
for(const panel of ['pages','regions']) $(`toggle-${panel}`).addEventListener('click',()=>{$('editor').classList.toggle(`hide-${panel}`);requestAnimationFrame(renderPage);});
window.addEventListener('resize',()=>{if(state.project&&state.fitMode)renderPage();});
function connectorPayload() {return {name:$('connector-name').value.trim(),provider:$('connector-provider').value,base_url:$('connector-url').value.trim(),model:$('connector-model').value.trim(),...($('connector-key').value?{api_key:$('connector-key').value}:{})};}
async function submitConnector(test) {
  if(!$('connector-form').reportValidity())return;
  if(state.processing||state.batchActive){flash(tr('Mettez le traitement en pause avant de changer les modèles.'));return;}
  const buttons=[...$('connector-form').querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);$('connector-message').textContent=tr('Connexion au service…');
  try {const data=await postJson(test?'/api/connectors/test':'/api/connectors',connectorPayload());$('connector-message').textContent=tr(test?'Connexion réussie.':'Connecteur ajouté. Sélectionnez-le comme modèle actif.');if(!test){$('connector-key').value='';await refreshModelCatalog();await loadModels();}if(test&&data.models?.length)$('connector-message').textContent+=' '+data.models.join(', ');}
  catch(error){$('connector-message').textContent=error.message;}finally{buttons.forEach(b=>b.disabled=false);}
}
$('connector-form').addEventListener('submit',event=>{event.preventDefault();submitConnector(false);});$('test-connector').addEventListener('click',()=>submitConnector(true));

$('copy-workspace-folder').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(state.workspace?.folder||'');flash(tr('Chemin copié.'));}catch(error){flash(error.message);}});
$('open-workspace-folder').addEventListener('click',async()=>{
  if(!state.workspace)return;
  try{await postJson(`/api/workspaces/${encodeURIComponent(state.workspace.id)}/open-folder`);flash('Dossier du projet ouvert dans Windows.');}
  catch(error){flash(`Impossible d’ouvrir le dossier : ${error.message}`);}
});
for(const id of ['source-language','target-language']) $(id).addEventListener('change',async()=>{
  if(!state.workspace)return;
  try {const {workspace}=await request(`/api/workspaces/${encodeURIComponent(state.workspace.id)}`,{method:'PUT',headers:{'Content-Type':'application/json','X-Renkavi-Client':'1'},body:JSON.stringify({source_language:$('source-language').value,target_language:$('target-language').value})});state.workspace=workspace;}
  catch(error){flash(error.message);$('source-language').value=state.workspace.source_language;$('target-language').value=state.workspace.target_language;}
});

// Icons are the official MIT licensed Tabler sprite, bundled with the application.
function icon(name){const svg=document.createElementNS(svgNS,'svg');svg.setAttribute('class','icon');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');const use=document.createElementNS(svgNS,'use');use.setAttribute('href',`/vendor/tabler-sprite.svg#tabler-${name}`);svg.append(use);return svg;}
window.workspaceIcon=icon;
function mountIcons(root=document){for(const el of root.querySelectorAll('[data-icon]'))if(!el.querySelector(':scope > .icon'))el.prepend(icon(el.dataset.icon));}
function uiElement(tag,className,text){const el=document.createElement(tag);if(className)el.className=className;if(text!==undefined)el.textContent=text;return el;}
function byteSize(bytes){if(!Number.isFinite(Number(bytes))||!Number(bytes))return tr('Taille non indiquée');const amount=Number(bytes);if(amount>=1073741824)return `${(amount/1073741824).toFixed(1)} GB`;if(amount>=1048576)return `${Math.round(amount/1048576)} MB`;return `${Math.round(amount/1024)} KB`;}
function kindName(kind){return tr({ocr:'Reconnaissance OCR',translation:'Traduction',inpainting:'Restauration'}[kind]||kind);}
function kindIcon(kind){return {ocr:'scan',translation:'language',inpainting:'brush'}[kind]||'cpu';}
const downloadLabels={downloading:'Téléchargement',paused:'En pause',downloaded:'Prêt à installer',installing:'Installation',installed:'Installé',failed:'Échec',cancelled:'Annulé',queued:'En attente'};
Object.assign(state,{hubModels:[],downloads:[],hubKind:'all',hubFetching:false,modelTab:'installed',system:null});
function renderDashboard(){
  const workspaces=state.workspaces||[],engines=state.engineStatus;
  $('dashboard-projects').textContent=workspaces.length;
  $('dashboard-chapters').textContent=workspaces.reduce((sum,w)=>sum+(w.chapter_count||0),0);
  $('dashboard-pages').textContent=`${workspaces.reduce((sum,w)=>sum+(w.page_count||0),0)} pages`;
  const ready=['ocr','translation','inpainting'].filter(kind=>engines?.[kind]?.ready).length;$('dashboard-engines').textContent=`${ready} / 3`;
  const active=(state.batches||[]).filter(b=>['queued','running','pausing'].includes(b.state));
  $('dashboard-queue').textContent=active.reduce((sum,b)=>sum+b.items.filter(i=>['pending','queued','running'].includes(i.state)).length,0);
  $('dashboard-queue-label').textContent=tr(active.length?'Traitement en cours':'Aucun traitement actif');
  $('dashboard-system-status').textContent=tr(ready===3?'Prêt':'À configurer');$('dashboard-system-status').dataset.state=ready===3?'installed':'paused';
  const system=$('dashboard-system');system.replaceChildren();
  for(const kind of ['ocr','translation','inpainting']){const engine=engines?.[kind],row=uiElement('div','system-item'),glyph=uiElement('span');glyph.append(icon(kindIcon(kind)));const copy=uiElement('div');copy.append(uiElement('strong','',kindName(kind)),uiElement('small','',engine?.name||tr('Non configuré')));const dot=uiElement('i',`engine-dot${engine?.ready?' ready':''}`);row.append(glyph,copy,dot);system.append(row);}
  if(state.system){const row=uiElement('div','system-item');const glyph=uiElement('span');glyph.append(icon('cpu'));const copy=uiElement('div');copy.append(uiElement('strong','',state.system.gpu?.name||'CPU'),uiElement('small','',`${byteSize(state.system.memory?.available_bytes)} ${tr('RAM disponible')}`));row.append(glyph,copy);system.append(row);}
  const resources=$('dashboard-resources');resources.replaceChildren();
  if(state.system){for(const [label,used,total] of [[tr('Mémoire RAM'),(state.system.memory?.total_bytes||0)-(state.system.memory?.available_bytes||0),state.system.memory?.total_bytes],[tr('Stockage'),state.system.disk?.used_bytes,state.system.disk?.total_bytes]]){if(!total)continue;const meter=uiElement('div','resource-meter'),line=uiElement('div');line.append(uiElement('span','',label),uiElement('small','',`${byteSize(total-used)} ${tr('libres')}`));const progress=document.createElement('progress');progress.max=100;progress.value=Math.round(100*used/total);meter.append(line,progress);resources.append(meter);}}
  const activity=$('dashboard-activity');activity.replaceChildren();
  for(const batch of (state.batches||[]).slice(0,4)){const row=uiElement('div','activity-row'),glyph=uiElement('span');glyph.append(icon(batch.state==='completed'?'circle-check':'list-check'));const copy=uiElement('div');copy.append(uiElement('strong','',batch.items.map(i=>i.title).slice(0,2).join(', ')),uiElement('small','',`${batch.items.length} ${tr('fichiers')} · ${batch.items.reduce((sum,i)=>sum+(i.pages_done||0),0)} pages`));const badge=uiElement('span','status-pill',tr(batchLabels[batch.state]||batch.state));badge.dataset.state=batch.state;row.append(glyph,copy,badge);activity.append(row);}
  if(!activity.children.length)activity.append(uiElement('p','empty-note',tr('Aucun traitement pour le moment. Lancez un lot depuis un projet.')));
}
function renderInstalledModels(){
  const list=$('installed-model-cards');list.replaceChildren();const models=state.engineStatus;
  $('installed-device').textContent=state.system?.gpu?.name||models?.device||tr('Détection du matériel…');
  $('installed-count').textContent=state.modelCatalog?.models?.filter(m=>m.available).length||0;
  for(const kind of ['ocr','translation','inpainting']){const engine=models?.[kind],card=uiElement('article','card installed-engine-card'),head=uiElement('div','hub-card-header'),glyph=uiElement('span','hub-model-icon');glyph.dataset.kind=kind;glyph.append(icon(kindIcon(kind)));const status=uiElement('span','status-pill',tr(engine?.ready?'Prêt':'À configurer'));status.dataset.state=engine?.ready?'installed':'paused';head.append(glyph,status);card.append(head,uiElement('h3','',kindName(kind)),uiElement('strong','installed-engine-name',engine?.name||tr('Non configuré')),uiElement('p','',tr(kind==='ocr'?'Détection des zones et reconnaissance du texte.':'Les nouveaux traitements utilisent le modèle sélectionné ci-dessous.')));list.append(card);}
}
async function loadSystem(){try{state.system=await request('/api/system');renderDashboard();renderInstalledModels();}catch{/* Existing engine status remains usable on older servers. */}}
function showModelTab(tab){state.modelTab=tab;for(const key of ['installed','catalog','connections']){$(`model-${key}-panel`).hidden=key!==tab;$(`tab-${key}`).classList.toggle('active',key===tab);$(`tab-${key}`).setAttribute('aria-selected',String(key===tab));}if(tab==='catalog')refreshModelHub();}
async function refreshModelHub(){
  if(state.hubFetching)return;state.hubFetching=true;
  try{const data=await request('/api/model-hub');state.hubModels=data.models||[];state.downloads=data.downloads||[];$('model-hub-error').hidden=true;renderModelHub();renderDownloads();renderInstalledModels();scheduleDownloadPoll();}
  catch(error){$('model-hub-error').hidden=false;$('model-hub-error').textContent=error.message;}
  finally{state.hubFetching=false;}
}
function modelDownload(model){return state.downloads.find(d=>d.id===model.download_id)||state.downloads.find(d=>d.model_id===model.id&&!['cancelled','failed'].includes(d.state))||state.downloads.find(d=>d.model_id===model.id);}
function renderModelHub(){
  const list=$('model-hub-list'),query=$('model-hub-search').value.trim().toLocaleLowerCase();list.replaceChildren();
  const models=state.hubModels.filter(m=>(state.hubKind==='all'||m.kind===state.hubKind)&&(!query||[m.name,m.repository,m.description,m.runtime].filter(Boolean).join(' ').toLocaleLowerCase().includes(query)));
  for(const model of models){
    const download=modelDownload(model),card=uiElement('article','card hub-card');card.dataset.modelId=model.id;
    const head=uiElement('div','hub-card-header'),glyph=uiElement('span','hub-model-icon');glyph.dataset.kind=model.kind;glyph.append(icon(kindIcon(model.kind)));head.append(glyph,uiElement('span','subtle-tag',kindName(model.kind)));
    const title=uiElement('h3','',model.name),repo=uiElement('span','hub-repo',model.repository||model.runtime||'');
    const description=uiElement('p','hub-description',tr(model.description||''));
    const specs=uiElement('div','hub-specs');for(const spec of [model.runtime,model.memory_gb?`${model.memory_gb} GB RAM`:null,model.ram_gb?`${model.ram_gb} GB RAM`:null,Array.isArray(model.languages)?model.languages.join(' / '):model.languages,model.license].filter(Boolean))specs.append(uiElement('span','',String(spec)));
    const footer=uiElement('div','hub-card-footer');const size=uiElement('small','',byteSize(model.size_bytes));if(model.model_card){const source=document.createElement('a');source.href=model.model_card;source.target='_blank';source.rel='noopener noreferrer';source.className='hub-source';source.title=tr('Voir la source');source.append(icon('external-link'));size.append(source);}const action=document.createElement('button');action.type='button';
    const installed=model.installed||download?.state==='installed',downloading=download&&['downloading','paused','queued','installing'].includes(download.state),downloaded=model.downloaded||download?.state==='downloaded';
    if(installed){action.append(icon('check'),document.createTextNode(tr('Installé')));action.disabled=true;}
    else if(downloading){action.append(icon(download.state==='paused'?'player-play':'player-pause'),document.createTextNode(tr(download.state==='paused'?'Reprendre':download.state==='installing'?'Installation':'Pause')));action.disabled=download.state==='installing';action.addEventListener('click',()=>downloadAction(download,download.state==='paused'?'resume':'pause'));}
    else if(downloaded){action.disabled=state.processing||state.batchActive;action.className='primary';action.append(icon('device-floppy'),document.createTextNode(tr('Installer')));action.addEventListener('click',()=>downloadAction(download,'install'));}
    else if(model.downloadable===false){action.append(icon('external-link'),document.createTextNode(tr('Voir la source')));if(model.model_card){action.addEventListener('click',()=>window.open(model.model_card,'_blank','noopener,noreferrer'));}else action.disabled=true;}
    else{action.className='primary';action.append(icon('download'),document.createTextNode(tr('Télécharger')));action.addEventListener('click',()=>startModelDownload(model,action));}
    footer.append(size,action);card.append(head,title,repo,description,specs);if(model.memory_note)card.append(uiElement('p','hub-memory-note',tr(model.memory_note)));card.append(footer);
    if(download&&!['installed','cancelled'].includes(download.state))card.append(downloadProgress(download));
    list.append(card);
  }
  if(!models.length)list.append(uiElement('p','library-empty',tr('Aucun modèle ne correspond à ces filtres.')));
}
function downloadProgress(download){const box=uiElement('div','hub-progress'),progress=document.createElement('progress');progress.max=100;const done=download.bytes_done??download.bytes_downloaded??0,total=download.total_bytes||0;progress.value=total?100*done/total:Math.round((download.progress||0)*100);const line=uiElement('div');line.append(uiElement('span','',tr(downloadLabels[download.state]||download.state)),uiElement('span','',`${Math.round(progress.value)} %`));box.append(progress,line);return box;}
function downloadRow(download,compact=false){
  const row=uiElement('div','download-row');row.dataset.downloadId=download.id;const model=state.hubModels.find(m=>m.id===download.model_id),glyph=uiElement('span','hub-model-icon');glyph.dataset.kind=model?.kind||'translation';glyph.append(icon(kindIcon(model?.kind)));const info=uiElement('div','download-info');info.append(uiElement('strong','',model?.name||download.name||download.model_id));
  const done=download.bytes_done??download.bytes_downloaded??0,total=download.total_bytes||0;
  if(!['installed','cancelled','failed'].includes(download.state))info.append(downloadProgress(download));
  info.append(uiElement('small','',`${byteSize(done)} / ${byteSize(total)}${download.speed_bps?' · '+byteSize(download.speed_bps)+'/s':''}`));
  if(download.error)info.append(uiElement('p','download-error',String(download.error)));
  const badge=uiElement('span','status-pill',tr(downloadLabels[download.state]||download.state));badge.dataset.state=download.state;const controls=uiElement('div','download-controls');
  const actions={downloading:['pause','cancel'],queued:['cancel'],paused:['resume','cancel'],downloaded:['install'],failed:['resume'],cancelled:['resume']};
  for(const action of actions[download.state]||[]){const button=document.createElement('button');button.type='button';button.append(icon({pause:'player-pause',resume:'player-play',cancel:'x',install:'device-floppy'}[action]),document.createTextNode(tr({pause:'Pause',resume:'Reprendre',cancel:'Annuler',install:'Installer'}[action])));if(action==='install'){button.className='primary';button.disabled=state.processing||state.batchActive;}button.addEventListener('click',()=>downloadAction(download,action,button));controls.append(button);}
  row.append(glyph,info,badge);if(!compact)row.append(controls);return row;
}
function renderDownloads(){
  const downloads=state.downloads,active=downloads.filter(d=>['downloading','paused','queued','installing','downloaded'].includes(d.state));
  $('download-library-count').textContent=downloads.length;$('dashboard-download-count').textContent=active.length;
  $('download-library-list').replaceChildren(...downloads.map(d=>downloadRow(d)));
  if(!downloads.length)$('download-library-list').append(uiElement('p','empty-note',tr('Les modèles téléchargés apparaîtront ici. Parcourez le catalogue pour commencer.')));
  $('dashboard-downloads').replaceChildren(...active.slice(0,2).map(d=>downloadRow(d,true)));
  if(!active.length)$('dashboard-downloads').append(uiElement('p','empty-note',tr('Aucun téléchargement en cours.')));
}
async function startModelDownload(model,button){button.disabled=true;try{const {download}=await postJson('/api/model-hub/download',{model_id:model.id});state.downloads=[download,...state.downloads.filter(d=>d.id!==download.id)];renderModelHub();renderDownloads();scheduleDownloadPoll();}catch(error){flash(error.message);button.disabled=false;}}
async function downloadAction(download,action,button){if(!download)return;if(action==='install'&&(state.processing||state.batchActive)){flash(tr('Mettez le traitement en pause avant de changer les modèles.'));return;}if(button)button.disabled=true;try{const data=await postJson(`/api/model-hub/downloads/${encodeURIComponent(download.id)}/${action}`);state.downloads=state.downloads.map(d=>d.id===download.id?data.download:d);renderDownloads();renderModelHub();scheduleDownloadPoll();if(action==='install'){await Promise.all([refreshModelCatalog(),loadModels(),refreshModelHub()]);}}catch(error){flash(error.message);}finally{if(button)button.disabled=false;}}
function scheduleDownloadPoll(){clearTimeout(state.downloadTimer);if(state.downloads.some(d=>['downloading','queued','installing'].includes(d.state)))state.downloadTimer=setTimeout(pollDownloads,1200);}
async function pollDownloads(){try{const {downloads}=await request('/api/model-hub/downloads');const wasInstalling=state.downloads.some(d=>d.state==='installing');state.downloads=downloads||[];renderDownloads();renderModelHub();if(wasInstalling&&!state.downloads.some(d=>d.state==='installing'))await Promise.all([refreshModelCatalog(),loadModels(),refreshModelHub()]);}catch(error){$('model-hub-error').textContent=error.message;$('model-hub-error').hidden=false;}finally{scheduleDownloadPoll();}}
let chaptersToMove=[];
async function showMoveChapters(ids,title){
  if(state.processing||state.batchActive){flash(tr('Mettez le traitement en pause avant de déplacer des fichiers.'));return;}
  chaptersToMove=ids;await loadWorkspaces();const choices=(state.workspaces||[]).filter(w=>w.id!==state.workspace?.id);
  if(!choices.length){flash(tr('Créez un autre projet pour déplacer ces fichiers.'));return;}
  $('move-chapter-name').textContent=title||`${ids.length} ${tr('fichiers sélectionnés')}`;$('move-chapter-workspace').replaceChildren(...choices.map(w=>new Option(w.title,w.id)));$('move-chapter-dialog').showModal();
}
$('move-chapter-form').addEventListener('submit',async event=>{
  event.preventDefault();if(state.processing||state.batchActive)return;const button=event.submitter;button.disabled=true;
  try{const target=$('move-chapter-workspace').value;for(const chapter_id of chaptersToMove)await postJson(`/api/workspaces/${encodeURIComponent(target)}/chapters`,{chapter_id});state.selected.clear();$('move-chapter-dialog').close();await loadProjects();renderProjectList();flash(tr('Fichiers déplacés.'));}
  catch(error){flash(error.message);await loadProjects();renderProjectList();}finally{button.disabled=false;}
});
$('close-move-chapter').addEventListener('click',()=>$('move-chapter-dialog').close());
const moveSelected=document.createElement('button');moveSelected.id='move-selected-chapters';moveSelected.type='button';moveSelected.className='text-button';moveSelected.disabled=true;moveSelected.append(icon('transfer'),document.createTextNode(tr('Déplacer')));moveSelected.addEventListener('click',()=>showMoveChapters([...state.selected]));$('clear-selection').before(moveSelected);
$('rename-workspace').addEventListener('click',()=>{$('rename-workspace-form').hidden=false;$('rename-workspace-name').value=state.workspace?.title||'';$('rename-workspace-name').focus();});
$('cancel-rename-workspace').addEventListener('click',()=>{$('rename-workspace-form').hidden=true;});
$('rename-workspace-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const {workspace}=await request(`/api/workspaces/${encodeURIComponent(state.workspace.id)}`,{method:'PUT',headers:{'Content-Type':'application/json','X-Renkavi-Client':'1'},body:JSON.stringify({title:$('rename-workspace-name').value.trim()})});state.workspace=workspace;$('workspace-heading').textContent=workspace.title;$('workspace-location').textContent=workspace.title;$('rename-workspace-form').hidden=true;await loadWorkspaces();}catch(error){flash(error.message);}finally{button.disabled=false;}});
$('workspace-search').addEventListener('input',renderWorkspaceCards);
for(const tab of ['installed','catalog','connections'])$(`tab-${tab}`).addEventListener('click',()=>showModelTab(tab));
$('model-hub-search').addEventListener('input',renderModelHub);
for(const button of $('model-kind-filters').querySelectorAll('button'))button.addEventListener('click',()=>{state.hubKind=button.dataset.kind;for(const b of $('model-kind-filters').children)b.classList.toggle('active',b===button);renderModelHub();});
$('dashboard-configure').addEventListener('click',()=>{navigate('models');showModelTab('installed');});
$('dashboard-catalog').addEventListener('click',()=>{navigate('models');showModelTab('catalog');});
mountIcons();renderDashboard();renderDownloads();renderInstalledModels();
initializeWorkspace();initialize();refreshBatches();refreshModelHub();loadSystem();

function batchOptionLabel(batch,index,total){return `${tr('Lot')} ${total-index} · ${batch.items.length} ${tr('fichiers')} · ${tr(batchLabels[batch.state]||batch.state)}`;}
function refreshLocalizedViews(){
  renderProjectList();renderWorkspaceCards();renderDashboard();renderInstalledModels();renderModelHub();renderDownloads();
  if(state.batch)renderBatch(state.batch);
  const batches=state.batches||[];for(const [index,option] of [...$('batch-history').options].entries())if(batches[index])option.textContent=batchOptionLabel(batches[index],index,batches.length);
  if(state.project)configureDocumentEditor();
}
