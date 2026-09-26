'use strict';
// JARVIS Agent Hub. Everything shown here is read from the backend; the browser keeps only
// display caches and reconciles on every poll. Closing the tab changes nothing on the backend.
// Layout: agent sidebar (scoped to the agent chosen in the picker) · main view ·
// agent details panel.

const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const view = $('#view');

// ---------- session ----------
const TOKEN_KEY = 'jarvis-hub-token';
let token = '';
// The operator link carries the token in the URL fragment (never sent to the server in a URL).
// It is captured on first load and also when the link is opened in an already-open tab.
function captureToken() {
  const fragment = new URLSearchParams(location.hash.replace(/^#/, ''));
  const fresh = fragment.get('token');
  if (!fresh) return false;
  sessionStorage.setItem(TOKEN_KEY, fresh); token = fresh;
  history.replaceState(null, '', location.pathname + '#/');
  return true;
}
captureToken();
token = token || sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || '';

async function api(path, body) {
  const options = {method: body === undefined ? 'GET' : 'POST', headers: {'Authorization': `Bearer ${token}`}};
  if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  if (response.status === 401) { needSignIn(); throw new Error('Not signed in to the Hub.'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}
async function apiBlob(path) {
  const response = await fetch(path, {headers: {'Authorization': `Bearer ${token}`}});
  if (!response.ok) throw new Error(`Could not load content (${response.status}).`);
  return response.blob();
}
let signInShown = false;
function needSignIn() {
  if (signInShown) return;
  signInShown = true;
  setConnection('down', 'Not signed in');
  $('#pair-dialog').showModal();
}
$('#pair-form').addEventListener('submit', async event => {
  event.preventDefault();
  const code = new FormData(event.target).get('code');
  try {
    const response = await fetch('/api/pair', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Pairing failed.');
    token = data.token; localStorage.setItem(TOKEN_KEY, token); signInShown = false;
    $('#pair-dialog').close(); toast('This device is paired. You can revoke it from Settings.'); tick(true);
  } catch (error) { toast(error.message, true); }
});

// ---------- small helpers ----------
function store(key, value) { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { /* storage unavailable */ } return null; }
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.classList.toggle('error', error); el.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 4200);
}
let skew = 0;
const now = () => Date.now() / 1000 + skew;
function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.round(now() - ts));
  if (s < 5) return 'just now'; if (s < 60) return `${s}s ago`; if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`; return new Date(ts * 1000).toLocaleDateString();
}
function dur(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
}
// Relative times are filled in by a ticker, outside the markup, so a view is only rebuilt when
// its content really changes — a rebuild every second would swallow clicks.
const agoTag = ts => ts ? `<span class="rel" data-ago="${Number(ts)}"></span>` : '';
const durTag = start => start ? `<span class="rel" data-dur="${Number(start)}"></span>` : '';
function updateRelative() {
  document.querySelectorAll('.rel[data-ago]').forEach(el => { el.textContent = ago(Number(el.dataset.ago)); });
  document.querySelectorAll('.rel[data-dur]').forEach(el => { el.textContent = dur(now() - Number(el.dataset.dur)); });
}
setInterval(updateRelative, 1000);
const clock = ts => ts ? new Date(ts * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : '';
const whenText = ts => ts ? new Date(ts * 1000).toLocaleString([], {weekday: 'short', hour: '2-digit', minute: '2-digit'}) : '';
const stateLabel = s => String(s || '').replace(/_/g, ' ').toLowerCase().replace(/^./, c => c.toUpperCase());
const statusBadge = st => `<span class="status tone-${esc(st.tone)}" title="${esc(st.detail)}"><i></i>${esc(st.label)}</span>`;
const fmtSize = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
function hue(id) { let h = 0; for (const c of String(id)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function avatar(agent, size = '', badge = false) {
  const tone = agent.status?.tone === 'live' ? 'live' : agent.status?.tone === 'attention' ? 'attention' : agent.status?.tone === 'danger' ? 'danger' : 'ok';
  return `<span class="avatar ${size} h${Math.floor(hue(agent.agent_id) / 30)}" aria-hidden="true">${esc((agent.name || '?').slice(0, 1).toUpperCase())}${badge ? `<i class="badge ${tone}"></i>` : ''}</span>`;
}
function dayGroups(items, key) {
  const start = new Date(); start.setHours(0, 0, 0, 0);
  const today = start.getTime() / 1000 - skew;
  const groups = [['Today', []], ['Yesterday', []], ['Previous 7 days', []], ['Older', []]];
  for (const item of items) {
    const ts = Number(item[key] || 0);
    groups[ts >= today ? 0 : ts >= today - 86400 ? 1 : ts >= today - 7 * 86400 ? 2 : 3][1].push(item);
  }
  return groups.filter(([, list]) => list.length);
}
function plain(text, limit = 150) {
  const flat = String(text || '').replace(/```[\s\S]*?```/g, ' ').replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[*_`#>]+/g, '').replace(/\s+/g, ' ').trim();
  return flat.length > limit ? flat.slice(0, limit - 1) + '…' : flat;
}
const ICONS = {
  chat: '<svg viewBox="0 0 24 24"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4A2.5 2.5 0 0 1 4 13.5z"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>',
  artifacts: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="3.25"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><path d="m16.75 13.5 3.25 6.5h-6.5z"/></svg>',
  goals: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".6"/></svg>',
  tasks: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="3.5"/><path d="m8.5 12 2.5 2.5 4.5-5"/></svg>',
  list: '<svg viewBox="0 0 24 24"><path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/></svg>',
  shield: '<svg viewBox="0 0 24 24"><path d="M12 3 5 6v5.5c0 4.3 3 7.9 7 9.5 4-1.6 7-5.2 7-9.5V6z"/><path d="m9 12 2 2 4-4"/></svg>',
  clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
  pulse: '<svg viewBox="0 0 24 24"><path d="M3 12h4l2.5-6 5 12 2.5-6h4"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3.5 7.5A2.5 2.5 0 0 1 6 5h3.5l2 2H18a2.5 2.5 0 0 1 2.5 2.5v7A2.5 2.5 0 0 1 18 19H6a2.5 2.5 0 0 1-2.5-2.5z"/></svg>',
  health: '<svg viewBox="0 0 24 24"><path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.3 4.3 4.3 0 0 1 19.5 10c0 5.4-7.5 10-7.5 10z"/></svg>',
  relationships: '<svg viewBox="0 0 24 24"><circle cx="9" cy="8.5" r="3"/><circle cx="16.5" cy="9.5" r="2.4"/><path d="M3.5 19c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5M14.5 14.3c2.9-.6 5.3.9 6 4.2"/></svg>',
  finance: '<svg viewBox="0 0 24 24"><path d="M12 3v18M16.5 7.5c-.6-1.4-2.2-2.3-4.5-2.3-2.6 0-4.3 1.3-4.3 3.2 0 4.3 9 2.3 9 6.8 0 1.9-1.9 3.3-4.7 3.3-2.4 0-4.2-1-4.8-2.6"/></svg>',
  career: '<svg viewBox="0 0 24 24"><rect x="3.5" y="7.5" width="17" height="12" rx="2.5"/><path d="M9 7.5V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v1.5M3.5 12.5h17"/></svg>',
  interests: '<svg viewBox="0 0 24 24"><path d="M12 3.5a8.5 8.5 0 1 0 0 17c1.4 0 2-1 1.5-2.1-.6-1.4.3-2.9 1.9-2.9h1.7a3.4 3.4 0 0 0 3.4-3.4C20.5 7.3 16.7 3.5 12 3.5z"/><circle cx="8" cy="11" r="1"/><circle cx="11" cy="7.5" r="1"/><circle cx="15.5" cy="8.5" r="1"/></svg>',
  productivity: '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="10.5" rx="1.8"/><path d="M2.5 19h19"/></svg>',
  other: '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="3.5"/><path d="m8.5 12 2.5 2.5 4.5-5"/></svg>',
};
const KIND_ICON = {documents: '▤', web: '◎', code: '</>', images: '▣', videos: '▶', audio: '♪', other: '◇'};
const KIND_LABEL = {all: 'All artifacts', documents: 'Documents', web: 'Web artifacts', code: 'Code', images: 'Images', videos: 'Videos', audio: 'Audio', other: 'Other files', folder: 'Project folder'};

// Safe Markdown: escape everything first, then add a small set of formatting. Links must be
// http(s); images must be https and are shown as thumbnails that link to their source.
function md(source) {
  const blocks = String(source || '').replace(/\r\n/g, '\n').split(/```/);
  return blocks.map((block, i) => {
    if (i % 2 === 1) return `<pre><code>${esc(block.replace(/^[a-z0-9_-]*\n/i, ''))}</code></pre>`;
    const lines = esc(block).split('\n');
    let html = '', list = null;
    const close = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const raw of lines) {
      let m;
      if (/^\s*(?:\[?!\[[^\]]*\]\(https:\/\/[^\s)]+\)(?:\]\(https?:\/\/[^\s)]+\))?\s*)+$/.test(raw)) { close(); html += `<div class="img-row">${inline(raw)}</div>`; }
      else if ((m = raw.match(/^(#{1,3})\s+(.*)$/))) { close(); html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; }
      else if ((m = raw.match(/^\s*[-*]\s+(.*)$/))) { if (list !== 'ul') { close(); html += '<ul>'; list = 'ul'; } html += `<li>${inline(m[1])}</li>`; }
      else if ((m = raw.match(/^\s*\d+[.)]\s+(.*)$/))) { if (list !== 'ol') { close(); html += '<ol>'; list = 'ol'; } html += `<li>${inline(m[1])}</li>`; }
      else if (!raw.trim()) { close(); html += '<br>'; }
      else { close(); html += `<div>${inline(raw)}</div>`; }
    }
    close();
    return html;
  }).join('');
}
const IMG = (src, alt) => `<img class="md-img" src="${src}" alt="${alt}" loading="lazy" referrerpolicy="no-referrer">`;
function inline(escaped) {
  return escaped
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[!\[([^\]]*)\]\((https:\/\/[^\s)]+)\)\]\((https?:\/\/[^\s)]+)\)/g, (_, alt, src, href) => `<a href="${href}" target="_blank" rel="noopener noreferrer nofollow">${IMG(src, alt)}</a>`)
    .replace(/!\[([^\]]*)\]\((https:\/\/[^\s)]+)\)/g, (_, alt, src) => `<a href="${src}" target="_blank" rel="noopener noreferrer nofollow">${IMG(src, alt)}</a>`)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)&]+(?:&amp;[^\s)&]+)*)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer nofollow">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer nofollow">$2</a>');
}
function diffHtml(diff) {
  return `<pre class="diff">${String(diff || '').split('\n').map(line => {
    const cls = line.startsWith('+++') || line.startsWith('---') ? 'hunk' : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : line.startsWith('@@') ? 'hunk' : '';
    return `<span class="${cls}">${esc(line)}</span>`;
  }).join('\n')}</pre>`;
}
const EVENT_GLYPH = {tool: '⚒', lifecycle: '●', artifact: '▤', approval: '⚑', steering: '↳', provider: '⌁', recovery: '↺', failover: '⇄', model: '◈', verify: '✓', settings: '⚙', project: '◇', runtime: '!', progress: '·', goal: '◎', schedule: '⏰', preview: '▶'};
function eventRow(e, context = true) {
  const agent = overview?.agents.find(a => a.agent_id === e.agent_id);
  const meta = context && agent ? `<a href="#/agents/${esc(agent.agent_id)}">${esc(agent.name)}</a>` : '';
  return `<div class="event level-${esc(e.level)} kind-${esc(e.kind)}"><time title="${esc(new Date(e.ts * 1000).toLocaleString())}">${esc(clock(e.ts))}</time><span class="glyph">${EVENT_GLYPH[e.kind] || '·'}</span><div><div class="text">${esc(e.summary)}</div>${meta ? `<div class="meta">${meta}</div>` : ''}</div></div>`;
}

// ---------- state, connection & polling ----------
let overview = null, lastSeq = 0, feed = [], failures = 0, route = null, pageData = null, agentData = null, ticks = 0;
let selectedId = store('jarvis.hub.agent') || '';
const ui = {detailsTab: store('jarvis.hub.detailsTab') || 'tasks', convoFilter: '', artifactFilter: 'all', artifactQuery: '',
  tasksFilter: 'active', search: {query: '', results: null, agent: ''}, gallery: {agent: '', at: 0, data: null}, goalForm: false};
const features = () => overview?.features || {};

function setConnection(state, text) {
  const el = $('#connection'); el.className = `connection ${state}`; el.title = text; el.querySelector('span').textContent = text;
}
async function pollEvents() {
  const data = await api(`/api/events?after=${lastSeq}&limit=500`);
  // Cursor semantics: every event after lastSeq exactly once, even across reconnects.
  for (const e of data.events) if (e.seq > lastSeq) { feed.push(e); lastSeq = e.seq; }
  if (feed.length > 1500) feed = feed.slice(-1500);
  return data.events.length;
}
function chooseAgent() {
  const live = overview.agents.filter(a => !a.archived);
  if (route?.name === 'agent' && overview.agents.some(a => a.agent_id === route.id)) selectedId = route.id;
  if (!overview.agents.some(a => a.agent_id === selectedId)) selectedId = live[0]?.agent_id || overview.agents[0]?.agent_id || '';
  store('jarvis.hub.agent', selectedId);
}
async function tick(force = false) {
  try {
    const [ov] = await Promise.all([api('/api/overview'), pollEvents()]);
    overview = ov; skew = ov.server_time - Date.now() / 1000;
    if (failures) toast('Reconnected — caught up on everything that happened meanwhile.');
    failures = 0; setConnection('live', 'Live');
    chooseAgent();
    ticks++;
    if (route.name !== 'agent' && selectedId && (force || !agentData || agentData.agent_id !== selectedId || ticks % 3 === 0)) {
      agentData = await api(`/api/agents/${encodeURIComponent(selectedId)}`).catch(() => agentData);
    }
    renderChrome();
    await renderRoute(false);
  } catch (error) {
    failures++;
    setConnection('down', failures > 2 ? 'Disconnected — retrying' : 'Reconnecting…');
  }
}
// Poll faster while any agent has work in flight, so tool steps and streamed drafts appear
// promptly; idle pages keep the slower cadence.
function liveWork() {
  return (overview?.agents || []).some(a => a.current_task && ['QUEUED', 'RUNNING'].includes(a.current_task.state));
}
function schedule() {
  const delay = failures ? Math.min(10000, 1000 * 2 ** Math.min(failures, 4)) : liveWork() ? 500 : 1500;
  setTimeout(async () => { await tick(); schedule(); }, delay);
}

// ---------- chrome: sidebar and details ----------
function setHtml(el, html) { if (el && el.dataset.content !== html) { el.innerHTML = html; el.dataset.content = html; updateRelative(); } }
const selected = () => overview?.agents.find(a => a.agent_id === selectedId) || null;
function spaceHref(space, id = selectedId) {
  if (!id) return '#/overview';
  return space === 'chat' ? `#/agents/${id}` : `#/agents/${id}/${space}`;
}
function currentSpace() {
  if (route.name === 'agent') return route.tab === 'work' ? 'chat' : route.tab;
  return route.name;
}
function renderChrome() {
  const f = features(), a = selected(), space = currentSpace();
  $('#provider-chips').innerHTML = Object.entries(overview.providers).map(([name, p]) => {
    const ok = p.installed && p.authenticated;
    return ok ? '' : `<a href="#/settings" class="chip bad" title="${esc(p.detail)}">${esc(name === 'claude-cli' ? 'Claude' : 'Codex')} not signed in</a>`;
  }).join('');
  // Picker
  setHtml($('#agent-picker'), a
    ? `${avatar(a, '', true)}<span class="picker-text"><b>${esc(a.name)}</b><small>${esc(a.status.label)} · ${esc(a.model)}</small></span><span class="chevron">▾</span>`
    : `<span class="picker-text"><b>No agent yet</b><small>Create one to start</small></span><span class="chevron">▾</span>`);
  renderSidebar(a, space);
  renderMainTitle(a);
  renderDetails();
}
function renderMainTitle(a) {
  const names = {overview: 'All agents', settings: 'Settings', projects: 'Project folders', activity: 'Activity'};
  let title = names[route.name] || '';
  if (route.name === 'agent' && a) {
    const labels = {work: '', goals: 'Goals', tasks: 'Tasks', artifacts: 'Artifacts', search: 'Search', config: 'Settings'};
    const chat = route.chat && (pageData?.chats || []).find(c => c.chat_id === route.chat);
    title = `${esc(a.name)}<small>${esc(route.tab === 'work' ? (chat ? chat.title : 'New chat') : labels[route.tab] || '')}</small>`;
  }
  setHtml($('#main-title'), title);
}
function renderSidebar(a, space) {
  const f = features(), data = agentData && agentData.agent_id === selectedId ? agentData : null;
  if (!a) {
    setHtml($('#space-links'), '<a href="#/overview">All agents</a>');
    setHtml($('#project-tree'), '<p class="note">No agent selected.</p>');
    setHtml($('#conversation-tree'), '<p class="note">Create an agent to start a conversation.</p>');
    return;
  }
  const openTasks = (data?.tasks || []).filter(t => !['COMPLETED', 'FAILED', 'CANCELLED'].includes(t.state)).length;
  const activeGoals = (data?.goals || []).filter(g => g.state === 'ACTIVE').length;
  const links = [['chat', 'Chat', ''], ...(f.goals ? [['goals', 'Goals', activeGoals || '']] : []), ['tasks', 'Tasks', openTasks || ''],
    ...(f.artifacts ? [['artifacts', 'Artifacts', '']] : []), ...(f.search ? [['search', 'Search', '']] : [])];
  setHtml($('#space-links'), links.map(([key, label, count]) => `<a href="${spaceHref(key)}" class="${space === key && !(key === 'chat' && route.chat) ? 'active' : ''}">${ICONS[key]}<span>${label}</span>${count !== '' ? `<span class="count">${count}</span>` : ''}</a>`).join(''));
  // The selected agent's project folder and what it recently made there.
  const seen = new Set(), recent = [];
  for (const x of data?.artifacts || []) { if (x.change !== 'deleted' && !seen.has(x.path)) { seen.add(x.path); recent.push(x); } if (recent.length >= 5) break; }
  setHtml($('#project-tree'), `<details class="project-folder" open><summary>▱ ${esc(a.project_name)}</summary>${recent.map(x => `<a href="${f.artifacts ? spaceHref('artifacts') : '#'}" data-artifact="${esc(x.artifact_id)}" title="${esc(x.path)}"><span>${esc(x.path.split('/').pop())}</span></a>`).join('') || '<p class="note">Nothing made yet.</p>'}${f.artifacts && recent.length ? `<a href="${spaceHref('artifacts')}">All files →</a>` : ''}</details>`);
  // Conversations, newest activity first, grouped by day; archived ones fold away.
  const q = ui.convoFilter.trim().toLowerCase();
  const chats = (data?.chats || []).filter(c => !q || String(c.title).toLowerCase().includes(q))
    .slice().sort((x, y) => (y.last_at || y.created_at) - (x.last_at || x.created_at));
  const live = chats.filter(c => !c.archived), archived = chats.filter(c => c.archived);
  const row = c => `<div class="convo"><a class="${route.chat === c.chat_id ? 'active' : ''}" href="#/agents/${esc(a.agent_id)}/work/${esc(c.chat_id)}" title="${esc(c.title)}">${c.active ? '<i class="live-dot" title="Working"></i>' : ''}<span>${esc(c.title)}</span></a>${f.archive ? `<button class="more" type="button" aria-label="Conversation actions" data-chat-menu="${esc(c.chat_id)}">⋯</button>` : ''}</div>`;
  const html = `<a class="new-conversation" href="#/agents/${esc(a.agent_id)}">＋ New chat</a>` +
    (live.length ? dayGroups(live, 'last_at').map(([label, list]) => `<div class="convo-group">${label}</div>${list.map(row).join('')}`).join('') : `<p class="note">${q ? 'No matching conversations.' : 'No chats yet.'}</p>`) +
    (archived.length ? `<details class="archived-group"${q ? ' open' : ''}><summary>Archived · ${archived.length}</summary>${archived.map(row).join('')}</details>` : '');
  const tree = $('#conversation-tree');
  const open = tree.querySelector('.archived-group')?.open;
  setHtml(tree, html);
  if (open && tree.querySelector('.archived-group')) tree.querySelector('.archived-group').open = true;
}
function taskIcon(t) {
  if (t.state === 'COMPLETED') return '<span class="state-icon done">✓</span>';
  if (t.state === 'RUNNING' || t.state === 'QUEUED') return '<span class="state-icon live">●</span>';
  if (t.state === 'FAILED') return '<span class="state-icon failed">!</span>';
  if (t.state === 'CANCELLED') return '<span class="state-icon">×</span>';
  return '<span class="state-icon attention">❚❚</span>';
}
function taskSubtitle(t) {
  if (t.state === 'RUNNING') return t.progress || 'Working…';
  if (t.state === 'QUEUED') return 'Queued';
  if (t.blocker && t.state !== 'COMPLETED') return t.blocker;
  return plain(t.result, 140) || stateLabel(t.state);
}
function taskItem(t, a) {
  const f = features();
  const href = t.chat_id ? `#/agents/${esc(a.agent_id)}/work/${esc(t.chat_id)}` : `#/agents/${esc(a.agent_id)}/tasks`;
  return `<div class="item">${taskIcon(t)}<a class="main-col plain-link" href="${href}"><div class="title">${esc(t.request?.startsWith('⏰') ? t.title : (t.title || t.request))}</div><div class="subtitle">${esc(taskSubtitle(t))}</div><div class="time">${agoTag(t.updated_at || t.created_at)}${t.archived ? ' · archived' : ''}</div></a>${f.archive ? `<button class="more" type="button" aria-label="Task actions" data-task-menu="${esc(t.task_id)}">⋯</button>` : ''}</div>`;
}
function cadence(s) {
  if (s.kind === 'interval') return s.every_minutes % 60 === 0 ? `Every ${s.every_minutes / 60} h` : `Every ${s.every_minutes} min`;
  if (s.kind === 'daily') return `Daily at ${s.daily_at}`;
  return `Once · ${whenText(s.next_run_at)}`;
}
function renderDetails() {
  const panel = $('#details'), a = selected();
  const data = agentData && agentData.agent_id === selectedId ? agentData : null;
  if (!a) { setHtml(panel, '<p class="note">No agent selected.</p>'); return; }
  const f = features();
  const tabs = [['tasks', 'Tasks', ICONS.list], ['access', 'Access', ICONS.shield], ...(f.schedules ? [['schedules', 'Scheduled jobs', ICONS.clock]] : []), ['activity', 'Activity', ICONS.pulse]];
  if (!tabs.some(([k]) => k === ui.detailsTab)) ui.detailsTab = 'tasks';
  let body = '';
  if (ui.detailsTab === 'tasks') {
    const tasks = (data?.tasks || []).filter(t => !t.archived).slice(0, 40);
    body = tasks.length ? dayGroups(tasks, 'created_at').map(([label, list]) => `<h4>${label}</h4><div class="list">${list.map(t => taskItem(t, a)).join('')}</div>`).join('')
      : '<p class="note">Nothing yet. Everything you ask for shows up here.</p>';
    if ((data?.tasks || []).some(t => t.archived)) body += `<p class="note"><a href="${spaceHref('tasks')}">See archived tasks →</a></p>`;
  } else if (ui.detailsTab === 'access') {
    const labels = data?.permission_labels || {};
    body = `<h4>Model</h4><p class="note inset">${esc(a.provider)} · <b>${esc(a.model)}</b>${a.effort ? ` · effort ${esc(effortLabel(a.effort))}` : ''}</p><h4>What ${esc(a.name)} may do</h4>${Object.entries(labels).map(([k, label]) => `<div class="perm"><span class="${a.permissions?.[k] ? 'yes' : 'no'}">${a.permissions?.[k] ? '✓' : '—'}</span>${esc(label)}</div>`).join('') || '<p class="note">Loading…</p>'}<p class="note">Sensitive actions still ask you first.</p><p><a href="${spaceHref('config')}">Change access or model →</a></p>`;
  } else if (ui.detailsTab === 'schedules') {
    const jobs = data?.schedules || [];
    body = jobs.length ? `<div class="list">${jobs.map(s => `<div class="item"><span class="state-icon ${s.enabled ? 'live' : ''}">⏰</span><div class="main-col"><div class="title">${esc(s.name)}</div><div class="subtitle">${esc(cadence(s))}${s.notify_when ? ' · reports only when something changes' : ''}</div><div class="time">${s.enabled ? (s.next_run_at ? `Next ${esc(whenText(s.next_run_at))}` : '') : 'Paused'}</div></div><button class="more" type="button" aria-label="Job actions" data-schedule-menu="${esc(s.schedule_id)}">⋯</button></div>`).join('')}</div>`
      : `<p class="note">No scheduled jobs. Ask ${esc(a.name)} to check in on something, or to watch for something and tell you.</p>`;
  } else {
    body = `<div class="feed">${(data?.events || []).slice().reverse().slice(0, 30).map(e => eventRow(e, false)).join('') || '<p class="note">No activity yet.</p>'}</div>`;
  }
  const st = a.status;
  const stateText = st.code === 'idle' ? 'Ready' : st.label;
  setHtml(panel, `<div class="profile">${avatar(a, 'lg')}<h2>${esc(a.name)}</h2><div class="state-line ${esc(st.tone)}"><i></i>${esc(stateText)}</div>${a.role ? `<p class="note">${esc(a.role)}</p>` : ''}</div>
    <div class="icon-tabs" role="tablist">${tabs.map(([k, label, icon]) => `<button type="button" role="tab" title="${label}" aria-label="${label}" data-details-tab="${k}" class="${ui.detailsTab === k ? 'active' : ''}">${icon}</button>`).join('')}</div>${body}`);
}

// ---------- routing ----------
function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  if (parts[0] === 'agents' && parts[1]) return {name: 'agent', id: parts[1], tab: parts[2] || 'work', chat: parts[2] === 'work' ? parts[3] : undefined};
  if (parts[0] === 'tasks' && parts[1]) return {name: 'task', id: parts[1]};
  if (['activity', 'projects', 'settings', 'overview'].includes(parts[0])) return {name: parts[0]};
  return {name: 'home'};
}
window.addEventListener('hashchange', () => {
  closeMenus();
  document.body.classList.remove('sidebar-open'); $('#sidebar-toggle').setAttribute('aria-expanded', 'false');
  if (captureToken()) { signInShown = false; if ($('#pair-dialog').open) $('#pair-dialog').close(); route = parseRoute(); tick(true); return; }
  route = parseRoute(); pageData = null;
  if (overview) { chooseAgent(); renderChrome(); }
  renderRoute(true); view.focus({preventScroll: true}); view.scrollTop = 0;
});

// A view re-renders only when its content changed and the operator is not typing in it.
let lastHtml = '';
let paintedRoute = '';
const drafts = new Map();
const sendingRoutes = new Set();
function paint(html) {
  if (html === lastHtml) return;
  const active = document.activeElement;
  const sameRoute = paintedRoute === location.hash;
  const editing = sameRoute && active && view.contains(active) && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName);
  // Configuration forms stay stable while editing; chat progress must keep updating.
  if (editing && !active.closest('#chat-form, #steer-form')) return;
  const field = editing ? active.name : null;
  const selection = editing && active.tagName === 'TEXTAREA' ? [active.selectionStart, active.selectionEnd] : null;
  const scroll = view.scrollTop;
  // Like a chat app: when the reader is already at the bottom, keep new steps and drafts in view.
  const following = sameRoute && view.scrollHeight - view.scrollTop - view.clientHeight < 120;
  const expanded = sameRoute ? [...view.querySelectorAll('details')].map(d => d.open) : [];
  const pinned = !!view.querySelector('.usage-wrap.pinned');
  view.innerHTML = html; lastHtml = html; updateRelative();
  if (pinned) view.querySelector('.usage-wrap')?.classList.add('pinned');
  view.querySelectorAll('details').forEach((d, i) => { if (expanded[i] !== undefined) d.open = expanded[i]; });
  paintedRoute = location.hash;
  const saved = drafts.get(location.hash);
  if (saved) for (const el of view.querySelectorAll('#chat-form [name], #steer-form [name]')) if (saved[el.name] !== undefined) el.value = saved[el.name];
  const box = $('#chat-request'); if (box) grow(box);
  if (field) {
    const replacement = [...view.querySelectorAll('[name]')].find(el => el.name === field);
    replacement?.focus({preventScroll: true});
    if (selection) replacement?.setSelectionRange(...selection);
  }
  if (view.querySelector('#chat-form') && (following || !sameRoute)) view.scrollTop = view.scrollHeight;
  else if (sameRoute) view.scrollTop = scroll;
  loadThumbs();
}
function grow(box) { box.style.height = 'auto'; box.style.height = Math.min(box.scrollHeight, 220) + 'px'; }
async function renderRoute(reset) {
  if (!overview) return;
  if (reset) lastHtml = '';
  try {
    if (route.name === 'home') {
      if (selectedId) { location.replace(`#/agents/${selectedId}`); return; }
      paint(renderOverview());
    }
    else if (route.name === 'overview') paint(renderOverview());
    else if (route.name === 'agent') {
      const source = location.hash, current = {...route};
      const data = await api(`/api/agents/${encodeURIComponent(current.id)}`);
      if (current.chat) data.conversation = await api(`/api/agents/${encodeURIComponent(current.id)}/chat?chat=${encodeURIComponent(current.chat)}`);
      else if (current.tab === 'work' && features().usage) data.usage = await api(`/api/agents/${encodeURIComponent(current.id)}/usage`).catch(() => null);
      if (current.tab === 'artifacts' && features().artifacts) data.gallery = await gallery(current.id, reset);
      if (location.hash !== source) return;
      pageData = data; agentData = data;
      renderSidebar(selected(), currentSpace()); renderDetails(); renderMainTitle(selected());
      paint(renderAgent(data));
      syncAppPanel((data.conversation?.agent_turns || []).flatMap(t => t.previews || []));
    }
    else if (route.name === 'task') {
      const source = location.hash;
      const previous = await api(`/api/tasks/${encodeURIComponent(route.id)}`);
      if (location.hash !== source) return;
      // Old bookmarks open ordinary chat; the underlying task record is untouched.
      location.hash = previous.agent ? `#/agents/${previous.agent.agent_id}` : '#/';
    }
    else if (route.name === 'projects') { pageData = await api('/api/projects'); paint(renderProjects(pageData)); }
    else if (route.name === 'activity') paint(renderActivity());
    else if (route.name === 'settings') { pageData = {audit: await api('/api/audit'), sessions: await api('/api/sessions').catch(() => [])}; paint(renderSettings(pageData)); }
  } catch (error) {
    paint(`<div class="page"><div class="empty">${esc(error.message)}</div></div>`);
  }
}
async function gallery(agentId, force) {
  const g = ui.gallery;
  // The folder listing walks the disk, so it refreshes every 10 s rather than every poll.
  if (force || g.agent !== agentId || Date.now() - g.at > 10000) { g.data = await api(`/api/agents/${encodeURIComponent(agentId)}/artifacts`); g.agent = agentId; g.at = Date.now(); }
  return g.data;
}

