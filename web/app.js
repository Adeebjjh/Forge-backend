'use strict';
const $ = id => document.getElementById(id);

/* ---------------- state ---------------- */
let profile = null;            // {name,kind,protocol,baseUrl,authMode,customHeader,models,model,validation}
let profileKey = '';           // API key: memory only in browser, encrypted vault on Android
let githubToken = '';          // GitHub PAT: runner memory, encrypted vault on Android
let browserToken = '', connected = false;

/* ---------------- shared backend (one Railway for every install) ---------------- */
// The owner bakes their Railway URL into web/shared.js before building. Every install
// then silently provisions its own device token + isolated workspace on first launch.
// Provider API keys are stored ONLY on the backend (api.json) — never on the device.
const SHARED_URL = (window.FORGE_SHARED_URL || '').trim();
let sharedActive = false;       // the connected runner reported shared:true
let provisionAttempted = false; // one silent provisioning attempt per launch
function deviceId() {
  try {
    let id = localStorage.getItem('forge.device.id');
    if (!id) {
      id = (crypto.randomUUID ? crypto.randomUUID() : 'd' + Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/[^A-Za-z0-9]/g, '');
      if (id.length < 8) id = 'd' + Date.now().toString(36) + Math.random().toString(36).slice(2, 14);
      localStorage.setItem('forge.device.id', id);
    }
    return id;
  } catch (_) { return 'd' + Date.now().toString(36); }
}
async function provisionShared() {
  if (provisionAttempted || !SHARED_URL) return;
  provisionAttempted = true;
  const device = deviceId();
  try {
    if (native) {
      // Java POSTs /api/provision (WebView CSP blocks JS network) and fires onNativeConfigured.
      await nativeCall('provisionShared', JSON.stringify({url: SHARED_URL, device}));
    } else {
      const r = await fetch('/api/provision', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({device})});
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || 'Provisioning failed.');
      browserToken = body.token || '';
      try { localStorage.setItem('forge.device.token', browserToken); } catch (_) {}
      await connect();
    }
  } catch (e) { toast('Cloud backend unavailable: ' + e.message); }
}
function sharedSameOrigin() {
  if (!SHARED_URL) return false;
  try { return new URL(SHARED_URL).origin === location.origin; } catch (_) { return false; }
}
let activeRun = null;          // {id, chatId, rendered, history}
let chats = [], currentChatId = null;
let pendingAttachments = [];  // [{name, size, kind:'text'|'binary', b64}]
let pollTimer = null, toastTimer = null, pendingSignature = '';
let setupModels = [], setupBusy = false, setupRevision = 0;
const native = typeof window.ForgeNative !== 'undefined';
const nativePending = new Map();
let nativeSequence = 0;

const protocols = {anthropic:'anthropic', responses:'responses', custom:'custom', ollama:'ollama', gemini:'gemini', azure:'azure'};
const presets = {
  custom:{name:'Custom provider', kind:'custom', baseUrl:'', authMode:'bearer'},
  openai:{name:'OpenAI', kind:'responses', baseUrl:'https://api.openai.com/v1', authMode:'bearer'},
  anthropic:{name:'Claude', kind:'anthropic', baseUrl:'https://api.anthropic.com/v1', authMode:'api-key'},
  gemini:{name:'Gemini', kind:'gemini', baseUrl:'https://generativelanguage.googleapis.com', authMode:'x-goog-api-key'},
  azure:{name:'Azure OpenAI', kind:'azure', baseUrl:'https://YOUR-RESOURCE.openai.azure.com', authMode:'api-key-header'},
  ollama:{name:'Ollama', kind:'ollama', baseUrl:'http://127.0.0.1:11434', authMode:'none'},
  deepseek:{name:'DeepSeek', kind:'custom', baseUrl:'https://api.deepseek.com/v1', authMode:'bearer'},
  groq:{name:'Groq', kind:'custom', baseUrl:'https://api.groq.com/openai/v1', authMode:'bearer'},
  together:{name:'Together', kind:'custom', baseUrl:'https://api.together.xyz/v1', authMode:'bearer'},
  xai:{name:'xAI', kind:'custom', baseUrl:'https://api.x.ai/v1', authMode:'bearer'},
  openrouter:{name:'OpenRouter', kind:'custom', baseUrl:'https://openrouter.ai/api/v1', authMode:'bearer'},
  mistral:{name:'Mistral', kind:'custom', baseUrl:'https://api.mistral.ai/v1', authMode:'bearer'},
  cohere:{name:'Cohere', kind:'custom', baseUrl:'https://api.cohere.com/compatibility/v1', authMode:'bearer'}
};

/* ---------------- tiny helpers ---------------- */
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5500); }
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

