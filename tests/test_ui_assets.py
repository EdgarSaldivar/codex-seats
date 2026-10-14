"""Browser behavior against synthetic routes only; no manager or provider is started.

Uses an existing Node + Playwright installation, otherwise skips browser coverage.
Set PLAYWRIGHT_MODULE to an installed module path, UI_BROWSER_EXECUTABLE to an
existing browser, and UI_SCREENSHOT_DIR to retain screenshots. No dependency is
downloaded by this test.
"""
from pathlib import Path
import os
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "lib" / "codex_seats" / "ui"
NODE = shutil.which("node") or ("/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").exists() else None)

BROWSER_TEST = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let pw;
try { pw = require(process.env.PLAYWRIGHT_MODULE || 'playwright'); }
catch (_) { console.log('SKIP: existing Playwright module unavailable'); process.exit(77); }
(async () => {
  let browser;
  try { browser = await pw.chromium.launch({headless:true, executablePath:process.env.UI_BROWSER_EXECUTABLE}); }
  catch (error) { console.log('SKIP: browser unavailable: '+error.message); process.exit(77); }
  try {
    if (process.env.UI_SCREENSHOT_DIR) fs.mkdirSync(process.env.UI_SCREENSHOT_DIR,{recursive:true});
    const context = await browser.newContext({viewport:{width:1024,height:900}});
    const page = await context.newPage();
    const errors = [], calls = [];
    page.on('pageerror', error => errors.push(error.message));
    const now = Math.floor(Date.now()/1000);
    const credit = {fingerprint:'synthetic-credit',title:'Test credit',status:'available',granted_at:now-600,expires_at:now+3600,expiry_status:'reported'};
    const seat = {id:'seat-test',label:'Test account',role:'worker',lifecycle:'active',busy:false,task:null,health:'ok',freshness:'fresh',observed_at:now,identity:{status:'fresh',email:'synthetic@example.test',observed_plan_type:'business'},capacity:{profile:'business-standard',label:'Standard',relative_capacity:1,source:'openai-published-baseline'},windows:[{name:'primary',window_minutes:120,remaining_pct:62,resets_at:now+1000,freshness:'fresh',observed_at:now},{name:'secondary',window_minutes:10080,remaining_pct:48,resets_at:now+50000,freshness:'fresh',observed_at:now}],banked_resets:{status:'fresh',available_count:2,credits:[credit],observed_at:now,last_error:null}};
    const state = {version:1,observed_at:now,seats:[seat],local_seats:[{id:'local-test',label:'Local test',host:'synthetic-host',lifecycle:'active',mode:'shadow',status:'unavailable',reason:'Synthetic host offline',busy:false,provider:'llama'}],claude:{account:'claude',configured_mode:'manual_only',mode:'manual_only',mode_reason:null,active_usage_refresh:false,status:{status:'ok',busy:false},headroom:{small_min_pct:10,big_min_pct:15},subscription_tier:{configured:'Max 5x',detected:null,source:'configured'},usage:{read:true,fresh:false,age_seconds:40000,windows:{five_hour:{remaining_percentage:78,resets_at:now+100},seven_day:{remaining_percentage:55,resets_at:now+9000}}},warnings:[]},policy:{'review.mode':'off','review.strategy':'single','execution.delegation_policy':'auto','execution.inline_max_minutes':5,'execution.head_permissions':'safe','execution.head_worker_fallback':false,'ui.theme':'system','ui.density':'comfortable','ui.accent':'teal'},refresh:{active:false,queued:false,last_result:null},warnings:[]};
    let op, sequence = 0, stateFailure = false, confirmFailure = false, completeError = false, holdPreparing = false, operationFailure = false;
    const json = (route, data, status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      assert.equal(url.origin,'http://ui.test','no external resources or API hosts');
      const p = url.pathname;
      if (p.startsWith('/api/')) {
        assert.equal(req.headers()['x-codex-seats-session'],'synthetic-session');
        const body = req.method() === 'POST' ? req.postDataJSON() : null;
        if (body) assert.equal(req.headers().origin,'http://ui.test');
        calls.push({p,method:req.method(),body});
        if (p === '/api/state') return json(route,stateFailure ? {error:'Synthetic cache unavailable'} : state,stateFailure ? 503 : 200);
        if (p === '/api/refresh') return json(route,{message:'Synthetic watcher refresh queued'});
        if (p === '/api/preview') {
          op = {id:`op-${++sequence}`,kind:body.kind,seat:body.seat,status:body.kind === 'redeem-reset' ? 'preparing' : 'ready',preview:{title:'Review synthetic action',diff:['- old\n+ new'],warnings:['Synthetic preview warning'],account_email:seat.identity.email,seat:body.seat,available_count:2,credit},result:null,error:null,changes:body.changes};
          return json(route,op);
        }
        if (p === '/api/cancel') { assert.equal(body.id,op.id); op.status='canceled'; return json(route,op); }
        if (p === '/api/confirm') {
          assert.equal(body.id,op.id);
          if (confirmFailure) return json(route,{error:'Synthetic preview conflict'},409);
          op.status='running'; return json(route,op);
        }
        if (p.startsWith('/api/operations/')) {
          if (operationFailure) return json(route,{error:'Synthetic status unavailable'},503);
          if (op.status === 'preparing' && !holdPreparing) op.status='ready';
          else if (op.status === 'running') {
            op.status=completeError ? 'error' : 'done';
            op.error=completeError ? 'Synthetic provider error' : null;
            op.result={message:completeError ? 'Unknown provider outcome' : 'Synthetic action complete',outcome:completeError ? 'unknown' : 'success'};
            if (!completeError) {
              if (op.kind === 'settings') Object.assign(state.policy,op.changes);
              if (op.kind === 'claude') {
                const c=op.changes;
                if ('mode' in c) state.claude.configured_mode=state.claude.mode=c.mode;
                if ('subscription_tier' in c) state.claude.subscription_tier.configured=c.subscription_tier;
                if ('active_usage_refresh' in c) state.claude.active_usage_refresh=c.active_usage_refresh;
                if ('small_task_min_headroom_pct' in c) state.claude.headroom.small_min_pct=c.small_task_min_headroom_pct;
                if ('big_task_min_headroom_pct' in c) state.claude.headroom.big_min_pct=c.big_task_min_headroom_pct;
              }
              if (op.kind === 'redeem-reset') seat.banked_resets.available_count=1;
              if (op.kind === 'pause') seat.lifecycle=seat.busy ? 'draining' : 'inactive';
              if (op.kind === 'reactivate') seat.lifecycle='active';
              if (op.kind === 'edit-seat') seat.label=op.changes.label;
              if (op.kind === 'set-role') state.seats.find(s => s.id === op.seat).role=op.changes.role;
              if (op.kind === 'remove-seat') state.seats=[];
              if (op.kind === 'add-seat') state.seats=[{...seat,label:op.changes.label,lifecycle:'inactive'}];
            }
          }
          return json(route,op);
        }
        throw new Error('Unexpected API request '+p);
      }
      if (p === '/' || p === '/app.js' || p === '/style.css') {
        const name = p === '/' ? 'index.html' : p.slice(1);
        return route.fulfill({contentType:name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html',body:fs.readFileSync(path.join(process.env.UI_ASSETS,name),'utf8')});
      }
      throw new Error('Unexpected resource '+p);
    });
    await page.clock.install();
    await page.goto('http://ui.test/#session=synthetic-session');
    assert.equal(new URL(page.url()).hash,'','launch fragment is cleared');
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('codex-seats-session')),'synthetic-session');
    const action = name => page.locator(`[data-action="${name}"]`);
    const field = (scope,key) => page.locator(`[data-scope="${scope}"][data-key="${key}"]`);
    const has = async text => {
      await page.waitForFunction(text => document.body.innerText.includes(text), text, {timeout:5000});
      assert.ok((await page.locator('body').innerText()).includes(text),text);
    };
    const tick = async (ms=5100) => { await page.clock.runFor(ms); await page.waitForFunction(() => !document.querySelector('#content .cs-loading')); };
    const ready = async () => { await page.clock.runFor(2100); await action('confirm').waitFor(); assert.equal(await action('confirm').isEnabled(),true); };
    const done = async () => { await page.clock.runFor(2100); await action('dismiss-operation').waitFor(); };
    await page.locator('#seat-0').waitFor();
    await has('2-hour window'); await has('62%');
    assert.equal(await action('redeem-reset').isEnabled(),true);
    // Pending preparation survives navigation, cancellation never confirms.
    holdPreparing=true;
    await action('redeem-reset').click(); await action('cancel').waitFor();
    await page.locator('#nav-settings').click(); await has('preparing');
    await action('cancel').click(); await action('dismiss-operation').waitFor();
    assert.equal(calls.filter(c=>c.p==='/api/confirm').length,0);
    await action('dismiss-operation').click(); await page.locator('#nav-inventory').click();
    holdPreparing=false;
    await action('redeem-reset').click(); await ready();
    await has('synthetic@example.test'); await has('irreversibly spends one'); await has('Test credit');
    const before = calls.filter(c=>c.p==='/api/confirm').length;
    await action('confirm').evaluate(el=>{el.click();el.click();});
    await page.waitForFunction(()=>document.querySelector('#operation').textContent.includes('running'));
    assert.equal(calls.filter(c=>c.p==='/api/confirm').length,before+1);
    assert.equal(await page.locator('.cs-bankcount').innerText(),'2','no optimistic bank decrement');
    await done(); assert.equal(await page.locator('.cs-bankcount').innerText(),'1');
    await action('dismiss-operation').click();
    // Focus, draft preservation, validation, exact typed keys and confirmed appearance.
    await page.locator('#nav-settings').click();
    assert.equal(await field('settings','review.strategy').isEnabled(),true);
    await field('settings','execution.inline_max_minutes').fill('241');
    assert.equal(await page.locator('#settings-save').isDisabled(),true);
    await field('settings','execution.inline_max_minutes').fill('12');
    await field('settings','ui.theme').selectOption('dark');
    await field('settings','ui.accent').selectOption('indigo');
    await field('settings','ui.density').selectOption('compact');
    await field('settings','execution.inline_max_minutes').focus();
    await tick();
    assert.equal(await field('settings','execution.inline_max_minutes').inputValue(),'12');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'settings-execution.inline_max_minutes');
    assert.equal(await page.locator('#codex-seats').getAttribute('data-accent'),'teal');
    await page.locator('#settings-save').click(); await action('confirm').waitFor();
    assert.equal(op.changes['execution.inline_max_minutes'],12);
    assert.equal(op.changes['ui.theme'],'dark');
    await action('confirm').click(); await done();
    assert.equal(await page.evaluate(()=>document.documentElement.style.colorScheme),'dark');
    assert.equal(await page.locator('#codex-seats').getAttribute('data-accent'),'indigo');
    assert.equal(await page.locator('#codex-seats').getAttribute('data-density'),'compact');
    await action('dismiss-operation').click();
    await field('settings','review.strategy').selectOption('cross');
    await action('discard-settings').click();
    assert.equal(await field('settings','review.strategy').inputValue(),'single');
    // Claude uses actual report structure, does not guess observed Max subtype.
    await page.locator('#nav-claude').click(); await has('observed tier: Unknown'); await has('Stale / unknown');
    await page.locator('#claude-headroom summary').click();
    await field('claude','small_task_min_headroom_pct').fill('30');
    assert.equal(await page.locator('#claude-save').isDisabled(),true);
    await field('claude','big_task_min_headroom_pct').fill('40');
    await field('claude','mode').selectOption('review_only');
    await field('claude','subscription_tier').selectOption('');
    await field('claude','active_usage_refresh').check();
    await page.locator('#claude-save').click(); await action('confirm').waitFor();
    assert.equal(op.changes.subscription_tier,null); assert.equal(op.changes.active_usage_refresh,true);
    assert.equal(op.changes.small_task_min_headroom_pct,30);
    await action('confirm').click(); await done(); await action('dismiss-operation').click();
    // Stale, unknown, busy, local placement, and malicious data cannot appear fresh or execute.
    await page.locator('#nav-inventory').click();
    seat.freshness='stale'; seat.windows[0].remaining_pct=null; seat.banked_resets.available_count=null;
    seat.label='<img src=x onerror="window.xss=1">'; await tick();
    assert.equal(await action('redeem-reset').isDisabled(),true);
    assert.equal(await page.locator('.cs-bankcount').innerText(),'Unknown');
    assert.equal(await page.locator('img').count(),0); assert.equal(await page.evaluate(()=>window.xss),undefined);
    await page.locator('#local-0').click(); await has('synthetic-host'); await has('Synthetic host offline'); await has('codex-seat dash --manage');
    assert.equal(await action('redeem-reset').count(),0);
    await page.locator('#seat-0').click(); seat.freshness='fresh'; seat.busy=true; await tick();
    assert.equal(await action('redeem-reset').isDisabled(),true); await has('Pause / drain');
    await action('pause').click(); await action('confirm').waitFor(); await action('confirm').click(); await done(); await action('dismiss-operation').click();
    assert.equal(seat.lifecycle,'draining'); seat.busy=false; seat.lifecycle='inactive'; await tick();
    await action('reactivate-primary').click(); await action('confirm').waitFor(); await action('confirm').click(); await done(); await action('dismiss-operation').click();
    // A 409 never retries POST; explicit re-preview is guarded by cancellation.
    confirmFailure=true;
    await action('pause').click(); await action('confirm').waitFor(); await action('confirm').click();
    await action('repreview').waitFor(); await has('HTTP 409');
    const afterConflict=calls.filter(c=>c.p==='/api/confirm').length;
    await tick(); assert.equal(calls.filter(c=>c.p==='/api/confirm').length,afterConflict);
    await action('repreview').click(); await action('confirm').waitFor(); confirmFailure=false;
    completeError=true; await action('confirm').click(); await done(); await has('Unknown provider outcome');
    assert.equal(await action('repreview').count(),0); await action('dismiss-operation').click(); completeError=false;
    // Cache errors retain registry, never turn failure into empty inventory.
    stateFailure=true; await tick(); await has('Synthetic cache unavailable'); await has('<img src=x');
    assert.equal(await action('add').isDisabled(),true);
    stateFailure=false; await action('retry-state').click(); await page.waitForFunction(()=>!document.querySelector('[data-action="retry-state"]'));
    // Refresh is one explicit request, polling never invokes providers.
    const refreshBefore=calls.filter(c=>c.p==='/api/refresh').length;
    await action('refresh').click(); await has('Synthetic watcher refresh queued'); await tick();
    assert.equal(calls.filter(c=>c.p==='/api/refresh').length,refreshBefore+1);
    // Empty registry add, edit, and retirement are real preview/confirm requests.
    state.seats=[]; state.local_seats=[]; await tick(); await has('Add your first Codex seat');
    await action('add-empty').click(); await field('seat','label').fill('New synthetic account');
    assert.equal(await field('seat','profile').inputValue(),'unverified');
    await page.locator('#seat-save').click(); await action('confirm').waitFor();
    assert.deepEqual(op.changes,{label:'New synthetic account',profile:'unverified'});
    await action('confirm').click(); await done(); await action('dismiss-operation').click(); await has('Seat added inactive');
    await page.locator('details[id^="advanced-"] summary').click(); await action('edit').click();
    await field('seat','label').fill('Edited synthetic account'); await page.locator('#seat-save').click();
    await action('confirm').waitFor(); assert.equal(op.kind,'edit-seat');
    await action('cancel').click(); await action('dismiss-operation').waitFor(); await action('dismiss-operation').click(); await action('close-editor').click();
    state.seats[0].lifecycle='active'; await tick();
    const advanced = page.locator('details[id^="advanced-"]');
    if (!(await advanced.evaluate(el => el.open))) await advanced.locator('summary').click();
    await field('role','choice').selectOption('deputy');
    await action('set-role').click(); await action('confirm').waitFor();
    assert.equal(op.kind,'set-role'); assert.deepEqual(op.changes,{role:'deputy'});
    await action('confirm').click(); await done(); await action('dismiss-operation').click();
    assert.equal(state.seats[0].role,'deputy');
    if (!(await advanced.evaluate(el => el.open))) await advanced.locator('summary').click();
    await field('role','choice').selectOption('worker');
    await action('set-role').click(); await action('confirm').waitFor();
    await action('confirm').click(); await done(); await action('dismiss-operation').click();
    state.seats[0].lifecycle='inactive'; await tick();
    await action('remove-seat').click(); await action('confirm').waitFor(); assert.equal(op.kind,'remove-seat');
    await action('cancel').click(); await action('dismiss-operation').waitFor(); await action('dismiss-operation').click();
    // Responsive pages and contrast modes. Screenshots contain synthetic data only.
    state.seats=[{...seat,label:'Test account',lifecycle:'active',windows:[{...seat.windows[0],remaining_pct:62}],banked_resets:{...seat.banked_resets,available_count:1}}];
    await tick();
    for (const theme of ['light','dark']) {
      state.policy['ui.theme']=theme; state.policy['ui.accent']='teal'; state.policy['ui.density']='comfortable'; await tick();
      for (const width of [320,360,736,1024]) {
        await page.setViewportSize({width,height:900});
        for (const name of ['inventory','settings','claude']) {
          await page.locator(`#nav-${name}`).click();
          assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),`${name} overflows at ${width} ${theme}`);
          assert.ok(await page.locator('button:visible').evaluateAll(els=>els.every(el=>el.getBoundingClientRect().height>=44)), '44px button targets');
          if (process.env.UI_SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.UI_SCREENSHOT_DIR,`${name}-${theme}-${width}.png`),fullPage:true});
        }
      }
    }
    assert.deepEqual(errors,[]);
    assert.equal(await page.evaluate(()=>localStorage.length),0);
    await page.reload(); await page.locator('#seat-0').waitFor();
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('codex-seats-session')),'synthetic-session');
    await page.evaluate(()=>sessionStorage.clear()); await page.reload();
    await has('Reopen the launch link from your terminal');
    assert.equal(await page.evaluate(()=>sessionStorage.length),0);
    console.log(`PASS: synthetic browser interactions; ${calls.length} API requests; 24 responsive light/dark page checks`);
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""

