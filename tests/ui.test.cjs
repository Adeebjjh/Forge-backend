'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const web = path.join(__dirname,'../web');
const wait = async fn => { for(let i=0;i<300;i++) {if(fn()) return;await new Promise(r=>setTimeout(r,5));} throw new Error('UI condition timed out'); };
function app({native=true, profile=null, vault={}, legacy=null, keyBackup=null, sharedUrl='', startUnpaired=false}={}) {
  const dom = new JSDOM(fs.readFileSync(path.join(web,'index.html'),'utf8'),{url:'https://forge.local',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window, d=w.document, requests=[];
  if (!w.TextDecoder) w.TextDecoder = require('util').TextDecoder;
  if (!w.TextEncoder) w.TextEncoder = require('util').TextEncoder;
  w.Element.prototype.scrollIntoView=()=>{};
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;};
  w.confirm=()=>true;
  w.FORGE_SHARED_URL=sharedUrl;
  if (profile) w.localStorage.setItem('forge.profile.v1',JSON.stringify(profile));
  if (legacy) { w.localStorage.setItem('forge.profiles.v2',JSON.stringify(legacy.profiles)); w.localStorage.setItem('forge.selected.v2',legacy.selected); }
  let keys=JSON.stringify(vault), delayed=null, runPrompt='', lastRun=null, githubLogin='', paired=!startUnpaired;
  const provisionCalls=[];
  const answer=(id,code,payload)=>w.setTimeout(()=>w.onNativeResponse(id,code,typeof payload==='string'?payload:JSON.stringify(payload)),1);
  const bridge={
    loadProviderVault:()=>keys,saveProviderVault:value=>{keys=value;return true;},configure:()=>{},
    deviceStatus:id=>answer(id,200,{running:true,url:'http://127.0.0.1:8787',workspace:'/w',paired:true}),
    deviceAction:id=>answer(id,200,{running:true,url:'http://127.0.0.1:8787',workspace:'/w',paired:true}),
    provisionShared(id,value){
      const v=JSON.parse(value);provisionCalls.push(v);
      paired=true;
      answer(id,200,{ok:true});
      w.setTimeout(()=>w.onNativeConfigured(),1);
    },
    request(id,url,method,body){
      const p=body?JSON.parse(body):{};requests.push({url,method,p});
      if(!paired)return answer(id,400,{error:'Open connection settings to pair your runner.'});
      if(url==='/api/health')return answer(id,200,{workspace:'test',version:'0.5.0',cliEnabled:false,shared:!!sharedUrl,tools:['list_files','read_file','run_python']});
      if(url==='/api/providers/discover') {
        const reply=()=>answer(id,200,{models:[{id:'model-a',name:'Model A'},{id:'model-b',name:'Model B'}],baseUrl:p.baseUrl,protocol:p.protocol});
        if(delayed)delayed(reply);else reply();return;
      }
      if(url==='/api/providers/test')return answer(id,p.apiKey==='test-key'?200:400,p.apiKey==='test-key'?{ok:true}:{error:'Provider rejected authentication (HTTP 401).'});
      if(url==='/api/runs'&&method==='POST'){runPrompt=p.prompt;lastRun=p;return answer(id,201,{id:'test-run'});}
      if(url==='/api/runs/test-run')return answer(id,200,{status:'done',events:[{type:'user',text:runPrompt},{type:'assistant',text:'Answer to '+runPrompt}],pending:[]});
      if(url==='/api/repos/fetch')return answer(id,200,{message:'Fetched owner/repo@main into repo (3 files).'});
      if(url==='/api/github'&&method==='GET')return answer(id,200,{connected:!!githubLogin,login:githubLogin});
      if(url==='/api/github'&&method==='POST'){githubLogin=p.token&&p.token.length>=20?'octocat':'';return answer(id,githubLogin?200:400,githubLogin?{login:'octocat'}:{error:'bad token'});}
      if(url==='/api/github/disconnect'){githubLogin='';return answer(id,200,{ok:true});}
      if(url==='/api/skills'&&method==='GET')return answer(id,200,{skills:[{name:'Code Review',description:'Reviews code.'},{name:'Write Tests',description:'Writes tests.'}]});
      if(url==='/api/provider-keys'&&method==='GET')return answer(id,200,{providers:keyBackup?[keyBackup]:[]});
      if(url==='/api/provider-keys'&&method==='POST')return answer(id,200,{ok:true});
      answer(id,200,{files:[],runs:[]});
    }
  };
  if(native)w.ForgeNative=bridge;
  // Minimal fetch mock for browser-mode tests (shared backend served same-origin).
  let browserDeviceToken='';
  w.fetch=async(url,opts={})=>{
    const method=opts.method||'GET';
    const body=opts.body?JSON.parse(opts.body):{};
    const auth=(opts.headers&&opts.headers.Authorization||'').replace('Bearer ','');
    requests.push({url,method,p:body,via:'fetch'});
    const respond=(status,payload)=>({ok:status<400,status,json:async()=>payload,text:async()=>JSON.stringify(payload)});
    if(url==='/api/provision'&&method==='POST'){
      if(!/^[A-Za-z0-9_-]{8,64}$/.test(body.device||''))return respond(400,{error:'bad device'});
      provisionCalls.push(body);browserDeviceToken='browser-device-token-1234567890';
      return respond(200,{token:browserDeviceToken});
    }
    if(url==='/api/health'){
      if(!auth||auth!==browserDeviceToken)return respond(401,{error:'unauthorized'});
      return respond(200,{workspace:'test',version:'0.5.0',cliEnabled:false,shared:!!sharedUrl,tools:['read_file']});
    }
    if(url==='/api/provider-keys'&&method==='POST')return respond(200,{ok:true});
    if(url==='/api/runs'&&method==='POST'){lastRun=body;return respond(201,{id:'test-run'});}
    if(url==='/api/runs/test-run')return respond(200,{status:'done',events:[{type:'user',text:'x'},{type:'assistant',text:'y'}],pending:[]});
    return respond(404,{error:'not mocked'});
  };
  const context=dom.getInternalVMContext();
  for(const script of ['app.js','cloud.js'])vm.runInContext(fs.readFileSync(path.join(web,script),'utf8'),context,{filename:script});
  const input=(id,value)=>{const el=d.getElementById(id);el.value=value;el.dispatchEvent(new w.Event('input',{bubbles:true}));};
  const change=(id,value)=>{const el=d.getElementById(id);el.value=value;el.dispatchEvent(new w.Event('change',{bubbles:true}));};
  return {dom,w,d,requests,input,change,keys:()=>JSON.parse(keys),eval:code=>vm.runInContext(code,context),delay:fn=>delayed=fn,lastRun:()=>lastRun,provisionCalls};
}
async function setup(t,options) {
  const a=app(options);t.after(()=>a.dom.window.close());
  if(options?.native!==false)await wait(()=>a.d.querySelector('#connection').classList.contains('connected'));
  return a;
}
const savedProfile=()=>({name:'Saved gateway',kind:'custom',protocol:'custom',baseUrl:'https://example.com/v1',authMode:'bearer',models:[{id:'m',name:'m'}],model:'m'});
async function openSetup(a) { a.eval('openSetup()'); await wait(()=>a.d.querySelector('#setup-dialog').open); }

test('setup dialog discovers models, saves without the key in storage, and fills the chat model picker',async t=>{
  const a=await setup(t);
  assert.equal(a.d.querySelector('#chat-model').value,'');
  await openSetup(a);
  a.change('setup-preset','custom');
  a.input('setup-name','Test gateway');a.input('setup-url','https://example.com/gateway/v2');a.input('setup-key','  Bearer test-key\n');
  a.d.querySelector('#setup-discover').click();
  await wait(()=>!a.d.querySelector('#setup-discover').disabled);
  assert.equal(a.d.querySelector('#setup-model').value,'model-a');
  assert.match(a.d.querySelector('#setup-endpoint').textContent,/gateway\/v2\/chat\/completions/);
  a.d.querySelector('#setup-save').click();
  assert.equal(a.d.querySelector('#chat-model').value,'model-a');
  const stored=a.w.localStorage.getItem('forge.profile.v1');
  assert.ok(stored.includes('model-a'));assert.ok(!stored.includes('test-key'));
  assert.equal(a.keys().default,'test-key');
});
test('chat model picker offers Model settings which re-opens the setup dialog',async t=>{
  const a=await setup(t);
  await openSetup(a);
  a.change('setup-preset','custom');
  a.input('setup-name','Test gateway');a.input('setup-url','https://example.com/v1');a.input('setup-key','test-key');
  a.d.querySelector('#setup-discover').click();
  await wait(()=>!a.d.querySelector('#setup-discover').disabled);
  a.d.querySelector('#setup-save').click();
  const sel=a.d.querySelector('#chat-model');
  const opt=[...sel.options].find(o=>o.value==='__settings');
  assert.ok(opt,'settings option present');
  a.change('chat-model','__settings');
  await wait(()=>a.d.querySelector('#setup-dialog').open);
  assert.equal(a.d.querySelector('#setup-name').value,'Test gateway');
});
test('setup dialog saves the permission mode on the profile',async t=>{
  const a=await setup(t);
  await openSetup(a);
  assert.equal(a.d.querySelector('#setup-perms').value,'ask');
  a.change('setup-preset','custom');
  a.input('setup-name','Test gateway');a.input('setup-url','https://example.com/v1');a.input('setup-key','test-key');
  a.change('setup-perms','auto');
  a.d.querySelector('#setup-discover').click();
  await wait(()=>!a.d.querySelector('#setup-discover').disabled);
  a.d.querySelector('#setup-save').click();
  const stored=JSON.parse(a.w.localStorage.getItem('forge.profile.v1'));
  assert.equal(stored.autoApprove,true);
  await openSetup(a);
  assert.equal(a.d.querySelector('#setup-perms').value,'auto');
});
test('settings page shows the provider and the permission toggle works',async t=>{
  const a=await setup(t);
  await openSetup(a);
  a.change('setup-preset','custom');
  a.input('setup-name','Test gateway');a.input('setup-url','https://example.com/v1');a.input('setup-key','test-key');
  a.d.querySelector('#setup-discover').click();
  await wait(()=>!a.d.querySelector('#setup-discover').disabled);
  a.d.querySelector('#setup-save').click();
  a.eval(`show('settings')`);
  assert.ok(a.d.querySelector('#settings').classList.contains('active'));
  assert.equal(a.d.querySelector('#settings-provider-name').textContent,'Test gateway');
  assert.match(a.d.querySelector('#settings-provider-detail').textContent,/example\.com/);
  assert.equal(a.d.querySelector('#perm-ask').className,'primary');
  a.d.querySelector('#perm-auto').click();
  assert.equal(a.d.querySelector('#perm-auto').className,'primary');
  assert.equal(a.d.querySelector('#perm-ask').className,'secondary');
  const stored=JSON.parse(a.w.localStorage.getItem('forge.profile.v1'));
  assert.equal(stored.autoApprove,true);
});
test('markdown renders code blocks, formatting and lists safely',async t=>{
  const a=await setup(t);
  const html=a.eval(`renderMarkdown('# Title\\n\\nHello **bold** and *italic* with \`code\`.\\n\\n- one\\n- two\\n\\n[link](https://example.com)\\n\\n\`\`\`python\\nprint(1)\\n\`\`\`\\n\\n<script>alert(1)</script>')`);
  assert.match(html,/<h1>Title<\/h1>/);
  assert.match(html,/<strong>bold<\/strong>/);
  assert.match(html,/<em>italic<\/em>/);
  assert.match(html,/<code>code<\/code>/);
  assert.match(html,/<ul>.*<li>one<\/li>.*<li>two<\/li>.*<\/ul>/s);
  assert.match(html,/<a href="https:\/\/example\.com"/);
  assert.match(html,/class="codeblock"/);
  assert.match(html,/print\(1\)/);
  assert.ok(!html.includes('<script>'),'script tag escaped');
});
test('assistant messages render as markdown in the feed',async t=>{
  const a=await setup(t);
  a.eval(`renderEvent({type:'assistant',text:'Try:\\n\\n\`\`\`js\\nconst x = 1;\\n\`\`\`'})`);
  const body=a.d.querySelector('#feed .message.assistant .message-text');
  assert.ok(body.querySelector('.codeblock'),'code block rendered');
  assert.ok(body.querySelector('.code-copy'),'copy button rendered');
});
test('index.html has balanced tags and unique ids',async t=>{
  const src=fs.readFileSync(path.join(web,'index.html'),'utf8');
  // lightweight tag-balance check without extra deps
  const stack=[];const errors=[];
  const re=/<\/?([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*>/g;let m;
  const voidEls=new Set(['br','img','input','meta','link','hr']);
  while((m=re.exec(src))){
    const tag=m[1].toLowerCase(),closing=m[0][1]==='/',selfClose=/\/>$/.test(m[0]);
    if(voidEls.has(tag)||selfClose)continue;
    if(closing){if(stack.length&&stack[stack.length-1]===tag)stack.pop();else errors.push('mismatch </'+tag+'>');}
    else stack.push(tag);
  }
  assert.deepEqual(errors,[]);
  assert.deepEqual(stack,[]);
  const ids=[...src.matchAll(/id="([^"]+)"/g)].map(x=>x[1]);
  const dup=ids.filter((id,i)=>ids.indexOf(id)!==i);
  assert.deepEqual(dup,[],'duplicate ids: '+dup.join(','));
  for(const page of ['chat','files','cloud','settings'])
    assert.ok(src.includes('data-page="'+page+'"'),page+' nav button present');
});
test('chat test reports a 401 until the key is corrected',async t=>{
  const a=await setup(t);await openSetup(a);
  a.change('setup-preset','custom');
  a.input('setup-name','Test gateway');a.input('setup-url','https://example.com/v1');a.input('setup-key','wrong-key');
  a.d.querySelector('#setup-discover').click();
  await wait(()=>!a.d.querySelector('#setup-discover').disabled);
  a.d.querySelector('#setup-test').click();await wait(()=>!a.d.querySelector('#setup-test').disabled);
  assert.equal(a.d.querySelector('#setup-notice').dataset.tone,'error');
  assert.match(a.d.querySelector('#setup-notice').textContent,/HTTP 401/);
  a.input('setup-key','test-key');
  a.d.querySelector('#setup-test').click();await wait(()=>!a.d.querySelector('#setup-test').disabled);
  assert.equal(a.d.querySelector('#setup-notice').dataset.tone,'success');
  assert.equal(a.requests.filter(r=>r.url.endsWith('/test')).at(-1).p.apiKey,'test-key');
});
test('stale discovery cannot overwrite a changed setup form',async t=>{
  const a=await setup(t);await openSetup(a);let release;
  a.delay(reply=>release=reply);
  a.change('setup-preset','custom');a.input('setup-url','https://example.com');a.input('setup-key','test-key');
  a.d.querySelector('#setup-discover').click();await wait(()=>release);
  a.change('setup-preset','ollama');release();await wait(()=>!a.d.querySelector('#setup-discover').disabled);
  assert.equal(a.d.querySelector('#setup-name').value,'Ollama');
  assert.equal(a.d.querySelector('#setup-url').value,'http://127.0.0.1:11434');
  assert.equal(a.w.localStorage.getItem('forge.profile.v1'),null);
});
test('chats stay separate: new chat is blank, switching restores history',async t=>{
  const a=await setup(t,{profile:savedProfile(),vault:{default:'test-key'}});
  a.input('prompt','first task');a.d.querySelector('#send').click();
  await wait(()=>a.d.querySelector('#run-status').textContent==='Done'&&!a.d.querySelector('#send').disabled);
  assert.match(a.d.querySelector('#feed').textContent,/first task/);
  a.d.querySelector('#menu').click();a.d.querySelector('#drawer-new').click();
  assert.ok(a.d.querySelector('#welcome'));
  assert.equal(a.d.querySelectorAll('.chat-row').length,2);
  a.input('prompt','second chat task');a.d.querySelector('#send').click();
  await wait(()=>a.d.querySelector('#run-status').textContent==='Done'&&!a.d.querySelector('#send').disabled);
  const rows=a.d.querySelectorAll('.chat-row .chat-title');
  rows[1].click();
  assert.match(a.d.querySelector('#feed').textContent,/first task/);
  assert.doesNotMatch(a.d.querySelector('#feed').textContent,/second chat task/);
  rows[0].click();
  assert.match(a.d.querySelector('#feed').textContent,/second chat task/);
  rows[0].parentElement.querySelector('.chat-del').click();
  assert.equal(a.d.querySelectorAll('.chat-row').length,1);
});
test('follow-up turns keep earlier messages as history',async t=>{
  const a=await setup(t,{profile:savedProfile(),vault:{default:'test-key'}});
  for(const prompt of ['first task','second task']) {
    a.input('prompt',prompt);a.d.querySelector('#send').click();
    await wait(()=>a.d.querySelector('#run-status').textContent==='Done'&&!a.d.querySelector('#send').disabled);
  }
  assert.match(a.d.querySelector('#feed').textContent,/first task/);
  assert.equal(a.d.querySelector('#feed').textContent.includes('second task'),true);
  assert.equal(a.requests.filter(r=>r.url==='/api/runs'&&r.method==='POST').at(-1).p.history.length,2);
  a.d.querySelector('#drawer-new').click();
  a.d.querySelector('[data-prompt]').click();assert.match(a.d.querySelector('#prompt').value,/Look at this project/);
});
test('attachments: 5 MB cap, text vs binary detection',async t=>{
  const a=await setup(t);
  const py=a.eval("classifyAttachment('a.py',4,new Uint8Array([104,105]))");
  assert.equal(py.kind,'text');assert.equal(py.name,'a.py');
  const nul=a.eval("classifyAttachment('data.bin',4,new Uint8Array([1,0,2,3]))");
  assert.equal(nul.kind,'binary');
  const zip=a.eval("classifyAttachment('a.zip',4,new Uint8Array([80,75,3,4]))");
  assert.equal(zip.kind,'binary');
  assert.throws(()=>a.eval("classifyAttachment('big.bin',6*1024*1024,new Uint8Array(4))"),/5 MB/);
  assert.equal(a.eval("formatSize(1536)"),'1.5 KB');
});
test('runner 401 and HTML responses have actionable messages',async t=>{
  const a=await setup(t);
  assert.throws(()=>a.eval("responseData('<html>login</html>',401,'Runner')"),/Runner pairing expired/);
  assert.throws(()=>a.eval("responseData('<html>login</html>',200,'Provider')"),/website or login page/);
  assert.throws(()=>a.eval("responseData('',200,'Provider')"),/empty or invalid JSON/);
  assert.equal(a.eval("responseData('\\uFEFF{\"ok\":true}',200).ok"),true);
});
test('old multi-profile settings migrate to the single model setup',async t=>{
  const legacy={profiles:{saved:{kind:'custom',name:'Saved gateway',baseUrl:'https://example.com/v1',authMode:'bearer',model:'m',models:[{id:'m'}]}},selected:'saved'};
  const a=app({profile:null,vault:{saved:'test-key'},legacy});t.after(()=>a.dom.window.close());
  await wait(()=>a.d.querySelector('#chat-model').value==='m');
  assert.equal(a.eval("profile.name"),'Saved gateway');
});
test('cloud page shows the on-device runner and the Railway card',async t=>{
  const a=await setup(t);
  a.d.querySelector('nav button[data-page="cloud"]').click();
  await wait(()=>a.d.querySelector('#device-badge').textContent==='RUNNING');
  assert.ok(a.d.querySelector('#railway-connect'));
  assert.match(a.d.querySelector('#cloud').textContent,/FORGE_TOKEN/);
});
test('gemini preset previews its endpoint in the setup dialog',async t=>{
  const a=await setup(t);await openSetup(a);
  a.change('setup-preset','gemini');
  assert.equal(a.d.querySelector('#setup-kind').value,'gemini');
  assert.match(a.d.querySelector('#setup-url').value,/generativelanguage/);
  assert.match(a.d.querySelector('#setup-endpoint').textContent,/:generateContent/);
});
test('GitHub fetch form posts repo, ref, and destination',async t=>{
  const a=await setup(t);
  a.d.querySelector('nav button[data-page="files"]').click();
  a.input('fetch-repo','owner/repo');a.input('fetch-ref','dev');a.input('fetch-dest','libs/repo');
  a.d.querySelector('#fetch-button').click();
  await wait(()=>a.requests.some(r=>r.url==='/api/repos/fetch'));
  const sent=a.requests.filter(r=>r.url==='/api/repos/fetch').at(-1).p;
  assert.equal(sent.repo,'owner/repo');assert.equal(sent.ref,'dev');assert.equal(sent.dest,'libs/repo');
  await wait(()=>a.d.querySelector('#toast').textContent.includes('Fetched owner/repo'));
});
test('github card connects, persists the token, and disconnects',async t=>{
  const a=await setup(t);
  a.d.querySelector('nav button[data-page="cloud"]').click();
  await wait(()=>a.d.querySelector('#github-badge').textContent==='NOT CONNECTED');
  a.input('github-token','ghp_'+'x'.repeat(36));
  a.d.querySelector('#github-connect').click();
  await wait(()=>a.d.querySelector('#github-badge').textContent==='CONNECTED');
  assert.match(a.d.querySelector('#github-status').textContent,/octocat/);
  assert.equal(a.keys()['github-token'],'ghp_'+'x'.repeat(36));
  a.d.querySelector('#github-disconnect').click();
  await wait(()=>a.d.querySelector('#github-badge').textContent==='NOT CONNECTED');
});
test('skills picker loads skills and sends the selection with the run',async t=>{
  const a=await setup(t,{profile:savedProfile(),vault:{default:'test-key'}});
  a.d.querySelector('#skills-btn').click();
  await wait(()=>a.d.querySelectorAll('#skills-list .skill-pick').length===2);
  a.d.querySelector('#skills-list .skill-pick input').click();
  a.d.querySelector('#skills-done').click();
  a.input('prompt','do the thing');a.d.querySelector('#send').click();
  await wait(()=>a.d.querySelector('#run-status').textContent==='Done'&&!a.d.querySelector('#send').disabled);
  assert.deepEqual(JSON.parse(JSON.stringify(a.lastRun().skills)),['Code Review']);
});
test('saving a model backs the key up to the runner api.json',async t=>{
  const a=await setup(t);
  await openSetup(a);
  a.change('setup-preset','custom');
  a.input('setup-name','Test gateway');a.input('setup-url','https://example.com/v1');a.input('setup-key','test-key');
  a.d.querySelector('#setup-discover').click();
  await wait(()=>!a.d.querySelector('#setup-discover').disabled);
  a.d.querySelector('#setup-save').click();
  await wait(()=>a.requests.some(r=>r.url==='/api/provider-keys'&&r.method==='POST'));
  const sent=a.requests.filter(r=>r.url==='/api/provider-keys'&&r.method==='POST').at(-1).p;
  assert.equal(sent.key,'test-key');
  assert.equal(sent.url,'https://example.com/v1');
});
test('missing key is restored from the runner backup on connect',async t=>{
  const backup={name:'Saved gateway',url:'https://example.com/v1',key:'restored-key'};
  const a=await setup(t,{profile:savedProfile(),vault:{},keyBackup:backup});
  await wait(()=>a.eval('profileKey')==='restored-key');
  assert.match(a.d.querySelector('#toast').textContent,/restored/);
});
test('shared backend provisions the device silently on first launch',async t=>{
  const a=await setup(t,{sharedUrl:'https://shared.example',startUnpaired:true});
  await wait(()=>a.provisionCalls.length===1);
  assert.equal(a.provisionCalls[0].url,'https://shared.example');
  assert.equal(a.provisionCalls[0].device,a.w.localStorage.getItem('forge.device.id'));
  assert.equal(a.eval('sharedActive'),true);
  assert.equal(a.d.querySelector('#device-card').hidden,true);
  assert.equal(a.d.querySelector('#railway-card').hidden,true);
});
test('shared mode keeps the provider key only on the backend',async t=>{
  const a=await setup(t,{sharedUrl:'https://shared.example',startUnpaired:true,profile:savedProfile(),vault:{}});
  await openSetup(a);
  a.change('setup-preset','custom');
  a.input('setup-name','Test gateway');a.input('setup-url','https://example.com/v1');a.input('setup-key','test-key');
  a.d.querySelector('#setup-discover').click();
  await wait(()=>!a.d.querySelector('#setup-discover').disabled);
  a.d.querySelector('#setup-save').click();
  await wait(()=>!a.d.querySelector('#setup-dialog').open&&a.keys().default==='');
  assert.equal(a.eval('profileKey'),'');
  const pushed=a.requests.filter(r=>r.url==='/api/provider-keys'&&r.method==='POST').at(-1);
  assert.equal(pushed.p.key,'test-key');
  a.input('prompt','do work');a.d.querySelector('#send').click();
  await wait(()=>a.d.querySelector('#run-status').textContent==='Done'&&!a.d.querySelector('#send').disabled);
  assert.ok(!('apiKey' in a.lastRun().profile));
});
test('browser on the shared backend provisions over fetch',async t=>{
  const a=app({native:false,sharedUrl:'https://forge.local'});t.after(()=>a.dom.window.close());
  await wait(()=>a.eval('connected')===true);
  assert.equal(a.provisionCalls.length,1);
  assert.equal(a.eval('browserToken'),'browser-device-token-1234567890');
  assert.equal(a.eval('sharedActive'),true);
});