/* ---------------- bridge ---------------- */
function responseData(payload, code, source = 'Runner') {
  let data;
  try { data = JSON.parse(payload.replace(/^\uFEFF/,'')); } catch (_) {
    const reason = code === 401 && source === 'Runner' ? 'Runner pairing expired (HTTP 401). Reconnect in Cloud.' :
      source + (code >= 400 ? ' HTTP ' + code + '. ' : ': ') + (/^\s*</.test(payload) ? 'Received a website or login page instead of JSON. Check the API URL.' : 'Received an empty or invalid JSON response. Check the endpoint.');
    const error = new Error(reason); error.status = code; throw error;
  }
  if (code >= 400 || code === 0) {
    const error = new Error(code === 401 && source === 'Runner' ? 'Runner pairing expired or its token is incorrect (HTTP 401). Reconnect in Cloud. Your model API key is separate.' :
      data && typeof data.error === 'string' ? data.error : source + ' HTTP ' + code + '. Check the connection settings.');
    error.status = code; throw error;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error(source + ' returned an unexpected response format.');
  return data;
}
window.onNativeResponse = (id, code, payload) => {
  const p = nativePending.get(id); if (!p) return; nativePending.delete(id); clearTimeout(p.timeout);
  try { p.resolve(responseData(payload, code, p.source)); } catch (e) { p.reject(e); }
};
window.onNativeConfigured = () => { connect(); };
async function api(path, method = 'GET', data) {
  if (native) return new Promise((resolve, reject) => {
    const id = String(++nativeSequence);
    const timeout = setTimeout(() => { nativePending.delete(id); reject(new Error('Runner request timed out.')); }, 75000);
    nativePending.set(id, {resolve, reject, timeout, source:'Runner'});
    try { window.ForgeNative.request(id, path, method, data === undefined ? '' : JSON.stringify(data)); }
    catch (e) { clearTimeout(timeout); nativePending.delete(id); reject(new Error('Android connection bridge is unavailable. Restart the app.')); }
  });
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 75000);
  try {
    const r = await fetch(path, {method, headers:{'Authorization':'Bearer ' + browserToken, 'Content-Type':'application/json'},
      body: data === undefined ? undefined : JSON.stringify(data), signal: controller.signal});
    return responseData(await r.text(), r.status);
  } finally { clearTimeout(timer); }
}
function nativeCall(method, value) {
  return new Promise((resolve, reject) => {
    const id = String(++nativeSequence);
    const timeout = setTimeout(() => { nativePending.delete(id); reject(new Error('Request timed out.')); }, 75000);
    nativePending.set(id, {resolve, reject, timeout, source:'Provider'});
    try { window.ForgeNative[method](id, value); }
    catch (e) { clearTimeout(timeout); nativePending.delete(id); reject(new Error('This action needs the updated app. Reinstall and try again.')); }
  });
}

/* ---------------- profile (one provider, kept simple) ---------------- */
function persistProfile() {
  const meta = {...(profile || {})}; delete meta.apiKey;
  try {
    if (native && !window.ForgeNative.saveProviderVault(JSON.stringify({default: profileKey || '', 'github-token': githubToken || ''}))) throw new Error('vault');
    localStorage.setItem('forge.profile.v1', JSON.stringify(meta));
  } catch (_) { throw new Error('Could not save settings on this device.'); }
}
function loadProfile() {
  try {
    const raw = localStorage.getItem('forge.profile.v1');
    if (raw) { const p = JSON.parse(raw); if (p && protocols[p.kind]) profile = {...p, models: Array.isArray(p.models) ? p.models : []}; }
    if (!profile) {
      // Migrate the previously selected provider from the old multi-profile store.
      const old = JSON.parse(localStorage.getItem('forge.profiles.v2') || '{}');
      const sel = localStorage.getItem('forge.selected.v2');
      const p = old[sel] || Object.values(old)[0];
      if (p && protocols[p.kind]) profile = {name: p.name || 'Provider', kind: p.kind, protocol: protocols[p.kind], baseUrl: p.baseUrl || '', authMode: p.authMode || 'auto', customHeader: p.customHeader || '', models: Array.isArray(p.models) ? p.models : [], model: p.model || '', validation: p.validation || null};
    }
  } catch (_) { profile = null; }
  if (native) {
    try {
      const vault = JSON.parse(window.ForgeNative.loadProviderVault() || '{}');
      if (typeof vault.default === 'string') profileKey = vault.default;
      else { const first = Object.values(vault).find(v => typeof v === 'string' && v); if (first) profileKey = first; }
      if (typeof vault['github-token'] === 'string' && vault['github-token']) githubToken = vault['github-token'];
    } catch (_) { /* key must be re-entered */ }
  }
  if (profile && !profileKey && !native) profileKey = '';
}
function normalizeKey(value) {
  const key = value.trim().replace(/^Bearer\s+/, '').trim();
  if (/[^\x21-\x7e]/.test(key)) throw new Error('API key contains spaces or unsupported characters. Paste only the key.');
  return key;
}
function needsKey(p) { return p && p.authMode !== 'none' && p.kind !== 'ollama'; }
function renderChatModel() {
  const sel = $('chat-model'); sel.replaceChildren();
  if (profile && profile.models && profile.models.length) {
    for (const m of profile.models) sel.add(new Option(m.name && m.name !== m.id ? m.name + ' · ' + m.id : m.id, m.id));
    if (profile.models.some(m => m.id === profile.model)) sel.value = profile.model;
    else { profile.model = profile.models[0].id; sel.value = profile.model; try { persistProfile(); } catch (_) {} }
  } else {
    sel.add(new Option(profile ? '⚙ Set up models…' : '⚙ Set up your model…', ''));
  }
}

