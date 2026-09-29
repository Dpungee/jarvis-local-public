// Agent-teams UI checks for the Agent Hub with synthetic API data. Never executes a real agent.
// Serves the static files under the Hub's real Content-Security-Policy, stubs the team routes of
// the contract (threads, rooms, tool-event detail), and fails on any other API call.
// Usage: node scripts/verify_hub_team.cjs [screenshot-dir]
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const root = path.resolve(__dirname, '../jarvis/agent_hub_static');
const shots = path.resolve(process.argv[2] || path.join(os.tmpdir(), 'jarvis-hub-team-shots'));
fs.mkdirSync(shots, {recursive: true});
const BASE = 'http://localhost:8798';
const CSP = "default-src 'self'; img-src 'self' blob: https:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-src http://127.0.0.1:* http://localhost:* http://[::1]:*; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

(async () => {
  const now = Date.now() / 1000, T0 = now - 3600;
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 900}, colorScheme: 'dark'});
  const page = await context.newPage();
  const errors = [], csp = [], calls = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (/Refused to/i.test(m.text())) csp.push(m.text()); });
  page.on('dialog', d => d.accept());

  // ---------- synthetic state (contract items 1–8) ----------
  const TEAM_LABEL = 'Talk to your other agents and join team discussions';
  const permission_labels = {web: 'Search and read the web', files: 'Read and write project files', team: TEAM_LABEL};
  const mk = (agent_id, name, role, model) => ({agent_id, name, role, purpose: '', project_id: 'research', project_name: 'research', provider: 'claude-cli', model, lifecycle: 'RUNNING', archived: false, effort: 'auto', effort_levels: [],
    status: {code: 'idle', label: 'Ready', detail: 'Ready', tone: 'ok'}, counts: {}, permissions: {web: true, files: true, team: true}});
  const atlas = mk('atlas', 'Atlas', 'Research lead', 'claude-sonnet-5'), coder = mk('coder', 'Coder', 'Software engineer', 'gpt-5.6-sol'), risk = mk('risk', 'Risk', 'Risk analyst', 'claude-sonnet-5');
  const archived = {...mk('old', 'Old helper', 'Retired', 'gpt-5.5'), archived: true};
  const overview = {agents: [atlas, coder, risk, archived], projects: [{project_id: 'research', name: 'research'}], providers: {'claude-cli': {installed: true, authenticated: true, models: [], detail: 'ok'}},
    defaults: {provider: 'claude-cli', model: 'claude-sonnet-5'}, known_models: {'claude-cli': ['claude-sonnet-5'], 'codex-cli': ['gpt-5.6-sol']}, permissions: {labels: permission_labels, defaults: {team: true}},
    features: {archive: true, goals: true, search: true, artifacts: true, schedules: true, effort: true, usage: false}, effort_labels: {auto: 'Auto'}, goal_categories: {}, server_time: now, last_seq: 0};
  const threads = [
    {thread_id: 'th1', peer: {agent_id: 'coder', name: 'Coder'}, last_at: now - 20, last_preview: 'Done. parse_csv() now handles quoted fields; tests pass.', count: 3, active: true},
    {thread_id: 'th2', peer: {agent_id: 'risk', name: 'Risk'}, last_at: now - 7200, last_preview: 'Max drawdown on the paper book is 4.1%.', count: 2, active: false}];
  const thread1 = {thread_id: 'th1', a: {agent_id: 'atlas', name: 'Atlas'}, b: {agent_id: 'coder', name: 'Coder'}, active: true, task_id: 't7', closed: false,
    approval: {id: 12, action: 'run_process', resource: 'npm publish --access public', reason: 'Publishing changes the npm registry.', status: 'pending', inline: true, agent_id: 'coder', agent_name: 'Coder', thread_id: 'th1', room_id: null},
    messages: [
      {message_id: 'm1', sender_agent_id: 'atlas', body: 'Please add a CSV parser to `tools/data.py` that handles quoted fields.', task_id: 't7', at: now - 300, state: 'COMPLETED'},
      {message_id: 'm2', sender_agent_id: 'coder', body: 'Done. `parse_csv()` now handles quoted fields; tests pass.\n\n```python\ndef parse_csv(text):\n    return list(csv.reader(io.StringIO(text)))\n```\nShould I also publish the package?', task_id: 't7', at: now - 120, state: 'COMPLETED'},
      {message_id: 'm3', sender_agent_id: 'atlas', kind: 'agent', body: 'Yes, publish it.', task_id: 't7', at: now - 20, state: 'sent'},
      {message_id: 'm4', sender_agent_id: null, kind: 'system', body: 'Coder needs your approval to run run_process (request #12).', task_id: 't7', at: now - 10, state: 'waiting'}]};
  const members = [{agent_id: 'atlas', name: 'Atlas'}, {agent_id: 'coder', name: 'Coder'}, {agent_id: 'risk', name: 'Risk'}];
  const rooms = new Map();
  rooms.set('r1', {room_id: 'r1', title: 'Launch plan', topic: 'Should we ship the paper trader to three more markets this week?', members, chair_id: 'atlas', state: 'running', created_at: now - 900, last_at: now - 10, speaking: 'risk',
    messages: [
      {message_id: 'rm1', sender: {kind: 'agent', agent_id: 'coder', name: 'Coder'}, body: 'The adapters for two of the three markets are ready; the third needs a week.', at: now - 800, round: 1},
      {message_id: 'rm2', sender: {kind: 'agent', agent_id: 'risk', name: 'Risk'}, body: 'Drawdown limits are not calibrated for the new markets yet.', at: now - 700, round: 1},
      {message_id: 'rm3', sender: {kind: 'agent', agent_id: 'atlas', name: 'Atlas'}, body: 'Let us narrow to two markets. Coder, what blocks the third?', at: now - 600, round: 1},
      {message_id: 'rm4', sender: {kind: 'operator', name: 'You'}, body: 'Keep the budget under $50 a day per market.', at: now - 500, round: 1},
      {message_id: 'rm4b', sender: {kind: 'system', name: 'Hub'}, body: 'Round 1 closed; the chair asked for another round.', at: now - 450, round: 1},
      {message_id: 'rm5', sender: {kind: 'agent', agent_id: 'coder', name: 'Coder'}, body: 'The exchange sandbox for the third market is down. **Two markets** is feasible today.', at: now - 300, round: 2}]});
  rooms.set('r2', {room_id: 'r2', title: '', topic: 'Weekly risk check', members: [members[0], members[2]], chair_id: 'risk', state: 'done', created_at: now - 86400, last_at: now - 80000,
    summary: '**Decision:** keep exposure at 30%.\n\n- Drawdown 4.1%, inside the limit\n- Revisit after the next rebalance', speaking: null,
    messages: [{message_id: 'x1', sender: {kind: 'agent', agent_id: 'atlas', name: 'Atlas'}, body: 'Exposure is 30%; drawdown 4.1%.', at: now - 85000, round: 1},
      {message_id: 'x2', sender: {kind: 'agent', agent_id: 'risk', name: 'Risk'}, body: 'That is inside the limit. Closing.', at: now - 84000, round: 1}]});
  rooms.set('r3', {room_id: 'r3', title: 'Data vendor review', topic: 'Pick a data vendor', members, chair_id: 'coder', state: 'interrupted', created_at: now - 7 * 86400, last_at: now - 6 * 86400, messages: []});
  const roomList = () => [...rooms.values()].map(({messages, speaking, approval, ...r}) => r);
  const ev = (seq, tool, summary, detail) => ({seq, ts: T0 + seq, agent_id: 'atlas', task_id: 't1', project_id: 'research', kind: 'tool', level: 'info', summary, detail: {tool, ok: true, ms: 20, ...detail}});
  const t1Events = [
    ev(1, 'list_agents', 'list_agents', {args: ''}),
    ev(2, 'ask_agent', 'ask_agent Coder', {args: 'Coder: Please add a CSV parser to tools/data.py that handles quoted fields.', peer_agent_id: 'coder', thread_id: 'th1', reply_preview: 'Done. `parse_csv()` now handles quoted fields; tests pass.'}),
    ev(3, 'start_team_discussion', 'start_team_discussion', {args: 'Should we ship the paper trader to three more markets?', room_id: 'r1', reply_preview: 'Narrow to two markets this week.'})];
  const conv = {messages: [
      {role: 'operator', body: 'Get Coder to add a CSV parser, then run a launch discussion.', text: 'Get Coder to add a CSV parser, then run a launch discussion.', state: 'APPLIED', message_id: 't1-user', task_id: 't1', files: []},
      {role: 'assistant', body: 'Coder added `parse_csv()` and the room agreed on two markets.', state: 'COMPLETED', message_id: 't1-assistant', task_id: 't1', feedback: null}],
    agent_turns: [{task_id: 't1', state: 'COMPLETED', request: 'Get Coder…', title: 'Team work', created_at: T0, started_at: T0, finished_at: T0 + 95, tool_calls: 3, actions: [], steps: [], artifacts: [], previews: []}], live_turns: []};
  const detail = id => ({...overview.agents.find(a => a.agent_id === id), instructions: '', chats: [{chat_id: 'chat1', title: 'Team work', created_at: T0, last_at: T0 + 95, turns: 1, archived: false, active: false}],
    tasks: [], approvals: [], artifacts: [], events: t1Events, errors: [], providers: overview.providers, known_models: overview.known_models, permission_labels, goals: [], schedules: [],
    team: {threads: threads.length, rooms_active: 1}, workspace: {folder: 'research', git_branch: null, git_dirty: null}});

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
    let m;
    if (p === '/api/overview') return json({...overview, server_time: Date.now() / 1000});
    if (p === '/api/events') return json({events: url.searchParams.get('task') === 't1' ? t1Events : [], last_seq: 0});
    if ((m = p.match(/^\/api\/agents\/(atlas|coder|risk)$/))) return json(detail(m[1]));
    if (p === '/api/agents/atlas/threads') return json(threads);
    if (p === '/api/agents/atlas/chat') return json(structuredClone(conv));
    if (p === '/api/agents/atlas/usage') return json(null);
    if (p === '/api/threads/th1') return json(thread1);
    if (p === '/api/threads/th1/stop') { thread1.active = false; threads[0].active = false; return json({ok: true}); }
    if (p === '/api/rooms' && method === 'GET') return json(roomList());
    if (p === '/api/rooms' && method === 'POST') {
      const room = {room_id: 'r9', title: body.title || '', topic: body.topic, members: body.member_ids.map(id => ({agent_id: id, name: overview.agents.find(a => a.agent_id === id).name})), chair_id: body.chair_id || body.member_ids[0], state: 'running', created_at: Date.now() / 1000, last_at: Date.now() / 1000, messages: [], speaking: body.member_ids[0]};
      rooms.set('r9', room); return json(room);
    }
    if ((m = p.match(/^\/api\/rooms\/(r\d)$/))) return json(structuredClone(rooms.get(m[1])));
    if ((m = p.match(/^\/api\/rooms\/(r\d)\/(messages|stop|resume)$/))) {
      const room = rooms.get(m[1]);
      if (m[2] === 'messages') room.messages.push({message_id: `op${room.messages.length}`, sender: {kind: 'operator', name: 'You'}, body: body.body, at: Date.now() / 1000, round: 2});
      if (m[2] === 'stop') { room.state = 'stopped'; room.speaking = null; }
      if (m[2] === 'resume') { room.state = 'running'; room.speaking = 'atlas'; }
      return json({...room, messages: undefined});
    }
    if (p === '/api/tasks/t7/approval') return json({ok: true});
    calls.push({unexpected: `${method} ${p}${url.search}`});
    return route.fulfill({status: 404, json: {error: 'Not found.'}});
  });
  const posts = suffix => calls.filter(c => c.method === 'POST' && c.path.endsWith(suffix));
  const results = [];
  const pass = label => { results.push(label); console.log(`  ok  ${label}`); };
  const shot = name => page.screenshot({path: path.join(shots, name)});
  const noHorizontalScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && view.scrollWidth <= view.clientWidth + 1);

  try {
    // ---------- sidebar: Team group and Team rooms nav ----------
    await page.goto(`${BASE}/#/agents/atlas`);
    await page.locator('#chat-request').waitFor();
    await page.waitForFunction(() => document.querySelectorAll('#conversation-tree .team-row').length === 2);
    const teamRows = page.locator('#conversation-tree .team-row');
    assert.equal((await page.locator('#conversation-tree .side-label').first().textContent()).trim(), 'Team');
    assert.equal((await teamRows.nth(0).locator('.tr-main b').textContent()).trim(), 'Coder', 'newest thread first, named by the peer');
    assert((await teamRows.nth(0).locator('small').textContent()).startsWith('Done. parse_csv() now handles'), 'last-message preview');
    assert.equal(await teamRows.nth(0).locator('.avatar').count(), 1, 'peer avatar');
    assert.equal(await teamRows.nth(0).locator('.live-dot').count(), 1, 'live dot while active');
    assert.equal(await teamRows.nth(1).locator('.live-dot').count(), 0);
    assert.equal(await teamRows.nth(0).getAttribute('href'), '#/agents/atlas/thread/th1');
    const roomsNav = page.locator('#space-links a[href="#/rooms"]');
    assert.equal((await roomsNav.locator('span').first().textContent()).trim(), 'Team rooms');
    assert.equal((await roomsNav.locator('.count').textContent()).trim(), '1', 'active rooms count from agent.team');
    await shot('01-sidebar-team.png');
    pass('sidebar: "Team" group lists A↔B threads (peer avatar + name, live dot while active, last-message preview), "Team rooms" nav row with the active count');

    // ---------- thread view ----------
    await teamRows.nth(0).click();
    await page.waitForURL('**/#/agents/atlas/thread/th1');
    await page.locator('.thread-view .agent-msg').first().waitFor();
    const msgs = page.locator('.thread-view .agent-msg:not(.speaking)');
    assert.equal(await msgs.count(), 3);
    assert.deepEqual(await page.locator('.thread-view .agent-msg:not(.speaking) .mname').allTextContents(), ['Atlas', 'Coder', 'Atlas'], 'each message names its agent');
    assert((await page.locator('.thread-view .team-system').textContent()).includes('needs your approval'), 'system notes are shown as notes, not as an agent');
    assert.equal(await msgs.nth(1).locator('.avatar').count(), 1, 'avatar per message');
    assert.equal(await page.locator('.thread-view .code-block').count(), 1, 'markdown in thread messages');
    assert.equal(await page.locator('#view #chat-form, #view #room-form, #view textarea').count(), 0, 'read-only: no composer');
    assert(await page.locator('.thread-view .perm-card [data-approve="t7"]').isVisible(), 'approval card in the thread, decided through the asking task');
    assert((await page.locator('.thread-view .speaking .shimmer').textContent()).includes('Coder is replying'), 'reply indicator names the peer');
    assert((await page.locator('.head-sub').textContent()).includes('Team thread'));
    assert(await page.locator('.team-row.active').count() === 1, 'thread highlighted in the sidebar');
    await shot('02-thread-view.png');
    await page.locator('.thread-view .perm-card [data-approve="t7"]').click();
    assert.deepEqual(posts('/api/tasks/t7/approval').pop().body, {decision: 'approve'});
    await page.locator('[data-thread-stop="th1"]').click();
    assert.equal(posts('/api/threads/th1/stop').length, 1, 'Stop posts to the thread');
    await page.waitForFunction(() => !document.querySelector('[data-thread-stop]') && !document.querySelector('.thread-view .speaking'));
    pass('thread view: two-party chat with avatar + name per message, markdown, approval card (approve posts), "Coder is replying…", Stop → /api/threads/th1/stop, read-only');

    // ---------- rooms list ----------
    await roomsNav.click();
    await page.waitForURL('**/#/rooms');
    await page.locator('.room-row').first().waitFor();
    assert.equal(await page.locator('.room-row').count(), 3);
    assert.deepEqual((await page.locator('.room-row .state-chip').allTextContents()).map(t => t.trim()), ['Running', 'Done', 'Interrupted']);
    assert((await page.locator('.room-row').nth(1).innerText()).includes('Weekly risk check'), 'untitled room shows its topic');
    assert.equal(await page.locator('.room-row').first().locator('.avatar-stack .avatar').count(), 3);
    assert.equal((await page.locator('.head-title').textContent()).trim(), 'Team rooms');
    assert(await page.locator('#space-links a[href="#/rooms"].active').count() === 1, 'nav row active');
    await shot('03-rooms-list.png');
    pass('rooms page: rooms newest first with member avatars, chair, summary/topic and state chips (Running, Done, Interrupted)');

    // ---------- New room dialog: validation, chair picker, payload; survives polling ----------
    await page.locator('[data-new-room]').click();
    await page.locator('#room-dialog[open]').waitFor();
    const picks = page.locator('#room-member-list input[name=member]');
    assert.equal(await picks.count(), 3, 'archived agents are not offered');
    assert.deepEqual(await page.locator('#room-member-list .mp-text b').allTextContents(), ['Atlas', 'Coder', 'Risk']);
    assert.equal(await picks.nth(0).isChecked(), true, 'the current agent is preselected');
    await page.locator('#room-new-form button.primary').click();
    assert.equal((await page.locator('#room-error').textContent()).trim(), 'Pick at least two agents for the room.');
    assert.equal(posts('/api/rooms').length, 0, 'nothing posted while invalid');
    await picks.nth(2).check(); await picks.nth(1).check();
    assert.deepEqual(await page.locator('#room-chair option').allTextContents(), ['Atlas (first picked)', 'Risk', 'Coder'], 'chair picker follows the picks');
    await page.locator('#room-new-form button.primary').click();
    assert.equal((await page.locator('#room-error').textContent()).trim(), 'Add a topic for the room to discuss.');
    await page.locator('#room-new-form textarea[name=topic]').fill('Plan the Q4 launch across three markets');
    await page.locator('#room-new-form input[name=title]').fill('Q4 launch');
    await page.evaluate(() => tick(true));
    await page.waitForTimeout(300);
    assert.equal(await page.locator('#room-new-form textarea[name=topic]').inputValue(), 'Plan the Q4 launch across three markets', 'polling does not wipe the open dialog');
    await page.locator('#room-chair').selectOption('risk');
    await shot('04-new-room-dialog.png');
    await page.locator('#room-new-form button.primary').click();
    await page.waitForURL('**/#/rooms/r9');
    assert.deepEqual(posts('/api/rooms').pop().body, {topic: 'Plan the Q4 launch across three markets', member_ids: ['atlas', 'risk', 'coder'], title: 'Q4 launch', chair_id: 'risk'});
    assert(await page.locator('#room-dialog').isHidden());
    pass('New room dialog: archived agents excluded, current agent preselected, "two or more" and topic validation (no POST), chair picker, survives polling, POST {topic, member_ids, title, chair_id} → opens the room');

    // ---------- a running room with three members ----------
    await page.goto(`${BASE}/#/rooms/r1`);
    await page.locator('.room-view .agent-msg').first().waitFor();
    assert.equal((await page.locator('.room-head h1').textContent()).trim(), 'Launch plan');
    assert.equal(await page.locator('.member-chip').count(), 3);
    const colours = await page.locator('.member-chip .mname').evaluateAll(els => els.map(el => getComputedStyle(el).color));
    assert.equal(new Set(colours).size, 3, `a colour per member: ${colours}`);
    const coderMsgColours = await page.locator('.room-view .agent-msg .mname', {hasText: 'Coder'}).evaluateAll(els => els.map(el => getComputedStyle(el).color));
    assert(coderMsgColours.length === 2 && coderMsgColours.every(c => c === colours[1]), 'the same colour on every message of a member');
    assert.equal(await page.locator('.member-chip .chair-tag').count(), 1, 'chair marked');
    assert.deepEqual(await page.locator('.round-sep').allTextContents(), ['Round 1', 'Round 2'], 'round separators');
    assert.equal(await page.locator('.room-view .team-system').count(), 1, 'Hub notes in the room');
    const op = page.locator('.room-op');
    assert.equal(await op.count(), 1);
    const threadBox = await page.locator('.room-view .chat-thread').boundingBox(), opBox = await op.locator('.bubble').boundingBox();
    assert(Math.abs(threadBox.x + threadBox.width - (opBox.x + opBox.width)) < 2, 'operator message is a right-hand bubble');
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('.room-op .bubble')).backgroundColor), 'rgb(48, 48, 48)');
    assert.equal((await page.locator('.room-view .speaking .shimmer').textContent()).trim(), 'Risk is speaking…');
    assert.equal((await page.locator('.room-head .state-chip').textContent()).trim(), 'Running');
    assert(await page.locator('.rh-side [data-room-stop="r1"]').isVisible() && await page.locator('#room-form .stop-button').isVisible());
    await shot('05-room-running.png');
    // The room composer keeps its draft through a poll repaint, sends with Enter.
    await page.locator('#room-request').fill('Please also estimate the fees.');
    await page.evaluate(() => { lastHtml = ''; return renderRoute(false); });
    assert.equal(await page.locator('#room-request').inputValue(), 'Please also estimate the fees.', 'draft survives repaint');
    await page.locator('#room-request').press('Enter');
    await page.waitForFunction(() => document.querySelectorAll('.room-op').length === 2);
    assert.deepEqual(posts('/api/rooms/r1/messages').pop().body, {body: 'Please also estimate the fees.'});
    assert.equal(await page.locator('#room-request').inputValue(), '');
    // Stop, then Resume.
    await page.locator('.rh-side [data-room-stop="r1"]').click();
    await page.waitForFunction(() => document.querySelector('.room-head .state-chip')?.textContent.trim() === 'Stopped');
    assert.equal(posts('/api/rooms/r1/stop').length, 1);
    assert.equal(await page.locator('.room-view .speaking').count(), 0, 'no speaker once stopped');
    await page.locator('[data-room-resume="r1"]').click();
    await page.waitForFunction(() => document.querySelector('.room-head .state-chip')?.textContent.trim() === 'Running');
    assert.equal(posts('/api/rooms/r1/resume').length, 1);
    assert((await page.locator('.room-view .speaking .shimmer').textContent()).includes('Atlas is speaking'));
    pass('room: 3 members with a stable colour each, chair tag, round separators, operator interjection as a right-hand #303030 bubble, "Risk is speaking…", composer draft survives polling, Enter posts {body}, Stop and Resume');

    // ---------- a finished room: summary card ----------
    await page.goto(`${BASE}/#/rooms/r2`);
    await page.locator('.summary-card').waitFor();
    assert((await page.locator('.summary-card .sc-head').innerText()).includes("Chair's summary") && (await page.locator('.summary-card .sc-head').innerText()).includes('Risk'));
    assert.equal(await page.locator('.summary-card .result strong').count(), 1, 'summary rendered as markdown');
    assert(await page.locator('#room-request').isDisabled() && await page.locator('#room-form .send-button').isDisabled(), 'composer closed when done');
    assert.equal(await page.locator('[data-room-stop], [data-room-resume]').count(), 0);
    await shot('06-room-summary.png');
    await page.goto(`${BASE}/#/rooms/r3`);
    await page.locator('[data-room-resume="r3"]').waitFor();
    pass("finished room: the chair's summary as a highlighted card (markdown, chair named), composer disabled; an interrupted room offers Resume");

    // ---------- inline team tool rows in the chat ----------
    await page.goto(`${BASE}/#/agents/atlas/work/chat1`);
    await page.locator('.worked').waitFor();
    await page.locator('.worked > summary').click();
    await page.waitForFunction(() => document.querySelectorAll('.worked .tool-row.k-team').length === 3);
    const ask = page.locator('.worked .tool-row.k-team').nth(1);
    assert.equal((await ask.locator(':scope > summary .tr-text').innerText()).trim(), 'Asked Coder: Please add a CSV parser to tools/data.py that handles quoted fields.');
    assert.equal((await ask.locator(':scope > summary .tr-verb').textContent()).trim(), 'Coder', 'peer name in bold');
    assert.equal((await page.locator('.worked .tool-row.k-team').nth(0).locator('.tr-verb').textContent()).trim(), 'Listed your agents');
    await ask.locator(':scope > summary').click();
    assert((await ask.locator('.tr-reply').innerText()).includes('Coder replied') && (await ask.locator('.tr-reply code').count()) === 1, 'reply shown on expand');
    const discuss = page.locator('.worked .tool-row.k-team').nth(2);
    assert((await discuss.innerText()).includes('Started a team discussion'));
    await discuss.locator(':scope > summary').click();
    assert.equal(await discuss.locator('a[href="#/rooms/r1"]').count(), 1, 'discussion links to its room');
    await shot('07-chat-team-rows.png');
    await ask.locator('a', {hasText: 'Open thread'}).click();
    await page.waitForURL('**/#/agents/atlas/thread/th1');
    await page.locator('.thread-view .agent-msg').first().waitFor();
    pass('chat tool rows: "Asked **Coder**: …" expands to the reply with "Open thread" (opens the thread), "Listed your agents", "Started a team discussion" linking to the room');

    // ---------- the team ability in the agent's settings ----------
    await page.goto(`${BASE}/#/agents/atlas/config`);
    const teamBox = page.locator('#config-form label.check', {hasText: TEAM_LABEL});
    await teamBox.waitFor();
    assert(await teamBox.locator('input[name="perm-team"]').isChecked());
    await page.locator('#details-toggle').click();
    await page.locator('[data-rp-tab=agent]').click();
    await page.locator('#rp-body [data-details-tab=access]').click();
    assert((await page.locator('#rp-body').innerText()).includes(TEAM_LABEL));
    await page.locator('#rp-close').click();
    pass(`abilities: the backend's "team" label ("${TEAM_LABEL}") in the agent's settings and Access list`);

    // ---------- 375 px ----------
    await page.setViewportSize({width: 375, height: 812});
    for (const hash of ['#/rooms', '#/rooms/r1', '#/rooms/r2', '#/agents/atlas/thread/th1', '#/agents/atlas/work/chat1']) {
      await page.goto(`${BASE}/${hash}`); await page.waitForTimeout(400);
      assert(await noHorizontalScroll(), `no horizontal scroll at 375 on ${hash}`);
    }
    await page.goto(`${BASE}/#/rooms/r1`);
    await page.locator('.room-view .agent-msg').first().waitFor();
    await shot('08-mobile-room.png');
    await page.locator('#sidebar-toggle').click();
    await page.waitForFunction(() => document.querySelectorAll('#conversation-tree .team-row').length === 2);
    await shot('09-mobile-sidebar-team.png');
    await page.mouse.click(360, 400);
    await page.goto(`${BASE}/#/rooms`);
    await page.locator('[data-new-room]').click();
    await page.locator('#room-dialog[open]').waitFor();
    assert((await page.locator('#room-dialog').boundingBox()).width <= 375, 'dialog fits');
    await shot('10-mobile-new-room.png');
    await page.keyboard.press('Escape');
    pass('375 px: no horizontal scroll on rooms, a room, a finished room, a thread and the chat; team drawer and New room dialog fit');

    assert.deepEqual(calls.filter(c => c.unexpected).map(c => c.unexpected), [], 'no unexpected API calls');
    assert.deepEqual(csp, [], 'no CSP violations');
    assert.deepEqual(errors, [], 'no page errors');
    console.log(`PASS: ${results.length} team groups, 0 page errors, 0 CSP violations, synthetic API only. Screenshots in ${shots}`);
  } catch (error) {
    await page.screenshot({path: path.join(shots, 'failure.png')}).catch(() => {});
    console.error('Unexpected calls:', calls.filter(c => c.unexpected));
    console.error('Page errors:', errors, 'CSP:', csp);
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