// ---------- overview ----------
function renderOverview() {
  const agents = overview.agents.filter(a => !a.archived);
  return `<div class="page"><div class="page-head"><h1>Your agents</h1><span class="spacer"></span><button class="primary" data-new-agent>＋ New agent</button><p class="sub">Pick one to chat, set goals and see what it made.</p></div>
    <div class="agent-cards">${agents.map(a => `<a class="agent-card" href="#/agents/${esc(a.agent_id)}" data-pick="${esc(a.agent_id)}">${avatar(a, '', true)}<div><b>${esc(a.name)}</b><small>${esc(a.role || 'AI agent')} · ${esc(a.project_name)}</small><small>${statusBadge(a.status)}</small></div></a>`).join('') || '<p>No agents yet.</p>'}</div>
    ${overview.agents.some(a => a.archived) ? `<h2>Archived agents</h2><div class="agent-cards">${overview.agents.filter(a => a.archived).map(a => `<a class="agent-card" href="#/agents/${esc(a.agent_id)}/config">${avatar(a)}<div><b>${esc(a.name)}</b><small>Archived</small></div></a>`).join('')}</div>` : ''}</div>`;
}

// ---------- agent ----------
function renderAgent(a) {
  const tab = route.tab;
  if (tab === 'config') return `<div class="page"><div class="page-head"><h1>${esc(a.name)} settings</h1><span class="spacer"></span><a href="#/agents/${esc(a.agent_id)}">Back to chat</a></div>${agentConfig(a)}${agentControls(a)}</div>`;
  if (tab === 'goals' && features().goals) return goalsPage(a);
  if (tab === 'tasks') return tasksPage(a);
  if (tab === 'artifacts' && features().artifacts) return artifactsPage(a);
  if (tab === 'search' && features().search) return searchPage(a);
  return `<div class="chat-workspace">${agentWork(a)}</div>`;
}
function agentControls(a) {
  const controls = [
    a.archived ? `<button data-archive="false">Restore agent</button>` : '',
    !a.archived && a.lifecycle !== 'RUNNING' ? `<button class="primary" data-life="start">Enable chat</button>` : '',
    !a.archived && a.lifecycle === 'RUNNING' ? `<button data-life="pause">Pause agent</button>` : '',
    !a.archived && a.lifecycle !== 'STOPPED' && a.lifecycle !== 'CREATED' ? `<button data-life="stop" class="danger">Disable</button>` : '',
    !a.archived ? `<button data-archive="true">Archive agent</button>` : '',
  ].join('');
  return `<div class="panel"><h3>Agent</h3><div class="row">${statusBadge(a.status)}<span class="spacer"></span>${controls}</div></div>`;
}
const STARTERS = [['Help me set a goal', 'Help me set a new goal.'], ['Research something', 'Research '], ['Build me an app', 'Build me a web app that '], ['Check in with me daily', 'Check in with me every day at 9:00 about ']];
function agentWork(a) {
  const blocked = a.archived ? 'This agent is archived.' : a.lifecycle !== 'RUNNING' ? 'Enable this agent in its settings to chat.' : '';
  const messages = a.conversation?.messages || [];
  const turns = a.conversation?.agent_turns || [];
  const active = turns.filter(t => !['COMPLETED', 'FAILED', 'CANCELLED'].includes(t.state));
  const tools = Object.keys(a.permissions || {}).filter(k => a.permissions[k]).length;
  const ids = new Set(turns.map(t => t.task_id));
  const events = (a.events || []).filter(e => ids.has(e.task_id) && e.kind === 'tool');
  const who = `<div class="who">${avatar(a, 'sm')}<span>${esc(a.name)}</span></div>`;
  return `
    ${messages.length ? `<div class="chat-thread" aria-live="polite">${messages.map(m => m.role === 'operator'
      ? `<article class="chat-message user-message"><h3>You</h3><div class="result">${md(m.body)}</div></article>`
      : `<article class="chat-message assistant-message">${who}<h3>${esc(a.name)}</h3><div class="result${m.provisional ? ' draft' : ''}">${m.state === 'LIVE_QUEUED' ? '<span class="muted">Thinking…</span>' : md(m.body)}</div>${m.provisional ? '<p class="note draft-note">Draft — still working. The verified answer replaces this when the run finishes.</p>' : ''}${/FAILED|INTERRUPTED/.test(m.state) ? '<p class="note">Response interrupted or unavailable. You can send another message.</p>' : ''}</article>`).join('')}</div>`
      : `<div class="chat-welcome">${avatar(a, 'lg')}<h2>What’s on your mind?</h2><p>Chat with ${esc(a.name)}.</p>${blocked ? '' : `<div class="starters">${STARTERS.map(([label, text]) => `<button type="button" data-starter="${esc(text)}">${esc(label)}</button>`).join('')}</div>`}</div>`}
    ${active.map(t => `${liveSteps(t)}<div class="turn-status"><span>${esc(t.state === 'RUNNING' ? (t.progress || 'Working…') : t.state === 'QUEUED' ? 'Message received — queued' : (t.blocker || stateLabel(t.state)))}</span><span class="spacer"></span>${t.actions?.includes('cancel') ? `<button data-chat-cancel="${esc(t.task_id)}">Stop</button>` : ''}${t.actions?.includes('resume') ? `<button data-chat-resume="${esc(t.task_id)}">Continue</button>` : ''}</div>${t.approval ? approvalBox(t.task_id, t.approval, 'This action') : ''}`).join('')}
    ${appCards(turns.flatMap(t => t.previews || []))}
    ${events.length ? `<details class="conversation-details"><summary>Tool activity · ${events.length}</summary>${events.map(e => eventRow(e, false)).join('')}</details>` : ''}
    ${turns.some(t => t.artifacts?.length) ? `<details class="conversation-details"><summary>Files</summary>${artifactList(turns.flatMap(t => t.artifacts || []))}</details>` : ''}
    <div class="chat-composer"><div class="composer-box">
      <button type="button" class="round plus" data-composer-menu aria-label="Quick actions">＋</button>
      <form id="chat-form">
        <label class="sr-only" for="chat-request">Message ${esc(a.name)}</label><textarea id="chat-request" name="body" rows="1" maxlength="20000" required placeholder="Message ${esc(a.name)}"></textarea>
        <button class="round primary send-button" aria-label="Send" ${blocked ? 'disabled' : ''}>↑</button>
      </form></div>
      ${composerFoot(a, blocked, tools)}
    </div>`;
}
// ---------- model, effort and context footer ----------
function prettyModel(model) {
  const m = String(model || '');
  let r = m.match(/^claude-(opus|sonnet|haiku)-(\d+)(?:-(\d+))?/i);
  if (r) return `${r[1][0].toUpperCase()}${r[1].slice(1)} ${r[2]}${r[3] ? '.' + r[3] : ''}`;
  r = m.match(/^gpt-([\d.]+)(?:-(\w+))?/i);
  if (r) return `GPT-${r[1]}${r[2] ? ' ' + r[2][0].toUpperCase() + r[2].slice(1) : ''}`;
  return m;
}
const fmtTok = n => n == null ? '—' : n >= 1e6 ? `${+(n / 1e6).toFixed(n % 1e6 ? 1 : 0)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(Math.round(n));
const effortLabel = e => (overview.effort_labels || {})[e] || e;
const EFFORT_HINT = {auto: 'JARVIS picks per task: quick for chat, deeper for research and code', none: 'No extra reasoning', minimal: 'Barely any reasoning', low: 'Fastest replies', medium: 'Balanced', high: 'Thinks longer', xhigh: 'Thinks much longer', max: 'Most thorough, slowest', ultracode: "Extra high plus Claude's workflow mode (runs as extra high inside JARVIS, which does its own tool work)", ultra: "Codex's deepest reasoning; slowest"};
function resetText(ts) {
  if (!ts) return '';
  const s = Number(ts) - now();
  if (s <= 0) return 'Resets now';
  if (s < 3600) return `Resets in ${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `Resets in ${Math.floor(s / 3600)} h ${Math.round(s % 3600 / 60)} min`;
  return `Resets ${new Date(ts * 1000).toLocaleString([], {weekday: 'short', hour: 'numeric', minute: '2-digit'})}`;
}
const LIMIT_NAMES = {five_hour: '5-hour limit', seven_day: 'Weekly · all models', seven_day_opus: 'Weekly · Opus', seven_day_sonnet: 'Weekly · Sonnet', seven_day_oauth_apps: 'Weekly · apps'};
const limitName = key => LIMIT_NAMES[key] || key.replace(/^seven_day_/, 'Weekly · ').replace(/_/g, ' ');
function meter(parts) {
  return `<div class="meter">${parts.map(([pct, cls, title]) => `<i class="${cls} w${Math.max(0, Math.min(100, Math.round(pct)))}" title="${esc(title || '')}"></i>`).join('')}</div>`;
}
function usagePanel(a, u) {
  // A reply is measured against the window of the model that wrote it; the next reply's
  // model (and its window) can differ after a switch.
  const last = u.last_turn, ctx = last?.context_tokens, win = last?.context_window || u.window?.tokens;
  const switched = last && last.model && last.model !== u.model;
  const pct = ctx != null && win ? 100 * ctx / win : null;
  const history = u.history || {};
  const histTok = history.chars != null ? history.chars / 4 : null;
  const compactAt = history.compact_at_chars != null ? history.compact_at_chars / 4 : null;
  const histShown = histTok != null && ctx != null ? Math.min(histTok, ctx) : null;
  const limits = u.limits?.windows || {};
  const limitRows = Object.entries(limits).map(([key, w]) => {
    const p = Math.round(100 * (w.utilization || 0));
    return `<div class="limit"><div class="limit-head"><b>${esc(limitName(key))}</b><span>${esc(resetText(w.resets_at))}</span><span>${p}%</span></div>${meter([[p, p >= 85 ? 'bad' : p >= 60 ? 'warn' : 'use']])}</div>`;
  }).join('');
  return `<div class="usage-pop" role="dialog" aria-label="Context and usage">
    <div class="limit-head"><b>Context window</b><span></span><span>${ctx != null ? `${fmtTok(ctx)} / ${fmtTok(win)} (${Math.round(pct)}%)` : `— / ${fmtTok(win)}`}</span></div>
    ${ctx != null ? meter([[100 * (histShown || 0) / win, 'hist', 'Conversation history (estimate)'], [100 * (ctx - (histShown || 0)) / win, 'other', 'Instructions, memory, tool results and the CLI\'s own prompt']]) : meter([])}
    <div class="legend">${ctx != null ? `<span><i class="hist"></i>History ~${fmtTok(histShown)}</span><span><i class="other"></i>Instructions, memory, tools ~${fmtTok(ctx - (histShown || 0))}</span>` : `<span>No reply measured in this chat yet.</span>`}</div>
    <div class="limit-head"><span>${compactAt != null && histTok != null ? (compactAt > histTok ? `${fmtTok(compactAt - histTok)} until auto-compact` : 'Auto-compacts before the next reply') : `Auto-compacts at ${fmtTok(compactAt)} of history`}</span><span></span>${route.chat ? '<button type="button" class="link-btn" data-compact>Compact session</button>' : ''}</div>
    <p class="fine">${switched ? `Last reply: ${esc(prettyModel(last.model))} (${fmtTok(win)} window). Next reply: ` : ''}${esc(prettyModel(u.model))} window ${fmtTok(u.window?.tokens)} · ${esc(u.window?.source || '')}. History may use half the window; older turns are condensed at 80% of that.</p>
    <hr>
    <div class="limit-head"><b>Plan usage limits${u.plan ? ` · ${esc(u.plan[0].toUpperCase() + u.plan.slice(1))}` : ''}</b></div>
    ${limitRows || `<p class="fine">${u.provider === 'codex-cli' ? 'Codex does not report plan limits to JARVIS.' : 'Shown after the next streamed reply.'}</p>`}
    ${u.limits?.observed_at ? `<p class="fine">As of ${esc(clock(u.limits.observed_at))}, from the latest streamed reply.</p>` : ''}
    ${last ? `<p class="fine">Last turn: ${last.calls} model call(s), peak ${fmtTok(last.peak_context_tokens)} context, ${fmtTok(last.output_tokens)} written.</p>` : ''}
  </div>`;
}
function composerFoot(a, blocked, tools) {
  const u = a.conversation?.usage || a.usage;
  const f = features();
  const win = u?.last_turn?.context_window || u?.window?.tokens, ctx = u?.last_turn?.context_tokens;
  const pct = ctx != null && win ? Math.min(100, 100 * ctx / win) : 0;
  const tone = pct >= 85 ? 'bad' : pct >= 60 ? 'warn' : 'use';
  const ring = `<svg class="ring ${tone}" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" class="track"/><circle cx="12" cy="12" r="9" class="fill" pathLength="100" stroke-dasharray="${pct.toFixed(1)} 100"/></svg>`;
  return `<div class="composer-foot">
    <button type="button" class="foot-chip" data-model-menu title="Change model">${esc(prettyModel(a.model))} ▾</button>
    ${f.effort ? `<button type="button" class="foot-chip" data-effort-menu title="Change effort">${esc(effortLabel(a.effort || 'auto'))} ▾</button>` : ''}
    <span class="foot-note">${esc(blocked || `${tools} abilities on · sensitive actions ask first`)}</span>
    <span class="spacer"></span>
    ${f.usage && u ? `<div class="usage-wrap"><button type="button" class="ring-btn" data-usage-pin aria-label="Context ${ctx != null ? Math.round(pct) + '%' : 'not measured yet'}">${ring}</button>${usagePanel(a, u)}</div>` : ''}
  </div>`;
}
function modelMenu(a) {
  const groups = Object.entries(overview.known_models || {});
  return groups.map(([provider, models]) => `<div class="menu-label">${provider === 'claude-cli' ? 'Claude' : 'Codex'}</div>${models.map(m => `<button type="button" data-set-model="${esc(provider)}|${esc(m)}">${esc(prettyModel(m))}<span class="muted">${esc(checkLabel((overview.providers[provider]?.models || []).find(x => x.model === m)))}</span>${m === a.model && provider === a.provider ? '<span class="check-mark">✓</span>' : ''}</button>`).join('')}`).join('');
}
function effortMenu(a) {
  const levels = ['auto', ...(a.effort_levels || [])];
  return `<div class="menu-label">Effort · ${esc(prettyModel(a.model))}</div>${levels.map(e => `<button type="button" class="effort-item" data-set-effort="${esc(e)}"><span><b>${esc(effortLabel(e))}</b><small>${esc(EFFORT_HINT[e] || '')}</small></span>${e === (a.effort || 'auto') ? '<span class="check-mark">✓</span>' : ''}</button>`).join('')}`;
}
// Live steps are the agent's recorded events for this turn (tool calls and phases), in
// plain words; nothing here is simulated.
function stepText(s) {
  const text = String(s.summary || '');
  if (/^synthesizing/i.test(text)) return 'Writing the answer…';
  if (/^task contract/i.test(text)) return 'Understanding the request…';
  if (/^current news · web_fetch/i.test(text)) return 'Reading news desks…';
  if (/^personal agent/i.test(text)) return 'Working on it…';
  if (s.kind === 'model') return `Thinking · ${text.split('·')[1]?.trim() || 'model'}`;
  return text;
}
function liveSteps(t) {
  const steps = t.steps || [];
  if (!steps.length) return '';
  return `<ol class="live-steps" aria-live="polite">${steps.map((s, i) => `<li class="${i === steps.length - 1 ? 'current' : 'done'}${s.kind === 'tool' ? ' tool' : ''}">${esc(stepText(s))}</li>`).join('')}</ol>`;
}
function approvalBox(taskId, approval, title) {
  return `<div class="blocker"><b>${esc(title || 'Task')}</b> asks to <b>${esc(approval?.action || 'run an action')}</b>
    ${approval?.resource ? `<pre>${esc(approval.resource)}</pre>` : ''}${approval?.reason ? `<div class="note">${esc(approval.reason)}</div>` : ''}
    <div class="row gap-top"><button class="primary" data-approve="${esc(taskId)}">Approve this exact action</button><button class="danger" data-deny="${esc(taskId)}">Deny</button></div></div>`;
}
function artifactList(items) {
  if (!items.length) return '<div class="empty">No files yet.</div>';
  return items.map(x => `<div class="artifact"><span class="change ${esc(x.change)}">${esc(x.change)}</span><div class="main-col"><div class="path" title="${esc(x.path)}">${esc(x.path)}</div><small class="muted">v${x.version} · ${esc(fmtSize(x.size))} · ${agoTag(x.created_at)}</small></div>${x.change !== 'deleted' ? `<button data-artifact="${esc(x.artifact_id)}">Open</button>` : ''}</div>`).join('');
}

