// Explicitly authorized synthetic live tests. Never run against private chats.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const os=require('node:os');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 try {
  await page.goto(process.env.JARVIS_TEST_URL);
  const agentName=process.env.JARVIS_TEST_AGENT || 'Claude Chat';
  await page.locator('#agent-switch option').filter({hasText:agentName}).waitFor({state:'attached'});
  await page.locator('#agent-switch').selectOption({label:agentName});
  await page.waitForFunction(n=>document.querySelector('#thread-title').textContent===n,agentName);
  const read=()=>page.evaluate(async()=>{
   const a=JSON.parse(localStorage.getItem('jarvis-agent-selections')||'{}');
   const id=document.querySelector('#agent-switch').value;
   const selection=a[id]||{project:'synthetic',chat:null};
   return fetch(`/api/conversation?project=${selection.project}&agent=${id}${selection.chat?'&chat='+selection.chat:''}`,{headers:{Authorization:'Bearer '+sessionStorage.getItem('jarvis-session')}}).then(r=>r.json());
  });
  async function send(body){
   await page.locator('#message-body').fill(body);
   await page.locator('#send-message').click();
   const end=Date.now()+190000;
   while(Date.now()<end){
    const data=await read();const turn=data.live_turns?.at(-1);
    if(turn&&turn.instruction===body&&['COMPLETED','FAILED'].includes(turn.state)){
     const response=data.messages.find(m=>m.message_id===turn.response_id);
     console.log(JSON.stringify({provider:turn.provider,state:turn.state,response:response.body,usage:JSON.parse(turn.usage)}));
     assert.equal(turn.state,'COMPLETED',response.body);return response.body;
    }
    await page.waitForTimeout(250);
   }
   throw new Error('Timed out waiting for actual provider response');
  }
  const first=await send('Synthetic integration check: remember the fictional lighthouse color is amber. Reply with the color only.');
  assert.match(first,/amber/i);
  const second=await send('What is the fictional lighthouse color I gave you? Reply with the color only.');
  assert.match(second,/amber/i);
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#messages').textContent.toLowerCase().includes('amber'));
  assert.match(await page.locator('#provider-notice').innerText(),/Verified .*subscription response/);
  await page.screenshot({path:path.join(os.tmpdir(),'jarvis-live-subscription-verified.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  console.log(`PASS: real ${agentName} multi-turn responses through UI/service, persisted reload, provider status, no browser errors.`);
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
