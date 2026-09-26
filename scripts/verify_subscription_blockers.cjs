// Real synthetic provider attempts, asserting honest blocked states rather than readiness.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const os=require('node:os');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 try{
  await page.goto(process.env.JARVIS_TEST_URL);
  await page.locator('#agent-switch option').filter({hasText:'Claude Chat'}).waitFor({state:'attached'});
  for(const [name,expected] of [['Claude Chat','Claude subscription login required']]){
   await page.locator('#agent-switch').selectOption({label:name});
   await page.waitForFunction(n=>document.querySelector('#thread-title').textContent===n,name);
   if (!process.env.JARVIS_RESTART_CHECK) {
    await page.locator('#message-body').fill('Synthetic connection check. Reply with hello only.');
    await page.locator('#send-message').click();
   }
   await page.waitForFunction(text=>document.querySelector('#messages').textContent.includes(text),expected,{timeout:45000});
   assert.match(await page.locator('#messages').innerText(),/FAILED/);
   await page.reload();
   await page.waitForFunction(text=>document.querySelector('#messages').textContent.includes(text),expected);
   console.log(`${name}: actionable blocker shown through actual UI and retained after reload`);
  }
  await page.screenshot({path:path.join(os.tmpdir(),'jarvis-subscription-blockers.png'),fullPage:true});
  console.log(`PASS: ${process.env.JARVIS_RESTART_CHECK?'service restart and reload persistence':'real failure paths'}, truthful provider status. No successful live response claimed.`);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
