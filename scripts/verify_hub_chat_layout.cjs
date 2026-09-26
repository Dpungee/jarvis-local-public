// UI contract checks with synthetic API data. Never executes a real agent.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const root = path.resolve(__dirname, '../jarvis/agent_hub_static');
(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  let sent;
  const conversation = {messages:[],live_turns:[]};
  const agent = {agent_id:'atlas', name:'Atlas', role:'Research assistant', purpose:'Explore ideas and turn them into useful work.', project_id:'research', project_name:'Research', provider:'codex-cli', model:'gpt-5.6-sol', lifecycle:'RUNNING', archived:false, status:{code:'idle',label:'Ready',detail:'Ready for a message',tone:'ok'}, counts:{running:0,queued:0,blocked:0,completed:1,failed:0}, tasks:[], approvals:[], artifacts:[], events:[], errors:[], chats:[], permissions:{}, permission_labels:{}, known_models:{'codex-cli':['gpt-5.6-sol','gpt-5.5']}};
  const task = {task_id:'task1',title:'Explore the product idea',agent,project_name:'Research',state:'COMPLETED',request:'Help me explore a new product idea.',result:'Let’s start with the problem you want to solve.\n\n**Three useful questions**\n- Who is it for?\n- What do they do today?\n- What would make the experience better?',model_configured:agent.model,model_used:agent.model,actions:[],steering:[],timeline:[],artifacts:[],tool_calls:0,attempt:1,created_at:Date.now()/1000};
  agent.tasks = [{...task,agent:undefined}];
  const overview = {agents:[agent],projects:[{project_id:'research',name:'Research'}],providers:{'codex-cli':{installed:true,authenticated:true,models:[]}},defaults:{provider:'codex-cli',model:agent.model},capacity:{running:0,slots:2},server_time:Date.now()/1000,known_models:agent.known_models,permissions:{labels:{},defaults:{}},last_seq:0};
  await page.route('http://hub.test/**', async route => {
    const url = new URL(route.request().url());
    if (!url.pathname.startsWith('/api/')) {
      const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      assert(['index.html','hub.js','hub.css'].includes(file));
      return route.fulfill({body:fs.readFileSync(path.join(root,file)),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
    }
    let data;
    if (url.pathname === '/api/overview') data = overview;
    else if (url.pathname === '/api/events') data = {events:[],last_seq:0};
    else if (url.pathname === '/api/agents/atlas/chats') {data={chat_id:'chat1',title:'Keep my draft'};agent.chats=[data];}
    else if (url.pathname === '/api/agents/atlas/chat') data=conversation;
    else if (url.pathname === '/api/agents/atlas/messages') {
      sent=route.request().postDataJSON();
      conversation.messages.push({role:'operator',body:sent.body,state:'COMPLETED'},{role:'assistant',body:'Hello. Let’s talk.',state:'COMPLETED'});
      data={ok:true};
    }
    else if (url.pathname === '/api/agents/atlas') data = agent;
    else if (url.pathname === '/api/tasks/task1') data = task;
    else throw Error('Unexpected API: '+url.pathname);
    await route.fulfill({json:data});
  });
  try {
    await page.goto('http://hub.test/#/agents/atlas');
    await page.locator('#chat-request').waitFor();
    assert.equal(await page.locator('input[name=title]').count(),0);
    assert.equal(await page.locator('#project-tree summary').textContent(),'▱ Research');
    await page.locator('#chat-request').fill('Keep my draft');
    await page.evaluate(() => { pageData.status.detail='Updated state'; paint(renderAgent(pageData)); });
    assert.equal(await page.locator('#chat-request').inputValue(),'Keep my draft');
    await page.locator('#chat-form button').click();
    await page.locator('.assistant-message').waitFor();
    assert.equal(sent.body,'Keep my draft'); assert.equal(sent.chat_id,'chat1'); assert(sent.request_id);
    assert.deepEqual(Object.keys(sent).sort(),['body','chat_id','request_id']);
    assert((await page.locator('.chat-thread').boundingBox()).width > 700);
    await page.reload();
    await page.locator('.assistant-message').waitFor();
    await page.screenshot({path:path.join(os.tmpdir(),'jarvis-hub-chat-desktop.png'),fullPage:true});
    await page.locator('#chat-request').fill('Please retain this follow-up');
    await page.evaluate(() => {pageData.conversation.messages[1].body='An updated reply'; paint(renderAgent(pageData));});
    assert.equal(await page.locator('#chat-request').inputValue(),'Please retain this follow-up');
    assert((await page.locator('.assistant-message').textContent()).includes('An updated reply'));
    assert.equal(await page.locator('[data-task-action], #task-form, #steer-form').count(),0);
    await page.evaluate(() => {pageData.conversation.agent_turns=[{task_id:'active1',state:'RUNNING',progress:'Searching the web',actions:['cancel'],artifacts:[]}]; paint(renderAgent(pageData));});
    assert(await page.locator('[data-chat-cancel]').isVisible());
    assert(await page.locator('#chat-form button').isEnabled());
    await page.locator('#chat-request').press('Shift+Enter');
    assert((await page.locator('#chat-request').inputValue()).includes('\n'));
    assert.equal(conversation.messages.length,2);
    await page.locator('#chat-request').press('Enter');
    await page.waitForFunction(()=>document.querySelectorAll('.assistant-message').length===2);
    for (const width of [1440,900,760,430]) {
      await page.setViewportSize({width,height:900});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),`Overflow at ${width}`);
    }
    await page.locator('#sidebar-toggle').click();
    await page.locator('#conversation-tree .new-conversation').click();
    await page.locator('#chat-request').waitFor();
    assert.equal(await page.locator('#chat-request').inputValue(),'');
    const bounds=await page.locator('#chat-form').boundingBox();
    assert(bounds.width <= 430);
    await page.screenshot({path:path.join(os.tmpdir(),'jarvis-hub-chat-mobile.png'),fullPage:true});
    await page.goto('http://hub.test/#/tasks/task1');
    await page.waitForURL('**/#/agents/atlas');
    await page.locator('#chat-form').waitFor();
    assert.equal(agent.tasks.length,1);
    assert.deepEqual(errors,[]);
    console.log('PASS: project folders, saved chat/reload, multi-turn chat composer, inline agent progress/Stop, follow-ups enabled while busy, draft retention, Enter and Shift-Enter, mobile sidebar, old links, 4 widths, no browser errors. Synthetic API only.');
  } finally {await browser.close();}
})();
