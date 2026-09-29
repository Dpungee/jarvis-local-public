// ChatGPT-parity UI checks for the Agent Hub with synthetic API data. Never executes a real agent.
// Serves the static files under the Hub's real Content-Security-Policy, stubs every API route the
// page calls (any other call fails the run), and asserts each parity item plus the agent
// workspace (top bar, inline tool rows, workspace panel tabs, slash commands).
// Usage: node scripts/verify_hub_chatgpt_parity.cjs [screenshot-dir]
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const zlib = require('node:zlib');

const root = path.resolve(__dirname, '../jarvis/agent_hub_static');
const shots = path.resolve(process.argv[2] || path.join(os.tmpdir(), 'jarvis-hub-parity-shots'));
fs.mkdirSync(shots, {recursive: true});
const BASE = 'http://localhost:8799';
const CSP = "default-src 'self'; img-src 'self' blob: https:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-src http://127.0.0.1:* http://localhost:* http://[::1]:*; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

// A real PNG (a small bar chart) so the inline image path is exercised end to end.
function png(width, height, paint) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (width * 3 + 1)] = 0;
    for (let x = 0; x < width; x++) raw.set(paint(x, y), y * (width * 3 + 1) + 1 + x * 3);
  }
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type), data]);
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(body) >>> 0);
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(width, 0); ihdr.writeUInt32BE(height, 4); ihdr.set([8, 2, 0, 0, 0], 8);
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}
const bars = [0.35, 0.62, 0.48, 0.84, 0.71, 0.93];
const coverPng = png(320, 200, (x, y) => [Math.round(40 + x / 2), Math.round(90 + y / 3), 200 - Math.round(x / 4)]);
const chartPng = png(480, 300, (x, y) => {
  const i = Math.floor((x - 30) / 72), inBar = x >= 30 && i < bars.length && (x - 30) % 72 < 50;
  if (y > 270 && y < 273 && x > 20 && x < 460) return [90, 90, 100];
  if (inBar && y < 270 && y > 270 - bars[i] * 240) return i === 5 ? [16, 163, 127] : [51, 156, 255];
  return (y % 60 === 0 && x > 20) ? [236, 236, 240] : [250, 250, 252];
});