CONTROLLER_TEST = r"""
// Exercise the complete app's event handlers and fetch flow in Node when the
// OS cannot launch a browser. This DOM adapter cannot prove layout/accessibility.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const nodes = new Map(), events = {}, documentEvents = {}, windowEvents = {}, timers = new Map();
  let timerId = 0;
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {id,innerHTML:'',textContent:'',dataset:{},style:{},
      querySelectorAll:()=>[],querySelector:()=>null,contains:()=>false,
      focus(){},setAttribute(){},removeAttribute(){},addEventListener(name,fn){events[name]=fn;}});
    return nodes.get(id);
  }
  const document = {hidden:false,activeElement:null,documentElement:{style:{}},getElementById:node,
    querySelector:()=>({content:'synthetic-session'}),addEventListener:(name,fn)=>{documentEvents[name]=fn;}};
  const calls=[];
  const seat={id:'seat-test',label:'<img src=x onerror="bad()">',lifecycle:'active',role:'worker',busy:false,health:'ok',freshness:'fresh',identity:{status:'fresh',email:'synthetic@example.test'},capacity:{},windows:[{name:'primary',window_minutes:90,remaining_pct:61,freshness:'fresh'}],banked_resets:{status:'fresh',available_count:2,credits:[{fingerprint:'credit-test',status:'available'}]}};
  const state={version:1,observed_at:100,seats:[seat],local_seats:[{id:'local-test',host:'synthetic-host',label:'Local test',status:'unknown'}],claude:{configured_mode:'off',mode:'off',headroom:{small_min_pct:10,big_min_pct:15},subscription_tier:{configured:null},active_usage_refresh:false,usage:{read:false}},policy:{'review.mode':'off','review.strategy':'single','execution.inline_max_minutes':5,'ui.theme':'system','ui.accent':'teal','ui.density':'comfortable'},warnings:[],refresh:{}};
  let op=null, sequence=0, failState=false, failConfirm=false, failOperation=false, unknownOutcome=false, holdPreparing=false;
  const response=(data,status=200)=>({ok:status<400,status,json:async()=>structuredClone(data)});
  const sandbox={document,window:{addEventListener:(name,fn)=>{windowEvents[name]=fn;}},AbortController,
    setTimeout:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),
    fetch:async (url,options)=>{
      assert.equal(options.headers['X-Codex-Seats-Session'],'synthetic-session');
      const body=options.body ? JSON.parse(options.body) : null;
      calls.push({url,body,method:options.method});
      if (url==='/api/state') return response(failState ? {error:'cache failed'} : state,failState ? 503 : 200);
      if (url==='/api/preview') {
        op={id:`op-${++sequence}`,kind:body.kind,seat:body.seat,status:'preparing',changes:body.changes,preview:{title:'Synthetic preview',account_email:seat.identity.email,seat:seat.id,credit:{fingerprint:'credit-test'},diff:['<script>bad()</script>'],warnings:[]}};
        return response(op);
      }
      if (url==='/api/confirm') { if(failConfirm)return response({error:'conflict'},409);op.status='running';return response(op); }
      if (url==='/api/cancel') {op.status='canceled';return response(op);}
      if (url==='/api/refresh') return response({message:'Synthetic refresh queued'});
      if (url.startsWith('/api/operations/')) {
        if(failOperation)return response({error:'status failed'},503);
        if(op.status==='preparing' && !holdPreparing)op.status='ready';
        else if(op.status==='running') {
          op.status=unknownOutcome?'error':'done';op.result={message:'Synthetic result',outcome:unknownOutcome?'unknown':'success'};
          if(!unknownOutcome && op.kind==='redeem-reset')seat.banked_resets.available_count=1;
          if(op.kind==='settings')Object.assign(state.policy,op.changes);
        }
        return response(op);
      }
      throw Error('Unexpected API '+url);
    }};
  vm.runInNewContext(fs.readFileSync(path.join(process.env.UI_ASSETS,'app.js'),'utf8'),sandbox);
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  async function tick(ms){for(const [id,t] of [...timers])if(t.ms===ms){timers.delete(id);await t.fn();}await flush();}
  async function click(data){const el={dataset:typeof data==='string'?{action:data}:data,type:'button',disabled:false,closest(){return this;}};await events.click({target:el});await flush();}
  async function input(scope,key,value,type='text'){events.input({target:{dataset:{scope,key},type,value,checked:value}});await flush();}
  async function submit(scope){events.submit({preventDefault(){},target:{id:`${scope}-form`,reportValidity:()=>true}});await flush();}
  const html=()=>node('content').innerHTML;
  const count=url=>calls.filter(c=>c.url===url).length;
  await flush();
  assert.ok(html().includes('90-minute window'));
  assert.ok(html().includes('&lt;img'));assert.ok(!html().includes('<img'));
  await click('redeem-reset');assert.equal(op.status,'preparing');
  await click({page:'settings'});assert.ok(node('operation').innerHTML.includes('preparing'));
  await click('cancel');assert.equal(op.status,'canceled');assert.equal(count('/api/confirm'),0);
  await click('dismiss-operation');await click({page:'inventory'});
  await click('redeem-reset');await tick(2000);assert.equal(op.status,'ready');
  assert.ok(node('operation').innerHTML.includes('&lt;script&gt;'));
  await Promise.all([click('confirm'),click('confirm')]);assert.equal(count('/api/confirm'),1);
  assert.ok(html().includes('cs-bankcount">2<'));await tick(2000);assert.ok(html().includes('cs-bankcount">1<'));
  await click('dismiss-operation');await click({page:'settings'});
  await input('settings','execution.inline_max_minutes','241','number');assert.equal(node('settings-save').disabled,true);
  await input('settings','execution.inline_max_minutes','12','number');await input('settings','ui.theme','dark');await input('settings','ui.accent','indigo');
  await tick(5000);assert.ok(html().includes('value="12"'));assert.equal(document.documentElement.style.colorScheme,'light dark');
  await submit('settings');await tick(2000);assert.equal(op.changes['execution.inline_max_minutes'],12);
  await click('confirm');await tick(2000);assert.equal(document.documentElement.style.colorScheme,'dark');assert.equal(node('codex-seats').dataset.accent,'indigo');
  await click('dismiss-operation');await input('settings','review.strategy','cross');await click('discard-settings');assert.ok(html().includes('value="single" selected'));
  await click({page:'claude'});await input('claude','small_task_min_headroom_pct','25','number');assert.equal(node('claude-save').disabled,true);
  await input('claude','big_task_min_headroom_pct','30','number');await input('claude','subscription_tier','');await input('claude','active_usage_refresh',true,'checkbox');
  await submit('claude');assert.equal(op.changes.active_usage_refresh,true);assert.equal(op.changes.small_task_min_headroom_pct,25);await tick(2000);await click('cancel');await click('dismiss-operation');
  await click({page:'inventory'});seat.busy=true;await tick(5000);const previews=count('/api/preview');await click('redeem-reset');assert.equal(count('/api/preview'),previews);
  seat.busy=false;seat.freshness='stale';seat.windows[0].remaining_pct=null;await tick(5000);await click('redeem-reset');assert.equal(count('/api/preview'),previews);assert.ok(html().includes('Unknown'));
  await input('role','choice','deputy');await click('set-role');assert.equal(op.kind,'set-role');assert.deepEqual(op.changes,{role:'deputy'});await tick(2000);await click('cancel');await click('dismiss-operation');
  await click({select:'local:local-test'});assert.ok(html().includes('synthetic-host'));assert.ok(!html().includes('data-action="redeem-reset"'));
  await click({select:'codex:seat-test'});seat.freshness='fresh';await tick(5000);
  failConfirm=true;await click('pause');await tick(2000);await click('confirm');assert.ok(node('operation').innerHTML.includes('HTTP 409'));
  const confirms=count('/api/confirm');await tick(5000);await click('confirm');assert.equal(count('/api/confirm'),confirms);
  await click('repreview');await tick(2000);failConfirm=false;unknownOutcome=true;await click('confirm');await tick(2000);assert.ok(node('operation').innerHTML.includes('Outcome: unknown'));assert.ok(!node('operation').innerHTML.includes('data-action="repreview"'));await click('dismiss-operation');unknownOutcome=false;
  failState=true;await tick(5000);assert.ok(html().includes('cache failed'));assert.ok(html().includes('&lt;img'));failState=false;await click('retry-state');
  await click('refresh');await tick(5000);assert.equal(count('/api/refresh'),1);
  await click('pause');failOperation=true;await tick(2000);assert.ok(node('operation').innerHTML.includes('Status unavailable'));const reads=count('/api/operations/'+op.id);await tick(2000);assert.equal(count('/api/operations/'+op.id),reads);failOperation=false;await click('check-operation');await click('cancel');await click('dismiss-operation');
  holdPreparing=true;await click('pause');const pendingId=op.id;const previewCount=count('/api/preview');
  for(let i=0;i<65;i++)await tick(2000);
  assert.equal(count('/api/operations/'+pendingId),60);assert.equal(count('/api/preview'),previewCount);assert.ok(node('operation').innerHTML.includes('Automatic status checks paused'));
  holdPreparing=false;await click('check-operation');await click('cancel');await click('dismiss-operation');
  seat.lifecycle='inactive';await tick(5000);assert.ok(html().includes('Reactivate as primary'));
  await click('reactivate-primary');assert.deepEqual(op.changes,{as_primary:true});await tick(2000);await click('cancel');await click('dismiss-operation');
  state.seats=[];state.local_seats=[];await tick(5000);assert.ok(html().includes('Add your first Codex seat'));
  await click('add-empty');await input('seat','label','New synthetic account');await submit('seat');assert.deepEqual(op.changes,{label:'New synthetic account',profile:'unverified'});await tick(2000);await click('cancel');await click('dismiss-operation');
  document.hidden=true;documentEvents.visibilitychange();const stateReads=count('/api/state');await tick(5000);assert.equal(count('/api/state'),stateReads);
  document.hidden=false;documentEvents.visibilitychange();await flush();assert.ok(count('/api/state')>stateReads);
  windowEvents.pagehide();assert.equal(timers.size,0);
  console.log('PASS: synthetic controller interaction and request-safety tests (DOM adapter; not browser/layout evidence)');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


class UIAssetsTest(unittest.TestCase):
    @unittest.skipUnless(NODE, "Node is not installed")
    def test_controller_interactions_without_browser(self):
        result = subprocess.run([NODE, "-e", CONTROLLER_TEST],
                                env={**os.environ, "UI_ASSETS": str(ASSETS)},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())

    @unittest.skipUnless(NODE, "Node is not installed")
    def test_javascript_syntax(self):
        subprocess.run([NODE, "--check", str(ASSETS / "app.js")], check=True)

    @unittest.skipUnless(NODE, "Node is not installed")
    def test_synthetic_browser_interactions(self):
        env = {**os.environ, "UI_ASSETS": str(ASSETS)}
        result = subprocess.run([NODE, "-e", BROWSER_TEST], env=env, text=True,
                                capture_output=True, timeout=180)
        if result.returncode == 77:
            self.skipTest(result.stdout.strip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