/* ---------------- chats ---------------- */
function saveChats() {
  try {
    localStorage.setItem('forge.chats.v1', JSON.stringify(chats.slice(0, 50)));
    localStorage.setItem('forge.chat.current', currentChatId || '');
  } catch (_) {}
}
function loadChats() {
  try {
    const raw = JSON.parse(localStorage.getItem('forge.chats.v1') || '[]');
    chats = (Array.isArray(raw) ? raw : []).filter(c => c && c.id && Array.isArray(c.messages)).slice(0, 50);
    currentChatId = localStorage.getItem('forge.chat.current') || '';
  } catch (_) { chats = []; currentChatId = null; }
  if (!chats.some(c => c.id === currentChatId)) currentChatId = chats[0] ? chats[0].id : null;
  if (!currentChatId) { const c = newChat(false); currentChatId = c.id; }
}
function newChat(render = true) {
  const c = {id: 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7), title: 'New chat', messages: [], updatedAt: Date.now()};
  chats.unshift(c); currentChatId = c.id; saveChats();
  if (render) { renderChats(); switchChat(c.id); }
  return c;
}
function currentChat() { return chats.find(c => c.id === currentChatId) || null; }
function renderChats() {
  const list = $('chats-list'); list.replaceChildren();
  for (const c of chats) {
    const row = document.createElement('div'); row.className = 'chat-row' + (c.id === currentChatId ? ' active' : '');
    const b = document.createElement('button'); b.className = 'chat-title'; b.textContent = c.title || 'New chat';
    b.onclick = () => { switchChat(c.id); closeDrawer(); };
    const del = document.createElement('button'); del.className = 'icon-btn chat-del'; del.setAttribute('aria-label', 'Delete chat'); del.textContent = '✕';
    del.onclick = e => {
      e.stopPropagation();
      chats = chats.filter(x => x.id !== c.id);
      if (currentChatId === c.id) { currentChatId = null; if (!chats.length) newChat(false); else currentChatId = chats[0].id; }
      saveChats(); renderChats(); switchChat(currentChatId);
    };
    row.append(b, del); list.append(row);
  }
}
function welcomeMarkup() {
  return '<div id="welcome"><div class="hero-icon">&gt;_</div><span class="eyebrow">LESS TYPING. MORE BUILDING.</span>' +
    '<h1>What should<br><em>we build?</em></h1>' +
    '<p>Pick a model below, attach a file if you like, and just talk.</p>' +
    '<div class="suggestions">' +
    '<button data-prompt="Look at this project and explain what it does. Make no changes."><div>Explain this project</div></button>' +
    '<button data-prompt="Look at this project, find a concrete bug, and fix it with a test."><div>Find and fix a bug</div></button>' +
    '<button data-prompt="What is the most valuable missing test in this project? Ask before writing it."><div>Suggest a test</div></button>' +
    '</div><div class="trust"><span>◇</span> You approve edits, commands, and tool calls before they run.</div></div>';
}
function switchChat(id) {
  currentChatId = id; saveChats(); renderChats();
  const c = currentChat();
  $('feed').innerHTML = c && c.messages.length ? '' : welcomeMarkup();
  if (c) for (const m of c.messages) renderEvent({type: m.role, text: m.content});
  $('approvals').replaceChildren(); pendingSignature = '';
  const viewing = activeRun && activeRun.chatId === id;
  setBusy(!!viewing);
  $('run-status').textContent = viewing ? 'Agent is working…' : (c && c.messages.length ? '' : 'Pick a model to begin');
}
function openDrawer() { $('chats-drawer').hidden = false; $('drawer-scrim').hidden = false; }
function closeDrawer() { $('chats-drawer').hidden = true; $('drawer-scrim').hidden = true; }

/* ---------------- connection ---------------- */
function pair() { if (native) window.ForgeNative.configure(); else $('pair-dialog').showModal(); }
async function connect() {
  if (!native && !browserToken) {
    try { browserToken = localStorage.getItem('forge.device.token') || ''; } catch (_) {}
  }
  try {
    const h = await api('/api/health'); connected = true;
    sharedActive = !!h.shared;
    $('connection').classList.add('connected'); $('connection').querySelector('span').textContent = 'Online';
    $('run-status').textContent = 'Ready';
    if (sharedActive) {
      const dc = $('device-card'), rc = $('railway-card');
      if (dc) dc.hidden = true; if (rc) rc.hidden = true; // the backend is invisible by design
    }
    toast('Runner online');
    if (githubToken) { try { await api('/api/github', 'POST', {token: githubToken}); } catch (_) {} }
    pullProviderKeys().finally(() => { if (typeof refreshGitHub === 'function') refreshGitHub(); });
  } catch (e) {
    connected = false; sharedActive = false;
    $('connection').classList.remove('connected'); $('connection').querySelector('span').textContent = 'Connect';
    const wantProvision = SHARED_URL && !provisionAttempted &&
      (native ? /pair/i.test(e.message || '') : sharedSameOrigin());
    if (wantProvision) return provisionShared();
    toast(e.message);
  }
}

/* ---------------- provider key backup (api.json on the runner) ---------------- */
async function pushProviderKey(force) {
  if (!connected || !profile || !profileKey) { if (force) throw new Error('Nothing to save.'); return; }
  try { await api('/api/provider-keys', 'POST', {name: profile.name, url: profile.baseUrl, key: profileKey}); }
  catch (e) { if (force) throw e; /* backup is best-effort otherwise */ }
}
async function pullProviderKeys() {
  if (sharedActive) return; // shared backend: keys live on the backend by design, never on the device
  if (!connected || !profile || profileKey) return;
  try {
    const r = await api('/api/provider-keys');
    const match = (r.providers || []).find(p => p.url === profile.baseUrl);
    if (match && match.key) {
      profileKey = match.key;
      try { persistProfile(); } catch (_) {}
      toast('API key restored from your runner backup.');
    }
  } catch (_) {}
}