// ---------- goals ----------
function goalsPage(a) {
  const goals = a.goals || [];
  const active = goals.filter(g => g.state === 'ACTIVE'), done = goals.filter(g => g.state === 'DONE');
  const categories = overview.goal_categories || {};
  const row = g => `<div class="item ${g.state === 'DONE' ? 'goal-done' : ''}"><button type="button" class="goal-check ${g.state === 'DONE' ? 'done' : ''}" data-goal-toggle="${esc(g.goal_id)}" aria-label="${g.state === 'DONE' ? 'Mark not done' : 'Mark done'}">${g.state === 'DONE' ? '✓' : ''}</button><div class="main-col"><div class="title">${esc(g.title)}</div><div class="subtitle">${esc(g.progress || g.detail || (g.state === 'DONE' ? 'Achieved.' : 'Just set; no progress logged yet.'))}</div><div class="time">${esc(g.category_label)} · ${g.state === 'DONE' ? `done ${agoTag(g.done_at)}` : `set ${agoTag(g.created_at)}`}${g.created_by === 'agent' ? ` with ${esc(a.name)}` : ''}</div></div><button class="more" type="button" aria-label="Goal actions" data-goal-menu="${esc(g.goal_id)}">⋯</button></div>`;
  return `<div class="page"><div class="page-head"><h1>Goals</h1><span class="spacer"></span><button type="button" data-goal-form>${ui.goalForm ? 'Cancel' : '＋ Add a goal'}</button>
      <p class="sub">${esc(a.name)} keeps these moving, logs progress as you work together and checks in on them.</p></div>
    ${ui.goalForm ? `<form id="goal-form" class="goal-form"><input name="title" maxlength="200" required placeholder="What do you want to achieve?" autocomplete="off"><select name="category">${Object.entries(categories).map(([k, v]) => `<option value="${esc(k)}" ${k === 'other' ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select><button class="primary">Add goal</button></form>` : ''}
    ${active.length ? `<div class="list">${active.map(row).join('')}</div>` : `<div class="empty">No goals yet. Pick an area below and ${esc(a.name)} will help you shape one.</div>`}
    ${done.length ? `<details class="conversation-details"><summary>Completed · ${done.length}</summary><div class="list">${done.map(row).join('')}</div></details>` : ''}
    <h2>Create a goal</h2>
    <div class="list category-list">${Object.entries(categories).map(([k, v]) => `<button type="button" class="item" data-goal-category="${esc(k)}">${ICONS[k] || ICONS.other}<span>${esc(v)}</span><span class="chev">›</span></button>`).join('')}</div></div>`;
}

// ---------- tasks ----------
const TASK_FILTERS = [['active', 'Active'], ['done', 'Done'], ['failed', 'Failed'], ['archived', 'Archived'], ['all', 'All']];
function tasksPage(a) {
  const open = t => !['COMPLETED', 'FAILED', 'CANCELLED'].includes(t.state);
  const keep = {active: t => !t.archived, done: t => !t.archived && t.state === 'COMPLETED', failed: t => !t.archived && ['FAILED', 'CANCELLED'].includes(t.state), archived: t => t.archived, all: () => true}[ui.tasksFilter] || (() => true);
  const tasks = (a.tasks || []).filter(keep);
  const working = tasks.filter(open);
  return `<div class="page"><div class="page-head"><h1>Tasks</h1><p class="sub">Everything ${esc(a.name)} has done for you. Archive to tidy up; delete to remove it and its record.</p></div>
    <div class="pills">${TASK_FILTERS.map(([k, l]) => `<button type="button" data-task-filter="${k}" class="${ui.tasksFilter === k ? 'active' : ''}">${l}</button>`).join('')}</div>
    ${working.length && ui.tasksFilter === 'active' ? `<h4>In progress</h4><div class="list">${working.map(t => taskItem(t, a)).join('')}</div>` : ''}
    ${dayGroups(tasks.filter(t => ui.tasksFilter !== 'active' || !open(t)), 'created_at').map(([label, list]) => `<h4>${label}</h4><div class="list">${list.map(t => taskItem(t, a)).join('')}</div>`).join('') || (working.length ? '' : '<div class="empty">Nothing here.</div>')}
    <p class="note">Showing the latest 100 tasks.</p></div>`;
}

// ---------- artifacts ----------
const thumbs = new Map();
function contentUrl(a, item) {
  if (item.on_disk) return `/api/agents/${encodeURIComponent(a.agent_id)}/files/content?path=${encodeURIComponent(item.path)}`;
  if (item.artifact_id && item.stored) return `/api/artifacts/${encodeURIComponent(item.artifact_id)}/content`;
  return null;
}
function galleryItems(a) {
  const g = a.gallery || {made: [], folder: []};
  if (ui.artifactFilter === 'folder') return g.folder;
  return g.made.filter(x => ui.artifactFilter === 'all' || x.kind === ui.artifactFilter);
}
function artifactCard(a, item, index) {
  const url = contentUrl(a, item), key = url ? `${url}#${item.size}:${item.created_at}` : '';
  const cached = thumbs.get(key);
  const thumb = cached || `<span>${esc(KIND_ICON[item.kind] || "◇")}</span>`;
  return `<button type="button" class="card" data-open-item="${index}"><div class="thumb" ${url && !cached ? `data-thumb="${esc(key)}" data-url="${esc(url)}" data-kind="${esc(item.kind)}" data-size="${Number(item.size)}"` : ''}>${thumb}</div>
    <div class="card-foot"><span class="kind-icon">${esc(KIND_ICON[item.kind] || "◇")}</span><div class="main-col"><b title="${esc(item.path)}">${esc(item.name)}</b><small>${esc(KIND_LABEL[item.kind] || 'File')} · ${agoTag(item.created_at)}${item.versions > 1 ? ` · v${item.version}` : ''}</small></div></div></button>`;
}
function artifactsPage(a) {
  const g = a.gallery || {made: [], folder: []};
  const q = ui.artifactQuery.trim().toLowerCase();
  const items = galleryItems(a).filter(x => !q || x.path.toLowerCase().includes(q));
  const count = kind => kind === 'all' ? g.made.length : kind === 'folder' ? g.folder.length : g.made.filter(x => x.kind === kind).length;
  const nav = (kind) => `<button type="button" data-artifact-filter="${kind}" class="${ui.artifactFilter === kind ? 'active' : ''}"><span>${KIND_LABEL[kind]}</span><span class="count">${count(kind) || ''}</span></button>`;
  const all = galleryItems(a);
  const index = item => all.indexOf(item);
  const recent = ui.artifactFilter === 'all' && !q ? items.slice(0, 4) : [];
  return `<div class="artifact-layout"><nav class="artifact-nav" aria-label="Artifact types"><div class="side-heading">Artifacts</div>${['all', 'documents', 'web', 'code'].map(nav).join('')}<div class="side-heading">Media</div>${['images', 'videos', 'audio'].map(nav).join('')}<div class="side-heading">Files</div>${['other', 'folder'].map(nav).join('')}</nav>
    <section><div class="page-head"><h1>${esc(KIND_LABEL[ui.artifactFilter])}</h1><span class="spacer"></span><input id="artifact-q" name="artifact-q" type="search" placeholder="Filter by name" value="${esc(ui.artifactQuery)}">
      <p class="sub">${ui.artifactFilter === 'folder' ? `Other files in ${esc(a.project_name)}, including ones programs wrote after a task ended.` : `Everything ${esc(a.name)} created or changed, newest version of each file.`}</p></div>
    ${recent.length ? `<h2>Recent</h2><div class="cards">${recent.map(x => artifactCard(a, x, index(x))).join('')}</div><h2>All</h2>` : ''}
    ${items.length ? `<div class="cards">${items.map(x => artifactCard(a, x, index(x))).join('')}</div>` : `<div class="empty">${q ? 'No files match.' : `Nothing here yet. Ask ${esc(a.name)} to make a document, an image or an app.`}</div>`}</section></div>`;
}
let thumbQueue = 0;
function loadThumbs() {
  for (const el of view.querySelectorAll('[data-thumb]')) {
    if (thumbQueue >= 6) return;
    const {thumb: key, url, kind} = el.dataset, size = Number(el.dataset.size || 0);
    const textual = ['documents', 'code', 'web'].includes(kind) && size <= 400000 && !/\.(pdf|docx?|pptx?|xlsx?|odt|epub|rtf)(\?|#|&|$)/i.test(decodeURIComponent(url));
    const image = kind === 'images' && size <= 8000000 && !/\.svg(&|$)/i.test(decodeURIComponent(url));
    el.removeAttribute('data-thumb');
    if (!textual && !image) continue;
    thumbQueue++;
    apiBlob(url).then(async blob => {
      const html = image ? `<img alt="" src="${URL.createObjectURL(blob)}">`
        : `<pre class="${kind === 'documents' ? '' : 'code'}">${esc((await blob.text()).slice(0, 1400))}</pre>`;
      thumbs.set(key, html);
      el.innerHTML = html;
    }).catch(() => { /* thumbnail is optional */ }).finally(() => { thumbQueue--; loadThumbs(); });
  }
}
let viewerUrls = [];
async function openItem(a, item) {
  const url = contentUrl(a, item);
  const body = $('#preview-body');
  viewerUrls.forEach(u => URL.revokeObjectURL(u)); viewerUrls = [];
  $('#preview-title').textContent = item.name;
  const task = item.task_id ? (a.tasks || []).find(t => t.task_id === item.task_id) : null;
  $('#preview-meta').innerHTML = `${esc(KIND_LABEL[item.kind] || 'File')} · ${esc(fmtSize(item.size || 0))} · <span class="mono">${esc(item.path)}</span>${item.versions > 1 ? ` · ${item.versions} versions` : ''}${task?.chat_id ? ` · <a href="#/agents/${esc(a.agent_id)}/work/${esc(task.chat_id)}" data-close>open its conversation</a>` : ''}`;
  $('#preview-tabs').innerHTML = `${item.artifact_id ? `<button data-history="${esc(item.artifact_id)}">Versions and changes</button>` : ''}<span class="spacer"></span>${url ? '<button data-download-item>Download</button>' : ''}`;
  $('#preview-tabs').onclick = async e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.history) { $('#preview-dialog').close(); openArtifact(b.dataset.history); }
    if (b.dataset.downloadItem !== undefined) { const blob = await apiBlob(url + (url.includes('?') ? '&' : '?') + 'download=1'); const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = item.name; link.click(); }
  };
  body.innerHTML = '<p class="muted">Loading…</p>';
  $('#preview-dialog').showModal();
  if (!url) { body.innerHTML = '<p class="muted">This version is too large to be stored in the Hub and the file is no longer in the project folder.</p>'; return; }
  try {
    if (['images', 'videos', 'audio'].includes(item.kind) && !/\.svg$/i.test(item.path)) {
      const src = URL.createObjectURL(await apiBlob(url)); viewerUrls.push(src);
      body.innerHTML = `<div class="viewer">${item.kind === 'images' ? `<img alt="${esc(item.name)}" src="${src}">` : item.kind === 'videos' ? `<video controls src="${src}"></video>` : `<audio controls src="${src}"></audio>`}</div>`;
      return;
    }
    if (/\.(pdf|docx?|pptx?|xlsx?|odt|epub|rtf|zip)$/i.test(item.path)) { body.innerHTML = '<p class="muted">No inline preview for this file type. Use Download.</p>'; return; }
    const text = await (await apiBlob(url)).text();
    body.innerHTML = (/\.md$/i.test(item.path) ? `<div class="result">${md(text)}</div>` : `<pre>${esc(text)}</pre>`)
      + (item.kind === 'web' ? '<p class="note">Shown as source: the Hub never runs pages an agent wrote. Ask the agent to open it as an app to use it.</p>' : '');
  } catch (error) { body.innerHTML = `<p class="muted">${esc(error.message)}</p>`; }
}
$('#preview-dialog').addEventListener('close', () => { viewerUrls.forEach(u => URL.revokeObjectURL(u)); viewerUrls = []; });

