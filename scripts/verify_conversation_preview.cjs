// Headless interaction verification against the disposable synthetic preview only.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  await page.addInitScript(() => {
    window.microphoneRequests = 0;
    if (navigator.mediaDevices) navigator.mediaDevices.getUserMedia = () => {window.microphoneRequests++; throw new Error('Host microphone forbidden in this test');};
  });
  const errors=[]; page.on('pageerror',error=>errors.push(error.message));
  try {
    await page.goto(process.env.JARVIS_TEST_URL);
    await page.locator('#agent-switch').selectOption({label:'Atlas'});
    await page.getByRole('heading',{name:'Atlas',exact:true}).waitFor();
    await page.getByRole('button',{name:'＋ New conversation',exact:true}).click();
    await page.locator('#chat-form input').fill('Interface exploration');
    await page.getByRole('button',{name:'Create conversation',exact:true}).click();
    await page.getByRole('heading',{name:'Interface exploration',exact:true}).waitFor();
    await page.locator('#message-body').fill('Draft stays with Atlas.');
    await page.locator('#agent-switch').selectOption({label:'Nova'});
    await page.getByRole('heading',{name:'Nova',exact:true}).waitFor();
    assert.equal(await page.locator('#message-body').inputValue(),'');
    await page.locator('#agent-switch').selectOption({label:'Atlas'});
    await page.getByRole('heading',{name:'Interface exploration',exact:true}).waitFor();
    assert.equal(await page.locator('#message-body').inputValue(),'Draft stays with Atlas.');
    await page.getByRole('button',{name:'Dictation unavailable',exact:true}).click();
    await page.getByRole('heading',{name:'Dictation unavailable',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.microphoneRequests),0);
    await page.getByRole('button',{name:'Close dictation',exact:true}).click();
    await page.getByRole('button',{name:'Permissions',exact:true}).click();
    await page.locator('#grant-attachments').check();
    await page.locator('#grant-file-preview').check();
    await page.getByRole('button',{name:'Close permissions',exact:true}).click();
    await page.locator('#attachment-picker').setInputFiles({name:'example.md',mimeType:'text/markdown',buffer:Buffer.from('Synthetic attachment only')});
    await page.locator('#staged-attachments').getByText(/example.md/).waitFor();
    await page.locator('#staged-attachments').getByRole('button',{name:'Remove',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#staged-attachments').textContent.includes('example.md'));
    async function send(kind,body) {
      await page.locator('#message-kind').selectOption(kind);
      await page.locator('#message-body').fill(body);
      await page.locator('#send-message').click();
      await page.locator('#messages').getByText(body,{exact:true}).waitFor();
    }
    await send('work','Explore a concise project overview.');
    await page.getByRole('button',{name:'Reply',exact:true}).waitFor({timeout:15000});
    assert.equal(await page.locator('#message-body').isEnabled(),true);
    await send('reply','Emphasize clear navigation.');
    await page.getByText(/Simulation: reply applied at checkpoint/).waitFor({timeout:10000});
    await send('steer','Use three short sections and preserve the current progress.');
    await page.getByText(/Simulation: steer applied at checkpoint/).waitFor({timeout:10000});
    await send('discuss','Can we discuss the tradeoffs while this continues?');
    await page.getByText(/Simulation: discussion received/).waitFor({timeout:10000});
    await page.locator('#work-strip').getByRole('button',{name:'Pause',exact:true}).click();
    await page.locator('#work-strip').getByText('PAUSED',{exact:true}).waitFor();
    const paused = await page.locator('#work-strip').innerText();
    await page.waitForTimeout(1400);
    assert.equal(await page.locator('#work-strip').innerText(),paused);
    await page.reload();
    await page.getByRole('heading',{name:'Interface exploration',exact:true}).waitFor();
    await page.getByText('Use three short sections and preserve the current progress.',{exact:true}).waitFor();
    await page.locator('#work-strip').getByRole('button',{name:'Resume',exact:true}).click();
    await page.getByRole('button',{name:'Files',exact:true}).click();
    await page.getByRole('button',{name:'Browse authorized workspace',exact:true}).click();
    await page.getByRole('button',{name:'▤ brief.md',exact:true}).click();
    await page.locator('#preview-body').getByText(/Synthetic project/).waitFor();
    await page.getByRole('button',{name:'Close preview'}).click();
    await page.locator('#agent-switch').selectOption({label:'Nova'});
    await page.getByRole('heading',{name:'Nova',exact:true}).waitFor();
    assert.equal(await page.getByText('Explore a concise project overview.',{exact:true}).count(),0);
    await page.locator('#agent-switch').selectOption({label:'Atlas'});
    await page.getByRole('heading',{name:'Interface exploration',exact:true}).waitFor();
    await page.getByText(/Simulation completed/).waitFor({timeout:30000});
    await page.getByRole('button',{name:'Artifacts',exact:true}).click();
    await page.getByRole('button',{name:/Simulation result.txt/}).click();
    assert.match(await page.locator('#preview-body').innerText(),/three short sections/);
    await page.getByRole('button',{name:'Close preview'}).click();
    await send('work','Second example to verify cancellation.');
    await page.getByRole('button',{name:'Reply',exact:true}).waitFor({timeout:15000});
    await page.locator('#work-strip').getByRole('button',{name:'Stop',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#work-strip').hidden);
    await page.getByRole('button',{name:'Activity',exact:true}).click();
    await page.locator('#context-content').getByText('CANCELLED',{exact:true}).waitFor();
    const screenshot = path.join(os.tmpdir(),'jarvis-conversation-verified.png');
    await page.screenshot({path:screenshot,fullPage:true});
    assert.deepEqual(errors,[]);
    for (const width of [900,760,430]) {
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`horizontal overflow at ${width}`);
    }
    console.log(JSON.stringify({result:'PASS',checks:['agent dropdown','new conversation','per-agent draft restore','dictation unavailable without microphone','local grants','explicit attachment staging/removal','clarification','mid-run steering acknowledgement','discussion during work','pause checkpoint','reload','resume','authorized file','project isolation','artifact','stop','no JS errors','responsive widths'],screenshot}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