/* ---------------- github ---------------- */
async function refreshGitHub() {
  const badge = $('github-badge'), status = $('github-status');
  if (!badge) return;
  if (!connected) {
    badge.textContent = '…'; status.textContent = 'Connect a runner first, then link GitHub.';
    $('github-form').hidden = false; $('github-disconnect').hidden = true; return;
  }
  try {
    const r = await api('/api/github');
    badge.textContent = r.connected ? 'CONNECTED' : 'NOT CONNECTED';
    $('github-form').hidden = r.connected;
    $('github-disconnect').hidden = !r.connected;
    status.textContent = r.connected
      ? 'Connected as @' + r.login + '. The agent can read repos and open pull requests.'
      : 'Connect GitHub so the agent can read your repositories and open pull requests.';
  } catch (e) { badge.textContent = 'ERROR'; status.textContent = e.message; }
}
async function connectGitHub() {
  if (!connected) return pair();
  const tok = $('github-token').value.trim();
  if (tok.length < 20) return toast('Paste a GitHub personal access token.');
  try {
    const r = await api('/api/github', 'POST', {token: tok});
    githubToken = tok; $('github-token').value = '';
    try { persistProfile(); } catch (e) { toast(e.message); }
    toast('GitHub connected as @' + r.login);
    refreshGitHub();
  } catch (e) { toast(e.message); }
}
async function disconnectGitHub() {
  try { await api('/api/github/disconnect', 'POST', {}); } catch (_) {}
  githubToken = '';
  try { persistProfile(); } catch (_) {}
  refreshGitHub(); toast('GitHub disconnected.');
}

/* ---------------- skills ---------------- */
let selectedSkills = [];
try { selectedSkills = JSON.parse(localStorage.getItem('forge.skills.selected') || '[]'); } catch (_) { selectedSkills = []; }
if (!Array.isArray(selectedSkills)) selectedSkills = [];
function updateSkillsBtn() {
  const b = $('skills-btn');
  b.classList.toggle('on', selectedSkills.length > 0);
  b.title = selectedSkills.length ? 'Skills: ' + selectedSkills.join(', ') : 'Skills';
}
async function openSkills() {
  const list = $('skills-list'); list.replaceChildren();
  let skills = [];
  if (connected) {
    try { skills = (await api('/api/skills')).skills || []; }
    catch (e) { list.textContent = 'Could not load skills: ' + e.message; }
  } else list.textContent = 'Connect a runner to load skills.';
  selectedSkills = selectedSkills.filter(n => skills.some(s => s.name === n));
  for (const s of skills) {
    const label = document.createElement('label'); label.className = 'skill-pick';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = selectedSkills.includes(s.name);
    cb.onchange = () => {
      selectedSkills = cb.checked ? [...new Set([...selectedSkills, s.name])] : selectedSkills.filter(n => n !== s.name);
      try { localStorage.setItem('forge.skills.selected', JSON.stringify(selectedSkills)); } catch (_) {}
      updateSkillsBtn();
    };
    const span = document.createElement('span');
    const strong = document.createElement('strong'); strong.textContent = s.name;
    const small = document.createElement('small'); small.textContent = s.description || '';
    span.append(strong, small); label.append(cb, span); list.append(label);
  }
  if (connected && !skills.length) list.textContent = 'No skills on this runner.';
  updateSkillsBtn();
  $('skills-dialog').showModal();
}

/* ---------------- rendering ---------------- */
function renderEvent(e) {
  const div = document.createElement('div');
  if (['user', 'assistant', 'error'].includes(e.type)) {
    div.className = 'message ' + e.type;
    const label = document.createElement('div'); label.className = 'message-label';
    label.textContent = e.type === 'user' ? 'You' : e.type === 'error' ? 'Something needs attention' : 'Forge';
    const body = document.createElement('div'); body.className = 'message-text'; body.textContent = e.text;
    div.append(label, body);
    if (e.type === 'error') { const b = document.createElement('button'); b.className = 'secondary'; b.textContent = 'Check model setup'; b.onclick = () => openSetup(); div.append(b); }
  } else if (e.type === 'tool') {
    const detail = document.createElement('details'); detail.className = 'tool-event';
    const summary = document.createElement('summary'); summary.textContent = '⌘ ' + (e.tool || 'Tool output');
    const pre = document.createElement('pre'); pre.textContent = e.text; detail.append(summary, pre); div.append(detail);
  } else { div.className = 'status-event'; div.textContent = '· ' + e.text; }
  const w = $('welcome'); if (w) w.remove();
  $('feed').append(div);
}
function renderApprovals(items) {
  const signature = JSON.stringify(items.map(i => i.id)); if (signature === pendingSignature) return;
  pendingSignature = signature; $('approvals').replaceChildren();
  for (const item of items) {
    const card = document.createElement('div'); card.className = 'approval-card';
    const h = document.createElement('h3'); h.textContent = item.title;
    const pre = document.createElement('pre'); pre.textContent = item.details;
    const note = document.createElement('p'); note.className = 'hint'; note.textContent = 'Waiting for your decision · expires in 10 minutes';
    card.append(h, pre, note);
    for (const allow of [true, false]) {
      const b = document.createElement('button'); b.className = allow ? 'primary' : 'secondary'; b.textContent = allow ? 'Approve' : 'Deny';
      b.onclick = async () => {
        card.querySelectorAll('button').forEach(x => x.disabled = true);
        try { await api('/api/runs/' + activeRun.id + '/approve', 'POST', {id: item.id, allow}); schedulePoll(50); }
        catch (e) { toast(e.message); card.querySelectorAll('button').forEach(x => x.disabled = false); }
      };
      card.append(b);
    }
    $('approvals').append(card);
  }
}
function setBusy(busy) { $('send').disabled = busy; $('stop').hidden = !busy; }