// ---------- search ----------
function searchPage(a) {
  const s = ui.search.agent === a.agent_id ? ui.search : {query: '', results: null};
  const mark = text => { const safe = esc(text); const q = esc(s.query.trim()); if (!q) return safe; const at = safe.toLowerCase().indexOf(q.toLowerCase()); return at < 0 ? safe : `${safe.slice(0, at)}<mark>${safe.slice(at, at + q.length)}</mark>${safe.slice(at + q.length)}`; };
  const r = s.results;
  return `<div class="page"><div class="page-head"><h1>Search</h1></div>
    <form id="search-form" class="search-box"><input name="q" type="search" value="${esc(s.query)}" placeholder="Search ${esc(a.name)}'s conversations" autocomplete="off"><button class="primary">Search</button></form>
    ${!r ? `<p class="note">Finds words in your messages and ${esc(a.name)}'s answers.</p>` : `
      ${r.chats.length ? `<h4>Conversations</h4><div class="list">${r.chats.map(c => `<a class="item" href="#/agents/${esc(a.agent_id)}/work/${esc(c.chat_id)}"><span class="state-icon">${ICONS.chat}</span><div class="main-col"><div class="title">${mark(c.title)}</div><div class="time">${agoTag(c.created_at)}</div></div></a>`).join('')}</div>` : ''}
      <h4>Messages</h4>${r.messages.length ? `<div class="list">${r.messages.map(m => `<a class="item" href="${m.chat_id ? `#/agents/${esc(a.agent_id)}/work/${esc(m.chat_id)}` : `#/agents/${esc(a.agent_id)}/tasks`}"><span class="state-icon">${m.where === 'you' ? 'You' : avatar(a, 'sm')}</span><div class="main-col"><div class="title">${esc(m.title)}</div><div class="subtitle">${mark(m.snippet)}</div><div class="time">${agoTag(m.created_at)}</div></div></a>`).join('')}</div>` : '<div class="empty">No messages match.</div>'}`}</div>`;
}

