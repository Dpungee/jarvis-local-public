'use strict';
// JARVIS Agent Hub. Everything shown here is read from the backend; the browser keeps only
// display caches and reconciles on every poll. Closing the tab changes nothing on the backend.
// Layout (ChatGPT-style chrome): agent sidebar with chats · main thread and composer ·
// a tabbed workspace panel (Progress, Changes, Preview, Files, Agent) on the right.

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
function session(key, value) { try { if (value === undefined) return sessionStorage.getItem(key); if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value); } catch { /* storage unavailable */ } return null; }
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.classList.toggle('error', error); el.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 4200);
}
const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fall back below */ }
  const area = document.createElement('textarea');
  area.value = text; area.setAttribute('readonly', ''); area.className = 'offscreen';
  document.body.appendChild(area); area.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch { ok = false; }
  area.remove();
  return ok;
}
function download(blob, name) {
  const link = document.createElement('a'), url = URL.createObjectURL(blob);
  link.href = url; link.download = String(name || 'download').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '_').slice(0, 120);
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
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
const baseName = p => String(p || '').split(/[\\/]/).pop();
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
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[*`#>]+/g, '')
    // _emphasis_ loses its marks; identifiers such as parse_csv keep their underscores.
    .replace(/(^|[\s(])_([^_\s][^_]*?)_(?=[\s).,;:!?]|$)/g, '$1$2').replace(/\s+/g, ' ').trim();
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
  compose: '<svg viewBox="0 0 24 24"><path d="M11.5 4.5H7A2.5 2.5 0 0 0 4.5 7v10A2.5 2.5 0 0 0 7 19.5h10a2.5 2.5 0 0 0 2.5-2.5v-4.5"/><path d="M17.3 3.9a1.9 1.9 0 0 1 2.7 2.7l-7.6 7.6-3.4.7.7-3.4z"/></svg>',
  library: '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="3"/><circle cx="9" cy="10" r="1.6"/><path d="m20.5 16-4.6-4.6a1.5 1.5 0 0 0-2.1 0L6 19.3"/></svg>',
  plug: '<svg viewBox="0 0 24 24"><path d="M9 3.5v4M15 3.5v4M6.5 7.5h11V11a5.5 5.5 0 0 1-11 0zM12 16.5v4"/></svg>',
  sidebar: '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="M9.5 4.5v15"/></svg>',
  panel: '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="M14.5 4.5v15"/></svg>',
  share: '<svg viewBox="0 0 24 24"><path d="M12 14.5V4M8 7.5l4-4 4 4"/><path d="M5 12v4.5A2.5 2.5 0 0 0 7.5 19h9a2.5 2.5 0 0 0 2.5-2.5V12"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2.5"/><path d="M15.5 8.5v-2A2.5 2.5 0 0 0 13 4H6.5A2.5 2.5 0 0 0 4 6.5V13a2.5 2.5 0 0 0 2.5 2.5h2"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M7.5 10.5v9h-2A1.5 1.5 0 0 1 4 18v-6a1.5 1.5 0 0 1 1.5-1.5zm0 0 3.6-6.3a1.7 1.7 0 0 1 3.1 1.2l-.7 4h4.6a2 2 0 0 1 2 2.4l-1.3 6.4a2.2 2.2 0 0 1-2.2 1.8H7.5"/></svg>',
  upFill: '<svg viewBox="0 0 24 24" class="filled"><path d="M7.5 10.5v9h-2A1.5 1.5 0 0 1 4 18v-6a1.5 1.5 0 0 1 1.5-1.5zm0 0 3.6-6.3a1.7 1.7 0 0 1 3.1 1.2l-.7 4h4.6a2 2 0 0 1 2 2.4l-1.3 6.4a2.2 2.2 0 0 1-2.2 1.8H7.5"/></svg>',
  down: '<svg viewBox="0 0 24 24"><g transform="rotate(180 12 12)"><path d="M7.5 10.5v9h-2A1.5 1.5 0 0 1 4 18v-6a1.5 1.5 0 0 1 1.5-1.5zm0 0 3.6-6.3a1.7 1.7 0 0 1 3.1 1.2l-.7 4h4.6a2 2 0 0 1 2 2.4l-1.3 6.4a2.2 2.2 0 0 1-2.2 1.8H7.5"/></g></svg>',
  downFill: '<svg viewBox="0 0 24 24" class="filled"><g transform="rotate(180 12 12)"><path d="M7.5 10.5v9h-2A1.5 1.5 0 0 1 4 18v-6a1.5 1.5 0 0 1 1.5-1.5zm0 0 3.6-6.3a1.7 1.7 0 0 1 3.1 1.2l-.7 4h4.6a2 2 0 0 1 2 2.4l-1.3 6.4a2.2 2.2 0 0 1-2.2 1.8H7.5"/></g></svg>',
  speaker: '<svg viewBox="0 0 24 24"><path d="M4.5 9.5v5H8l4.5 4v-13L8 9.5z"/><path d="M16 9a4.2 4.2 0 0 1 0 6M18.6 6.4a7.8 7.8 0 0 1 0 11.2"/></svg>',
  stopSq: '<svg viewBox="0 0 24 24" class="filled"><rect x="7" y="7" width="10" height="10" rx="1.6"/></svg>',
  regen: '<svg viewBox="0 0 24 24"><path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4.2h-4.2"/></svg>',
  dots: '<svg viewBox="0 0 24 24" class="filled"><circle cx="5.5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="18.5" cy="12" r="1.5"/></svg>',
  pencil: '<svg viewBox="0 0 24 24"><path d="M15.6 5.4a2 2 0 0 1 2.9 2.9L8.7 18.1l-3.9 1 1-3.9z"/><path d="m14 7 3 3"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  mic: '<svg viewBox="0 0 24 24"><rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v2.5"/></svg>',
  arrowUp: '<svg viewBox="0 0 24 24"><path d="M12 19V5M6 11l6-6 6 6"/></svg>',
  arrowDown: '<svg viewBox="0 0 24 24"><path d="M12 5v14M6 13l6 6 6-6"/></svg>',
  clip: '<svg viewBox="0 0 24 24"><path d="m19 11.5-6.8 6.8a4.5 4.5 0 0 1-6.4-6.4l7.3-7.3a3 3 0 0 1 4.2 4.2l-7.2 7.2a1.5 1.5 0 0 1-2.1-2.1l6.6-6.6"/></svg>',
  image: '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="m3.5 16 5-5 4 4 2.5-2.5 5 5"/><circle cx="15.5" cy="9" r="1.5"/></svg>',
  telescope: '<svg viewBox="0 0 24 24"><path d="m3.5 13.5 11-6 2 3.6-11 6z"/><path d="m14.5 7.5 3-1.6 2 3.6-3 1.6M10 15.5l-2.5 5M11.5 15l2.5 5.5"/></svg>',
  chevronDown: '<svg viewBox="0 0 24 24" class="chev"><path d="m7 10 5 5 5-5"/></svg>',
  chevronRight: '<svg viewBox="0 0 24 24" class="chev"><path d="m10 7 5 5-5 5"/></svg>',
  close: '<svg viewBox="0 0 24 24"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>',
  file: '<svg viewBox="0 0 24 24"><path d="M13.5 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8.5z"/><path d="M13.5 3.5v5h5"/></svg>',
  run: '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="m7.5 9.5 3 2.5-3 2.5M12.5 15h4"/></svg>',
  globe: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.6 3.6 5.4 3.6 8.5s-1.1 5.9-3.6 8.5c-2.5-2.6-3.6-5.4-3.6-8.5S9.5 6.1 12 3.5z"/></svg>',
  browser: '<svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="13" rx="2.5"/><path d="M3.5 8.5h17M13 12.5l6 2.2-2.6 1-1 2.6z"/></svg>',
  memory: '<svg viewBox="0 0 24 24"><path d="M7 3.5h10a1.5 1.5 0 0 1 1.5 1.5v15.5L12 16.5l-6.5 4V5A1.5 1.5 0 0 1 7 3.5z"/></svg>',
  wrench: '<svg viewBox="0 0 24 24"><path d="M14.7 6.3a4 4 0 0 0 5 5l-8.9 8.9a2.1 2.1 0 1 1-3-3l8.9-8.9a4 4 0 0 0-2-2z"/><path d="M14.7 6.3 17 4l3 3-2.3 2.3"/></svg>',
  branch: '<svg viewBox="0 0 24 24"><circle cx="7" cy="5.5" r="2"/><circle cx="7" cy="18.5" r="2"/><circle cx="17" cy="8" r="2"/><path d="M7 7.5v9M17 10c0 4-10 2.5-10 6.5"/></svg>',
  diff: '<svg viewBox="0 0 24 24"><path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M12 8.5v5M9.5 11h5M9.5 16.5h5"/></svg>',
  eye: '<svg viewBox="0 0 24 24"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  agent: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8.5" r="3.5"/><path d="M5 19.5c.8-3.5 3.6-5.5 7-5.5s6.2 2 7 5.5"/></svg>',
  gear: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.4-2.3.9a7 7 0 0 0-2-1.2L14.3 3h-4l-.4 2.6a7 7 0 0 0-2 1.2l-2.3-.9-2 3.4 2 1.5a7 7 0 0 0 0 2.4l-2 1.5 2 3.4 2.3-.9a7 7 0 0 0 2 1.2l.4 2.6h4l.4-2.6a7 7 0 0 0 2-1.2l2.3.9 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z"/></svg>',
  sliders: '<svg viewBox="0 0 24 24"><path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></svg>',
  keyboard: '<svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="2.5"/><path d="M7 10h.01M11 10h.01M15 10h.01M8 14h8"/></svg>',
  help: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M9.8 9.5a2.3 2.3 0 0 1 4.4.9c0 1.6-2.2 2-2.2 3.4M12 16.5h.01"/></svg>',
  model: '<svg viewBox="0 0 24 24"><path d="M12 3.5 19.5 8v8L12 20.5 4.5 16V8z"/><path d="M12 12 19.5 8M12 12v8.5M12 12 4.5 8"/></svg>',
  gauge: '<svg viewBox="0 0 24 24"><path d="M4.5 16a7.5 7.5 0 1 1 15 0"/><path d="m12 16 3.5-4.5"/></svg>',
  compact: '<svg viewBox="0 0 24 24"><path d="M8 4v4.5H3.5M16 4v4.5h4.5M8 20v-4.5H3.5M16 20v-4.5h4.5"/></svg>',
};
const KIND_ICON = {documents: '▤', web: '◎', code: '</>', images: '▣', videos: '▶', audio: '♪', other: '◇'};
const KIND_LABEL = {all: 'All artifacts', documents: 'Documents', web: 'Web artifacts', code: 'Code', images: 'Images', videos: 'Videos', audio: 'Audio', other: 'Other files', folder: 'Project folder'};

// ---------- Markdown ----------
// Safe Markdown: escape everything first, then add a small set of formatting. Links must be
// http(s); images must be https and are shown as thumbnails that link to their source. Code
// blocks are highlighted by a small built-in tokenizer and every token is escaped. Rendering is
// pure, so results are cached by source text (views repaint on every poll).
const mdCache = new Map();
function md(source) {
  const key = String(source || '');
  const hit = mdCache.get(key);
  if (hit !== undefined) return hit;
  const html = key.replace(/\r\n/g, '\n').split(/```/).map((block, i) => i % 2 === 1 ? codeBlock(block) : mdText(block)).join('');
  mdCache.set(key, html);
  if (mdCache.size > 400) mdCache.delete(mdCache.keys().next().value);
  return html;
}
function codeBlock(block) {
  const lang = (block.match(/^([\w#+.-]+)[ \t]*\n/) || [])[1] || '';
  const code = block.replace(/^[\w#+.-]*[ \t]*\n/, '').replace(/\n$/, '');
  return `<div class="code-block"><div class="code-head"><span class="code-lang">${esc(lang.toLowerCase() || 'code')}</span><button type="button" class="code-copy" data-copy-code aria-label="Copy code">${ICONS.copy}<span>Copy code</span></button></div><pre${lang ? ` data-lang="${esc(lang)}"` : ''}><code>${highlight(code, lang)}</code></pre></div>`;
}
function mdText(block) {
  // Display math ($$…$$ or \[…\]) is lifted out first so emphasis rules never touch it.
  const maths = [];
  const src = block.replace(/\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]/g, (_, a, b) => `\n\u0001${maths.push(a ?? b) - 1}\u0001\n`);
  const lines = esc(src).split('\n');
  let html = '', list = null, para = [], quote = [], table = [];
  const flushPara = () => { if (para.length) { html += `<p>${para.map(inline).join('<br>')}</p>`; para = []; } };
  const flushList = () => { if (list) { html += `</${list}>`; list = null; } };
  const flushQuote = () => { if (quote.length) { html += `<blockquote>${quote.map(inline).join('<br>')}</blockquote>`; quote = []; } };
  const flushTable = () => { if (table.length) { html += mdTable(table); table = []; } };
  const flushAll = () => { flushPara(); flushList(); flushQuote(); flushTable(); };
  for (const raw of lines) {
    let m;
    if ((m = raw.match(/^\s*\u0001(\d+)\u0001\s*$/))) { flushAll(); const tex = esc(maths[Number(m[1])].trim()); html += `<div class="math-block" role="math" aria-label="${tex}">${texText(tex)}</div>`; continue; }
    if (/^\s*\|.*\|\s*$/.test(raw)) { flushPara(); flushList(); flushQuote(); table.push(raw); continue; }
    flushTable();
    if (/^\s*(?:\[?!\[[^\]]*\]\(https:\/\/[^\s)]+\)(?:\]\(https?:\/\/[^\s)]+\))?\s*)+$/.test(raw)) { flushAll(); html += `<div class="img-row">${inline(raw)}</div>`; }
    else if ((m = raw.match(/^(#{1,4})\s+(.*)$/))) { flushAll(); const level = [0, 2, 2, 3, 4][m[1].length]; html += `<h${level}>${inline(m[2])}</h${level}>`; }
    // A line that is only bold text ("**4. Big news** (Sep 25)") is a section title.
    else if (/^\s*\*\*[^*]{2,120}\*\*(?:\s*[(:—-][^*]{0,60})?\s*$/.test(raw) && !/^\s*\*\*[^*]+\*\*\s*[:—-]\s*\S{20,}/.test(raw)) { flushAll(); html += `<h3>${inline(raw.trim())}</h3>`; }
    else if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(raw)) { flushAll(); html += '<hr>'; }
    else if ((m = raw.match(/^\s*&gt;\s?(.*)$/))) { flushPara(); flushList(); quote.push(m[1]); }
    else if ((m = raw.match(/^(\s*)[-*•]\s+(.*)$/))) { flushPara(); flushQuote(); if (list !== 'ul') { flushList(); html += '<ul>'; list = 'ul'; } html += `<li${m[1].length >= 2 ? ' class="sub"' : ''}>${inline(m[2])}</li>`; }
    else if ((m = raw.match(/^\s*(\d+)[.)]\s+(.*)$/))) { flushPara(); flushQuote(); if (list !== 'ol') { flushList(); html += `<ol start="${Number(m[1])}">`; list = 'ol'; } html += `<li>${inline(m[2])}</li>`; }
    else if (!raw.trim()) { flushAll(); }
    else { flushList(); flushQuote(); para.push(raw); }
  }
  flushAll();
  return html;
}
function mdTable(rows) {
  const cells = row => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  const separator = row => /^\s*\|?(\s*:?-{2,}:?\s*\|)*\s*:?-{2,}:?\s*\|?\s*$/.test(row);
  const header = rows.length > 1 && separator(rows[1]) ? cells(rows[0]) : null;
  const body = rows.filter((row, i) => !(header && i <= 1) && !separator(row));
  return `<div class="table-wrap"><table>${header ? `<thead><tr>${header.map(c => `<th>${inline(c)}</th>`).join('')}</tr></thead>` : ''}<tbody>${body.map(row => `<tr>${cells(row).map(c => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
const IMG = (src, alt) => `<img class="md-img" src="${src}" alt="${alt}" loading="lazy" referrerpolicy="no-referrer">`;
function linkHost(url) {
  try { return new URL(url.replace(/&amp;/g, '&')).hostname.replace(/^www\./, ''); } catch { return 'link'; }
}
const LINK = (href, text, chip) => `<a class="${chip ? 'link-chip' : 'md-link'}" href="${href}" title="${href}" target="_blank" rel="noopener noreferrer nofollow">${text}</a>`;
function inline(escaped) {
  const codes = [], maths = [];
  let text = escaped.replace(/`([^`]+)`/g, (_, code) => `\u0000${codes.push(code) - 1}\u0000`)
    .replace(/\\\((.+?)\\\)/g, (_, tex) => `\u0002${maths.push(tex) - 1}\u0002`);
  text = text
    .replace(/\[!\[([^\]]*)\]\((https:\/\/[^\s)]+)\)\]\((https?:\/\/[^\s)]+)\)/g, (_, alt, src, href) => `<a href="${href}" target="_blank" rel="noopener noreferrer nofollow">${IMG(src, alt)}</a>`)
    .replace(/!\[([^\]]*)\]\((https:\/\/[^\s)]+)\)/g, (_, alt, src) => `<a href="${src}" target="_blank" rel="noopener noreferrer nofollow">${IMG(src, alt)}</a>`)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)&]+(?:&amp;[^\s)&]+)*)\)/g, (_, label, href) => /^https?:\/\//.test(label) ? LINK(href, linkHost(href), true) : LINK(href, label, false))
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (_, lead, href) => `${lead}${LINK(href.replace(/[.,;:!?]+$/, ''), linkHost(href), true)}${(href.match(/[.,;:!?]+$/) || [''])[0]}`)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w*])\*(?![\s*])([^*\n]+?)\*(?![\w*])/g, '$1<em>$2</em>')
    .replace(/(^|[\s(>])_(?![\s_])([^_\n<]+?)_(?![\w])/g, '$1<em>$2</em>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>');
  return text.replace(/\u0002(\d+)\u0002/g, (_, i) => `<span class="math-inline" role="math">${texText(maths[Number(i)])}</span>`)
    .replace(/\u0000(\d+)\u0000/g, (_, index) => `<code>${codes[Number(index)]}</code>`);
}
// KaTeX-free math: common TeX commands become Unicode, ^ and _ become super/subscripts. The input
// is already escaped text; only <sup>, <sub> and <br> are added.
const TEX = {alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', varepsilon: 'ε', zeta: 'ζ', eta: 'η', theta: 'θ', vartheta: 'ϑ', iota: 'ι', kappa: 'κ', lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ', tau: 'τ', upsilon: 'υ', phi: 'φ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
  Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π', Sigma: 'Σ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω', times: '×', cdot: '·', div: '÷', pm: '±', mp: '∓', leq: '≤', le: '≤', geq: '≥', ge: '≥', neq: '≠', ne: '≠',
  approx: '≈', equiv: '≡', sim: '∼', propto: '∝', infty: '∞', sum: '∑', prod: '∏', int: '∫', oint: '∮', partial: '∂', nabla: '∇', in: '∈', notin: '∉', subset: '⊂', subseteq: '⊆', supset: '⊃', supseteq: '⊇', cup: '∪', cap: '∩',
  emptyset: '∅', forall: '∀', exists: '∃', neg: '¬', land: '∧', lor: '∨', to: '→', rightarrow: '→', leftarrow: '←', Rightarrow: '⇒', Leftarrow: '⇐', leftrightarrow: '↔', iff: '⇔', implies: '⇒', mapsto: '↦',
  ldots: '…', cdots: '⋯', dots: '…', circ: '∘', degree: '°', angle: '∠', perp: '⊥', parallel: '∥', hbar: 'ℏ', ell: 'ℓ', aleph: 'ℵ', langle: '⟨', rangle: '⟩', lfloor: '⌊', rfloor: '⌋', lceil: '⌈', rceil: '⌉', mid: '∣',
  star: '⋆', ast: '∗', oplus: '⊕', otimes: '⊗', quad: ' ', qquad: '  ', log: 'log', ln: 'ln', sin: 'sin', cos: 'cos', tan: 'tan', exp: 'exp', lim: 'lim', max: 'max', min: 'min', det: 'det', mod: 'mod'};
function texText(s) {
  let t = String(s);
  for (let i = 0; i < 4; i++) t = t.replace(/\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, '($1)/($2)').replace(/\\sqrt\s*\{([^{}]*)\}/g, '√($1)');
  return t.replace(/\\(?:begin|end)\s*\{[^{}]*\}/g, '')
    .replace(/\\(?:text|mathrm|mathbf|mathit|mathsf|mathtt|operatorname|mathbb|mathcal|boldsymbol)\s*\{([^{}]*)\}/g, '$1')
    .replace(/\\(?:left|right|big|Big|bigg|Bigg|displaystyle)\b\s*/g, '')
    .replace(/\\\\/g, '<br>').replace(/&amp;/g, ' ')
    .replace(/\\([A-Za-z]+)/g, (m, name) => TEX[name] ?? name)
    .replace(/\\[,;:! ]/g, ' ').replace(/\\([{}%$#_])/g, '$1')
    .replace(/\^\{([^{}]*)\}/g, '<sup>$1</sup>').replace(/\^([A-Za-z0-9+\-*′])/g, '<sup>$1</sup>')
    .replace(/_\{([^{}]*)\}/g, '<sub>$1</sub>').replace(/_([A-Za-z0-9])/g, '<sub>$1</sub>')
    .replace(/[{}]/g, '');
}
// ---------- syntax highlighting (small built-in tokenizer; every token is escaped) ----------
const KW_JS = 'break case catch class const continue debugger default delete do else export extends finally for from function if import in instanceof let new of return static super switch this throw try typeof var void while with yield async await';
const KW_C = 'auto break case catch char class const constexpr continue default delete do double else enum explicit extern float for friend goto if inline int long namespace new operator private protected public register return short signed sizeof static struct switch template this throw try typedef typename union unsigned using virtual void volatile while bool';
const HL_WORDS = {
  js: KW_JS, ts: `${KW_JS} interface type enum implements namespace declare readonly private public protected abstract as keyof infer is satisfies`,
  py: 'and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case',
  sh: 'if then else elif fi for while until do done case esac function in return local export readonly declare source alias set unset shift exit sudo cd echo',
  ps: 'begin break catch class continue data do dynamicparam else elseif end exit filter finally for foreach from function if in param process return switch throw trap try until using while',
  sql: 'select from where and or not insert into values update set delete create table drop alter add column join left right inner outer full cross on group by order having limit offset as distinct union all is in like between case when then else end primary key foreign references index view with returning default constraint unique check exists asc desc',
  c: KW_C, cs: `${KW_C} abstract as base checked decimal event fixed foreach implicit in interface internal is lock object out override params readonly ref sbyte sealed stackalloc string uint ulong unchecked unsafe ushort var async await get set yield`,
  java: `${KW_C} abstract assert boolean byte extends final finally implements import instanceof interface native package strictfp super synchronized throws transient var record`,
  go: 'break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var',
  rust: 'as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait type unsafe use where while',
  rb: 'alias and begin break case class def do else elsif end ensure for if in module next not or redo rescue retry return self super then undef unless until when while yield require',
  php: 'abstract and array as break case catch class clone const continue declare default do echo else elseif empty extends final finally fn for foreach function global if implements include instanceof interface isset list match namespace new or print private protected public readonly require return static switch throw trait try unset use var while yield',
  kt: 'as break class continue do else for fun if in interface is object package return super this throw try typealias val var when while data sealed override open private public internal suspend',
  swift: 'class deinit enum extension func import init inout internal let open operator private protocol public static struct subscript typealias var break case continue default defer do else fallthrough for guard if in repeat return switch where while as catch is throw throws try await async',
  css: 'important media import supports keyframes font-face from to', json: '', yaml: '',
  generic: 'if else elif for while return function def class import from const let var new try catch finally throw switch case break continue in of fn func local end then do',
};
const HL_BUILTINS = {py: 'str int float bool bytes list dict set tuple len print range open type object Exception ValueError TypeError KeyError super isinstance enumerate zip map filter sorted sum min max abs any all self cls',
  js: 'console Math JSON Object Array Promise Number String Boolean Date Map Set Error RegExp Symbol window document globalThis require module process', ts: 'console Math JSON Object Array Promise Number String Boolean Date Map Set Error RegExp Symbol window document string number boolean any unknown never void Record Partial',
  sh: 'grep sed awk cat ls rm cp mv mkdir curl git npm node python pip docker', ps: 'Write-Host Get-ChildItem Set-Location Get-Content Set-Content New-Item Remove-Item',
  go: 'fmt string int error bool byte rune len make append panic nil', rust: 'String Vec Option Result Some None Ok Err Box println format i32 i64 u8 u32 u64 usize f64 bool str', java: 'String System Integer List Map ArrayList HashMap Override', cs: 'Console String List Dictionary Task var', c: 'printf std cout endl size_t NULL', kt: 'println String Int List', swift: 'print String Int Array'};
const HL_LIT = new Set('true false null undefined None True False nil NaN Infinity TRUE FALSE NULL'.split(' '));
const HL_LANG = {javascript: 'js', js: 'js', jsx: 'js', mjs: 'js', cjs: 'js', node: 'js', typescript: 'ts', ts: 'ts', tsx: 'ts', python: 'py', py: 'py', python3: 'py', bash: 'sh', sh: 'sh', shell: 'sh', zsh: 'sh', console: 'sh', terminal: 'sh', dockerfile: 'sh', makefile: 'sh',
  powershell: 'ps', ps1: 'ps', pwsh: 'ps', ps: 'ps', bat: 'ps', cmd: 'ps', sql: 'sql', postgres: 'sql', postgresql: 'sql', mysql: 'sql', sqlite: 'sql', c: 'c', h: 'c', cpp: 'c', 'c++': 'c', hpp: 'c', cc: 'c', cs: 'cs', csharp: 'cs', 'c#': 'cs',
  java: 'java', scala: 'java', dart: 'java', go: 'go', golang: 'go', rust: 'rust', rs: 'rust', ruby: 'rb', rb: 'rb', php: 'php', kotlin: 'kt', kt: 'kt', swift: 'swift', css: 'css', scss: 'css', less: 'css',
  json: 'json', jsonc: 'json', json5: 'json', yaml: 'yaml', yml: 'yaml', toml: 'yaml', ini: 'yaml', env: 'yaml', html: 'html', xml: 'html', svg: 'html', vue: 'html', svelte: 'html', markup: 'html', lua: 'generic', r: 'generic', perl: 'generic'};
const HL_COMMENTS = {js: ['//', '/*'], ts: ['//', '/*'], c: ['//', '/*'], cs: ['//', '/*'], java: ['//', '/*'], go: ['//', '/*'], rust: ['//', '/*'], kt: ['//', '/*'], swift: ['//', '/*'], php: ['//', '#', '/*'], css: ['/*'],
  py: ['#'], sh: ['#'], ps: ['#'], rb: ['#'], yaml: ['#'], sql: ['--', '/*'], json: [], generic: ['//', '#', '/*']};
const hlSpecs = new Map();
function hlSpec(lang) {
  if (hlSpecs.has(lang)) return hlSpecs.get(lang);
  const c = HL_COMMENTS[lang] || [];
  const comments = [c.includes('/*') && /\/\*[\s\S]*?(?:\*\/|$)/.source, c.includes('//') && /\/\/[^\n]*/.source, c.includes('#') && /#[^\n]*/.source, c.includes('--') && /--[^\n]*/.source].filter(Boolean);
  const strings = [lang === 'py' && /"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)/.source, /"(?:\\[\s\S]|[^"\\\n])*"?/.source,
    lang !== 'rust' && /'(?:\\[\s\S]|[^'\\\n])*'?/.source, ['js', 'ts', 'sh', 'generic'].includes(lang) && /`(?:\\[\s\S]|[^`\\])*`?/.source].filter(Boolean);
  const numbers = [lang === 'css' && /#[\da-fA-F]{3,8}\b/.source, /\b0[xX][\da-fA-F_]+\b|\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?[a-zA-Z%]*/.source].filter(Boolean);
  const word = lang === 'css' ? /-?[A-Za-z_][\w-]*/.source : /[A-Za-z_$][\w$]*/.source;
  const spec = {
    re: new RegExp(`(${comments.join('|') || '(?!)'})|(${strings.join('|')})|(${numbers.join('|')})|(${word})`, 'g'),
    kw: new Set(HL_WORDS[lang].split(' ').filter(Boolean)), ci: lang === 'sql' || lang === 'ps', bi: new Set((HL_BUILTINS[lang] || '').split(' ').filter(Boolean)),
    types: ['ts', 'c', 'cs', 'java', 'go', 'rust', 'kt', 'swift', 'php'].includes(lang), keys: ['json', 'yaml', 'css'].includes(lang),
  };
  hlSpecs.set(lang, spec);
  return spec;
}
const FOLLOWED_BY_PAREN = /\s*\(/y, FOLLOWED_BY_COLON = /\s*:(?!:)/y;
const DEFINES_FN = new Set(['def', 'function', 'fn', 'func', 'fun']), DEFINES_TYPE = new Set(['class', 'struct', 'interface', 'enum', 'trait', 'type', 'impl']);
function highlight(code, lang) {
  const key = HL_LANG[String(lang).toLowerCase()] || (lang && !/^(text|txt|plain|plaintext|output|log|md|markdown)$/i.test(lang) ? 'generic' : '');
  if (!key || code.length > 60000) return esc(code);
  if (key === 'html') return highlightHtml(code);
  const spec = hlSpec(key), re = spec.re;
  let out = '', last = 0, prev = '', m;
  const span = (cls, text) => `<span class="tk-${cls}">${esc(text)}</span>`;
  re.lastIndex = 0;
  while ((m = re.exec(code))) {
    if (m[0] === '') { re.lastIndex++; continue; }
    if (m.index > last) out += esc(code.slice(last, m.index));
    const t = m[0];
    if (m[1] !== undefined) out += span('c', t);
    else if (m[2] !== undefined) { FOLLOWED_BY_COLON.lastIndex = re.lastIndex; out += span(spec.keys && FOLLOWED_BY_COLON.test(code) ? 'a' : 's', t); }
    else if (m[3] !== undefined) out += span('n', t);
    else {
      let cls = '';
      FOLLOWED_BY_PAREN.lastIndex = re.lastIndex;
      if (spec.kw.has(spec.ci ? t.toLowerCase() : t)) cls = 'k';
      else if (HL_LIT.has(t)) cls = 'l';
      else if (spec.bi.has(t)) cls = 't';
      else if (DEFINES_FN.has(prev)) cls = 'f';
      else if (DEFINES_TYPE.has(prev)) cls = 't';
      else if (FOLLOWED_BY_PAREN.test(code)) cls = 'f';
      else if (spec.keys) { FOLLOWED_BY_COLON.lastIndex = re.lastIndex; if (FOLLOWED_BY_COLON.test(code)) cls = 'a'; }
      else if (spec.types && /^[A-Z][a-z0-9]+[A-Za-z0-9]*$/.test(t)) cls = 't';
      out += cls ? span(cls, t) : esc(t);
      prev = t;
    }
    last = re.lastIndex;
  }
  return out + esc(code.slice(last));
}
function highlightHtml(code) {
  const re = /(<!--[\s\S]*?(?:-->|$))|(<\/?[A-Za-z!][^>]*>?)/g;
  let out = '', last = 0, m;
  while ((m = re.exec(code))) {
    if (m.index > last) out += esc(code.slice(last, m.index));
    if (m[1] !== undefined) out += `<span class="tk-c">${esc(m[1])}</span>`;
    else {
      const tag = m[2], name = (tag.match(/^<\/?[\w!:.-]+/) || [''])[0];
      const rest = tag.slice(name.length).replace(/([\w:@.-]+)(=)?("[^"]*"|'[^']*'|[^\s>]*)?|([^\w:@.-]+)/g, (all, attr, eq, value, other) =>
        other !== undefined ? esc(other) : `<span class="tk-a">${esc(attr)}</span>${eq ? esc(eq) : ''}${value ? `<span class="tk-s">${esc(value)}</span>` : ''}`);
      out += `<span class="tk-t">${esc(name)}</span>${rest}`;
    }
    last = re.lastIndex;
  }
  return out + esc(code.slice(last));
}
function diffHtml(diff) {
  return `<pre class="diff">${String(diff || '').split('\n').map(line => {
    const cls = line.startsWith('+++') || line.startsWith('---') ? 'hunk' : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : line.startsWith('@@') ? 'hunk' : '';
    return `<span class="${cls}">${esc(line)}</span>`;
  }).join('\n')}</pre>`;
}
function diffStats(diff) {
  let add = 0, del = 0;
  for (const line of String(diff || '').split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) add++;
    else if (line.startsWith('-') && !line.startsWith('---')) del++;
  }
  return {add, del};
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
  tasksFilter: 'active', search: {query: '', results: null, agent: ''}, gallery: {agent: '', at: 0, data: null, tried: 0}, goalForm: false,
  rpTab: store('jarvis.hub.rpTab') || 'progress', changesSel: '', previewKey: '', previewDevice: store('jarvis.hub.previewDevice') || 'desktop',
  editing: null, personalDraft: null, scrollTo: ''};
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
    loadThreads(force);
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

// ---------- chrome: sidebar, header and the workspace panel ----------
function setHtml(el, html) { if (el && el.dataset.content !== html) { el.innerHTML = html; el.dataset.content = html; updateRelative(); } }
// Like setHtml, but folders and disclosures the operator opened stay open across updates.
function setKeptHtml(el, html) {
  if (!el || el.dataset.content === html) return;
  const open = new Map([...el.querySelectorAll('details[data-key]')].map(d => [d.dataset.key, d.open]));
  const scroll = el.scrollTop;
  el.innerHTML = html; el.dataset.content = html; updateRelative();
  el.querySelectorAll('details[data-key]').forEach(d => { if (open.has(d.dataset.key)) d.open = open.get(d.dataset.key); });
  el.scrollTop = scroll;
}
const selected = () => overview?.agents.find(a => a.agent_id === selectedId) || null;
function spaceHref(space, id = selectedId) {
  if (!id) return '#/overview';
  // Route values can originate in browser storage or the URL. Encode each path
  // segment before it reaches links in the sidebar or composer menu.
  const agent = encodeURIComponent(String(id));
  const section = encodeURIComponent(String(space));
  return space === 'chat' ? `#/agents/${agent}` : `#/agents/${agent}/${section}`;
}
function currentSpace() {
  if (route.name === 'agent') return route.tab === 'work' ? 'chat' : route.tab;
  return route.name;
}
function renderChrome() {
  const a = selected(), space = currentSpace();
  $('#provider-chips').innerHTML = Object.entries(overview.providers).map(([name, p]) => {
    const ok = p.installed && p.authenticated;
    // OpenRouter is optional: only flag a missing key when an agent actually uses it.
    if (name === 'openrouter' && !(overview.agents || []).some(x => x.provider === 'openrouter' && !x.archived)) return '';
    return ok ? '' : `<a href="#/settings" class="chip bad" title="${esc(p.detail)}">${esc(name === 'openrouter' ? 'OpenRouter key missing' : `${providerName(name).replace(' CLI', '')} not signed in`)}</a>`;
  }).join('');
  setHtml($('#agent-picker'), a
    ? `${avatar(a, 'sm', true)}<span class="picker-text"><b>${esc(a.name)}</b></span>${ICONS.chevronDown}`
    : `<span class="picker-text"><b>No agent yet</b></span>${ICONS.chevronDown}`);
  $('#agent-picker').title = a ? `${a.name} · ${a.status.label} · ${a.model}` : 'Choose an agent';
  renderSidebar(a, space);
  renderMainTitle(a);
  renderDetails();
}
function workspaceInfo(a) {
  // Folder name, Git branch and dirty flag when the backend reports them; nothing is guessed.
  const data = agentData && agentData.agent_id === a.agent_id ? agentData : pageData && pageData.agent_id === a.agent_id ? pageData : a;
  const w = data.workspace || a.workspace || {};
  return {folder: w.folder || a.project_name, branch: w.git_branch || data.git_branch || null, dirty: w.git_dirty ?? data.git_dirty ?? null};
}
function renderMainTitle(a) {
  const names = {overview: 'All agents', settings: 'Settings', projects: 'Project folders', activity: 'Activity', connections: 'Apps & connections', rooms: 'Team rooms', room: 'Team rooms'};
  let title = names[route.name] ? `<span class="head-title">${esc(names[route.name])}</span>` : '', crumbs = '';
  const onAgent = route.name === 'agent' && a;
  if (onAgent) {
    const labels = {goals: 'Goals', tasks: 'Tasks', artifacts: 'Library', search: 'Search', config: 'Agent settings', thread: 'Team thread'};
    const current = route.chat && (pageData?.chats || agentData?.chats || []).find(c => c.chat_id === route.chat);
    title = `<button type="button" class="model-switch" data-model-menu aria-haspopup="menu" aria-expanded="false" title="Change model"><span class="ms-agent">${esc(a.name)}</span><span class="ms-model">${esc(prettyModel(a.model))}</span>${ICONS.chevronDown}</button>${labels[route.tab] ? `<span class="head-sub">${esc(labels[route.tab])}</span>` : ''}${route.tab === 'work' && current ? `<span class="head-chat-wrap" id="head-chat-title"><button type="button" class="head-chat" data-rename-chat="${esc(current.chat_id)}" data-rename-where="head" title="Rename this chat">${esc(current.title)}</button></span>` : ''}`;
    const w = workspaceInfo(a), st = a.status;
    crumbs = `<span class="crumb folder" title="Project folder ${esc(w.folder)}">${ICONS.folder}<span>${esc(w.folder)}</span></span>${w.branch ? `<span class="crumb branch" title="Git branch ${esc(w.branch)}${w.dirty ? ' · uncommitted changes' : w.dirty === false ? ' · clean' : ''}">${ICONS.branch}<span>${esc(w.branch)}</span>${w.dirty ? '<i class="dirty" aria-hidden="true"></i><span class="sr-only">, uncommitted changes</span>' : ''}</span>` : ''}<span class="status-pill tone-${esc(st.tone)}" title="${esc(st.detail)}"><i></i>${esc(st.code === 'idle' ? 'Ready' : st.label)}</span>`;
  }
  // An open inline rename keeps its field; the title redraws when it closes.
  if (ui.renaming?.where !== 'head') setHtml($('#main-title'), title);
  setHtml($('#crumbs'), crumbs);
  $('#share-button').hidden = !(onAgent && route.tab === 'work' && route.chat);
  $('#head-new-chat').href = spaceHref('chat');
  const chat = onAgent && route.chat && (pageData?.chats || agentData?.chats || []).find(c => c.chat_id === route.chat);
  const roomTitle = route.name === 'room' && pageData?.room?.room_id === route.id ? pageData.room.title || pageData.room.topic : '';
  document.title = chat ? `${chat.title} · JARVIS` : onAgent ? `${a.name} · JARVIS` : roomTitle ? `${roomTitle} · JARVIS` : 'JARVIS · Agent Hub';
}
function renderSidebar(a, space) {
  const f = features(), data = agentData && agentData.agent_id === selectedId ? agentData : null;
  $('#side-new-chat').href = spaceHref('chat');
  if (!a) {
    setHtml($('#space-links'), `<a class="nav-row" href="#/overview">${ICONS.agent}<span>All agents</span></a>`);
    setHtml($('#conversation-tree'), '<p class="note side-note">Create an agent to start a conversation.</p>');
    return;
  }
  const openTasks = (data?.tasks || []).filter(t => !['COMPLETED', 'FAILED', 'CANCELLED'].includes(t.state)).length;
  const activeGoals = (data?.goals || []).filter(g => g.state === 'ACTIVE').length;
  const sp = space === 'room' ? 'rooms' : space;
  const row = (href, key, icon, label, count = '', extra = '') => `<a href="${href}" class="nav-row${key && sp === key ? ' active' : ''}${extra}">${icon}<span>${label}</span>${count !== '' && count ? `<span class="count">${count}</span>` : ''}</a>`;
  setHtml($('#space-links'), [
    `<a href="${spaceHref('chat')}" class="nav-row nav-new${space === 'chat' && !route.chat ? ' active' : ''}">${ICONS.compose}<span>New chat</span><kbd>Ctrl ⇧ O</kbd></a>`,
    `<button type="button" class="nav-row" data-search-chats>${ICONS.search}<span>Search chats</span><kbd>Ctrl K</kbd></button>`,
    f.artifacts ? row(spaceHref('artifacts'), 'artifacts', ICONS.library, 'Library') : '',
    f.goals ? row(spaceHref('goals'), 'goals', ICONS.goals, 'Goals', activeGoals || '') : '',
    row(spaceHref('tasks'), 'tasks', ICONS.tasks, 'Tasks', openTasks || ''),
    teamEnabled() ? row('#/rooms', 'rooms', ICONS.relationships, 'Team rooms', data?.team?.rooms_active || '') : '',
    row('#/connections', 'connections', ICONS.plug, 'Apps &amp; connections'),
  ].join(''));
  // Conversations, newest activity first, grouped by day; archived ones fold away.
  const q = ui.convoFilter.trim().toLowerCase();
  const chats = (data?.chats || []).filter(c => !q || String(c.title).toLowerCase().includes(q))
    .slice().sort((x, y) => (y.last_at || y.created_at) - (x.last_at || x.created_at));
  const live = chats.filter(c => !c.archived), archived = chats.filter(c => c.archived);
  const item = c => `<div class="convo" data-chat="${esc(c.chat_id)}"><a class="${route.chat === c.chat_id ? 'active' : ''}" href="#/agents/${esc(a.agent_id)}/work/${esc(c.chat_id)}" title="${esc(c.title)}">${c.active ? '<i class="live-dot" title="Working"></i>' : ''}<span>${esc(c.title)}</span></a>${f.archive ? `<button class="more" type="button" aria-label="Conversation actions" aria-haspopup="menu" data-chat-menu="${esc(c.chat_id)}">${ICONS.dots}</button>` : ''}</div>`;
  const html = teamSidebar(a) + `<div class="side-label">Chats</div>` +
    (live.length ? dayGroups(live, 'last_at').map(([label, list]) => `<div class="convo-group">${label}</div>${list.map(item).join('')}`).join('') : `<p class="note side-note">${q ? 'No matching conversations.' : 'No chats yet.'}</p>`) +
    (archived.length ? `<details class="archived-group" data-key="archived"${q ? ' open' : ''}><summary>Archived · ${archived.length}</summary>${archived.map(item).join('')}</details>` : '');
  if (ui.renaming?.where !== 'side') setKeptHtml($('#conversation-tree'), html);
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
  return `<div class="item">${taskIcon(t)}<a class="main-col plain-link" href="${href}"><div class="title">${esc(t.request?.startsWith('⏰') ? t.title : (t.title || t.request))}</div><div class="subtitle">${esc(taskSubtitle(t))}</div><div class="time">${agoTag(t.updated_at || t.created_at)}${t.archived ? ' · archived' : ''}</div></a>${f.archive ? `<button class="more" type="button" aria-label="Task actions" aria-haspopup="menu" data-task-menu="${esc(t.task_id)}">${ICONS.dots}</button>` : ''}</div>`;
}
function cadence(s) {
  if (s.kind === 'interval') return s.every_minutes % 60 === 0 ? `Every ${s.every_minutes / 60} h` : `Every ${s.every_minutes} min`;
  if (s.kind === 'daily') return `Daily at ${s.daily_at}`;
  return `Once · ${whenText(s.next_run_at)}`;
}

// ---------- workspace panel (Progress · Changes · Preview · Files · Agent) ----------
const RP_TABS = [['progress', 'Progress', 'pulse'], ['changes', 'Changes', 'diff'], ['preview', 'Preview', 'eye'], ['files', 'Files', 'folder'], ['agent', 'Agent', 'agent']];
const WIDE_PANEL = 1100;
function panelOpen() { return innerWidth > WIDE_PANEL ? !document.body.classList.contains('details-closed') : document.body.classList.contains('details-open'); }
function openPanel(tab, explicit = true) {
  if (tab) { ui.rpTab = tab; store('jarvis.hub.rpTab', tab); }
  if (innerWidth > WIDE_PANEL) document.body.classList.remove('details-closed'); else document.body.classList.add('details-open');
  if (explicit) { session('jarvis.hub.rpClosed', null); store('jarvis.hub.detailsClosed', '0'); }
  renderDetails();
}
function closePanel() {
  if (innerWidth > WIDE_PANEL) { document.body.classList.add('details-closed'); store('jarvis.hub.detailsClosed', '1'); }
  document.body.classList.remove('details-open');
  session('jarvis.hub.rpClosed', '1');
  renderDetails();
}
// New images, apps or file edits bring the panel forward on wide screens, unless the operator
// closed it during this session; on narrow screens it is an overlay and never opens by itself.
function autoOpenPanel(tab) {
  if (innerWidth <= WIDE_PANEL || session('jarvis.hub.rpClosed') === '1') return;
  if (panelOpen() && ui.rpTab === tab) return;
  openPanel(tab, false);
}
function panelConvo() {
  return route?.name === 'agent' && route.chat && pageData && pageData.agent_id === selectedId ? pageData.conversation || null : null;
}
function renderDetails() {
  const a = selected();
  $('#details-toggle')?.setAttribute('aria-expanded', String(panelOpen()));
  const changes = chatChanges();
  const working = (panelConvo()?.agent_turns || []).some(isActive);
  setHtml($('#rp-tabs'), RP_TABS.map(([k, label, icon]) => `<button type="button" role="tab" aria-selected="${ui.rpTab === k}" data-rp-tab="${k}" class="${ui.rpTab === k ? 'active' : ''}">${ICONS[icon]}<span>${label}</span>${k === 'changes' && changes?.length ? `<i class="tab-count">${changes.length}</i>` : ''}${k === 'progress' && working ? '<i class="tab-live"></i>' : ''}</button>`).join(''));
  const previewOn = ui.rpTab === 'preview';
  $('#rp-body').hidden = previewOn; $('#rp-preview').hidden = !previewOn;
  if (!a) { setHtml($('#rp-body'), '<p class="note">No agent selected.</p>'); return; }
  if (!panelOpen()) return;  // nothing to draw while it is closed; opening redraws
  if (previewOn) { renderPreviewTab(a); return; }
  const body = {progress: progressTab, changes: changesTab, files: filesTab, agent: agentTab}[ui.rpTab] || progressTab;
  setKeptHtml($('#rp-body'), body(a));
}
const rpEmpty = (icon, text) => `<div class="rp-empty">${ICONS[icon]}<p>${text}</p></div>`;
function progressTab(a) {
  const c = panelConvo();
  if (!c) {
    const t = a.current_task;
    return rpEmpty('pulse', `Open a chat to follow ${esc(a.name)}'s work step by step.`) + (t ? `<div class="pg-turn"><div class="pg-head">${taskIcon(t)}<b>${esc(t.title || 'Current task')}</b></div><p class="note">${esc(taskSubtitle(t))}</p></div>` : '');
  }
  const turns = c.agent_turns || [];
  const active = turns.filter(isActive), done = turns.filter(t => !isActive(t));
  const show = [...active, ...(done.length ? [done[done.length - 1]] : [])];
  if (!show.length) return rpEmpty('pulse', 'Nothing has run in this chat yet. Steps appear here while the agent works.');
  const source = pageData && pageData.agent_id === a.agent_id ? pageData : agentData || a;
  return show.map(t => {
    if (!isActive(t)) requestTurnEvents(t.task_id);
    const events = turnEvents(source, t).filter(e => ['tool', 'progress', 'model', 'approval', 'verify', 'recovery', 'failover', 'steering'].includes(e.kind));
    const live = isActive(t), running = t.state === 'RUNNING';
    const toolCount = Math.max(events.filter(e => e.kind === 'tool').length, t.tool_calls || 0);
    const items = events.slice(-40).map((e, i, list) => {
      const info = e.kind === 'tool' ? toolInfo(e) : null;
      const current = running && i === list.length - 1 && e.kind !== 'tool';
      const text = info ? `${info.verb} ${info.object}` : stepText(e);
      return `<li class="${current ? 'current' : info && !info.ok ? 'fail' : 'done'}"><span class="ck"></span><span>${esc(text)}</span></li>`;
    });
    if (running && !items.length) items.push('<li class="current"><span class="ck"></span><span>Starting…</span></li>');
    if (!live && t.state === 'COMPLETED') items.push('<li class="done"><span class="ck"></span><span>Answer written</span></li>');
    const secs = t.finished_at && (t.started_at || t.created_at) ? t.finished_at - (t.started_at || t.created_at) : 0;
    const meta = live ? `${running ? 'Working' : t.state === 'QUEUED' ? 'Queued' : esc(stateLabel(t.state))} · ${durTag(t.started_at || t.created_at)}` : `${esc(stateLabel(t.state))}${secs ? ` · worked for ${dur(secs)}` : ''}`;
    const buttons = [live && t.actions?.includes('cancel') ? `<button type="button" data-chat-cancel="${esc(t.task_id)}">${ICONS.stopSq}Stop</button>` : '',
      t.actions?.includes('resume') ? `<button type="button" class="primary" data-chat-resume="${esc(t.task_id)}">Continue</button>` : ''].join('');
    return `<section class="pg-turn${live ? ' live' : ''}"><div class="pg-head"><span class="pg-dot ${live ? 'live' : t.state === 'COMPLETED' ? 'ok' : 'bad'}"></span><b title="${esc(t.request)}">${esc(plain(t.title || t.request, 90))}</b></div>
      <div class="pg-meta">${meta} · ${toolCount} tool call${toolCount === 1 ? '' : 's'}</div>
      ${t.state === 'WAITING_APPROVAL' && t.approval ? approvalBox(t.task_id, t.approval, 'This action') : ''}
      <ol class="checklist">${items.join('')}</ol>${buttons ? `<div class="row gap-top">${buttons}</div>` : ''}</section>`;
  }).join('');
}
function chatChanges() {
  const c = panelConvo();
  if (!c) return null;
  const byPath = new Map();
  const turns = c.agent_turns || [];
  for (const t of turns) for (const x of t.artifacts || []) {
    const entry = byPath.get(x.path) || {path: x.path, versions: []};
    entry.versions.push(x); entry.latest = x; byPath.set(x.path, entry);
  }
  // A running turn's writes are recorded as artifacts when its run finishes; list them meanwhile.
  const source = pageData && pageData.agent_id === selectedId ? pageData : agentData;
  for (const t of turns.filter(isActive)) for (const e of source ? turnEvents(source, t) : []) {
    const path = e.kind === 'tool' && e.detail?.ok !== false ? e.detail?.path : '';
    if (path && !byPath.has(path)) byPath.set(path, {path, versions: [], pending: true, tool: e.detail.tool});
  }
  return [...byPath.values()];
}
const diffCache = new Map(), diffLoading = new Set();
function queueDiff(id) {
  if (!id || diffCache.has(id) || diffLoading.has(id)) return;
  diffLoading.add(id);
  api(`/api/artifacts/${encodeURIComponent(id)}/diff`).then(d => diffCache.set(id, String(d.diff || '')))
    .catch(() => diffCache.set(id, '')).finally(() => { diffLoading.delete(id); renderDetails(); repaintSoon(); });
}
function changeStats(entry) {
  if (entry.pending) return {add: 0, del: 0, known: true, status: 'P'};
  let add = 0, del = 0, known = true;
  for (const v of entry.versions) {
    if (!v.has_diff) continue;
    if (!diffCache.has(v.artifact_id)) { known = false; queueDiff(v.artifact_id); continue; }
    const s = diffStats(diffCache.get(v.artifact_id)); add += s.add; del += s.del;
  }
  const first = entry.versions[0].change;
  const status = entry.latest.change === 'deleted' ? 'D' : first === 'created' ? 'A' : 'M';
  return {add, del, known, status};
}
function changesTab(a) {
  const files = chatChanges();
  if (!files) return rpEmpty('diff', 'Open a chat to review the files it created or changed.');
  if (!files.length) return rpEmpty('diff', `No file changes in this chat yet. When ${esc(a.name)} writes or edits files, they are listed here with their diffs.`);
  const stats = new Map(files.map(f => [f.path, changeStats(f)]));
  const totals = [...stats.values()].reduce((s, x) => ({add: s.add + x.add, del: s.del + x.del}), {add: 0, del: 0});
  const sel = files.find(f => f.path === ui.changesSel) || files[files.length - 1];
  const list = files.map(f => {
    const s = stats.get(f.path), dir = f.path.includes('/') ? f.path.slice(0, f.path.lastIndexOf('/')) : '';
    return `<button type="button" class="cf-row${f === sel ? ' active' : ''}" data-change-file="${esc(f.path)}" title="${esc(f.path)}"><span class="cf-status s-${s.status}" aria-label="${{A: 'Added', M: 'Modified', D: 'Deleted', P: 'Being written'}[s.status]}">${s.status === 'P' ? '•' : s.status}</span><span class="cf-name"><b>${esc(baseName(f.path))}</b>${dir ? `<small>${esc(dir)}</small>` : ''}</span><span class="cf-stat">${s.add ? `<i class="add">+${s.add}</i>` : ''}${s.del ? `<i class="del">−${s.del}</i>` : ''}${!s.known ? '<i class="muted">…</i>' : ''}</span></button>`;
  }).join('');
  const s = stats.get(sel.path);
  if (sel.pending) return `<div class="cf-summary"><b>${files.length} file${files.length === 1 ? '' : 's'} changed</b></div><div class="changes"><div class="cf-list" role="listbox" aria-label="Changed files">${list}</div><div class="cf-review"><div class="cf-head"><span class="cf-status s-P">•</span><span class="mono cf-path">${esc(sel.path)}</span></div><p class="note">Being written by the running turn. The diff appears here when the run finishes.</p></div></div>`;
  const diffs = sel.versions.filter(v => v.has_diff).map(v => diffCache.has(v.artifact_id) ? (diffCache.get(v.artifact_id) ? diffHtml(diffCache.get(v.artifact_id)) : '<p class="note">Diff unavailable.</p>') : '<p class="note">Loading diff…</p>').join('');
  const review = `<div class="cf-review"><div class="cf-head"><span class="cf-status s-${s.status}">${s.status}</span><span class="mono cf-path">${esc(sel.path)}</span><span class="spacer"></span>${sel.latest.change !== 'deleted' ? `<button type="button" class="small" data-artifact="${esc(sel.latest.artifact_id)}">Open</button>` : ''}</div>
    ${diffs || `<p class="note">${sel.latest.change === 'created' ? `New file · ${esc(fmtSize(sel.latest.size || 0))}. No diff was recorded; open it to see the content.` : 'No diff recorded for this change.'}</p>`}</div>`;
  return `<div class="cf-summary"><b>${files.length} file${files.length === 1 ? '' : 's'} changed</b><span class="cf-stat">${totals.add ? `<i class="add">+${totals.add}</i>` : ''}${totals.del ? `<i class="del">−${totals.del}</i>` : ''}</span></div>
    <div class="changes"><div class="cf-list" role="listbox" aria-label="Changed files">${list}</div>${review}</div>`;
}
let galleryBusy = false;
function refreshGallery(agentId) {
  const g = ui.gallery;
  if (galleryBusy || !features().artifacts || Date.now() - g.tried < 10000) return;
  if (g.agent === agentId && g.data && Date.now() - g.at < 10000) return;
  galleryBusy = true; g.tried = Date.now();
  gallery(agentId, false).then(() => renderDetails()).catch(() => { /* the tree falls back to recent files */ }).finally(() => { galleryBusy = false; });
}
function pathTree(items) {
  const root = {dirs: new Map(), files: []};
  for (const x of items) {
    const parts = x.path.split('/'); let node = root;
    for (const p of parts.slice(0, -1)) { if (!node.dirs.has(p)) node.dirs.set(p, {dirs: new Map(), files: []}); node = node.dirs.get(p); }
    node.files.push(x);
  }
  return root;
}
function treeHtml(node, prefix = '') {
  return [...node.dirs].sort(([x], [y]) => x.localeCompare(y)).map(([name, child]) => `<details class="ft-dir" data-key="ft:${esc(prefix + name)}"><summary>${ICONS.folder}<span>${esc(name)}</span></summary><div class="ft-children">${treeHtml(child, `${prefix}${name}/`)}</div></details>`).join('')
    + node.files.sort((x, y) => x.path.localeCompare(y.path)).map(x => `<button type="button" class="ft-file" data-open-path="${esc(x.path)}" title="${esc(x.path)}">${ICONS.file}<span>${esc(baseName(x.path))}</span></button>`).join('');
}
function filesTab(a) {
  const f = features(), data = agentData && agentData.agent_id === a.agent_id ? agentData : null;
  refreshGallery(a.agent_id);
  const g = ui.gallery.agent === a.agent_id ? ui.gallery.data : null;
  const seen = new Set(), recent = [];
  for (const x of data?.artifacts || []) { if (x.change !== 'deleted' && !seen.has(x.path)) { seen.add(x.path); recent.push(x); } if (recent.length >= 6) break; }
  const tree = `<div id="project-tree"><details class="project-folder" data-key="pf" open><summary>▱ ${esc(a.project_name)}</summary>${recent.map(x => `<button type="button" class="ft-file" data-artifact="${esc(x.artifact_id)}" title="${esc(x.path)}">${ICONS.file}<span>${esc(baseName(x.path))}</span></button>`).join('') || '<p class="note">Nothing made yet.</p>'}${f.artifacts && recent.length ? `<a class="ft-more" href="${spaceHref('artifacts')}">All files →</a>` : ''}</details></div>`;
  const all = new Map();
  for (const x of [...(g?.made || []), ...(g?.folder || [])]) if (!all.has(x.path)) all.set(x.path, x);
  const uploads = [...all.values()].filter(x => /^uploads\//.test(x.path)).sort((x, y) => (y.created_at || 0) - (x.created_at || 0));
  return `<div class="rp-section"><div class="rp-label">Recent</div>${tree}</div>
    ${uploads.length ? `<div class="rp-section"><div class="rp-label">Uploads · ${uploads.length}</div>${uploads.slice(0, 30).map(x => `<button type="button" class="ft-file" data-open-path="${esc(x.path)}" title="${esc(x.path)}">${ICONS.clip}<span>${esc(baseName(x.path))}</span><small>${esc(fmtSize(x.size || 0))}</small></button>`).join('')}</div>` : ''}
    <div class="rp-section"><div class="rp-label">Project folder${all.size ? ` · ${all.size}` : ''}</div>${g ? (all.size ? `<div class="file-tree">${treeHtml(pathTree([...all.values()]))}</div>` : '<p class="note">The folder is empty.</p>') : `<p class="note">${f.artifacts ? 'Loading the folder…' : 'Folder listing is not available.'}</p>`}</div>`;
}
function agentTab(a) {
  const data = agentData && agentData.agent_id === selectedId ? agentData : null;
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
    body = jobs.length ? `<div class="list">${jobs.map(s => `<div class="item"><span class="state-icon ${s.enabled ? 'live' : ''}">⏰</span><div class="main-col"><div class="title">${esc(s.name)}</div><div class="subtitle">${esc(cadence(s))}${s.notify_when ? ' · reports only when something changes' : ''}</div><div class="time">${s.enabled ? (s.next_run_at ? `Next ${esc(whenText(s.next_run_at))}` : '') : 'Paused'}</div></div><button class="more" type="button" aria-label="Job actions" aria-haspopup="menu" data-schedule-menu="${esc(s.schedule_id)}">${ICONS.dots}</button></div>`).join('')}</div>`
      : `<p class="note">No scheduled jobs. Ask ${esc(a.name)} to check in on something, or to watch for something and tell you.</p>`;
  } else {
    body = `<div class="feed">${(data?.events || []).slice().reverse().slice(0, 30).map(e => eventRow(e, false)).join('') || '<p class="note">No activity yet.</p>'}</div>`;
  }
  const st = a.status;
  const u = pageData && pageData.agent_id === a.agent_id ? (pageData.conversation?.usage || pageData.usage) : null;
  return `<div class="profile">${avatar(a, 'lg')}<h2>${esc(a.name)}</h2><div class="state-line ${esc(st.tone)}"><i></i>${esc(st.code === 'idle' ? 'Ready' : st.label)}</div>${a.role ? `<p class="note">${esc(a.role)}</p>` : ''}</div>
    ${f.usage && u ? `<div class="usage-inline">${usagePanel(a, u)}</div>` : ''}
    <div class="icon-tabs" role="tablist">${tabs.map(([k, label, icon]) => `<button type="button" role="tab" title="${label}" aria-label="${label}" data-details-tab="${k}" class="${ui.detailsTab === k ? 'active' : ''}">${icon}</button>`).join('')}</div>${body}`;
}
// Preview: a live canvas for the running app, generated images and documents. The app frame is a
// persistent element (never rebuilt by polling), so a running game keeps its state.
function previewCandidates(a) {
  const c = panelConvo(), out = [], seen = new Set();
  const push = item => { if (!seen.has(item.key)) { seen.add(item.key); out.push(item); } };
  const turns = (c?.agent_turns || []).slice().reverse();
  const apps = turns.flatMap(t => t.previews || []);
  if (appPanel.id && appPanel.previews.has(appPanel.id)) apps.unshift(appPanel.previews.get(appPanel.id));
  for (const p of apps) push({key: `app:${p.preview_id}`, type: 'app', label: p.title || 'App', preview: p});
  for (const t of turns) for (const img of (t.images || []).slice().reverse()) { const src = imageSrc(img); if (src) push({key: `img:${src}`, type: 'image', label: baseName(img.path) || 'Image', src, artifact_id: img.artifact_id}); }
  for (const t of turns) for (const x of (t.artifacts || []).slice().reverse()) {
    if (x.change === 'deleted') continue;
    if (/^image\/(png|jpeg|gif|webp)$/.test(x.mime || '')) push({key: `img:/api/artifacts/${x.artifact_id}/content`, type: 'image', label: baseName(x.path), src: `/api/artifacts/${encodeURIComponent(x.artifact_id)}/content`, artifact_id: x.artifact_id});
    else if (/\.(md|markdown|txt|html?|css|js|ts|py|json|csv|ya?ml|toml|sql|sh|ps1|xml|svg)$/i.test(x.path)) push({key: `art:${x.path}`, type: 'doc', label: baseName(x.path), artifact: x});
  }
  return out.slice(0, 16);
}
const previewText = new Map();
function renderPreviewTab(a) {
  const items = previewCandidates(a);
  let current = items.find(x => x.key === ui.previewKey);
  if (!current) current = items.find(x => x.type === 'app' && x.preview.preview_id === appPanel.id) || items.find(x => x.type !== 'app') || items.find(x => x.type === 'app' && !dismissedApps.has(x.preview.preview_id));
  const isApp = current?.type === 'app';
  setHtml($('#rp-preview-bar'), items.length ? `<div class="pv-pick" role="tablist" aria-label="Preview">${items.map(x => `<button type="button" role="tab" aria-selected="${x === current}" class="pv-chip${x === current ? ' active' : ''}" data-preview-pick="${esc(x.key)}" title="${esc(x.label)}">${ICONS[x.type === 'app' ? 'browser' : x.type === 'image' ? 'image' : 'file']}<span>${esc(x.label)}</span></button>`).join('')}</div>
    ${isApp ? `<div class="pv-devices" role="group" aria-label="Preview width">${[['desktop', 'Desktop'], ['tablet', 'Tablet'], ['phone', 'Phone']].map(([k, l]) => `<button type="button" class="seg${ui.previewDevice === k ? ' active' : ''}" aria-pressed="${ui.previewDevice === k}" data-preview-device="${k}">${l}</button>`).join('')}</div>` : ''}` : '');
  const frame = $('#app-panel-frame');
  frame.classList.remove('dev-desktop', 'dev-tablet', 'dev-phone'); frame.classList.add(`dev-${ui.previewDevice}`);
  if (isApp) {
    if (appPanel.id !== current.preview.preview_id) { appPanel.id = null; showApp(current.preview, false); }
    $('#app-panel').hidden = false; $('#rp-preview-content').hidden = true;
    return;
  }
  $('#app-panel').hidden = true; $('#rp-preview-content').hidden = false;
  if (!current) { setHtml($('#rp-preview-content'), rpEmpty('eye', `Nothing to preview yet. Apps ${esc(a.name)} builds, images it makes and documents it writes open here.`)); return; }
  if (current.type === 'image') {
    const blob = turnImageUrls.get(current.src);
    if (!blob) loadImage(current.src);
    setHtml($('#rp-preview-content'), `<div class="pv-canvas">${blob && blob !== 'error' ? `<img src="${esc(blob)}" alt="${esc(current.label)}">` : `<p class="note">${blob === 'error' ? 'Could not load this image.' : 'Loading…'}</p>`}</div><div class="pv-actions">${current.artifact_id ? `<button type="button" class="small" data-artifact="${esc(current.artifact_id)}">Open in viewer</button>` : ''}</div>`);
    return;
  }
  const x = current.artifact, cached = previewText.get(x.artifact_id);
  if (cached === undefined) {
    previewText.set(x.artifact_id, null);
    apiBlob(`/api/artifacts/${encodeURIComponent(x.artifact_id)}/content`).then(b => b.text()).then(t => previewText.set(x.artifact_id, t.slice(0, 200000)))
      .catch(() => previewText.set(x.artifact_id, false)).finally(() => renderDetails());
  }
  const ext = (x.path.match(/\.([a-z0-9]+)$/i) || [])[1]?.toLowerCase() || '';
  let body = '<p class="note">Loading…</p>';
  if (cached === false) body = '<p class="note">No stored content for this version.</p>';
  else if (typeof cached === 'string') {
    if (ext === 'md' || ext === 'markdown') body = `<div class="result pv-doc">${md(cached)}</div>`;
    else if (ext === 'csv') body = `<div class="result pv-doc">${mdTable(cached.split(/\r?\n/).filter(Boolean).slice(0, 200).map(r => `|${esc(r).split(',').join('|')}|`).reduce((rows, r, i) => i === 1 ? [...rows, '|---|', r] : [...rows, r], []))}</div>`;
    else if (ext === 'txt') body = `<pre class="pv-text">${esc(cached)}</pre>`;
    else body = `<div class="result pv-doc">${codeBlock(`${ext === 'htm' ? 'html' : ext}\n${cached}`)}</div>${/^(html?|svg)$/.test(ext) ? '<p class="note">Shown as source: the Hub never runs pages an agent wrote. Ask the agent to open it as an app to use it.</p>' : ''}`;
  }
  setHtml($('#rp-preview-content'), `<div class="pv-doc-head"><span class="mono">${esc(x.path)}</span><span class="spacer"></span><button type="button" class="small" data-artifact="${esc(x.artifact_id)}">Open in viewer</button></div>${body}`);
}

// ---------- agent teams: agent-to-agent threads and team rooms ----------
const team = {threads: new Map(), at: 0, missingUntil: 0, loading: false};
function teamEnabled() {
  return !!overview?.permissions?.labels?.team || !!(agentData && agentData.agent_id === selectedId && agentData.team);
}
// An agent by id for avatars and names; one the overview no longer lists still gets a stable look.
function agentRef(id, name = '') {
  const known = overview?.agents.find(a => a.agent_id === id);
  return known ? (name && known.name !== name ? {...known, name} : known) : {agent_id: id || name || '?', name: name || id || 'Agent'};
}
// One colour per member, the same as its avatar.
const memberClass = id => `h${Math.floor(hue(id) / 30)}`;
const ROOM_STATES = {running: ['live', 'Running'], done: ['ok', 'Done'], stopped: ['', 'Stopped'], stalled: ['attention', 'Stalled'], interrupted: ['attention', 'Interrupted']};
function stateChip(state) {
  const [tone, label] = ROOM_STATES[state] || ['', stateLabel(state)];
  return `<span class="state-chip st-${esc(state)}${tone ? ` ${tone}` : ''}"><i aria-hidden="true"></i>${esc(label)}</span>`;
}
function loadThreads(force = false) {
  const id = selectedId;
  if (!id || !teamEnabled() || team.loading || Date.now() < team.missingUntil) return;
  if (!force && Date.now() - team.at < 1400) return;
  team.loading = true; team.at = Date.now();
  api(`/api/agents/${encodeURIComponent(id)}/threads`).then(data => {
    team.threads.set(id, Array.isArray(data) ? data : data?.threads || []);
    if (id === selectedId && overview) renderSidebar(selected(), currentSpace());
  }).catch(error => { if (/not found|404/i.test(error.message)) team.missingUntil = Date.now() + 30000; })
    .finally(() => { team.loading = false; });
}
function teamSidebar(a) {
  const threads = (team.threads.get(a.agent_id) || []).slice().sort((x, y) => (y.last_at || 0) - (x.last_at || 0));
  if (!threads.length) return '';
  return `<div class="side-label">Team</div>${threads.map(t => {
    const peer = agentRef(t.peer?.agent_id, t.peer?.name);
    return `<a class="team-row${route.thread === t.thread_id ? ' active' : ''}" href="#/agents/${esc(a.agent_id)}/thread/${esc(t.thread_id)}" title="${esc(a.name)} and ${esc(peer.name)}">${avatar(peer, 'sm')}<span class="tr-main"><b>${esc(peer.name)}</b><small>${esc(plain(t.last_preview || '', 90) || `${Number(t.count || 0)} messages`)}</small></span>${t.active ? '<i class="live-dot" title="Working"></i>' : ''}</a>`;
  }).join('')}`;
}
function agentMessage(agent, body, at, {state = '', same = false, chair = false} = {}) {
  const note = /FAILED|REFUSED|CANCELLED|STOPPED|INTERRUPTED/i.test(state || '') ? `<p class="note">${esc(stateLabel(state))}</p>` : '';
  return `<article class="chat-message agent-msg${same ? ' same' : ''}"><div class="am-avatar">${same ? '' : avatar(agent, 'sm')}</div><div class="am-main">${same ? '' : `<div class="am-head"><b class="mname ${memberClass(agent.agent_id)}">${esc(agent.name)}</b>${chair ? '<span class="chair-tag">Chair</span>' : ''}${at ? `<span class="am-time">${esc(clock(at))}</span>` : ''}</div>`}<div class="result">${md(String(body || ''))}</div>${note}</div></article>`;
}
const systemNote = (body, at) => `<div class="team-system" role="note">${ICONS.shield}<span>${esc(plain(body, 600))}</span>${at ? `<span class="am-time">${esc(clock(at))}</span>` : ''}</div>`;
function speakingRow(agent, verb = 'is speaking') {
  return `<div class="chat-message agent-msg speaking"><div class="am-avatar">${avatar(agent, 'sm')}</div><div class="am-main"><div class="thinking-line" role="status"><span class="spinner" aria-hidden="true"></span><span class="shimmer">${esc(agent.name)} ${verb}…</span></div></div></div>`;
}
// A read-only two-party conversation between agents (one asked the other with ask_agent).
function threadPage(a) {
  const th = a.thread;
  if (!th || th.error) return `<div class="page"><div class="empty">${esc(th?.error || 'This thread could not be loaded.')}</div></div>`;
  const A = agentRef(th.a?.agent_id, th.a?.name), B = agentRef(th.b?.agent_id, th.b?.name);
  const party = id => id === A.agent_id ? A : id === B.agent_id ? B : agentRef(id);
  const msgs = th.messages || [];
  // Whoever owes the next word: the agent with a pending approval, else the other side of the
  // last agent message (Hub notes do not count).
  const last = msgs.filter(m => m.sender_agent_id && m.kind !== 'system').pop();
  const replier = th.approval?.agent_id ? party(th.approval.agent_id) : last && last.sender_agent_id === A.agent_id ? B : last ? A : B;
  const body = msgs.map((m, i) => m.kind === 'system' || !m.sender_agent_id ? systemNote(m.body, m.at)
    : agentMessage(party(m.sender_agent_id), m.body, m.at, {state: m.state, same: msgs[i - 1]?.sender_agent_id === m.sender_agent_id && msgs[i - 1]?.kind !== 'system'})).join('');
  // The approval belongs to the asking task; deciding it lets the ask continue.
  const approval = th.approval ? approvalBox(th.approval.task_id || th.task_id || '', th.approval, th.approval.agent_name || replier.name) : '';
  return `<div class="chat-workspace team-view thread-view">
    <header class="team-head"><div class="th-parties">${avatar(A, 'sm')}<b class="mname ${memberClass(A.agent_id)}">${esc(A.name)}</b><span class="th-arrow" aria-label="and">⇄</span>${avatar(B, 'sm')}<b class="mname ${memberClass(B.agent_id)}">${esc(B.name)}</b></div>
      <span class="spacer"></span>${th.active ? `${stateChip('running')}<button type="button" class="small" data-thread-stop="${esc(th.thread_id)}">${ICONS.stopSq}<span>Stop</span></button>` : `${th.closed ? `<span class="state-chip" title="A newer thread between these agents replaced this one"><i></i>Closed</span>` : ''}<span class="muted fine">${msgs.length} message${msgs.length === 1 ? '' : 's'}</span>`}</header>
    <p class="note team-note">A conversation between your agents. Each one answers with its own abilities, model and memory. You can read it, and stop an ask that is in flight.</p>
    <div class="chat-thread team-thread">${body || '<p class="note">No messages yet.</p>'}${approval}${th.active ? speakingRow(replier, 'is replying') : ''}</div>
    <p class="readonly-foot">Read-only. Agents write here when one asks another.</p></div>`;
}
function roomsPage(rooms) {
  const list = (rooms || []).slice().sort((x, y) => (y.last_at || y.created_at || 0) - (x.last_at || x.created_at || 0));
  return `<div class="page"><div class="page-head"><h1>Team rooms</h1><span class="spacer"></span><button type="button" class="primary" data-new-room>New room</button>
      <p class="sub">Group chats between your agents on a topic. The chair speaks last in each round and closes with a summary. You can join in, or stop a room, at any time.</p></div>
    ${list.length ? `<div class="list rooms-list">${list.map(roomRow).join('')}</div>` : '<div class="empty">No team rooms yet. Start one to have two or more agents discuss a topic.</div>'}</div>`;
}
function roomRow(r) {
  const members = r.members || [], chair = members.find(m => m.agent_id === r.chair_id);
  const detail = r.summary ? plain(r.summary, 110) : r.title && r.topic ? plain(r.topic, 110) : '';
  return `<a class="item room-row" href="#/rooms/${esc(r.room_id)}"><span class="avatar-stack">${members.slice(0, 4).map(m => avatar(agentRef(m.agent_id, m.name), 'sm')).join('')}</span><div class="main-col"><div class="title">${esc(r.title || r.topic || 'Team room')}</div><div class="subtitle">${esc(members.map(m => m.name).join(', '))}${chair ? ` · chaired by ${esc(chair.name)}` : ''}${detail ? ` · ${esc(detail)}` : ''}</div><div class="time">${agoTag(r.last_at || r.created_at)}</div></div>${stateChip(r.state)}</a>`;
}
function roomPage(r) {
  const byId = new Map((r.members || []).map(m => [m.agent_id, m]));
  const chair = byId.get(r.chair_id), running = r.state === 'running';
  let round = null, prev = '';
  const messages = (r.messages || []).map(m => {
    let html = '';
    if (m.round != null && m.round !== round) { round = m.round; prev = ''; html += `<div class="round-sep" role="separator"><span>Round ${Number(m.round)}</span></div>`; }
    const sender = m.sender || {};
    if (sender.kind === 'operator') { prev = 'operator'; return `${html}<article class="chat-message user-message room-op"><div class="op-name">You</div><div class="bubble">${esc(m.body)}</div></article>`; }
    if (sender.kind === 'system') { prev = 'system'; return html + systemNote(m.body, m.at); }
    const agent = agentRef(sender.agent_id, sender.name || byId.get(sender.agent_id)?.name);
    html += agentMessage(agent, m.body, m.at, {same: prev === sender.agent_id, chair: sender.agent_id === r.chair_id});
    prev = sender.agent_id;
    return html;
  }).join('');
  const speaker = running && r.speaking ? agentRef(r.speaking, byId.get(r.speaking)?.name) : null;
  const summary = r.summary ? `<section class="summary-card" aria-label="Chair's summary"><div class="sc-head">${ICONS.check}<b>Chair's summary</b>${chair ? `<span class="muted">· ${esc(chair.name)}</span>` : ''}</div><div class="result">${md(r.summary)}</div></section>` : '';
  const approval = r.approval ? approvalBox(r.approval.task_id || r.task_id || '', r.approval, r.approval.agent_name || agentRef(r.approval.agent_id).name) : '';
  const canPost = r.state !== 'done';
  return `<div class="chat-workspace team-view room-view">
    <header class="team-head room-head"><div class="rh-main"><h1>${esc(r.title || r.topic || 'Team room')}</h1>${r.title && r.topic ? `<p class="rh-topic">${esc(r.topic)}</p>` : ''}${r.goal ? `<p class="rh-topic"><b>Goal:</b> ${esc(r.goal)}</p>` : ''}
      <div class="member-chips">${(r.members || []).map(m => `<span class="member-chip">${avatar(agentRef(m.agent_id, m.name), 'sm')}<span class="mname ${memberClass(m.agent_id)}">${esc(m.name)}</span>${m.agent_id === r.chair_id ? '<span class="chair-tag">Chair</span>' : ''}</span>`).join('')}</div></div>
      <div class="rh-side">${stateChip(r.state)}${running ? `<button type="button" class="small" data-room-stop="${esc(r.room_id)}">${ICONS.stopSq}<span>Stop</span></button>` : ''}${['stopped', 'interrupted'].includes(r.state) ? `<button type="button" class="small primary" data-room-resume="${esc(r.room_id)}">Resume</button>` : ''}</div></header>
    <div class="chat-thread team-thread">${messages || '<p class="note">The room is starting…</p>'}${approval}${speaker ? speakingRow(speaker) : ''}${summary}</div>
    <div class="chat-composer">
      <button type="button" class="to-bottom" data-to-bottom aria-label="Scroll to bottom">${ICONS.arrowDown}</button>
      <form id="room-form" class="composer${running ? ' busy' : ''}${canPost ? '' : ' disabled'}"><div class="composer-grid"><div class="c-lead"></div>
        <div class="c-input"><label class="sr-only" for="room-request">Message the room</label><textarea id="room-request" name="body" rows="1" maxlength="8000" placeholder="${canPost ? 'Message the room' : 'This room has finished'}"${canPost ? '' : ' disabled'}></textarea></div>
        <div class="c-trail">${running ? `<button type="button" class="stop-button" data-room-stop="${esc(r.room_id)}" aria-label="Stop the room" data-tip="Stop the room">${ICONS.stopSq}</button>` : ''}<button type="submit" class="send-button" aria-label="Send to the room" data-tip="Send"${canPost ? '' : ' disabled'}>${ICONS.arrowUp}</button></div></div></form>
      <div class="composer-foot"><span class="foot-note">${canPost ? 'Your message joins the transcript before the next speaker.' : 'This room has finished. Start a new room to continue.'}</span></div>
    </div></div>`;
}
// The New room dialog lives outside the view, so polling never touches it while it is open.
const roomPick = {order: []};
function openRoomDialog(preselect = '') {
  const form = $('#room-new-form');
  form.reset(); roomPick.order = [];
  const agents = (overview?.agents || []).filter(a => !a.archived && a.lifecycle === 'RUNNING');
  $('#room-member-list').innerHTML = agents.map(a => `<label class="member-pick"><input type="checkbox" name="member" value="${esc(a.agent_id)}">${avatar(a, 'sm')}<span class="mp-text"><b>${esc(a.name)}</b><small>${esc(a.role || 'Agent')} · ${esc(prettyModel(a.model))}</small></span></label>`).join('') || '<p class="note">No enabled agents.</p>';
  const box = [...form.querySelectorAll('input[name=member]')].find(i => i.value === preselect);
  if (box) { box.checked = true; roomPick.order.push(preselect); }
  syncChair(); showRoomError('');
  $('#room-dialog').showModal();
  form.elements.topic.focus();
}
function syncChair() {
  const select = $('#room-chair'), current = select.value;
  const names = new Map((overview?.agents || []).map(a => [a.agent_id, a.name]));
  select.innerHTML = roomPick.order.length ? roomPick.order.map((id, i) => `<option value="${esc(id)}">${esc(names.get(id) || id)}${i === 0 ? ' (first picked)' : ''}</option>`).join('') : '<option value="">Pick the agents first</option>';
  if (roomPick.order.includes(current)) select.value = current;
  select.disabled = !roomPick.order.length;
}
function showRoomError(text) { const el = $('#room-error'); el.textContent = text; el.hidden = !text; }

// ---------- routing ----------
function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  if (parts[0] === 'agents' && parts[1]) return {name: 'agent', id: parts[1], tab: parts[2] || 'work', chat: parts[2] === 'work' ? parts[3] : undefined, thread: parts[2] === 'thread' ? parts[3] : undefined};
  if (parts[0] === 'rooms') return parts[1] ? {name: 'room', id: parts[1]} : {name: 'rooms'};
  if (parts[0] === 'tasks' && parts[1]) return {name: 'task', id: parts[1]};
  if (['activity', 'projects', 'settings', 'overview', 'connections'].includes(parts[0])) return {name: parts[0]};
  return {name: 'home'};
}
window.addEventListener('hashchange', () => {
  closeMenus();
  if ($('#search-dialog').open) $('#search-dialog').close();
  document.body.classList.remove('sidebar-open'); $('#sidebar-toggle').setAttribute('aria-expanded', 'false');
  if (innerWidth <= WIDE_PANEL) document.body.classList.remove('details-open');
  if (captureToken()) { signInShown = false; if ($('#pair-dialog').open) $('#pair-dialog').close(); route = parseRoute(); tick(true); return; }
  if (speech.key) { window.speechSynthesis?.cancel(); speech.key = ''; speech.utter = null; }
  if (dictation.active && dictation.hash !== location.hash) stopDictation();
  if (ui.editing && ui.editing.hash !== location.hash) ui.editing = null;
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
  if (editing && !active.closest('#chat-form, #steer-form, #room-form, .msg-editor')) return;
  const field = editing ? active.name : null;
  const selection = editing && active.tagName === 'TEXTAREA' ? [active.selectionStart, active.selectionEnd] : null;
  const scroll = view.scrollTop;
  // Like a chat app: when the reader is already at the bottom, keep new steps and drafts in view.
  const following = sameRoute && view.scrollHeight - view.scrollTop - view.clientHeight < 120;
  // Disclosures keep their open state; keyed ones by identity, the rest by position.
  const expanded = new Map();
  if (sameRoute) view.querySelectorAll('details').forEach((d, i) => expanded.set(d.dataset.key || `#${i}`, d.open));
  const pinned = !!view.querySelector('.usage-wrap.pinned');
  view.innerHTML = html; lastHtml = html; updateRelative();
  if (pinned) view.querySelector('.usage-wrap')?.classList.add('pinned');
  view.querySelectorAll('details').forEach((d, i) => { const k = d.dataset.key || `#${i}`; if (expanded.has(k)) d.open = expanded.get(k); });
  paintedRoute = location.hash;
  const saved = drafts.get(location.hash);
  if (saved) for (const el of view.querySelectorAll('#chat-form [name], #steer-form [name], #room-form [name]')) if (saved[el.name] !== undefined) el.value = saved[el.name];
  const box = $('#chat-request') || $('#room-request'); if (box) grow(box);
  const editor = $('#edit-body'); if (editor) grow(editor);
  if (field) {
    const replacement = [...view.querySelectorAll('[name]')].find(el => el.name === field);
    replacement?.focus({preventScroll: true});
    if (selection) replacement?.setSelectionRange(...selection);
  }
  if (view.querySelector('#chat-form, #room-form, .thread-view') && (following || !sameRoute)) view.scrollTop = view.scrollHeight;
  else if (sameRoute) view.scrollTop = scroll;
  updateToBottom();
  loadThumbs();
  loadTurnImages();
  if (ui.scrollTo) { const target = document.getElementById(ui.scrollTo); if (target) { target.scrollIntoView({block: 'start'}); ui.scrollTo = ''; } }
}
// Re-render the current agent view from state (after a local change such as an open editor).
function repaint() { if (route?.name === 'agent' && pageData) { lastHtml = ''; paint(renderAgent(pageData)); } }
let repaintTimer = 0;
function repaintSoon() { clearTimeout(repaintTimer); repaintTimer = setTimeout(repaint, 30); }
function grow(box) {
  const form = box.closest('.composer');
  if (form) {
    // Like ChatGPT: the pill widens into a two-row composer once the text wraps, and stays so
    // until it is cleared.
    if (!box.value) form.classList.remove('expanded');
    else if (!form.classList.contains('expanded')) { box.style.height = 'auto'; if (box.value.includes('\n') || box.scrollHeight > 52) form.classList.add('expanded'); }
  }
  box.style.height = 'auto'; box.style.height = Math.min(box.scrollHeight, form ? 208 : 360) + 'px';
}
function updateToBottom() {
  const away = !!view.querySelector('.chat-thread') && view.scrollHeight - view.scrollTop - view.clientHeight > 160;
  document.body.classList.toggle('away-from-bottom', away);
}
view.addEventListener('scroll', updateToBottom, {passive: true});
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
      if (current.tab === 'thread' && current.thread) data.thread = await api(`/api/threads/${encodeURIComponent(current.thread)}`).catch(error => ({error: error.message}));
      if (location.hash !== source) return;
      pageData = data; agentData = data;
      renderSidebar(selected(), currentSpace()); renderMainTitle(selected());
      paint(renderAgent(data));
      syncAppPanel((data.conversation?.agent_turns || []).flatMap(t => t.previews || []));
      notePanelNews(data);
      renderDetails();
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
    else if (route.name === 'settings') {
      const [audit, sessions, personalization] = await Promise.all([api('/api/audit'), api('/api/sessions').catch(() => []), api('/api/personalization').catch(() => null)]);
      pageData = {audit, sessions, personalization}; paint(renderSettings(pageData));
    }
    else if (route.name === 'connections') { pageData = await api('/api/connections'); paint(renderConnections(pageData)); }
    else if (route.name === 'rooms') {
      const source = location.hash, data = await api('/api/rooms');
      if (location.hash !== source) return;
      pageData = {rooms: Array.isArray(data) ? data : data?.rooms || []}; paint(roomsPage(pageData.rooms));
    }
    else if (route.name === 'room') {
      const source = location.hash, room = await api(`/api/rooms/${encodeURIComponent(route.id)}`);
      if (location.hash !== source) return;
      pageData = {room}; paint(roomPage(room)); renderMainTitle(selected());
    }
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
// Opens the panel on news in the open chat: new images → Preview, new file edits → Changes.
const panelSeen = new Map();
function notePanelNews(data) {
  const c = data.conversation;
  if (!c || !route.chat) return;
  const key = `${data.agent_id}/${route.chat}`, turns = c.agent_turns || [];
  const arts = new Set(turns.flatMap(t => (t.artifacts || []).map(x => x.artifact_id)));
  const imgs = new Set(turns.flatMap(t => (t.images || []).map(i => i.artifact_id || i.url)));
  const before = panelSeen.get(key);
  panelSeen.set(key, {arts, imgs});
  if (!before) return;  // the first look at a chat reports nothing as new
  if ([...imgs].some(id => !before.imgs.has(id))) { ui.previewKey = ''; autoOpenPanel('preview'); }
  else if ([...arts].some(id => !before.arts.has(id))) autoOpenPanel('changes');
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
  if (tab === 'thread') return threadPage(a);
  return agentWork(a);
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
const DONE_STATES = ['COMPLETED', 'FAILED', 'CANCELLED'];
const isActive = t => !DONE_STATES.includes(t.state);
const actBtn = (icon, attrs, label) => `<button type="button" class="act" ${attrs} aria-label="${label}" data-tip="${label}">${ICONS[icon]}</button>`;
function agentWork(a) {
  const blocked = a.archived ? 'This agent is archived.' : a.lifecycle !== 'RUNNING' ? 'Enable this agent in its settings to chat.' : '';
  const messages = a.conversation?.messages || [];
  const turns = a.conversation?.agent_turns || [];
  const byId = new Map(turns.map(t => [t.task_id, t]));
  const active = turns.filter(isActive);
  const busy = active.length > 0;
  const stopTurn = active.find(t => t.state === 'RUNNING' && t.actions?.includes('cancel')) || active.find(t => t.actions?.includes('cancel'));
  let lastReply = -1;
  messages.forEach((m, i) => { if (m.role !== 'operator') lastReply = i; });
  const empty = !messages.length;
  const composer = composerHtml(a, blocked, stopTurn, empty);
  if (empty) {
    return `<div class="chat-workspace is-empty"><div class="chat-welcome"><h1>What can I help with?</h1></div>${composer}
      ${blocked ? `<p class="note center">${esc(blocked)}</p>` : `<div class="starters">${STARTERS.map(([label, text]) => `<button type="button" data-starter="${esc(text)}">${esc(label)}</button>`).join('')}</div>`}</div>`;
  }
  const thread = messages.map((m, i) => {
    const turn = byId.get(m.task_id || String(m.message_id || '').replace(/-(assistant|user)$/, '')) || null;
    return m.role === 'operator' ? userMessage(a, m, i, busy) : assistantMessage(a, m, i, turn, i === lastReply, busy);
  }).join('');
  return `<div class="chat-workspace"><div class="chat-thread" aria-live="polite">${thread}</div>${composer}</div>`;
}
// Messages are named by message_id (<task>-user / <task>-assistant), which stays stable when a
// hidden watch turn or a new turn shifts positions; messages from before the Hub have none.
const msgKey = (m, i) => m.message_id || `#${i}`;
function findMessage(key) {
  const list = pageData?.conversation?.messages || [];
  return String(key).startsWith('#') ? list[Number(String(key).slice(1))] : list.find(m => m.message_id === key);
}
// The words as typed; older messages only have body (which also holds the attachment line).
const typedText = m => typeof m.text === 'string' ? m.text : String(m.body || '');
// The image lane marks its reply with [[jarvis-image:<path>]]; the picture itself is shown below.
const IMAGE_MARKER = /\[\[jarvis-image:([^\]\n]+)\]\]/g;
const replyText = m => String(m.body || '').replace(IMAGE_MARKER, '').replace(/[ \t]+\n/g, '\n').trim();
function userMessage(a, m, i, busy) {
  const key = msgKey(m, i);
  const editing = ui.editing && ui.editing.hash === location.hash && ui.editing.id === key;
  const files = (m.files || []).map((f, j) => fileCard(f, `data-open-file="${esc(key)}|${j}"`)).join('');
  const text = typedText(m);
  const body = editing
    ? `<div class="msg-editor"><label class="sr-only" for="edit-body">Edit your message</label><textarea id="edit-body" name="edit-body" rows="1" maxlength="20000">${esc(ui.editing.text)}</textarea><div class="editor-actions"><button type="button" data-edit-cancel>Cancel</button><button type="button" class="primary" data-edit-send>Send</button></div></div>`
    : text.trim() ? `<div class="bubble">${esc(text)}</div>` : '';
  const actions = editing ? '' : `<div class="msg-actions">${text.trim() ? actBtn('copy', `data-copy-msg="${esc(key)}"`, 'Copy') : ''}${m.task_id && !busy ? actBtn('pencil', `data-edit-msg="${esc(key)}"`, 'Edit message') : ''}</div>`;
  return `<article class="chat-message user-message${editing ? ' editing' : ''}" data-message="${esc(key)}">${files ? `<div class="msg-files">${files}</div>` : ''}${body}${actions}</article>`;
}
function assistantMessage(a, m, i, t, last, busy) {
  const key = msgKey(m, i);
  const live = !!t && isActive(t);
  const legacyWait = m.state === 'LIVE_QUEUED';
  let html = t ? activityHtml(a, t) : '';
  if (legacyWait) html += `<div class="thinking-line"><span class="spinner"></span><span class="shimmer">Thinking…</span></div>`;
  if (m.provisional) html += `<div class="result draft">${md(replyText(m))}</div><p class="note draft-note">Draft — still working. The verified answer replaces this when the run finishes.</p>`;
  else if (live && !['QUEUED', 'RUNNING'].includes(t.state) && !(t.state === 'WAITING_APPROVAL' && t.approval)) html += `<div class="turn-status"><span>${esc(t.blocker || stateLabel(t.state))}</span>${t.actions?.includes('resume') ? `<button type="button" class="small primary" data-chat-resume="${esc(t.task_id)}">Continue</button>` : ''}</div>`;
  else if (!live && !legacyWait) html += `<div class="result">${md(replyText(m))}</div>`;
  if (/FAILED|INTERRUPTED/.test(m.state) && !live) html += '<p class="note">Response interrupted or unavailable. You can send another message.</p>';
  if (t) html += turnImagesHtml(t, markedImages(a, m, t)) + appCards(t.previews || []);
  const settled = !m.provisional && !legacyWait && !live;
  if (settled) {
    const rating = m.feedback?.rating || null, speaking = speech.key === key && speech.hash === location.hash;
    // Only a Hub turn can be rated or regenerated; older messages can be copied.
    const rated = m.task_id ? `${actBtn(rating === 'up' ? 'upFill' : 'up', `data-feedback="${esc(key)}|up" aria-pressed="${rating === 'up'}"`, 'Good response')}${actBtn(rating === 'down' ? 'downFill' : 'down', `data-feedback="${esc(key)}|down" aria-pressed="${rating === 'down'}"`, 'Bad response')}${'speechSynthesis' in window ? actBtn(speaking ? 'stopSq' : 'speaker', `data-speak="${esc(key)}" aria-pressed="${speaking}"`, speaking ? 'Stop reading' : 'Read aloud') : ''}${last && !busy ? actBtn('regen', 'data-regenerate', 'Regenerate') : ''}${actBtn('dots', `data-msg-more="${esc(key)}" aria-haspopup="menu"`, 'More actions')}` : '';
    html += `<div class="msg-actions${last ? ' always' : ''}">${actBtn('copy', `data-copy-msg="${esc(key)}"`, 'Copy')}${rated}</div>`;
  }
  return `<article class="chat-message assistant-message${last ? ' last' : ''}" data-message="${esc(key)}">${html}</article>`;
}
// A marked picture that is not already among the turn's images is fetched from the project.
function markedImages(a, m, t) {
  const known = new Set((t.images || []).map(img => img.path));
  return [...String(m.body || '').matchAll(IMAGE_MARKER)].map(x => x[1].trim()).filter(p => p && !known.has(p))
    .map(p => ({path: p, url: `/api/agents/${encodeURIComponent(a.agent_id)}/files/content?path=${encodeURIComponent(p)}`}));
}
// ---------- tool activity (Claude Code-style rows) ----------
const turnEventCache = new Map();
let turnEventInflight = 0;
const turnEventQueue = [];
function requestTurnEvents(taskId) {
  if (turnEventCache.has(taskId)) return;
  turnEventCache.set(taskId, {events: [], loading: true});
  turnEventQueue.push(taskId); pumpTurnEvents();
}
function pumpTurnEvents() {
  while (turnEventInflight < 3 && turnEventQueue.length) {
    const id = turnEventQueue.shift(); turnEventInflight++;
    api(`/api/events?task=${encodeURIComponent(id)}&after=0&limit=500`).then(d => turnEventCache.set(id, {events: d.events || []}))
      .catch(() => turnEventCache.set(id, {events: [], failed: true}))
      .finally(() => { turnEventInflight--; pumpTurnEvents(); repaintSoon(); renderDetails(); });
  }
}
function turnEvents(a, t) {
  const seen = new Map();
  const add = e => { if (e && e.task_id === t.task_id) seen.set(e.seq ?? `${e.ts}|${e.summary}`, e); };
  (turnEventCache.get(t.task_id)?.events || []).forEach(add);
  (a.events || []).forEach(add);
  feed.forEach(add);
  return [...seen.values()].sort((x, y) => (x.seq ?? 0) - (y.seq ?? 0) || x.ts - y.ts);
}
const TOOL_KINDS = [[/^(read_file|read_document|list_files|list_dir|files_read|files_list|glob|stat_file|file_info|read_)/, 'read'], [/^(write_file|edit_file|apply_patch|files_write|files_edit|create_file|delete_file|move_file|rename_file|append_file|replace_in_file|copy_path|move_path|trash_path|build_document)/, 'edit'],
  [/^(search_files|grep|files_search|find_files)/, 'search'], [/^(run_process|start_process|stop_process|process_|shell|http_health|run_|git_)/, 'run'], [/^(web_search|web_research|web_fetch|fetch_url|http_get|news)/, 'web'],
  [/^(browser_|web_app_check|open_url|computer_|desktop_)/, 'browser'], [/^(remember|recall|forget_memory|memory_)/, 'memory'], [/^(create_image|generate_image|edit_attached_image|image_|create_chart|chart)/, 'image'], [/^(schedule_)/, 'schedule'], [/^(goal_)/, 'goal'], [/^(helper|spawn_|delegate)/, 'helper']];
const KIND_ROW_ICON = {team: 'relationships', read: 'file', edit: 'pencil', search: 'search', run: 'run', web: 'globe', browser: 'browser', memory: 'memory', image: 'image', schedule: 'clock', goal: 'goals', helper: 'relationships', approval: 'shield', tool: 'wrench'};
function toolInfo(e) {
  // A permission request is a step, not a failure: it carries no red badge.
  if (e.kind === 'approval') return {kind: 'approval', ok: true, verb: 'Permission:', object: String(e.summary || ''), target: '', outcome: '', name: 'approval', groupable: false};
  const helper = e.detail?.helper || '';
  let summary = String(e.summary || '');
  if (helper && summary.startsWith(`${helper} · `)) summary = summary.slice(helper.length + 3);
  const d = e.detail || {};
  const name = String(d.tool || summary.split(/\s/)[0] || 'tool');
  const arrow = summary.indexOf(' → ');
  const head = arrow >= 0 ? summary.slice(0, arrow) : summary, outcome = arrow >= 0 ? summary.slice(arrow + 3) : '';
  const ok = d.ok !== false && e.level !== 'warn' && e.level !== 'error';
  let kind = helper ? 'helper' : (TOOL_KINDS.find(([re]) => re.test(name)) || [null, 'tool'])[1];
  // The event's own argument line and written path, when recorded; the summary is the fallback.
  const args = typeof d.args === 'string' ? d.args.trim() : '';
  const parsed = head.startsWith(name) ? head.slice(name.length).trim() : head;
  const target = d.path || args || parsed;
  let verb, object = target, teamInfo = null;
  if (['ask_agent', 'list_agents', 'start_team_discussion', 'end_discussion'].includes(name)) {
    // Agents talking to each other: who was asked, what, and where the conversation lives.
    kind = 'team';
    const known = d.peer_agent_id ? overview?.agents.find(x => x.agent_id === d.peer_agent_id) : null;
    let peer = known?.name || '', message = args || parsed;
    const split = message.match(/^([^:\n]{1,60}):\s*([\s\S]+)$/);
    if (name === 'ask_agent' && split && (!peer || split[1].trim().toLowerCase() === peer.toLowerCase())) { peer = peer || split[1].trim(); message = split[2]; }
    teamInfo = {peer: peer || 'another agent', message, thread_id: d.thread_id || '', room_id: d.room_id || '', reply: d.reply_preview || '', from: e.agent_id || ''};
    verb = {ask_agent: 'Asked', list_agents: 'Listed your agents', start_team_discussion: 'Started a team discussion:', end_discussion: 'Closed the discussion'}[name];
    object = name === 'ask_agent' ? `${teamInfo.peer}: ${message}` : name === 'start_team_discussion' ? message : '';
  }
  else if (/^ran `/.test(head) || name === 'run_process') { kind = 'run'; verb = 'Ran'; object = args || (head.match(/^ran `([\s\S]*)`/) || [])[1] || parsed; }
  else if (/^started `/.test(head) || name === 'start_process') { kind = 'run'; verb = 'Started'; object = args || (head.match(/`([^`]*)`/) || [])[1] || parsed; }
  else if (/^searched “/.test(head) || name === 'web_search') { kind = 'web'; verb = 'Searched the web:'; object = args || (head.match(/“([\s\S]*)”/) || [])[1] || parsed; }
  else if (/^browser check/.test(head)) { kind = 'browser'; verb = 'Checked in the browser'; object = head.split('·')[1]?.trim() || ''; }
  else {
    verb = {read: name.startsWith('list') ? 'Listed' : 'Read', edit: name === 'write_file' || name === 'create_file' ? 'Wrote' : /^(delete|trash)/.test(name) ? 'Deleted' : name === 'copy_path' ? 'Copied' : name === 'move_path' ? 'Moved' : name === 'build_document' ? 'Built' : 'Edited', search: 'Searched files for',
      web: name === 'web_fetch' || name === 'fetch_url' ? 'Read' : 'Researched', browser: 'Used the browser:', memory: name === 'recall' ? 'Recalled' : name === 'remember' ? 'Remembered' : 'Memory:', image: d.path ? 'Made an image' : 'Image:',
      schedule: 'Scheduled', goal: 'Updated a goal', helper: `${helper} ·`, tool: name.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase())}[kind];
    if (kind === 'helper') object = summary;
    if ((kind === 'read' || kind === 'edit' || (kind === 'image' && d.path)) && target) object = baseName(target) || target;
    if (name === 'github_create_pull_request') { verb = 'Opened a pull request:'; object = args || parsed; }
    if (object === '.' || object === './') object = kind === 'read' ? 'the project folder' : '';
    if (kind === 'web' && /^https?:\/\//.test(target)) object = linkHost(target);
  }
  return {kind, name, ok, verb, object, target, outcome, ms: d.ms, team: teamInfo, path: d.path || '', artifact_id: d.artifact_id || '', has_diff: !!d.has_diff, change: d.change || '',
    groupable: ['read', 'edit', 'search', 'web'].includes(kind) && ok, group: kind === 'web' ? `web|${verb}` : kind};
}
function teamRowBody(info) {
  const x = info.team, from = x.from || selectedId, lines = [];
  if (info.name === 'ask_agent' && x.message) lines.push(`<div class="tr-line"><span class="tr-k">Asked</span><span class="tr-msg">${esc(x.message)}</span></div>`);
  if (info.name === 'start_team_discussion' && x.message) lines.push(`<div class="tr-line"><span class="tr-k">Topic</span><span class="tr-msg">${esc(x.message)}</span></div>`);
  if (x.reply) lines.push(`<div class="tr-reply"><div class="tr-k">${esc(info.name === 'ask_agent' ? `${x.peer} replied` : 'Result')}</div><div class="result">${md(x.reply)}</div></div>`);
  if (info.outcome) lines.push(`<div class="tr-line"><span class="tr-k">Result</span><span class="${info.ok ? '' : 'bad-text'}">${esc(info.outcome)}</span></div>`);
  const links = [x.thread_id ? `<a class="link-btn" href="#/agents/${esc(from)}/thread/${esc(x.thread_id)}">Open thread →</a>` : '', x.room_id ? `<a class="link-btn" href="#/rooms/${esc(x.room_id)}">Open room →</a>` : ''].join('');
  if (links) lines.push(`<div class="tr-line tr-links">${links}</div>`);
  return lines.join('') || '<p class="note">No details recorded.</p>';
}
function rowBody(info, e, t) {
  if (info.team) return teamRowBody(info);
  const lines = [`<div class="tr-line"><span class="tr-k">Tool</span><code>${esc(info.name)}</code>${info.ms != null ? `<span class="muted"> · ${Number(info.ms)} ms</span>` : ''}${e.ts ? `<span class="muted"> · ${esc(clock(e.ts))}</span>` : ''}</div>`];
  if (info.target && info.kind !== 'approval') lines.push(`<div class="tr-line"><span class="tr-k">${info.kind === 'run' ? 'Command' : info.kind === 'web' ? 'Query' : 'Target'}</span>${/^https?:\/\//.test(info.target) ? LINK(esc(info.target), esc(linkHost(info.target)), true) : `<code class="wrap">${esc(info.kind === 'run' ? info.object : info.target)}</code>`}</div>`);
  if (info.outcome) lines.push(`<div class="tr-line"><span class="tr-k">Result</span><span class="${info.ok ? '' : 'bad-text'}">${esc(info.outcome)}</span></div>`);
  if (info.kind === 'approval') lines.push(`<div class="tr-line">${esc(info.object)}</div>`);
  const art = editArtifact(info, t);
  if (art) {
    if (art.has_diff) lines.push(diffCache.has(art.artifact_id) ? (diffCache.get(art.artifact_id) ? diffHtml(diffCache.get(art.artifact_id)) : '<p class="note">Diff unavailable.</p>') : `<p class="note" data-want-diff="${esc(art.artifact_id)}">Loading diff…</p>`);
    else lines.push(`<p class="note">${esc(stateLabel(art.change || 'created'))}${art.size ? ` · ${esc(fmtSize(art.size))}` : ''} · <button type="button" class="link-btn" data-artifact="${esc(art.artifact_id)}">Open</button></p>`);
  } else if (info.kind === 'edit' && info.path && t && isActive(t)) lines.push('<p class="note">The diff appears when this run finishes.</p>');
  return lines.join('');
}
// The file a row wrote: named by the event once the run has recorded it, else matched by path.
function editArtifact(info, t) {
  // A failed call wrote nothing, so it never borrows the file's diff.
  if (!['edit', 'image'].includes(info.kind) || !info.ok) return null;
  const arts = t?.artifacts || [];
  if (info.artifact_id) return arts.find(x => x.artifact_id === info.artifact_id) || {artifact_id: info.artifact_id, has_diff: info.has_diff, change: info.change, path: info.path || info.target, size: 0};
  const p = info.path || (info.kind === 'edit' ? info.target : '');
  return p ? arts.find(x => x.path === p || x.path.endsWith(`/${p}`) || p.endsWith(`/${x.path}`)) || null : null;
}
function singleRow({e, info}, t) {
  const art = editArtifact(info, t), s = art?.has_diff && diffCache.get(art.artifact_id) ? diffStats(diffCache.get(art.artifact_id)) : null;
  const text = info.team && info.name === 'ask_agent'
    ? `<span class="tr-plain">Asked</span> <span class="tr-verb">${esc(info.team.peer)}</span><span class="tr-obj">: ${esc(info.team.message)}</span>`
    : `<span class="tr-verb">${esc(info.verb)}</span> <span class="tr-obj" title="${esc(info.target)}">${esc(info.object)}</span>`;
  return `<details class="tool-row k-${info.kind}${info.ok ? '' : ' fail'}" data-key="tr-${esc(e.seq ?? e.ts)}"${art?.has_diff ? ` data-diff-id="${esc(art.artifact_id)}"` : ''}><summary><span class="tr-icon">${ICONS[KIND_ROW_ICON[info.kind]] || ICONS.wrench}</span><span class="tr-text">${text}</span>${s ? `<span class="cf-stat"><i class="add">+${s.add}</i><i class="del">−${s.del}</i></span>` : ''}${info.ok ? '' : `<span class="tr-fail">${info.kind === 'run' && /^exit/.test(info.outcome) ? esc(info.outcome) : 'failed'}</span>`}${ICONS.chevronRight}</summary><div class="tr-body">${rowBody(info, e, t)}</div></details>`;
}
function groupRow(g, t) {
  const n = g.items.length, verbs = new Set(g.items.map(x => x.info.verb));
  const label = g.kind === 'read' ? (verbs.size === 1 && verbs.has('Read') ? `Read ${n} files` : `Explored ${n} files and folders`)
    : g.kind === 'edit' ? (verbs.size === 1 && verbs.has('Edited') ? `Edited ${n} files` : `Changed ${n} files`)
    : g.kind === 'search' ? `Ran ${n} file searches` : g.kind === 'web' ? (verbs.has('Read') ? `Read ${n} web pages` : `Searched the web ${n} times`) : `${n} steps`;
  return `<details class="tool-row group k-${g.kind}" data-key="tg-${esc(g.items[0].e.seq ?? g.items[0].e.ts)}"><summary><span class="tr-icon">${ICONS[KIND_ROW_ICON[g.kind]]}</span><span class="tr-text"><span class="tr-verb">${esc(label)}</span> <span class="tr-obj">${esc(g.items.map(x => x.info.object).slice(0, 3).join(', '))}${n > 3 ? '…' : ''}</span></span>${ICONS.chevronRight}</summary><div class="tr-body nested">${g.items.map(x => singleRow(x, t)).join('')}</div></details>`;
}
function toolRows(t, events) {
  const groups = [];
  for (const e of events) {
    if (!['tool', 'approval'].includes(e.kind)) continue;
    const info = toolInfo(e), prev = groups[groups.length - 1];
    if (prev && info.groupable && prev.groupable && prev.group === info.group) prev.items.push({e, info});
    else groups.push({kind: info.kind, group: info.group, groupable: info.groupable, items: [{e, info}]});
  }
  return groups.map(g => g.items.length > 1 ? groupRow(g, t) : singleRow(g.items[0], t)).join('');
}
function thinkingLine(t) {
  const steps = t.steps || [];
  const queued = t.state === 'QUEUED';
  const current = queued ? 'Message received — queued' : steps.length ? stepText(steps[steps.length - 1]) : (t.progress || '');
  return `<details class="thinking" data-key="th-${esc(t.task_id)}"><summary><span class="spinner" aria-hidden="true"></span><span class="shimmer">${queued ? 'Queued' : 'Thinking'}…</span>${current && !/^(thinking|working)…?$/i.test(current) ? `<span class="think-now">${esc(current)}</span>` : ''}<span class="think-time">${durTag(t.started_at || t.created_at)}</span>${ICONS.chevronRight}</summary>${liveSteps(t) || '<p class="note">Starting…</p>'}</details>`;
}
function activityHtml(a, t) {
  const events = turnEvents(a, t);
  const rows = toolRows(t, events);
  if (isActive(t)) {
    const running = ['QUEUED', 'RUNNING'].includes(t.state);
    const card = t.state === 'WAITING_APPROVAL' && t.approval ? approvalBox(t.task_id, t.approval, 'This action') : '';
    return rows || card || running ? `<div class="activity live">${rows}${card}${running ? thinkingLine(t) : ''}</div>` : '';
  }
  const toolCount = Math.max(events.filter(e => e.kind === 'tool').length, t.tool_calls || 0);
  const secs = t.finished_at && (t.started_at || t.created_at) ? t.finished_at - (t.started_at || t.created_at) : 0;
  const files = t.artifacts || [];
  if (!rows && !files.length && !toolCount && secs < 20) return '';
  const cached = turnEventCache.get(t.task_id);
  const label = `${secs ? `Worked for ${dur(secs)}` : 'Worked'}${toolCount ? ` · ${toolCount} tool call${toolCount === 1 ? '' : 's'}` : ''}${files.length ? ` · ${files.length} file${files.length === 1 ? '' : 's'}` : ''}`;
  return `<details class="worked" data-key="w-${esc(t.task_id)}" data-task="${esc(t.task_id)}"><summary><span class="worked-label">${label}</span>${ICONS.chevronRight}</summary><div class="activity">${rows || `<p class="note">${cached?.loading ? 'Loading steps…' : toolCount ? 'Step details are no longer available for this turn.' : 'No tool activity recorded.'}</p>`}${files.length ? `<div class="worked-files">${artifactList(files)}</div>` : ''}</div></details>`;
}
// Opening a disclosure loads what it shows: a finished turn's full step list, or an edit's diff.
document.addEventListener('toggle', event => {
  const d = event.target;
  if (!(d instanceof HTMLDetailsElement) || !d.open) return;
  if (d.dataset.task && !turnEventCache.has(d.dataset.task)) requestTurnEvents(d.dataset.task);
  if (d.dataset.diffId) queueDiff(d.dataset.diffId);
  d.querySelectorAll('[data-want-diff]').forEach(el => queueDiff(el.dataset.wantDiff));
}, true);
// ---------- files: pending uploads, sent file cards, turn images ----------
function fileKind(name, mime = '') {
  const ext = String(name || '').split('.').pop().toLowerCase();
  if (/^image\//.test(mime) || ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'].includes(ext)) return ['image', 'Image'];
  if (ext === 'pdf') return ['pdf', 'PDF'];
  if (['doc', 'docx', 'odt', 'rtf'].includes(ext)) return ['doc', 'Document'];
  if (['xls', 'xlsx', 'csv', 'tsv', 'ods'].includes(ext)) return ['sheet', 'Spreadsheet'];
  if (['ppt', 'pptx', 'odp', 'key'].includes(ext)) return ['slides', 'Presentation'];
  if (['zip', 'tar', 'gz', 'tgz', '7z', 'rar'].includes(ext)) return ['zip', 'Archive'];
  if (['py', 'js', 'ts', 'tsx', 'jsx', 'json', 'html', 'css', 'java', 'c', 'cpp', 'cs', 'go', 'rs', 'rb', 'php', 'sh', 'ps1', 'sql', 'yaml', 'yml', 'toml', 'xml', 'kt', 'swift', 'ipynb'].includes(ext)) return ['code', 'Code'];
  if (/^audio\//.test(mime)) return ['audio', 'Audio'];
  if (/^video\//.test(mime)) return ['video', 'Video'];
  if (['txt', 'md', 'log'].includes(ext) || /^text\//.test(mime)) return ['text', ext === 'md' ? 'Markdown' : 'Text'];
  return ['file', 'File'];
}
const GALLERY_KIND = {image: 'images', pdf: 'documents', doc: 'documents', sheet: 'documents', slides: 'documents', text: 'documents', code: 'code', audio: 'audio', video: 'videos', zip: 'other', file: 'other'};
function fileCard(f, attrs, remove = '') {
  const [k, label] = fileKind(f.name, f.mime);
  const inner = `<span class="file-ico fk-${k}">${ICONS.file}</span><span class="fc-meta"><b>${esc(f.name)}</b><small>${esc(label)}${f.size ? ` · ${esc(fmtSize(f.size))}` : ''}</small></span>`;
  return remove ? `<div class="file-card pending" title="${esc(f.name)}">${inner}${remove}</div>` : `<button type="button" class="file-card" ${attrs} title="${esc(f.path || f.name)}">${inner}</button>`;
}
const pendingUploads = new Map();
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
const MAX_FILES = 10, MAX_FILE_BYTES = 20 * 1024 * 1024, MAX_IMAGES = 4, MAX_IMAGE_BYTES = 5 * 1024 * 1024, MAX_MESSAGE_BYTES = 100 * 1024 * 1024;
function attachChips() {
  return (pendingUploads.get(location.hash) || []).map((u, i) => {
    const remove = `<button type="button" class="chip-x" data-unattach="${i}" aria-label="Remove ${esc(u.name)}">${ICONS.close}</button>`;
    return u.kind === 'image' ? `<div class="img-chip" title="${esc(u.name)}"><img src="${esc(u.preview)}" alt="${esc(u.name)}">${remove}</div>` : fileCard(u, '', remove);
  }).join('');
}
function refreshAttachRow() {
  const row = $('#attach-row'); if (row) row.innerHTML = attachChips();
  $('#chat-form')?.classList.toggle('has-files', !!(pendingUploads.get(location.hash) || []).length);
  lastHtml = '';
}
const readBase64 = file => new Promise((resolve, reject) => { const r = new FileReader(); r.onload = () => resolve(String(r.result).split(',')[1] || ''); r.onerror = reject; r.readAsDataURL(file); });
// Images within the vision limits travel as `images`; everything else (any type) as `files`,
// which the backend saves in the project's uploads folder for the agent to read.
async function addFiles(fileList) {
  const hash = location.hash, list = pendingUploads.get(hash) || [];
  for (const file of fileList) {
    const images = list.filter(u => u.kind === 'image').length, files = list.length - images;
    const name = file.name || `pasted-${list.length + 1}${file.type === 'image/png' ? '.png' : ''}`;
    const asImage = IMAGE_TYPES.includes(file.type) && file.size <= MAX_IMAGE_BYTES && images < MAX_IMAGES;
    if (!asImage) {
      if (files >= MAX_FILES) { toast(`Up to ${MAX_FILES} files per message.`, true); break; }
      if (file.size > MAX_FILE_BYTES) { toast(`${name} is over 20 MB.`, true); continue; }
    }
    if (list.reduce((sum, u) => sum + (u.size || 0), 0) + file.size > MAX_MESSAGE_BYTES) { toast(`${name} would take this message over 100 MB of attachments.`, true); continue; }
    try {
      const data = await readBase64(file);
      list.push({kind: asImage ? 'image' : 'file', name, mime: file.type || 'application/octet-stream', size: file.size, data, preview: asImage ? URL.createObjectURL(file) : ''});
    } catch { toast(`Could not read ${name}.`, true); }
  }
  pendingUploads.set(hash, list);
  refreshAttachRow();
}
function clearUploads(hash) {
  for (const u of pendingUploads.get(hash) || []) if (u.preview) URL.revokeObjectURL(u.preview);
  pendingUploads.delete(hash);
}
const turnImageUrls = new Map();
function imageSrc(img) {
  const url = String(img?.url || '');
  // Only same-origin API paths get the bearer token; anything else is ignored.
  if (/^\/api\/[^/]/.test(url)) return url;
  return img?.artifact_id ? `/api/artifacts/${encodeURIComponent(img.artifact_id)}/content` : '';
}
function loadImage(src) {
  if (turnImageUrls.has(src)) return;
  turnImageUrls.set(src, '');
  apiBlob(src).then(blob => { turnImageUrls.set(src, URL.createObjectURL(blob)); })
    .catch(() => turnImageUrls.set(src, 'error'))
    .finally(() => { view.querySelectorAll('[data-turn-img]').forEach(el => { if (el.dataset.turnImg === src) { const url = turnImageUrls.get(src); el.outerHTML = url && url !== 'error' ? `<img src="${esc(url)}" alt="${esc(el.dataset.alt || '')}">` : '<span class="img-slot failed">Image unavailable</span>'; } }); lastHtml = ''; renderDetails(); });
}
function loadTurnImages() { view.querySelectorAll('[data-turn-img]').forEach(el => loadImage(el.dataset.turnImg)); }
function turnImagesHtml(t, extra = []) {
  const imgs = [...(t.images || []), ...extra].map(img => ({img, src: imageSrc(img)})).filter(x => x.src);
  if (!imgs.length) return '';
  return `<div class="turn-images n${Math.min(imgs.length, 4)}">${imgs.map(({img, src}) => {
    const url = turnImageUrls.get(src), name = baseName(img.path) || 'Generated image';
    const inner = url && url !== 'error' ? `<img src="${esc(url)}" alt="${esc(name)}">` : url === 'error' ? '<span class="img-slot failed">Image unavailable</span>' : `<span class="img-slot" data-turn-img="${esc(src)}" data-alt="${esc(name)}"></span>`;
    return `<button type="button" class="turn-image" ${img.artifact_id ? `data-artifact="${esc(img.artifact_id)}"` : `data-open-image="${esc(src)}"`} aria-label="Open ${esc(name)}">${inner}</button>`;
  }).join('')}</div>`;
}
// ---------- composer ----------
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition || null;
function composerHtml(a, blocked, stopTurn, empty) {
  const pending = (pendingUploads.get(location.hash) || []).length;
  const tools = Object.keys(a.permissions || {}).filter(k => a.permissions[k]).length;
  return `<div class="chat-composer">
    <button type="button" class="to-bottom" data-to-bottom aria-label="Scroll to bottom">${ICONS.arrowDown}</button>
    <form id="chat-form" class="composer${stopTurn ? ' busy' : ''}${pending ? ' has-files' : ''}">
      <div class="attach-row" id="attach-row">${attachChips()}</div>
      <div class="composer-grid">
        <div class="c-lead"><button type="button" class="c-btn" data-composer-menu aria-haspopup="menu" aria-label="Add files and more" data-tip="Add files and more">${ICONS.plus}</button></div>
        <div class="c-input"><label class="sr-only" for="chat-request">Message ${esc(a.name)}</label><textarea id="chat-request" name="body" rows="1" maxlength="20000" placeholder="${empty ? `Message ${esc(a.name)}` : 'Ask anything'}" aria-label="Message ${esc(a.name)}"></textarea></div>
        <div class="c-trail">
          ${SpeechRec ? `<button type="button" class="c-btn mic${dictation.active ? ' listening' : ''}" data-dictate aria-pressed="${dictation.active}" aria-label="${dictation.active ? 'Stop dictation' : 'Dictate'}" data-tip="${dictation.active ? 'Stop dictation' : 'Dictate'}">${ICONS.mic}</button>` : ''}
          ${stopTurn ? `<button type="button" class="stop-button" data-chat-cancel="${esc(stopTurn.task_id)}" aria-label="Stop" data-tip="Stop">${ICONS.stopSq}</button>` : ''}
          <button type="submit" class="send-button" aria-label="Send" data-tip="Send" ${blocked ? 'disabled' : ''}>${ICONS.arrowUp}</button>
        </div>
      </div>
    </form>
    ${composerFoot(a, blocked, tools)}
  </div>`;
}
// ---------- model, effort and context footer ----------
const PROVIDER_NAMES = {'claude-cli': 'Claude CLI', 'codex-cli': 'Codex CLI', 'openrouter': 'OpenRouter'};
function providerName(p) { return PROVIDER_NAMES[p] || p; }
function providerList() { return Object.keys(overview?.known_models || {'claude-cli': 1, 'codex-cli': 1}); }
function providerOptions(selectedProvider) { return providerList().map(p => `<option value="${esc(p)}" ${p === selectedProvider ? 'selected' : ''}>${esc(providerName(p))}</option>`).join(''); }
// OpenRouter facts shown next to a model: free or paid, and whether it reads images.
function orFacts(provider, m) {
  if (provider !== 'openrouter') return '';
  const f = overview?.openrouter_models?.[m];
  return f ? ` · ${f.free ? 'free' : 'paid'}${f.vision ? ' · reads images' : ''}` : '';
}
function modelOption(provider, m, selectedModel) {
  const check = checkLabel((overview.providers[provider]?.models || []).find(x => x.model === m));
  return `<option value="${esc(m)}" ${m === selectedModel ? 'selected' : ''}>${esc(m)}${esc(orFacts(provider, m))}${esc(check)}</option>`;
}
function openRouterKeyDialog() {
  let dialog = document.getElementById('openrouter-key-dialog');
  if (!dialog) {
    dialog = document.createElement('dialog');
    dialog.id = 'openrouter-key-dialog';
    dialog.innerHTML = `<form method="dialog" id="openrouter-key-form" class="dialog-body">
      <h3>OpenRouter API key</h3>
      <p class="note">Create a key at openrouter.ai (Keys page) and paste it here. It is stored on this computer for your Windows account only, is never shown again, and is never sent to an agent or put in a chat.</p>
      <label>Key<input name="key" type="password" autocomplete="off" spellcheck="false" placeholder="sk-or-..." required></label>
      <div class="dialog-actions"><button type="button" data-close-key>Cancel</button><button class="primary">Save key</button></div></form>`;
    document.body.appendChild(dialog);
  }
  dialog.querySelector('form').reset();
  dialog.showModal();
}
function prettyModel(model) {
  const m = String(model || '');
  let r = m.match(/^claude-(opus|sonnet|haiku)-(\d+)(?:-(\d+))?/i);
  if (r) return `${r[1][0].toUpperCase()}${r[1].slice(1)} ${r[2]}${r[3] ? '.' + r[3] : ''}`;
  r = m.match(/^gpt-([\d.]+)(?:-(\w+))?/i);
  if (r) return `GPT-${r[1]}${r[2] ? ' ' + r[2][0].toUpperCase() + r[2].slice(1) : ''}`;
  const facts = overview?.openrouter_models?.[m];
  if (facts?.name) return facts.name.replace(/\s*\(free\)$/i, '');
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
    ${limitRows || `<p class="fine">${u.provider === 'claude-cli' ? 'Shown after the next streamed reply.' : u.provider === 'openrouter' ? 'OpenRouter limits and credits are on your OpenRouter account page.' : `${esc(providerName(u.provider))} does not report plan limits to JARVIS.`}</p>`}
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
    <button type="button" class="foot-chip" data-model-menu aria-haspopup="menu" title="Change model">${esc(prettyModel(a.model))}${ICONS.chevronDown}</button>
    ${f.effort ? `<button type="button" class="foot-chip" data-effort-menu aria-haspopup="menu" title="Change effort">${esc(effortLabel(a.effort || 'auto'))}${ICONS.chevronDown}</button>` : ''}
    <a class="foot-chip" href="${spaceHref('config', a.agent_id)}" title="Abilities and access — sensitive actions ask first">${ICONS.shield}<span>${tools} abilities</span></a>
    <button type="button" class="foot-chip slash-chip" data-slash-menu aria-haspopup="menu" aria-label="Commands" title="Commands (type / in the box)">/</button>
    <span class="foot-note">${esc(blocked || 'Sensitive actions ask first')}</span>
    <span class="spacer"></span>
    ${f.usage && u ? `<div class="usage-wrap"><button type="button" class="ring-btn" data-usage-pin aria-label="Context ${ctx != null ? Math.round(pct) + '%' : 'not measured yet'}">${ring}</button>${usagePanel(a, u)}</div>` : ''}
  </div>`;
}
function modelMenu(a) {
  const groups = Object.entries(overview.known_models || {});
  return groups.map(([provider, models]) => `<div class="menu-label">${esc(providerName(provider))}</div>${(provider === 'openrouter' ? models.slice(0, 12) : models).map(m => `<button type="button" data-set-model="${esc(provider)}|${esc(m)}">${esc(prettyModel(m))}<span class="muted">${esc(orFacts(provider, m))}${esc(checkLabel((overview.providers[provider]?.models || []).find(x => x.model === m)))}</span>${m === a.model && provider === a.provider ? '<span class="check-mark">✓</span>' : ''}</button>`).join('')}`).join('');
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
  return `<div class="blocker perm-card" role="group" aria-label="Permission request"><div class="pc-head">${ICONS.shield}<span><b>${esc(title || 'Task')}</b> asks to <b>${esc(approval?.action || 'run an action')}</b></span></div>
    ${approval?.resource ? `<pre>${esc(approval.resource)}</pre>` : ''}${approval?.reason ? `<div class="note">${esc(approval.reason)}</div>` : ''}
    ${taskId ? `<div class="row gap-top"><button class="primary" data-approve="${esc(taskId)}">Approve this exact action</button><button class="danger" data-deny="${esc(taskId)}">Deny</button></div>` : '<p class="note">Decide it in the asking agent\'s chat or on its Tasks page.</p>'}</div>`;
}
function artifactList(items) {
  if (!items.length) return '<div class="empty">No files yet.</div>';
  return items.map(x => `<div class="artifact"><span class="change ${esc(x.change)}">${esc(x.change)}</span><div class="main-col"><div class="path" title="${esc(x.path)}">${esc(x.path)}</div><small class="muted">v${x.version} · ${esc(fmtSize(x.size))} · ${agoTag(x.created_at)}</small></div>${x.change !== 'deleted' ? `<button class="small" data-artifact="${esc(x.artifact_id)}">Open</button>` : ''}</div>`).join('');
}

// ---------- goals ----------
function goalsPage(a) {
  const goals = a.goals || [];
  const active = goals.filter(g => g.state === 'ACTIVE'), done = goals.filter(g => g.state === 'DONE');
  const categories = overview.goal_categories || {};
  const row = g => `<div class="item ${g.state === 'DONE' ? 'goal-done' : ''}"><button type="button" class="goal-check ${g.state === 'DONE' ? 'done' : ''}" data-goal-toggle="${esc(g.goal_id)}" aria-label="${g.state === 'DONE' ? 'Mark not done' : 'Mark done'}">${g.state === 'DONE' ? '✓' : ''}</button><div class="main-col"><div class="title">${esc(g.title)}</div><div class="subtitle">${esc(g.progress || g.detail || (g.state === 'DONE' ? 'Achieved.' : 'Just set; no progress logged yet.'))}</div><div class="time">${esc(g.category_label)} · ${g.state === 'DONE' ? `done ${agoTag(g.done_at)}` : `set ${agoTag(g.created_at)}`}${g.created_by === 'agent' ? ` with ${esc(a.name)}` : ''}</div></div><button class="more" type="button" aria-label="Goal actions" aria-haspopup="menu" data-goal-menu="${esc(g.goal_id)}">${ICONS.dots}</button></div>`;
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
  return `<div class="artifact-layout"><nav class="artifact-nav" aria-label="Artifact types"><div class="side-heading">Library</div>${['all', 'documents', 'web', 'code'].map(nav).join('')}<div class="side-heading">Media</div>${['images', 'videos', 'audio'].map(nav).join('')}<div class="side-heading">Files</div>${['other', 'folder'].map(nav).join('')}</nav>
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
    if (b.dataset.downloadItem !== undefined) { const blob = await apiBlob(url + (url.includes('?') ? '&' : '?') + 'download=1'); download(blob, item.name); }
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
// A file from a message chip or the Files tab: stored artifacts open with their history; other
// project files open from the folder.
function openProjectFile(path, extra = {}) {
  const a = agentData && agentData.agent_id === selectedId ? agentData : pageData;
  if (!a) return;
  const g = ui.gallery.agent === a.agent_id ? ui.gallery.data : null;
  const known = [...(g?.made || []), ...(g?.folder || [])].find(x => x.path === path);
  if (known) { openItem(a, known); return; }
  const [k] = fileKind(extra.name || path, extra.mime || '');
  openItem(a, {name: extra.name || baseName(path), path, size: extra.size || 0, kind: GALLERY_KIND[k] || 'other', on_disk: true});
}

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
// Ctrl+K: search chat titles as you type; Enter searches the text of every message.
const chatSearch = {query: '', results: null, ranFor: '', sel: -1, busy: false};
function openSearchChats() {
  if (!selectedId) return;
  closeMenus();
  document.body.classList.remove('sidebar-open');
  Object.assign(chatSearch, {query: '', results: null, ranFor: '', sel: -1});
  const input = $('#search-chats-input'); input.value = '';
  renderSearchChats();
  const dialog = $('#search-dialog');
  if (!dialog.open) dialog.showModal();
  input.focus();
  if (!agentData || agentData.agent_id !== selectedId) api(`/api/agents/${encodeURIComponent(selectedId)}`).then(d => { agentData = d; renderSearchChats(); }).catch(() => {});
}
function renderSearchChats() {
  const a = selected(); if (!a) return;
  const data = agentData && agentData.agent_id === selectedId ? agentData : null;
  const q = chatSearch.query.trim(), ql = q.toLowerCase();
  const mark = text => { const safe = esc(text); const needle = esc(q); if (!needle) return safe; const at = safe.toLowerCase().indexOf(needle.toLowerCase()); return at < 0 ? safe : `${safe.slice(0, at)}<mark>${safe.slice(at, at + needle.length)}</mark>${safe.slice(at + needle.length)}`; };
  const chats = (data?.chats || []).slice().sort((x, y) => (y.last_at || y.created_at) - (x.last_at || x.created_at));
  const item = (href, icon, title, sub, time) => `<a class="search-item" href="${href}" data-search-item>${icon}<span class="si-text"><b>${title}</b>${sub ? `<small>${sub}</small>` : ''}</span>${time ? `<span class="si-time">${time}</span>` : ''}</a>`;
  const chatItem = c => item(`#/agents/${esc(a.agent_id)}/work/${esc(c.chat_id)}`, ICONS.chat, mark(c.title), c.archived ? 'Archived' : '', agoTag(c.last_at || c.created_at));
  let html = item(`#/agents/${esc(a.agent_id)}`, ICONS.compose, 'New chat', '', '');
  if (!q) html += dayGroups(chats.filter(c => !c.archived).slice(0, 80), 'last_at').map(([label, list]) => `<div class="search-group">${label}</div>${list.map(chatItem).join('')}`).join('') || '<p class="note">No chats yet.</p>';
  else {
    const matches = chats.filter(c => String(c.title).toLowerCase().includes(ql)).slice(0, 30);
    html += matches.length ? `<div class="search-group">Chats</div>${matches.map(chatItem).join('')}` : '';
    const r = chatSearch.ranFor === q ? chatSearch.results : null;
    if (chatSearch.busy) html += '<p class="note search-hint">Searching messages…</p>';
    else if (r) {
      const byChat = (r.messages || []).slice(0, 40);
      html += `<div class="search-group">Messages</div>${byChat.length ? byChat.map(m => item(m.chat_id ? `#/agents/${esc(a.agent_id)}/work/${esc(m.chat_id)}` : `#/agents/${esc(a.agent_id)}/tasks`, m.where === 'you' ? ICONS.agent : ICONS.chat, esc(m.title), mark(m.snippet), agoTag(m.created_at))).join('') : '<p class="note search-hint">No messages match.</p>'}`;
    } else html += `<p class="note search-hint">Press Enter to search the text of every message${matches.length ? '' : ' — no chat titles match'}.</p>`;
    if (features().search) html += `<button type="button" class="search-item subtle" data-open-search-page>${ICONS.search}<span class="si-text"><b>Open full search for “${esc(q)}”</b></span></button>`;
  }
  const box = $('#search-chats-results');
  box.innerHTML = html; updateRelative();
  const items = box.querySelectorAll('[data-search-item], [data-open-search-page]');
  items.forEach((el, i) => el.classList.toggle('sel', i === chatSearch.sel));
}
async function runChatSearch() {
  const q = chatSearch.query.trim();
  if (!q || !selectedId) return;
  chatSearch.busy = true; renderSearchChats();
  try { chatSearch.results = await api(`/api/agents/${encodeURIComponent(selectedId)}/search?q=${encodeURIComponent(q)}`); chatSearch.ranFor = q; }
  catch (error) { toast(error.message, true); }
  finally { chatSearch.busy = false; renderSearchChats(); }
}
$('#search-chats-input').addEventListener('input', event => { chatSearch.query = event.target.value; chatSearch.sel = -1; renderSearchChats(); });
$('#search-chats-input').addEventListener('keydown', event => {
  const items = [...$('#search-chats-results').querySelectorAll('[data-search-item], [data-open-search-page]')];
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    chatSearch.sel = items.length ? (chatSearch.sel + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length : -1;
    items.forEach((el, i) => el.classList.toggle('sel', i === chatSearch.sel));
    items[chatSearch.sel]?.scrollIntoView({block: 'nearest'});
  } else if (event.key === 'Enter' && !event.isComposing) {
    event.preventDefault();
    if (chatSearch.sel >= 0 && items[chatSearch.sel]) items[chatSearch.sel].click();
    else runChatSearch();
  } else if (event.key === 'Escape') {
    // A search field would clear itself first; like ChatGPT, one Esc closes the dialog.
    event.preventDefault(); event.stopPropagation();
    $('#search-dialog').close();
  }
});
$('#search-dialog').addEventListener('click', event => {
  if (event.target === event.currentTarget) { event.currentTarget.close(); return; }
  const link = event.target.closest('[data-search-item]');
  if (link) { event.currentTarget.close(); return; }
  if (event.target.closest('[data-open-search-page]')) {
    ui.search = {query: chatSearch.query.trim(), agent: selectedId, results: chatSearch.ranFor === chatSearch.query.trim() ? chatSearch.results : null};
    event.currentTarget.close();
    location.hash = `#/agents/${selectedId}/search`;
  }
});

// ---------- app preview (hosted in the workspace panel's Preview tab) ----------
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
    ${p.state === 'RUNNING' ? `<button class="small" data-app-show="${esc(p.preview_id)}">Show</button>${isAppUrl(p.url) ? `<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">New tab ↗</a>` : ''}<button class="small" data-app-stop="${esc(p.preview_id)}">Stop</button>` : `<button class="small primary" data-app-start="${esc(p.preview_id)}">Start again</button>`}</div>`).join('')}</div>`;
}
function syncAppPanel(previews) {
  for (const p of previews) appPanel.previews.set(p.preview_id, p);
  if (appPanel.id && appPanel.previews.has(appPanel.id)) { showApp(appPanel.previews.get(appPanel.id), false); return; }
  // Open a newly verified app once, as the operator asked; a closed panel stays closed.
  const fresh = previews.filter(p => p.state === 'RUNNING' && !shownApps.has(p.preview_id) && !dismissedApps.has(p.preview_id)).pop();
  if (fresh) { shownApps.add(fresh.preview_id); rememberApps(); showApp(fresh); autoOpenPanel('preview'); }
}
function showApp(p, select = true) {
  const box = $('#app-panel-frame');
  if (select) ui.previewKey = `app:${p.preview_id}`;
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
  $('#app-panel').hidden = true;
  if (ui.previewKey.startsWith('app:')) ui.previewKey = '';
  renderDetails();
}
async function appCommand(id, verb) {
  const result = await act(api(`/api/previews/${encodeURIComponent(id)}/${verb}`, {}), verb === 'stop' ? 'App stopped.' : 'App started again.');
  if (result) { appPanel.previews.set(id, result); if (appPanel.id === id) { appPanel.url = undefined; showApp(result, false); } else if (verb === 'start') { dismissedApps.delete(id); rememberApps(); showApp(result); openPanel('preview'); } }
}

// ---------- configuration, projects, activity, settings ----------
function agentConfig(a) {
  const providers = overview.providers;
  const options = p => (a.known_models[p] || []).map(m => modelOption(p, m, p === a.provider ? a.model : null)).join('');
  return `<form class="panel" id="config-form">
    <h3>Model and abilities</h3>
    <div class="form-grid">
      <label>Provider<select name="provider" id="cfg-provider">${providerOptions(a.provider)}</select></label>
      <label>Model<select name="model" id="cfg-model">${options(a.provider)}</select></label>
      <p class="note wide">Model changes apply to new messages; no provider is silently substituted.</p>
      ${providers.openrouter && !providers.openrouter.authenticated && a.provider === 'openrouter' ? `<div class="row wide"><span class="note">OpenRouter needs your API key before this agent can run.</span><span class="spacer"></span><button type="button" class="primary" data-openrouter-key>Add OpenRouter key</button></div>` : ''}
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
// ---------- apps & connections (MCP servers) ----------
const CONNECTION_STATUS = {connected: ['ok', 'connected'], 'needs sign-in': ['bad', 'needs sign-in'], error: ['bad', 'error'], 'not tested': ['', 'not tested']};
function renderConnections(data) {
  const items = data.connections || [];
  const cards = items.map(c => {
    const [tone, label] = CONNECTION_STATUS[c.status] || ['', c.status];
    const needsConnect = c.kind !== 'stdio' && (c.auth === 'oauth' || c.status === 'needs sign-in');
    const tools = c.tools || [];
    return `<div class="panel conn-card"><div class="row"><h3 class="flush">${esc(c.name)}</h3><span class="chip ${tone}">${esc(label)}</span>${c.enabled ? '' : '<span class="chip">off</span>'}<span class="spacer"></span><span class="muted mono">${esc(c.kind === 'stdio' ? (c.command || []).join(' ').slice(0, 60) : c.url)}</span></div>
      ${c.error ? `<p class="note bad-text">${esc(c.error)}</p>` : ''}
      <p class="note">${tools.length ? `${tools.length} tools · ${tools.filter(t => t.read_only).length} read-only` : 'No tools yet — test or connect it.'}${c.server ? ` · server: ${esc(c.server)}` : ''}${c.env_names?.length ? ` · secrets: ${esc(c.env_names.join(', '))}` : ''}</p>
      <div class="row gap-top wrap">
        ${needsConnect ? `<button class="primary" data-conn-connect="${esc(c.id)}">${c.signed_in ? 'Sign in again' : 'Connect'}</button>` : ''}
        ${c.kind !== 'stdio' ? `<button data-conn-token="${esc(c.id)}">${c.auth === 'token' && c.signed_in ? 'Replace token' : 'Use a token'}</button>` : ''}
        <button data-conn-test="${esc(c.id)}">Test</button>
        <button data-conn-enable="${esc(c.id)}|${c.enabled ? 'off' : 'on'}">${c.enabled ? 'Turn off' : 'Turn on'}</button>
        <label class="inline">Ask me before <select data-conn-ask="${esc(c.id)}">${[['changes', 'anything that sends or changes'], ['always', 'every action'], ['never', 'nothing (trusted)']].map(([v, l]) => `<option value="${v}" ${c.ask === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <span class="spacer"></span><button class="danger" data-conn-delete="${esc(c.id)}">Remove</button></div>
      ${tools.length ? `<details class="gap-top"><summary>Tools</summary><div class="list">${tools.map(t => `<div class="item"><div class="main-col"><div class="mono">${esc(t.name)}</div><div class="subtitle">${esc(t.description)}</div></div><span class="chip">${t.read_only ? 'reads' : 'can change'}</span></div>`).join('')}</div></details>` : ''}
    </div>`;
  }).join('');
  const presets = (data.presets || []).map(p => `<button type="button" class="preset" data-conn-add="${esc(p.id)}"><b>${esc(p.name)}</b><small>${esc(p.category)} · ${p.auth === 'oauth' ? 'sign in' : p.auth === 'token' ? 'token' : p.auth === 'env' ? 'local program' : 'URL'}</small></button>`).join('');
  return `<div class="page"><div class="page-head"><h1>Apps &amp; connections</h1><p class="sub">Connect your accounts and any MCP server once; every agent with the “connected apps” ability can then use them. Reading is automatic; anything that sends, posts, buys or changes asks you first. What a connected app returns is treated as untrusted, like a web page.</p></div>
    <section><h2 class="section-title">Your connections</h2>${cards || '<div class="panel"><p class="note">Nothing connected yet. Pick one below — Zapier alone covers Gmail, Outlook/Hotmail, YouTube, X, Google Calendar and thousands more.</p></div>'}</section>
    <section class="gap-top"><h2 class="section-title">Add a connection</h2><div class="preset-grid">${presets}</div></section></div>`;
}
function connectionDialog(preset) {
  let dialog = document.getElementById('connection-dialog');
  if (!dialog) { dialog = document.createElement('dialog'); dialog.id = 'connection-dialog'; document.body.appendChild(dialog); }
  const field = (name, label, attrs = '') => `<label>${label}<input name="${name}" ${attrs}></label>`;
  const body = preset.auth === 'env'
    ? `${field('name', 'Name', 'required maxlength="60" placeholder="My server"')}
       ${field('command', 'Command', 'required placeholder="npx -y some-mcp-server" spellcheck="false"')}
       <label>Secret settings (one NAME=value per line, optional)<textarea name="env" rows="3" spellcheck="false" placeholder="API_KEY=..."></textarea></label>`
    : preset.id === 'zapier'
    ? `${field('secret', 'Zapier server URL or token (optional)', 'type="password" autocomplete="off" spellcheck="false" placeholder="Leave empty to sign in with Zapier"')}`
    : `${preset.url ? '' : field('url', 'Server URL', 'required placeholder="https://example.com/mcp" spellcheck="false"')}
       ${preset.id === 'custom-url' ? field('name', 'Name', 'maxlength="60" placeholder="My server"') : ''}
       ${preset.auth === 'oauth' ? '' : field('token', preset.auth === 'token' ? 'Token' : 'Token (optional)', `${preset.auth === 'token' ? 'required' : ''} type="password" autocomplete="off" spellcheck="false"`)}`;
  dialog.innerHTML = `<form method="dialog" id="connection-form" class="dialog-body" data-preset="${esc(preset.id)}">
    <h3>${esc(preset.name)}</h3><p class="note">${esc(preset.setup || '')}</p>${body}
    ${preset.auth === 'oauth' || preset.id === 'zapier' ? '<p class="note">After adding, a sign-in page opens in a new tab. Approve JARVIS there, then come back.</p>' : ''}
    <p class="note">Secrets are stored on this computer for your Windows account only and are never shown again or given to an agent.</p>
    <div class="dialog-actions"><button type="button" data-close-key>Cancel</button><button class="primary">Add</button></div></form>`;
  dialog.showModal();
}
function tokenDialog(id) {
  let dialog = document.getElementById('connection-dialog');
  if (!dialog) { dialog = document.createElement('dialog'); dialog.id = 'connection-dialog'; document.body.appendChild(dialog); }
  dialog.innerHTML = `<form method="dialog" id="connection-token-form" class="dialog-body" data-id="${esc(id)}"><h3>Token</h3>
    <label>Token<input name="token" type="password" required autocomplete="off" spellcheck="false"></label>
    <div class="dialog-actions"><button type="button" data-close-key>Cancel</button><button class="primary">Save and test</button></div></form>`;
  dialog.showModal();
}
async function startConnect(id) {
  const result = await act(api(`/api/connections/${encodeURIComponent(id)}/connect`, {}), null);
  if (result?.authorize_url) { window.open(result.authorize_url, '_blank', 'noopener'); toast('Finish signing in on the page that just opened, then come back here.'); }
}
// Theme: system (default), light or dark; stored per browser.
function themePref() { return store('jarvis.hub.theme') || 'system'; }
function applyTheme(pref) {
  const root = document.documentElement;
  if (pref === 'light' || pref === 'dark') root.dataset.theme = pref; else delete root.dataset.theme;
}
applyTheme(themePref());
function personalizationPanel(p) {
  if (!p || p.error) return `<div class="panel" id="personalization"><h3>Personalization</h3><p class="note">Personalization is not available on this Hub yet.</p></div>`;
  const v = ui.personalDraft || p;
  return `<form class="panel" id="personalization"><h3>Personalization</h3><p class="note">Every agent reads these as your preferences: information about you, never a rule that overrides its limits or asks.</p>
    <label class="gap-top">What should agents know about you?<textarea name="about_you" rows="4" maxlength="1500" placeholder="For example: I'm a product designer in Toronto. I prefer metric units.">${esc(v.about_you || '')}</textarea></label>
    <label class="gap-top">How should agents respond?<textarea name="response_style" rows="4" maxlength="1500" placeholder="For example: Be concise. Use tables for comparisons.">${esc(v.response_style || '')}</textarea></label>
    <div class="dialog-actions">${ui.personalDraft ? '<button type="button" data-personal-reset>Discard changes</button>' : ''}<button class="primary">Save</button></div></form>`;
}
function renderSettings(data) {
  const d = overview.defaults;
  const pref = themePref();
  const providerPanels = Object.entries(overview.providers).map(([name, p]) => `
    <div class="panel"><div class="row"><h3 class="flush">${esc(providerName(name))}</h3><span class="chip ${p.installed && p.authenticated ? 'ok' : 'bad'}">${name === 'openrouter' ? (p.authenticated ? 'key saved' : 'no key') : p.installed ? (p.authenticated ? 'signed in' : 'not signed in') : 'not installed'}</span><span class="spacer"></span><span class="muted mono">${esc(p.version || '')}</span></div>
      ${name === 'openrouter' ? `<p class="note">One API key gives agents access to hundreds of models, including free ones. Models marked free cost nothing; paid ones use your OpenRouter credits. Prompts, files and screenshots an agent sends go to OpenRouter and the model's host; free and stealth models may log them, so keep private data on the Claude or Codex subscriptions.</p>
      <div class="row gap-top"><button class="primary" data-openrouter-key>${p.authenticated ? 'Replace key' : 'Add OpenRouter key'}</button>${p.authenticated ? '<button class="danger" data-forget-openrouter>Remove key</button>' : ''}</div>` : ''}
      <p class="note">${esc(p.detail)} ${p.checked_at ? `Checked ${agoTag(p.checked_at)}.` : ''}</p>
      <div class="list gap-top">${p.models.map(m => `<div class="item"><div class="main-col"><div class="mono">${esc(m.model)}</div><div class="subtitle">${esc(verificationText(m))}</div></div>
        <button data-verify="${esc(name)}|${esc(m.model)}">Verify</button>
        ${d.provider === name && d.model === m.model ? '<span class="chip">default</span>' : `<button data-default="${esc(name)}|${esc(m.model)}">Make default</button>`}</div>`).join('')}</div></div>`).join('');
  return `<div class="page"><div class="page-head"><h1>Settings</h1><span class="spacer"></span><button id="refresh-providers">Re-check providers</button><p class="sub">Appearance, personalization, providers, the default model for new agents, remote access and the audit log. <a href="#/activity">Activity</a> · <a href="#/projects">Project folders</a></p></div>
    <div class="panel" id="appearance"><div class="row"><h3 class="flush">Theme</h3><span class="spacer"></span><div class="segmented" role="radiogroup" aria-label="Theme">${[['system', 'System'], ['light', 'Light'], ['dark', 'Dark']].map(([k, l]) => `<button type="button" role="radio" aria-checked="${pref === k}" class="seg${pref === k ? ' active' : ''}" data-theme-set="${k}">${l}</button>`).join('')}</div></div></div>
    ${personalizationPanel(data.personalization)}
    <div class="panel"><h3>Default for new agents</h3><div class="row"><span class="mono">${esc(d.provider)} · <b>${esc(d.model)}</b></span><span class="muted">${esc(verificationText(d.check || {}))}</span></div>
      <p class="note">Verification sends one tiny request through the same path agents use and records the model the CLI reports it actually ran. An unverified or unavailable model is never silently replaced.</p></div>
    <div class="panel"><h3>Agent abilities</h3><p class="note">Give every agent every ability: web, files, programs, memory, apps and websites on this computer, your connected accounts, background jobs and new skills. Sensitive actions still ask you first, and you can switch any ability off per agent in its settings.</p>
      <div class="row gap-top"><button class="primary" id="grant-full-access">Give every agent full access</button></div></div>
    ${providerPanels}
    <div class="panel"><h3>Remote access</h3><p class="note">The Hub listens on this computer only. For another device, put Tailscale Serve (HTTPS on your private network) in front of it, start the Hub with <code>--remote-access paired --trusted-host &lt;your-tailnet-hostname&gt;</code>, then pair each device with a one-time code. Sessions are revocable below.</p>
      ${data.sessions.length ? `<div class="list">${data.sessions.map(s => `<div class="item"><div class="main-col"><div>${esc(s.label || 'device')}</div><div class="subtitle">created ${esc(s.created_at)} · ${s.revoked_at ? 'revoked' : `expires ${esc(s.expires_at)}`}</div></div>${s.revoked_at ? '' : `<button class="danger" data-revoke="${esc(s.session_id)}">Revoke</button>`}</div>`).join('')}</div>` : '<p class="muted">No paired devices.</p>'}</div>
    <div class="panel"><h3>Audit log</h3>${data.audit.length ? `<div class="list">${data.audit.slice(0, 40).map(r => `<div class="item"><div class="main-col"><div>${esc(r.action)} ${r.target ? `<span class="muted mono">${esc(r.target)}</span>` : ''}</div><div class="subtitle">${esc(r.actor)} · ${agoTag(r.ts)}${r.detail ? ` · ${esc(r.detail)}` : ''}</div></div><span class="chip ${r.outcome === 'ok' ? 'ok' : 'bad'}">${esc(r.outcome)}</span></div>`).join('')}</div>` : '<p class="muted">No commands yet.</p>'}</div></div>`;
}
function repaintSettings() { if (route?.name === 'settings' && pageData?.audit) { lastHtml = ''; paint(renderSettings(pageData)); } }

// ---------- menus ----------
let menuAnchor = null;
const slash = {open: false, items: [], sel: 0, filter: ''};
function closeMenus() {
  $('#item-menu').hidden = true; $('#agent-menu').hidden = true;
  $('#agent-picker').setAttribute('aria-expanded', 'false');
  if (menuAnchor) { menuAnchor.setAttribute('aria-expanded', 'false'); menuAnchor = null; }
  slash.open = false;
}
function openMenu(anchor, html, opts = {}) {
  const menu = $('#item-menu');
  if (!opts.keep && menuAnchor === anchor && !menu.hidden) { closeMenus(); return; }
  closeMenus();
  menu.className = `menu${opts.cls ? ` ${opts.cls}` : ''}`;
  menu.innerHTML = html; menu.hidden = false;
  menuAnchor = anchor; anchor.setAttribute('aria-expanded', 'true');
  const box = anchor.getBoundingClientRect(), width = menu.offsetWidth, height = menu.offsetHeight;
  const left = opts.align === 'left' ? box.left : box.right - width;
  menu.style.left = `${Math.max(8, Math.min(innerWidth - width - 8, left))}px`;
  const above = opts.above || box.bottom + height + 8 > innerHeight;
  menu.style.top = `${above ? Math.max(8, box.top - height - 6) : box.bottom + 6}px`;
  if (opts.focus !== false) menu.querySelector('button:not([disabled]), a')?.focus();
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
function composerMenu() {
  const prefill = (text, icon, label) => `<button type="button" role="menuitem" data-prefill="${esc(text)}">${ICONS[icon]}<span>${label}</span></button>`;
  return `<button type="button" role="menuitem" data-attach>${ICONS.clip}<span>Add photos &amp; files</span></button>
    ${prefill('Create an image of ', 'image', 'Create image')}${prefill('Do deep research on ', 'telescope', 'Deep research')}<hr>
    ${features().goals ? `<a role="menuitem" href="${spaceHref('goals')}">${ICONS.goals}<span>Set a goal</span></a>` : ''}
    ${prefill('Check in with me every day at 9:00 about ', 'clock', 'Schedule a check-in')}${prefill('Build me a web app that ', 'browser', 'Build an app')}
    ${prefill('Research ', 'search', 'Research something')}${prefill('Make me a document about ', 'file', 'Make a document')}`;
}
function profileMenu() {
  return `<a role="menuitem" href="#/settings" data-scroll-to="personalization">${ICONS.sliders}<span>Personalization</span></a><a role="menuitem" href="#/settings">${ICONS.gear}<span>Settings</span></a><hr>
    <a role="menuitem" href="#/overview">${ICONS.agent}<span>All agents</span></a><a role="menuitem" href="#/activity">${ICONS.pulse}<span>Activity</span></a><a role="menuitem" href="#/projects">${ICONS.folder}<span>Project folders</span></a><a role="menuitem" href="#/connections">${ICONS.plug}<span>Apps &amp; connections</span></a><hr>
    <button type="button" role="menuitem" data-help>${ICONS.keyboard}<span>Keyboard shortcuts &amp; commands</span></button>`;
}
function exportMenu() {
  return `<div class="menu-label">Export this chat</div><button type="button" role="menuitem" data-export="md">${ICONS.file}<span>Markdown (.md)</span></button><button type="button" role="menuitem" data-export="json">${ICONS.file}<span>JSON (.json)</span></button>`;
}
// Slash commands: typing "/" at the start of the composer opens this list.
const SLASH = [['new', 'Start a new chat', 'compose'], ['model', 'Change the model', 'model'], ['effort', 'Change reasoning effort', 'gauge'], ['export', 'Export this chat as Markdown', 'share'], ['compact', 'Compact this conversation', 'compact'],
  ['goal', 'Set a goal', 'goals'], ['schedule', 'Schedule a check-in', 'clock'], ['image', 'Create an image', 'image'], ['research', 'Deep research', 'telescope'], ['help', 'Commands and keyboard shortcuts', 'help']];
function slashHtml() {
  const items = SLASH.filter(([c]) => c.startsWith(slash.filter.toLowerCase()));
  slash.items = items.map(([c]) => c);
  slash.sel = Math.max(0, Math.min(slash.sel, items.length - 1));
  return items.length ? `<div class="menu-label">Commands</div>${items.map(([c, label, icon], i) => `<button type="button" role="menuitem" class="slash-item${i === slash.sel ? ' active' : ''}" data-slash="${c}">${ICONS[icon]}<span><b>/${c}</b><small>${label}</small></span></button>`).join('')}` : '';
}
function openSlash(filter, anchor) {
  slash.filter = filter;
  if (!slash.open) slash.sel = 0;
  const html = slashHtml();
  if (!html) { closeMenus(); return; }
  if (slash.open && !$('#item-menu').hidden) { $('#item-menu').innerHTML = html; return; }
  const target = anchor || $('#chat-form') || $('#chat-request');
  if (!target) return;
  openMenu(target, html, {focus: !!anchor, above: true, align: 'left', cls: 'slash-menu', keep: true});
  slash.open = true;
}
async function runSlash(cmd) {
  closeMenus();
  const box = $('#chat-request');
  const setBox = text => { if (!box) return; box.value = text; grow(box); drafts.set(location.hash, {body: text}); if (text) { box.focus(); box.setSelectionRange(text.length, text.length); } };
  if (box && /^\/\w*$/.test(box.value)) setBox('');
  const a = pageData && pageData.agent_id === selectedId ? pageData : null;
  if (cmd === 'new') newChat();
  else if (cmd === 'model') { const chip = $('.composer-foot [data-model-menu]'); if (chip && a) openMenu(chip, modelMenu(a)); }
  else if (cmd === 'effort') { const chip = $('[data-effort-menu]'); if (chip && a) openMenu(chip, effortMenu(a)); else toast('Effort is not adjustable for this agent.', true); }
  else if (cmd === 'export') { if (route.chat) exportChat('md'); else toast('Send a message first; then the chat can be exported.', true); }
  else if (cmd === 'compact') { if (route.chat) compactChat(); else toast('Open a chat with messages to compact it.', true); }
  else if (cmd === 'goal') setBox('Help me set a goal: ');
  else if (cmd === 'schedule') setBox('Check in with me every day at 9:00 about ');
  else if (cmd === 'image') setBox('Create an image of ');
  else if (cmd === 'research') setBox('Do deep research on ');
  else if (cmd === 'help') helpDialog();
}
function helpDialog() {
  let dialog = document.getElementById('help-dialog');
  if (!dialog) { dialog = document.createElement('dialog'); dialog.id = 'help-dialog'; document.body.appendChild(dialog); }
  const keys = [['Ctrl K', 'Search chats'], ['Ctrl Shift O', 'New chat'], ['Shift Esc', 'Focus the message box'], ['Esc', 'Stop dictation, close menus'], ['Enter', 'Send'], ['Shift Enter', 'New line'], ['/', 'Commands (at the start of the message box)']];
  dialog.innerHTML = `<div class="dialog-body"><div class="dialog-head"><h2>Shortcuts &amp; commands</h2><button type="button" class="icon" data-close aria-label="Close">×</button></div>
    <div class="kbd-list">${keys.map(([k, l]) => `<div class="kbd-row"><span>${esc(l)}</span><span>${k.split(' ').map(x => `<kbd>${esc(x)}</kbd>`).join('')}</span></div>`).join('')}</div>
    <h3 class="gap-top">Commands</h3><div class="kbd-list">${SLASH.map(([c, l]) => `<div class="kbd-row"><span>${esc(l)}</span><span><kbd>/${c}</kbd></span></div>`).join('')}</div></div>`;
  dialog.showModal();
}
function newChat() {
  if (!selectedId) return;
  const target = `#/agents/${selectedId}`;
  if (location.hash === target) { focusComposer(); return; }
  location.hash = target;
  setTimeout(focusComposer, 50);
}
function focusComposer() { const box = $('#chat-request'); if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); } }
async function exportChat(format) {
  const a = pageData && pageData.agent_id === selectedId ? pageData : null;
  if (!a || !route.chat) return;
  try {
    const response = await fetch(`/api/agents/${encodeURIComponent(a.agent_id)}/chats/${encodeURIComponent(route.chat)}/export?format=${format === 'json' ? 'json' : 'md'}`, {headers: {'Authorization': `Bearer ${token}`}});
    if (response.status === 401) { needSignIn(); return; }
    if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.error || `Export failed (${response.status}).`); }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const named = (disposition.match(/filename\*=UTF-8''([^;]+)/i) || [])[1] || (disposition.match(/filename="?([^";]+)"?/i) || [])[1];
    const chat = (a.chats || []).find(c => c.chat_id === route.chat);
    let name = `${String(chat?.title || 'chat').replace(/[^\w .-]+/g, '').trim().slice(0, 60) || 'chat'}.${format === 'json' ? 'json' : 'md'}`;
    if (named) { try { name = decodeURIComponent(named); } catch { name = named; } }
    download(blob, name);
    toast('Chat exported.');
  } catch (error) { toast(error.message, true); }
}
async function compactChat() {
  const a = pageData && pageData.agent_id === selectedId ? pageData : null;
  if (!a || !route.chat) return;
  if (!confirm(`Compact this conversation now?\n\nThe last ${4} turns stay word for word; older ones are condensed into a short summary the agent keeps. The original older turns are sealed with the agent's memory key and can be read back only with that key file.`)) return;
  const result = await act(api(`/api/agents/${a.agent_id}/chats/${route.chat}/compact`, {}));
  if (result) toast(result.compacted ? `Compacted ${result.messages} older messages into a summary.` : result.reason);
}
// ---------- dictation (Web Speech API) and read aloud ----------
const dictation = {rec: null, active: false, hash: '', base: ''};
function setDictationText(text) {
  if (location.hash !== dictation.hash) return;
  const box = $('#chat-request'); if (box) { box.value = text; grow(box); }
  drafts.set(dictation.hash, {...(drafts.get(dictation.hash) || {}), body: text});
}
function refreshMic() {
  const mic = $('[data-dictate]');
  if (mic) { mic.classList.toggle('listening', dictation.active); mic.setAttribute('aria-pressed', String(dictation.active)); mic.setAttribute('aria-label', dictation.active ? 'Stop dictation' : 'Dictate'); mic.dataset.tip = dictation.active ? 'Stop dictation' : 'Dictate'; }
  lastHtml = '';
}
function startDictation() {
  const box = $('#chat-request');
  if (!box || !SpeechRec || dictation.active) return;
  let rec;
  try { rec = new SpeechRec(); } catch { toast('Dictation is not available in this browser.', true); return; }
  rec.continuous = true; rec.interimResults = true; rec.lang = navigator.language || 'en-US';
  Object.assign(dictation, {rec, active: true, hash: location.hash, base: box.value ? box.value.replace(/\s*$/, ' ') : ''});
  rec.onresult = event => {
    let finalText = '', interim = '';
    for (const result of event.results) { if (result.isFinal) finalText += result[0].transcript; else interim += result[0].transcript; }
    setDictationText(dictation.base + finalText + interim);
  };
  rec.onerror = event => { if (['not-allowed', 'service-not-allowed'].includes(event.error)) toast('Microphone access was blocked. Allow it for this page to dictate.', true); else if (event.error !== 'aborted' && event.error !== 'no-speech') toast(`Dictation stopped: ${event.error}.`, true); };
  rec.onend = () => { if (dictation.rec === rec) { dictation.active = false; dictation.rec = null; refreshMic(); } };
  try { rec.start(); } catch { dictation.active = false; dictation.rec = null; toast('Dictation could not start.', true); }
  refreshMic();
  box.focus();
}
function stopDictation() { try { dictation.rec?.stop(); } catch { /* already stopped */ } dictation.active = false; refreshMic(); }
const speech = {key: '', hash: '', utter: null};
function speak(key) {
  const synth = window.speechSynthesis; if (!synth) return;
  const same = speech.key === key && speech.hash === location.hash;
  synth.cancel(); speech.key = ''; speech.utter = null;
  if (!same) {
    const m = findMessage(key);
    if (m) {
      const u = new SpeechSynthesisUtterance(plain(replyText(m), 100000));
      u.onend = u.onerror = () => { if (speech.utter === u) { speech.key = ''; speech.utter = null; repaint(); } };
      Object.assign(speech, {key, hash: location.hash, utter: u});
      synth.speak(u);
    }
  }
  repaint();
}
// A fresh id per request; the backend replays the first result if the same id arrives again.
const newRequestId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
async function sendEdit() {
  const e = ui.editing, a = pageData;
  if (!e || !a || !route.chat) return;
  const body = e.text.trim();
  if (!body) { toast('The message is empty.', true); return; }
  const result = await act(api(`/api/agents/${encodeURIComponent(a.agent_id)}/chats/${encodeURIComponent(route.chat)}/edit`, {message_id: e.id, body, request_id: newRequestId()}), 'Sent your edited message.');
  if (result) { ui.editing = null; repaint(); }
}
async function giveFeedback(key, rating) {
  const a = pageData, m = findMessage(key);
  if (!m || !m.task_id || !route.chat) return;
  const previous = m.feedback ?? null, next = previous?.rating === rating ? null : rating;
  m.feedback = next ? {rating: next, note: null, at: now()} : null; repaint();
  try {
    const saved = await api(`/api/agents/${encodeURIComponent(a.agent_id)}/chats/${encodeURIComponent(route.chat)}/feedback`, {message_id: m.message_id, rating: next});
    if (saved && 'feedback' in saved) { m.feedback = saved.feedback; repaint(); }
    if (next) toast('Thanks for the feedback.');
  } catch (error) { m.feedback = previous; repaint(); toast(error.message, true); }
}
// Rename a chat in place (sidebar row or the header title), like ChatGPT.
function beginRename(chatId, where) {
  const data = agentData && agentData.agent_id === selectedId ? agentData : pageData;
  const chat = (data?.chats || []).find(c => c.chat_id === chatId);
  if (!chat || ui.renaming) return;
  const side = () => [...document.querySelectorAll('#conversation-tree .convo')].find(el => el.dataset.chat === chatId);
  let host = where === 'head' ? $('#head-chat-title') : side();
  if (!host && where === 'head') { where = 'side'; host = side(); }
  if (!host) return;
  ui.renaming = {chat_id: chatId, where, original: chat.title};
  const input = document.createElement('input');
  input.type = 'text'; input.className = 'rename-input'; input.value = chat.title; input.maxLength = 120; input.spellcheck = false;
  input.setAttribute('aria-label', 'Chat title');
  host.replaceChildren(input); host.classList.add('renaming');
  input.focus(); input.select();
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); commitRename(input); }
    else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); endRename(); }
  });
  input.addEventListener('blur', () => commitRename(input));
}
function endRename() {
  if (!ui.renaming) return;
  ui.renaming = null;
  $('#conversation-tree').dataset.content = ''; $('#main-title').dataset.content = '';
  renderSidebar(selected(), currentSpace()); renderMainTitle(selected());
}
async function commitRename(input) {
  const r = ui.renaming;
  if (!r || r.saving) return;
  const title = input.value.replace(/\s+/g, ' ').trim();
  if (!title || title === r.original) { endRename(); return; }
  r.saving = true;
  try {
    const saved = await api(`/api/agents/${encodeURIComponent(selectedId)}/chats/${encodeURIComponent(r.chat_id)}/rename`, {title});
    for (const d of [agentData, pageData]) { const c = d?.chats?.find(x => x.chat_id === r.chat_id); if (c) c.title = saved?.title || title; }
    endRename();
    toast('Chat renamed.');
    tick(true);
  } catch (error) { r.saving = false; toast(error.message, true); input.focus(); }
}
function flashCopied(button) {
  const original = button.innerHTML;
  button.innerHTML = button.classList.contains('code-copy') ? `${ICONS.check}<span>Copied</span>` : ICONS.check;
  button.classList.add('copied');
  setTimeout(() => { if (button.isConnected) { button.innerHTML = original; button.classList.remove('copied'); } }, 2000);
}

// ---------- attachments: paste, drop, picker ----------
document.addEventListener('paste', event => {
  if (!event.target.closest?.('#chat-form')) return;
  const files = [...(event.clipboardData?.files || [])];
  if (files.length) { event.preventDefault(); addFiles(files); }
});
document.addEventListener('dragover', event => { if (event.target.closest?.('.chat-workspace') && $('#chat-form')) { event.preventDefault(); document.body.classList.add('dropping'); } });
document.addEventListener('dragleave', event => { if (!event.relatedTarget) document.body.classList.remove('dropping'); });
document.addEventListener('drop', event => {
  document.body.classList.remove('dropping');
  if (!event.target.closest?.('.chat-workspace') || !$('#chat-form')) return;
  event.preventDefault();
  addFiles([...(event.dataTransfer?.files || [])]);
});
async function startChat(agentId, body, title) {
  const chat = await api(`/api/agents/${agentId}/chats`, {title: (title || body).slice(0, 80)});
  await api(`/api/agents/${agentId}/messages`, {chat_id: chat.chat_id, body, request_id: newRequestId()});
  location.hash = `#/agents/${agentId}/work/${chat.chat_id}`;
}

// ---------- interactions ----------
async function act(promise, message) {
  try { const result = await promise; if (message) toast(message); await tick(true); return result; }
  catch (error) { toast(error.message, true); return null; }
}
function toggleSidebar() {
  const toggle = $('#sidebar-toggle');
  if (innerWidth <= 900) { const open = document.body.classList.toggle('sidebar-open'); toggle.setAttribute('aria-expanded', String(open)); }
  else { const collapsed = document.body.classList.toggle('sidebar-collapsed'); store('jarvis.hub.sidebarCollapsed', collapsed ? '1' : '0'); toggle.setAttribute('aria-expanded', String(!collapsed)); }
}
document.addEventListener('keydown', event => {
  const key = event.key, mod = event.ctrlKey || event.metaKey;
  if (mod && !event.shiftKey && !event.altKey && key.toLowerCase() === 'k') { event.preventDefault(); openSearchChats(); return; }
  if (mod && event.shiftKey && key.toLowerCase() === 'o') { event.preventDefault(); newChat(); return; }
  if (event.shiftKey && key === 'Escape') { event.preventDefault(); closeMenus(); focusComposer(); return; }
  // Slash-command list: arrows move, Enter or Tab runs, Esc closes.
  if (slash.open && event.target.id === 'chat-request') {
    if (key === 'ArrowDown' || key === 'ArrowUp') { event.preventDefault(); slash.sel = (slash.sel + (key === 'ArrowDown' ? 1 : -1) + slash.items.length) % Math.max(1, slash.items.length); $('#item-menu').innerHTML = slashHtml(); return; }
    if ((key === 'Enter' || key === 'Tab') && slash.items.length && !event.shiftKey) { event.preventDefault(); runSlash(slash.items[slash.sel]); return; }
    if (key === 'Escape') { event.preventDefault(); closeMenus(); return; }
  }
  if (key === 'Escape') {
    if (dictation.active) stopDictation();
    if (event.target.id === 'edit-body') { ui.editing = null; repaint(); return; }
    closeMenus(); document.body.classList.remove('sidebar-open');
    if (innerWidth <= WIDE_PANEL) document.body.classList.remove('details-open');
  }
  // Arrow keys move through an open menu.
  if ((key === 'ArrowDown' || key === 'ArrowUp') && event.target.closest?.('.menu')) {
    const items = [...event.target.closest('.menu').querySelectorAll('button:not([disabled]), a')];
    const at = items.indexOf(event.target);
    if (at >= 0) { event.preventDefault(); items[(at + (key === 'ArrowDown' ? 1 : -1) + items.length) % items.length].focus(); }
  }
  if ((event.target.id === 'chat-request' || event.target.id === 'room-request') && key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    const form = event.target.form;
    if (!form.querySelector('.send-button')?.disabled) form.requestSubmit();
  }
  if (event.target.id === 'edit-body' && key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendEdit(); }
});
document.addEventListener('click', async event => {
  if (!event.target.closest('.menu, .more, [aria-haspopup], #agent-picker')) closeMenus();
  // Tapping outside an open drawer closes it.
  if (document.body.classList.contains('sidebar-open') && !event.target.closest('#hub-sidebar, #sidebar-toggle, .menu, dialog')) { document.body.classList.remove('sidebar-open'); $('#sidebar-toggle').setAttribute('aria-expanded', 'false'); }
  if (innerWidth <= WIDE_PANEL && document.body.classList.contains('details-open') && !event.target.closest('#details, #details-toggle, .menu, dialog, [data-app-show], [data-app-start], [data-rp-open]')) document.body.classList.remove('details-open');
  const scrollLink = event.target.closest('[data-scroll-to]');
  if (scrollLink) ui.scrollTo = scrollLink.dataset.scrollTo;
  // Like ChatGPT, a click anywhere on the composer pill puts the cursor in the message box.
  if (event.target.closest('.composer') && !event.target.closest('button, textarea, input, a, .file-card, .img-chip')) focusComposer();
  const pick = event.target.closest('[data-pick]');
  if (pick && pick.dataset.pick !== selectedId) { selectedId = pick.dataset.pick; store('jarvis.hub.agent', selectedId); agentData = null; }
  const el = event.target.closest('button');
  if (!el) return;
  const d = el.dataset, a = selected(), data = agentData;
  if (el.id === 'agent-picker') { const menu = $('#agent-menu'); const open = menu.hidden; closeMenus(); if (open && overview) { menu.innerHTML = pickerMenu(); menu.hidden = false; el.setAttribute('aria-expanded', 'true'); } return; }
  if (d.pickAgent) { pickAgent(d.pickAgent); return; }
  if (el.id === 'sidebar-toggle' || el.id === 'sidebar-collapse') { toggleSidebar(); return; }
  if (el.id === 'details-toggle') { if (panelOpen()) closePanel(); else openPanel(); return; }
  if (el.id === 'rp-close') { closePanel(); return; }
  if (d.rpTab) { ui.rpTab = d.rpTab; store('jarvis.hub.rpTab', d.rpTab); renderDetails(); return; }
  if (el.id === 'profile-button') { openMenu(el, profileMenu(), {align: 'left', above: true}); return; }
  if (el.id === 'share-button') { openMenu(el, exportMenu()); return; }
  if (d.searchChats !== undefined) { openSearchChats(); return; }
  if (d.newRoom !== undefined) { closeMenus(); openRoomDialog(route.name === 'agent' ? route.id : selectedId); return; }
  if (d.threadStop) { el.disabled = true; await act(api(`/api/threads/${encodeURIComponent(d.threadStop)}/stop`, {}), 'Stop requested.'); return; }
  if (d.roomStop) { el.disabled = true; await act(api(`/api/rooms/${encodeURIComponent(d.roomStop)}/stop`, {}), 'Room stopped.'); return; }
  if (d.roomResume) { el.disabled = true; await act(api(`/api/rooms/${encodeURIComponent(d.roomResume)}/resume`, {}), 'Resuming the room.'); return; }
  if (d.detailsTab) { ui.detailsTab = d.detailsTab; store('jarvis.hub.detailsTab', d.detailsTab); renderDetails(); return; }
  if (d.close !== undefined) { el.closest('dialog').close(); return; }
  if (d.help !== undefined) { closeMenus(); helpDialog(); return; }
  if (el.id === 'new-agent' || d.newAgent !== undefined) { closeMenus(); openNewAgent(); return; }
  if (d.appShow) { const p = appPanel.previews.get(d.appShow); if (p) { dismissedApps.delete(p.preview_id); rememberApps(); appPanel.id = null; showApp(p); openPanel('preview'); } return; }
  if (d.appStop) { await appCommand(d.appStop, 'stop'); return; }
  if (d.appStart) { await appCommand(d.appStart, 'start'); return; }
  if (el.id === 'app-panel-close') { closeApp(); return; }
  if (el.id === 'app-panel-reload') { const frame = $('#app-panel-frame iframe'); if (frame) frame.src = appPanel.url; return; }
  if (el.id === 'app-panel-power') { await appCommand(el.dataset.id, el.dataset.state === 'RUNNING' ? 'stop' : 'start'); return; }
  if (d.previewPick) { ui.previewKey = d.previewPick; if (d.previewPick.startsWith('app:')) { dismissedApps.delete(d.previewPick.slice(4)); rememberApps(); } renderDetails(); return; }
  if (d.previewDevice) { ui.previewDevice = d.previewDevice; store('jarvis.hub.previewDevice', d.previewDevice); renderDetails(); return; }
  if (d.changeFile) { ui.changesSel = d.changeFile; renderDetails(); return; }
  if (d.openPath) { openProjectFile(d.openPath); return; }
  // Thread actions
  if (d.copyCode !== undefined) { const code = el.closest('.code-block')?.querySelector('pre code')?.textContent || ''; if (await copyText(code)) flashCopied(el); else toast('Could not copy.', true); return; }
  const messageText = m => m.role === 'operator' ? typedText(m) : replyText(m);
  if (d.copyMsg !== undefined) { const m = findMessage(d.copyMsg); if (m && await copyText(messageText(m))) flashCopied(el); else toast('Could not copy.', true); return; }
  if (d.copyPlain !== undefined) { closeMenus(); const m = findMessage(d.copyPlain); if (m && await copyText(plain(messageText(m), 1e6))) toast('Copied as plain text.'); return; }
  if (d.feedback) { const at = d.feedback.lastIndexOf('|'); await giveFeedback(d.feedback.slice(0, at), d.feedback.slice(at + 1)); return; }
  if (d.speak !== undefined) { speak(d.speak); return; }
  if (d.regenerate !== undefined && pageData && route.chat) { el.disabled = true; const done = await act(api(`/api/agents/${encodeURIComponent(pageData.agent_id)}/chats/${encodeURIComponent(route.chat)}/regenerate`, {request_id: newRequestId()}), 'Regenerating the last reply.'); if (!done && el.isConnected) el.disabled = false; return; }
  if (d.renameChat) { closeMenus(); beginRename(d.renameChat, d.renameWhere || 'side'); return; }
  if (d.msgMore !== undefined) { openMenu(el, `${exportMenu()}<hr><button type="button" role="menuitem" data-copy-plain="${esc(d.msgMore)}">${ICONS.copy}<span>Copy as plain text</span></button>`); return; }
  if (d.export) { closeMenus(); await exportChat(d.export); return; }
  if (d.editMsg !== undefined) {
    const m = findMessage(d.editMsg); if (!m || !m.task_id) return;
    ui.editing = {hash: location.hash, id: m.message_id, text: typedText(m)}; repaint();
    const box = $('#edit-body'); if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
    return;
  }
  if (d.editCancel !== undefined) { ui.editing = null; repaint(); return; }
  if (d.editSend !== undefined) { await sendEdit(); return; }
  if (d.openFile) { const at = d.openFile.lastIndexOf('|'); const f = findMessage(d.openFile.slice(0, at))?.files?.[Number(d.openFile.slice(at + 1))]; if (f) { if (f.artifact_id) openArtifact(f.artifact_id); else openProjectFile(f.path || f.name, f); } return; }
  if (d.openImage) { const url = turnImageUrls.get(d.openImage); if (url && url !== 'error') { $('#preview-title').textContent = 'Image'; $('#preview-meta').textContent = ''; $('#preview-tabs').innerHTML = ''; $('#preview-body').innerHTML = `<div class="viewer"><img alt="" src="${esc(url)}"></div>`; $('#preview-dialog').showModal(); } return; }
  if (d.toBottom !== undefined) { view.scrollTo({top: view.scrollHeight, behavior: reducedMotion() ? 'auto' : 'smooth'}); return; }
  if (d.dictate !== undefined) { if (dictation.active) stopDictation(); else startDictation(); return; }
  if (d.slashMenu !== undefined) { if (slash.open) closeMenus(); else { slash.sel = 0; openSlash('', el); } return; }
  if (d.slash) { await runSlash(d.slash); return; }
  // Composer
  if (d.starter !== undefined || d.prefill !== undefined) { closeMenus(); const box = $('#chat-request'); if (box) { box.value = d.starter ?? d.prefill; grow(box); box.focus(); box.setSelectionRange(box.value.length, box.value.length); drafts.set(location.hash, {body: box.value}); } return; }
  if (d.composerMenu !== undefined) { openMenu(el, composerMenu(), {align: 'left', above: true}); return; }
  if (d.modelMenu !== undefined) { const target = pageData && pageData.agent_id === selectedId ? pageData : null; if (target) openMenu(el, modelMenu(target), {align: el.classList.contains('model-switch') ? 'left' : undefined}); return; }
  if (d.effortMenu !== undefined && pageData) { openMenu(el, effortMenu(pageData)); return; }
  if (d.setModel && pageData) {
    closeMenus();
    const [provider, model] = d.setModel.split('|');
    await act(api(`/api/agents/${pageData.agent_id}/config`, {provider, model, permissions: pageData.permissions}), `${prettyModel(model)} for new messages.`);
    return;
  }
  if (d.setEffort && pageData) { closeMenus(); await act(api(`/api/agents/${pageData.agent_id}/effort`, {effort: d.setEffort}), `Effort: ${effortLabel(d.setEffort)} for new messages.`); return; }
  if (d.usagePin !== undefined) { el.closest('.usage-wrap')?.classList.toggle('pinned'); return; }
  if (d.compact !== undefined && pageData && route.chat) { await compactChat(); return; }
  // Settings
  if (d.themeSet) { store('jarvis.hub.theme', d.themeSet); applyTheme(d.themeSet); repaintSettings(); return; }
  if (d.personalReset !== undefined) { ui.personalDraft = null; repaintSettings(); return; }
  // Conversation, task, goal and job menus
  if (d.chatMenu && a) {
    const chat = (data?.chats || []).find(c => c.chat_id === d.chatMenu); if (!chat) return;
    openMenu(el, `<button type="button" data-rename-chat="${esc(chat.chat_id)}" data-rename-where="side">${ICONS.pencil}<span>Rename</span></button><button type="button" data-chat-archive="${esc(chat.chat_id)}" data-value="${chat.archived ? 'false' : 'true'}">${ICONS.artifacts}<span>${chat.archived ? 'Restore' : 'Archive'}</span></button><hr><button type="button" class="danger" data-chat-delete="${esc(chat.chat_id)}">${ICONS.close}<span>Delete…</span></button>`);
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
  if (d.attach !== undefined) { closeMenus(); $('#attach-input')?.click(); return; }
  if (d.unattach !== undefined) { const list = pendingUploads.get(location.hash) || []; const [gone] = list.splice(Number(d.unattach), 1); if (gone?.preview) URL.revokeObjectURL(gone.preview); pendingUploads.set(location.hash, list); refreshAttachRow(); return; }
  if (d.connAdd) { const preset = (pageData?.presets || []).find(p => p.id === d.connAdd); if (preset) connectionDialog(preset); return; }
  if (d.connTest) { el.disabled = true; el.textContent = 'Testing…'; await act(api(`/api/connections/${encodeURIComponent(d.connTest)}/test`, {}), 'Checked.'); lastHtml = ''; await renderRoute(true); return; }
  if (d.connConnect) { await startConnect(d.connConnect); return; }
  if (d.connToken) { tokenDialog(d.connToken); return; }
  if (d.connEnable) { const [id, state] = d.connEnable.split('|'); await act(api(`/api/connections/${encodeURIComponent(id)}/settings`, {enabled: state === 'on'}), state === 'on' ? 'Turned on.' : 'Turned off.'); lastHtml = ''; await renderRoute(true); return; }
  if (d.connDelete) { if (!confirm('Remove this connection? Its saved sign-in is deleted from this computer.')) return; await act(api(`/api/connections/${encodeURIComponent(d.connDelete)}/delete`, {}), 'Removed.'); lastHtml = ''; await renderRoute(true); return; }
  if (d.openrouterKey !== undefined) { openRouterKeyDialog(); return; }
  if (d.closeKey !== undefined) { el.closest('dialog')?.close(); return; }
  if (d.forgetOpenrouter !== undefined) { if (!confirm('Remove the OpenRouter key? Agents on OpenRouter models will wait until a key is added again.')) return; await act(api('/api/providers/openrouter/forget-key', {}), 'OpenRouter key removed.'); return; }
  if (el.id === 'refresh-providers') { await act(api('/api/providers/refresh', {}), 'Providers re-checked.'); return; }
  if (d.newProject !== undefined) { const name = prompt('Project name'); if (name) await act(api('/api/projects', {name}), 'Project created.'); }
});
document.addEventListener('input', event => {
  const target = event.target;
  const composer = target.closest('#chat-form, #steer-form, #room-form');
  if (composer) drafts.set(location.hash, Object.fromEntries(new FormData(composer)));
  if (target.id === 'chat-request') {
    grow(target);
    if (/^\/\w*$/.test(target.value)) openSlash(target.value.slice(1));
    else if (slash.open) closeMenus();
  }
  if (target.id === 'room-request') grow(target);
  if (target.closest?.('#room-new-form') && !$('#room-error').hidden) showRoomError('');
  if (target.id === 'edit-body' && ui.editing) { ui.editing.text = target.value; grow(target); }
  if (target.closest?.('#personalization')) { const form = target.closest('form'); ui.personalDraft = {about_you: form.elements.about_you.value, response_style: form.elements.response_style.value}; }
  if (target.id === 'artifact-q') {
    ui.artifactQuery = target.value; const pos = target.selectionStart;
    lastHtml = ''; paint(renderAgent(pageData)); const box = $('#artifact-q'); box?.focus(); box?.setSelectionRange(pos, pos);
  }
});
document.addEventListener('change', event => {
  if (event.target.id === 'attach-input') { addFiles([...event.target.files]); event.target.value = ''; return; }
  if (event.target.name === 'member' && event.target.closest('#room-new-form')) {
    const id = event.target.value;
    roomPick.order = roomPick.order.filter(x => x !== id);
    if (event.target.checked) roomPick.order.push(id);
    syncChair(); showRoomError('');
    return;
  }
  const composer = event.target.closest('#chat-form, #steer-form, #room-form');
  if (composer) drafts.set(location.hash, Object.fromEntries(new FormData(composer)));
  const id = event.target.id;
  if (id === 'a-agent') { activityFilter.agent = event.target.value; lastHtml = ''; paint(renderActivity()); }
  if (id === 'a-detail') { activityFilter.detail = event.target.checked; lastHtml = ''; paint(renderActivity()); }
  if (event.target.dataset?.connAsk) { act(api(`/api/connections/${encodeURIComponent(event.target.dataset.connAsk)}/settings`, {ask: event.target.value}), 'Saved.'); return; }
  if (id === 'cfg-provider') { const models = pageData.known_models[event.target.value] || []; $('#cfg-model').innerHTML = models.map(m => modelOption(event.target.value, m, null)).join(''); }
});
document.addEventListener('submit', async event => {
  const form = event.target;
  if (form.id === 'chat-form') {
    event.preventDefault();
    const sourceRoute = location.hash;
    if (sendingRoutes.has(sourceRoute)) return;
    if (dictation.active) stopDictation();
    closeMenus();
    const agentId = route.id;
    const uploads = pendingUploads.get(sourceRoute) || [];
    const pick = kind => uploads.filter(u => u.kind === kind).map(({name, mime, data}) => ({name, mime, data}));
    const images = pick('image'), files = pick('file');
    // Attachments may go on their own: the body is then empty.
    const body = String(new FormData(form).get('body') || '').trim();
    if (!body && !images.length && !files.length) return;
    sendingRoutes.add(sourceRoute);
    form.classList.add('sending');
    try {
      let chatId = route.chat;
      if (!chatId) {
        const chat = await api(`/api/agents/${agentId}/chats`, {title: body.slice(0, 80) || uploads[0]?.name?.slice(0, 80) || 'New chat'});
        chatId = chat.chat_id;
      }
      const payload = {chat_id: chatId, body, request_id: newRequestId()};
      if (images.length) payload.images = images;
      if (files.length) payload.files = files;
      await api(`/api/agents/${agentId}/messages`, payload);
      clearUploads(sourceRoute);
      drafts.delete(sourceRoute); lastHtml = '';
      if (location.hash === sourceRoute) {
        form.reset();
        location.hash = `#/agents/${agentId}/work/${chatId}`;
        await renderRoute(true);
      }
    } catch (error) { toast(error.message, true); }
    finally { sendingRoutes.delete(sourceRoute); form.classList.remove('sending'); }
  }
  if (form.id === 'room-form') {
    event.preventDefault();
    const source = location.hash, body = String(new FormData(form).get('body') || '').trim();
    if (!body || sendingRoutes.has(source) || route.name !== 'room') return;
    sendingRoutes.add(source);
    try { await api(`/api/rooms/${encodeURIComponent(route.id)}/messages`, {body}); drafts.delete(source); form.reset(); lastHtml = ''; await renderRoute(true); }
    catch (error) { toast(error.message, true); }
    finally { sendingRoutes.delete(source); }
    return;
  }
  if (form.id === 'room-new-form') {
    event.preventDefault();
    const data = new FormData(form);
    const member_ids = roomPick.order.filter(id => [...form.querySelectorAll('input[name=member]')].some(i => i.value === id && i.checked));
    const topic = String(data.get('topic') || '').trim(), title = String(data.get('title') || '').trim(), chair = String(data.get('chair_id') || '');
    if (member_ids.length < 2) { showRoomError('Pick at least two agents for the room.'); return; }
    if (!topic) { showRoomError('Add a topic for the room to discuss.'); form.elements.topic.focus(); return; }
    const payload = {topic, member_ids};
    if (title) payload.title = title;
    if (chair && member_ids.includes(chair)) payload.chair_id = chair;
    const button = form.querySelector('button.primary'); button.disabled = true;
    try { const room = await api('/api/rooms', payload); $('#room-dialog').close(); toast('Room started.'); location.hash = `#/rooms/${room.room_id}`; }
    catch (error) { showRoomError(error.message); }
    finally { button.disabled = false; }
    return;
  }
  if (form.id === 'personalization') {
    event.preventDefault();
    const values = {about_you: String(form.elements.about_you.value || ''), response_style: String(form.elements.response_style.value || '')};
    document.activeElement?.blur();
    const saved = await act(api('/api/personalization', values), 'Saved. Agents use this from their next message.');
    if (saved) { ui.personalDraft = null; if (pageData?.audit) pageData.personalization = {...values, ...(saved && typeof saved === 'object' ? saved : {})}; repaintSettings(); }
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
  if (form.id === 'connection-form') {
    event.preventDefault();
    const data = new FormData(form), preset = form.dataset.preset, body = {preset};
    for (const key of ['name', 'url', 'token']) { const v = String(data.get(key) || '').trim(); if (v) body[key] = v; }
    const secret = String(data.get('secret') || '').trim();
    if (secret) { if (/^https?:\/\//i.test(secret)) body.url = secret; else body.token = secret; }
    const command = String(data.get('command') || '').trim();
    if (command) body.command = command.match(/"[^"]*"|\S+/g).map(part => part.replace(/^"|"$/g, ''));
    const env = String(data.get('env') || '').trim();
    if (env) body.env = Object.fromEntries(env.split(/\r?\n/).filter(line => line.includes('=')).map(line => [line.slice(0, line.indexOf('=')).trim(), line.slice(line.indexOf('=') + 1).trim()]));
    form.querySelectorAll('input[type=password]').forEach(i => { i.value = ''; });
    const added = await act(api('/api/connections', body), null);
    if (!added) return;
    form.closest('dialog')?.close();
    if (added.auth === 'oauth' && !added.signed_in) await startConnect(added.id);
    else toast(added.status === 'connected' ? `${added.name} connected · ${added.tools.length} tools.` : `${added.name} added: ${added.error || added.status}`, added.status !== 'connected');
    lastHtml = ''; await renderRoute(true);
    return;
  }
  if (form.id === 'connection-token-form') {
    event.preventDefault();
    const field = form.querySelector('input[name="token"]'), token = field.value.trim(); field.value = '';
    const saved = await act(api(`/api/connections/${encodeURIComponent(form.dataset.id)}/settings`, {token}), null);
    if (saved) { form.closest('dialog')?.close(); toast(saved.status === 'connected' ? 'Token accepted.' : `Not connected: ${saved.error || saved.status}`, saved.status !== 'connected'); lastHtml = ''; await renderRoute(true); }
    return;
  }
  if (form.id === 'openrouter-key-form') {
    event.preventDefault();
    const field = form.querySelector('input[name="key"]'), key = field.value.trim();
    field.value = '';
    const saved = await act(api('/api/providers/openrouter/key', {key}), 'OpenRouter key saved and accepted.');
    if (saved) form.closest('dialog')?.close();
    return;
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
  $('#agent-model').innerHTML = models.map(m => modelOption(provider, m, preferred)).join('');
  updateModelNote();
}
function updateModelNote() {
  const provider = $('#agent-provider').value;
  const p = overview.providers[provider];
  const model = $('#agent-model').value, facts = overview.openrouter_models?.[model];
  $('#agent-model-note').textContent = provider === 'openrouter'
    ? (!p?.authenticated ? 'Add your OpenRouter API key in Settings first.' : `Runs on OpenRouter with your key${facts ? ` · ${facts.free ? 'free model' : 'paid model (uses your credits)'}${facts.context_length ? ` · ${Math.round(facts.context_length / 1000)}k context` : ''}` : ''}. What the agent sends goes to OpenRouter and the model's host.`)
    : !p?.authenticated ? `${providerName(provider)} is not signed in on the backend host.` : 'Agent execution uses the selected subscription and the abilities you grant below.';
}
function openNewAgent() {
  if (!overview) return;
  const d = overview.defaults, form = $('#agent-form');
  form.reset();
  $('#agent-provider').innerHTML = providerOptions(d.provider);
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
      if (/^image\/(png|jpeg|gif|webp)$/.test(a.mime)) { const url = URL.createObjectURL(await apiBlob(`/api/artifacts/${a.artifact_id}/content`)); viewerUrls.push(url); body.innerHTML = `<div class="viewer"><img alt="${esc(a.path)}" src="${url}"></div>`; return; }
      if (a.mime.startsWith('text/') || /\.(md|txt|py|js|ts|json|csv|css|html|yml|yaml|toml|sh|ps1)$/i.test(a.path)) {
        const text = await (await apiBlob(`/api/artifacts/${a.artifact_id}/content`)).text();
        const ext = (a.path.match(/\.([a-z0-9]+)$/i) || [])[1] || '';
        body.innerHTML = /\.md$/i.test(a.path) ? `<div class="result">${md(text)}</div>` : /\.txt$/i.test(a.path) ? `<pre>${esc(text)}</pre>` : `<div class="result">${codeBlock(`${ext}\n${text}`)}</div>`;
        return;
      }
      body.innerHTML = '<p class="muted">No inline preview for this file type. Use Download.</p>';
    };
    $('#preview-tabs').onclick = async e => {
      const b = e.target.closest('button'); if (!b) return;
      if (b.dataset.ptab) show(b.dataset.ptab);
      if (b.dataset.download) { const blob = await apiBlob(`/api/artifacts/${a.artifact_id}/content?download=1`); download(blob, a.path.split('/').pop()); }
    };
    await show('preview');
    if (!$('#preview-dialog').open) $('#preview-dialog').showModal();
  } catch (error) { toast(error.message, true); }
}

// ---------- workspace panel resizing ----------
(function panelResizer() {
  const handle = $('#rp-resizer'), root = document.documentElement;
  const apply = width => {
    const max = Math.max(320, Math.min(1100, innerWidth - 520));
    const w = Math.round(Math.max(300, Math.min(max, width)));
    root.style.setProperty('--rp-w', `${w}px`);
    return w;
  };
  const saved = Number(store('jarvis.hub.rpWidth'));
  if (saved) apply(saved);
  handle.addEventListener('pointerdown', event => {
    if (innerWidth <= WIDE_PANEL) return;
    event.preventDefault(); handle.setPointerCapture(event.pointerId); document.body.classList.add('resizing');
    const move = e => store('jarvis.hub.rpWidth', String(apply(innerWidth - e.clientX)));
    const up = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); handle.removeEventListener('pointercancel', up); document.body.classList.remove('resizing'); };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up); handle.addEventListener('pointercancel', up);
  });
  handle.addEventListener('keydown', event => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    store('jarvis.hub.rpWidth', String(apply($('#details').getBoundingClientRect().width + (event.key === 'ArrowLeft' ? 24 : -24))));
  });
})();

// ---------- start ----------
// The workspace panel starts closed on a first visit; the choice is remembered after that.
if (store('jarvis.hub.detailsClosed') !== '0') document.body.classList.add('details-closed');
if (store('jarvis.hub.sidebarCollapsed') === '1' && innerWidth > 900) document.body.classList.add('sidebar-collapsed');
route = parseRoute();
tick(true).then(schedule);