/* ---------------- run loop ---------------- */
function schedulePoll(delay = 900) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, delay); }
async function poll() {
  const run = activeRun; if (!run) return;
  try {
    const r = await api('/api/runs/' + run.id); if (activeRun !== run) return;
    run.events = r.events;
    const viewing = run.chatId === currentChatId;
    if (viewing) {
      if (run.rendered === 0) { $('feed').replaceChildren(); for (const m of run.history) renderEvent({type: m.role, text: m.content}); }
      const feed = $('feed');
      const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 100;
      for (let i = run.rendered; i < r.events.length; i++) renderEvent(r.events[i]);
      if (atBottom) feed.scrollTop = feed.scrollHeight;
      renderApprovals(r.pending);
    }
    run.rendered = r.events.length;
    const busy = ['running', 'approval'].includes(r.status);
    if (viewing) {
      setBusy(busy);
      $('run-status').textContent = r.status === 'approval' ? 'Your approval is needed' : r.status === 'running' ? 'Forge is working…' : 'Done';
    }
    if (busy) { schedulePoll(); return; }
    // Terminal: fold the run's messages into its chat.
    const chat = chats.find(c => c.id === run.chatId);
    if (chat) {
      const fresh = r.events.filter(e => ['user', 'assistant'].includes(e.type)).map(e => ({role: e.type, content: e.text}));
      chat.messages = run.history.concat(fresh);
      chat.updatedAt = Date.now(); saveChats(); renderChats();
    }
    activeRun = null;
    if (viewing) { setBusy(false); $('run-status').textContent = r.status === 'done' ? 'Done' : 'Task ' + r.status; }
  } catch (e) {
    if (activeRun !== run) return;
    if (e.status === 401 || e.status === 404) {
      activeRun = null; setBusy(false);
      $('run-status').textContent = e.status === 401 ? 'Reconnect your runner' : 'Task expired after runner restart';
      if (e.status === 401) { connected = false; $('connection').classList.remove('connected'); $('connection').querySelector('span').textContent = 'Connect'; }
      if (run.chatId === currentChatId) renderEvent({type: 'error', text: e.message});
    } else { $('run-status').textContent = 'Connection lost · retrying'; schedulePoll(4000); }
  }
}
async function sendTask() {
  let prompt = $('prompt').value.trim();
  if (!prompt && !pendingAttachments.length) return toast('Write a message or attach a file first.');
  if (activeRun) return toast('Forge is already working on a task.');
  if (!connected) return pair();
  if (!profile || !profile.model) { openSetup(); return toast('Set up your model first — it takes a minute.'); }
  if (needsKey(profile) && !profileKey && !sharedActive) { openSetup(); setupNotice('Re-enter your API key to continue.', 'error'); return; }
  const chat = currentChat(); if (!chat) return;
  const history = chat.messages.slice(-20);
  // On the shared backend the runner injects the key from api.json; the app never holds it.
  const fullProfile = sharedActive ? {...profile} : {...profile, apiKey: profileKey};
  setBusy(true); $('run-status').textContent = pendingAttachments.length ? 'Uploading attachments…' : 'Starting…';
  try {
    const parts = [];
    for (const a of pendingAttachments) {
      if (a.kind === 'text' && a.size <= 200 * 1024) {
        parts.push('[Attached file: ' + a.name + ']\n```\n' + decodeB64Text(a.b64) + '\n```');
      } else {
        const r = await api('/api/uploads', 'POST', {name: a.name, content: a.b64});
        parts.push('[Attached file: ' + r.path + ' (' + a.kind + ', ' + formatSize(a.size) + ') — saved to your workspace; read it with your file tools if it is relevant.]');
      }
    }
    if (parts.length) prompt = parts.join('\n\n') + (prompt ? '\n\n' + prompt : '');
    if (!prompt.trim()) throw new Error('Write a message first.');
    const r = await api('/api/runs', 'POST', {prompt, profile: fullProfile, history, skills: selectedSkills});
    activeRun = {id: r.id, chatId: chat.id, rendered: 0, events: [], history};
    if (!chat.messages.length) chat.title = ($('prompt').value.trim() || pendingAttachments.map(a => a.name).join(', ')).slice(0, 42) || 'New chat';
    $('prompt').value = ''; clearAttachments(); $('feed').replaceChildren(); $('approvals').replaceChildren();
    for (const m of history) renderEvent({type: m.role, text: m.content});
    pendingSignature = ''; saveChats(); renderChats(); schedulePoll(50);
  } catch (e) {
    setBusy(false); $('run-status').textContent = 'Ready'; toast(e.message);
    if (e.status === 401) { connected = false; $('connection').classList.remove('connected'); $('connection').querySelector('span').textContent = 'Connect'; }
  }
}