// ---------- app preview panel ----------
// A verified app runs on its own loopback address (a different origin from this page). It is
// framed in a sandbox outside #view, so repainting the chat never reloads a running game and
// generated HTML never executes inside the Hub page.
const appPanel = {id: null, url: null, previews: new Map()};
const dismissedApps = new Set(JSON.parse(sessionStorage.getItem('jarvis.hub.dismissedApps') || '[]'));
const shownApps = new Set(JSON.parse(sessionStorage.getItem('jarvis.hub.shownApps') || '[]'));
function rememberApps() {
  try {
    sessionStorage.setItem('jarvis.hub.dismissedApps', JSON.stringify([...dismissedApps].slice(-100)));
    sessionStorage.setItem('jarvis.hub.shownApps', JSON.stringify([...shownApps].slice(-100)));
  } catch { /* storage unavailable: panels simply reopen */ }
}
function isAppUrl(value) {
  try {
    const url = new URL(value);
    const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
    return url.protocol === 'http:' && loopback && url.port && !(url.hostname === location.hostname && url.port === location.port);
  } catch { return false; }
}
function appCards(previews) {
  if (!previews.length) return '';
  return `<div class="app-cards">${previews.slice().reverse().map(p => `<div class="app-card"><span class="kind-icon">▶</span><div class="main-col"><b>${esc(p.title)}</b> ${p.state === 'RUNNING' ? '<span class="tag ok">Running</span>' : '<span class="tag">Stopped</span>'}<div class="note"><code>${esc(p.url)}</code>${p.detail ? ` · ${esc(p.detail)}` : ''}</div></div>
    ${p.state === 'RUNNING' ? `<button data-app-show="${esc(p.preview_id)}">Show</button>${isAppUrl(p.url) ? `<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">New tab ↗</a>` : ''}<button data-app-stop="${esc(p.preview_id)}">Stop</button>` : `<button class="primary" data-app-start="${esc(p.preview_id)}">Start again</button>`}</div>`).join('')}</div>`;
}
function syncAppPanel(previews) {
  for (const p of previews) appPanel.previews.set(p.preview_id, p);
  if (appPanel.id && appPanel.previews.has(appPanel.id)) { showApp(appPanel.previews.get(appPanel.id)); return; }
  // Open a newly verified app once, as the operator asked; a closed panel stays closed.
  const fresh = previews.filter(p => p.state === 'RUNNING' && !shownApps.has(p.preview_id) && !dismissedApps.has(p.preview_id)).pop();
  if (fresh) { shownApps.add(fresh.preview_id); rememberApps(); showApp(fresh); }
}
function showApp(p) {
  const panel = $('#app-panel'), box = $('#app-panel-frame');
  panel.hidden = false; document.body.classList.add('app-open');
  $('#app-panel-title').textContent = p.title;
  const state = $('#app-panel-state');
  state.textContent = p.state === 'RUNNING' ? 'Running' : 'Stopped';
  state.className = `tag ${p.state === 'RUNNING' ? 'ok' : ''}`;
  const tab = $('#app-panel-newtab');
  if (isAppUrl(p.url)) tab.href = p.url; else tab.removeAttribute('href');
  tab.hidden = p.state !== 'RUNNING';
  $('#app-panel-reload').hidden = p.state !== 'RUNNING';
  const power = $('#app-panel-power');
  power.textContent = p.state === 'RUNNING' ? 'Stop' : 'Start again';
  power.dataset.id = p.preview_id; power.dataset.state = p.state;
  const remote = !['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
  $('#app-panel-note').textContent = remote
    ? `This app runs on the Hub computer at ${p.url} and can be opened only there.`
    : p.state === 'RUNNING'
      ? `Running on this computer at ${p.url}, a separate address from the Hub, so it cannot read your Hub session. Click inside the app to use the keyboard.`
      : `Stopped. Start again serves the same files at ${p.url}.`;
  const running = p.state === 'RUNNING' && isAppUrl(p.url);
  if (appPanel.id === p.preview_id && appPanel.url === (running ? p.url : null) && box.firstChild) return;
  appPanel.id = p.preview_id; appPanel.url = running ? p.url : null;
  if (!running) {
    box.innerHTML = `<div class="app-stopped"><p>${esc(p.detail || 'This app is stopped.')}</p><button class="primary" data-app-start="${esc(p.preview_id)}">Start again</button></div>`;
    return;
  }
  const frame = document.createElement('iframe');
  frame.title = `App preview: ${p.title}`;
  // Scripts run in the app's own origin; no top navigation, popups or downloads into the Hub.
  frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-pointer-lock allow-modals');
  frame.setAttribute('allow', 'fullscreen; gamepad; autoplay');
  frame.referrerPolicy = 'no-referrer';
  frame.src = p.url;
  frame.addEventListener('load', () => { try { frame.contentWindow.focus(); } catch { /* cross-origin focus may be refused */ } });
  box.replaceChildren(frame);
}
function closeApp() {
  if (appPanel.id) { dismissedApps.add(appPanel.id); rememberApps(); }
  appPanel.id = appPanel.url = null;
  $('#app-panel-frame').replaceChildren();
  $('#app-panel').hidden = true; document.body.classList.remove('app-open');
}
async function appCommand(id, verb) {
  const result = await act(api(`/api/previews/${encodeURIComponent(id)}/${verb}`, {}), verb === 'stop' ? 'App stopped.' : 'App started again.');
  if (result) { appPanel.previews.set(id, result); if (appPanel.id === id) { appPanel.url = undefined; showApp(result); } else if (verb === 'start') { dismissedApps.delete(id); rememberApps(); showApp(result); } }
}

// ---------- configuration, projects, activity, settings ----------
function agentConfig(a) {
  const providers = overview.providers;
  const options = p => (a.known_models[p] || []).map(m => `<option value="${esc(m)}" ${m === a.model && p === a.provider ? 'selected' : ''}>${esc(m)}${checkLabel((providers[p]?.models || []).find(x => x.model === m))}</option>`).join('');
  return `<form class="panel" id="config-form">
    <h3>Model and abilities</h3>
    <div class="form-grid">
      <label>Provider<select name="provider" id="cfg-provider">${['claude-cli', 'codex-cli'].map(p => `<option value="${p}" ${p === a.provider ? 'selected' : ''}>${p === 'claude-cli' ? 'Claude CLI' : 'Codex CLI'}</option>`).join('')}</select></label>
      <label>Model<select name="model" id="cfg-model">${options(a.provider)}</select></label>
      <p class="note wide">Model changes apply to new messages; no provider is silently substituted.</p>
    </div>
    <fieldset class="permissions gap-top"><legend>What this agent may do</legend>
      ${Object.entries(a.permission_labels || {}).map(([k, label]) => `<label class="check"><input type="checkbox" name="perm-${esc(k)}" ${a.permissions?.[k] ? 'checked' : ''}> ${esc(label)}</label>`).join('')}
      <p class="note">Sensitive actions such as launching apps, controlling the desktop, opening sites or changing your connected accounts always ask you first.</p></fieldset>
    <div class="dialog-actions"><button class="primary">Save</button></div>
  </form>`;
}
function checkLabel(m) { return !m ? '' : m.verified === true ? ' ✓ verified' : m.verified === false ? ' ✗ unavailable' : ' · unverified'; }
function verificationText(m) {
  if (m.verified === true) return `Verified${m.resolved?.length ? ` — the CLI ran ${m.resolved.join(', ')}` : ''}.`;
  if (m.verified === false) return `Unavailable: ${m.detail}`;
  return 'Not verified from this backend yet.';
}
function renderProjects(projects) {
  return `<div class="page"><div class="page-head"><h1>Project folders</h1><span class="spacer"></span><button data-new-project>＋ New folder</button></div>` +
    projects.map(p => `<section class="panel"><h3>▱ ${esc(p.name)}</h3><div class="list">${p.agents.filter(a => !a.archived).map(a => `<a class="item" href="#/agents/${esc(a.agent_id)}">${esc(a.name)}<span class="spacer"></span><span class="note">Open chat →</span></a>`).join('') || '<p class="note">No agents in this folder yet.</p>'}</div></section>`).join('') + '</div>';
}
const activityFilter = {agent: '', detail: false};
const LOW_LEVEL = new Set(['progress', 'model', 'verify']);
function renderActivity() {
  const items = feed.filter(e => (!activityFilter.agent || e.agent_id === activityFilter.agent) && (activityFilter.detail || !LOW_LEVEL.has(e.kind))).slice(-400).reverse();
  return `<div class="page"><div class="page-head"><h1>Activity</h1><p class="sub">Observable actions, tool results and deliverables — not model thoughts.</p></div>
    <div class="row gap-bottom"><select id="a-agent" class="auto-width" aria-label="Agent"><option value="">All agents</option>${overview.agents.map(a => `<option value="${esc(a.agent_id)}" ${activityFilter.agent === a.agent_id ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}</select>
      <label class="check"><input type="checkbox" id="a-detail" ${activityFilter.detail ? 'checked' : ''}> Show low-level steps</label></div>
    <div class="panel"><div class="feed">${items.map(e => eventRow(e, true)).join('') || '<div class="empty">No activity yet.</div>'}</div></div></div>`;
}
function renderSettings(data) {
  const d = overview.defaults;
  const providerPanels = Object.entries(overview.providers).map(([name, p]) => `
    <div class="panel"><div class="row"><h3 class="flush">${name === 'claude-cli' ? 'Claude CLI' : 'Codex CLI'}</h3><span class="chip ${p.installed && p.authenticated ? 'ok' : 'bad'}">${p.installed ? (p.authenticated ? 'signed in' : 'not signed in') : 'not installed'}</span><span class="spacer"></span><span class="muted mono">${esc(p.version || '')}</span></div>
      <p class="note">${esc(p.detail)} ${p.checked_at ? `Checked ${agoTag(p.checked_at)}.` : ''}</p>
      <div class="list gap-top">${p.models.map(m => `<div class="item"><div class="main-col"><div class="mono">${esc(m.model)}</div><div class="subtitle">${esc(verificationText(m))}</div></div>
        <button data-verify="${esc(name)}|${esc(m.model)}">Verify</button>
        ${d.provider === name && d.model === m.model ? '<span class="chip">default</span>' : `<button data-default="${esc(name)}|${esc(m.model)}">Make default</button>`}</div>`).join('')}</div></div>`).join('');
  return `<div class="page"><div class="page-head"><h1>Settings</h1><span class="spacer"></span><button id="refresh-providers">Re-check providers</button><p class="sub">Providers, the default model for new agents, remote access and the audit log. <a href="#/activity">Activity</a> · <a href="#/projects">Project folders</a></p></div>
    <div class="panel"><h3>Default for new agents</h3><div class="row"><span class="mono">${esc(d.provider)} · <b>${esc(d.model)}</b></span><span class="muted">${esc(verificationText(d.check || {}))}</span></div>
      <p class="note">Verification sends one tiny request through the same path agents use and records the model the CLI reports it actually ran. An unverified or unavailable model is never silently replaced.</p></div>
    <div class="panel"><h3>Agent abilities</h3><p class="note">Give every agent every ability: web, files, programs, memory, apps and websites on this computer, your connected accounts, background jobs and new skills. Sensitive actions still ask you first, and you can switch any ability off per agent in its settings.</p>
      <div class="row gap-top"><button class="primary" id="grant-full-access">Give every agent full access</button></div></div>
    ${providerPanels}
    <div class="panel"><h3>Remote access</h3><p class="note">The Hub listens on this computer only. For another device, put Tailscale Serve (HTTPS on your private network) in front of it, start the Hub with <code>--remote-access paired --trusted-host &lt;your-tailnet-hostname&gt;</code>, then pair each device with a one-time code. Sessions are revocable below.</p>
      ${data.sessions.length ? `<div class="list">${data.sessions.map(s => `<div class="item"><div class="main-col"><div>${esc(s.label || 'device')}</div><div class="subtitle">created ${esc(s.created_at)} · ${s.revoked_at ? 'revoked' : `expires ${esc(s.expires_at)}`}</div></div>${s.revoked_at ? '' : `<button class="danger" data-revoke="${esc(s.session_id)}">Revoke</button>`}</div>`).join('')}</div>` : '<p class="muted">No paired devices.</p>'}</div>
    <div class="panel"><h3>Audit log</h3>${data.audit.length ? `<div class="list">${data.audit.slice(0, 40).map(r => `<div class="item"><div class="main-col"><div>${esc(r.action)} ${r.target ? `<span class="muted mono">${esc(r.target)}</span>` : ''}</div><div class="subtitle">${esc(r.actor)} · ${agoTag(r.ts)}${r.detail ? ` · ${esc(r.detail)}` : ''}</div></div><span class="chip ${r.outcome === 'ok' ? 'ok' : 'bad'}">${esc(r.outcome)}</span></div>`).join('')}</div>` : '<p class="muted">No commands yet.</p>'}</div></div>`;
}

// ---------- menus ----------
let menuAnchor = null;
function closeMenus() {
  $('#item-menu').hidden = true; $('#agent-menu').hidden = true;
  $('#agent-picker').setAttribute('aria-expanded', 'false');
  if (menuAnchor) { menuAnchor.setAttribute('aria-expanded', 'false'); menuAnchor = null; }
}
function openMenu(anchor, html) {
  closeMenus();
  const menu = $('#item-menu');
  menu.innerHTML = html; menu.hidden = false;
  menuAnchor = anchor; anchor.setAttribute('aria-expanded', 'true');
  const box = anchor.getBoundingClientRect(), width = menu.offsetWidth, height = menu.offsetHeight;
  menu.style.left = `${Math.max(8, Math.min(innerWidth - width - 8, box.right - width))}px`;
  menu.style.top = `${box.bottom + height + 8 > innerHeight ? Math.max(8, box.top - height - 4) : box.bottom + 4}px`;
  menu.querySelector('button, a')?.focus();
}
function pickerMenu() {
  const agents = overview.agents.filter(a => !a.archived);
  const byProject = new Map();
  for (const a of agents) { if (!byProject.has(a.project_name)) byProject.set(a.project_name, []); byProject.get(a.project_name).push(a); }
  return [...byProject].map(([project, list]) => `<div class="menu-label">${esc(project)}</div>${list.map(a => `<button type="button" role="menuitem" data-pick-agent="${esc(a.agent_id)}">${avatar(a, 'sm', true)}<span>${esc(a.name)}</span>${a.agent_id === selectedId ? '<span class="check-mark">✓</span>' : ''}</button>`).join('')}`).join('')
    + `<hr><button type="button" role="menuitem" data-new-agent>＋ New agent</button><a role="menuitem" href="#/overview">All agents</a>${selectedId ? `<a role="menuitem" href="#/agents/${esc(selectedId)}/config">Agent settings</a>` : ''}`;
}
function pickAgent(id) {
  const space = currentSpace();
  selectedId = id; store('jarvis.hub.agent', id); agentData = null;
  closeMenus();
  location.hash = ['goals', 'tasks', 'artifacts', 'search', 'config'].includes(space) ? `#/agents/${id}/${space}` : `#/agents/${id}`;
}
async function startChat(agentId, body, title) {
  const chat = await api(`/api/agents/${agentId}/chats`, {title: (title || body).slice(0, 80)});
  const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
  await api(`/api/agents/${agentId}/messages`, {chat_id: chat.chat_id, body, request_id: requestId});
  location.hash = `#/agents/${agentId}/work/${chat.chat_id}`;
}

// ---------- interactions ----------
async function act(promise, message) {
  try { const result = await promise; if (message) toast(message); await tick(true); return result; }
  catch (error) { toast(error.message, true); return null; }
}
function detailsToggle() {
  const wide = innerWidth > 1280;
  if (wide) { const closed = document.body.classList.toggle('details-closed'); store('jarvis.hub.detailsClosed', closed ? '1' : '0'); }
  else document.body.classList.toggle('details-open');
}
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') { closeMenus(); document.body.classList.remove('details-open', 'sidebar-open'); }
  if (event.target.id === 'chat-request' && event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    const form = event.target.form;
    if (!form.querySelector('button.primary')?.disabled) form.requestSubmit();
  }
});
document.addEventListener('click', async event => {
  if (!event.target.closest('.menu, .more, #agent-picker, [data-composer-menu]')) closeMenus();
  const pick = event.target.closest('[data-pick]');
  if (pick && pick.dataset.pick !== selectedId) { selectedId = pick.dataset.pick; store('jarvis.hub.agent', selectedId); agentData = null; }
  const el = event.target.closest('button');
  if (!el) return;
  const d = el.dataset, a = selected(), data = agentData;
  if (el.id === 'agent-picker') { const menu = $('#agent-menu'); const open = menu.hidden; closeMenus(); if (open && overview) { menu.innerHTML = pickerMenu(); menu.hidden = false; el.setAttribute('aria-expanded', 'true'); } return; }
  if (d.pickAgent) { pickAgent(d.pickAgent); return; }
  if (el.id === 'sidebar-toggle') { const open = document.body.classList.toggle('sidebar-open'); el.setAttribute('aria-expanded', String(open)); return; }
  if (el.id === 'details-toggle') { detailsToggle(); return; }
  if (d.detailsTab) { ui.detailsTab = d.detailsTab; store('jarvis.hub.detailsTab', d.detailsTab); renderDetails(); return; }
  if (d.close !== undefined) { el.closest('dialog').close(); return; }
  if (el.id === 'new-agent' || d.newAgent !== undefined) { closeMenus(); openNewAgent(); return; }
  if (d.appShow) { const p = appPanel.previews.get(d.appShow); if (p) { dismissedApps.delete(p.preview_id); rememberApps(); appPanel.id = null; showApp(p); } return; }
  if (d.appStop) { await appCommand(d.appStop, 'stop'); return; }
  if (d.appStart) { await appCommand(d.appStart, 'start'); return; }
  if (el.id === 'app-panel-close') { closeApp(); return; }
  if (el.id === 'app-panel-reload') { const frame = $('#app-panel-frame iframe'); if (frame) frame.src = appPanel.url; return; }
  if (el.id === 'app-panel-power') { await appCommand(el.dataset.id, el.dataset.state === 'RUNNING' ? 'stop' : 'start'); return; }
  // Composer
  if (d.starter !== undefined || d.prefill !== undefined) { closeMenus(); const box = $('#chat-request'); if (box) { box.value = d.starter ?? d.prefill; grow(box); box.focus(); box.setSelectionRange(box.value.length, box.value.length); drafts.set(location.hash, {body: box.value}); } return; }
  if (d.composerMenu !== undefined) { openMenu(el, `${features().goals ? `<a role="menuitem" href="${spaceHref('goals')}">◎ Set a goal</a>` : ''}<button type="button" data-prefill="Check in with me every day at 9:00 about ">⏰ Schedule a check-in</button><button type="button" data-prefill="Build me a web app that ">▶ Build an app</button><button type="button" data-prefill="Research ">🔎 Research something</button><button type="button" data-prefill="Make me a document about ">▤ Make a document</button>`); return; }
  if (d.modelMenu !== undefined && pageData) { openMenu(el, modelMenu(pageData)); return; }
  if (d.effortMenu !== undefined && pageData) { openMenu(el, effortMenu(pageData)); return; }
  if (d.setModel && pageData) {
    closeMenus();
    const [provider, model] = d.setModel.split('|');
    await act(api(`/api/agents/${pageData.agent_id}/config`, {provider, model, permissions: pageData.permissions}), `${prettyModel(model)} for new messages.`);
    return;
  }
  if (d.setEffort && pageData) { closeMenus(); await act(api(`/api/agents/${pageData.agent_id}/effort`, {effort: d.setEffort}), `Effort: ${effortLabel(d.setEffort)} for new messages.`); return; }
  if (d.usagePin !== undefined) { el.closest('.usage-wrap')?.classList.toggle('pinned'); return; }
  if (d.compact !== undefined && pageData && route.chat) {
    if (!confirm(`Compact this conversation now?\n\nThe last ${4} turns stay word for word; older ones are condensed into a short summary the agent keeps. The original older turns are sealed with the agent's memory key and can be read back only with that key file.`)) return;
    const result = await act(api(`/api/agents/${pageData.agent_id}/chats/${route.chat}/compact`, {}));
    if (result) toast(result.compacted ? `Compacted ${result.messages} older messages into a summary.` : result.reason);
    return;
  }
  // Conversation, task, goal and job menus
  if (d.chatMenu && a) {
    const chat = (data?.chats || []).find(c => c.chat_id === d.chatMenu); if (!chat) return;
    openMenu(el, `<button type="button" data-chat-archive="${esc(chat.chat_id)}" data-value="${chat.archived ? 'false' : 'true'}">${chat.archived ? 'Restore' : 'Archive'}</button><hr><button type="button" class="danger" data-chat-delete="${esc(chat.chat_id)}">Delete…</button>`);
    return;
  }
  if (d.chatArchive && a) { closeMenus(); await act(api(`/api/agents/${a.agent_id}/chats/${d.chatArchive}/archive`, {archived: d.value === 'true'}), d.value === 'true' ? 'Conversation archived.' : 'Conversation restored.'); return; }
  if (d.chatDelete && a) {
    closeMenus();
    const chat = (data?.chats || []).find(c => c.chat_id === d.chatDelete) || {title: 'this conversation', turns: 0};
    const jobs = (data?.schedules || []).filter(s => s.chat_id === d.chatDelete).length;
    if (!confirm(`Delete “${chat.title}”?\n\nThis removes its ${chat.turns} message(s), their records and ${a.name}'s memory of this conversation${jobs ? `, and stops and removes ${jobs} scheduled job(s) that post here` : ''}. Files it made stay in the project folder, and things ${a.name} saved to long-term memory stay.\n\nThis can't be undone.`)) return;
    const result = await act(api(`/api/agents/${a.agent_id}/chats/${d.chatDelete}/delete`, {}), 'Conversation deleted.');
    if (result && route.chat === d.chatDelete) location.hash = `#/agents/${a.agent_id}`;
    return;
  }
  if (d.taskMenu && a) {
    const task = (data?.tasks || []).find(t => t.task_id === d.taskMenu); if (!task) return;
    const done = ['COMPLETED', 'FAILED', 'CANCELLED'].includes(task.state);
    openMenu(el, `${task.chat_id ? `<a href="#/agents/${esc(a.agent_id)}/work/${esc(task.chat_id)}">Open conversation</a>` : ''}<button type="button" data-task-archive="${esc(task.task_id)}" data-value="${task.archived ? 'false' : 'true'}">${task.archived ? 'Unarchive' : 'Archive'}</button><hr>${done ? `<button type="button" class="danger" data-task-delete="${esc(task.task_id)}">Delete…</button>` : '<button type="button" disabled>Delete (stop it first)</button>'}`);
    return;
  }
  if (d.taskArchive) { closeMenus(); await act(api(`/api/tasks/${d.taskArchive}/archive`, {archived: d.value === 'true'}), d.value === 'true' ? 'Task archived.' : 'Task restored.'); return; }
  if (d.taskDelete) {
    closeMenus();
    const task = (data?.tasks || []).find(t => t.task_id === d.taskDelete);
    if (!confirm(`Delete “${task?.title || 'this task'}”?\n\nIts message and answer${task?.chat_id ? ' leave the conversation' : ''} and its record is removed. Files it made stay in the project folder. This can't be undone.`)) return;
    await act(api(`/api/tasks/${d.taskDelete}/delete`, {}), 'Task deleted.');
    return;
  }
  if (d.taskFilter) { ui.tasksFilter = d.taskFilter; renderRoute(true); return; }
  if (d.goalForm !== undefined) { ui.goalForm = !ui.goalForm; await renderRoute(true); if (ui.goalForm) $('#goal-form input')?.focus(); return; }
  if (d.goalToggle) {
    const goal = (data?.goals || []).find(g => g.goal_id === d.goalToggle); if (!goal) return;
    await act(api(`/api/goals/${goal.goal_id}/update`, {state: goal.state === 'DONE' ? 'ACTIVE' : 'DONE'}), goal.state === 'DONE' ? 'Goal reopened.' : 'Goal achieved — nice work.');
    return;
  }
  if (d.goalMenu) {
    const goal = (data?.goals || []).find(g => g.goal_id === d.goalMenu); if (!goal) return;
    openMenu(el, `${goal.chat_id ? `<a href="#/agents/${esc(a.agent_id)}/work/${esc(goal.chat_id)}">Open conversation</a>` : ''}<button type="button" data-goal-talk="${esc(goal.goal_id)}">Talk about it</button><button type="button" data-goal-edit="${esc(goal.goal_id)}">Rename</button><button type="button" data-goal-note="${esc(goal.goal_id)}">Update progress</button><button type="button" data-goal-archive="${esc(goal.goal_id)}">Archive</button><hr><button type="button" class="danger" data-goal-delete="${esc(goal.goal_id)}">Delete…</button>`);
    return;
  }
  if (d.goalTalk && a) { closeMenus(); const goal = (data?.goals || []).find(g => g.goal_id === d.goalTalk); try { await startChat(a.agent_id, `Let's work on my goal: ${goal.title}. Where do things stand, and what's the next step?`, goal.title); } catch (error) { toast(error.message, true); } return; }
  if (d.goalEdit) { closeMenus(); const goal = (data?.goals || []).find(g => g.goal_id === d.goalEdit); const title = prompt('Goal', goal?.title || ''); if (title && title.trim()) await act(api(`/api/goals/${d.goalEdit}/update`, {title}), 'Goal renamed.'); return; }
  if (d.goalNote) { closeMenus(); const goal = (data?.goals || []).find(g => g.goal_id === d.goalNote); const progress = prompt('Progress', goal?.progress || ''); if (progress !== null) await act(api(`/api/goals/${d.goalNote}/update`, {progress}), 'Progress updated.'); return; }
  if (d.goalArchive) { closeMenus(); await act(api(`/api/goals/${d.goalArchive}/update`, {state: 'ARCHIVED'}), 'Goal archived.'); return; }
  if (d.goalDelete) { closeMenus(); if (!confirm('Delete this goal? This can’t be undone.')) return; await act(api(`/api/goals/${d.goalDelete}/delete`, {}), 'Goal deleted.'); return; }
  if (d.goalCategory && a) {
    if (a.lifecycle !== 'RUNNING' || a.archived) { toast('Enable this agent in its settings first.', true); return; }
    const label = (overview.goal_categories || {})[d.goalCategory] || 'new';
    const body = d.goalCategory === 'other' ? 'Help me set a new goal.' : `Help me set a ${label.toLowerCase()} goal.`;
    el.disabled = true;
    try { await startChat(a.agent_id, body, `Set a ${label === 'Something else' ? 'new' : label.toLowerCase()} goal`); } catch (error) { toast(error.message, true); el.disabled = false; }
    return;
  }
  if (d.scheduleMenu) {
    const job = (data?.schedules || []).find(s => s.schedule_id === d.scheduleMenu); if (!job) return;
    openMenu(el, `${job.chat_id && a ? `<a href="#/agents/${esc(a.agent_id)}/work/${esc(job.chat_id)}">Open conversation</a>` : ''}<button type="button" data-schedule-do="${job.enabled ? 'pause' : 'resume'}" data-id="${esc(job.schedule_id)}">${job.enabled ? 'Pause' : 'Resume'}</button><hr><button type="button" class="danger" data-schedule-do="delete" data-id="${esc(job.schedule_id)}">Delete…</button>`);
    return;
  }
  if (d.scheduleDo) { closeMenus(); if (d.scheduleDo === 'delete' && !confirm('Delete this scheduled job? It will not run again.')) return; await act(api(`/api/schedules/${d.id}/${d.scheduleDo}`, {}), {pause: 'Job paused.', resume: 'Job resumed.', delete: 'Job deleted.'}[d.scheduleDo]); return; }
  // Artifacts
  if (d.artifactFilter) { ui.artifactFilter = d.artifactFilter; renderRoute(false); return; }
  if (d.openItem !== undefined && pageData?.gallery) { const item = galleryItems(pageData)[Number(d.openItem)]; if (item) openItem(pageData, item); return; }
  if (d.artifact) { event.preventDefault(); openArtifact(d.artifact); return; }
  // Agent and task controls
  if (d.life) { await act(api(`/api/agents/${route.id}/lifecycle`, {action: d.life}), {start: 'Agent enabled.', pause: 'Agent paused — queued work waits.', stop: 'Agent disabled.'}[d.life]); return; }
  if (d.archive) { if (d.archive === 'true' && !confirm('Archive this agent? Its tasks, reports and files are kept.')) return; await act(api(`/api/agents/${route.id}/archive`, {archived: d.archive === 'true'}), d.archive === 'true' ? 'Archived — its reports and work are kept.' : 'Restored.'); return; }
  if (d.chatCancel || d.chatResume) { await act(api(`/api/tasks/${d.chatCancel || d.chatResume}/${d.chatCancel ? 'cancel' : 'resume'}`, {}), d.chatCancel ? 'Stop requested.' : 'Continuing.'); return; }
  if (d.approve || d.deny) { const id = d.approve || d.deny; await act(api(`/api/tasks/${id}/approval`, {decision: d.approve ? 'approve' : 'deny'}), d.approve ? 'Approved — the task resumes.' : 'Denied — the task stopped.'); return; }
  if (d.verify) { const [provider, model] = d.verify.split('|'); el.disabled = true; el.textContent = 'Verifying…'; await act(api('/api/providers/verify', {provider, model}), `Checked ${model}.`); return; }
  if (d.default) { const [provider, model] = d.default.split('|'); await act(api('/api/settings/default-model', {provider, model}), `New agents now default to ${model}.`); return; }
  if (d.revoke) { if (!confirm('Revoke this device session?')) return; await act(api('/api/sessions/revoke', {session_id: d.revoke}), 'Session revoked.'); return; }
  if (el.id === 'grant-full-access') { if (!confirm('Give every agent full access? Sensitive actions will still ask you first.')) return; await act(api('/api/agents/full-access', {}), 'Every agent now has full access.'); return; }
  if (el.id === 'refresh-providers') { await act(api('/api/providers/refresh', {}), 'Providers re-checked.'); return; }
  if (d.newProject !== undefined) { const name = prompt('Project name'); if (name) await act(api('/api/projects', {name}), 'Project created.'); }
});
document.addEventListener('input', event => {
  const composer = event.target.closest('#chat-form, #steer-form');
  if (composer) drafts.set(location.hash, Object.fromEntries(new FormData(composer)));
  if (event.target.id === 'chat-request') grow(event.target);
  if (event.target.id === 'chat-filter') { ui.convoFilter = event.target.value; renderSidebar(selected(), currentSpace()); }
  if (event.target.id === 'artifact-q') {
    ui.artifactQuery = event.target.value; const pos = event.target.selectionStart;
    lastHtml = ''; paint(renderAgent(pageData)); const box = $('#artifact-q'); box?.focus(); box?.setSelectionRange(pos, pos);
  }
});
document.addEventListener('change', event => {
  const composer = event.target.closest('#chat-form, #steer-form');
  if (composer) drafts.set(location.hash, Object.fromEntries(new FormData(composer)));
  const id = event.target.id;
  if (id === 'a-agent') { activityFilter.agent = event.target.value; lastHtml = ''; paint(renderActivity()); }
  if (id === 'a-detail') { activityFilter.detail = event.target.checked; lastHtml = ''; paint(renderActivity()); }
  if (id === 'cfg-provider') { const models = pageData.known_models[event.target.value] || []; $('#cfg-model').innerHTML = models.map(m => `<option value="${esc(m)}">${esc(m)}${checkLabel((overview.providers[event.target.value]?.models || []).find(x => x.model === m))}</option>`).join(''); }
});
document.addEventListener('submit', async event => {
  const form = event.target;
  if (form.id === 'chat-form') {
    event.preventDefault();
    const sourceRoute = location.hash;
    if (sendingRoutes.has(sourceRoute)) return;
    const agentId = route.id;
    const body = String(new FormData(form).get('body') || '').trim();
    if (!body) return;
    sendingRoutes.add(sourceRoute);
    try {
      let chatId = route.chat;
      if (!chatId) {
        const chat = await api(`/api/agents/${agentId}/chats`, {title: body.slice(0, 80) || 'New chat'});
        chatId = chat.chat_id;
      }
      const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
      await api(`/api/agents/${agentId}/messages`, {chat_id: chatId, body, request_id: requestId});
      drafts.delete(sourceRoute); lastHtml = '';
      if (location.hash === sourceRoute) {
        form.reset();
        location.hash = `#/agents/${agentId}/work/${chatId}`;
        await renderRoute(true);
      }
    } catch (error) { toast(error.message, true); }
    finally { sendingRoutes.delete(sourceRoute); }
  }
  if (form.id === 'goal-form') {
    event.preventDefault();
    const data = new FormData(form);
    document.activeElement?.blur();  // a focused form field holds repaints
    const goal = await act(api(`/api/agents/${route.id}/goals`, {title: data.get('title'), category: data.get('category')}), 'Goal added.');
    if (goal) { ui.goalForm = false; await renderRoute(true); }
  }
  if (form.id === 'search-form') {
    event.preventDefault();
    const query = String(new FormData(form).get('q') || '').trim();
    try { ui.search = {query, agent: route.id, results: query ? await api(`/api/agents/${route.id}/search?q=${encodeURIComponent(query)}`) : null}; document.activeElement?.blur(); await renderRoute(true); }
    catch (error) { toast(error.message, true); }
  }
  if (form.id === 'config-form') {
    event.preventDefault();
    const data = new FormData(form);
    const permissions = Object.fromEntries(Object.keys(pageData?.permission_labels || {}).map(k => [k, data.get(`perm-${k}`) === 'on']));
    await act(api(`/api/agents/${route.id}/config`, {provider: data.get('provider'), model: data.get('model'), permissions}), 'Agent settings saved for new messages.');
    lastHtml = '';
  }
});