(async () => {
  const now = Date.now() / 1000;
  const T0 = now - 3600;
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 900}, colorScheme: 'dark', acceptDownloads: true});
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: BASE});
  // Speech stubs: a recognizer that dictates "hello world", and a recorder for read-aloud.
  await context.addInitScript(() => {
    window.__speech = {spoken: [], cancelled: 0};
    class FakeRecognition {
      start() { setTimeout(() => this.onresult?.({results: [Object.assign([{transcript: 'hello wor'}], {isFinal: false})]}), 30);
        setTimeout(() => this.onresult?.({results: [Object.assign([{transcript: 'hello world'}], {isFinal: true})]}), 80); }
      stop() { setTimeout(() => this.onend?.(), 0); }
    }
    window.SpeechRecognition = window.webkitSpeechRecognition = FakeRecognition;
    Object.defineProperty(window, 'speechSynthesis', {configurable: true, value: {speak: u => window.__speech.spoken.push(u.text), cancel: () => { window.__speech.cancelled++; }}});
  });
  const page = await context.newPage();
  const errors = [], csp = [];
  page.on('pageerror', e => errors.push(e.message));
  // Only real violations count; Chromium also warns that the header's http://[::1]:* source is invalid.
  page.on('console', m => { if (/Refused to/i.test(m.text())) csp.push(m.text()); });
  page.on('dialog', d => { if (d.type() === 'alert') errors.push(`alert: ${d.message()}`); d.accept(); });

  // ---------- synthetic state ----------
  const calls = [];
  const agent = {agent_id: 'atlas', name: 'Atlas', role: 'Research assistant', purpose: 'Explore ideas.', project_id: 'research', project_name: 'research', provider: 'codex-cli', model: 'gpt-5.6-sol',
    lifecycle: 'RUNNING', archived: false, effort: 'auto', effort_levels: ['low', 'medium', 'high'], status: {code: 'idle', label: 'Ready', detail: 'Ready for a message', tone: 'ok'},
    counts: {running: 0, queued: 0, blocked: 0, completed: 3, failed: 0}, permissions: {web: true, files: true, programs: true, memory: true}, workspace: {folder: 'research', git_branch: 'main', git_dirty: true}};
  const permission_labels = {web: 'Search and read the web', files: 'Read and write project files', programs: 'Run programs', memory: 'Remember things'};
  const overview = {agents: [agent], projects: [{project_id: 'research', name: 'research'}], providers: {'codex-cli': {installed: true, authenticated: true, models: [{model: 'gpt-5.6-sol', verified: true}], detail: 'ok'}},
    defaults: {provider: 'codex-cli', model: 'gpt-5.6-sol', check: {verified: true}}, capacity: {running: 0, slots: 2}, server_time: now, known_models: {'codex-cli': ['gpt-5.6-sol', 'gpt-5.5'], 'claude-cli': ['claude-sonnet-4-5']},
    permissions: {labels: permission_labels, defaults: {}}, features: {archive: true, goals: true, search: true, artifacts: true, schedules: true, effort: true, usage: true},
    effort_labels: {auto: 'Auto', low: 'Low', medium: 'Medium', high: 'High'}, goal_categories: {health: 'Health', other: 'Something else'}, last_seq: 0};
  const reply1 = [
    '## Summary', '', 'The report tracks **six releases**; the fix normalises Unicode in `slugify()` before stripping. See [the spec](https://example.com/spec) and https://docs.python.org/3/library/unicodedata.html.', '',
    '```python', 'import re, unicodedata', '', 'def slugify(text: str) -> str:', '    # Fold accents, then keep word characters', '    text = unicodedata.normalize("NFKD", text)', '    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]  # at most 80 characters', '```', '',
    '```js', 'const total = items.reduce((sum, x) => sum + x.value, 0); // running total', 'console.log(`total: ${total}`, null, 3.14);', '```', '',
    '| Release | Tests | Status |', '|---|---|---|', '| 0.6.1 | 212 | pass |', '| 0.6.2 | 230 | **pass** |', '| 0.6.3 | 241 | pass |', '',
    'The growth rate is \\(r = \\frac{\\Delta y}{\\Delta x}\\) and the total is:', '', '$$\\sum_{i=1}^{n} x_i^2 \\leq \\pi \\cdot n$$', '',
    '- Unicode inputs now fold to ASCII', '- Tests cover *accents* and emoji', '', 'Untrusted text stays text: <img src=x onerror=alert(1)> and <script>alert(2)</script>.'].join('\n');
  const conv1 = {usage: {model: 'gpt-5.6-sol', provider: 'codex-cli', window: {tokens: 400000, source: 'catalog'}, last_turn: {context_tokens: 52000, context_window: 400000, calls: 3, peak_context_tokens: 52000, output_tokens: 1800}, history: {chars: 60000, compact_at_chars: 800000}},
    // Legacy messages (from before the Hub) come first and carry no message_id or task_id.
    messages: [
      {role: 'operator', body: 'Hello from before the Hub.', state: 'COMPLETED'},
      {role: 'assistant', body: 'Hi. This reply predates the Hub.', state: 'COMPLETED'},
      {role: 'operator', body: 'Summarize the attached report and fix slugify for Unicode.\n\n📎 Attached: report.pdf, results.csv', text: 'Summarize the attached report and fix slugify for Unicode.', state: 'APPLIED', message_id: 't1-user', task_id: 't1', created_at: T0,
        files: [{path: 'uploads/2026-09-27/report.pdf', name: 'report.pdf', size: 482113, mime: 'application/pdf', artifact_id: 'art_up1'}, {path: 'uploads/2026-09-27/results.csv', name: 'results.csv', size: 2048, mime: 'text/csv', artifact_id: 'art_up2'}]},
      {role: 'assistant', body: reply1, state: 'COMPLETED', message_id: 't1-assistant', task_id: 't1', created_at: T0 + 73, feedback: null},
      {role: 'operator', body: 'Make a chart of the test counts.', text: 'Make a chart of the test counts.', state: 'APPLIED', message_id: 't2-user', task_id: 't2', created_at: T0 + 100, files: []},
      {role: 'assistant', body: 'Here is the chart of test counts per release. The latest release is highlighted. [[jarvis-image:charts/test_counts.png]]\n\nDone — I also created a cover picture. [[jarvis-image:images/cover.png]]', state: 'COMPLETED', message_id: 't2-assistant', task_id: 't2', created_at: T0 + 131, feedback: {rating: 'up', note: null, at: T0 + 140}}],
    agent_turns: [
      {task_id: 't1', state: 'COMPLETED', request: 'Summarize the attached report and fix slugify for Unicode.', title: 'Summarize the report', created_at: T0, started_at: T0 + 1, finished_at: T0 + 73, tool_calls: 7, actions: [], steps: [], previews: [],
        artifacts: [{artifact_id: 'art_a', task_id: 't1', path: 'src/slugify.py', change: 'modified', version: 2, size: 812, mime: 'text/x-python', created_at: T0 + 50, has_diff: true},
          {artifact_id: 'art_b', task_id: 't1', path: 'tests/test_slugify.py', change: 'created', version: 1, size: 420, mime: 'text/x-python', created_at: T0 + 60, has_diff: true}]},
      {task_id: 't2', state: 'COMPLETED', request: 'Make a chart of the test counts.', title: 'Chart', created_at: T0 + 100, started_at: T0 + 101, finished_at: T0 + 131, tool_calls: 2, actions: [], steps: [], previews: [],
        artifacts: [{artifact_id: 'art_img1', task_id: 't2', path: 'charts/test_counts.png', change: 'created', version: 1, size: chartPng.length, mime: 'image/png', created_at: T0 + 130, has_diff: false}],
        images: [{artifact_id: 'art_img1', path: 'charts/test_counts.png', url: '/api/artifacts/art_img1/content', mime: 'image/png'}]}],
    live_turns: []};
  const ev = (seq, task_id, summary, detail, level = 'info', kind = 'tool') => ({seq, ts: T0 + seq - 100, agent_id: 'atlas', task_id, project_id: 'research', kind, level, summary, detail});
  // Tool events carry detail.args, and for written files path plus (after the run) artifact_id.
  const t1Events = [ev(101, 't1', 'read_document uploads/2026-09-27/report.pdf', {tool: 'read_document', ok: true, ms: 40, args: 'uploads/2026-09-27/report.pdf'}), ev(102, 't1', 'read_file src/slugify.py', {tool: 'read_file', ok: true, ms: 3, args: 'src/slugify.py'}),
    ev(103, 't1', 'read_file tests/test_utils.py', {tool: 'read_file', ok: true, ms: 2, args: 'tests/test_utils.py'}), ev(104, 't1', 'searched “python unicodedata normalize NFKD” → 6 results', {tool: 'web_search', ok: true, ms: 900, args: 'python unicodedata normalize NFKD'}),
    ev(105, 't1', 'edit_file src/slugify.py', {tool: 'edit_file', ok: true, ms: 5, args: 'src/slugify.py', path: 'src/slugify.py', artifact_id: 'art_a', change: 'modified', has_diff: true}),
    ev(106, 't1', 'write_file tests/test_slugify.py', {tool: 'write_file', ok: true, ms: 4, args: 'tests/test_slugify.py', path: 'tests/test_slugify.py', artifact_id: 'art_b', change: 'created', has_diff: true}),
    ev(107, 't1', 'ran `python -m pytest -q` → exit 0', {tool: 'run_process', ok: true, ms: 4100, args: 'python -m pytest -q'})];
  const conv2 = {usage: conv1.usage,
    messages: [
      {role: 'operator', body: 'Build me a snake game and check it in the browser.', text: 'Build me a snake game and check it in the browser.', state: 'APPLIED', message_id: 't4-user', task_id: 't4', files: []},
      {role: 'assistant', body: 'The game is running. Use the arrow keys to play.', state: 'COMPLETED', message_id: 't4-assistant', task_id: 't4', feedback: null},
      {role: 'operator', body: 'Now add a high-score table and run the tests.', text: 'Now add a high-score table and run the tests.', state: 'APPLIED', message_id: 't5-user', task_id: 't5', files: []},
      {role: 'assistant', body: 'Working…', state: 'RUNNING', message_id: 't5-assistant', task_id: 't5', feedback: null}],
    agent_turns: [
      {task_id: 't4', state: 'COMPLETED', request: 'Build me a snake game', title: 'Snake game', created_at: T0 + 200, started_at: T0 + 201, finished_at: T0 + 296, tool_calls: 5, actions: [], steps: [], artifacts: [],
        previews: [{preview_id: 'pv1', title: 'Snake game', url: 'http://127.0.0.1:5173/', state: 'RUNNING', detail: 'Verified in the browser'}]},
      {task_id: 't5', state: 'RUNNING', request: 'Now add a high-score table and run the tests.', title: 'High scores', created_at: now - 40, started_at: now - 38, tool_calls: 4, actions: ['cancel', 'pause', 'steer'], artifacts: [], previews: [],
        progress: 'Running the tests', steps: [{kind: 'progress', summary: 'Task contract · understood', ts: now - 37}, {kind: 'tool', summary: 'read_file game.js', ts: now - 30}, {kind: 'progress', summary: 'Running the tests', ts: now - 5}]}],
    live_turns: []};
  // A running turn: its write has a path but no artifact_id yet (recorded when the run finishes).
  const t5Events = [ev(201, 't5', 'read_file game.js', {tool: 'read_file', ok: true, ms: 2, args: 'game.js'}), ev(202, 't5', 'read_file index.html', {tool: 'read_file', ok: true, ms: 2, args: 'index.html'}),
    ev(203, 't5', 'edit_file game.js', {tool: 'edit_file', ok: true, ms: 6, args: 'game.js', path: 'game.js'}), ev(204, 't5', 'ran `npm test` → exit 1', {tool: 'run_process', ok: false, ms: 2200, args: 'npm test'}, 'warn')];
  let regenerateCalls = 0;
  const chats = [{chat_id: 'chat1', title: 'Slugify fix and report summary', created_at: T0, last_at: now - 600, turns: 2, archived: false, active: false},
    {chat_id: 'chat2', title: 'Snake game', created_at: T0 + 200, last_at: now - 30, turns: 2, archived: false, active: true},
    {chat_id: 'chat0', title: 'Weekly planning', created_at: now - 9 * 86400, last_at: now - 9 * 86400, turns: 4, archived: false, active: false}];
  const detail = () => ({...agent, instructions: '', chats, tasks: [{task_id: 't5', title: 'High scores', request: 'Now add a high-score table', state: 'RUNNING', created_at: now - 40, updated_at: now - 5, chat_id: 'chat2', archived: false}],
    approvals: [], artifacts: [...conv1.agent_turns.flatMap(t => t.artifacts)], events: [...t1Events.slice(-2), ...t5Events], errors: [], providers: overview.providers, known_models: overview.known_models,
    permission_labels, goals: [{goal_id: 'g1', title: 'Ship 0.7', state: 'ACTIVE', category_label: 'Work', created_at: T0}], schedules: []});
  const galleryData = {made: [{path: 'src/slugify.py', name: 'slugify.py', kind: 'code', size: 812, created_at: T0 + 50, artifact_id: 'art_a', stored: true}, {path: 'charts/test_counts.png', name: 'test_counts.png', kind: 'images', size: chartPng.length, created_at: T0 + 130, artifact_id: 'art_img1', stored: true}],
    folder: [{path: 'uploads/2026-09-27/report.pdf', name: 'report.pdf', kind: 'documents', size: 482113, created_at: T0, on_disk: true}, {path: 'uploads/2026-09-27/results.csv', name: 'results.csv', kind: 'documents', size: 2048, created_at: T0, on_disk: true}, {path: 'README.md', name: 'README.md', kind: 'documents', size: 900, created_at: T0, on_disk: true}]};
  const diffs = {art_a: '--- a/src/slugify.py\n+++ b/src/slugify.py\n@@ -1,4 +1,7 @@\n-import re\n+import re\n+import unicodedata\n \n def slugify(text):\n+    text = unicodedata.normalize("NFKD", text)\n+    text = text.encode("ascii", "ignore").decode()\n     return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")',
    art_b: '--- /dev/null\n+++ b/tests/test_slugify.py\n@@ -0,0 +1,3 @@\n+from src.slugify import slugify\n+def test_accents():\n+    assert slugify("Café Déjà") == "cafe-deja"'};
  let personalization = {about_you: '', response_style: ''};

  await page.route('http://127.0.0.1:5173/**', route => route.fulfill({contentType: 'text/html', body: '<!doctype html><meta charset="utf-8"><title>Snake</title><body style="margin:0;background:#111;color:#7CFC00;font:20px monospace;display:grid;place-items:center;height:100vh">SNAKE ▮▮▮▮ score 12</body>'}));
  await page.route(`${BASE}/**`, async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname, method = request.method();
    if (!p.startsWith('/api/')) {
      const file = p === '/' ? 'index.html' : p.slice(1);
      assert(['index.html', 'hub.js', 'hub.css'].includes(file), `static ${file}`);
      return route.fulfill({body: fs.readFileSync(path.join(root, file)), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', headers: {'Content-Security-Policy': CSP}});
    }
    const body = method === 'POST' ? request.postDataJSON() : undefined;
    calls.push({method, path: p, search: url.search, body});
    const json = data => route.fulfill({json: data});
    if (p === '/api/overview') return json({...overview, server_time: Date.now() / 1000});
    if (p === '/api/events') {
      const task = url.searchParams.get('task');
      return json({events: task === 't1' ? t1Events : task === 't5' ? t5Events : task ? [] : [], last_seq: 0});
    }
    if (p === '/api/agents/atlas') return json(detail());
    if (p === '/api/agents/atlas/chat') return json(structuredClone(url.searchParams.get('chat') === 'chat2' ? conv2 : conv1));
    if (p === '/api/agents/atlas/usage') return json(conv1.usage);
    if (p === '/api/agents/atlas/artifacts') return json(galleryData);
    if (p === '/api/agents/atlas/search') return json({chats: [chats[0]], messages: [{chat_id: 'chat1', where: 'agent', title: 'Slugify fix and report summary', snippet: 'the fix normalises Unicode in slugify() before stripping', created_at: T0 + 73}]});
    if (p === '/api/agents/atlas/chats' && method === 'POST') return json({chat_id: 'chat3', title: body.title});
    if (p === '/api/agents/atlas/messages') return json({ok: true, task_id: 't9'});
    const chatVerb = p.match(/^\/api\/agents\/atlas\/chats\/(chat\d)\/(\w+)$/);
    if (chatVerb) {
      const [, chat, verb] = chatVerb;
      const conv = chat === 'chat2' ? conv2 : conv1;
      if (verb === 'feedback') {
        const m = conv.messages.find(x => x.message_id === body.message_id);
        if (!m || m.role !== 'assistant' || !m.task_id) return route.fulfill({status: 400, json: {error: "Only the agent's replies in this chat can be rated."}});
        m.feedback = body.rating ? {rating: body.rating, note: body.note ?? null, at: Date.now() / 1000} : null;
        return json({task_id: m.task_id, feedback: m.feedback});
      }
      if (verb === 'regenerate' && regenerateCalls++ === 0) return route.fulfill({status: 400, json: {error: 'A turn in this chat is still in progress; wait for it to finish, then regenerate.'}});
      if (verb === 'regenerate' || verb === 'edit') return json({ok: true, task_id: 't9', task: {task_id: 't9', state: 'QUEUED'}, superseded: verb === 'edit' ? ['t1', 't2'] : ['t2'], files: []});
      if (verb === 'rename') { const c = chats.find(x => x.chat_id === chat); c.title = body.title.replace(/\s+/g, ' ').trim(); return json({chat_id: c.chat_id, project_id: 'research', agent_id: 'atlas', title: c.title, created_at: c.created_at, archived: false}); }
      if (verb === 'export') return route.fulfill({body: `# ${chats[0].title}\n\n### You\nSummarize the attached report…\n`, contentType: 'text/markdown; charset=utf-8', headers: {'Content-Disposition': `attachment; filename="${chats[0].title.replace(/[^\w -]/g, '-')}.md"`}});
      if (verb === 'compact') return json({compacted: false, reason: 'Nothing to compact yet.'});
    }
    if (p === '/api/artifacts/art_a/diff' || p === '/api/artifacts/art_b/diff') return json({diff: diffs[p.split('/')[3]]});
    if (p === '/api/artifacts/art_img1/content') return route.fulfill({body: chartPng, contentType: 'image/png'});
    if (p === '/api/agents/atlas/files/content' && url.searchParams.get('path') === 'images/cover.png') return route.fulfill({body: coverPng, contentType: 'image/png'});
    if (p === '/api/artifacts/art_up1') return json({artifact_id: 'art_up1', path: 'uploads/2026-09-27/report.pdf', change: 'created', version: 1, size: 482113, mime: 'application/pdf', versions: [{artifact_id: 'art_up1', version: 1, change: 'created', size: 482113, path: 'uploads/2026-09-27/report.pdf', created_at: T0}], diff: ''});
    if (p === '/api/artifacts/art_img1') return json({artifact_id: 'art_img1', path: 'charts/test_counts.png', change: 'created', version: 1, size: chartPng.length, mime: 'image/png', versions: [{artifact_id: 'art_img1', version: 1, change: 'created', size: chartPng.length, path: 'charts/test_counts.png', created_at: T0}], diff: ''});
    if (p === '/api/artifacts/art_a') return json({artifact_id: 'art_a', path: 'src/slugify.py', change: 'modified', version: 2, size: 812, mime: 'text/x-python', versions: [{artifact_id: 'art_a', version: 2, change: 'modified', size: 812, path: 'src/slugify.py', created_at: T0}], diff: diffs.art_a});
    if (p === '/api/artifacts/art_a/content') return route.fulfill({body: 'import re\nimport unicodedata\n', contentType: 'text/plain'});
    if (p === '/api/personalization') { if (method === 'POST') personalization = {...body}; return json(personalization); }
    if (p === '/api/audit') return json([]);
    if (p === '/api/sessions') return json([]);
    if (/^\/api\/tasks\/t5\/(cancel|approval|resume)$/.test(p)) return json({ok: true});
    calls.push({unexpected: `${method} ${p}${url.search}`});
    return route.fulfill({status: 404, json: {error: 'Not found.'}});
  });
  const posts = (suffix) => calls.filter(c => c.method === 'POST' && c.path.endsWith(suffix));
  const results = [];
  const pass = label => { results.push(label); console.log(`  ok  ${label}`); };
  // Resolved and read in one step: a poll repaint can replace a node between two calls.
  const bg = sel => page.evaluate(q => getComputedStyle(document.querySelector(q)).backgroundColor, sel);
  const repaintFromServer = () => page.evaluate(() => tick(true));
  const shot = async name => page.screenshot({path: path.join(shots, name)});

  try {
    // ---------- 9. empty chat ----------
    await page.goto(`${BASE}/#/agents/atlas`);
    await page.locator('#chat-request').waitFor();
    assert.equal((await page.locator('.chat-welcome h1').textContent()).trim(), 'What can I help with?');
    assert.equal(await page.locator('.starters button').count(), 4);
    assert.equal(await page.locator('#chat-request').getAttribute('placeholder'), 'Message Atlas');
    assert.equal(await page.locator('#chat-request').getAttribute('aria-label'), 'Message Atlas');
    assert(await page.evaluate(() => document.body.classList.contains('details-closed')), 'workspace panel closed on first visit');
    assert.equal(await bg('body'), 'rgb(33, 33, 33)');
    assert.equal(await bg('.sidebar'), 'rgb(24, 24, 24)');
    assert.equal(await bg('#chat-form'), 'rgb(48, 48, 48)');
    assert.equal(await page.locator('#chat-form').evaluate(el => getComputedStyle(el).borderRadius), '28px');
    const welcome = await page.locator('.chat-welcome h1').boundingBox(), composerBox = await page.locator('#chat-form').boundingBox();
    assert(welcome.y > 200 && composerBox.y > welcome.y && composerBox.y < 700, 'empty state centred: heading above a centred composer');
    await shot('01-desktop-dark-empty.png');
    pass('9 empty chat: centred "What can I help with?", centred composer, 4 starter pills, panel closed by default, #212121/#181818/#303030 palette, 28px pill');

    // ---------- 6. chrome: sidebar + header ----------
    const nav = (await page.locator('#space-links').innerText()).replace(/\s+/g, ' ');
    for (const label of ['New chat', 'Search chats', 'Library', 'Goals', 'Tasks', 'Apps & connections']) assert(nav.includes(label), `nav ${label}`);
    assert.equal((await page.locator('#conversation-tree .side-label').textContent()).trim(), 'Chats');
    assert(await page.locator('#conversation-tree .convo-group').count() >= 2, 'date groups');
    assert(await page.locator('#profile-button').isVisible());
    assert((await page.locator('.model-switch').innerText()).includes('GPT-5.6 Sol'));
    await page.locator('.model-switch').click();
    assert(await page.locator('#item-menu [data-set-model]').count() >= 3, 'model menu from header');
    await page.keyboard.press('Escape');
    assert(await page.locator('#item-menu').isHidden(), 'Esc closes menus');
    const crumbs = await page.locator('#crumbs').innerText();
    assert(crumbs.includes('research') && crumbs.includes('main') && crumbs.includes('Ready'), `crumbs: ${crumbs}`);
    assert.equal(await page.locator('#crumbs .crumb.branch .dirty').count(), 1, 'dirty marker on the branch crumb');
    assert(await page.locator('#share-button').isHidden(), 'no share on a new chat');
    assert.equal(await page.locator('#head-new-chat').getAttribute('href'), '#/agents/atlas');
    assert.equal(await page.locator('.side-top #side-new-chat').getAttribute('href'), '#/agents/atlas', 'pencil beside the compact picker');
    await page.locator('#sidebar-collapse').click();
    assert(await page.locator('#hub-sidebar').isHidden() && await page.locator('#sidebar-toggle').isVisible(), 'sidebar collapses on desktop');
    await page.locator('#sidebar-toggle').click();
    assert(await page.locator('#hub-sidebar').isVisible(), 'sidebar reopens');
    await page.locator('#profile-button').click();
    const profile = await page.locator('#item-menu').innerText();
    assert(profile.includes('Personalization') && profile.includes('Settings') && profile.includes('Activity'), 'profile menu');
    await page.keyboard.press('Escape');
    pass('6 chrome: nav rows, "Chats" date groups, profile row+menu, header model selector opens model menu, crumbs folder/branch/status, collapsible sidebar');

    // ---------- 3. composer: + menu, files, dictation, send payload ----------
    await page.locator('[data-composer-menu]').click();
    const plusMenu = await page.locator('#item-menu').innerText();
    for (const label of ['Add photos & files', 'Create image', 'Deep research', 'Schedule a check-in', 'Build an app', 'Research something', 'Make a document']) assert(plusMenu.includes(label), `+ menu ${label}`);
    await page.locator('#item-menu [data-prefill="Create an image of "]').click();
    assert.equal(await page.locator('#chat-request').inputValue(), 'Create an image of ');
    await page.locator('#chat-request').fill('');
    assert.equal(await page.locator('#attach-input').getAttribute('accept'), null, 'file picker accepts any type');
    await page.locator('#attach-input').setInputFiles([
      {name: 'notes.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 test')},
      {name: 'data.csv', mimeType: 'text/csv', buffer: Buffer.from('a,b\n1,2\n')},
      {name: 'shot.png', mimeType: 'image/png', buffer: chartPng}]);
    await page.waitForFunction(() => document.querySelectorAll('#attach-row .file-card.pending').length === 2 && document.querySelectorAll('#attach-row .img-chip').length === 1);
    await page.locator('#attach-input').setInputFiles([{name: 'huge.bin', mimeType: 'application/octet-stream', buffer: Buffer.alloc(21 * 1024 * 1024)}]);
    await page.waitForFunction(() => /over 20 MB/.test(document.querySelector('#toast').textContent) && document.querySelector('#toast').classList.contains('show'));
    await page.locator('#attach-row .file-card.pending').nth(1).locator('.chip-x').click();
    assert.equal(await page.locator('#attach-row .file-card.pending').count(), 1, 'remove chip');
    // Dictation writes interim then final text; a repaint keeps it and the pending chips.
    await page.locator('[data-dictate]').click();
    await page.waitForFunction(() => document.querySelector('#chat-request').value === 'hello world');
    await page.evaluate(() => { pageData.status = {...pageData.status, detail: 'changed'}; paint(renderAgent(pageData)); lastHtml = ''; paint(renderAgent(pageData)); });
    assert.equal(await page.locator('#chat-request').inputValue(), 'hello world', 'dictated text survives repaint');
    assert.equal(await page.locator('#attach-row .file-card.pending').count(), 1, 'chips survive repaint');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('[data-dictate]').getAttribute('aria-pressed'), 'false', 'Esc stops dictation');
    await page.locator('#chat-request').fill('Please read these');
    await page.locator('#chat-form .send-button').click();
    await page.waitForURL('**/#/agents/atlas/work/chat3');
    const sent = posts('/api/agents/atlas/messages').pop().body;
    assert.deepEqual(Object.keys(sent).sort(), ['body', 'chat_id', 'files', 'images', 'request_id']);
    assert.equal(sent.files.length, 1); assert.deepEqual(Object.keys(sent.files[0]).sort(), ['data', 'mime', 'name']);
    assert.equal(sent.files[0].name, 'notes.pdf'); assert.equal(sent.files[0].mime, 'application/pdf'); assert.equal(Buffer.from(sent.files[0].data, 'base64').toString(), '%PDF-1.4 test');
    assert.equal(sent.images.length, 1); assert.equal(sent.images[0].mime, 'image/png');
    assert.equal(sent.body, 'Please read these');
    assert.match(sent.request_id, /^[0-9a-f]{32}$/);
    // 100 MB per message in total; then a files-only message (empty body).
    await page.evaluate(() => pendingUploads.set(location.hash, [{kind: 'file', name: 'big.bin', mime: 'application/octet-stream', size: 95 * 1024 * 1024, data: ''}]));
    await page.locator('#attach-input').setInputFiles([{name: 'more.bin', mimeType: 'application/octet-stream', buffer: Buffer.alloc(6 * 1024 * 1024)}]);
    await page.waitForFunction(() => /over 100 MB/.test(document.querySelector('#toast').textContent), null, {timeout: 5000})
      .catch(async error => { console.error('toast:', await page.evaluate(() => [document.querySelector('#toast').textContent, location.hash, JSON.stringify([...pendingUploads].map(([k, v]) => [k, v.map(u => [u.name, u.size])]))])); throw error; });
    await page.evaluate(() => { pendingUploads.delete(location.hash); refreshAttachRow(); });
    const sentBefore = posts('/api/agents/atlas/messages').length;
    await page.locator('#attach-input').setInputFiles([{name: 'results.csv', mimeType: 'text/csv', buffer: Buffer.from('release,tests\n0.6.3,241\n')}]);
    await page.waitForFunction(() => document.querySelectorAll('#attach-row .file-card.pending').length === 1);
    await page.locator('#chat-form .send-button').click();
    await page.waitForFunction(() => !document.querySelector('#attach-row .file-card.pending'));
    for (let i = 0; i < 50 && posts('/api/agents/atlas/messages').length === sentBefore; i++) await page.waitForTimeout(100);
    const filesOnly = posts('/api/agents/atlas/messages').pop().body;
    assert.equal(filesOnly.body, '', 'files-only message has an empty body');
    assert.equal(filesOnly.files[0].name, 'results.csv');
    assert.notEqual(filesOnly.request_id, sent.request_id, 'fresh request_id per message');
    pass('3 composer: + menu (files, Create image, Deep research, quick actions), any-type picker, file/image chips with ×, 20 MB and 100 MB-per-message toasts, dictation interim→final survives repaint, Esc stops it, files:[{name,mime,data}] + images on send, files-only message with empty body');

    // ---------- 1/2/4. a settled thread ----------
    await page.goto(`${BASE}/#/agents/atlas/work/chat1`);
    await page.locator('.assistant-message .result h2').waitFor();
    const u1 = page.locator('[data-message="t1-user"]'), r1 = page.locator('[data-message="t1-assistant"]'), r2 = page.locator('[data-message="t2-assistant"]');
    const userBubble = u1.locator('.bubble');
    assert.equal(await bg('[data-message="t1-user"] .bubble'), 'rgb(48, 48, 48)');
    assert.equal((await userBubble.innerText()).trim(), 'Summarize the attached report and fix slugify for Unicode.', 'operator text without the attachment line');
    const thread = await page.locator('.chat-thread').boundingBox(), bubble = await userBubble.boundingBox();
    assert(Math.abs(thread.x + thread.width - (bubble.x + bubble.width)) < 2, 'user bubble right-aligned');
    assert(bubble.width <= thread.width * 0.71, 'bubble max-width ~70%');
    assert(thread.width <= 769 && thread.width >= 700, `48rem column (${thread.width})`);
    assert.equal(await bg('[data-message="t1-assistant"]'), 'rgba(0, 0, 0, 0)');
    assert.equal(await page.locator('.assistant-message .who, .assistant-message h3.name, .assistant-message .avatar').count(), 0, 'no name/avatar on replies');
    assert.equal(await r1.locator('.result').evaluate(el => getComputedStyle(el).fontSize), '16px');
    assert.equal(await u1.locator('.file-card').count(), 2, 'sent file chips in the user bubble');
    // Legacy messages (no task_id) can only be copied.
    const legacyReply = page.locator('.assistant-message').first(), legacyUser = page.locator('.user-message').first();
    assert.equal(await legacyReply.getAttribute('data-message'), '#1');
    assert.deepEqual(await legacyReply.locator('.msg-actions button').evaluateAll(b => b.map(x => x.getAttribute('aria-label'))), ['Copy']);
    assert.deepEqual(await legacyUser.locator('.msg-actions button').evaluateAll(b => b.map(x => x.getAttribute('aria-label'))), ['Copy']);
    // File chips open the artifact viewer by artifact_id.
    await u1.locator('.file-card').first().click();
    await page.waitForFunction(() => document.querySelector('#preview-dialog').open && document.querySelector('#preview-title').textContent.includes('report.pdf'));
    await page.locator('#preview-dialog [data-close]').click();
    // Markdown
    const code = r1.locator('.code-block').first();
    assert.equal((await code.locator('.code-lang').textContent()).trim(), 'python');
    assert(await code.locator('.tk-k').count() >= 3 && await code.locator('.tk-s').count() >= 2 && await code.locator('.tk-c').count() >= 1 && await code.locator('.tk-n').count() >= 1 && await code.locator('.tk-f').count() >= 1, 'python tokens');
    const js = r1.locator('.code-block').nth(1);
    assert(await js.locator('.tk-k').count() >= 1 && await js.locator('.tk-t').count() >= 1 && await js.locator('.tk-s').count() >= 1 && await js.locator('.tk-c').count() >= 1, 'js tokens');
    await code.locator('.code-copy').click();
    await page.waitForFunction(() => document.querySelector('.code-copy.copied')?.textContent.includes('Copied'));
    const copiedCode = (await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n');  // the Windows clipboard returns CRLF
    assert(copiedCode.startsWith('import re, unicodedata\n\ndef slugify'), `code copied: ${JSON.stringify(copiedCode.slice(0, 80))}`);
    assert.notEqual(await bg('.result p code'), 'rgba(0, 0, 0, 0)', 'inline code styled');
    assert.equal(await page.locator('.result .table-wrap table th').count(), 3);
    assert.notEqual(await page.locator('.result td').first().evaluate(el => getComputedStyle(el).borderBottomWidth), '0px', 'table borders');
    const math = await page.locator('.math-block').textContent();
    assert(math.includes('∑') && math.includes('≤') && math.includes('π'), `math block: ${math}`);
    assert((await page.locator('.math-inline').textContent()).includes('Δ'), 'inline math');
    assert(await page.locator('.result a.link-chip').count() >= 1 && await page.locator('.result a.md-link').count() >= 1, 'links');
    assert.equal(await page.locator('.result img[src="x"]').count(), 0, 'escaped HTML stays text');
    assert((await r1.locator('.result').innerText()).includes('<img src=x onerror=alert(1)>'));
    pass('2 markdown: code header+language+Copy code→Copied, built-in highlighting (python/js), inline code, bordered table, $$…$$ and \\(…\\) math, link chips, raw HTML stays text');
    // Inline images: the turn's chart plus the picture named only by the image-lane marker; no raw marker text.
    await page.waitForFunction(() => { const imgs = [...document.querySelectorAll('[data-message="t2-assistant"] .turn-images img')]; return imgs.length === 2 && imgs.every(i => i.src.startsWith('blob:') && i.naturalWidth > 0); });
    assert(!(await page.locator('.chat-thread').innerText()).includes('[[jarvis-image'), 'image marker never shown as text');
    assert(calls.some(c => c.path === '/api/agents/atlas/files/content' && c.search === '?path=images%2Fcover.png'), 'marker image fetched with the bearer token');
    await r2.locator('.turn-image').first().click();
    await page.locator('#preview-dialog[open] #preview-body img').waitFor();
    await page.locator('#preview-dialog [data-close]').click();
    pass('4 inline images: agent_turns[].images (with mime) as a grid under the reply via authorised blobs, [[jarvis-image:…]] markers removed (an unlisted one is fetched), click opens the viewer');
    // Actions, by message_id
    await r1.hover();
    for (const label of ['Copy', 'Good response', 'Bad response', 'Read aloud', 'More actions']) assert(await r1.locator(`.msg-actions [aria-label="${label}"]`).isVisible(), `action ${label}`);
    assert.equal(await r1.locator('[data-regenerate]').count(), 0, 'regenerate only on the last reply');
    assert.equal(await r2.locator('[data-feedback="t2-assistant|up"]').getAttribute('aria-pressed'), 'true', 'saved feedback {rating} shown');
    assert(await r2.locator('.msg-actions.always [data-regenerate]').isVisible(), 'last reply row always visible with Regenerate');
    await page.mouse.move(5, 5);
    await page.waitForFunction(() => getComputedStyle(document.querySelector('[data-message="t1-assistant"] .msg-actions')).opacity === '0');  // older reply rows show on hover only
    await r1.hover(); await r1.locator('[aria-label="Copy"]').click();
    await page.waitForFunction(() => document.querySelector('[data-message="t1-assistant"] [data-copy-msg].copied'));
    assert((await page.evaluate(() => navigator.clipboard.readText())).startsWith('## Summary'), 'reply copied as Markdown');
    await r1.locator('[aria-label="Bad response"]').click();
    assert.deepEqual(posts('/chats/chat1/feedback').pop().body, {message_id: 't1-assistant', rating: 'down'});
    assert.equal(await r1.locator('[data-feedback="t1-assistant|down"]').getAttribute('aria-pressed'), 'true');
    await repaintFromServer();
    assert.equal(await r1.locator('[data-feedback="t1-assistant|down"]').getAttribute('aria-pressed'), 'true', 'saved feedback reflected after reload');
    await r1.hover();
    await r1.locator('[data-feedback="t1-assistant|down"]').click();
    assert.deepEqual(posts('/chats/chat1/feedback').pop().body, {message_id: 't1-assistant', rating: null}, 'second click clears');
    await r1.hover();
    await r1.locator('[data-speak]').click();
    assert((await page.evaluate(() => window.__speech.spoken[0])).startsWith('Summary The report tracks six releases'));
    assert.equal(await r1.locator('[data-speak]').getAttribute('aria-label'), 'Stop reading');
    await r1.locator('[data-speak]').click();
    assert.equal(await r1.locator('[data-speak]').getAttribute('aria-label'), 'Read aloud');
    // Regenerate: a refusal (turn still in progress) is shown; each attempt carries a fresh request_id.
    await r2.locator('[data-regenerate]').click();
    await page.waitForFunction(() => document.querySelector('#toast.error')?.textContent.includes('still in progress'));
    await r2.locator('[data-regenerate]').click();
    await page.waitForFunction(() => /Regenerating/.test(document.querySelector('#toast').textContent));
    const regens = posts('/chats/chat1/regenerate').map(c => c.body);
    assert.equal(regens.length, 2); assert.deepEqual(Object.keys(regens[1]), ['request_id']);
    assert(/^[0-9a-f]{32}$/.test(regens[0].request_id) && regens[0].request_id !== regens[1].request_id, 'fresh request_id per attempt');
    const download = page.waitForEvent('download');
    await r1.hover();
    await r1.locator('[data-msg-more]').click();
    await page.locator('#item-menu [data-export="md"]').click();
    assert.equal((await download).suggestedFilename(), 'Slugify fix and report summary.md', 'filename from Content-Disposition');
    assert(calls.some(c => c.path === '/api/agents/atlas/chats/chat1/export' && c.search === '?format=md'), 'export md requested');
    // Edit by message_id: the editor survives a poll repaint; Send posts {message_id, body, request_id}.
    await u1.hover();
    assert(await u1.locator('[aria-label="Copy"]').isVisible() && await u1.locator('[aria-label="Edit message"]').isVisible());
    await u1.locator('[aria-label="Edit message"]').click();
    assert.equal(await page.locator('#edit-body').inputValue(), 'Summarize the attached report and fix slugify for Unicode.', 'editor starts from the typed text');
    await page.locator('#edit-body').fill('Summarize the report in three bullets.');
    await repaintFromServer();
    assert.equal(await page.locator('#edit-body').inputValue(), 'Summarize the report in three bullets.', 'editor survives repaint');
    await page.locator('[data-edit-send]').click();
    const edit = posts('/chats/chat1/edit').pop().body;
    assert.deepEqual(Object.keys(edit).sort(), ['body', 'message_id', 'request_id']);
    assert.equal(edit.message_id, 't1-user'); assert.equal(edit.body, 'Summarize the report in three bullets.'); assert.match(edit.request_id, /^[0-9a-f]{32}$/);
    await page.locator('#edit-body').waitFor({state: 'detached'});
    pass('1 thread: operator text + file chips (open by artifact_id), plain replies in a 48rem column, right-aligned #303030 bubbles ≤70%, legacy messages Copy only, hover rows (Copy/👍/👎/Read aloud/…), last row always visible, feedback {message_id, rating} saved + reflected + cleared, read aloud, regenerate refusal toast + fresh request_id, export filename from Content-Disposition, inline edit {message_id, body, request_id}');
    await page.locator('.assistant-message.last').scrollIntoViewIfNeeded();
    await page.evaluate(() => { view.scrollTop = 0; });
    await shot('02-desktop-dark-thread-code-table.png');
    await page.evaluate(() => { view.scrollTop = view.scrollHeight; });
    await page.waitForTimeout(100);
    await shot('03-desktop-dark-thread-image.png');

    // ---------- 7. scroll-to-bottom and shortcuts ----------
    await page.evaluate(() => { view.scrollTop = 0; view.dispatchEvent(new Event('scroll')); });
    assert(await page.locator('.to-bottom').isVisible(), '↓ appears away from the bottom');
    await page.locator('.to-bottom').click();
    await page.waitForFunction(() => view.scrollHeight - view.scrollTop - view.clientHeight < 4);
    assert(await page.locator('.to-bottom').isHidden(), '↓ hides at the bottom');
    await page.keyboard.press('Control+k');
    await page.locator('#search-dialog[open]').waitFor();
    assert(await page.evaluate(() => document.activeElement.id === 'search-chats-input'));
    await page.keyboard.type('slug');
    assert.equal(await page.locator('#search-chats-results .search-item mark').first().textContent(), 'Slug');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => /Messages/.test(document.querySelector('#search-chats-results').innerText));
    assert(calls.some(c => c.path === '/api/agents/atlas/search' && c.search === '?q=slug'), 'full-text search on Enter');
    await shot('04-desktop-dark-search.png');
    await page.keyboard.press('Escape');
    assert(await page.locator('#search-dialog').isHidden());
    await page.evaluate(() => document.activeElement?.blur());
    await page.keyboard.press('Shift+Escape');
    assert(await page.evaluate(() => document.activeElement.id === 'chat-request'), 'Shift+Esc focuses the composer');
    await page.keyboard.press('Control+Shift+O');
    await page.waitForURL(/#\/agents\/atlas$/);
    pass('7 scroll-to-bottom ↓, Ctrl+K title search with highlight + Enter full-text, Esc closes, Shift+Esc focuses composer, Ctrl+Shift+O new chat');

    // ---------- rename (contract item 10): sidebar menu and header title, inline ----------
    await page.goto(`${BASE}/#/agents/atlas/work/chat1`);
    await page.locator('.assistant-message .result h2').waitFor();
    const row = page.locator('#conversation-tree .convo[data-chat="chat1"]');
    await row.hover();
    await row.locator('.more').click();
    await page.locator('#item-menu [data-rename-chat="chat1"]').click();
    const sideInput = page.locator('#conversation-tree .convo.renaming .rename-input');
    assert.equal(await sideInput.inputValue(), 'Slugify fix and report summary');
    await repaintFromServer();
    assert(await sideInput.isVisible(), 'rename field survives polling');
    await sideInput.fill('Unicode   slugify fix');
    await sideInput.press('Enter');
    await page.waitForFunction(() => document.querySelector('#conversation-tree .convo[data-chat="chat1"] a')?.textContent.trim() === 'Unicode slugify fix');
    assert.deepEqual(posts('/chats/chat1/rename').pop().body, {title: 'Unicode slugify fix'}, 'whitespace collapsed like the backend');
    assert.equal((await page.locator('.head-chat').textContent()).trim(), 'Unicode slugify fix', 'header shows the new title');
    await page.locator('.head-chat').click();
    await page.locator('#head-chat-title .rename-input').press('Escape');
    assert.equal(posts('/chats/chat1/rename').length, 1, 'Esc cancels without saving');
    await page.locator('.head-chat').click();
    await page.locator('#head-chat-title .rename-input').fill('Slugify fix and report summary');
    await page.locator('#head-chat-title .rename-input').press('Enter');
    await page.waitForFunction(() => document.querySelector('.head-chat')?.textContent.trim() === 'Slugify fix and report summary');
    assert.deepEqual(posts('/chats/chat1/rename').pop().body, {title: 'Slugify fix and report summary'});
    pass('10 rename: chat "…" menu Rename and header title edit inline (Enter saves, Esc cancels, survives polling) → rename {title}');

    // ---------- 5 + A/B/C/D: live work in the agent workspace ----------
    await page.goto(`${BASE}/#/agents/atlas/work/chat2`);
    await page.locator('.thinking').waitFor();
    // The running app opens the workspace panel on Preview by itself (wide screen).
    await page.waitForFunction(() => !document.body.classList.contains('details-closed') && document.querySelector('[data-rp-tab=preview]').classList.contains('active'));
    const frame = page.locator('#details #app-panel iframe');
    assert.equal(await frame.getAttribute('src'), 'http://127.0.0.1:5173/');
    assert(await page.locator('#details #app-panel').isVisible());
    for (const id of ['#app-panel-reload', '#app-panel-newtab', '#app-panel-power']) assert(await page.locator(id).isVisible(), id);
    await page.locator('[data-preview-device=phone]').click();
    assert.equal(Math.round((await frame.boundingBox()).width), 390);
    await shot('05-workspace-preview-app.png');
    await page.locator('[data-preview-device=desktop]').click();
    pass('C preview: running app hosted in the panel (Reload / new tab / Stop), auto-opened on a new app, device widths');
    assert.equal((await page.locator('.thinking .shimmer').textContent()).trim(), 'Thinking…');
    assert.equal(await page.locator('.thinking').evaluate(d => d.open), false, 'steps collapsed by default');
    await page.locator('.thinking > summary').click();
    assert.equal(await page.locator('.thinking .live-steps li').count(), 3);
    assert(await page.locator('.worked summary').first().innerText().then(t => /Worked for 1m 35s · 5 tool calls/.test(t)), 'Worked for disclosure');
    // Stop replaces send while the composer is empty; typing brings Send back beside it.
    assert(await page.locator('#chat-form .stop-button[data-chat-cancel=t5]').isVisible());
    assert(await page.locator('#chat-form .send-button').isHidden(), 'send hidden while busy and empty');
    await page.locator('#chat-request').fill('also add sound');
    assert(await page.locator('#chat-form .send-button').isVisible() && await page.locator('#chat-form .stop-button').isVisible());
    await page.locator('#chat-request').fill('');
    await page.locator('#chat-form .stop-button').click();
    assert(posts('/api/tasks/t5/cancel').length === 1, 'Stop cancels the turn');
    pass('5 live work: "Thinking…" shimmer with collapsed live steps, "Worked for 1m 35s · 5 tool calls", ■ Stop swaps in for send while busy and cancels');
    // B: inline tool rows, grouped reads, expandable detail and diffs.
    const rows = page.locator('.activity.live > .tool-row');
    assert.equal(await rows.count(), 3, 'reads grouped + edit + run');
    assert.equal((await rows.nth(0).locator(':scope > summary .tr-verb').textContent()).trim(), 'Read 2 files');
    assert((await rows.nth(2).innerText()).includes('Ran') && (await rows.nth(2).innerText()).includes('npm test'));
    assert(await rows.nth(2).evaluate(el => el.classList.contains('fail')), 'failed run marked');
    await rows.nth(0).locator(':scope > summary').click();
    assert.equal(await rows.nth(0).locator('.tr-body .tool-row').count(), 2, 'group expands to its reads');
    await rows.nth(2).locator(':scope > summary').click();
    assert((await rows.nth(2).locator('.tr-body').innerText()).includes('exit 1'));
    Object.assign(conv2.agent_turns[1], {state: 'WAITING_APPROVAL', actions: ['cancel', 'approve', 'deny'], approval: {action: 'run_process', resource: '{"program": "npm", "arguments": ["publish"]}', reason: 'Publishing changes the npm registry.'}});
    conv2.messages[3].state = 'WAITING_APPROVAL';
    await repaintFromServer();
    const card = page.locator('.activity.live .perm-card');
    assert(await card.isVisible(), 'permission card inline');
    assert(await card.locator('[data-approve=t5]').isVisible() && await card.locator('[data-deny=t5]').isVisible());
    assert.equal((await card.locator('[data-approve=t5]').textContent()).trim(), 'Approve this exact action');
    await shot('06-workspace-thread-tool-rows-permission.png');
    await card.locator('[data-approve=t5]').click();
    assert.deepEqual(posts('/api/tasks/t5/approval').pop().body, {decision: 'approve'});
    Object.assign(conv2.agent_turns[1], {state: 'RUNNING', actions: ['cancel', 'pause', 'steer'], approval: null});
    conv2.messages[3].state = 'RUNNING';
    await repaintFromServer();
    await rows.nth(1).locator(':scope > summary').click();
    assert((await rows.nth(1).locator('.tr-body').innerText()).includes('The diff appears when this run finishes'), 'running write: diff after the run');
    pass('B inline tool rows: kind icons, verb summaries from detail.args, "Read 2 files" group, expandable detail, failed run flagged, running write waits for its artifact, inline permission card → approval');
    // C: tabs
    await page.locator('[data-rp-tab=progress]').click();
    await page.locator('.pg-turn').first().waitFor();
    assert((await page.locator('.pg-turn').first().innerText()).includes('tool call'));
    assert(await page.locator('.pg-turn .checklist li').count() >= 4);
    assert(await page.locator('.pg-turn [data-chat-cancel]').count() >= 1, 'progress has Stop');
    await shot('07-workspace-progress.png');
    await page.locator('[data-rp-tab=changes]').click();
    await page.waitForFunction(() => document.querySelector('.cf-list .cf-status.s-P'));
    assert((await page.locator('.cf-list').innerText()).includes('game.js'), 'a running write is listed before its artifact');
    await page.locator('[data-rp-tab=files]').click();
    await page.waitForFunction(() => document.querySelector('#rp-body .file-tree'));
    const files = await page.locator('#rp-body').innerText();
    assert(files.includes('▱ research') && files.includes('Uploads') && files.includes('report.pdf'), 'files tab: folder + uploads');
    await page.locator('#rp-body .ft-dir summary').first().click();
    await shot('08-workspace-files.png');
    await page.locator('[data-rp-tab=agent]').click();
    assert.equal((await page.locator('#rp-body .profile h2').textContent()).trim(), 'Atlas');
    assert(await page.locator('#rp-body .usage-inline').isVisible(), 'context usage in Agent tab');
    await page.locator('#rp-body [data-details-tab=access]').click();
    assert((await page.locator('#rp-body').innerText()).includes('Search and read the web'));
    await shot('09-workspace-agent.png');
    // Changes, on the chat that edited files; counts come from the synthetic diffs.
    await page.goto(`${BASE}/#/agents/atlas/work/chat1`);
    await page.locator('.assistant-message .result h2').waitFor();
    // The finished turn's rows name their files and diffs from detail.path / artifact_id.
    await page.locator('[data-message="t1-assistant"] .worked > summary').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-message="t1-assistant"] .worked .tool-row').length >= 5);
    const worked = page.locator('[data-message="t1-assistant"] .worked');
    assert((await worked.innerText()).includes('Read 3 files'), 'reads grouped');
    assert((await worked.innerText()).includes('python -m pytest -q'), 'run row shows detail.args');
    const editGroup = worked.locator('.tool-row.group.k-edit');
    assert.equal((await editGroup.locator(':scope > summary .tr-verb').textContent()).trim(), 'Changed 2 files', 'an edit and a write group together');
    await editGroup.locator(':scope > summary').click();
    const editRow = worked.locator('.tool-row[data-diff-id="art_a"]');
    await editRow.locator(':scope > summary').click();
    await page.waitForFunction(() => document.querySelector('[data-message="t1-assistant"] .tool-row[data-diff-id="art_a"] pre.diff .add'));
    await page.locator('[data-rp-tab=changes]').click();
    await page.waitForFunction(() => document.querySelectorAll('.cf-list .cf-row').length === 3 && !document.querySelector('.cf-list').innerText.includes('…'));
    const list = await page.locator('.cf-list').innerText();
    assert(/slugify\.py[\s\S]*\+4[\s\S]*−1/.test(list) && /test_slugify\.py[\s\S]*\+3/.test(list), `changes counts: ${list}`);
    assert.deepEqual(await page.locator('.cf-list .cf-status').allTextContents(), ['M', 'A', 'A']);
    await page.locator('.cf-row').first().click();
    assert(await page.locator('.cf-review pre.diff .add').count() >= 3);
    assert((await page.locator('.cf-summary').innerText()).includes('3 files changed'));
    await shot('10-workspace-changes.png');
    // Resizable, remembered.
    const before = (await page.locator('#details').boundingBox()).width;
    const handle = await page.locator('#rp-resizer').boundingBox();
    await page.mouse.move(handle.x + 3, handle.y + 300); await page.mouse.down(); await page.mouse.move(handle.x - 117, handle.y + 300, {steps: 6}); await page.mouse.up();
    const after = (await page.locator('#details').boundingBox()).width;
    assert(Math.abs(after - before - 120) <= 6, `resize ${before} → ${after}`);
    assert.equal(Number(await page.evaluate(() => localStorage.getItem('jarvis.hub.rpWidth'))), Math.round(after));
    pass('C panel: Progress checklist (tool calls, Stop), Changes with M/A/A and +4 −1 / +3 from diffs and a diff review, Files tree + uploads, Agent profile/access/usage, drag-resize remembered');
    // A: top bar on a chat — Share/Export, panel toggle, New chat.
    assert(await page.locator('#share-button').isVisible());
    await page.locator('#share-button').click();
    assert(await page.locator('#item-menu [data-export=json]').isVisible());
    await page.keyboard.press('Escape');
    await page.locator('#details-toggle').click();
    assert(await page.locator('#details').isHidden(), 'panel toggles closed');
    assert.equal(await page.evaluate(() => sessionStorage.getItem('jarvis.hub.rpClosed')), '1');
    pass('A top bar: model selector, folder/branch/status crumbs, Share→export menu, panel toggle, New chat');
    // D: slash commands.
    await page.locator('#chat-request').fill('');
    await page.locator('#chat-request').pressSequentially('/');
    await page.locator('.slash-menu').waitFor();
    assert.equal(await page.locator('.slash-menu [data-slash]').count(), 10);
    for (const c of ['new', 'model', 'effort', 'export', 'compact', 'goal', 'schedule', 'image', 'research', 'help']) assert(await page.locator(`.slash-menu [data-slash=${c}]`).count() === 1, `/${c}`);
    await page.locator('#chat-request').pressSequentially('im');
    assert.equal(await page.locator('.slash-menu [data-slash]').count(), 1);
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#chat-request').inputValue(), 'Create an image of ');
    await page.locator('#chat-request').fill('');
    await page.locator('#chat-request').pressSequentially('/ne');
    await page.keyboard.press('Enter');
    await page.waitForURL(/#\/agents\/atlas$/);
    await page.locator('.chat-welcome').waitFor();
    assert.equal(await page.locator('#chat-request').inputValue(), '');
    await page.locator('[data-slash-menu]').click();
    assert.equal(await page.locator('.slash-menu [data-slash]').count(), 10, '/ chip opens the list');
    await page.keyboard.press('Escape');
    const foot = await page.locator('.composer-foot').innerText();
    assert(foot.includes('GPT-5.6 Sol') && foot.includes('Auto') && foot.includes('abilities'), `composer foot: ${foot}`);
    pass('D composer row: model, effort, abilities chip, context ring, "/" list; typing /im + Enter prefills, /ne + Enter runs /new');

    // ---------- 8. settings: personalization + theme ----------
    await page.goto(`${BASE}/#/settings`);
    await page.locator('#personalization textarea[name=about_you]').waitFor();
    assert((await page.locator('#personalization').innerText()).includes('What should agents know about you?'));
    assert((await page.locator('#personalization').innerText()).includes('How should agents respond?'));
    await page.locator('#personalization textarea[name=about_you]').fill('I am a designer in Toronto.');
    await page.locator('#personalization textarea[name=response_style]').fill('Be concise.');
    await repaintFromServer();
    assert.equal(await page.locator('#personalization textarea[name=about_you]').inputValue(), 'I am a designer in Toronto.', 'draft survives a poll');
    await page.locator('#personalization button.primary').click();
    await page.waitForFunction(() => /Saved/.test(document.querySelector('#toast').textContent));
    assert.deepEqual(posts('/api/personalization').pop().body, {about_you: 'I am a designer in Toronto.', response_style: 'Be concise.'});
    await page.locator('[data-theme-set=light]').click();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), 'light');
    assert.equal(await bg('body'), 'rgb(255, 255, 255)');
    await page.locator('[data-theme-set=system]').click();
    assert.equal(await page.evaluate(() => document.documentElement.hasAttribute('data-theme')), false);
    assert.equal(await bg('body'), 'rgb(33, 33, 33)', 'system follows the (dark) OS');
    await page.emulateMedia({colorScheme: 'light'});
    assert.equal(await bg('body'), 'rgb(255, 255, 255)', 'system follows the (light) OS');
    await page.emulateMedia({colorScheme: 'dark'});
    await page.locator('[data-theme-set=light]').click();
    pass('8 settings: Personalization (both questions, draft survives polling, Save posts {about_you, response_style}), light/dark/system theme');
    await page.goto(`${BASE}/#/agents/atlas/work/chat1`);
    await page.locator('.assistant-message .result h2').waitFor();
    await page.evaluate(() => { view.scrollTop = 0; });
    await shot('11-desktop-light-thread.png');
    await page.goto(`${BASE}/#/agents/atlas`);
    await page.locator('.chat-welcome').waitFor();
    await shot('12-desktop-light-empty.png');
    await page.evaluate(() => { localStorage.setItem('jarvis.hub.theme', 'dark'); applyTheme('dark'); });

    // ---------- mobile 375 ----------
    await page.setViewportSize({width: 375, height: 812});
    for (const hash of ['#/agents/atlas', '#/agents/atlas/work/chat1', '#/agents/atlas/work/chat2', '#/settings', '#/agents/atlas/artifacts']) {
      await page.goto(`${BASE}/${hash}`); await page.waitForTimeout(250);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && view.scrollWidth <= view.clientWidth + 1), `no horizontal scroll at 375 on ${hash}`);
    }
    await page.goto(`${BASE}/#/agents/atlas/work/chat1`);
    await page.locator('.assistant-message .result h2').waitFor();
    assert(await page.locator('#hub-sidebar').isHidden());
    await shot('13-mobile-thread.png');
    await page.locator('#sidebar-toggle').click();
    assert(await page.locator('#hub-sidebar').isVisible(), 'drawer opens');
    await shot('14-mobile-drawer.png');
    await page.mouse.click(360, 400);
    assert(await page.locator('#hub-sidebar').isHidden(), 'tap outside closes the drawer');
    await page.goto(`${BASE}/#/agents/atlas`);
    await page.locator('.chat-welcome').waitFor();
    await shot('15-mobile-empty.png');
    pass('mobile 375: no horizontal scroll on 5 views, drawer sidebar opens and closes');

    const unexpected = calls.filter(c => c.unexpected).map(c => c.unexpected);
    assert.deepEqual(unexpected, [], 'no unexpected API calls');
    assert.deepEqual(csp, [], 'no CSP violations');
    assert.deepEqual(errors, [], 'no page errors');
    console.log(`PASS: ${results.length} parity groups, 0 page errors, 0 CSP violations, synthetic API only. Screenshots in ${shots}`);
  } catch (error) {
    await page.screenshot({path: path.join(shots, 'failure.png')}).catch(() => {});
    console.error('Unexpected calls:', calls.filter(c => c.unexpected));
    console.error('Page errors:', errors, 'CSP:', csp);
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