/* ---------------- attachments ---------------- */
const MAX_ATTACH = 5 * 1024 * 1024;
const BINARY_EXTS = new Set(['zip','pdf','png','jpg','jpeg','gif','webp','mp3','mp4','mov','exe','apk','aab','dmg','iso','ttf','otf','woff','woff2','sqlite','db','pyc','class']);
function formatSize(n) { return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }
function looksText(bytes) {
  if (typeof TextDecoder === 'undefined') {
    for (let i = 0; i < bytes.length; i++) { const b = bytes[i]; if (b === 0 || b === 27 || (b < 32 && b !== 9 && b !== 10 && b !== 13)) return false; }
    return true;
  }
  try { return new TextDecoder('utf-8', {fatal: true}).decode(bytes).indexOf('\0') === -1; }
  catch (_) { return false; }
}
function classifyAttachment(name, size, bytes) {
  if (!(bytes instanceof Uint8Array)) bytes = new Uint8Array(bytes);
  if (size > MAX_ATTACH) throw new Error('"' + name + '" is over the 5 MB per-file limit.');
  let kind = looksText(bytes) ? 'text' : 'binary';
  const ext = name.split('.').pop().toLowerCase();
  if (BINARY_EXTS.has(ext)) kind = 'binary';
  let bin = '';
  for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
  return {name, size, kind, b64: btoa(bin)};
}
function decodeB64Text(b64) {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  if (typeof TextDecoder !== 'undefined') return new TextDecoder().decode(bytes);
  let s = '';
  for (let i = 0; i < bytes.length; i += 8192) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
  return s;
}
function renderAttachChips() {
  const chip = $('attach-chip');
  if (!pendingAttachments.length) { chip.hidden = true; return; }
  chip.hidden = false; chip.replaceChildren();
  for (const a of pendingAttachments) {
    const tag = document.createElement('span'); tag.className = 'attach-tag';
    tag.textContent = a.name + ' · ' + formatSize(a.size);
    const x = document.createElement('button'); x.className = 'icon-btn'; x.setAttribute('aria-label', 'Remove ' + a.name); x.textContent = '✕';
    x.onclick = () => { pendingAttachments = pendingAttachments.filter(p => p !== a); renderAttachChips(); };
    tag.append(x); chip.append(tag);
  }
}
function clearAttachments() { pendingAttachments = []; renderAttachChips(); $('attach-input').value = ''; }