// ---------- new agent ----------
function fillModels(provider, preferred) {
  const models = overview.known_models[provider] || [];
  const checks = overview.providers[provider]?.models || [];
  $('#agent-model').innerHTML = models.map(m => `<option value="${esc(m)}" ${m === preferred ? 'selected' : ''}>${esc(m)}${checkLabel(checks.find(x => x.model === m))}</option>`).join('');
  updateModelNote();
}
function updateModelNote() {
  const provider = $('#agent-provider').value;
  const p = overview.providers[provider];
  $('#agent-model-note').textContent = !p?.authenticated ? `${provider} is not signed in on the backend host.` : 'Agent execution uses the selected subscription and the abilities you grant below.';
}
function openNewAgent() {
  if (!overview) return;
  const d = overview.defaults, form = $('#agent-form');
  form.reset();
  $('#agent-provider').innerHTML = ['claude-cli', 'codex-cli'].map(p => `<option value="${p}" ${p === d.provider ? 'selected' : ''}>${p === 'claude-cli' ? 'Claude CLI' : 'Codex CLI'}</option>`).join('');
  fillModels(d.provider, d.model);
  $('#agent-project').innerHTML = overview.projects.map(p => `<option value="${esc(p.project_id)}">${esc(p.name)}</option>`).join('');
  $('#agent-permissions').innerHTML = Object.entries(overview.permissions.labels).map(([k, label]) => `<label class="check"><input type="checkbox" name="perm-${k}" ${overview.permissions.defaults[k] ? 'checked' : ''}> ${esc(label)}</label>`).join('');
  $('#agent-dialog').showModal();
}
$('#agent-provider').addEventListener('change', event => fillModels(event.target.value, null));
$('#agent-model').addEventListener('change', updateModelNote);
$('#agent-form').addEventListener('submit', async event => {
  event.preventDefault();
  const data = new FormData(event.target);
  const permissions = Object.fromEntries(Object.keys(overview.permissions.labels).map(k => [k, data.get(`perm-${k}`) === 'on']));
  const body = {name: data.get('name'), role: data.get('role'), purpose: data.get('purpose'), instructions: data.get('instructions'),
    provider: data.get('provider'), model: data.get('model'), project_id: data.get('project_id'), permissions, enable: data.get('enable') === 'on'};
  const agent = await act(api('/api/agents', body), 'Agent created.');
  if (agent) { $('#agent-dialog').close(); pickAgent(agent.agent_id); }
});

