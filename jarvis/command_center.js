'use strict';
const $ = selector => document.querySelector(selector);
const freshToken = new URLSearchParams(location.hash.slice(1)).get('token');
if (freshToken) sessionStorage.setItem('jarvis-session', freshToken);
const token = sessionStorage.getItem('jarvis-session');
history.replaceState(null, '', location.pathname);
let state = {agents: [], projects: [], providers: {}, integrations: []};
let project = localStorage.getItem('jarvis-project'), selected = localStorage.getItem('jarvis-agent');
let chat = null;
let agentSelections = {};
try {agentSelections = JSON.parse(localStorage.getItem('jarvis-agent-selections') || '{}');} catch { /* ignore corrupt local preferences */ }
if (selected && agentSelections[selected]) ({project,chat} = agentSelections[selected]);
let thread = {messages: [], work: [], proposals: [], artifacts: []}, tab = 'activity', files = null;
let refreshBusy = false, sending = false, messageSignature = '', selectionVersion = 0;
let pendingNewProject = null;
function draftKey() {return `jarvis-draft:${project}/${selected}/${chat || 'general'}`;}
function saveSelection() {
  if (!selected) return;
  sessionStorage.setItem(draftKey(), $('#message-body').value);
  agentSelections[selected] = {project,chat};
  localStorage.setItem('jarvis-agent-selections', JSON.stringify(agentSelections));
}
$('#message-body').value = sessionStorage.getItem(draftKey()) || '';
$('#message-body').addEventListener('input',saveSelection);
window.addEventListener('pagehide',saveSelection);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Durations use the server's clock so elapsed times are right even if this browser's clock drifts.
let clockSkew = 0;
const serverNow = () => Date.now() / 1000 + clockSkew;
function duration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
}
const PHASE_LABEL = {starting:'starting', connecting:'connecting to provider', responding:'receiving response'};
// Real subscription turns and offline simulation are always labeled differently; never merged.
function kindBadge(status) {
  // This badge names the provider kind only. Whether the agent is doing anything is the status line.
  if (status?.real_provider) return '<i class="kind live" title="Backed by a real provider. The status line says whether it is working.">REAL</i>';
  if (status?.simulated) return '<i class="kind demo" title="Offline simulation. No model is ever called.">DEMO</i>';
  return '<i class="kind off" title="No usable provider in this session.">NO PROVIDER</i>';
}
function turnTiming(turn) {
  if (turn.state === 'QUEUED') return `queued ${duration(serverNow() - turn.created_at)} ago`;
  if (turn.state === 'RUNNING' && turn.started_at) return `running ${duration(serverNow() - turn.started_at)} · ${PHASE_LABEL[turn.phase] || 'running'}`;
  if (turn.finished_at && turn.started_at) return `${turn.state === 'COMPLETED' ? 'finished' : 'ended'} after ${duration(turn.finished_at - turn.started_at)}`;
  if (turn.finished_at) return 'ended before it started';
  return {PAUSED:'paused · resume restarts it', INTERRUPTED:'interrupted by restart · resume restarts it'}[turn.state] || '';
}
let rosterSignature = '';
function renderRoster() {
  const html = state.agents.map(a => {
    const s = a.status || {code:'unknown', label:a.lifecycle, detail:''};
    const action = s.action ? `<button class="agent-card-action" data-agent-action="${esc(s.action)}" data-agent-id="${esc(a.agent_id)}">${s.action === 'start' ? 'Enable' : 'Resume'}</button>` : '';
    return `<div class="agent-card status-${esc(s.code)}${a.agent_id === selected ? ' active' : ''}" role="listitem">` +
      `<button class="agent-card-main" data-agent="${esc(a.agent_id)}" aria-label="${esc(a.display_name)}: ${esc(s.label)}">` +
      `<span class="agent-card-top"><strong>${esc(a.display_name)}</strong>${kindBadge(s)}</span>` +
      `<span class="agent-card-meta">${esc(a.role)} · ${esc(a.model_provider)} / ${esc(a.model_name)}</span>` +
      `<span class="status-pill"><i class="dot"></i>${esc(s.label)}</span>` +
      `<span class="agent-card-detail">${esc(s.detail)}</span></button>${action}</div>`;
  }).join('') || '<p class="muted">No agents yet. Use ＋ New Agent.</p>';
  if (html !== rosterSignature) {$('#agent-roster').innerHTML = html; rosterSignature = html;}
}
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers: {'Authorization': `Bearer ${token}`, 'Content-Type':'application/json'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed.');
  return result;
}
function toast(message) {$('#toast').textContent = message; $('#toast').classList.add('show'); setTimeout(() => $('#toast').classList.remove('show'), 4500);}
function agent() {return state.agents.find(a => a.agent_id === selected && a.project_id === project);}
function activeWork() {
  const work = [...thread.work,...(thread.live_turns || []).map(w=>({...w,run_id:w.turn_id,live:true}))];
  return work.find(w=>['RUNNING','AWAITING_REPLY'].includes(w.state)) || work.find(w=>['PAUSED','INTERRUPTED','QUEUED'].includes(w.state));
}
function selectThread(nextProject, nextAgent, nextChat=null) {
  saveSelection();
  project = nextProject; selected = nextAgent; chat = nextChat; selectionVersion++;
  localStorage.setItem('jarvis-project', project || ''); localStorage.setItem('jarvis-agent', selected || '');
  $('#message-body').value = sessionStorage.getItem(draftKey()) || '';
  thread = {messages:[],work:[],proposals:[],artifacts:[]}; files = null; messageSignature = '';
  render(); refresh();
}
async function refresh() {
  if (refreshBusy) return;
  refreshBusy = true; const version = selectionVersion;
  try {
    state = await api('/api/state');
    if (state.server_time) clockSkew = state.server_time - Date.now() / 1000;
    if (!state.agents.some(a => a.agent_id === selected)) {selected = state.agents[0]?.agent_id; chat = null;}
    if (selected) project = state.agents.find(a => a.agent_id === selected).project_id;
    else if (!state.projects.some(p => p.project_id === project)) project = state.projects[0]?.project_id;
    const p = project, a = selected, c = chat;
    const result = a ? await api(`/api/conversation?project=${encodeURIComponent(p)}&agent=${encodeURIComponent(a)}${c ? '&chat='+encodeURIComponent(c):''}`) : {messages:[],work:[],proposals:[],artifacts:[]};
    if (version !== selectionVersion || p !== project || a !== selected || c !== chat) return;
    thread = result;
    $('#connection').textContent = state.conversation_health === 'AVAILABLE' ? '● Connected locally' : 'Conversation engine unavailable';
    render();
  } catch (error) {$('#connection').textContent = 'Disconnected'; toast(error.message);}
  finally {refreshBusy = false;}
}
function providerOptions(select) {
  const previous = select.value;
  select.innerHTML = Object.entries(state.providers).map(([id,p]) => `<option value="${esc(id)}">${esc(p.label)} · ${esc(id === 'offline-demo' ? 'simulated' : p.connection || 'gated')}</option>`).join('');
  if (previous) select.value = previous;
}
function render() {
  const agentSwitch = $('#agent-switch');
  const options = state.agents.map(a => `<option value="${a.agent_id}">${esc(a.display_name)}</option>`).join('') || '<option>No agents</option>';
  if (agentSwitch.innerHTML !== options) agentSwitch.innerHTML = options;
  agentSwitch.value = selected || '';
  const chats = `<button class="nav-item ${chat === null?'active':''}" data-chat="">General conversation</button>` + (thread.chats || []).map(c => `<button class="nav-item ${c.chat_id === chat?'active':''}" data-chat="${c.chat_id}">${esc(c.title)}</button>`).join('');
  $('#chats').innerHTML = selected ? chats : '<p class="muted">Create an agent to begin.</p>';
  $('#projects').innerHTML = state.projects.filter(p => !selected || p.project_id === project).map(p => `<div class="project-group"><button class="nav-item project-folder" data-project="${esc(p.project_id)}" aria-expanded="${p.project_id === project}">◇ ${esc(p.name)}</button>${p.project_id === project && selected ? `<div class="project-conversations">${chats}</div>`:''}</div>`).join('');
  const a = agent(), work = activeWork();
  $('#project-label').textContent = state.projects.find(p => p.project_id === project)?.name || 'PROJECT';
  $('#thread-title').textContent = chat ? (thread.chats || []).find(c=>c.chat_id === chat)?.title || 'Conversation' : a?.display_name || 'A place to work together';
  $('#thread-subtitle').textContent = a ? `${a.display_name} · ${a.role} · ${a.model_name}` : 'Create an agent or select a conversation.';
  const provider = state.providers[a?.model_provider];
  renderRoster();
  const status = a?.status;
  $('#agent-status').textContent = !a ? 'No agent selected' : `${status?.real_provider ? 'REAL' : status?.simulated ? 'DEMO' : 'NO PROVIDER'} · ${status?.label || a.lifecycle}`;
  $('#agent-status').dataset.code = status?.code || '';
  $('#agent-status').title = status?.detail || '';
  $('#provider-notice').textContent = a?.model_provider === 'offline-demo' ? 'OFFLINE SIMULATION · Scripted interaction test. No live model, tools or external services.' : `${provider?.reason || 'Live chat is gated.'} ${provider?.enabled ? 'Sending releases this text and this model segment’s chat history only. No tools or attachments. Model changes start a fresh context segment.' : 'Messages remain local while this provider is blocked.'}`;
  $('#send-message').disabled = !a || sending;
  $('#composer-model').textContent = a ? `Model · ${a.model_name}` : 'Model';
  $('#context-usage').textContent = `Token usage / model capacity unavailable · ${thread.usage?.visible_characters || 0} visible conversation characters (not tokens).`;
  const usage = (thread.live_usage || []).at(-1);
  if (usage && Object.keys(usage).length) $('#context-usage').textContent = `Last completed turn: ${usage.input_tokens ?? '?'} input tokens · ${usage.output_tokens ?? '?'} output tokens · ${usage.cache_read_input_tokens ?? usage.cached_input_tokens ?? 0} cache-read tokens. Subscription remaining capacity unavailable.`;
  $('#staged-attachments').innerHTML = (thread.attachments || []).filter(a=>!a.message_id).map(a=>`<div class="attachment-chip">${esc(a.name)} · ${a.characters} chars · local only<button type="button" data-remove-attachment="${a.attachment_id}">Remove</button></div>`).join('');
  const signature = JSON.stringify(thread.messages);
  if (signature !== messageSignature) {
    const box = $('#messages'), follow = box.scrollHeight - box.scrollTop - box.clientHeight < 90 || !messageSignature;
    box.innerHTML = thread.messages.map(m => `<article class="message ${esc(m.role)}"><div class="message-head"><strong>${m.role === 'operator' ? 'YOU' : m.role === 'simulator' ? 'OFFLINE SIMULATOR' : m.role === 'assistant' && !['FAILED','CANCELLED','PAUSED','INTERRUPTED','LIVE_QUEUED'].includes(m.state) ? 'ASSISTANT' : 'WORKSPACE'}</strong><span>${esc(m.kind)} · ${new Date(m.created_at*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</span></div><p>${esc(m.body)}</p><div class="receipt">${esc(m.state)}${m.applied_at ? ' · response completed' : m.delivered_at && ['DELIVERED','LIVE_DELIVERED'].includes(m.state) ? ' · awaiting response' : ''}</div></article>`).join('') || '<div class="welcome"><div class="orb">◈</div><h2>Think it through. Keep the context.</h2><p>Enable your agent in settings, then send a message. Provider readiness is shown above.</p></div>';
    thread.messages.forEach((m,index)=>{
      const linked=(thread.attachments||[]).filter(a=>a.message_id===m.message_id);
      if(linked.length){const note=document.createElement('div');note.className='receipt';note.textContent='Attached locally only: '+linked.map(a=>a.name).join(', ')+' · not sent to a model';box.children[index].append(note);}
    });
    messageSignature = signature; if (follow) box.scrollTop = box.scrollHeight;
  }
  const strip = $('#work-strip'); strip.hidden = !work;
  if (work) strip.innerHTML = `<span><b>${esc(work.state.replaceAll('_',' '))}</b> · checkpoint ${work.checkpoint}/20 · simulation</span>${work.state === 'AWAITING_REPLY' ? '<button id="reply-shortcut">Reply</button>':''}<button data-action="${['PAUSED','INTERRUPTED'].includes(work.state)?'resume':'pause'}">${['PAUSED','INTERRUPTED'].includes(work.state)?'Resume':'Pause'}</button><button data-action="stop" class="danger">Stop</button>`;
  if (work?.live) strip.innerHTML = `<span><b>${esc(work.state)}</b> · live subscription text turn${turnTiming(work) ? ' · ' + esc(turnTiming(work)) : ''}${work.response_chars ? ` · ${work.response_chars} characters received` : ''}</span><button data-action="${['PAUSED','INTERRUPTED'].includes(work.state)?'resume':'pause'}">${['PAUSED','INTERRUPTED'].includes(work.state)?'Resume turn':'Pause'}</button><button data-action="stop" class="danger">Stop</button>`;
  $('#integrations').innerHTML = state.integrations.map(i => `<div class="integration">${esc(i.label)}<span>${esc(i.state)}</span></div>`).join('') + '<p class="muted">No tool grants active. Memory bridge disabled.</p>';
  if (!$('#agent-dialog').open) providerOptions($('#provider-select'));
  renderContext();
}
function renderContext() {
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  if (tab === 'activity') {
    // Newest live turn first: it is the one the operator is watching.
    const live = [...(thread.live_turns || [])].reverse().map(w => {
      const outcome = w.result_preview ? `<div class="work-result"><span>Result</span>${esc(w.result_preview)}</div>`
        : w.error ? `<div class="work-result failed"><span>Failure</span>${esc(w.error)}</div>` : '';
      const received = w.state === 'RUNNING' && w.response_chars ? ` · ${w.response_chars} characters received so far` : '';
      return `<div class="work-card live state-${esc(w.state.toLowerCase())}"><div class="work-card-head"><b>${esc(w.state)}</b><i class="kind live" title="Sent to a real provider.">REAL</i></div>` +
        `<p>${esc(w.instruction.slice(0,180))}</p>` +
        `<small>${esc(w.kind === 'work' ? 'New work' : w.kind)} · ${esc(w.provider)} · ${esc(w.model)}${w.kind === 'steer' ? ' · queued after targeted turn' : ''}<br>${esc(turnTiming(w))}${esc(received)}</small>${outcome}</div>`;
    }).join('');
    const simulated = thread.work.map(w => `<div class="work-card simulated"><div class="work-card-head"><b>${esc(w.state)}</b><i class="kind demo">SIMULATION</i></div><p>${esc(w.instruction.slice(0,180))}</p><small>Simulation · checkpoint ${w.checkpoint}/20 · no model called<br>Model binding: ${esc(w.model)}</small></div>`).join('');
    const proposals = thread.proposals.map(p => `<div class="work-card"><b>DELEGATION · ${esc(p.state)}</b><p>${esc(p.readback)}</p>${p.state === 'AWAITING_APPROVAL' ? `<div class="controls"><button data-proposal="${p.proposal_id}" data-decision="approve">Approve readback</button><button data-proposal="${p.proposal_id}" data-decision="deny">Decline</button></div>`:''}</div>`).join('');
    $('#context-content').innerHTML = (live + simulated + proposals) || '<p class="muted">No work in this conversation. Discussion messages do not create tasks.</p>';
  } else if (tab === 'artifacts') {
    $('#context-content').innerHTML = thread.artifacts.map(a => `<button class="file-button" data-artifact="${a.artifact_id}">▤ ${esc(a.name)}<small> · simulated</small></button>`).join('') || '<p class="muted">No artifacts yet. Completed simulations produce a clearly labeled example.</p>';
  } else {
    const configured = state.projects.find(p => p.project_id === project)?.files_enabled;
    $('#context-content').innerHTML = !configured ? '<p class="muted">No file workspace is authorized for this project.</p>' : files ? `<button class="file-button" data-path="">↥ Workspace root</button>${files.entries.map(e => `<button class="file-button" data-path="${esc(e.path)}">${e.directory?'▸':'▤'} ${esc(e.name)}</button>`).join('')}` : '<button class="file-button" data-path="">Browse authorized workspace</button>';
    $('#context-content').innerHTML += '<div class="work-card"><b>CHANGES</b><p>No file-writing adapter is enabled. No tracked changes are reported.</p></div>';
  }
}
document.addEventListener('click', async event => {
  const b = event.target.closest('button'); if (!b) return;
  try {
    if (b.dataset.project) selectThread(b.dataset.project, selected, chat);
    if (b.dataset.agent) {
      // An agent lives in exactly one project; open it there, on the chat it last had open.
      const a = state.agents.find(x => x.agent_id === b.dataset.agent);
      if (a) selectThread(a.project_id, a.agent_id, agentSelections[a.agent_id]?.chat || null);
    }
    if (b.dataset.agentAction) {
      await api(`/api/agents/${encodeURIComponent(b.dataset.agentId)}/lifecycle`, {action: b.dataset.agentAction});
      await refresh();
      toast(b.dataset.agentAction === 'start' ? 'Agent enabled. It is now eligible for work; no model was called.' : 'Resumed. Held work restarts from its saved context.');
    }
    if (b.dataset.chat !== undefined) selectThread(project, selected, b.dataset.chat || null);
    if (b.dataset.close) $('#'+b.dataset.close).close();
    if (b.dataset.tab) {tab = b.dataset.tab; renderContext();}
    if (b.dataset.action && selected) {await api(`/api/agents/${selected}/lifecycle`, {action:b.dataset.action}); await refresh();}
    if (b.id === 'reply-shortcut') {$('#message-kind').value = 'reply'; $('#message-body').focus();}
    if (b.dataset.proposal) {await api(`/api/projects/${project}/agents/${selected}/approval`, {proposal_id:b.dataset.proposal, decision:b.dataset.decision,chat_id:chat}); await refresh();}
    if (b.dataset.removeAttachment) {await api(`/api/projects/${project}/agents/${selected}/remove-attachment`,{chat_id:chat,attachment_id:b.dataset.removeAttachment});await refresh();}
    if (b.dataset.artifact) {const a = thread.artifacts.find(a => a.artifact_id === b.dataset.artifact); $('#preview-title').textContent = a.name; $('#preview-body').textContent = a.body; $('#preview-dialog').showModal();}
    if (b.dataset.path !== undefined) {
      const p = project, result = await api(`/api/files?agent=${selected}&project=${encodeURIComponent(p)}&path=${encodeURIComponent(b.dataset.path)}`);
      if (p !== project) return;
      if (result.entries) {files = result; renderContext();} else {$('#preview-title').textContent = result.path; $('#preview-body').textContent = result.body; $('#preview-dialog').showModal();}
    }
  } catch (error) {toast(error.message);}
});
$('#composer').onsubmit = async event => {
  event.preventDefault(); if (sending || !selected) return;
  const body = $('#message-body').value, kind = $('#message-kind').value;
  const p = project, a = selected, c = chat, work = activeWork();
  if (['steer','reply'].includes(kind) && !work) {toast('No active work to target. Choose Discussion or New work.'); return;}
  sending = true; $('#send-message').disabled = true;
  try {await api(`/api/projects/${p}/agents/${a}/messages`, {body, kind, chat_id:c, attachments:(thread.attachments||[]).filter(a=>!a.message_id).map(a=>a.attachment_id),request_id:crypto.randomUUID(), ...(['steer','reply'].includes(kind)?{run_id:work.run_id}:{})}); if (p === project && a === selected && c === chat && $('#message-body').value === body) {$('#message-body').value = '';$('#message-kind').value='discuss';$('#message-kind').onchange();saveSelection();} await refresh();}
  catch (error) {toast(error.message);} finally {sending=false; $('#send-message').disabled = !agent();}
};
$('#new-agent').onclick = () => {pendingNewProject=null;providerOptions($('#provider-select')); $('#agent-dialog').showModal();};
$('#new-project').onclick = () => $('#project-dialog').showModal();
$('#new-chat').onclick = () => {if(!selected){toast('Create an agent first.');return;}$('#chat-dialog').showModal();};
$('#chat-form').onsubmit = async event => {event.preventDefault();try{const c=await api(`/api/projects/${project}/agents/${selected}/chats`,Object.fromEntries(new FormData(event.target)));$('#chat-dialog').close();event.target.reset();selectThread(project,selected,c.chat_id);}catch(e){toast(e.message);}};
$('#agent-switch').onchange = event => {const a=state.agents.find(a=>a.agent_id===event.target.value);const saved=agentSelections[a.agent_id];selectThread(a.project_id,a.agent_id,saved?.chat || null);};
$('#agent-form').onsubmit = async event => {event.preventDefault(); try {const a = await api('/api/agents', {...Object.fromEntries(new FormData(event.target)),project_id:pendingNewProject || project}); $('#agent-dialog').close(); event.target.reset(); pendingNewProject=null;selectThread(a.project_id,a.agent_id); toast(a.lifecycle === 'RUNNING' ? 'Agent created and enabled. Choose “New work” and send to start a live turn.' : 'Agent created. Use Enable on its card when ready.');}catch(e){toast(e.message);}};
$('#project-form').onsubmit = async event => {event.preventDefault();try{const p=await api('/api/projects',Object.fromEntries(new FormData(event.target)));$('#project-dialog').close();event.target.reset();toast('Project created. Create its agent next.');pendingNewProject=p.project_id;providerOptions($('#provider-select'));$('#agent-dialog').showModal();}catch(e){toast(e.message);}};
$('#settings-toggle').onclick = () => {const a = agent(); if(!a){toast('Select an agent first.');return;} providerOptions($('#model-provider'));$('#model-provider').value=a.model_provider;$('#model-name').value=a.model_name;$('#settings-identity').textContent=`${a.display_name} · ${a.lifecycle === 'RUNNING'?'enabled':a.lifecycle.toLowerCase()}`;$('#settings').showModal();};
$('#model-form').onsubmit = async event => {event.preventDefault();try{await api(`/api/agents/${selected}/model`,Object.fromEntries(new FormData(event.target)));$('#settings').close();await refresh();toast('Model saved. Changed bindings start a fresh context segment; queued turns retain their original provider.');}catch(e){toast(e.message);}};
$('#composer-model').onclick = () => $('#settings-toggle').click();
$('#dictation-button').onclick = () => $('#dictation-dialog').showModal();
$('#permissions-button').onclick = () => {if(!selected){toast('Select an agent first.');return;}$('#permission-scope').textContent=`${agent().display_name} · ${state.projects.find(p=>p.project_id===project)?.name} · no cross-agent inheritance`;$('#grant-attachments').checked=!!thread.grants?.attachments;$('#grant-file-preview').checked=!!thread.grants?.file_preview;$('#permissions-dialog').showModal();};
for (const [id,capability] of [['grant-attachments','attachments'],['grant-file-preview','file_preview']]) {
  $('#'+id).onchange = async event => {const checkbox=event.target;try{await api(`/api/projects/${project}/agents/${selected}/grants`,{capability,enabled:checkbox.checked});await refresh();toast('Local permission updated.');}catch(e){checkbox.checked=!checkbox.checked;toast(e.message);}};
}
$('#attach-button').onclick = () => {if(!thread.grants?.attachments){toast('Enable local attachment staging in Permissions first.');return;}$('#attachment-picker').click();};
$('#attachment-picker').onchange = async event => {
  const file=event.target.files[0],p=project,a=selected,c=chat;event.target.value='';if(!file)return;
  try{if(file.size>20000)throw new Error('Attachment limit is 20 KB; choose .txt, .md or .csv.');const body=await file.text();await api(`/api/projects/${p}/agents/${a}/attachments`,{chat_id:c,name:file.name,body});await refresh();toast('Staged locally. No cloud upload authorized.');}catch(e){toast(e.message);}
};
$('#message-kind').onchange = () => {
  const live = agent()?.model_provider !== 'offline-demo';
  $('#composer-hint').textContent = {discuss:'Discussion does not redirect existing work.',work:live?'Starts a text-generation turn. No file edits or tools are enabled.':'Creates new work. Other work keeps its progress.',steer:live?'Queued after the active turn; does not change an in-flight response.':'Targets the current work. Applied only after checkpoint acknowledgement.',reply:live?'Queues a follow-up after the active turn.':'Answers the clarification in this thread without creating another task.',delegate:'Creates an exact readback for approval. Delegation execution remains unavailable.'}[$('#message-kind').value];
};
setInterval(refresh, 1000); refresh();