/* ---------------- setup dialog ---------------- */
function setupNotice(message, tone = 'info') {
  const n = $('setup-notice'); n.textContent = message || ''; n.dataset.tone = tone; n.hidden = !message;
}
function fillPresets() {
  const sel = $('setup-preset'); sel.replaceChildren();
  for (const [id, p] of Object.entries(presets)) sel.add(new Option(p.name, id));
}
function applyPreset(id) {
  const p = presets[id] || presets.custom;
  $('setup-name').value = p.name; $('setup-url').value = p.baseUrl;
  $('setup-kind').value = p.kind; $('setup-auth').value = p.authMode;
  $('setup-header-row').hidden = p.authMode !== 'custom';
  updateSetupEndpoint();
}
function openSetup() {
  setupRevision++; setupModels = profile ? [...(profile.models || [])] : []; setupNotice('');
  const p = profile || presets.custom;
  let presetId = 'custom';
  for (const [id, pr] of Object.entries(presets)) {
    if (pr.baseUrl && pr.baseUrl === p.baseUrl && pr.kind === p.kind) { presetId = id; break; }
  }
  $('setup-preset').value = presetId;
  $('setup-name').value = p.name || ''; $('setup-url').value = p.baseUrl || ''; $('setup-key').value = profileKey || '';
  $('setup-kind').value = p.kind || 'custom'; $('setup-auth').value = p.authMode || 'auto';
  $('setup-header').value = p.customHeader || ''; $('setup-header-row').hidden = ($('setup-auth').value !== 'custom');
  renderSetupModels(); updateSetupEndpoint();
  $('setup-dialog').showModal();
}
function renderSetupModels() {
  const sel = $('setup-model'); sel.replaceChildren();
  $('setup-model-row').hidden = !setupModels.length;
  for (const m of setupModels) sel.add(new Option(m.name && m.name !== m.id ? m.name + ' · ' + m.id : m.id, m.id));
  if (profile && profile.model && setupModels.some(m => m.id === profile.model)) sel.value = profile.model;
}
function setupProfile() {
  const kind = $('setup-kind').value;
  return {
    name: $('setup-name').value.trim(), kind, protocol: protocols[kind], baseUrl: $('setup-url').value.trim(),
    apiKey: normalizeKey($('setup-key').value), authMode: $('setup-auth').value,
    customHeader: $('setup-auth').value === 'custom' ? $('setup-header').value.trim() : '',
    models: [...setupModels], model: $('setup-model').value || '', validation: (profile && profile.validation) || null
  };
}
function updateSetupEndpoint() {
  const el = $('setup-endpoint');
  try {
    const kind = protocols[$('setup-kind').value];
    let b = $('setup-url').value.trim().replace(/\/+$/, ''); const parsed = new URL(b);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error();
    const suffix = ['/chat/completions', '/api/chat', '/api/tags', '/messages', '/responses', '/models'].find(s => b.endsWith(s));
    if (suffix) b = b.slice(0, -suffix.length);
    if (kind === 'ollama') b = b.replace(/\/api$/, '');
    else if (kind === 'gemini') b = b.replace(/:(streamGenerateContent|generateContent)$/, '').replace(/\/models\/[^/]+$/, '');
    else if (kind === 'azure') b = b.replace(/\/openai(\/deployments\/.*)?$/, '');
    else if (new URL(b).pathname === '/' && !suffix) b += '/v1';
    const model = $('setup-model').value || '{model}';
    const chat = kind === 'anthropic' ? '/messages' : kind === 'responses' ? '/responses' : kind === 'ollama' ? '/api/chat' :
      kind === 'gemini' ? '/v1beta/models/' + model + ':generateContent' :
      kind === 'azure' ? '/openai/deployments/' + model + '/chat/completions' : '/chat/completions';
    el.textContent = 'Chat endpoint: ' + b + chat;
  } catch (_) { el.textContent = 'Enter the API base URL from your provider\u2019s documentation.'; }
}
async function setupRequest(action, p) {
  if (connected) return api('/api/providers/' + action, 'POST', p);
  if (native) return nativeCall(action === 'test' ? 'testProvider' : 'discoverModels', JSON.stringify(p));
  throw new Error('Connect your runner first, or use the Android app for direct checks.');
}
function setSetupBusy(busy) {
  setupBusy = busy;
  ['setup-discover', 'setup-test', 'setup-save'].forEach(id => $(id).disabled = busy);
}
async function discoverSetupModels() {
  if (setupBusy) return; const revision = ++setupRevision; setSetupBusy(true); setupNotice('Loading models…');
  try {
    const p = setupProfile();
    if (!p.name || !p.baseUrl) throw new Error('Enter a name and API base URL first.');
    const r = await setupRequest('discover', p); if (revision !== setupRevision) return;
    setupModels = r.models || []; p.models = setupModels; p.baseUrl = r.baseUrl || p.baseUrl; p.protocol = r.protocol || p.protocol;
    if (!setupModels.some(m => m.id === p.model)) p.model = setupModels.length ? setupModels[0].id : '';
    profile = {...profile, ...p}; delete profile.apiKey;
    renderSetupModels(); $('setup-url').value = p.baseUrl; updateSetupEndpoint();
    setupNotice(setupModels.length ? setupModels.length + ' models loaded. Pick one, then save.' : 'No models returned. Check the base URL.', setupModels.length ? 'success' : 'error');
  } catch (e) { if (revision === setupRevision) setupNotice(e.message, 'error'); }
  finally { setSetupBusy(false); }
}
async function testSetupChat() {
  if (setupBusy) return; const revision = ++setupRevision; setSetupBusy(true); setupNotice('Sending a small test request…');
  try {
    const p = setupProfile();
    if (!p.model) throw new Error('Discover models and pick one first.');
    if (needsKey(p) && !p.apiKey) throw new Error('Enter your API key first.');
    const r = await setupRequest('test', p); if (revision !== setupRevision) return;
    if (!r.ok) throw new Error('Chat test did not complete.');
    p.validation = {model: p.model, via: connected ? 'runner' : 'phone', at: Date.now()};
    profile = {...profile, ...p}; delete profile.apiKey;
    if (!setupModels.some(m => m.id === p.model)) { setupModels.push({id: p.model, name: p.model}); renderSetupModels(); }
    setupNotice('Chat API accepted the request. Save to start chatting.', 'success');
  } catch (e) { if (revision === setupRevision) setupNotice(e.message, 'error'); }
  finally { setSetupBusy(false); }
}
async function saveSetup() {
  try {
    const p = setupProfile();
    if (!p.name || !p.baseUrl) throw new Error('Enter a name and API base URL.');
    if (!p.model) throw new Error('Discover models and pick one first.');
    if (needsKey(p) && !p.apiKey) throw new Error('Enter your API key.');
    if (!p.models.some(m => m.id === p.model)) p.models.push({id: p.model, name: p.model});
    profileKey = p.apiKey; profile = {...p}; delete profile.apiKey;
    persistProfile(); renderChatModel(); setupRevision++;
    if (sharedActive) {
      // Keys live ONLY on the backend: push, then wipe every local copy.
      await pushProviderKey(true);
      profileKey = '';
      persistProfile();
      $('setup-dialog').close(); toast('Model ready — your key is stored safely on the cloud backend.');
    } else {
      pushProviderKey();
      $('setup-dialog').close(); toast('Model ready. Say hi.');
    }
  } catch (e) { setupNotice(e.message, 'error'); }
}

/* ---------------- pages ---------------- */
function show(page) {
  document.querySelectorAll('.page').forEach(e => e.classList.toggle('active', e.id === page));
  document.querySelectorAll('nav button').forEach(e => e.classList.toggle('active', e.dataset.page === page));
  if (page === 'files' && connected) refreshFiles();
  if (page === 'cloud' && typeof refreshCloud === 'function') refreshCloud();
}

/* ---------------- files (unchanged behavior) ---------------- */
let allFiles = [];
async function refreshFiles() {
  try { const data = await api('/api/files'); allFiles = data.files; filterFiles(); } catch (e) { toast(e.message); }
}
function filterFiles() {
  const q = $('file-filter').value.toLowerCase(); $('file-list').replaceChildren();
  for (const path of allFiles.filter(p => p.toLowerCase().includes(q))) {
    const b = document.createElement('button'); b.className = 'file-row'; b.textContent = '↳ ' + path;
    b.onclick = async () => {
      try {
        const r = await api('/api/file?path=' + encodeURIComponent(path));
        $('file-path').value = path; $('file-content').value = r.content; $('editor-panel').hidden = false;
      } catch (e) { toast(e.message); }
    };
    $('file-list').append(b);
  }
  if (!$('file-list').children.length) $('file-list').textContent = 'No matching files.';
}

