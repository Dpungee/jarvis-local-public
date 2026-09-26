// Local-only UI contract checks; no message is submitted to a provider.
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
  const target=await page.evaluate(()=>{
   const saved=thread;
   thread={work:[],live_turns:[{turn_id:'active',state:'RUNNING'},{turn_id:'later',state:'QUEUED'}]};
   const id=activeWork().run_id;thread=saved;return id;
  });
  assert.equal(target,'active');
  await page.locator('#message-kind').selectOption('steer');
  assert.match(await page.locator('#composer-hint').innerText(),/does not change an in-flight response/);
  await page.locator('#message-kind').selectOption('work');
  assert.match(await page.locator('#composer-hint').innerText(),/No file edits or tools/);
  console.log('PASS: steering targets running turn before queued followups; text-only semantics visible. No provider calls.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
