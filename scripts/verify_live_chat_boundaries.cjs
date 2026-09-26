// Authorized synthetic live Codex checks after restarting the disposable server.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage();
 try{
  await page.goto(process.env.JARVIS_TEST_URL);
  await page.locator('#agent-switch option').filter({hasText:'Codex Chat'}).waitFor({state:'attached'});
  await page.locator('#agent-switch').selectOption({label:'Codex Chat'});
  await page.waitForFunction(()=>document.querySelector('#thread-title').textContent==='Codex Chat');
  async function latest(){return page.evaluate(async()=>{
   const id=document.querySelector('#agent-switch').value;
   const selection=JSON.parse(localStorage.getItem('jarvis-agent-selections')||'{}')[id]||{project:'synthetic'};
   return fetch(`/api/conversation?project=${selection.project}&agent=${id}${selection.chat?'&chat='+selection.chat:''}`,{headers:{Authorization:'Bearer '+sessionStorage.getItem('jarvis-session')}}).then(r=>r.json());
  });}
  async function send(body,expectedState='COMPLETED'){
   await page.locator('#message-body').fill(body);await page.locator('#send-message').click();
   const end=Date.now()+190000;
   while(Date.now()<end){
    const data=await latest(),turn=data.live_turns?.at(-1);
    if(turn?.instruction===body&&['COMPLETED','FAILED'].includes(turn.state)){
     const response=data.messages.find(m=>m.message_id===turn.response_id).body;
     assert.equal(turn.state,expectedState,response);
     console.log(JSON.stringify({state:turn.state,model:turn.model,response}));return response;
    }
    await page.waitForTimeout(250);
   }
   throw new Error('Turn timed out');
  }
  async function model(value){
   await page.locator('#settings-toggle').click();await page.locator('#model-name').fill(value);
   await page.locator('#model-form button.primary').click();
   await page.waitForFunction(v=>document.querySelector('#composer-model').textContent.includes(v),value);
  }
  assert.match(await send('After the service restart, what fictional lighthouse color did I give you? Reply with the color only.'),/amber/i);
  await page.locator('#new-chat').click();await page.locator('#chat-form input').fill('Live isolation check');
  await page.locator('#chat-form button.primary').click();
  await page.waitForFunction(()=>document.querySelector('#thread-title').textContent==='Live isolation check');
  assert.match(await send('If no earlier message in this supplied conversation gave a lighthouse color, reply UNKNOWN. Otherwise give that color. No explanation.'),/UNKNOWN/);
  await model('gpt-5.5');
  assert.match(await send('Synthetic unsupported-model check. Reply hello.','FAILED'),/no verified tool-free configuration/);
  await model('default');
  assert.match(await send('If this supplied conversation has no earlier user messages, reply UNKNOWN. Otherwise reply HISTORY. No explanation.'),/UNKNOWN/);
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#messages').textContent.includes('UNKNOWN'));
  console.log('PASS: actual restart recall, new-chat isolation, unsupported model refusal, model-segment reset and reload.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