/* ---------------- wiring ---------------- */
document.querySelectorAll('nav button').forEach(b => b.onclick = () => show(b.dataset.page));
$('menu').onclick = openDrawer; $('drawer-close').onclick = closeDrawer; $('drawer-scrim').onclick = closeDrawer;
$('drawer-new').onclick = () => { newChat(); closeDrawer(); };
$('feed').addEventListener('click', e => { const b = e.target.closest('[data-prompt]'); if (b) { $('prompt').value = b.dataset.prompt; $('prompt').focus(); } });
$('connection').onclick = pair;
$('close-pair').onclick = () => $('pair-dialog').close();
$('pair-form').onsubmit = async e => { e.preventDefault(); browserToken = $('pair-token').value.trim(); $('pair-token').value = ''; $('pair-dialog').close(); await connect(); };
$('send').onclick = sendTask;
$('prompt').onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !$('send').disabled) { e.preventDefault(); sendTask(); } };
$('stop').onclick = async () => {
  if (!activeRun) return;
  try { await api('/api/runs/' + activeRun.id + '/cancel', 'POST', {}); $('run-status').textContent = 'Stopping…'; }
  catch (e) { toast(e.message); }
};
$('chat-model').onchange = e => {
  if (e.target.value === '') { openSetup(); renderChatModel(); return; }
  if (profile) { try { profile.model = e.target.value; profile.validation = null; persistProfile(); } catch (err) { toast(err.message); } }
};
$('attach').onclick = () => $('attach-input').click();
$('attach-input').onchange = async e => {
  const files = Array.from(e.target.files || []);
  for (const f of files) {
    try {
      const buf = await f.arrayBuffer();
      pendingAttachments.push(classifyAttachment(f.name, f.size, new Uint8Array(buf)));
    } catch (err) { toast(err.message); }
  }
  $('attach-input').value = '';
  renderAttachChips();
  if (pendingAttachments.length) toast(pendingAttachments.length + ' file' + (pendingAttachments.length > 1 ? 's' : '') + ' attached.');
};
$('setup-preset').onchange = e => { setupRevision++; applyPreset(e.target.value); };
$('setup-show-key').onclick = () => {
  const k = $('setup-key'), reveal = k.type === 'password';
  k.type = reveal ? 'text' : 'password'; $('setup-show-key').textContent = reveal ? 'Hide' : 'Show';
};
$('setup-kind').onchange = updateSetupEndpoint;
$('setup-url').oninput = updateSetupEndpoint;
$('setup-model').onchange = updateSetupEndpoint;
$('setup-auth').onchange = () => { $('setup-header-row').hidden = $('setup-auth').value !== 'custom'; };
$('setup-discover').onclick = discoverSetupModels;
$('setup-test').onclick = testSetupChat;
$('setup-save').onclick = saveSetup;
$('setup-cancel').onclick = () => $('setup-dialog').close();
$('github-connect').onclick = connectGitHub;
$('github-disconnect').onclick = disconnectGitHub;
$('github-show').onclick = () => {
  const k = $('github-token'), reveal = k.type === 'password';
  k.type = reveal ? 'text' : 'password'; $('github-show').textContent = reveal ? 'Hide' : 'Show';
};
$('skills-btn').onclick = openSkills;
$('skills-close').onclick = () => $('skills-dialog').close();
$('skills-done').onclick = () => $('skills-dialog').close();
$('refresh-files').onclick = refreshFiles; $('file-filter').oninput = filterFiles;
$('new-file').onclick = () => { $('editor-panel').hidden = false; $('file-path').value = ''; $('file-content').value = ''; $('file-path').focus(); };
$('close-editor').onclick = () => $('editor-panel').hidden = true;
$('save-file').onclick = async () => {
  const path = $('file-path').value.trim(); if (!path) return toast('Enter a relative path.');
  if (!confirm('Save the displayed contents to ' + path + '? Existing contents will be replaced.') || !connected) return;
  try { await api('/api/file', 'POST', {path, content: $('file-content').value}); toast('File saved.'); refreshFiles(); } catch (e) { toast(e.message); }
};
$('fetch-button').onclick = async () => {
  const repo = $('fetch-repo').value.trim(); if (!repo) return toast('Enter a repository as owner/repo.');
  if (!connected) return pair();
  const b = $('fetch-button'); b.disabled = true; b.textContent = 'Fetching…';
  try {
    const r = await api('/api/repos/fetch', 'POST', {repo, ref: $('fetch-ref').value.trim() || 'main', dest: $('fetch-dest').value.trim()});
    toast(r.message); refreshFiles();
  } catch (e) { toast(e.message); } finally { b.disabled = false; b.textContent = 'Fetch repository'; }
};

/* ---------------- init ---------------- */
loadProfile(); loadChats(); fillPresets(); renderChatModel(); renderChats(); switchChat(currentChatId); updateSkillsBtn();
if (native) connect();
else if (sharedSameOrigin()) connect(); // page served by the shared backend: provision silently