// ---------- artifact versions ----------
async function openArtifact(id) {
  try {
    const a = await api(`/api/artifacts/${encodeURIComponent(id)}`);
    $('#preview-title').textContent = a.path;
    $('#preview-meta').innerHTML = `${esc(a.change)} · version ${a.version} of ${a.versions.length} · ${esc(fmtSize(a.size))} · ${esc(a.mime)}`;
    const tabs = [['preview', 'Preview']];
    if (a.diff) tabs.push(['diff', 'Changes']);
    if (a.versions.length > 1) tabs.push(['versions', 'Versions']);
    $('#preview-tabs').innerHTML = tabs.map(([k, l], i) => `<button data-ptab="${k}" class="${i === 0 ? 'active' : ''}">${l}</button>`).join('') + `<span class="spacer"></span><button data-download="${esc(a.artifact_id)}">Download</button>`;
    const body = $('#preview-body');
    const show = async tab => {
      document.querySelectorAll('[data-ptab]').forEach(b => b.classList.toggle('active', b.dataset.ptab === tab));
      if (tab === 'diff') { body.innerHTML = diffHtml(a.diff); return; }
      if (tab === 'versions') { body.innerHTML = artifactList(a.versions.slice().reverse()); return; }
      if (/^image\/(png|jpeg|gif|webp)$/.test(a.mime)) { const url = URL.createObjectURL(await apiBlob(`/api/artifacts/${a.artifact_id}/content`)); viewerUrls.push(url); body.innerHTML = `<img alt="${esc(a.path)}" src="${url}">`; return; }
      if (a.mime.startsWith('text/') || /\.(md|txt|py|js|ts|json|csv|css|html|yml|yaml|toml|sh|ps1)$/i.test(a.path)) {
        const text = await (await apiBlob(`/api/artifacts/${a.artifact_id}/content`)).text();
        body.innerHTML = /\.md$/i.test(a.path) ? `<div class="result">${md(text)}</div>` : `<pre>${esc(text)}</pre>`;
        return;
      }
      body.innerHTML = '<p class="muted">No inline preview for this file type. Use Download.</p>';
    };
    $('#preview-tabs').onclick = async e => {
      const b = e.target.closest('button'); if (!b) return;
      if (b.dataset.ptab) show(b.dataset.ptab);
      if (b.dataset.download) { const blob = await apiBlob(`/api/artifacts/${a.artifact_id}/content?download=1`); const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = a.path.split('/').pop(); link.click(); }
    };
    await show('preview');
    if (!$('#preview-dialog').open) $('#preview-dialog').showModal();
  } catch (error) { toast(error.message, true); }
}

// ---------- start ----------
if (store('jarvis.hub.detailsClosed') === '1') document.body.classList.add('details-closed');
route = parseRoute();
tick(true).then(schedule);
