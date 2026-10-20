/* File-based ChatGPT workflow; never loads a local translation model. */
let chatgptPreview=null;

function updateChatGPTMode(){
  const active=chatgptMode();
  $('chatgpt-panel').hidden=!active;
  $('process-translate').hidden=active;
  setButtonLabel($('process-all'),active?'Préparer pour ChatGPT':'Traduction complète','player-play');
  const all=$('batch-stages').querySelector('option[value="all"]');
  all.textContent=tr(active?'Préparation OCR pour ChatGPT':'Traduction complète');
  $('batch-stages').querySelector('option[value="translate"]').disabled=active;
  if(active&&$('batch-stages').value==='translate')$('batch-stages').value='ocr';
  if(active){
    $('engine-translation-name').textContent='ChatGPT · JSON';$('engine-translation-status').textContent=tr('Échange de fichiers');
    $('batch-models').textContent=`ChatGPT · JSON · ${state.engineStatus?.inpainting?.name||tr('Nettoyage')}`;
    $('model-summary').textContent=tr('Traduction dans ChatGPT. OCR et nettoyage locaux.');
  }
  syncChatGPTBatch();
  if(state.project)lockEditor(state.processing);
  selectionChanged();
}

function syncChatGPTBatch(){
  const disabled=chatgptMode()&&$('batch-stages').value!=='render';
  $('batch-export').disabled=disabled;if(disabled)$('batch-export').checked=false;
}
$('batch-stages').addEventListener('change',syncChatGPTBatch);

async function refreshChatGPTExchange(){
  if(!state.project)return;
  const pid=state.project.project_id;
  try{
    const data=await request(`/api/projects/${encodeURIComponent(pid)}/chatgpt`);
    if(state.project?.project_id!==pid)return;
    state.chatgptExchange=data.exchange;
    $('chatgpt-path').textContent=data.exchange?.request_path||'';
    $('chatgpt-status').textContent=data.exchange?
      (data.exchange.imported?tr('Traductions importées. Lancez le nettoyage et le rendu.'):
       data.exchange.response_ready?tr('Réponse disponible dans le dossier. Vous pouvez la vérifier avant import.'):
       `${data.exchange.count} ${tr('répliques à traduire. Copiez la consigne ou joignez demande.json à ChatGPT.')}`):
      tr('Commencez par préparer le document pour ChatGPT.');
  }catch(error){$('chatgpt-status').textContent=error.message;}
}

async function createChatGPTExchange(){
  if(!state.project||state.processing||state.batchActive)return null;
  if(!(await saveProject()))return null;
  const pid=state.project.project_id;
  const data=await postJson(`/api/projects/${encodeURIComponent(pid)}/chatgpt/export`,{expected_revision:state.project.revision});
  if(state.project?.project_id===pid){state.chatgptExchange=data.exchange;await refreshChatGPTExchange();}
  return data.exchange;
}

async function prepareChatGPT(){
  try{
    if(!state.project||state.processing||state.batchActive)return;
    if(isWordDocument(state.project)){await createChatGPTExchange();return;}
    $('process-scope').value='chapter';
    state.chatgptAfterOCR=state.project.project_id;
    const reference=await processStages(['ocr']);
    if(!reference)state.chatgptAfterOCR=null;
  }catch(error){state.chatgptAfterOCR=null;flash(error.message);}
}

$('chatgpt-export').addEventListener('click',async()=>{
  try{
    const info=await createChatGPTExchange();if(!info)return;
    if(window.pywebview?.api?.save_export){
      const result=await window.pywebview.api.save_export('chatgpt',state.project.project_id,{exchange_id:info.exchange_id});
      if(result.saved)flash(tr('JSON enregistré.'));
    }else{
      const link=document.createElement('a');link.href=projectUrl(`/chatgpt/${info.exchange_id}/request`);link.download='demande.json';link.click();
    }
  }catch(error){flash(error.message);}
});

$('chatgpt-prompt').addEventListener('click',async()=>{
  try{
    if(!state.chatgptExchange||state.chatgptExchange.imported||state.dirty||state.chatgptExchange.revision!==state.project.revision)await createChatGPTExchange();
    if(!state.chatgptExchange)return;
    await navigator.clipboard.writeText(state.chatgptExchange.instructions);
    flash(tr('Consigne copiée. Collez-la dans ChatGPT avec Desktop Commander, ou joignez le JSON.'));
  }catch(error){flash(error.message);}
});

async function previewChatGPT(response){
  if(!state.project||state.processing||state.batchActive)return;
  if(!(await saveProject()))return;
  const pid=state.project.project_id,revision=state.project.revision;
  const body={expected_revision:revision,dry_run:true};
  if(response!==undefined)body.response=response;
  const result=await postJson(`/api/projects/${encodeURIComponent(pid)}/chatgpt/import`,body);
  if(state.project?.project_id!==pid||state.project.revision!==revision)return;
  chatgptPreview={pid,revision,response:result.response};
  $('chatgpt-preview-count').textContent=`${result.count} ${tr('traductions validées. Les corrections protégées sont conservées.')}`;
  const list=$('chatgpt-preview-lines');list.replaceChildren();
  for(const row of result.preview.slice(0,40)){
    const entry=document.createElement('article');
    for(const text of [row.page,row.source,row.before?`${tr('Avant')} : ${row.before}`:'',`${tr('Après')} : ${row.after}`]){
      if(!text)continue;const line=document.createElement('p');line.textContent=text;entry.append(line);
    }
    list.append(entry);
  }
  if(result.preview.length>40){const note=document.createElement('p');note.textContent=tr('Aperçu limité aux 40 premières répliques.');list.append(note);}
  $('chatgpt-preview').showModal();
}

$('chatgpt-folder-import').addEventListener('click',()=>previewChatGPT().catch(error=>flash(error.message)));
$('chatgpt-file-button').addEventListener('click',()=>$('chatgpt-file').click());
$('chatgpt-file').addEventListener('change',async event=>{
  const file=event.target.files[0];event.target.value='';if(!file)return;
  try{
    if(file.size>8*1024*1024)throw Error(tr('Réponse JSON trop volumineuse.'));
    await previewChatGPT(JSON.parse((await file.text()).replace(/^\uFEFF/,'')));
  }catch(error){flash(error.message);}
});
$('chatgpt-cancel').addEventListener('click',()=>{$('chatgpt-preview').close();chatgptPreview=null;});
$('chatgpt-apply').addEventListener('click',async()=>{
  const pending=chatgptPreview;if(!pending)return;
  $('chatgpt-apply').disabled=true;
  try{
    if(state.project?.project_id!==pending.pid||state.dirty)throw Error(tr('Le document a changé. Vérifiez de nouveau la réponse.'));
    const data=await postJson(`/api/projects/${encodeURIComponent(pending.pid)}/chatgpt/import`,{expected_revision:pending.revision,response:pending.response});
    if(state.project?.project_id!==pending.pid)return;
    state.project=data.project;state.dirty=false;state.regionId=null;state.variant='original';$('view-variant').value='original';
    $('chatgpt-preview').close();chatgptPreview=null;
    updateProjectMeta();renderPages();renderPage();await refreshChatGPTExchange();
    flash(tr('Traductions importées. Lancez le nettoyage et le rendu.'));
  }catch(error){flash(error.message);}finally{$('chatgpt-apply').disabled=false;}
});
updateChatGPTMode();
